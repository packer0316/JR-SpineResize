"""檔案操作工具"""
from __future__ import annotations

import shutil
from pathlib import Path

from config.constants import ATLAS_EXTENSIONS


def atlas_stem(path: Path) -> str:
    """取得 atlas 的主檔名（同時處理 ``.atlas`` 與 ``.atlas.txt``）。"""
    name = path.name
    for ext in sorted(ATLAS_EXTENSIONS, key=len, reverse=True):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return path.stem


def is_atlas_file(path: Path) -> bool:
    return path.is_file() and path.name.lower().endswith(ATLAS_EXTENSIONS)


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024.0 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def backup_once(path: Path) -> Path | None:
    """
    覆寫原檔前先備份一次。

    已經有 .bak 就不再覆蓋，避免第二次執行把「原始檔的備份」換成「已處理檔的備份」。
    """
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.exists():
        return backup
    shutil.copy2(path, backup)
    return backup


def copy_file(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def longest_matching_root(target: Path, roots: list[Path]) -> Path | None:
    """在多個來源根目錄中找出 target 所屬、且最深的那一個。"""
    best: Path | None = None
    for root in roots:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if best is None or len(root.parts) > len(best.parts):
            best = root
    return best
