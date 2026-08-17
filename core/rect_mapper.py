"""
Atlas 座標重算

--------------------------------------------------------------------------
為什麼只需要改 .atlas，不需要改 .skel
--------------------------------------------------------------------------
Spine runtime 計算 RegionAttachment 頂點的方式（3.8 ~ 4.x 皆同）：

    regionScaleX = attachment.width / region.origWidth      <- width 來自 .skel
    localX       = -attachment.width / 2 + region.offsetX * regionScaleX
    localX2      = localX + region.sizeWidth * regionScaleX

把 atlas 內 ``xy / size / orig / offset / 頁面尺寸`` 全部同乘 s：
``origWidth`` 變 1/s 倍讓 ``regionScaleX`` 變 s 倍，而 ``offsetX`` 與
``sizeWidth`` 各縮 s 倍，兩者相乘剛好抵銷 → localX 與 localX2 完全不變。

Mesh 也一樣：UV 是以頁面尺寸正規化的比值，頂點座標存在 .skel 且與 atlas 無關。

結論：等比縮貼圖時 **.skel 不能動**，動了才會破圖。

--------------------------------------------------------------------------
真正的誤差來源
--------------------------------------------------------------------------
atlas 座標必須是整數，s = 0.5 碰到奇數就會產生 0.5 px 的餘數。這裡用
「保留邊界」的作法處理：不是各自四捨五入寬高，而是把左右兩個邊界各自
四捨五入後相減得到新寬度。這樣相鄰區塊不會出現重疊或縫隙，累積誤差
也不會沿著頁面往後放大。

另外針對「未裁切」的區塊（offset 為 0 且 orig == size）直接令
新 orig = 新 size，讓最常見的情況做到零誤差。

被裁切的區塊則沒辦法完全精確：Spine 算出來的長度是
``attachment 尺寸 x (size / orig)``，而 size 與 orig 都必須是整數。
例如 ``size=21, orig=22`` 縮一半後理想值是 ``10.5 / 11``，但 21/22 這個
分數無法用更小的整數表示，只能取 10/10 或 10/11，兩者都差約半個像素。
這是「縮小 atlas」本身的數學下限，不是實作問題。

程式的作法是：先決定實際的像素矩形（不可更動），再在候選整數中挑一組
讓 ``size/orig`` 與 ``offset/orig`` 兩個比值合計誤差最小的 orig / offset，
並把殘餘誤差同時以百分比與原始像素兩種單位回報。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from models.atlas_data import AtlasFile, AtlasPage, AtlasProp, AtlasRegion


def round_half_up(value: float) -> int:
    """
    四捨五入（.5 一律進位）。

    Python 內建的 round() 是銀行家捨入（0.5 進到偶數），會讓相鄰區塊的邊界
    往不同方向靠，也會讓預覽與實際處理的結果對不上，所以統一用這個。
    """
    return math.floor(value + 0.5)


def align_up(value: int, align: int) -> int:
    """把畫布尺寸補到指定對齊；align <= 0 代表補到 2 的次方。"""
    if align <= 0:
        size = 1
        while size < value:
            size *= 2
        return size
    if align <= 1:
        return value
    return ((value + align - 1) // align) * align


@dataclass
class RegionMapping:
    """單一區塊縮放前後的對照"""

    region: AtlasRegion
    src_rect: tuple[int, int, int, int]  # 原頁面矩形 (x, y, w, h)
    dst_rect: tuple[int, int, int, int]  # 新頁面矩形 (x, y, w, h)
    new_xy: tuple[int, int]
    new_size: tuple[int, int]
    new_offset: tuple[int, int]
    new_orig: tuple[int, int]

    # 幾何漂移（相對於 attachment 尺寸的百分比，0 代表完全精確）
    size_drift_pct: tuple[float, float] = (0.0, 0.0)
    offset_drift_pct: tuple[float, float] = (0.0, 0.0)
    # 同一個誤差換算成「原始貼圖的像素」，比百分比直觀
    max_drift_px: float = 0.0

    @property
    def max_drift_pct(self) -> float:
        return max(
            abs(self.size_drift_pct[0]),
            abs(self.size_drift_pct[1]),
            abs(self.offset_drift_pct[0]),
            abs(self.offset_drift_pct[1]),
        )

    @property
    def is_exact(self) -> bool:
        return self.max_drift_pct < 1e-9


@dataclass
class PageMapping:
    """一張頁面縮放前後的對照"""

    page: AtlasPage
    src_size: tuple[int, int]
    dst_canvas: tuple[int, int]
    scale_x: float
    scale_y: float
    regions: list[RegionMapping] = field(default_factory=list)

    @property
    def max_drift_pct(self) -> float:
        return max((r.max_drift_pct for r in self.regions), default=0.0)


def _map_axis(start: int, length: int, scale: float, limit: int) -> tuple[int, int]:
    """
    把一維區間 [start, start+length) 映射到新座標。

    兩個邊界各自四捨五入，再相減得到新長度——這是避免相鄰區塊重疊/縫隙的關鍵。
    """
    new_start = round_half_up(start * scale)
    new_end = round_half_up((start + length) * scale)
    if length > 0 and new_end <= new_start:
        new_end = new_start + 1  # 極小區塊在大幅縮小時不可以消失
    new_start = max(0, min(new_start, max(0, limit - 1)))
    new_end = max(new_start + (1 if length > 0 else 0), min(new_end, limit))
    return new_start, new_end - new_start


def _solve_orig_offset(
    size: int,
    orig: int,
    offset: int,
    new_size: int,
    scale: float,
) -> tuple[int, int]:
    """
    在新的像素尺寸已經固定的前提下，挑出誤差最小的 orig 與 offset。

    渲染結果只跟 ``size/orig`` 與 ``offset/orig`` 兩個比值有關，所以直接以
    「還原這兩個比值」為目標搜尋候選整數，比各自獨立四捨五入準得多。
    """
    if orig <= 0 or size <= 0:
        return max(new_size, round_half_up(orig * scale)), max(0, round_half_up(offset * scale))

    target_size_ratio = size / orig
    target_offset_ratio = offset / orig
    natural = orig * scale
    ideal = new_size / target_size_ratio  # 完全保住 size/orig 時的理想 orig

    candidates: set[int] = set()
    for base in (math.floor(ideal), math.ceil(ideal), round_half_up(natural)):
        candidates.update(range(base - 1, base + 2))

    best: tuple[float, int, int] | None = None
    for candidate in sorted(candidates):
        if candidate < new_size or candidate <= 0:
            continue  # orig 不可能小於 size
        new_offset = round_half_up(candidate * target_offset_ratio)
        new_offset = max(0, min(new_offset, candidate - new_size))
        error = (
            abs(new_size / candidate - target_size_ratio)
            + abs(new_offset / candidate - target_offset_ratio)
            # 同分時偏好最接近「原尺寸 x 縮放比」的那個，結果比較符合直覺
            + 1e-6 * abs(candidate - natural)
        )
        if best is None or error < best[0]:
            best = (error, candidate, new_offset)

    if best is None:  # 理論上不會發生，保底
        return max(new_size, round_half_up(natural)), max(0, round_half_up(offset * scale))
    return best[1], best[2]


def map_region(
    region: AtlasRegion,
    scale_x: float,
    scale_y: float,
    canvas_w: int,
    canvas_h: int,
) -> RegionMapping:
    """重算單一區塊的所有座標。"""
    src_x, src_y, src_w, src_h = region.page_rect

    dst_x, dst_w = _map_axis(src_x, src_w, scale_x, canvas_w)
    dst_y, dst_h = _map_axis(src_y, src_h, scale_y, canvas_h)

    return _finish_region(region, (dst_x, dst_y, dst_w, dst_h), scale_x, scale_y)


def map_region_to_rect(region: AtlasRegion, dst_rect: tuple[int, int, int, int]) -> RegionMapping:
    """
    把區塊映射到指定的目標矩形（合圖版面模式）。

    位置與尺寸都由版面決定，比例則從「新／舊寬高」反推——每個區塊各自等比，
    所以 x 與 y 的比例相同，size/orig 與 offset/orig 兩個比值依舊守得住。
    """
    src_x, src_y, src_w, src_h = region.page_rect
    dst_w, dst_h = dst_rect[2], dst_rect[3]
    scale_x = dst_w / src_w if src_w else 1.0
    scale_y = dst_h / src_h if src_h else 1.0
    return _finish_region(region, dst_rect, scale_x, scale_y)


def _finish_region(
    region: AtlasRegion,
    dst_rect: tuple[int, int, int, int],
    scale_x: float,
    scale_y: float,
) -> RegionMapping:
    """由「已決定的目標矩形」算出 size / orig / offset 與漂移量。"""
    src_x, src_y, src_w, src_h = region.page_rect
    dst_x, dst_y, dst_w, dst_h = dst_rect

    rotated = region.is_rotated
    if rotated:
        # 旋轉時區塊自身的 x 軸躺在頁面的 y 軸上
        new_size = (dst_h, dst_w)
        region_scale_x, region_scale_y = scale_y, scale_x
    else:
        new_size = (dst_w, dst_h)
        region_scale_x, region_scale_y = scale_x, scale_y

    off_x, off_y = region.offset
    orig_w, orig_h = region.orig
    size_w, size_h = region.size

    if off_x == 0 and off_y == 0 and (orig_w, orig_h) == (size_w, size_h):
        # 未裁切：讓 orig 跟著 size 走，比例完全不變（零誤差）
        new_offset = (0, 0)
        new_orig = new_size
    else:
        orig_x, offset_x_new = _solve_orig_offset(
            size_w, orig_w, off_x, new_size[0], region_scale_x
        )
        orig_y, offset_y_new = _solve_orig_offset(
            size_h, orig_h, off_y, new_size[1], region_scale_y
        )
        new_offset = (offset_x_new, offset_y_new)
        new_orig = (orig_x, orig_y)

    size_drift = _size_drift((size_w, size_h), (orig_w, orig_h), new_size, new_orig)
    offset_drift = _offset_drift((off_x, off_y), (orig_w, orig_h), new_offset, new_orig)

    return RegionMapping(
        region=region,
        src_rect=(src_x, src_y, src_w, src_h),
        dst_rect=(dst_x, dst_y, dst_w, dst_h),
        new_xy=(dst_x, dst_y),
        new_size=new_size,
        new_offset=new_offset,
        new_orig=new_orig,
        size_drift_pct=size_drift,
        offset_drift_pct=offset_drift,
        max_drift_px=max(
            abs(size_drift[0]) * orig_w,
            abs(size_drift[1]) * orig_h,
            abs(offset_drift[0]) * orig_w,
            abs(offset_drift[1]) * orig_h,
        )
        / 100.0,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _size_drift(
    old_size: tuple[int, int],
    old_orig: tuple[int, int],
    new_size: tuple[int, int],
    new_orig: tuple[int, int],
) -> tuple[float, float]:
    """
    渲染出來的區塊長度 = attachment 尺寸 x (size / orig)。
    比較這個比值縮放前後的變化，就是實際的視覺誤差。
    """
    drift: list[float] = []
    for i in range(2):
        old = _ratio(old_size[i], old_orig[i])
        new = _ratio(new_size[i], new_orig[i])
        drift.append((new - old) * 100.0 if old is not None and new is not None else 0.0)
    return drift[0], drift[1]


def _offset_drift(
    old_offset: tuple[int, int],
    old_orig: tuple[int, int],
    new_offset: tuple[int, int],
    new_orig: tuple[int, int],
) -> tuple[float, float]:
    """區塊在 attachment 內的位移變化，同樣換算成 attachment 尺寸的百分比。"""
    drift: list[float] = []
    for i in range(2):
        old = _ratio(old_offset[i], old_orig[i])
        new = _ratio(new_offset[i], new_orig[i])
        drift.append((new - old) * 100.0 if old is not None and new is not None else 0.0)
    return drift[0], drift[1]


def apply_layout(
    atlas: AtlasFile, page: AtlasPage, layout
) -> tuple[list[tuple[int, PageMapping]], list[str]]:
    """
    把（可能拆分成多頁的）合圖版面套進這份 atlas。

    * 區塊依版面的頁指派分組：留在主頁（page 0）的不動，其餘**搬到新建的
      拆分頁**——props 複製自原頁、插在原頁之後。沒分到任何區塊的拆分頁
      不建立：每份 atlas 只列出自己用到的頁（共用貼圖時各 atlas 只認得
      自己那部分的區塊，這是正常情況，不是錯誤）。
    * Spine 的 attachment 只以「區塊名稱」對應 atlas，區塊搬到哪一頁都
      不影響 .skel，所以拆頁不會破圖。
    * 回傳 ``[(版面頁索引, 對照表)]`` 與找不到對應元件的區塊名稱。
      對照表**尚未**套用座標——呼叫端驗證後逐一 ``apply_page_mapping``
      （拆分頁的尺寸也由它寫入）。

    Args:
        atlas: 這份 atlas（拆分頁會插進它的 pages）
        page: 要套用版面的頁面
        layout: ``models.sheet_layout.SheetLayout``
    """
    placements = layout.by_rect()
    grouped: dict[int, list[tuple[AtlasRegion, object]]] = {}
    unmatched: list[str] = []
    for region in page.regions:
        placement = placements.get(region.page_rect)
        if placement is None or placement.pos is None:
            unmatched.append(region.name)
            continue
        grouped.setdefault(placement.page, []).append((region, placement))
    if unmatched:
        return [], unmatched

    def make_mapping(target: AtlasPage, canvas: tuple[int, int], index: int) -> PageMapping:
        mapping = PageMapping(
            page=target,
            src_size=page.size,
            dst_canvas=canvas,
            scale_x=0.0,   # 版面模式沒有單一比例；漂移統計以各區塊自己的比例計算
            scale_y=0.0,
        )
        mapping.regions = [
            map_region_to_rect(region, placement.dst_rect)  # type: ignore[attr-defined]
            for region, placement in grouped.get(index, [])
        ]
        return mapping

    results: list[tuple[int, PageMapping]] = [(0, make_mapping(page, layout.canvas, 0))]

    moved_ids: set[int] = set()
    insert_at = atlas.pages.index(page) + 1
    for index in range(1, layout.page_count):
        entries = grouped.get(index)
        if not entries:
            continue
        split = AtlasPage(
            name=layout.page_name(index),
            props=[
                AtlasProp(p.key, list(p.values), p.indent, p.sep, p.delim)
                for p in page.props
            ],
            leading_blank=True,
        )
        for region, _placement in entries:
            split.regions.append(region)
            moved_ids.add(id(region))
        atlas.pages.insert(insert_at, split)
        insert_at += 1
        results.append((index, make_mapping(split, layout.page_canvas(index), index)))

    if moved_ids:
        page.regions = [r for r in page.regions if id(r) not in moved_ids]
    return results, []


def build_page_mapping(
    page: AtlasPage,
    scale_x: float,
    scale_y: float,
    canvas: tuple[int, int] | None = None,
) -> PageMapping:
    """
    建立整頁的對照表。

    ``canvas`` 為新頁面畫布尺寸；省略時用 ``round(原尺寸 x 縮放比)``。
    畫布可以比內容大（例如補到 2 的次方），這不會影響縮放比例。
    """
    src_w, src_h = page.size
    if canvas is None:
        canvas = (max(1, round_half_up(src_w * scale_x)), max(1, round_half_up(src_h * scale_y)))
    canvas_w, canvas_h = canvas

    mapping = PageMapping(
        page=page,
        src_size=(src_w, src_h),
        dst_canvas=(canvas_w, canvas_h),
        scale_x=scale_x,
        scale_y=scale_y,
    )
    mapping.regions = [
        map_region(region, scale_x, scale_y, canvas_w, canvas_h) for region in page.regions
    ]
    return mapping


def apply_page_mapping(mapping: PageMapping) -> None:
    """把對照結果寫回 AtlasPage / AtlasRegion。"""
    mapping.page.set_size(*mapping.dst_canvas)
    for item in mapping.regions:
        item.region.apply(
            xy=item.new_xy,
            size=item.new_size,
            offset=item.new_offset,
            orig=item.new_orig,
        )
