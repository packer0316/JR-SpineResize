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
    # 使用者拖入的來源根目錄，用於在自訂輸出時保留相對路徑結構
    source_roots: list[Path] = field(default_factory=list)

    @property
    def scale(self) -> float:
        if not self.resize_enabled:
            return 1.0
        return self.scale_percent / 100.0

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        """供專案檔儲存（路徑一律存絕對路徑字串）"""
        return {
            "mode": self.mode,
            "resize_enabled": self.resize_enabled,
            "scale_percent": self.scale_percent,
            "resample": self.resample,
            "alpha_mode": self.alpha_mode,
            "bleed": self.bleed,
            "bleed_px": self.bleed_px,
            "page_align": self.page_align,
            "compression": self.compression.to_dict(),
            "prescaled_dir": str(self.prescaled_dir.resolve()) if self.prescaled_dir else "",
            "derive_scale_from_image": self.derive_scale_from_image,
            "output_mode": self.output_mode,
            "output_dir": str(self.output_dir.resolve()) if self.output_dir else "",
            "subfolder_name": self.subfolder_name,
            "filename_suffix": self.filename_suffix,
            "copy_skeleton": self.copy_skeleton,
            "source_roots": [str(Path(r).resolve()) for r in self.source_roots],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProcessOptions":
        """從專案檔還原（欄位缺失或損壞時退回預設值）"""
        options = cls()
        try:
            options.mode = str(data.get("mode", options.mode))
            options.resize_enabled = bool(data.get("resize_enabled", options.resize_enabled))
            options.scale_percent = float(data.get("scale_percent", options.scale_percent))
            options.resample = str(data.get("resample", options.resample))
            options.alpha_mode = str(data.get("alpha_mode", options.alpha_mode))
            options.bleed = str(data.get("bleed", options.bleed))
            options.bleed_px = int(data.get("bleed_px", options.bleed_px))
            options.page_align = int(data.get("page_align", options.page_align))
            options.compression = CompressionOptions.from_dict(data.get("compression", {}))
            prescaled = data.get("prescaled_dir", "")
            options.prescaled_dir = Path(prescaled) if prescaled else None
            options.derive_scale_from_image = bool(
                data.get("derive_scale_from_image", options.derive_scale_from_image)
            )
            options.output_mode = str(data.get("output_mode", options.output_mode))
            output_dir = data.get("output_dir", "")
            options.output_dir = Path(output_dir) if output_dir else None
            options.subfolder_name = str(data.get("subfolder_name", options.subfolder_name))
            options.filename_suffix = str(data.get("filename_suffix", options.filename_suffix))
            options.copy_skeleton = bool(data.get("copy_skeleton", options.copy_skeleton))
            options.source_roots = [Path(r) for r in data.get("source_roots", []) if r]
        except (ValueError, TypeError):
            pass  # 損壞的欄位保持預設
        return options

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
