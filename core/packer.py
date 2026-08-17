"""
合圖重新排版（裝箱）

每個元件各自縮放後，原本的版面就會留下一堆縫隙，所以要重新排一次並把頁面
縮到「裝得下的最小尺寸」。

作法：MaxRects（與 Spine 打包器、libgdx TexturePacker 同一族），但不只跑一種
策略——裝箱是 NP-hard，單一啟發式在某些素材上就是會吃虧，所以每個候選寬度
會用多組「放置順序 × 挑位規則」各排一次，取面積最小的：

* 放置順序：面積、最長邊、高、寬、周長（都由大到小）
* 挑位規則：BSSF（短邊剩最少）與 BAF（剩餘面積最小），同分時偏好靠上靠左

最小尺寸的求法是**掃寬度**：

1. 候選寬度＝等距掃描 ＋ 幾個「一定要試」的點：總面積開根號（接近正方形）、
   原頁面寬度、2 的次方、以及「最寬的前 k 個元件並排」的寬度
   （規則排列的素材最佳解常剛好落在 k 欄的寬度上，等距掃會跳過）
2. 每個寬度排一次（高度不設限），先用預設策略粗掃
3. 在表現最好的幾個寬度上把所有策略組合都跑過，再細掃冠軍寬度附近
4. 取面積最小的一組（同面積時偏好接近正方形的）

候選寬度依對齊設定產生（2 的次方時只試 2 的次方），所以「補到 POT」不是
排完再補，而是直接在 POT 的框裡找最小的一組，不會白白浪費空間。

元件之間會保留間距（padding），這是等比縮小後**必須**補回來的：原本 2px
的間距縮一半只剩 1px，擋不住 GPU Linear 取樣跨過邊界吃到隔壁的圖塊。

> 「永不比原版面差」的保證不在這裡，在 ``sheet_group.repack``：它會把
> 「原始版面等比縮小」也當成一個候選，與這裡的結果比面積取小——
> 100% 時該候選就是原版面本身，所以排版結果保證不會比原檔大。
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

# 放置順序（sort key，都由大到小排）。單一順序在某些形狀分佈上就是會吃虧：
# 高瘦元件多時「高優先」贏，寬扁元件多時「寬優先」贏，混合時「面積」通常最穩。
_ORDERS = (
    lambda i: (i.width * i.height, max(i.width, i.height)),   # 面積
    lambda i: (max(i.width, i.height), i.width * i.height),   # 最長邊
    lambda i: (i.height, i.width),                            # 高
    lambda i: (i.width, i.height),                            # 寬
    lambda i: (i.width + i.height, i.width * i.height),       # 周長
)
_RULES = ("bssf", "baf")


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
    """搜尋過程中的候選畫布（記下策略組合，細掃階段沿用）"""

    area: int
    squareness: int
    canvas: tuple[int, int]
    positions: dict[int, tuple[int, int]]
    order: int = 0
    rule: str = "bssf"
    width: int = 0

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

    def insert(self, width: int, height: int, rule: str = "bssf") -> tuple[int, int] | None:
        """
        挑一個空位放進去。

        * ``bssf``：較短邊剩餘最少（貼合最緊的空位）
        * ``baf``：剩餘面積最小（優先塞滿小洞）

        同分時偏好靠上、再靠左——讓內容往左上角聚，右下的大片空間留給後面的
        元件，也讓結果可重現。
        """
        best: tuple[tuple, int, int] | None = None
        for fx, fy, fw, fh in self.free:
            if fw < width or fh < height:
                continue
            leftover_x = fw - width
            leftover_y = fh - height
            if rule == "baf":
                score = (fw * fh - width * height, min(leftover_x, leftover_y), fy, fx)
            else:
                score = (min(leftover_x, leftover_y), max(leftover_x, leftover_y), fy, fx)
            if best is None or score < best[0]:
                best = (score, fx, fy)
        if best is None:
            return None
        x, y = best[1], best[2]
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


def _pack_at(
    items: list[PackItem],
    width: int,
    padding: int,
    order: int = 0,
    rule: str = "bssf",
) -> tuple[dict[int, tuple[int, int]], int, list[PackItem]]:
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

    for item in sorted(movable, key=_ORDERS[order], reverse=True):
        placed = bin_.insert(item.width + padding, item.height + padding, rule)
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

    # 元件很多時把放置順序減到兩種，互動才不會頓；元件少時全開拚品質
    order_count = len(_ORDERS) if len(items) <= 150 else 2

    if fixed_canvas is not None:
        width, height = fixed_canvas
        fallback: tuple[dict[int, tuple[int, int]], list[PackItem]] | None = None
        for order in range(order_count):
            for rule in _RULES:
                positions, _, overflow = _pack_at(items, width, padding, order, rule)
                # 高度不設限地排完才知道有沒有超出指定畫布
                too_tall = [
                    item for item in items
                    if id(item) in positions
                    and positions[id(item)][1] + item.height > height
                ]
                bad = overflow + too_tall
                if not bad:
                    return PackResult(canvas=fixed_canvas, positions=positions, overflow=[])
                if fallback is None or len(bad) < len(fallback[1]):
                    fallback = (positions, bad)
        assert fallback is not None
        return PackResult(canvas=fixed_canvas, positions=fallback[0], overflow=fallback[1])

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
    width_scores: dict[int, tuple[int, int]] = {}
    tried: set[tuple[int, int, str]] = set()

    def consider(width: int, order: int, rule: str) -> None:
        nonlocal best, best_any
        key = (width, order, rule)
        if key in tried or width < low or width > MAX_PAGE_SIZE:
            return
        tried.add(key)
        positions, content_h, overflow = _pack_at(items, width, padding, order, rule)
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
            order=order,
            rule=rule,
            width=width,
        )
        recorded = width_scores.get(width)
        if recorded is None or candidate.score < recorded:
            width_scores[width] = candidate.score
        if best_any is None or candidate.score < best_any.score:
            best_any = candidate
        if candidate.aspect <= MAX_ASPECT and (best is None or candidate.score < best.score):
            best = candidate

    if align <= 0:  # 2 的次方：候選本來就只有幾個，全部策略都試
        for width in _pot_widths(low):
            for order in range(order_count):
                for rule in _RULES:
                    consider(width, order, rule)
    else:
        step = max(1, align)
        limit = min(MAX_PAGE_SIZE, max(low, root * 2))

        # 候選寬度：等距掃 + 幾個「一定要試」的點。等距掃很容易剛好跳過最佳解
        # （規則排列的素材最佳解常落在 k 欄寬度或正方形那一點上），所以把
        # 面積開根號、原頁面寬度、2 的次方、k 欄寬度都直接加進候選。
        candidates = set(_sweep(low, limit, step, 12))
        specials: list[int] = [root, *_pot_widths(low)[:3]]
        if hint_width:
            specials.append(min(hint_width, MAX_PAGE_SIZE))
        running = 0
        for item_w in sorted((i.width + padding for i in items), reverse=True)[:12]:
            running += item_w
            if running > limit:
                break
            specials.append(running)  # 最寬的前 k 個並排的寬度
        candidates.update(
            align_up(w, step) for w in specials if low <= w <= MAX_PAGE_SIZE
        )

        # 粗掃：預設策略掃過所有候選寬度
        for width in sorted(candidates):
            consider(width, 0, "bssf")

        # 深掘：在表現最好的幾個寬度上，把所有「順序 × 規則」組合都跑過
        ranked = sorted(width_scores.items(), key=lambda kv: kv[1])[:2]
        retry = {width for width, _ in ranked}
        first = best or best_any
        if first is not None:
            retry.add(first.width)
        for width in sorted(retry):
            for order in range(order_count):
                for rule in _RULES:
                    consider(width, order, rule)

        # 細掃冠軍寬度附近（沿用它的策略組合，外加預設組合）
        leader = best or best_any
        if leader is not None:
            span = max(step, (limit - low) // 12)
            for width in _sweep(
                max(low, leader.width - span), min(limit, leader.width + span), step, 6
            ):
                consider(width, leader.order, leader.rule)
                consider(width, 0, "bssf")

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
