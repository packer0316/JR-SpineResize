"""資源路徑（相容 PyInstaller 打包後的臨時解壓目錄）"""
from __future__ import annotations

import sys
from pathlib import Path


def get_base_path() -> Path:
    """取得資源根目錄：打包後為 _MEIPASS，開發時為專案根目錄。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return Path(__file__).resolve().parent.parent


def get_resource_path(*parts: str) -> Path:
    return get_base_path().joinpath(*parts)


def get_app_icon_path() -> Path:
    return get_resource_path("ico", "JR-SpineResize.ico")
