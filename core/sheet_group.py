"""
合圖群組

一張合圖（.png）常被多份 atlas 共用——實測素材裡有兩組「三個 .skel 指向
同一張 png」。這種素材只要有一份 atlas 自己重排版面，另外兩份的座標就全錯，
輸出直接壞掉。

所以編輯的單位不是 atlas，而是 **合圖**：

    合圖群組 = 一張貼圖 + 所有引用它的 atlas + 那些 atlas 所屬的專案

群組內的區塊以「來源頁面矩形」為身分取聯集（同一塊像素在不同 atlas 裡是
同一個矩形，名稱卻可能不同），排出來的版面套用回群組裡的每一份 atlas，
所以三份 atlas 永遠拿到一模一樣的座標。

另外提供 ``cluster_projects``：把有共用貼圖的專案串成一群，讓清單可以把
它們排在一起——共用的素材一起看、一起改，才不會漏掉其中一份。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.constants import PAGE_ALIGN_NONE
from core.packer import PackItem, pack
from models.atlas_data import AtlasPage
from models.sheet_layout import Placement, Rect, SheetLayout, layout_key
from models.spine_asset import SpineAsset
from models.spine_project import SpineProject

# 重新排版時預設保留的元件間距。等比縮小後原本的間距會跟著變小
# （2px 縮一半只剩 1px），擋不住 GPU Linear 取樣跨過邊界，所以補回來
DEFAULT_PADDING = 2


@dataclass
class SheetMember:
    """群組裡的一份 atlas 頁面（同一個 atlas 的不同頁面算不同 member）"""

    project: SpineProject
    asset: SpineAsset
    page: AtlasPage

    @property
    def page_name(self) -> str:
        return self.page.name

    @property
    def atlas_name(self) -> str:
        return self.asset.atlas_path.name


@dataclass
class SheetGroup:
    """一張合圖與所有引用它的 atlas"""

    page_path: Path                      # 貼圖的實際路徑
    src_canvas: tuple[int, int]           # atlas 宣告的頁面尺寸
    members: list[SheetMember] = field(default_factory=list)
    # 區塊聯集：來源矩形 -> 名稱們（依第一次出現的順序）
    regions: dict[Rect, list[str]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 摘要

    @property
    def key(self) -> str:
        return layout_key(self.page_path)

    @property
    def name(self) -> str:
        return self.page_path.name

    @property
    def is_shared(self) -> bool:
        """被兩份以上的 atlas 或兩份以上的專案共用"""
        return len(self.atlas_names) > 1 or len(self.project_names) > 1

    @property
    def atlas_names(self) -> list[str]:
        seen: list[str] = []
        for member in self.members:
            if member.atlas_name not in seen:
                seen.append(member.atlas_name)
        return seen

    @property
    def project_names(self) -> list[str]:
        seen: list[str] = []
        for member in self.members:
            if member.project.name not in seen:
                seen.append(member.project.name)
        return seen

    @property
    def projects(self) -> list[SpineProject]:
        result: list[SpineProject] = []
        for member in self.members:
            if not any(p is member.project for p in result):
                result.append(member.project)
        return result

    @property
    def region_count(self) -> int:
        return len(self.regions)

    @property
    def source_bytes(self) -> int:
        try:
            return self.page_path.stat().st_size
        except OSError:
            return 0

    @property
    def can_edit(self) -> bool:
        return not self.conflicts and bool(self.regions) and self.page_path.is_file()

    def describe_usage(self) -> str:
        atlases = len(self.atlas_names)
        projects = len(self.project_names)
        if atlases <= 1 and projects <= 1:
            return "單獨使用"
        return f"{atlases} 份 atlas / {projects} 份專案共用"

    # ------------------------------------------------------------ 版面

    def build_layout(
        self,
        scale: float = 1.0,
        padding: int = DEFAULT_PADDING,
        align: int = PAGE_ALIGN_NONE,
    ) -> SheetLayout:
        """
        建一份初始版面：所有元件同一個比例，位置尚未決定（呼叫 repack 排）。

        元件順序固定為「來源座標由上到下、由左到右」，讓編輯器的清單與
        每次重建的結果都穩定。
        """
        layout = SheetLayout(
            page_path=self.page_path,
            src_canvas=self.src_canvas,
            padding=max(0, padding),
            align=align,
        )
        for rect in sorted(self.regions, key=lambda r: (r[1], r[0])):
            placement = Placement(src_rect=rect, names=list(self.regions[rect]))
            placement.set_scale(scale)
            layout.placements.append(placement)
        repack(layout, hint_width=self.src_canvas[0])
        return layout

    def sync_layout(self, layout: SheetLayout) -> list[str]:
        """
        把既有版面對齊到目前的區塊聯集（素材換過、或載入舊專案檔時）。

        回傳說明文字；沒有差異時回傳空清單。版面裡多出來的元件會被移除，
        少掉的會以「群組目前的共同比例」補上。
        """
        notes: list[str] = []
        existing = layout.by_rect()
        wanted = set(self.regions)

        stale = [rect for rect in existing if rect not in wanted]
        if stale:
            layout.placements = [p for p in layout.placements if p.src_rect in wanted]
            notes.append(f"移除 {len(stale)} 個已不存在的元件")

        missing = [rect for rect in self.regions if rect not in existing]
        if missing:
            base = layout.uniform_scale
            if base is None:
                base = (
                    sum(p.scale for p in layout.placements) / len(layout.placements)
                    if layout.placements else 1.0
                )
            for rect in sorted(missing, key=lambda r: (r[1], r[0])):
                placement = Placement(src_rect=rect, names=list(self.regions[rect]))
                placement.set_scale(base)
                layout.placements.append(placement)
            notes.append(f"新增 {len(missing)} 個元件（比例 {base * 100:g}%）")

        # 名稱可能因為新增了共用的 atlas 而變多
        for placement in layout.placements:
            names = self.regions.get(placement.src_rect)
            if names is not None and names != placement.names:
                placement.names = list(names)

        if layout.src_canvas != self.src_canvas:
            notes.append(
                f"原頁面尺寸由 {layout.src_canvas[0]}x{layout.src_canvas[1]} "
                f"改為 {self.src_canvas[0]}x{self.src_canvas[1]}"
            )
            layout.src_canvas = self.src_canvas

        if notes:
            repack(layout, hint_width=self.src_canvas[0])
        return notes


# ---------------------------------------------------------------- 排版


def repack(layout: SheetLayout, hint_width: int = 0) -> list[Placement]:
    """
    重新排版並把畫布縮到最小；回傳排不進去的元件（正常情況是空的）。

    手動搬過的元件（``pinned``）位置固定，其餘自動填空隙。
    """
    items = [
        PackItem(
            key=placement,
            width=placement.dst_size[0],
            height=placement.dst_size[1],
            fixed=placement.pos if placement.pinned else None,
        )
        for placement in layout.placements
    ]
    result = pack(
        items,
        padding=layout.padding,
        align=layout.align,
        hint_width=hint_width or layout.src_canvas[0],
    )
    for item in items:
        position = result.positions.get(id(item))
        if position is not None:
            item.key.pos = position  # type: ignore[union-attr]
    layout.canvas = result.canvas
    return [item.key for item in result.overflow]  # type: ignore[misc]


def repack_fixed(layout: SheetLayout, canvas: tuple[int, int]) -> list[Placement]:
    """排進指定的畫布（使用者手動指定尺寸時）；回傳塞不下的元件。"""
    items = [
        PackItem(
            key=placement,
            width=placement.dst_size[0],
            height=placement.dst_size[1],
            fixed=placement.pos if placement.pinned else None,
        )
        for placement in layout.placements
    ]
    result = pack(items, padding=layout.padding, align=1, fixed_canvas=canvas)
    for item in items:
        position = result.positions.get(id(item))
        if position is not None:
            item.key.pos = position  # type: ignore[union-attr]
    layout.canvas = canvas
    return [item.key for item in result.overflow]  # type: ignore[misc]


# ---------------------------------------------------------------- 群組建立


def build_sheet_groups(projects: list[SpineProject]) -> list[SheetGroup]:
    """
    掃出所有合圖群組（一張貼圖一組），依貼圖路徑排序。

    找不到貼圖檔、或 atlas 無法解析的頁面會略過——這些專案本來就不能處理。
    """
    groups: dict[str, SheetGroup] = {}

    for project in projects:
        for asset in project.atlases:
            if asset.atlas is None:
                continue
            for page in asset.atlas.pages:
                path = asset.pages.get(page.name)
                if path is None:
                    continue
                key = layout_key(path)
                group = groups.get(key)
                if group is None:
                    group = SheetGroup(page_path=path, src_canvas=page.size)
                    groups[key] = group
                elif group.src_canvas != page.size:
                    group.conflicts.append(
                        f"{asset.atlas_path.name} 宣告 {page.size[0]}x{page.size[1]}，"
                        f"但 {group.members[0].atlas_name} 宣告 "
                        f"{group.src_canvas[0]}x{group.src_canvas[1]}"
                    )
                group.members.append(SheetMember(project=project, asset=asset, page=page))
                _merge_regions(group, page)

    return sorted(groups.values(), key=lambda g: str(g.page_path).lower())


def _merge_regions(group: SheetGroup, page: AtlasPage) -> None:
    """把一頁的區塊併進群組的聯集（以來源矩形為身分）"""
    for region in page.regions:
        rect = region.page_rect
        names = group.regions.setdefault(rect, [])
        if region.name not in names:
            names.append(region.name)


def groups_for_project(
    groups: list[SheetGroup], project: SpineProject
) -> list[SheetGroup]:
    """這份專案用到的合圖群組（依原順序）"""
    return [g for g in groups if any(m.project is project for m in g.members)]


# ---------------------------------------------------------------- 專案分群


def cluster_projects(projects: list[SpineProject]) -> dict[int, int]:
    """
    把有共用貼圖的專案串成一群（傳遞性：A 與 B 共用、B 與 C 共用 → 同一群）。

    Returns:
        ``id(project) -> 群組編號``；編號依「該群最小的排序鍵」決定，
        所以清單套用後同一群一定相鄰，整體順序仍然接近字母序。
    """
    parent: dict[int, int] = {id(p): id(p) for p in projects}

    def find(key: int) -> int:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: dict[str, int] = {}
    for project in projects:
        for path in project.page_paths:
            key = layout_key(path)
            if key in owner:
                union(owner[key], id(project))
            else:
                owner[key] = id(project)

    return {id(p): find(id(p)) for p in projects}


def shared_page_paths(projects: list[SpineProject]) -> dict[str, list[SpineProject]]:
    """被兩份以上專案共用的貼圖 -> 那些專案"""
    users: dict[str, list[SpineProject]] = {}
    for project in projects:
        for path in project.page_paths:
            users.setdefault(layout_key(path), []).append(project)
    return {key: found for key, found in users.items() if len(found) > 1}
