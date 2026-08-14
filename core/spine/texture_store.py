"""
貼圖庫：把 atlas 區塊還原成「未裁切、未旋轉」的獨立影像

Mesh 的 UV 與 RegionAttachment 的幾何都定義在「原始（未裁切）區塊空間」，
把每個區塊還原成 origW x origH 的獨立影像後：

* mesh 頂點的 UV 直接乘上 (origW, origH) 就是貼圖座標
* region attachment 直接把整張影像做仿射變換即可

rotate 的方向約定（以真實素材的文字區塊驗證過）：legacy atlas 的
``rotate: true`` 表示打包時「逆時針」轉了 90 度，還原時要順時針轉回。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from PyQt6.QtGui import QImage

from models.atlas_data import AtlasFile
from utils.image_utils import to_rgba_array


@dataclass
class RegionTexture:
    image: QImage          # 未裁切、未旋轉（origW x origH）
    orig_w: int
    orig_h: int
    rgba: np.ndarray       # 供產生染色版本


def _to_qimage(arr: np.ndarray) -> QImage:
    h, w = arr.shape[:2]
    data = np.ascontiguousarray(arr)
    image = QImage(data.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
    return image.copy()  # 複製一份，脫離 numpy buffer 生命週期


class AtlasTextureStore:
    """由 AtlasFile + 頁面影像建立區塊貼圖快取"""

    def __init__(self, atlas: AtlasFile, pages: dict[str, Image.Image]) -> None:
        self._regions: dict[str, RegionTexture] = {}
        self._tinted: dict[tuple[str, int], QImage] = {}

        for page in atlas.pages:
            source = pages.get(page.name)
            if source is None:
                continue
            arr = to_rgba_array(source)
            page_h, page_w = arr.shape[:2]
            for region in page.regions:
                if region.name in self._regions:
                    continue  # 同名區塊（sequence）取第一個
                x, y, w, h = region.page_rect
                x0, y0 = max(0, x), max(0, y)
                x1, y1 = min(page_w, x + w), min(page_h, y + h)
                if x1 <= x0 or y1 <= y0:
                    continue
                block = arr[y0:y1, x0:x1]
                if region.is_rotated:
                    block = np.ascontiguousarray(np.rot90(block, k=-1))  # 順時針轉回

                size_w, size_h = region.size
                orig_w, orig_h = region.orig
                off_x, off_y = region.offset
                if (orig_w, orig_h) != (size_w, size_h) or (off_x, off_y) != (0, 0):
                    canvas = np.zeros((max(orig_h, 1), max(orig_w, 1), 4), dtype=np.uint8)
                    # atlas 的 offset_y 從下往上算，影像座標從上往下
                    top = orig_h - off_y - block.shape[0]
                    left = off_x
                    t0 = max(0, top)
                    l0 = max(0, left)
                    bh = min(block.shape[0], canvas.shape[0] - t0)
                    bw = min(block.shape[1], canvas.shape[1] - l0)
                    if bh > 0 and bw > 0:
                        canvas[t0 : t0 + bh, l0 : l0 + bw] = block[:bh, :bw]
                    block = canvas
                else:
                    orig_w, orig_h = block.shape[1], block.shape[0]

                self._regions[region.name] = RegionTexture(
                    image=_to_qimage(block),
                    orig_w=block.shape[1],
                    orig_h=block.shape[0],
                    rgba=block,
                )

    def get(self, name: str) -> RegionTexture | None:
        return self._regions.get(name)

    def get_tinted(self, name: str, color: tuple[float, float, float]) -> QImage | None:
        """回傳乘上 RGB 色調的版本（快取，色彩量化到 5 bits 避免爆量）"""
        texture = self._regions.get(name)
        if texture is None:
            return None
        r = int(color[0] * 31.999)
        g = int(color[1] * 31.999)
        b = int(color[2] * 31.999)
        if r == 31 and g == 31 and b == 31:
            return texture.image
        key = (name, (r << 10) | (g << 5) | b)
        cached = self._tinted.get(key)
        if cached is None:
            arr = texture.rgba.astype(np.uint16).copy()
            arr[..., 0] = arr[..., 0] * (r * 255 // 31) // 255
            arr[..., 1] = arr[..., 1] * (g * 255 // 31) // 255
            arr[..., 2] = arr[..., 2] * (b * 255 // 31) // 255
            cached = _to_qimage(arr.astype(np.uint8))
            self._tinted[key] = cached
        return cached
