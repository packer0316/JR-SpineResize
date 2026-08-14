"""Spine 資產模型：一份 .atlas + 其貼圖頁面 + 對應的骨架檔"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.skeleton_reader import SkeletonInfo
from models.atlas_data import AtlasFile


@dataclass
class SpineAsset:
    atlas_path: Path
    atlas: AtlasFile | None = None
    skeleton_path: Path | None = None
    skeleton: SkeletonInfo | None = None
    # atlas 中的頁面名稱 -> 實際檔案路徑（找不到時為 None）
    pages: dict[str, Path | None] = field(default_factory=dict)
    load_error: str = ""
    warnings: list[str] = field(default_factory=list)
    selected: bool = True

    @property
    def name(self) -> str:
        return self.atlas_path.stem

    @property
    def folder(self) -> Path:
        return self.atlas_path.parent

    @property
    def is_loadable(self) -> bool:
        return self.atlas is not None and not self.load_error

    @property
    def missing_pages(self) -> list[str]:
        return [name for name, path in self.pages.items() if path is None]

    @property
    def region_count(self) -> int:
        return self.atlas.region_count if self.atlas else 0

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def spine_version(self) -> str:
        return self.skeleton.version if self.skeleton else ""

    def page_size_text(self) -> str:
        if not self.atlas:
            return ""
        return " / ".join(f"{w}x{h}" for w, h in (p.size for p in self.atlas.pages))
