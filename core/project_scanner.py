"""
以 .skel 為單位的專案掃描

配對規則（依優先序）：

1. 與 skel 同主檔名的 atlas
2. 資料夾內唯一的 atlas
3. 多個 atlas 時，選「區塊名稱被骨架字串引用最多」的那一份

沒被任何 skel 認領的 atlas 自成一個專案（無法預覽，但仍可縮放處理）。
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from core.asset_scanner import find_atlas_files, load_asset
from core.exceptions import SkeletonParseError
from core.skeleton_reader import read_skeleton
from models.spine_asset import SpineAsset
from models.spine_project import SpineProject
from utils.file_utils import atlas_stem


def find_skeleton_files(paths: Iterable[Path]) -> list[Path]:
    found: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for pattern in ("*.skel", "*.skel.bytes"):
                found.update(p for p in path.rglob(pattern) if p.is_file())
        elif path.is_file() and path.suffix.lower() in (".skel",):
            found.add(path)
        elif path.is_file() and path.name.lower().endswith(".skel.bytes"):
            found.add(path)
    return sorted(found)


def _atlas_score(asset: SpineAsset, needed: set[str]) -> int:
    """骨架字串池中出現的區塊名稱數（弱參考，僅用於排序）"""
    if not asset.atlas or not needed:
        return 0
    return sum(1 for region in asset.atlas.regions if region.name in needed)


def scan_projects(paths: Iterable[Path]) -> list[SpineProject]:
    """掃描所有 .skel 與 .atlas，組成以 skel 為單位的專案清單。"""
    skeleton_paths = find_skeleton_files(paths)
    atlas_paths = find_atlas_files(paths)

    # atlas 只載入一次（多個 skel 可能共用）
    assets: dict[Path, SpineAsset] = {p: load_asset(p) for p in atlas_paths}
    claimed: set[Path] = set()
    projects: list[SpineProject] = []

    for skel_path in skeleton_paths:
        project = SpineProject(skeleton_path=skel_path)
        try:
            project.skeleton_info = read_skeleton(skel_path)
        except SkeletonParseError as exc:
            project.warnings.append(str(exc))

        stem = skel_path.stem.lower()
        folder = skel_path.parent
        folder_assets = [a for p, a in assets.items() if p.parent == folder]

        chosen: SpineAsset | None = None
        # 1. 同主檔名
        for asset in folder_assets:
            if atlas_stem(asset.atlas_path).lower() == stem:
                chosen = asset
                break
        # 2. 資料夾唯一
        if chosen is None and len(folder_assets) == 1:
            chosen = folder_assets[0]
        # 3. 字串池覆蓋率
        if chosen is None and folder_assets:
            needed = set(project.skeleton_info.string_pool) if project.skeleton_info else set()
            scored = sorted(folder_assets, key=lambda a: _atlas_score(a, needed), reverse=True)
            chosen = scored[0]
            if len(folder_assets) > 1:
                project.warnings.append(
                    f"資料夾內有 {len(folder_assets)} 份 atlas，自動選擇 {chosen.atlas_path.name}"
                )

        if chosen is not None:
            # skel 專屬配對：覆寫 asset 掃描時自己找的骨架
            chosen.skeleton_path = skel_path
            chosen.skeleton = project.skeleton_info
            project.atlases.append(chosen)
            claimed.add(chosen.atlas_path)
            if chosen.load_error:
                project.warnings.append(chosen.load_error)
            project.warnings.extend(w for w in chosen.warnings if "找不到對應" not in w)
        else:
            project.warnings.append("找不到對應的 .atlas，無法處理")

        projects.append(project)

    # 孤兒 atlas：沒被任何 skel 認領
    for path, asset in assets.items():
        if path in claimed:
            continue
        project = SpineProject(atlases=[asset])
        if asset.skeleton_path is not None:
            # asset_scanner 自己配到的骨架（可能是 json）
            project.skeleton_path = asset.skeleton_path
            project.skeleton_info = asset.skeleton
        else:
            project.warnings.append("沒有骨架檔（無法預覽，仍可縮放）")
        if asset.load_error:
            project.warnings.append(asset.load_error)
        project.warnings.extend(asset.warnings)
        projects.append(project)

    projects.sort(key=lambda p: (str(p.folder).lower(), p.name.lower()))
    return projects
