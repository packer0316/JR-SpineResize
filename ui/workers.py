"""背景執行緒：掃描、處理、預覽建構與容量估算，都不阻塞介面"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from config.constants import MODE_RESCALE
from core.pipeline import BatchResult, build_preview, process_asset
from core.project_scanner import scan_projects
from core.spine.texture_store import AtlasTextureStore
from models.process_options import ProcessOptions
from models.spine_project import STATUS_DONE, STATUS_FAILED, SpineProject


class ScanWorker(QThread):
    """掃描拖入的路徑，組成以 skel 為單位的專案"""

    finished_scan = pyqtSignal(list)

    def __init__(self, paths: list[Path], parent=None) -> None:
        super().__init__(parent)
        self._paths = paths

    def run(self) -> None:
        self.finished_scan.emit(scan_projects(self._paths))


class ProcessWorker(QThread):
    """批次處理所有已套用設定的專案"""

    progress = pyqtSignal(int, int, str)
    project_done = pyqtSignal(object)          # SpineProject
    finished_process = pyqtSignal(object, list)  # BatchResult, skipped names

    def __init__(self, projects: list[SpineProject], parent=None) -> None:
        super().__init__(parent)
        self._projects = projects
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        batch = BatchResult()
        rendered_pages: dict[Path, tuple[Path, int]] = {}
        todo = [p for p in self._projects if p.applied_options is not None and p.can_process]
        skipped = [p.name for p in self._projects if p.applied_options is None]
        total = len(todo)

        for index, project in enumerate(todo):
            if self._cancelled:
                break
            self.progress.emit(index, total, project.name)
            options = project.applied_options
            assert options is not None
            ok = True
            detail = ""
            for asset in project.atlases:
                if not asset.is_loadable or asset.missing_pages:
                    continue
                result = process_asset(
                    asset,
                    options,
                    rendered_pages=rendered_pages,
                    progress=lambda msg, i=index: self.progress.emit(i, total, msg),
                )
                batch.results.append(result)
                if not result.ok:
                    ok = False
                    detail = result.error or (
                        result.report.errors[0].message if result.report.errors else "驗證未通過"
                    )
            project.status = STATUS_DONE if ok else STATUS_FAILED
            project.status_detail = detail
            self.project_done.emit(project)

        self.progress.emit(total, total, "完成")
        self.finished_process.emit(batch, skipped)


def _preview_label(options: ProcessOptions) -> str:
    return f"{options.scale_percent:g}%" if options.resize_enabled else "壓縮後"


class PreviewWorker(QThread):
    """
    為選中的專案產生「縮放後」貼圖庫供播放器切換對比，並回報大小估算。

    實際工作由 ``core.pipeline.build_preview`` 完成——與正式輸出同一條路徑，
    所以預覽畫面與估算數字都忠於處理結果。
    """

    built = pyqtSignal(object, object, str)   # project, AtlasTextureStore, label
    estimated = pyqtSignal(object, object)    # project, SizeEstimate
    failed = pyqtSignal(object, str)          # project, error

    def __init__(self, project: SpineProject, options: ProcessOptions, parent=None) -> None:
        super().__init__(parent)
        self.project = project  # 公開：呼叫端用來判斷這份是否已在計算中
        self._options = options

    def run(self) -> None:
        project = self.project
        options = self._options
        primary = project.primary_atlas
        if primary is None or not primary.is_loadable:
            self.failed.emit(project, "沒有可用的 atlas")
            return
        if options.mode != MODE_RESCALE:
            self.failed.emit(project, "「只重算 atlas」模式不提供縮放預覽")
            return
        try:
            build = build_preview(project.atlases, options, preview_asset=primary)
            if build.atlas is not None:
                store = AtlasTextureStore(build.atlas, build.pages)
                self.built.emit(project, store, _preview_label(options))
            self.estimated.emit(project, build.estimate)
        except Exception as exc:  # noqa: BLE001 - 預覽失敗回報即可
            self.failed.emit(project, str(exc))


class EstimateWorker(QThread):
    """
    批次估算多份專案處理後的容量（供清單的「容量變化」欄）。

    不保留任何貼圖影像，所以幾十份專案一起估也不會吃掉記憶體；
    每完成一份就 emit 一次，清單會逐列亮起來。
    """

    estimated = pyqtSignal(object, object)  # project, SizeEstimate
    finished_all = pyqtSignal()

    def __init__(self, jobs: list[tuple[SpineProject, ProcessOptions]], parent=None) -> None:
        super().__init__(parent)
        self._jobs = jobs
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        for project, options in self._jobs:
            if self._cancelled:
                break
            if options.mode != MODE_RESCALE:
                continue  # 只重算 atlas：貼圖原樣複製，容量不變
            try:
                build = build_preview(project.atlases, options)
            except Exception:  # noqa: BLE001 - 單一專案估算失敗不影響其他
                continue
            if not self._cancelled:
                self.estimated.emit(project, build.estimate)
        self.finished_all.emit()
