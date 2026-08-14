"""主視窗：左＝skel 專案清單、中＝檔案與播放預覽、右＝縮放設定與套用"""
from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from config import settings as user_settings
from config.constants import APP_NAME, APP_TITLE, OUTPUT_CUSTOM, OUTPUT_INPLACE
from config.version import VERSION
from core.pipeline import BatchResult
from models.spine_project import STATUS_APPLIED, STATUS_IDLE, SpineProject
from ui.components.project_detail import ProjectDetail
from ui.components.project_list import ProjectList
from ui.components.settings_panel import SettingsPanel
from ui.dialogs.about_dialog import AboutDialog
from ui.dialogs.progress_dialog import ProgressDialog
from ui.dialogs.report_dialog import ReportDialog
from ui.styles.theme import THEMES, build_stylesheet
from ui.workers import PreviewWorker, ProcessWorker, ScanWorker


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION} — {APP_TITLE}")
        self.resize(1480, 900)
        self.setAcceptDrops(True)

        self._scan_worker: ScanWorker | None = None
        self._process_worker: ProcessWorker | None = None
        self._preview_workers: list[PreviewWorker] = []
        self._progress: ProgressDialog | None = None
        self._source_roots: list[Path] = []
        # 縮放後貼圖庫快取：id(project) -> (設定指紋, AtlasTextureStore, label)
        self._preview_cache: dict[int, tuple[tuple, object, str]] = {}

        self._build_ui()
        theme = user_settings.load_theme()
        index = self.theme_combo.findData(theme)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)
        self._apply_theme(theme)
        self.settings_panel.set_options(user_settings.load_options())
        self._update_footer()

        geometry = user_settings.load_window_geometry()
        if geometry:
            self.restoreGeometry(geometry)

    # ------------------------------------------------------------ 介面

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        layout.addLayout(self._build_header())
        layout.addLayout(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.project_list = ProjectList()
        self.project_list.setMinimumWidth(360)
        self.project_list.selection_changed.connect(self._on_project_selected)
        splitter.addWidget(self.project_list)

        self.detail = ProjectDetail()
        self.detail.setMinimumWidth(380)
        splitter.addWidget(self.detail)

        splitter.addWidget(self._build_settings_column())

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([460, 560, 420])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        layout.addLayout(self._build_footer())
        self.setCentralWidget(central)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("拖放資料夾或檔案到視窗中——每個 .skel 是一個專案")
        title.setProperty("role", "heading")
        row.addWidget(title)
        row.addStretch(1)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("標準", "light")
        self.theme_combo.addItem("暗黑", "dark")
        self.theme_combo.setFixedWidth(90)
        self.theme_combo.currentIndexChanged.connect(
            lambda: self._apply_theme(self.theme_combo.currentData())
        )
        row.addWidget(self.theme_combo)

        about = QPushButton("原理說明")
        about.clicked.connect(lambda: AboutDialog(self).exec())
        row.addWidget(about)
        return row

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        for text, slot in (
            ("加入資料夾", self._add_folder),
            ("加入檔案", self._add_files),
            ("清空", self._clear),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _build_settings_column(self) -> QWidget:
        column = QWidget()
        column.setMinimumWidth(400)
        column.setMaximumWidth(520)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.settings_panel = SettingsPanel()
        layout.addWidget(self.settings_panel, 1)

        apply_row = QHBoxLayout()
        self.apply_button = QPushButton("套用到此專案")
        self.apply_button.setProperty("role", "primary")
        self.apply_button.clicked.connect(self._apply_current)
        apply_row.addWidget(self.apply_button, 1)

        self.apply_all_button = QPushButton("套用到全部")
        self.apply_all_button.clicked.connect(self._apply_all)
        apply_row.addWidget(self.apply_all_button)

        self.unapply_button = QPushButton("取消套用")
        self.unapply_button.clicked.connect(self._unapply_current)
        apply_row.addWidget(self.unapply_button)
        layout.addLayout(apply_row)

        hint = QLabel("套用後即可在中間預覽切換「原始 / 縮放後」播放對比。")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return column

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.status_label = QLabel("尚未載入任何專案")
        self.status_label.setProperty("role", "hint")
        row.addWidget(self.status_label)
        row.addStretch(1)

        self.start_button = QPushButton("開始處理")
        self.start_button.setProperty("role", "primary")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start_processing)
        row.addWidget(self.start_button)
        return row

    def _apply_theme(self, key: str) -> None:
        self.setStyleSheet(build_stylesheet(THEMES.get(key, THEMES["light"])))
        user_settings.save_theme(key)

    # ------------------------------------------------------------ 載入

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile()
        ]
        if paths:
            self._load_paths(paths)
            event.acceptProposedAction()

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "選擇 Spine 檔案", user_settings.load_last_folder(),
            "Spine (*.skel *.atlas *.atlas.txt);;所有檔案 (*.*)",
        )
        if files:
            self._load_paths([Path(f) for f in files])

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "選擇資料夾", user_settings.load_last_folder())
        if folder:
            self._load_paths([Path(folder)])

    def _load_paths(self, paths: list[Path]) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        for path in paths:
            root = path if path.is_dir() else path.parent
            if root not in self._source_roots:
                self._source_roots.append(root)
        user_settings.save_last_folder(str(paths[0] if paths[0].is_dir() else paths[0].parent))

        self.status_label.setText("掃描中…")
        self._scan_worker = ScanWorker(paths, self)
        self._scan_worker.finished_scan.connect(self._on_scan_finished)
        self._scan_worker.start()

    def _on_scan_finished(self, projects: list) -> None:
        self._scan_worker = None
        existing_keys = {
            (p.skeleton_path, tuple(a.atlas_path for a in p.atlases))
            for p in self.project_list.projects
        }
        merged = list(self.project_list.projects)
        for project in projects:
            key = (project.skeleton_path, tuple(a.atlas_path for a in project.atlases))
            if key not in existing_keys:
                merged.append(project)
        self.project_list.set_projects(merged)
        self._update_footer()
        if not merged:
            QMessageBox.information(self, APP_NAME, "找不到任何 .skel 或 .atlas 檔案。")

    def _clear(self) -> None:
        self.project_list.clear_projects()
        self._source_roots.clear()
        self._preview_cache.clear()
        self.detail.show_project(None)
        self._update_footer()

    # ------------------------------------------------------------ 選擇與套用

    def _options_fingerprint(self, options) -> tuple:
        # 涵蓋所有會影響輸出貼圖的欄位（含壓縮設定），避免預覽/估算沿用過期快取
        return options.render_fingerprint()

    def _on_project_selected(self, project: SpineProject | None) -> None:
        self.detail.show_project(project)
        if project is None:
            return
        if project.applied_options is not None:
            self.settings_panel.set_options(project.applied_options)
            self._attach_preview(project)

    def _attach_preview(self, project: SpineProject) -> None:
        """把快取的（或新建的）縮放後貼圖庫掛到播放器"""
        options = project.applied_options
        if options is None or not project.can_preview:
            return
        cached = self._preview_cache.get(id(project))
        fingerprint = self._options_fingerprint(options)
        if cached is not None and cached[0] == fingerprint:
            self.detail.player.set_scaled_store(cached[1], cached[2])
            return
        worker = PreviewWorker(project, copy.deepcopy(options), self)
        worker.built.connect(self._on_preview_built)
        worker.estimated.connect(self._on_estimate_ready)
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(lambda w=worker: self._preview_workers.remove(w) if w in self._preview_workers else None)
        self._preview_workers.append(worker)
        worker.start()
        self.detail.preview_hint.setText("正在產生縮放後預覽…")

    def _on_preview_built(self, project, store, label) -> None:
        options = project.applied_options
        if options is not None:
            self._preview_cache[id(project)] = (self._options_fingerprint(options), store, label)
        if self.project_list.current_project() is project:
            self.detail.player.set_scaled_store(store, label)
            self.detail.preview_hint.setText(
                f"縮放後預覽已就緒（{label}）——按「縮放後 / 原始」鈕即時切換對比。"
            )

    def _on_preview_failed(self, project, message: str) -> None:
        if self.project_list.current_project() is project:
            self.detail.preview_hint.setText(f"縮放後預覽不可用：{message}")

    def _on_estimate_ready(self, project, estimate: dict) -> None:
        """PreviewWorker 完成壓縮估算：記到專案並更新畫面（原始 vs 處理後大小差距）"""
        options = project.applied_options
        # 估算期間設定可能又被改過，過期結果直接丟棄
        if options is None or estimate.get("fingerprint") != self._options_fingerprint(options):
            return
        project.size_estimate = estimate
        if self.project_list.current_project() is project:
            self.detail.apply_estimate(project)

    def _apply_current(self) -> None:
        project = self.project_list.current_project()
        if project is None:
            return
        if not project.can_process:
            QMessageBox.warning(self, APP_NAME, "此專案缺少可用的 atlas 或貼圖，無法處理。")
            return
        self._apply_to(project)
        self._attach_preview(project)
        self._after_apply()

    def _apply_all(self) -> None:
        applied = 0
        for project in self.project_list.projects:
            if project.can_process:
                self._apply_to(project)
                applied += 1
        current = self.project_list.current_project()
        if current is not None and current.applied_options is not None:
            self._attach_preview(current)
        self._after_apply()
        self.status_label.setText(f"已套用到 {applied} 份專案")

    def _apply_to(self, project: SpineProject) -> None:
        options = copy.deepcopy(self.settings_panel.get_options())
        options.source_roots = list(self._source_roots)
        project.applied_options = options
        project.status = STATUS_APPLIED
        project.status_detail = ""
        project.size_estimate = None  # 設定變了，舊估算作廢
        self._preview_cache.pop(id(project), None)
        if self.project_list.current_project() is project:
            self.detail.apply_estimate(project)

    def _unapply_current(self) -> None:
        project = self.project_list.current_project()
        if project is None:
            return
        project.applied_options = None
        project.status = STATUS_IDLE
        project.size_estimate = None
        self.detail.apply_estimate(project)
        self._preview_cache.pop(id(project), None)
        self.detail.player.set_scaled_store(None, "")
        self.detail.preview_hint.setText("")
        self._after_apply()

    def _after_apply(self) -> None:
        self.project_list.refresh_all()
        self._update_footer()
        user_settings.save_options(self.settings_panel.get_options())

    def _update_footer(self) -> None:
        projects = self.project_list.projects
        if not projects:
            self.status_label.setText("尚未載入任何專案")
            self.start_button.setText("開始處理")
            self.start_button.setEnabled(False)
            return
        applied = [p for p in projects if p.applied_options is not None and p.can_process]
        skipped_hint = (
            f"（未套用的 {len(projects) - len(applied)} 份處理時會略過）"
            if applied and len(applied) < len(projects)
            else ""
        )
        self.status_label.setText(
            f"已載入 {len(projects)} 份專案，已套用 {len(applied)} 份{skipped_hint}"
        )
        self.start_button.setText(f"開始處理（{len(applied)}）" if applied else "開始處理")
        self.start_button.setEnabled(bool(applied))

    # ------------------------------------------------------------ 處理

    def _start_processing(self) -> None:
        projects = self.project_list.projects
        applied = [p for p in projects if p.applied_options is not None and p.can_process]
        if not applied:
            return

        for project in applied:
            options = project.applied_options
            assert options is not None
            if options.output_mode == OUTPUT_CUSTOM and not options.output_dir:
                QMessageBox.warning(self, APP_NAME, f"{project.name}：輸出到指定路徑，但尚未選擇資料夾。")
                return
        if any(p.applied_options.output_mode == OUTPUT_INPLACE for p in applied):
            answer = QMessageBox.question(
                self, APP_NAME,
                "部分專案使用「原地覆蓋」，會直接改寫原始檔（會先建立 .bak 備份）。\n確定要繼續嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._progress = ProgressDialog("處理中", self)
        self._progress.cancelled.connect(
            lambda: self._process_worker.cancel() if self._process_worker else None
        )
        self._process_worker = ProcessWorker(list(projects), self)
        self._process_worker.progress.connect(self._progress.update_progress)
        self._process_worker.project_done.connect(self.project_list.refresh_project)
        self._process_worker.finished_process.connect(self._on_process_finished)
        self._process_worker.start()
        self.start_button.setEnabled(False)
        self._progress.exec()

    def _on_process_finished(self, batch: BatchResult, skipped: list) -> None:
        if self._progress is not None:
            self._progress.accept()
            self._progress = None
        self._process_worker = None
        self.project_list.refresh_all()
        self._update_footer()
        ReportDialog(batch, skipped, self).exec()

    # ------------------------------------------------------------ 關閉

    def closeEvent(self, event) -> None:  # noqa: N802
        self.detail.player.stop()
        for worker in (self._scan_worker, self._process_worker, *self._preview_workers):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(3000)
        user_settings.save_options(self.settings_panel.get_options())
        user_settings.save_window_geometry(bytes(self.saveGeometry()))
        super().closeEvent(event)
