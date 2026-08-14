"""處理選項"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.constants import (
    ALPHA_MODE_PREMULTIPLY,
    BLEED_RGB,
    DEFAULT_BLEED_PX,
    DEFAULT_DITHERING,
    DEFAULT_PNG_FORMAT,
    DEFAULT_RESAMPLE,
    DEFAULT_SCALE_PERCENT,
    DEFAULT_SUBFOLDER_NAME,
    MODE_RESCALE,
    OUTPUT_SUBFOLDER,
    PAGE_ALIGN_NONE,
)


@dataclass
class ProcessOptions:
    """一次處理的全部設定"""

    # ---- 模式 ----------------------------------------------------------
    # MODE_RESCALE     由本工具逐圖塊縮放並重寫 atlas（推薦）
    # MODE_REMAP_ONLY  貼圖已在外部縮好，只重算 atlas 數值
    mode: str = MODE_RESCALE

    # ---- 縮放 ----------------------------------------------------------
    scale_percent: float = DEFAULT_SCALE_PERCENT
    resample: str = DEFAULT_RESAMPLE
    alpha_mode: str = ALPHA_MODE_PREMULTIPLY
    bleed: str = BLEED_RGB
    bleed_px: int = DEFAULT_BLEED_PX
    page_align: int = PAGE_ALIGN_NONE
    # 貼圖輸出的色彩編碼；預設跟隨來源，避免已量化的素材被存成 32-bit 而變大
    png_format: str = DEFAULT_PNG_FORMAT
    dithering: float = DEFAULT_DITHERING

    # ---- MODE_REMAP_ONLY 專用 ------------------------------------------
    # 已縮好的貼圖所在資料夾；None 代表與 atlas 同層
    prescaled_dir: Path | None = None
    # True 時直接用「已縮好的圖 / 原圖」的實際像素比例當縮放比，忽略 scale_percent
    derive_scale_from_image: bool = True

    # ---- 輸出 ----------------------------------------------------------
    output_mode: str = OUTPUT_SUBFOLDER
    output_dir: Path | None = None
    subfolder_name: str = DEFAULT_SUBFOLDER_NAME
    filename_suffix: str = ""
    copy_skeleton: bool = True
    overwrite: bool = True
    # 使用者拖入的來源根目錄，用於在自訂輸出時保留相對路徑結構
    source_roots: list[Path] = field(default_factory=list)

    @property
    def scale(self) -> float:
        return self.scale_percent / 100.0

    def describe(self) -> str:
        if self.mode == MODE_RESCALE:
            return f"縮放並重寫（{self.scale_percent:g}%，{self.resample}）"
        return "只重算 atlas（貼圖已在外部縮好）"
