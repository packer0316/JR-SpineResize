"""
專案檔（.jrspine）的儲存與載入

專案檔只是一份 JSON 紀錄：記下**素材的絕對路徑**與每份專案套用的設定，
**不含任何圖片資料**。載入時依這些路徑重新掃描素材，所以檔案很小，
也不會因為複製專案檔而搬動素材。

素材被移動或刪除時載入不會失敗，只會回報哪些找不到。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config.version import VERSION
from core.project_scanner import scan_projects
from models.process_options import ProcessOptions
from models.spine_project import STATUS_APPLIED, SpineProject

FILE_EXTENSION = ".jrspine"
FILE_FILTER = f"JR-SpineResize 專案 (*{FILE_EXTENSION});;所有檔案 (*.*)"
_FORMAT = "JR-SpineResize project"


def _entry_key(skeleton: str, atlases: list[str]) -> tuple:
    """比對用的鍵：骨架路徑 + atlas 路徑組合（全部小寫化的絕對路徑）"""
    return (skeleton.lower(), tuple(sorted(a.lower() for a in atlases)))


def _project_key(project: SpineProject) -> tuple:
    skeleton = str(project.skeleton_path.resolve()).lower() if project.skeleton_path else ""
    atlases = [str(a.atlas_path.resolve()).lower() for a in project.atlases]
    return _entry_key(skeleton, atlases)


@dataclass
class LoadResult:
    """載入結果：重建出的專案與所有找不到的路徑"""

    projects: list[SpineProject] = field(default_factory=list)
    source_roots: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 素材檔案已不存在
    unmatched: list[str] = field(default_factory=list)  # 檔案在但配對結果不同，設定沒還原
    applied: int = 0
    saved_at: str = ""
    saved_version: str = ""


# ---------------------------------------------------------------- 儲存


def save_project_file(
    projects: list[SpineProject],
    path: Path,
    source_roots: list[Path] | None = None,
) -> Path:
    """
    寫出專案檔。

    未套用設定的專案也會記錄（只是 options 為 null），這樣下次開啟時
    整個工作清單都在，不必重新拖檔。
    """
    entries = []
    for project in projects:
        options = project.applied_options
        entries.append({
            "name": project.name,
            "skeleton": str(project.skeleton_path.resolve()) if project.skeleton_path else "",
            "atlases": [str(a.atlas_path.resolve()) for a in project.atlases],
            "options": options.to_dict() if options is not None else None,
        })

    payload = {
        "format": _FORMAT,
        "app_version": VERSION,
        "saved_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "note": "此檔只記錄素材的絕對路徑與設定，不含任何圖片資料",
        "source_roots": [str(Path(r).resolve()) for r in (source_roots or [])],
        "projects": entries,
    }

    if path.suffix.lower() != FILE_EXTENSION:
        path = path.with_suffix(FILE_EXTENSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------- 載入


def load_project_file(path: Path) -> LoadResult:
    """
    讀入專案檔並依記錄的絕對路徑重新掃描素材。

    Raises:
        ValueError: 檔案不是合法的專案檔。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"無法讀取專案檔：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != _FORMAT:
        raise ValueError("這不是 JR-SpineResize 專案檔")

    result = LoadResult(
        saved_at=str(payload.get("saved_at", "")),
        saved_version=str(payload.get("app_version", "")),
        source_roots=[Path(r) for r in payload.get("source_roots", []) if r],
    )

    # 收集要重新掃描的素材路徑，順手記下已經不存在的
    scan_paths: list[Path] = []
    wanted: dict[tuple, dict] = {}
    for entry in payload.get("projects", []):
        if not isinstance(entry, dict):
            continue
        skeleton = str(entry.get("skeleton", "") or "")
        atlases = [str(a) for a in entry.get("atlases", []) if a]

        present_atlases = []
        for atlas in atlases:
            atlas_path = Path(atlas)
            if atlas_path.is_file():
                present_atlases.append(atlas)
                scan_paths.append(atlas_path)
            else:
                result.missing.append(atlas)
        if skeleton:
            skeleton_path = Path(skeleton)
            if skeleton_path.is_file():
                scan_paths.append(skeleton_path)
            else:
                result.missing.append(skeleton)
                skeleton = ""  # 骨架不在了，靠 atlas 重建（仍可縮放處理）

        if not present_atlases:
            continue  # atlas 全都不在，這份無法重建
        wanted[_entry_key(skeleton, present_atlases)] = entry

    if scan_paths:
        result.projects = scan_projects(scan_paths)

    # 把設定貼回對應的專案
    for project in result.projects:
        entry = wanted.pop(_project_key(project), None)
        if entry is None:
            continue
        raw_options = entry.get("options")
        if raw_options is None:
            continue
        options = ProcessOptions.from_dict(raw_options)
        if not options.source_roots:
            options.source_roots = list(result.source_roots)
        project.applied_options = options
        project.status = STATUS_APPLIED
        result.applied += 1

    # 檔案還在、但重新掃描後配對結果不同的（例如 atlas 被改名或搬走）
    result.unmatched = [
        str(entry.get("name") or entry.get("skeleton") or "")
        for entry in wanted.values()
        if entry.get("options") is not None
    ]
    return result


def describe_load(result: LoadResult) -> str:
    """給使用者看的載入摘要"""
    parts = [f"載入 {len(result.projects)} 份專案，其中 {result.applied} 份已套用設定"]
    if result.saved_at:
        parts.append(f"（存檔時間 {result.saved_at}）")
    return "".join(parts)
