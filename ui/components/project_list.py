"""Spine 專案清單（以 .skel 為單位）"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from models.spine_project import SpineProject
from utils.file_utils import format_bytes

_COLUMNS = ("名稱", "Spine", "頁面尺寸", "區塊", "貼圖", "狀態")


class ProjectList(QTableWidget):
    selection_changed = pyqtSignal(object)  # SpineProject | None

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self._projects: list[SpineProject] = []

        self.setHorizontalHeaderLabels(_COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(_COLUMNS) - 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)

        self.itemSelectionChanged.connect(
            lambda: self.selection_changed.emit(self.current_project())
        )

    # ------------------------------------------------------------ 資料

    @property
    def projects(self) -> list[SpineProject]:
        return self._projects

    def set_projects(self, projects: list[SpineProject]) -> None:
        self._projects = projects
        self.setRowCount(len(projects))
        for row, project in enumerate(projects):
            self._fill_row(row, project)
        if projects:
            self.selectRow(0)
        else:
            self.selection_changed.emit(None)

    def _fill_row(self, row: int, project: SpineProject) -> None:
        name_item = QTableWidgetItem(project.name)
        tooltip = str(project.skeleton_path or project.folder)
        name_item.setToolTip(tooltip)
        self.setItem(row, 0, name_item)
        self.setItem(row, 1, QTableWidgetItem(project.spine_version or "—"))
        self.setItem(row, 2, QTableWidgetItem(project.page_size_text() or "—"))
        self.setItem(row, 3, QTableWidgetItem(str(project.region_count) if project.region_count else "—"))
        size = project.source_bytes
        self.setItem(row, 4, QTableWidgetItem(format_bytes(size) if size else "—"))
        status = QTableWidgetItem(project.status_text)
        status.setForeground(QColor(project.status_colour))
        if project.warnings:
            status.setToolTip("\n".join(project.warnings))
        self.setItem(row, 5, status)

    def refresh_project(self, project: SpineProject) -> None:
        try:
            row = self._projects.index(project)
        except ValueError:
            return
        self._fill_row(row, project)

    def refresh_all(self) -> None:
        for row, project in enumerate(self._projects):
            self._fill_row(row, project)

    def current_project(self) -> SpineProject | None:
        row = self.currentRow()
        if 0 <= row < len(self._projects):
            return self._projects[row]
        return None

    def clear_projects(self) -> None:
        self.set_projects([])
