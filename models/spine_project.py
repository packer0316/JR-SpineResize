"""Spine 專案模型：以一個 .skel 為單位，聚合其 atlas 與貼圖頁面"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.skeleton_reader import SkeletonInfo
from models.process_options import ProcessOptions
from models.spine_asset import SpineAsset

STATUS_IDLE = "idle"          # 尚未套用設定
STATUS_APPLIED = "applied"    # 已套用，等待處理
STATUS_DONE = "done"          # 處理完成
STATUS_FAILED = "failed"      # 處理失敗


@dataclass
class SpineProject:
    """一個 .skel（或孤兒 atlas）專案"""

    skeleton_path: Path | None = None
    skeleton_info: SkeletonInfo | None = None
    atlases: list[SpineAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # 套用/處理狀態
    applied_options: ProcessOptions | None = None
    status: str = STATUS_IDLE
    status_detail: str = ""
    # 套用後由 PreviewWorker 產生的檔案大小預估：
    # {"fingerprint": tuple, "pages": [...], "src_total": int, "est_total": int}
    size_estimate: dict | None = None

    @property
    def name(self) -> str:
        if self.skeleton_path is not None:
            return self.skeleton_path.stem
        if self.atlases:
            return self.atlases[0].name
        return "(空專案)"

    @property
    def folder(self) -> Path:
        if self.skeleton_path is not None:
            return self.skeleton_path.parent
        return self.atlases[0].folder if self.atlases else Path(".")

    @property
    def spine_version(self) -> str:
        return self.skeleton_info.version if self.skeleton_info else ""

    @property
    def can_preview(self) -> bool:
        """預覽播放需要 3.8 binary skel + 至少一份可用 atlas"""
        return (
            self.skeleton_path is not None
            and self.skeleton_path.suffix.lower() != ".json"
            and self.spine_version.startswith("3.8")
            and any(a.is_loadable and not a.missing_pages for a in self.atlases)
        )

    @property
    def can_process(self) -> bool:
        return any(a.is_loadable and not a.missing_pages for a in self.atlases)

    @property
    def primary_atlas(self) -> SpineAsset | None:
        for asset in self.atlases:
            if asset.is_loadable and not asset.missing_pages:
                return asset
        return self.atlases[0] if self.atlases else None

    @property
    def page_count(self) -> int:
        return sum(a.page_count for a in self.atlases)

    @property
    def region_count(self) -> int:
        return sum(a.region_count for a in self.atlases)

    def page_size_text(self) -> str:
        parts = [a.page_size_text() for a in self.atlases if a.atlas]
        return " / ".join(p for p in parts if p)

    @property
    def source_bytes(self) -> int:
        total = 0
        seen: set[Path] = set()
        for asset in self.atlases:
            for path in asset.pages.values():
                if path and path.exists() and path not in seen:
                    seen.add(path)
                    total += path.stat().st_size
        return total

    @property
    def status_text(self) -> str:
        if self.status == STATUS_DONE:
            return "完成"
        if self.status == STATUS_FAILED:
            return f"失敗：{self.status_detail[:30]}" if self.status_detail else "失敗"
        if self.status == STATUS_APPLIED and self.applied_options is not None:
            options = self.applied_options
            if not options.resize_enabled:
                return "已套用（只壓縮）"
            return f"已套用 {options.scale_percent:g}%"
        if not self.can_process:
            return self.warnings[0][:36] if self.warnings else "無法處理"
        return "未套用"

    @property
    def status_colour(self) -> str:
        if self.status == STATUS_DONE:
            return "#16a34a"
        if self.status == STATUS_FAILED:
            return "#dc2626"
        if self.status == STATUS_APPLIED:
            return "#2f6fed"
        if not self.can_process:
            return "#dc2626"
        return "#6b7280"
