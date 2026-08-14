"""
頁面重繪

「整張 PNG 丟進圖片工具縮小」最常見的兩個破圖原因：

* 縮放濾鏡的取樣半徑會跨過圖塊之間的間距，把隔壁圖塊的顏色吃進來（滲色）；
* 圖塊邊界落在非整數位置，四捨五入後多切或少切一排像素（接縫）。

這裡改成逐圖塊裁切、各自縮放、再放回新頁面的對應位置，兩個問題都不會發生。
版面配置維持與原本相同（只是等比縮小），所以輸出的 atlas 與原檔可以直接 diff。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from config.constants import ALPHA_MODE_PREMULTIPLY, BLEED_NONE, BLEED_RGB
from core.rect_mapper import PageMapping
from utils.image_utils import array_to_image, bleed_edges, get_resample, resize_block, to_rgba_array


@dataclass
class RenderSettings:
    resample: str = "lanczos"
    alpha_mode: str = ALPHA_MODE_PREMULTIPLY
    bleed: str = BLEED_RGB
    bleed_px: int = 2


@dataclass
class RenderResult:
    image: Image.Image
    notes: list[str]


def render_page(
    source: Image.Image,
    mapping: PageMapping,
    settings: RenderSettings,
) -> RenderResult:
    """依照對照表把來源頁面重繪成縮放後的新頁面。"""
    return _render(
        source=source,
        blits=[(item.src_rect, item.dst_rect) for item in mapping.regions],
        canvas_size=mapping.dst_canvas,
        premultiplied=mapping.page.is_premultiplied,
        settings=settings,
    )


def render_sheet(
    source: Image.Image,
    layout,
    settings: RenderSettings,
    premultiplied: bool = False,
) -> RenderResult:
    """
    依合圖版面重繪整張合圖（元件各自的比例與新位置都來自版面）。

    這裡刻意走**版面的元件聯集**而不是某一份 atlas 的區塊清單：一張合圖被
    三份 atlas 共用時，每份 atlas 只認得自己那部分的區塊，若照著其中一份畫，
    另外兩份需要的像素就不會出現在輸出的圖裡。

    Args:
        layout: ``models.sheet_layout.SheetLayout``
    """
    return _render(
        source=source,
        blits=[
            (p.src_rect, p.dst_rect) for p in layout.placements if p.pos is not None
        ],
        canvas_size=layout.canvas,
        premultiplied=premultiplied,
        settings=settings,
    )


def _render(
    source: Image.Image,
    blits: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]],
    canvas_size: tuple[int, int],
    premultiplied: bool,
    settings: RenderSettings,
) -> RenderResult:
    """逐塊裁切 → 各自縮放 → 放進新畫布的對應位置。"""
    notes: list[str] = []
    src = to_rgba_array(source)
    src_h, src_w = src.shape[:2]

    canvas_w, canvas_h = canvas_size
    canvas = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)
    occupied = np.zeros((canvas_h, canvas_w), dtype=bool)

    resample = get_resample(settings.resample)

    clipped = 0
    for (sx, sy, sw, sh), (dx, dy, dw, dh) in blits:
        # 來源座標可能超出實際圖檔（atlas 與 png 不同步時），先夾住避免整批失敗
        x0, y0 = max(0, sx), max(0, sy)
        x1, y1 = min(src_w, sx + sw), min(src_h, sy + sh)
        if x1 <= x0 or y1 <= y0:
            clipped += 1
            continue
        if (x1 - x0, y1 - y0) != (sw, sh):
            clipped += 1

        block = src[y0:y1, x0:x1]
        if dw <= 0 or dh <= 0:
            continue

        scaled = resize_block(
            block,
            dw,
            dh,
            resample,
            alpha_mode=settings.alpha_mode,
            source_is_premultiplied=premultiplied,
        )

        # 夾到畫布內（POT 對齊時畫布只會更大，這裡純粹是保險）
        put_w = min(dw, canvas_w - dx)
        put_h = min(dh, canvas_h - dy)
        if put_w <= 0 or put_h <= 0:
            continue
        canvas[dy : dy + put_h, dx : dx + put_w] = scaled[:put_h, :put_w]
        occupied[dy : dy + put_h, dx : dx + put_w] = True

    if clipped:
        notes.append(f"{clipped} 個區塊的座標超出貼圖實際範圍，已依實際尺寸裁切")

    bleed_mode = settings.bleed
    if premultiplied and bleed_mode == BLEED_RGB:
        # 預乘 alpha 的頁面，透明處必須維持 (0,0,0,0)，滲色反而會產生光暈
        bleed_mode = BLEED_NONE
        notes.append("頁面為預乘 alpha（pma: true），已略過邊緣滲出")

    bleed_edges(canvas, occupied, settings.bleed_px, bleed_mode)

    return RenderResult(image=array_to_image(canvas), notes=notes)
