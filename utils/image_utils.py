"""
影像處理工具

這裡集中處理兩個 atlas 縮放最常見的破圖來源：

1. **透明邊黑框**：直接對「直通 alpha」的圖做插值，透明像素那些沒有意義的
   RGB 值會被混進來，邊緣就會出現黑邊或髒邊。正確作法是先預乘 alpha、
   縮放完再還原。
2. **邊緣取樣吃到空白**：GPU 用 Linear 取樣時，區塊最外圈的像素會取到
   區塊外的透明像素。把顏色往外滲一圈（alpha 維持 0）就能補掉。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from config.constants import ALPHA_MODE_PREMULTIPLY, BLEED_FULL, BLEED_NONE

RESAMPLE_MAP = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "box": Image.Resampling.BOX,
    "nearest": Image.Resampling.NEAREST,
}


def get_resample(name: str) -> Image.Resampling:
    return RESAMPLE_MAP.get(name, Image.Resampling.LANCZOS)


def to_rgba_array(image: Image.Image) -> np.ndarray:
    """轉成 HxWx4 的 uint8 陣列。"""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    return np.asarray(image, dtype=np.uint8)


def premultiply(arr: np.ndarray) -> np.ndarray:
    """直通 alpha -> 預乘 alpha"""
    alpha = arr[..., 3:4].astype(np.uint16)
    rgb = (arr[..., :3].astype(np.uint16) * alpha + 127) // 255
    out = arr.copy()
    out[..., :3] = rgb.astype(np.uint8)
    return out


def unpremultiply(arr: np.ndarray) -> np.ndarray:
    """預乘 alpha -> 直通 alpha"""
    alpha = arr[..., 3:4].astype(np.uint16)
    safe = np.maximum(alpha, 1)
    rgb = (arr[..., :3].astype(np.uint32) * 255 + safe // 2) // safe
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    out = arr.copy()
    out[..., :3] = np.where(alpha == 0, 0, rgb)
    return out


def resize_block(
    block: np.ndarray,
    width: int,
    height: int,
    resample: Image.Resampling,
    alpha_mode: str = ALPHA_MODE_PREMULTIPLY,
    source_is_premultiplied: bool = False,
) -> np.ndarray:
    """
    縮放單一區塊。

    ``source_is_premultiplied`` 對應 atlas 的 ``pma: true``——來源本來就是預乘的
    話，直接縮放即為正確，不需要（也不可以）再乘一次。
    """
    if block.shape[0] == height and block.shape[1] == width:
        return block.copy()

    needs_premultiply = (
        alpha_mode == ALPHA_MODE_PREMULTIPLY
        and not source_is_premultiplied
        and resample is not Image.Resampling.NEAREST
    )

    working = premultiply(block) if needs_premultiply else block
    resized = np.asarray(
        Image.fromarray(working, mode="RGBA").resize((width, height), resample),
        dtype=np.uint8,
    )
    if needs_premultiply:
        # 插值可能讓 RGB 超過 alpha（Lanczos 的過衝），還原前先夾回去
        alpha = resized[..., 3:4]
        resized = resized.copy()
        resized[..., :3] = np.minimum(resized[..., :3], alpha)
        resized = unpremultiply(resized)
    return resized


_NEIGHBOURS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def bleed_edges(
    canvas: np.ndarray,
    occupied: np.ndarray,
    pixels: int,
    mode: str,
) -> None:
    """
    把已佔用區塊的顏色往外滲 ``pixels`` 圈，就地修改 ``canvas``。

    只會寫入尚未被任何區塊佔用的像素，因此不可能污染到隔壁圖塊。
    ``mode == BLEED_FULL`` 時連 alpha 一起外擴（輪廓會變大）；
    預設只滲 RGB，alpha 維持 0，視覺輪廓完全不變。
    """
    if pixels <= 0 or mode == BLEED_NONE:
        return

    height, width = occupied.shape
    copy_alpha = mode == BLEED_FULL
    source_mask = occupied.copy()

    for _ in range(pixels):
        next_mask = source_mask.copy()
        for dy, dx in _NEIGHBOURS:
            dst_y = slice(max(0, -dy), height - max(0, dy))
            src_y = slice(max(0, dy), height - max(0, -dy))
            dst_x = slice(max(0, -dx), width - max(0, dx))
            src_x = slice(max(0, dx), width - max(0, -dx))

            need = (~next_mask[dst_y, dst_x]) & source_mask[src_y, src_x]
            if not need.any():
                continue

            dst_view = canvas[dst_y, dst_x]
            src_values = canvas[src_y, src_x]
            channels = 4 if copy_alpha else 3
            dst_view[..., :channels][need] = src_values[..., :channels][need]

            mask_view = next_mask[dst_y, dst_x]
            mask_view[need] = True

        if np.array_equal(next_mask, source_mask):
            break  # 已無可滲出的位置
        source_mask = next_mask


def array_to_image(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(arr, mode="RGBA")
