"""
合圖版面（SheetLayout）

一般縮放是「整頁同一個比例」，版面配置與原本完全相同。合圖編輯器則讓每個
區塊各自有比例，再重新排版到最小的頁面——所以需要一份獨立的版面紀錄。

--------------------------------------------------------------------------
為什麼「每個區塊各自縮放」仍然不必改 .skel
--------------------------------------------------------------------------
Spine 算頂點只用到 **同一個區塊自己** 的三個數字：

    regionScaleX = attachment.width / region.origWidth
    localX       = -attachment.width / 2 + region.offsetX * regionScaleX
    localX2      = localX + region.sizeWidth * regionScaleX

``size`` / ``orig`` / ``offset`` 是同一個區塊內部的比值，只要**這個區塊**的
x 與 y 用同一個比例縮，算出來的 localX / localX2 就完全不變——隔壁區塊用
什麼比例完全無關。

區塊在頁面上的 **位置（xy）** 只影響 UV 取樣的起點，Spine 會拿 xy 與頁面
尺寸重算 UV，所以重新排版也不會破圖。Mesh 的 UV 同樣是「區塊內的正規化
比值」，區塊搬到哪、縮多少都跟著走。

結論：**逐區塊等比縮放 + 重新排版是安全的，.skel 依然只讀不寫。**
唯一的限制是同一個區塊的 x/y 必須同比例（不能拉成長方形），這也正是
編輯器只提供等比縮放的原因。

--------------------------------------------------------------------------
身分：以「來源頁面矩形」認人
--------------------------------------------------------------------------
一張合圖常被多份 atlas 共用（實測素材裡有三個 .skel 指向同一張 png）。
版面必須是「這張貼圖」的屬性，不能是「某份 atlas」的屬性，否則兩份 atlas
各自排版就會互相蓋掉——這就是輸出壞掉的根源。

所以 ``SheetLayout`` 以貼圖的絕對路徑為鍵，區塊則以 **來源頁面矩形**
``(x, y, w, h)`` 為身分：同一塊像素在任何 atlas 裡都是同一個矩形，
名稱反而可能不同（打包器去重時會讓多個名稱共用一塊像素）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.constants import PAGE_ALIGN_NONE
from core.rect_mapper import round_half_up

# 區塊縮放比例的可調範圍（與整頁縮放一致，避免編輯器能做出處理不了的值）
MIN_REGION_SCALE = 0.01
MAX_REGION_SCALE = 4.0

Rect = tuple[int, int, int, int]


@dataclass
class Placement:
    """
    合圖上的一個元件。

    ``src_rect`` 是它在**原始貼圖**上佔的矩形（已含旋轉，也就是實際像素矩形），
    同時當作跨 atlas 的身分。``scale`` 是相對原始像素的等比縮放，
    ``pos`` 是重新排版後的左上角；``pos`` 為 None 表示還沒排版。
    """

    src_rect: Rect
    scale: float = 1.0
    pos: tuple[int, int] | None = None
    # 使用者手動搬過的元件：自動排版時位置固定不動
    pinned: bool = False
    # 這塊像素對應的區塊名稱（可能來自多份 atlas，僅供介面顯示）
    names: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 幾何

    @property
    def src_size(self) -> tuple[int, int]:
        return self.src_rect[2], self.src_rect[3]

    @property
    def dst_size(self) -> tuple[int, int]:
        """
        縮放後的像素尺寸。

        x 與 y 都用同一個 ``scale`` 四捨五入——內容不會被拉變形，
        殘餘的整數誤差由 rect_mapper 的 orig/offset 搜尋吸收。
        """
        w, h = self.src_size
        return max(1, round_half_up(w * self.scale)), max(1, round_half_up(h * self.scale))

    @property
    def dst_rect(self) -> Rect:
        x, y = self.pos or (0, 0)
        w, h = self.dst_size
        return x, y, w, h

    @property
    def label(self) -> str:
        if not self.names:
            return f"{self.src_rect[0]},{self.src_rect[1]}"
        first = self.names[0]
        return f"{first} +{len(self.names) - 1}" if len(self.names) > 1 else first

    def set_scale(self, scale: float) -> None:
        self.scale = min(MAX_REGION_SCALE, max(MIN_REGION_SCALE, scale))

    def scale_for_width(self, width: int) -> float:
        """把「希望的新寬度」換算成比例（供拖曳控制點使用）"""
        src_w = max(1, self.src_size[0])
        return min(MAX_REGION_SCALE, max(MIN_REGION_SCALE, width / src_w))

    def scale_for_height(self, height: int) -> float:
        src_h = max(1, self.src_size[1])
        return min(MAX_REGION_SCALE, max(MIN_REGION_SCALE, height / src_h))

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        return {
            "rect": list(self.src_rect),
            "scale": round(self.scale, 6),
            "pos": list(self.pos) if self.pos is not None else None,
            "pinned": self.pinned,
            "names": list(self.names),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Placement | None":
        try:
            rect = [int(v) for v in data["rect"]]
            if len(rect) != 4:
                return None
            raw_pos = data.get("pos")
            pos = (int(raw_pos[0]), int(raw_pos[1])) if raw_pos else None
            placement = cls(
                src_rect=(rect[0], rect[1], rect[2], rect[3]),
                pos=pos,
                pinned=bool(data.get("pinned", False)),
                names=[str(n) for n in data.get("names", [])],
            )
            placement.set_scale(float(data.get("scale", 1.0)))
            return placement
        except (KeyError, TypeError, ValueError, IndexError):
            return None


@dataclass
class SheetLayout:
    """
    一張合圖的自訂版面。

    ``page_path`` 是貼圖的絕對路徑（版面的身分）；有版面的頁面在處理時
    **完全由版面決定**輸出（畫布尺寸與每個元件的位置、大小），
    全域的縮放比例對它不再生效——這樣才不會兩邊各算一次而互相打架。
    """

    page_path: Path
    src_canvas: tuple[int, int]
    canvas: tuple[int, int] = (0, 0)
    padding: int = 2
    align: int = PAGE_ALIGN_NONE
    placements: list[Placement] = field(default_factory=list)

    # ------------------------------------------------------------ 查詢

    @property
    def key(self) -> str:
        return layout_key(self.page_path)

    @property
    def is_packed(self) -> bool:
        return self.canvas[0] > 0 and self.canvas[1] > 0 and all(
            p.pos is not None for p in self.placements
        )

    @property
    def uniform_scale(self) -> float | None:
        """所有元件同一個比例時回傳該比例，否則 None（介面顯示用）"""
        if not self.placements:
            return None
        first = self.placements[0].scale
        return first if all(abs(p.scale - first) < 1e-9 for p in self.placements) else None

    @property
    def is_identity(self) -> bool:
        """
        與原始貼圖完全相同的版面（比例 1.0、位置與畫布都照原樣）。

        這種版面的意義是「這張合圖不要動」：輸出的 atlas 數值與原檔一模一樣，
        貼圖也是逐塊 1:1 複製（``resize_block`` 對同尺寸會直接回傳原內容），
        所以連 PNG 的像素都不會變。用來把某張合圖排除在全域縮放之外。
        """
        if not self.placements or self.canvas != self.src_canvas:
            return False
        return all(
            abs(p.scale - 1.0) < 1e-9 and p.pos == (p.src_rect[0], p.src_rect[1])
            for p in self.placements
        )

    def reset_to_source(self) -> None:
        """
        還原成原始版面：比例 1.0、位置與畫布都回到來源 atlas 的樣子。

        刻意**不**固定元件：沒有任何操作會自動重排，恆等版面自然保持原樣；
        使用者一旦再調整（改比例、重排），就照一般規則整張重新排版。
        以前這裡把全部元件標成 pinned，結果還原後再調整任何一個元件，
        其餘元件全被固定住，版面永遠縮不下去（看起來像自動重排壞掉）。
        """
        for placement in self.placements:
            placement.set_scale(1.0)
            placement.pos = (placement.src_rect[0], placement.src_rect[1])
            placement.pinned = False
        self.canvas = self.src_canvas

    def by_rect(self) -> dict[Rect, Placement]:
        return {p.src_rect: p for p in self.placements}

    def find(self, src_rect: Rect) -> Placement | None:
        for placement in self.placements:
            if placement.src_rect == src_rect:
                return placement
        return None

    @property
    def content_bounds(self) -> tuple[int, int]:
        """所有元件實際佔用的右／下邊界（自動排版後即為最小內容尺寸）"""
        right = bottom = 0
        for placement in self.placements:
            if placement.pos is None:
                continue
            x, y, w, h = placement.dst_rect
            right = max(right, x + w)
            bottom = max(bottom, y + h)
        return right, bottom

    @property
    def used_area(self) -> int:
        return sum(w * h for w, h in (p.dst_size for p in self.placements))

    def scale_all(self, scale: float) -> None:
        """整組套用同一個比例（合圖群組的共同調整）"""
        for placement in self.placements:
            placement.set_scale(scale)

    def area_ratio(self) -> float:
        """新畫布面積 / 原畫布面積（<1 代表變小）"""
        src = self.src_canvas[0] * self.src_canvas[1]
        if src <= 0 or not self.is_packed:
            return 1.0
        return (self.canvas[0] * self.canvas[1]) / src

    def fingerprint(self) -> tuple:
        """影響輸出內容的所有欄位（預覽／估算快取用）"""
        return (
            self.key,
            self.canvas,
            self.padding,
            self.align,
            tuple(
                (p.src_rect, round(p.scale, 6), p.pos)
                for p in sorted(self.placements, key=lambda p: p.src_rect)
            ),
        )

    def describe(self) -> str:
        if self.is_identity:
            return (
                f"{self.src_canvas[0]}x{self.src_canvas[1]} 原始版面"
                f"（{len(self.placements)} 個元件、不縮放）"
            )
        scale = self.uniform_scale
        scale_text = f"{scale * 100:g}%" if scale is not None else "各區塊不同比例"
        return (
            f"{self.src_canvas[0]}x{self.src_canvas[1]} → "
            f"{self.canvas[0]}x{self.canvas[1]}（{len(self.placements)} 個元件、{scale_text}）"
        )

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        return {
            "page": str(self.page_path),
            "src_canvas": list(self.src_canvas),
            "canvas": list(self.canvas),
            "padding": self.padding,
            "align": self.align,
            "placements": [p.to_dict() for p in self.placements],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SheetLayout | None":
        try:
            page = str(data.get("page", ""))
            if not page:
                return None
            src_canvas = [int(v) for v in data.get("src_canvas", [0, 0])]
            canvas = [int(v) for v in data.get("canvas", [0, 0])]
            layout = cls(
                page_path=Path(page),
                src_canvas=(src_canvas[0], src_canvas[1]),
                canvas=(canvas[0], canvas[1]),
                padding=max(0, int(data.get("padding", 2))),
                align=int(data.get("align", PAGE_ALIGN_NONE)),
            )
        except (TypeError, ValueError, IndexError):
            return None
        for raw in data.get("placements", []):
            if not isinstance(raw, dict):
                continue
            placement = Placement.from_dict(raw)
            if placement is not None:
                layout.placements.append(placement)
        if layout.is_identity:
            # 舊版「還原原始版面」會把恆等版面的元件全部固定；恆等版面
            # 不靠固定維持（沒編輯就不會重排），載入時把固定拿掉，
            # 否則舊專案檔還原出來的版面一調整就全被固定卡死、縮不下去
            for placement in layout.placements:
                placement.pinned = False
        return layout if layout.placements else None


def layout_key(path: Path) -> str:
    """
    版面的查詢鍵：解析後的絕對路徑（Windows 不分大小寫，一律小寫化）。

    多份 atlas 可能用不同的相對寫法指到同一張圖，解析後才是同一個檔案。
    """
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


class LayoutStore:
    """
    所有合圖版面的集中管理（以貼圖路徑為鍵）。

    刻意**不**放進 ProcessOptions：options 是每份專案各存一份的，
    兩份專案共用同一張合圖時就會各自帶著一份版面，改了一邊另一邊不知道，
    輸出時互相蓋掉——正是要避免的狀況。版面只有這一份，所有專案都讀它。
    """

    def __init__(self) -> None:
        self._layouts: dict[str, SheetLayout] = {}

    def __len__(self) -> int:
        return len(self._layouts)

    def __bool__(self) -> bool:
        return bool(self._layouts)

    def __iter__(self):
        return iter(self._layouts.values())

    def get(self, path: Path | None) -> SheetLayout | None:
        if path is None:
            return None
        return self._layouts.get(layout_key(path))

    def has(self, path: Path | None) -> bool:
        return self.get(path) is not None

    def put(self, layout: SheetLayout) -> None:
        self._layouts[layout.key] = layout

    def remove(self, path: Path | None) -> bool:
        if path is None:
            return False
        return self._layouts.pop(layout_key(path), None) is not None

    def clear(self) -> None:
        self._layouts.clear()

    def layouts(self) -> list[SheetLayout]:
        return list(self._layouts.values())

    def fingerprint_for(self, paths: list[Path]) -> tuple:
        """指定貼圖們的版面指紋（沒有版面的貼圖不佔位）"""
        items = []
        for path in paths:
            layout = self.get(path)
            if layout is not None:
                items.append(layout.fingerprint())
        return tuple(sorted(items))

    # ------------------------------------------------------------ 序列化

    def to_list(self) -> list[dict]:
        return [layout.to_dict() for layout in self._layouts.values()]

    def load_list(self, raw: list) -> int:
        """從專案檔還原；回傳成功還原的份數（貼圖已不在的也留著，處理時才會跳過）"""
        count = 0
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            layout = SheetLayout.from_dict(item)
            if layout is not None:
                self.put(layout)
                count += 1
        return count
