"""
合圖重新排版（裝箱）

每個元件各自縮放後，原本的版面就會留下一堆縫隙，所以要重新排一次並把頁面
縮到「裝得下的最小尺寸」。

作法：MaxRects + BSSF（Best Short Side Fit）——與 Spine 打包器、
libgdx TexturePacker 同一族的演算法，實作簡單而且填充率高。

最小尺寸的求法不是解析解（裝箱是 NP-hard），而是**掃寬度**：

1. 以總面積開根號估一個起點，往兩邊列出一串候選寬度
2. 每個候選寬度都排一次，高度不設限，排完取實際用到的高度
3. 取面積最小的那一組（同面積時偏好比較接近正方形的）

候選寬度會依對齊設定產生（2 的次方時只試 2 的次方），所以「補到 POT」
不是排完再補，而是直接在 POT 的框裡找最小的一組，不會白白浪費空間。

元件之間會保留間距（padding），這是等比縮小後**必須**補回來的：原本 2px
的間距縮一半只剩 1px，擋不住 GPU Linear 取樣跨過邊界吃到隔壁的圖塊。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config.constants import MAX_PAGE_SIZE
from core.rect_mapper import align_up

Rect = tuple[int, int, int, int]

# 畫布長寬比上限。面積一樣時 1612x1612 遠比 415x6235 好用（GPU 貼圖尺寸限制、
# 記憶體對齊、肉眼檢查都是），所以先在這個比例內找最小面積，
# 真的只有極端長條的內容才會退回不設限的解。
MAX_ASPECT = 4.0


@dataclass
class PackItem:
    """要排版的一個元件（key 只是讓呼叫端對回自己的資料）"""

    key: object
    width: int
    height: int
    # 位置固定的元件（使用者手動搬過）：直接佔位，不參與擺放
    fixed: tuple[int, int] | None = None


@dataclass
class PackResult:
    canvas: tuple[int, int]
    positions: dict[int, tuple[int, int]]   # id(item) -> (x, y)
    overflow: list[PackItem]                # 排不進去的（理論上不會有，保底）

    @property
    def ok(self) -> bool:
        return not self.overflow


@dataclass
class _Best:
    """搜尋過程中的候選畫布"""

    area: int
    squareness: int
    canvas: tuple[int, int]
    positions: dict[int, tuple[int, int]]

    @property
    def score(self) -> tuple[int, int]:
        return self.area, self.squareness

    @property
    def aspect(self) -> float:
        long_, short = max(self.canvas), max(1, min(self.canvas))
        return long_ / short


# ---------------------------------------------------------------- MaxRects


class _MaxRects:
    """
    MaxRects 的最小可用實作。

    維護一組「還能用的最大空矩形」，每放一個元件就把與它相交的空矩形切成
    數塊，再刪掉被別人完全包含的冗餘塊。高度給一個很大的上限（等於不限高），
    最後看實際用到多高。
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.free: list[Rect] = [(0, 0, width, height)]
        self.used: list[Rect] = []

    # -------------------------------------------------- 放置

    def occupy(self, rect: Rect) -> None:
        """把一塊區域直接標成已用（位置固定的元件）"""
        self._split_all(rect)
        self.used.append(rect)

    def insert(self, width: int, height: int) -> tuple[int, int] | None:
        """BSSF：挑「較短邊剩餘最少」的空位，同分時比較長邊"""
        best: tuple[int, int, int, int] | None = None  # (short, long, x, y)
        for fx, fy, fw, fh in self.free:
            if fw < width or fh < height:
                continue
            leftover_x = fw - width
            leftover_y = fh - height
            short = min(leftover_x, leftover_y)
            long_ = max(leftover_x, leftover_y)
            if best is None or (short, long_) < (best[0], best[1]):
                best = (short, long_, fx, fy)
        if best is None:
            return None
        x, y = best[2], best[3]
        rect = (x, y, width, height)
        self._split_all(rect)
        self.used.append(rect)
        return x, y

    # -------------------------------------------------- 空間維護

    def _split_all(self, rect: Rect) -> None:
        """
        切開所有與 rect 相交的空矩形。

        剔除冗餘塊時只比對「新產生的塊」，不是整份清單自己比自己——後者是
        O(k²)，在幾百個元件 x 幾十個候選寬度下會慢到互動不能用。
        """
        kept: list[Rect] = []
        fresh: list[Rect] = []
        for free in self.free:
            if _intersects(free, rect):
                fresh.extend(_split(free, rect))
            else:
                kept.append(free)
        self.free = _merge(kept, fresh)


def _intersects(a: Rect, b: Rect) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (bx >= ax + aw or bx + bw <= ax or by >= ay + ah or by + bh <= ay)


def _split(free: Rect, used: Rect) -> list[Rect]:
    """把 free 切成不與 used 相交的最大矩形（上下左右各一塊，重疊是正常的）"""
    fx, fy, fw, fh = free
    ux, uy, uw, uh = used
    pieces: list[Rect] = []
    if uy > fy:                                    # 上
        pieces.append((fx, fy, fw, uy - fy))
    if uy + uh < fy + fh:                          # 下
        pieces.append((fx, uy + uh, fw, fy + fh - (uy + uh)))
    if ux > fx:                                    # 左
        pieces.append((fx, fy, ux - fx, fh))
    if ux + uw < fx + fw:                          # 右
        pieces.append((ux + uw, fy, fx + fw - (ux + uw), fh))
    return [p for p in pieces if p[2] > 0 and p[3] > 0]


def _contains(outer: Rect, inner: Rect) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def _merge(kept: list[Rect], fresh: list[Rect]) -> list[Rect]:
    """
    合併「沒被切到的空塊」與「剛切出來的空塊」，去掉互相包含的冗餘。

    kept 之間本來就已經互相不包含（上一輪剔除過），所以只需要：
    新塊之間互比、新塊與舊塊互比。
    """
    unique: list[Rect] = []
    for rect in fresh:
        if any(_contains(other, rect) for other in unique):
            continue
        unique = [other for other in unique if not _contains(rect, other)]
        unique.append(rect)

    result = [rect for rect in kept if not any(_contains(other, rect) for other in unique)]
    result.extend(rect for rect in unique if not any(_contains(other, rect) for other in result))
    return result


# ---------------------------------------------------------------- 單次排版


def _pack_at(items: list[PackItem], width: int, padding: int) -> tuple[dict[int, tuple[int, int]], int, list[PackItem]]:
    """
    在指定寬度下排一次（高度不設限）。

    padding 的處理方式：每個元件都以 (w + padding, h + padding) 佔位，
    元件本身靠佔位格的左上角。這樣任兩個元件之間至少隔 padding 個像素，
    頁面右／下緣也會留下 padding（避免取樣吃到畫布邊界外的空白）。
    """
    bin_ = _MaxRects(width, MAX_PAGE_SIZE)
    positions: dict[int, tuple[int, int]] = {}
    overflow: list[PackItem] = []

    fixed = [i for i in items if i.fixed is not None]
    movable = [i for i in items if i.fixed is None]

    for item in fixed:
        x, y = item.fixed  # type: ignore[misc]
        bin_.occupy((x, y, item.width + padding, item.height + padding))
        positions[id(item)] = (x, y)

    # 大的先放（面積 → 最長邊），這是 MaxRects 填充率的關鍵
    order = sorted(
        movable,
        key=lambda i: (i.width * i.height, max(i.width, i.height)),
        reverse=True,
    )
    for item in order:
        placed = bin_.insert(item.width + padding, item.height + padding)
        if placed is None:
            overflow.append(item)
            continue
        positions[id(item)] = placed

    # 回傳「內容本身」的高度（不含 padding）——padding 由呼叫端統一補上，
    # 免得右緣算一次、下緣又算一次而不一致
    content_h = max(
        (positions[id(i)][1] + i.height for i in items if id(i) in positions), default=0
    )
    return positions, content_h, overflow


def _sweep(low: int, high: int, step: int, count: int) -> list[int]:
    """low..high 之間取最多 count 個對齊到 step 的寬度（含兩端）"""
    low = align_up(low, step)
    high = max(low, align_up(high, step))
    if count <= 1 or high == low:
        return [low]
    span = max(step, ((high - low) // (count - 1) // step) * step)
    values = list(range(low, high + 1, span))
    if values[-1] != high:
        values.append(high)
    return values


def _pot_widths(low: int) -> list[int]:
    """不小於 low 的 2 的次方，往上五級（足夠涵蓋最佳解）"""
    size = 1
    while size < low:
        size *= 2
    widths: list[int] = []
    for _ in range(6):
        if size > MAX_PAGE_SIZE:
            break
        widths.append(size)
        size *= 2
    return widths


def pack(
    items: list[PackItem],
    padding: int = 2,
    align: int = 1,
    fixed_canvas: tuple[int, int] | None = None,
    hint_width: int = 0,
) -> PackResult:
    """
    把元件排進最小的頁面。

    Args:
        items: 要排的元件（``fixed`` 有值的位置不動）
        padding: 元件之間（與頁面右／下緣）保留的像素
        align: 畫布對齊；``PAGE_ALIGN_POT``(-1) 代表補到 2 的次方
        fixed_canvas: 指定畫布尺寸（不搜尋最小值），排不進去就回報 overflow
        hint_width: 額外一定要試的寬度（通常給原頁面寬度——原本裝得下的
            比例通常也是縮放後的好答案）

    Returns:
        PackResult；``positions`` 以 ``id(item)`` 為鍵。
    """
    if not items:
        canvas = fixed_canvas or (1, 1)
        return PackResult(canvas=canvas, positions={}, overflow=[])

    if fixed_canvas is not None:
        width, height = fixed_canvas
        positions, _, overflow = _pack_at(items, width, padding)
        # 高度不設限地排完才知道有沒有超出指定畫布
        too_tall = [
            item for item in items
            if id(item) in positions
            and positions[id(item)][1] + item.height > height
        ]
        return PackResult(
            canvas=fixed_canvas,
            positions=positions,
            overflow=overflow + too_tall,
        )

    widest = max(i.width + padding for i in items)
    area = sum((i.width + padding) * (i.height + padding) for i in items)
    root = int(math.sqrt(area))
    low = max(1, widest)
    # 位置固定的元件可能落在很右邊，畫布至少要容得下它們
    for item in items:
        if item.fixed is not None:
            low = max(low, item.fixed[0] + item.width + padding)

    # 兩個最佳解：長寬比合理的（優先採用）與不設限的（保底）
    best: _Best | None = None
    best_any: _Best | None = None

    def consider(width: int) -> None:
        nonlocal best, best_any
        positions, content_h, overflow = _pack_at(items, width, padding)
        if overflow:
            return
        content_w = max(
            (positions[id(i)][0] + i.width for i in items if id(i) in positions), default=0
        )
        # 內容右／下緣要留 padding，再套對齊
        canvas_w = align_up(min(width, content_w + padding), align)
        canvas_h = align_up(content_h + padding, align)
        if canvas_w > MAX_PAGE_SIZE or canvas_h > MAX_PAGE_SIZE:
            return
        candidate = _Best(
            area=canvas_w * canvas_h,
            squareness=abs(canvas_w - canvas_h),
            canvas=(canvas_w, canvas_h),
            positions=positions,
        )
        if best_any is None or candidate.score < best_any.score:
            best_any = candidate
        if candidate.aspect <= MAX_ASPECT and (best is None or candidate.score < best.score):
            best = candidate

    if align <= 0:  # 2 的次方：候選本來就只有幾個，全試
        for width in _pot_widths(low):
            consider(width)
    else:
        # 粗掃一輪找到大概的最佳寬度，再在它附近細掃——比等距掃 50 個點
        # 快三倍以上，結果幾乎一樣（裝箱的面積對寬度是相當平滑的曲線）
        step = max(1, align)
        limit = min(MAX_PAGE_SIZE, max(low, root * 2))
        # 等距掃很容易剛好跳過「正方形」那一點：規則排列的內容常有好幾個
        # 面積完全相同的解（例如 16x4 塊與 8x8 塊），漏掉正方形那個就會
        # 得到 2048x512 這種同面積但難用的結果。所以把幾個「一定要試」的
        # 寬度直接加進來：面積開根號、2 的次方、以及原頁面寬度。
        specials = [root, *_pot_widths(low)[:4]]
        if hint_width:
            specials.append(hint_width)
        candidates = _sweep(low, limit, step, 12)
        candidates.extend(
            align_up(w, step) for w in specials if low <= w <= limit
        )
        for width in sorted(set(candidates)):
            consider(width)
        chosen = best or best_any
        if chosen is not None:
            centre = chosen.canvas[0]
            span = max(step, (limit - low) // 12)
            for width in _sweep(max(low, centre - span), min(limit, centre + span), step, 6):
                consider(width)

    winner = best or best_any
    if winner is None:
        # 所有候選都排不下（元件比上限還大）：退回不限高的單欄排法
        widest = max(i.width for i in items) + padding
        positions, content_h, overflow = _pack_at(items, min(widest, MAX_PAGE_SIZE), padding)
        return PackResult(
            canvas=(align_up(widest, align), align_up(content_h + padding, align)),
            positions=positions,
            overflow=overflow,
        )

    return PackResult(canvas=winner.canvas, positions=winner.positions, overflow=[])


def minimal_canvas(
    sizes: list[tuple[int, int]],
    padding: int = 2,
    align: int = 1,
) -> tuple[int, int]:
    """只想知道「這些尺寸最小能塞進多大的頁面」時的捷徑。"""
    items = [PackItem(key=index, width=w, height=h) for index, (w, h) in enumerate(sizes)]
    return pack(items, padding=padding, align=align).canvas


__all__ = [
    "MAX_ASPECT",
    "PackItem",
    "PackResult",
    "minimal_canvas",
    "pack",
]
