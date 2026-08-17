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

from config.constants import MAX_PAGE_SIZE, PAGE_ALIGN_NONE
from core.packer import PackItem, pack
from core.rect_mapper import align_up, round_half_up
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
        建一份初始版面：所有元件同一個比例。

        元件順序固定為「來源座標由上到下、由左到右」，讓編輯器的清單與
        每次重建的結果都穩定。

        比例是 100% 時**直接沿用原始版面**（位置與畫布都照原檔），不重新排版：
        什麼都還沒改，版面就不該有任何變化——重排有可能排出比原檔更大的畫布
        （原檔是美術工具花更多時間排的），「還沒動就先變大」絕對不能發生。
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

        aligned_src = (
            align_up(self.src_canvas[0], align), align_up(self.src_canvas[1], align)
        )
        if abs(scale - 1.0) < 1e-9 and aligned_src == self.src_canvas:
            for placement in layout.placements:
                placement.pos = (placement.src_rect[0], placement.src_rect[1])
            layout.canvas = self.src_canvas
        else:
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
    重新排版並把每一頁的畫布縮到最小；回傳排不進去的元件（正常情況是空的）。

    版面可以拆成多個輸出頁（``placement.page``），每一頁**獨立**排版：
    手動搬過的元件（``pinned``）位置固定，其餘自動填空隙。
    沒有任何元件的頁維持與原圖一樣大——新增的空白頁在使用者丟入元件
    之前不縮排，才有地方可以放。

    「原始版面等比縮小」永遠是候選之一：整組同一個比例時，把原版面照比例
    縮小也是一個合法排法（原檔是美術工具排的，常比啟發式排得更緊）。
    兩者比面積取小，所以結果**永遠不會比原版面差**——100% 時該候選就是
    原版面本身，保證面積不變大。拆頁後原版面的相對位置不再成立，不做此比較。
    """
    hint = hint_width or layout.src_canvas[0]
    overflow: list[Placement] = []
    for index in range(layout.page_count):
        members = layout.placements_on(index)
        if not members:
            layout.set_page_canvas(index, layout.src_canvas)
            continue
        items = [
            PackItem(
                key=placement,
                width=placement.dst_size[0],
                height=placement.dst_size[1],
                fixed=placement.pos if placement.pinned else None,
            )
            for placement in members
        ]
        result = pack(items, padding=layout.padding, align=layout.align, hint_width=hint)
        for item in items:
            position = result.positions.get(id(item))
            if position is not None:
                item.key.pos = position  # type: ignore[union-attr]
        layout.set_page_canvas(index, result.canvas)
        overflow.extend(item.key for item in result.overflow)  # type: ignore[misc]
    if not overflow and not layout.extra_pages:
        _prefer_source_baseline(layout)
    # 固定住的元件互相壓到時打包器不會動它們——重疊絕不允許，最後強制排開
    resolve_overlaps(layout)
    return overflow


def _clashing_ids(members: list[Placement]) -> set[int]:
    """同一頁內互相重疊的元件 id 集合（呼叫端保證 members 同頁且已排版）"""
    clashed: set[int] = set()
    for index, first in enumerate(members):
        ax, ay, aw, ah = first.dst_rect
        for second in members[index + 1:]:
            bx, by, bw, bh = second.dst_rect
            if bx >= ax + aw or bx + bw <= ax or by >= ay + ah or by + bh <= ay:
                continue
            clashed.add(id(first))
            clashed.add(id(second))
    return clashed


def overlapping_placements(layout: SheetLayout) -> list[Placement]:
    """
    找出互相重疊的元件（同一頁才算數；不同輸出頁不可能互相干擾）。

    元件以「來源矩形」為身分、建群組時已去重，所以兩個元件疊在同一個
    位置**必然**是錯的（畫的是不同來源的像素）——完全重合也算重疊。
    atlas 區塊層級的「打包器去重別名」在元件層已被合併成同一個元件，
    不會出現在這裡。
    """
    result: list[Placement] = []
    for index in range(layout.page_count):
        members = [p for p in layout.placements_on(index) if p.pos is not None]
        clashed = _clashing_ids(members)
        result.extend(p for p in members if id(p) in clashed)
    return result


def resolve_overlaps(
    layout: SheetLayout, keep: list[Placement] | None = None
) -> list[Placement]:
    """
    把重疊的元件排開——**元件不可重疊**是這個工具最嚴重的規定
    （重疊的區塊會讓 UV 取到別張圖），任何操作結束後都不允許存在。

    ``keep`` 是剛被使用者放下的元件：位置優先保住，被壓到的讓開；
    keep 之間互相壓到時只保得住排在前面的。被搬動的元件會取消固定
    （那個位置已經不是使用者挑的）。沒有重疊時不動任何東西。

    Returns:
        實際被搬動的元件。
    """
    keep_ids = {id(p) for p in (keep or [])}
    moved: list[Placement] = []
    for index in range(layout.page_count):
        members = [p for p in layout.placements_on(index) if p.pos is not None]
        involved = _clashing_ids(members)
        if not involved:
            continue
        victim_ids = involved - keep_ids
        # 留在原位的元件彼此不能再重疊（打包器不會動固定的東西）；
        # 仍互撞就把排在後面的降級成要重排的
        while True:
            stay = [p for p in members if id(p) not in victim_ids]
            still = _clashing_ids(stay)
            if not still:
                break
            victim_ids.add(next(id(p) for p in reversed(stay) if id(p) in still))

        items = [
            PackItem(
                key=placement,
                width=placement.dst_size[0],
                height=placement.dst_size[1],
                fixed=None if id(placement) in victim_ids else placement.pos,
            )
            for placement in members
        ]
        result = pack(
            items, padding=layout.padding, align=layout.align,
            hint_width=layout.src_canvas[0],
        )
        if result.overflow:
            # 保住位置會超出頁面上限：整頁自由重排，至少絕不能留下重疊
            victim_ids = {id(p) for p in members}
            items = [
                PackItem(key=p, width=p.dst_size[0], height=p.dst_size[1])
                for p in members
            ]
            result = pack(
                items, padding=layout.padding, align=layout.align,
                hint_width=layout.src_canvas[0],
            )
        for item in items:
            position = result.positions.get(id(item))
            if position is not None:
                item.key.pos = position  # type: ignore[union-attr]
        layout.set_page_canvas(index, result.canvas)
        for placement in members:
            if id(placement) in victim_ids:
                placement.pinned = False
                moved.append(placement)
    return moved


def _prefer_source_baseline(layout: SheetLayout) -> None:
    """
    把「原始版面等比縮小」當候選，比排版結果小（或相等）就採用它。

    只在「整組同一個比例、沒有任何固定位置、沒有拆頁」時有意義：比例不一致、
    有元件被手動搬過或拆到別頁，原版面的相對位置就不再成立。
    相等面積時偏好原版面——與原檔的 diff 最小，肉眼比對也容易。
    """
    scale = layout.uniform_scale
    if scale is None or not layout.placements or layout.extra_pages:
        return
    if any(p.pinned or p.page != 0 for p in layout.placements):
        return

    exact = abs(scale - 1.0) < 1e-9
    placed: list[tuple[Placement, tuple[int, int]]] = []
    rects: list[Rect] = []
    for placement in layout.placements:
        src_x, src_y, _, _ = placement.src_rect
        w, h = placement.dst_size
        x = max(0, round_half_up(src_x * scale))
        y = max(0, round_half_up(src_y * scale))
        placed.append((placement, (x, y)))
        rects.append((x, y, w, h))

    canvas_w = align_up(max(1, round_half_up(layout.src_canvas[0] * scale)), layout.align)
    canvas_h = align_up(max(1, round_half_up(layout.src_canvas[1] * scale)), layout.align)
    for x, y, w, h in rects:
        canvas_w = max(canvas_w, align_up(x + w, layout.align))
        canvas_h = max(canvas_h, align_up(y + h, layout.align))
    if canvas_w > MAX_PAGE_SIZE or canvas_h > MAX_PAGE_SIZE:
        return
    if canvas_w * canvas_h > layout.canvas[0] * layout.canvas[1]:
        return

    # 縮小後元件間的距離也跟著縮，掉到要求的間距以下就會滲色，不能採用。
    # 100% 時像素原封不動（沒有重新取樣），維持原檔的間距即可，不另外要求。
    required = 0 if exact else layout.padding
    for index, first in enumerate(rects):
        ax, ay, aw, ah = first
        for second in rects[index + 1:]:
            if first == second:
                continue  # 打包器去重的別名：多個名稱共用同一塊像素
            bx, by, bw, bh = second
            gap = max(
                max(bx - (ax + aw), ax - (bx + bw)),
                max(by - (ay + ah), ay - (by + bh)),
            )
            if gap < required:
                return

    for placement, position in placed:
        placement.pos = position
    layout.canvas = (canvas_w, canvas_h)


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
