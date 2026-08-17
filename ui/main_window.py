"""主視窗：左＝skel 專案清單、中＝檔案與播放預覽、右＝縮放設定與套用"""
from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
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
from config.constants import APP_NAME, APP_TITLE, OUTPUT_CUSTOM
from config.version import VERSION
from core.log_writer import log_filename, write_settings_log
from core.pipeline import BatchResult
from core.project_file import (
    FILE_EXTENSION,
    FILE_FILTER,
    describe_load,
    load_project_file,
    save_project_file,
)
from core.sheet_group import build_sheet_groups, groups_for_project
from models.sheet_layout import LayoutStore, layout_key
from models.size_estimate import aggregate_estimates
from models.spine_project import STATUS_APPLIED, STATUS_IDLE, SpineProject
from ui.components.project_detail import ProjectDetail
from ui.components.project_filter import ProjectFilterBar
from ui.components.project_list import ProjectList
from ui.components.settings_panel import SettingsPanel
from ui.dialogs.about_dialog import AboutDialog
from ui.dialogs.progress_dialog import ProgressDialog
from ui.dialogs.report_dialog import ReportDialog
from ui.dialogs.sheet_editor import SheetEditorDialog
from ui.styles.theme import DELTA_DOWN_COLOUR, DELTA_UP_COLOUR, THEMES, build_stylesheet
from ui.workers import EstimateWorker, PreviewWorker, ProcessWorker, ScanWorker
from utils.file_utils import format_bytes


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION} — {APP_TITLE}")
        self.resize(1480, 900)
        self.setAcceptDrops(True)

        self._scan_worker: ScanWorker | None = None
        self._process_worker: ProcessWorker | None = None
        self._preview_workers: list[PreviewWorker] = []
        self._estimate_worker: EstimateWorker | None = None
        self._progress: ProgressDialog | None = None
        self._source_roots: list[Path] = []
        # 縮放後貼圖庫快取：id(project) -> (設定指紋, AtlasTextureStore, label)
        self._preview_cache: dict[int, tuple[tuple, object, str]] = {}
        # 合圖版面：以貼圖路徑為鍵的單一份紀錄。刻意不放進各專案的 options——
        # 一張合圖被多份專案共用時，版面只能有一份，否則兩邊各改一次就壞了
        self._layouts = LayoutStore()

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

        splitter.addWidget(self._build_project_column())

        self.detail = ProjectDetail()
        self.detail.setMinimumWidth(380)
        splitter.addWidget(self.detail)

        splitter.addWidget(self._build_settings_column())

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 0)
        # 左欄要放得下八個欄位（含合圖與容量變化），預設給寬一點免得一開就出現橫向捲軸
        splitter.setSizes([680, 400, 400])
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
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)

        self.remove_button = QPushButton("移除選取")
        self.remove_button.setToolTip("把選取的專案從清單移除（不會刪除本地檔案）")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_button)

        clear_button = QPushButton("清空")
        clear_button.setToolTip("清空整個清單（不會刪除本地檔案）")
        clear_button.clicked.connect(self._clear)
        row.addWidget(clear_button)

        row.addWidget(self._separator())

        self.sheet_button = QPushButton("合圖編輯")
        self.sheet_button.setToolTip(
            "以合圖為單位重新排版：元件各自等比縮放、版面自動縮到最小尺寸\n"
            "共用同一張合圖的所有 atlas 會一起套用同一份版面"
        )
        self.sheet_button.setEnabled(False)
        self.sheet_button.clicked.connect(lambda: self._open_sheet_editor(None))
        row.addWidget(self.sheet_button)

        row.addWidget(self._separator())

        open_button = QPushButton("開啟專案")
        open_button.setToolTip(
            f"載入 *{FILE_EXTENSION} 專案檔——依裡面記錄的絕對路徑重新掃描素材並還原設定"
        )
        open_button.clicked.connect(self._open_project_file)
        row.addWidget(open_button)

        self.save_project_button = QPushButton("儲存專案")
        self.save_project_button.setToolTip(
            "把目前的清單與各專案的設定存成專案檔\n"
            "（只記錄素材的絕對路徑與設定，不含任何圖片）"
        )
        self.save_project_button.setEnabled(False)
        self.save_project_button.clicked.connect(self._save_project_file)
        row.addWidget(self.save_project_button)

        self.export_log_button = QPushButton("匯出紀錄")
        self.export_log_button.setToolTip(
            "把目前已套用的設定寫成一份文字紀錄\n"
            "（每組設定的完整內容、套用到哪些專案、每張貼圖的路徑與預估容量變化）"
        )
        self.export_log_button.setEnabled(False)
        self.export_log_button.clicked.connect(self._export_log)
        row.addWidget(self.export_log_button)

        row.addStretch(1)
        return row

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setProperty("role", "separator")
        line.setFixedWidth(1)
        return line

    def _build_project_column(self) -> QWidget:
        """左欄：篩選列 + 專案清單"""
        column = QWidget()
        column.setMinimumWidth(360)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.filter_bar = ProjectFilterBar()
        self.filter_bar.changed.connect(self._on_filter_changed)
        layout.addWidget(self.filter_bar)

        self.project_list = ProjectList()
        self.project_list.selection_changed.connect(self._on_project_selected)
        self.project_list.remove_requested.connect(self._remove_selected)
        self.project_list.edit_sheet_requested.connect(self._open_sheet_editor)
        self.project_list.rows_rebuilt.connect(self._sync_filter_counts)
        layout.addWidget(self.project_list, 1)
        return column

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
        self.apply_button = QPushButton("套用到此專案 (T)")
        self.apply_button.setProperty("role", "primary")
        self.apply_button.setToolTip("套用目前設定到選取的專案（快捷鍵：T）")
        self.apply_button.clicked.connect(self._apply_current)
        apply_row.addWidget(self.apply_button, 1)

        self.apply_all_button = QPushButton("套用到全部")
        self.apply_all_button.clicked.connect(self._apply_all)
        apply_row.addWidget(self.apply_all_button)

        self.unapply_button = QPushButton("取消套用 (R)")
        self.unapply_button.setToolTip("取消選取專案的套用（快捷鍵：R）")
        self.unapply_button.clicked.connect(self._unapply_current)
        apply_row.addWidget(self.unapply_button)
        layout.addLayout(apply_row)

        hint = QLabel(
            "左側清單可用 Ctrl / Shift 多選批量套用，右鍵可開啟檔案資料夾或移除。\n"
            "套用後即可在中間預覽切換「原始 / 縮放後」播放對比。"
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return column

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.status_label = QLabel("尚未載入任何專案")
        self.status_label.setProperty("role", "hint")
        row.addWidget(self.status_label)

        self.total_label = QLabel("")
        self.total_label.setProperty("role", "stat")
        row.addSpacing(12)
        row.addWidget(self.total_label)
        row.addStretch(1)

        self.start_button = QPushButton("開始處理")
        self.start_button.setProperty("role", "primary")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start_processing)
        row.addWidget(self.start_button)
        return row

    def _apply_theme(self, key: str) -> None:
        self.setStyleSheet(build_stylesheet(THEMES.get(key, THEMES["dark"])))
        # 清單的共用貼圖分組色是用 QColor 直接上的，不吃 stylesheet，要另外通知
        self.project_list.set_theme(key)
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
        # 新加入的專案可能用到已經有版面的合圖，標示要跟著更新
        self._sync_layout_marks()
        self._update_footer()
        if not merged:
            QMessageBox.information(self, APP_NAME, "找不到任何 .skel 或 .atlas 檔案。")

    def _clear(self) -> None:
        if self._estimate_worker is not None and self._estimate_worker.isRunning():
            self._estimate_worker.cancel()
        self.project_list.clear_projects()
        self._source_roots.clear()
        self._preview_cache.clear()
        self._layouts.clear()
        self._sync_layout_marks()
        self.detail.show_project(None)
        self._update_footer()

    # ------------------------------------------------------------ 專案檔與紀錄

    def _default_save_dir(self) -> Path:
        """預設存檔位置：使用者最後操作的資料夾"""
        last = user_settings.load_last_folder()
        if last and Path(last).is_dir():
            return Path(last)
        return Path.home()

    def _save_project_file(self) -> None:
        projects = self.project_list.projects
        if not projects:
            return
        suggested = self._default_save_dir() / f"專案清單{FILE_EXTENSION}"
        chosen, _ = QFileDialog.getSaveFileName(
            self, "儲存專案檔", str(suggested), FILE_FILTER
        )
        if not chosen:
            return
        try:
            path = save_project_file(
                projects, Path(chosen), self._source_roots, self._layouts
            )
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"專案檔儲存失敗：{exc}")
            return
        applied = sum(1 for p in projects if p.applied_options is not None)
        sheets = f"、{len(self._layouts)} 張合圖版面" if len(self._layouts) else ""
        self.status_label.setText(
            f"已儲存專案檔（{len(projects)} 份專案、{applied} 份含設定{sheets}）：{path.name}"
        )

    def _open_project_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "開啟專案檔", str(self._default_save_dir()), FILE_FILTER
        )
        if not chosen:
            return
        try:
            result = load_project_file(Path(chosen))
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return

        if not result.projects:
            QMessageBox.warning(
                self, APP_NAME,
                "專案檔裡記錄的素材都找不到了。\n\n"
                + "\n".join(result.missing[:8])
                + ("\n…" if len(result.missing) > 8 else ""),
            )
            return

        # 載入專案檔＝換一份工作清單，先收乾淨再換
        if self._estimate_worker is not None and self._estimate_worker.isRunning():
            self._estimate_worker.cancel()
        self._preview_cache.clear()
        self._source_roots = list(result.source_roots)
        self._layouts = result.layouts
        self.project_list.set_projects(result.projects)
        self._sync_layout_marks()
        self._update_footer()
        self._start_estimates()
        self.status_label.setText(describe_load(result))

        notes = []
        stale = self._stale_layout_notes(result.projects)
        if stale:
            notes.append(stale)
        if result.missing:
            notes.append(
                f"有 {len(result.missing)} 個檔案已不在原位置：\n"
                + "\n".join(result.missing[:6])
                + ("\n…" if len(result.missing) > 6 else "")
            )
        if result.unmatched:
            notes.append(
                f"有 {len(result.unmatched)} 份專案的素材配對變了，設定沒還原：\n"
                + "、".join(result.unmatched[:6])
            )
        if notes:
            QMessageBox.information(self, APP_NAME, "\n\n".join(notes))

    def _stale_layout_notes(self, projects: list[SpineProject]) -> str:
        """
        載入的合圖版面與現在的素材對不上時的說明。

        素材被重新匯出（區塊位置變了）時版面就過期了，直接沿用會讓 atlas
        與貼圖對不上。這裡只回報，並把過期的版面移掉，讓它回到全域比例。
        """
        groups = {g.key: g for g in build_sheet_groups(projects)}
        stale: list[str] = []
        for layout in self._layouts.layouts():
            group = groups.get(layout.key)
            if group is None:
                continue  # 這張貼圖不在清單裡，留著也不會被用到
            notes = group.sync_layout(layout)
            if notes:
                stale.append(f"{group.name}：{'、'.join(notes)}")
        if stale:
            self._sync_layout_marks()
            return (
                f"有 {len(stale)} 張合圖的版面已依目前素材重新對齊，請進「合圖編輯」確認：\n"
                + "\n".join(stale[:6])
                + ("\n…" if len(stale) > 6 else "")
            )
        return ""

    def _export_log(self) -> None:
        projects = self.project_list.projects
        applied = [p for p in projects if p.applied_options is not None]
        if not applied:
            QMessageBox.information(
                self, APP_NAME, "還沒有任何專案套用設定，沒有內容可以紀錄。"
            )
            return
        suggested = self._default_save_dir() / log_filename()
        chosen, _ = QFileDialog.getSaveFileName(
            self, "匯出設定紀錄", str(suggested), "文字檔 (*.txt);;所有檔案 (*.*)"
        )
        if not chosen:
            return
        try:
            path = write_settings_log(
                applied, Path(chosen), total_count=len(projects), layouts=self._layouts
            )
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"紀錄匯出失敗：{exc}")
            return
        sheets = f"、{len(self._layouts)} 張合圖版面" if len(self._layouts) else ""
        self.status_label.setText(
            f"已匯出設定紀錄（{len(applied)} 份專案{sheets}）：{path.name}"
        )

    # ------------------------------------------------------------ 選擇與套用

    def _options_fingerprint(self, options, project: SpineProject | None = None) -> tuple:
        """
        預覽／估算快取的鍵。

        除了設定本身，還要帶上這份專案用到的合圖版面指紋——版面改了但設定
        沒動時，快取一樣得作廢，不然畫面與數字都會停在舊版面上。
        """
        base = options.render_fingerprint()
        if project is None:
            return base
        return base + self._layouts.fingerprint_for(project.page_paths)

    def _on_project_selected(self, project: SpineProject | None) -> None:
        self.detail.show_project(project)
        self._update_selection_labels()
        if project is None:
            return
        if project.applied_options is not None:
            self.settings_panel.set_options(project.applied_options)
            self._attach_preview(project)

    def _on_filter_changed(self) -> None:
        """篩選條件改動：重建清單並同步計數與按鈕文字"""
        self.project_list.set_filter(self.filter_bar.criteria())
        self._sync_filter_counts()
        self._update_selection_labels()

    def _sync_filter_counts(self) -> None:
        self.filter_bar.set_counts(
            len(self.project_list.visible_projects()), len(self.project_list.projects)
        )

    def _update_selection_labels(self) -> None:
        """多選時把數量標在按鈕上，避免誤以為只會動到目前這一份"""
        count = len(self.project_list.selected_projects())
        self.remove_button.setEnabled(count > 0)
        if count > 1:
            self.apply_button.setText(f"套用到選取的 {count} 份 (T)")
            self.remove_button.setText(f"移除選取（{count}）")
        else:
            self.apply_button.setText("套用到此專案 (T)")
            self.remove_button.setText("移除選取")

        # 有篩選時「套用到全部」只會動到顯示中的專案，按鈕上要講清楚
        visible = len(self.project_list.visible_projects())
        if self.project_list.criteria.is_active:
            self.apply_all_button.setText(f"套用到篩選結果（{visible}）")
            self.apply_all_button.setToolTip("只套用到目前顯示的專案，被篩選掉的不受影響")
        else:
            self.apply_all_button.setText("套用到全部")
            self.apply_all_button.setToolTip("")

    def _attach_preview(self, project: SpineProject) -> None:
        """把快取的（或新建的）縮放後貼圖庫掛到播放器"""
        options = project.applied_options
        if options is None or not project.can_preview:
            return
        cached = self._preview_cache.get(id(project))
        fingerprint = self._options_fingerprint(options, project)
        if cached is not None and cached[0] == fingerprint:
            self.detail.player.set_scaled_store(cached[1], cached[2])
            return
        worker = PreviewWorker(
            project, copy.deepcopy(options), self._layouts, fingerprint, self
        )
        # 以 worker 當時的設定當快取鍵——產生期間設定又被改過的話，
        # 這份結果就是過期的，不能用新指紋存進快取
        worker.built.connect(
            lambda p, store, label, f=fingerprint: self._on_preview_built(p, store, label, f)
        )
        worker.estimated.connect(self._on_estimate_ready)
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(lambda w=worker: self._preview_workers.remove(w) if w in self._preview_workers else None)
        self._preview_workers.append(worker)
        worker.start()
        self.detail.preview_hint.setText("正在產生縮放後預覽…")

    def _on_preview_built(self, project, store, label, fingerprint: tuple) -> None:
        options = project.applied_options
        if options is None or fingerprint != self._options_fingerprint(options, project):
            return
        self._preview_cache[id(project)] = (fingerprint, store, label)
        if self.project_list.current_project() is project:
            self.detail.player.set_scaled_store(store, label)
            self.detail.preview_hint.setText(
                f"縮放後預覽已就緒（{label}）——按「縮放後 / 原始」鈕即時切換對比。"
            )

    def _on_preview_failed(self, project, message: str) -> None:
        if self.project_list.current_project() is project:
            self.detail.preview_hint.setText(f"縮放後預覽不可用：{message}")

    def _on_estimate_ready(self, project, estimate) -> None:
        """估算完成：記到專案，更新清單的容量變化欄與詳細面板"""
        options = project.applied_options
        # 估算期間設定或合圖版面可能又被改過，過期結果直接丟棄
        if options is None or estimate.fingerprint != self._options_fingerprint(options, project):
            return
        project.size_estimate = estimate
        self.project_list.refresh_project(project)
        if self.project_list.current_project() is project:
            self.detail.apply_estimate(project)
        self._update_footer()

    def _start_estimates(self) -> None:
        """為所有已套用但還沒有（或已過期）估算的專案排背景估算"""
        jobs = []
        for project in self.project_list.projects:
            options = project.applied_options
            if options is None or not project.can_process:
                continue
            fingerprint = self._options_fingerprint(options, project)
            estimate = project.size_estimate
            if estimate is not None and estimate.fingerprint == fingerprint:
                continue  # 這份的估算還是新的
            # 已經有 PreviewWorker 在算這份（它會順便回報估算），不重複跑
            if any(w.project is project for w in self._preview_workers):
                continue
            jobs.append((project, copy.deepcopy(options), fingerprint))
        if not jobs:
            return
        if self._estimate_worker is not None and self._estimate_worker.isRunning():
            self._estimate_worker.cancel()
            self._estimate_worker.wait(2000)
        worker = EstimateWorker(jobs, self._layouts, self)
        worker.estimated.connect(self._on_estimate_ready)
        self._estimate_worker = worker
        worker.start()

    def _apply_current(self) -> None:
        """套用到目前選取的專案（可多選）"""
        selected = self.project_list.selected_projects()
        if not selected:
            return
        applicable = [p for p in selected if p.can_process]
        if not applicable:
            QMessageBox.warning(
                self, APP_NAME,
                "選取的專案缺少可用的 atlas 或貼圖，無法處理。" if len(selected) == 1
                else f"選取的 {len(selected)} 份專案都缺少可用的 atlas 或貼圖，無法處理。",
            )
            return

        for project in applicable:
            self._apply_to(project)

        current = self.project_list.current_project()
        if current is not None and current.applied_options is not None:
            self._attach_preview(current)
        self._after_apply()

        skipped = len(selected) - len(applicable)
        if len(applicable) > 1 or skipped:
            note = f"（略過無法處理的 {skipped} 份）" if skipped else ""
            self.status_label.setText(f"已套用到選取的 {len(applicable)} 份專案{note}")

    def _apply_all(self) -> None:
        """套用到清單上所有顯示中的專案（有篩選時就是篩選結果）"""
        targets = self.project_list.visible_projects()
        applied = 0
        for project in targets:
            if project.can_process:
                self._apply_to(project)
                applied += 1
        current = self.project_list.current_project()
        if current is not None and current.applied_options is not None:
            self._attach_preview(current)
        self._after_apply()
        hidden = len(self.project_list.projects) - len(targets)
        note = f"（篩選外的 {hidden} 份未受影響）" if hidden else ""
        self.status_label.setText(f"已套用到 {applied} 份專案{note}")

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

    # ------------------------------------------------------------ 合圖編輯

    def _open_sheet_editor(self, project: SpineProject | None) -> None:
        """
        開啟合圖群組編輯器。

        群組是「一張貼圖 + 所有引用它的 atlas」，所以套用後共用同一張合圖的
        每一份 atlas 都會拿到同一份版面——這是這個功能存在的理由。
        """
        projects = self.project_list.projects
        if not projects:
            return
        groups = build_sheet_groups(projects)
        if not groups:
            QMessageBox.information(self, APP_NAME, "清單上的專案都沒有可用的貼圖。")
            return

        initial = None
        target = project or self.project_list.current_project()
        if target is not None:
            mine = groups_for_project(groups, target)
            if mine:
                initial = mine[0].key

        options = self.settings_panel.get_options()
        dialog = SheetEditorDialog(
            groups=groups,
            layouts=self._layouts,
            default_scale=options.scale,
            initial_key=initial,
            parent=self,
        )
        if dialog.exec() != SheetEditorDialog.DialogCode.Accepted:
            return

        layouts, removed = dialog.result_layouts()
        if not layouts and not removed:
            return
        # 先移除再寫入：同一張合圖若先被移除、後來又調整過，要以調整後的版面為準
        for key in removed:
            for group in groups:
                if group.key == key:
                    self._layouts.remove(group.page_path)
        for layout in layouts:
            self._layouts.put(layout)

        newly_applied = self._invalidate_sheet_users(
            groups, {item.key for item in layouts} | removed
        )
        parts = [f"已更新 {len(layouts)} 張合圖的版面"] if layouts else []
        if removed:
            parts.append(f"移除 {len(removed)} 張的自訂版面")
        if newly_applied:
            parts.append(f"順便套用設定到相關的 {newly_applied} 份專案")
        self.status_label.setText("、".join(parts))

    def _invalidate_sheet_users(self, groups, keys: set[str]) -> int:
        """
        版面變了：所有用到這些合圖的專案都要重算預覽與估算。

        一張合圖被三份專案共用時，三份的預估容量與播放預覽全部得跟著更新，
        不能只更新目前選到的那一份。

        還沒套用設定的專案會**順便套用目前右側的設定**：套用版面本身就是
        「我要改這張圖的輸出」的意思，若不一起套用，清單的容量變化與播放
        預覽都不會有任何變化（要處理時也會被略過），看起來就像沒生效。
        編輯器的起始比例本來就是取自右側設定，兩者是一致的。
        """
        affected: list[SpineProject] = []
        for group in groups:
            if group.key not in keys:
                continue
            for project in group.projects:
                if not any(p is project for p in affected):
                    affected.append(project)

        newly_applied = 0
        for project in affected:
            if project.applied_options is None and project.can_process:
                self._apply_to(project)
                newly_applied += 1
            else:
                project.size_estimate = None
                self._preview_cache.pop(id(project), None)

        # 這裡面會順便重畫檔案面板的「✎自訂版面」標記
        self._sync_layout_marks()
        self.project_list.refresh_all()
        current = self.project_list.current_project()
        if current is not None:
            self.detail.apply_estimate(current)
            if any(p is current for p in affected) and current.applied_options is not None:
                self._attach_preview(current)
        self._start_estimates()
        self._update_footer()
        if newly_applied:
            user_settings.save_options(self.settings_panel.get_options())
        return newly_applied

    def _sync_layout_marks(self) -> None:
        """把「哪些貼圖有自訂版面」同步給各專案、清單與檔案面板"""
        keys = {layout.key for layout in self._layouts}
        for project in self.project_list.projects:
            project.custom_sheets = sum(
                1 for path in project.page_paths if layout_key(path) in keys
            )
        # 清單要連版面的輸出尺寸一起帶：套用版面後「頁面尺寸」欄
        # 顯示的就是實際會輸出的大小，不是原檔宣告的尺寸
        self.project_list.set_custom_layouts(
            {layout.key: layout.canvas for layout in self._layouts}
        )
        self.detail.set_custom_layouts(keys)

    def _remove_selected(self) -> None:
        """把選取的專案移出清單——只是不再編輯它們，本地檔案完全不動"""
        projects = self.project_list.selected_projects()
        if not projects:
            return
        for project in projects:
            self._preview_cache.pop(id(project), None)
        if self.project_list.remove_projects(projects):
            self._update_footer()

    def _unapply_current(self) -> None:
        """取消目前選取專案的套用（可多選）"""
        projects = self.project_list.selected_projects()
        if not projects:
            return
        for project in projects:
            project.applied_options = None
            project.status = STATUS_IDLE
            project.size_estimate = None
            self._preview_cache.pop(id(project), None)
        current = self.project_list.current_project()
        if current is not None:
            self.detail.apply_estimate(current)
        self.detail.player.set_scaled_store(None, "")
        self.detail.preview_hint.setText("")
        self._after_apply()
        if len(projects) > 1:
            self.status_label.setText(f"已取消 {len(projects)} 份專案的套用")

    def _after_apply(self) -> None:
        self.project_list.refresh_all()
        self._update_footer()
        self._start_estimates()
        user_settings.save_options(self.settings_panel.get_options())

    def _update_footer(self) -> None:
        projects = self.project_list.projects
        if not projects:
            self.status_label.setText("尚未載入任何專案")
            self.total_label.setText("")
            self.start_button.setText("開始處理")
            self.start_button.setEnabled(False)
            self.save_project_button.setEnabled(False)
            self.sheet_button.setEnabled(False)
            self.export_log_button.setEnabled(False)
            self._sync_filter_counts()
            return
        applied = [p for p in projects if p.applied_options is not None and p.can_process]
        self._sync_filter_counts()
        self.save_project_button.setEnabled(True)
        self.sheet_button.setEnabled(True)
        self.export_log_button.setEnabled(
            any(p.applied_options is not None for p in projects)
        )
        skipped_hint = (
            f"（未套用的 {len(projects) - len(applied)} 份處理時會略過）"
            if applied and len(applied) < len(projects)
            else ""
        )
        sheet_hint = (
            f"，{len(self._layouts)} 張合圖使用自訂版面" if len(self._layouts) else ""
        )
        self.status_label.setText(
            f"已載入 {len(projects)} 份專案，已套用 {len(applied)} 份{skipped_hint}{sheet_hint}"
        )
        self._update_total_label(applied)
        self.start_button.setText(f"開始處理（{len(applied)}）" if applied else "開始處理")
        self.start_button.setEnabled(bool(applied))

    def _update_total_label(self, applied: list[SpineProject]) -> None:
        """已套用專案的容量總計；估算還沒跑完就先標示進度"""
        estimated = [p for p in applied if p.size_estimate is not None]
        if not estimated:
            self.total_label.setText("估算容量中…" if applied else "")
            self.total_label.setStyleSheet("")
            return
        # 跨專案共用的貼圖只算一次，否則會高估節省量
        src_total, est_total = aggregate_estimates(p.size_estimate for p in estimated)
        delta = est_total - src_total
        pending = len(applied) - len(estimated)
        suffix = f"，另 {pending} 份估算中…" if pending else ""
        if delta <= 0:
            self.total_label.setText(f"預估節省 {format_bytes(-delta)}{suffix}")
            self.total_label.setStyleSheet(f"color: {DELTA_DOWN_COLOUR};")
        else:
            self.total_label.setText(f"預估增加 {format_bytes(delta)}{suffix}")
            self.total_label.setStyleSheet(f"color: {DELTA_UP_COLOUR};")

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

        self._progress = ProgressDialog("處理中", self)
        self._progress.cancelled.connect(
            lambda: self._process_worker.cancel() if self._process_worker else None
        )
        self._process_worker = ProcessWorker(list(projects), self._layouts, self)
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

    # ------------------------------------------------------------ 快捷鍵

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """
        T = 套用到此專案、R = 取消套用。

        刻意用 keyPressEvent 而不是 QShortcut：按鍵會先給焦點所在的控制項，
        所以在檔名後綴等輸入框裡打 t / r 仍然是打字，不會誤觸按鈕。
        """
        if event.modifiers() == Qt.KeyboardModifier.NoModifier:
            button = {
                Qt.Key.Key_T: self.apply_button,
                Qt.Key.Key_R: self.unapply_button,
            }.get(event.key())
            if button is not None and button.isEnabled():
                button.animateClick()
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 關閉

    def closeEvent(self, event) -> None:  # noqa: N802
        self.detail.player.stop()
        for worker in (self._scan_worker, self._process_worker,
                       self._estimate_worker, *self._preview_workers):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(3000)
        user_settings.save_options(self.settings_panel.get_options())
        user_settings.save_window_geometry(bytes(self.saveGeometry()))
        super().closeEvent(event)
