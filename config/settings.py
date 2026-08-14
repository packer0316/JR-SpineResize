"""使用者設定的儲存與讀取（記住上次的處理選項）"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSettings

from config.constants import (
    ALPHA_MODE_PREMULTIPLY,
    APP_NAME,
    BLEED_RGB,
    DEFAULT_BLEED_PX,
    DEFAULT_PNG_FORMAT,
    DEFAULT_RESAMPLE,
    DEFAULT_SCALE_PERCENT,
    DEFAULT_SUBFOLDER_NAME,
    MODE_RESCALE,
    ORG_NAME,
    OUTPUT_SUBFOLDER,
    PAGE_ALIGN_NONE,
)
from models.process_options import ProcessOptions


def _settings() -> QSettings:
    return QSettings(ORG_NAME, APP_NAME)


def load_options() -> ProcessOptions:
    s = _settings()
    output_dir = s.value("output/dir", "", type=str)
    prescaled = s.value("scale/prescaled_dir", "", type=str)
    return ProcessOptions(
        mode=s.value("mode", MODE_RESCALE, type=str),
        scale_percent=s.value("scale/percent", DEFAULT_SCALE_PERCENT, type=float),
        resample=s.value("scale/resample", DEFAULT_RESAMPLE, type=str),
        alpha_mode=s.value("scale/alpha_mode", ALPHA_MODE_PREMULTIPLY, type=str),
        bleed=s.value("scale/bleed", BLEED_RGB, type=str),
        bleed_px=s.value("scale/bleed_px", DEFAULT_BLEED_PX, type=int),
        page_align=s.value("scale/page_align", PAGE_ALIGN_NONE, type=int),
        png_format=s.value("scale/png_format", DEFAULT_PNG_FORMAT, type=str),
        prescaled_dir=Path(prescaled) if prescaled else None,
        derive_scale_from_image=s.value("scale/derive_from_image", True, type=bool),
        output_mode=s.value("output/mode", OUTPUT_SUBFOLDER, type=str),
        output_dir=Path(output_dir) if output_dir else None,
        subfolder_name=s.value("output/subfolder", DEFAULT_SUBFOLDER_NAME, type=str),
        filename_suffix=s.value("output/suffix", "", type=str),
        copy_skeleton=s.value("output/copy_skeleton", True, type=bool),
    )


def save_options(options: ProcessOptions) -> None:
    s = _settings()
    s.setValue("mode", options.mode)
    s.setValue("scale/percent", options.scale_percent)
    s.setValue("scale/resample", options.resample)
    s.setValue("scale/alpha_mode", options.alpha_mode)
    s.setValue("scale/bleed", options.bleed)
    s.setValue("scale/bleed_px", options.bleed_px)
    s.setValue("scale/page_align", options.page_align)
    s.setValue("scale/png_format", options.png_format)
    s.setValue("scale/prescaled_dir", str(options.prescaled_dir) if options.prescaled_dir else "")
    s.setValue("scale/derive_from_image", options.derive_scale_from_image)
    s.setValue("output/mode", options.output_mode)
    s.setValue("output/dir", str(options.output_dir) if options.output_dir else "")
    s.setValue("output/subfolder", options.subfolder_name)
    s.setValue("output/suffix", options.filename_suffix)
    s.setValue("output/copy_skeleton", options.copy_skeleton)


def load_window_geometry() -> bytes | None:
    value = _settings().value("window/geometry")
    return value if isinstance(value, (bytes, bytearray)) else None


def save_window_geometry(data: bytes) -> None:
    _settings().setValue("window/geometry", data)


def load_last_folder() -> str:
    return _settings().value("last_folder", "", type=str)


def save_last_folder(path: str) -> None:
    _settings().setValue("last_folder", path)


def load_theme() -> str:
    return _settings().value("theme", "light", type=str)


def save_theme(name: str) -> None:
    _settings().setValue("theme", name)
