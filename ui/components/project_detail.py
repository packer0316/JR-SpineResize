"""選定專案的詳細面板：檔案清單 + Spine 播放預覽"""
from __future__ import annotations

from PIL import Image
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.spine_project import SpineProject
from ui.components.spine_player import SpinePlayer
from utils.file_utils import format_bytes

_FILE_COLUMNS = ("類型", "檔案", "資訊")


class ProjectDetail(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: SpineProject | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        files_group = QGroupBox("檔案")
        files_layout = QVBoxLayout(files_group)
        self.files_table = QTableWidget(0, len(_FILE_COLUMNS))
        self.files_table.setHorizontalHeaderLabels(_FILE_COLUMNS)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.files_table.setShowGrid(False)
        self.files_table.setFocusPolicy(self.files_table.focusPolicy().NoFocus)
        header = self.files_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.files_table.setMaximumHeight(150)
        self.files_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        files_layout.addWidget(self.files_table)
        layout.addWidget(files_group)

        preview_group = QGroupBox("Spine 預覽")
        preview_layout = QVBoxLayout(preview_group)
        self.player = SpinePlayer()
        preview_layout.addWidget(self.player)
        self.preview_hint = QLabel("")
        self.preview_hint.setProperty("role", "hint")
        self.preview_hint.setWordWrap(True)
        preview_layout.addWidget(self.preview_hint)
        layout.addWidget(preview_group, 1)

    # ------------------------------------------------------------ 載入

    def show_project(self, project: SpineProject | None) -> None:
        self._project = project
        self._fill_files(project)
        self.preview_hint.setText("")

        if project is None:
            self.player.clear()
            return
        if not project.can_preview:
            reason = "此專案無法預覽播放"
            if project.skeleton_path is None:
                reason = "沒有骨架檔，無法預覽（仍可縮放處理）"
            elif not project.spine_version.startswith("3.8"):
                reason = f"預覽僅支援 Spine 3.8 binary（此為 {project.spine_version or '未知'}），仍可縮放處理"
            elif not project.can_process:
                reason = "atlas 或貼圖缺失，無法預覽"
            self.player.clear(reason)
            return

        asset = project.primary_atlas
        assert asset is not None and asset.atlas is not None
        pages: dict[str, Image.Image] = {}
        for page_name, path in asset.pages.items():
            if path is not None and path.exists():
                try:
                    with Image.open(path) as img:
                        pages[page_name] = img.convert("RGBA")
                except OSError:
                    pass
        assert project.skeleton_path is not None
        if self.player.load(project.skeleton_path, asset.atlas, pages):
            skeleton = self.player.viewport.skeleton
            if skeleton is not None and skeleton.notes:
                self.preview_hint.setText("；".join(skeleton.notes))

    def _fill_files(self, project: SpineProject | None) -> None:
        table = self.files_table
        table.setRowCount(0)
        if project is None:
            return

        def add_row(kind: str, name: str, info: str, colour: str | None = None) -> None:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(kind))
            item = QTableWidgetItem(name)
            table.setItem(row, 1, item)
            info_item = QTableWidgetItem(info)
            if colour:
                info_item.setForeground(QColor(colour))
            table.setItem(row, 2, info_item)

        if project.skeleton_path is not None:
            info = project.spine_version or "無法解析"
            add_row(".skel", project.skeleton_path.name,
                    f"Spine {info}" if project.spine_version else info)
        for asset in project.atlases:
            add_row(".atlas", asset.atlas_path.name,
                    f"{asset.region_count} 區塊" if asset.atlas else asset.load_error[:40],
                    None if asset.atlas else "#dc2626")
            for page_name, path in asset.pages.items():
                if path is None:
                    add_row("貼圖", page_name, "找不到檔案", "#dc2626")
                    continue
                try:
                    with Image.open(path) as img:
                        mode = "8-bit 調色盤" if img.mode in ("P", "PA") else img.mode
                        info = f"{img.size[0]}x{img.size[1]}  {format_bytes(path.stat().st_size)}  {mode}"
                except OSError:
                    info = "無法讀取"
                add_row("貼圖", path.name, info)
