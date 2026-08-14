"""
壓縮選項模型（移植自 JR-Img-Compresser，設定項目與行為一致）

貼圖輸出永遠保持原始檔案格式（atlas 以檔名引用貼圖，轉檔會斷開引用），
因此沒有「輸出格式」選項；PNG / JPEG / WebP 的參數依貼圖副檔名自動套用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PngMode(Enum):
    """PNG 壓縮模式"""

    LOSSY = "lossy"        # 智慧有損（imagequant 量化，同 TinyPNG）
    LOSSLESS = "lossless"  # 無損（零品質損失，只重新最佳化編碼）


class PngColorFormat(Enum):
    """
    PNG 色彩格式（模擬遊戲引擎貼圖格式）

    PNG 規格寫不出真正的 RGBA4444 / RGB565，這裡把各通道量化到目標
    位元深度後仍存成合法 PNG：畫面與引擎轉檔後一致，檔案也因熵降低變小。
    """

    RGBA8888 = "rgba8888"  # 原始 32-bit，不量化
    RGBA5551 = "rgba5551"  # 16-bit，透明度只剩鏤空/不鏤空
    RGBA4444 = "rgba4444"  # 16-bit，引擎最常用的半透明貼圖格式
    RGB565 = "rgb565"      # 16-bit，不含透明通道（透明區合成白底）


class ChromaSubsampling(Enum):
    """JPEG 色度取樣"""

    AUTO = "auto"
    CS_444 = "4:4:4"
    CS_422 = "4:2:2"
    CS_420 = "4:2:0"


class CompressionEffort(Enum):
    """無損最佳化強度（影響 oxipng 等級與耗時）"""

    FAST = "fast"
    STANDARD = "standard"
    MAX = "max"


@dataclass
class CompressionOptions:
    """壓縮選項（欄位語義與 JR-Img-Compresser 相同）"""

    # ---- PNG（Spine 貼圖絕大多數是 PNG）----
    png_mode: PngMode = PngMode.LOSSLESS
    png_quality: int = 80            # 有損品質上限（1-100）
    png_dithering: float = 1.0       # 有損量化的漸層抖動（0.0-1.0）
    png_color_format: PngColorFormat = PngColorFormat.RGBA8888
    png_format_dither: bool = False  # 色彩格式量化時的 Bayer 有序抖動
    effort: CompressionEffort = CompressionEffort.STANDARD

    # ---- JPEG / WebP（少數非 PNG 貼圖沿用預設值）----
    quality: int = 92
    progressive: bool = True
    chroma_subsampling: ChromaSubsampling = ChromaSubsampling.AUTO
    webp_quality: int = 85
    webp_lossless: bool = True       # 貼圖預設走無損，避免悄悄劣化

    # ---- 通用 ----
    remove_exif: bool = True
    target_size_enabled: bool = False
    target_size_kb: int = 500

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        return {
            "png_mode": self.png_mode.value,
            "png_quality": self.png_quality,
            "png_dithering": self.png_dithering,
            "png_color_format": self.png_color_format.value,
            "png_format_dither": self.png_format_dither,
            "effort": self.effort.value,
            "quality": self.quality,
            "progressive": self.progressive,
            "chroma_subsampling": self.chroma_subsampling.value,
            "webp_quality": self.webp_quality,
            "webp_lossless": self.webp_lossless,
            "remove_exif": self.remove_exif,
            "target_size_enabled": self.target_size_enabled,
            "target_size_kb": self.target_size_kb,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CompressionOptions":
        options = cls()
        try:
            options.png_mode = PngMode(data.get("png_mode", options.png_mode.value))
            options.png_quality = int(data.get("png_quality", options.png_quality))
            options.png_dithering = float(data.get("png_dithering", options.png_dithering))
            options.png_color_format = PngColorFormat(
                data.get("png_color_format", options.png_color_format.value)
            )
            options.png_format_dither = bool(
                data.get("png_format_dither", options.png_format_dither)
            )
            options.effort = CompressionEffort(data.get("effort", options.effort.value))
            options.quality = int(data.get("quality", options.quality))
            options.progressive = bool(data.get("progressive", options.progressive))
            options.chroma_subsampling = ChromaSubsampling(
                data.get("chroma_subsampling", options.chroma_subsampling.value)
            )
            options.webp_quality = int(data.get("webp_quality", options.webp_quality))
            options.webp_lossless = bool(data.get("webp_lossless", options.webp_lossless))
            options.remove_exif = bool(data.get("remove_exif", options.remove_exif))
            options.target_size_enabled = bool(
                data.get("target_size_enabled", options.target_size_enabled)
            )
            options.target_size_kb = int(data.get("target_size_kb", options.target_size_kb))
        except (ValueError, TypeError):
            pass  # 資料損壞的欄位保持預設
        return options

    # ------------------------------------------------------------ 描述

    @property
    def alters_pixels(self) -> bool:
        """True 表示輸出像素與輸入不同（有損量化或色彩格式轉換）"""
        return (
            self.png_mode == PngMode.LOSSY
            or self.png_color_format != PngColorFormat.RGBA8888
        )

    def describe_png(self) -> str:
        mode = "無損" if self.png_mode == PngMode.LOSSLESS else f"智慧有損 Q{self.png_quality}"
        if self.png_color_format != PngColorFormat.RGBA8888:
            return f"{mode}・{self.png_color_format.value.upper()}"
        return mode
