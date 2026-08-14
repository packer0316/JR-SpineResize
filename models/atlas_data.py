"""
Atlas 資料模型

設計重點：屬性以「原始文字片段」保存（縮排、冒號後空白、值分隔符都記下來），
輸出時只替換數值本身，其餘一字不動。這樣同一個 atlas 讀進來再寫出去會完全相同，
縮放後的差異也只會出現在真正該變的數字上，方便肉眼 diff 比對。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from config.constants import ATLAS_STYLE_LEGACY, ATLAS_STYLE_MODERN


@dataclass
class AtlasProp:
    """atlas 中的一行 `key: value[, value...]`"""

    key: str
    values: list[str]
    indent: str = ""
    sep: str = " "      # 冒號與第一個值之間的空白
    delim: str = ", "   # 值與值之間的分隔符

    def to_line(self) -> str:
        return f"{self.indent}{self.key}:{self.sep}{self.delim.join(self.values)}"

    def ints(self) -> list[int]:
        return [int(v) for v in self.values]

    def set_ints(self, numbers: list[int]) -> None:
        self.values = [str(n) for n in numbers]


class _PropHolder:
    """提供以 key 存取 AtlasProp 的共用行為"""

    props: list[AtlasProp]

    def prop(self, key: str) -> AtlasProp | None:
        for p in self.props:
            if p.key == key:
                return p
        return None

    def has(self, key: str) -> bool:
        return self.prop(key) is not None

    def _ints(self, key: str, fallback: list[int]) -> list[int]:
        p = self.prop(key)
        if p is None:
            return list(fallback)
        try:
            return p.ints()
        except ValueError:
            return list(fallback)


@dataclass
class AtlasRegion(_PropHolder):
    """
    一個貼圖區塊。

    無論來源是 legacy（xy/size/orig/offset）或 modern（bounds/offsets）格式，
    對外都以同一組欄位表示：

    * ``x, y``            區塊左上角在頁面中的位置（頁面座標）
    * ``width, height``   區塊「未旋轉」的尺寸；rotate 為真時，實際佔用的頁面
                          矩形是 ``(height, width)``
    * ``orig_*``          去除透明邊之前的原始尺寸
    * ``offset_*``        區塊在原始尺寸中的位置（offset_y 由下往上算）
    """

    name: str
    props: list[AtlasProp] = field(default_factory=list)
    style: str = ATLAS_STYLE_LEGACY

    # ------------------------------------------------------------ 讀取

    @property
    def rotate_raw(self) -> str:
        p = self.prop("rotate")
        return p.values[0] if p and p.values else "false"

    @property
    def is_rotated(self) -> bool:
        """
        legacy 用 true/false；modern 用角度（0 / 90 / 180 / 270）。

        只有 90 與 270 會讓寬高對調，180 不會。
        """
        raw = self.rotate_raw.strip().lower()
        if raw in ("true", "false"):
            return raw == "true"
        try:
            return int(float(raw)) % 180 == 90
        except ValueError:
            return False

    @property
    def xy(self) -> tuple[int, int]:
        if self.style == ATLAS_STYLE_MODERN:
            b = self._ints("bounds", [0, 0, 0, 0])
            return b[0], b[1]
        return tuple(self._ints("xy", [0, 0]))[:2]  # type: ignore[return-value]

    @property
    def size(self) -> tuple[int, int]:
        if self.style == ATLAS_STYLE_MODERN:
            b = self._ints("bounds", [0, 0, 0, 0])
            return b[2], b[3]
        return tuple(self._ints("size", [0, 0]))[:2]  # type: ignore[return-value]

    @property
    def offset(self) -> tuple[int, int]:
        if self.style == ATLAS_STYLE_MODERN:
            o = self.prop("offsets")
            if o is None:
                return 0, 0
            v = o.ints()
            return v[0], v[1]
        return tuple(self._ints("offset", [0, 0]))[:2]  # type: ignore[return-value]

    @property
    def orig(self) -> tuple[int, int]:
        if self.style == ATLAS_STYLE_MODERN:
            o = self.prop("offsets")
            if o is None:
                return self.size
            v = o.ints()
            return v[2], v[3]
        p = self.prop("orig")
        if p is None:
            return self.size
        return tuple(p.ints())[:2]  # type: ignore[return-value]

    @property
    def page_rect(self) -> tuple[int, int, int, int]:
        """區塊實際佔用的頁面矩形 (x, y, w, h)，已考慮旋轉。"""
        x, y = self.xy
        w, h = self.size
        if self.is_rotated:
            w, h = h, w
        return x, y, w, h

    @property
    def is_trimmed(self) -> bool:
        """是否有被去除透明邊。未裁切的區塊縮放時可以做到完全精確。"""
        return self.offset != (0, 0) or self.orig != self.size

    # ------------------------------------------------------------ 寫入

    def apply(
        self,
        xy: tuple[int, int],
        size: tuple[int, int],
        offset: tuple[int, int],
        orig: tuple[int, int],
    ) -> None:
        """寫回新的座標與尺寸，維持原本的格式風格。"""
        if self.style == ATLAS_STYLE_MODERN:
            bounds = self.prop("bounds")
            if bounds is not None:
                bounds.set_ints([xy[0], xy[1], size[0], size[1]])
            offsets = self.prop("offsets")
            if offsets is not None:
                offsets.set_ints([offset[0], offset[1], orig[0], orig[1]])
            elif offset != (0, 0) or orig != size:
                # 原本沒有 offsets（代表未裁切），縮放後若出現差異就補一行
                self.props.append(
                    AtlasProp("offsets", [str(v) for v in (*offset, *orig)], indent="")
                )
            return

        xy_prop = self.prop("xy")
        if xy_prop is not None:
            xy_prop.set_ints(list(xy))
        size_prop = self.prop("size")
        if size_prop is not None:
            size_prop.set_ints(list(size))
        offset_prop = self.prop("offset")
        if offset_prop is not None:
            offset_prop.set_ints(list(offset))
        orig_prop = self.prop("orig")
        if orig_prop is not None:
            orig_prop.set_ints(list(orig))

    def to_lines(self) -> list[str]:
        return [self.name] + [p.to_line() for p in self.props]


@dataclass
class AtlasPage(_PropHolder):
    """一張貼圖頁面（一個 .png）與其上的所有區塊"""

    name: str
    props: list[AtlasProp] = field(default_factory=list)
    regions: list[AtlasRegion] = field(default_factory=list)
    leading_blank: bool = True  # 這一頁之前是否有空行（第一頁通常沒有）

    @property
    def size(self) -> tuple[int, int]:
        return tuple(self._ints("size", [0, 0]))[:2]  # type: ignore[return-value]

    def set_size(self, width: int, height: int) -> None:
        p = self.prop("size")
        if p is None:
            self.props.insert(0, AtlasProp("size", [str(width), str(height)]))
        else:
            p.set_ints([width, height])

    @property
    def is_premultiplied(self) -> bool:
        p = self.prop("pma")
        return bool(p and p.values and p.values[0].strip().lower() == "true")

    def to_lines(self) -> list[str]:
        lines: list[str] = []
        if self.leading_blank:
            lines.append("")
        lines.append(self.name)
        lines.extend(p.to_line() for p in self.props)
        for region in self.regions:
            lines.extend(region.to_lines())
        return lines


@dataclass
class AtlasFile:
    """一份 .atlas 的完整內容"""

    pages: list[AtlasPage] = field(default_factory=list)
    style: str = ATLAS_STYLE_LEGACY
    newline: str = "\n"
    trailing_newline: bool = True

    @property
    def regions(self) -> Iterator[AtlasRegion]:
        for page in self.pages:
            yield from page.regions

    @property
    def region_count(self) -> int:
        return sum(len(p.regions) for p in self.pages)

    def to_text(self) -> str:
        lines: list[str] = []
        for page in self.pages:
            lines.extend(page.to_lines())
        text = self.newline.join(lines)
        if self.trailing_newline:
            text += self.newline
        return text
