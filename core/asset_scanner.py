"""
Spine 資產掃描

從使用者拖進來的檔案／資料夾中找出所有 .atlas，並替每一份配對貼圖頁面與骨架檔。
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from config.constants import PAGE_EXTENSIONS, SKELETON_BINARY_EXTENSIONS
from core.atlas_parser import parse_atlas_file
from core.exceptions import AtlasParseError, SkeletonParseError
from core.skeleton_reader import read_skeleton
from models.spine_asset import SpineAsset
from utils.file_utils import atlas_stem, is_atlas_file


def find_atlas_files(paths: Iterable[Path]) -> list[Path]:
    """展開資料夾，收集所有 .atlas（去重並排序）。"""
    found: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for ext in ("*.atlas", "*.atlas.txt"):
                found.update(p for p in path.rglob(ext) if p.is_file())
        elif is_atlas_file(path):
            found.add(path)
    return sorted(found)


def resolve_page(atlas_dir: Path, page_name: str) -> Path | None:
    """
    找出 atlas 頁面對應的實際圖檔。

    頁面名稱是相對於 atlas 所在資料夾的路徑；找不到時依序退而求其次嘗試
    大小寫不敏感比對與其他影像副檔名（例如 atlas 寫 .png 但素材已轉成 .webp）。
    """
    direct = atlas_dir / page_name
    if direct.is_file():
        return direct

    parent = direct.parent
    if not parent.is_dir():
        return None

    target = direct.name.lower()
    entries = [p for p in parent.iterdir() if p.is_file()]
    for entry in entries:
        if entry.name.lower() == target:
            return entry

    stem = Path(page_name).stem.lower()
    for entry in entries:
        if entry.stem.lower() == stem and entry.suffix.lower() in PAGE_EXTENSIONS:
            return entry
    return None


def _looks_like_json_skeleton(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return '"skeleton"' in head or '"bones"' in head


def resolve_skeleton(atlas_path: Path) -> Path | None:
    """找出與 atlas 對應的骨架檔（優先同名）。"""
    folder = atlas_path.parent
    stem = atlas_stem(atlas_path)

    for ext in (*SKELETON_BINARY_EXTENSIONS, ".json"):
        candidate = folder / f"{stem}{ext}"
        if candidate.is_file():
            if ext == ".json" and not _looks_like_json_skeleton(candidate):
                continue
            return candidate

    binaries = sorted(p for p in folder.glob("*.skel") if p.is_file())
    if len(binaries) == 1:
        return binaries[0]

    jsons = sorted(p for p in folder.glob("*.json") if _looks_like_json_skeleton(p))
    if len(jsons) == 1:
        return jsons[0]
    return None


def load_asset(atlas_path: Path) -> SpineAsset:
    """載入單一 atlas 並配對其頁面與骨架。"""
    asset = SpineAsset(atlas_path=atlas_path)

    try:
        asset.atlas = parse_atlas_file(atlas_path)
    except AtlasParseError as exc:
        asset.load_error = str(exc)
        return asset

    atlas_dir = atlas_path.parent
    for page in asset.atlas.pages:
        asset.pages[page.name] = resolve_page(atlas_dir, page.name)

    missing = asset.missing_pages
    if missing:
        asset.warnings.append(f"找不到 {len(missing)} 張貼圖：{'、'.join(missing[:3])}")

    skeleton_path = resolve_skeleton(atlas_path)
    if skeleton_path is None:
        asset.warnings.append("找不到對應的 .skel / .json")
    else:
        asset.skeleton_path = skeleton_path
        try:
            asset.skeleton = read_skeleton(skeleton_path)
        except SkeletonParseError as exc:
            asset.warnings.append(str(exc))

    return asset


def scan(paths: Iterable[Path]) -> list[SpineAsset]:
    """掃描並載入所有 Spine 資產。"""
    return [load_asset(p) for p in find_atlas_files(paths)]
