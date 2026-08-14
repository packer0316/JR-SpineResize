"""處理選項"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.constants import (
    ALPHA_MODE_PREMULTIPLY,
    BLEED_RGB,
    DEFAULT_BLEED_PX,
    DEFAULT_RESAMPLE,
    DEFAULT_SCALE_PERCENT,
    DEFAULT_SUBFOLDER_NAME,
    MODE_RESCALE,
    OUTPUT_INPLACE,
    PAGE_ALIGN_NONE,
)
from models.compression_options import CompressionOptions


@dataclass
class ProcessOptions:
    """一次處理的全部設定"""

    # ---- 模式 ----------------------------------------------------------
    # MODE_RESCALE     由本工具逐圖塊縮放並重寫 atlas（推薦）
    # MODE_REMAP_ONLY  貼圖已在外部縮好，只重算 atlas 數值
    mode: str = MODE_RESCALE

    # ---- 尺寸調整（綁定整個 Spine 專案）--------------------------------
    # 關閉時比例固定 100%，變成「只壓縮、不縮放」
    resize_enabled: bool = True
    scale_percent: float = DEFAULT_SCALE_PERCENT
    resample: str = DEFAULT_RESAMPLE
    alpha_mode: str = ALPHA_MODE_PREMULTIPLY
    bleed: str = BLEED_RGB
    bleed_px: int = DEFAULT_BLEED_PX
    page_align: int = PAGE_ALIGN_NONE

    # ---- 壓縮（與 JR-Img-Compresser 相同的設定項）----------------------
    compression: CompressionOptions = field(default_factory=CompressionOptions)

    # ---- MODE_REMAP_ONLY 專用 ------------------------------------------
    # 已縮好的貼圖所在資料夾；None 代表與 atlas 同層
    prescaled_dir: Path | None = None
    # True 時直接用「已縮好的圖 / 原圖」的實際像素比例當縮放比，忽略 scale_percent
    derive_scale_from_image: bool = True

    # ---- 輸出 ----------------------------------------------------------
    output_mode: str = OUTPUT_INPLACE
    output_dir: Path | None = None
    subfolder_name: str = DEFAULT_SUBFOLDER_NAME
    filename_suffix: str = ""
    copy_skeleton: bool = True
    # 匯出處理紀錄：每張貼圖的檔名、絕對路徑、尺寸與容量變化
    export_log: bool = False
    # 使用者拖入的來源根目錄，用於在自訂輸出時保留相對路徑結構
    source_roots: list[Path] = field(default_factory=list)

    @property
    def scale(self) -> float:
        if not self.resize_enabled:
            return 1.0
        return self.scale_percent / 100.0

    def render_fingerprint(self) -> tuple:
        """會影響輸出貼圖內容的所有欄位（預覽/估算快取用）"""
        return (
            self.mode,
            self.resize_enabled,
            self.scale_percent,
            self.resample,
            self.alpha_mode,
            self.bleed,
            self.bleed_px,
            self.page_align,
            tuple(sorted(self.compression.to_dict().items())),
        )

    def describe(self) -> str:
        if self.mode == MODE_RESCALE:
            if not self.resize_enabled:
                return f"只壓縮（{self.compression.describe_png()}）"
            return f"縮放 {self.scale_percent:g}% + 壓縮（{self.compression.describe_png()}）"
        return "只重算 atlas（貼圖已在外部縮好）"
