"""Spine 專案清單（以 .skel 為單位）"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
)

from models.spine_project import SpineProject
from ui.components.project_filter import FilterCriteria
from ui.styles.theme import DELTA_DOWN_COLOUR, DELTA_UP_COLOUR
from utils.file_utils import format_bytes, format_size_delta

_COLUMNS = ("名稱", "Spine", "頁面尺寸", "區塊", "貼圖", "狀態", "容量變化")
_COL_DELTA = len(_COLUMNS) - 1

# 明確欄寬（名稱欄吃掉剩餘空間）。改用固定值而非 ResizeToContents：
# 七個欄位靠內容自動撐開會超出面板寬度，而且內容變動時欄位會左右跳動。
# 寬度依 13px Segoe UI 下最長內容推算：
#   Spine「3.8.99」/ 頁面尺寸「1204x1053」/ 貼圖「709.4 KB」
#   狀態「已套用 100%」/ 容量變化「1023.9 KB ↓100.0%」
_COL_WIDTHS = {1: 52, 2: 82, 3: 42, 4: 72, 5: 92, _COL_DELTA: 128}


class ProjectList(QTableWidget):
    selection_changed = pyqtSignal(object)  # 目前列的 SpineProject | None
    remove_requested = pyqtSignal()         # 右鍵選單要求移除選取的專案
    rows_rebuilt = pyqtSignal()             # 列已重建（載入、篩選、移除後）

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self._projects: list[SpineProject] = []   # 全部（不受篩選影響）
        self._visible: list[SpineProject] = []    # 實際顯示的列，順序即排序結果
        self._criteria = FilterCriteria()

        self.setHorizontalHeaderLabels(_COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 支援 Ctrl / Shift 多選，套用與移除都能一次處理多份
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in _COL_WIDTHS.items():
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(col, width)

        self.itemSelectionChanged.connect(
            lambda: self.selection_changed.emit(self.current_project())
        )

    # ------------------------------------------------------------ 資料

    @property
    def projects(self) -> list[SpineProject]:
        """全部專案（不受篩選影響）"""
        return self._projects

    def visible_projects(self) -> list[SpineProject]:
        """通過篩選、目前顯示在清單上的專案"""
        return list(self._visible)

    @property
    def criteria(self) -> FilterCriteria:
        return self._criteria

    def set_projects(self, projects: list[SpineProject]) -> None:
        self._projects = projects
        self._rebuild(select_first=True)

    def set_filter(self, criteria: FilterCriteria) -> None:
        """套用新的篩選與排序，並盡量保住原本的選取"""
        self._criteria = criteria
        self._rebuild(select_first=False)

    def _rebuild(self, select_first: bool) -> None:
        """依目前條件重建所有列"""
        keep = [] if select_first else self.selected_projects()
        current = None if select_first else self.current_project()

        self._visible = self._criteria.apply(self._projects)

        # 重建期間擋掉選取變更訊號，否則每寫一列就會觸發一次中間面板重載
        self.blockSignals(True)
        try:
            self.setRowCount(len(self._visible))
            for row, project in enumerate(self._visible):
                self._fill_row(row, project)
            self.clearSelection()
        finally:
            self.blockSignals(False)

        self.rows_rebuilt.emit()

        if not self._visible:
            self.selection_changed.emit(None)
            return

        rows = [i for i, p in enumerate(self._visible) if any(p is k for k in keep)]
        if not rows:
            rows = [0]
        # 先設 current（決定中間面板顯示哪一份），再補上其餘選取
        current_row = next(
            (i for i, p in enumerate(self._visible) if current is not None and p is current),
            rows[0],
        )
        self.selectRow(current_row)
        if len(rows) > 1:
            model = self.selectionModel()
            flags = (
                model.SelectionFlag.Select | model.SelectionFlag.Rows
            )
            for row in rows:
                if row != current_row:
                    model.select(self.model().index(row, 0), flags)

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
        self.setItem(row, _COL_DELTA, self._delta_item(project))

    @staticmethod
    def _delta_item(project: SpineProject) -> QTableWidgetItem:
        """容量變化欄：已套用才有值，估算完成前顯示「估算中…」"""
        estimate = project.size_estimate
        if estimate is None or not estimate.pages:
            item = QTableWidgetItem("估算中…" if project.applied_options is not None else "—")
            item.setForeground(QColor("#94A3B8"))
            return item
        text, increased = format_size_delta(estimate.src_total, estimate.est_total)
        item = QTableWidgetItem(text)
        item.setForeground(QColor(DELTA_UP_COLOUR if increased else DELTA_DOWN_COLOUR))
        item.setToolTip(
            f"原始 {format_bytes(estimate.src_total)} → 處理後 {format_bytes(estimate.est_total)}"
            f"（{len(estimate.pages)} 張貼圖）"
        )
        return item

    def _row_of(self, project: SpineProject) -> int | None:
        """
        專案對應的列號；被篩選掉時回傳 None。

        以身分（不是相等）比對——不同專案的欄位值可能完全相同。
        """
        return next((i for i, p in enumerate(self._visible) if p is project), None)

    def refresh_project(self, project: SpineProject) -> None:
        row = self._row_of(project)
        if row is not None:
            self._fill_row(row, project)

    def remove_projects(self, projects: list[SpineProject]) -> int:
        """
        從清單移除（只移出編輯器，不動本地檔案），回傳實際移除數量。

        移除後整批重建（篩選與排序照舊生效），重建期間會擋掉選取變更訊號，
        免得中間面板反覆重新載入貼圖與播放器。
        """
        targets = {id(p) for p in projects}
        remaining = [p for p in self._projects if id(p) not in targets]
        removed = len(self._projects) - len(remaining)
        if not removed:
            return 0
        self._projects = remaining
        self._rebuild(select_first=False)
        return removed

    def refresh_all(self) -> None:
        """重新填入所有顯示中的列（狀態或估算變動後呼叫）"""
        for row, project in enumerate(self._visible):
            self._fill_row(row, project)

    def current_project(self) -> SpineProject | None:
        row = self.currentRow()
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return None

    def selected_projects(self) -> list[SpineProject]:
        """所有被選取的專案（依清單順序）"""
        rows = sorted({index.row() for index in self.selectedIndexes()})
        return [self._visible[r] for r in rows if 0 <= r < len(self._visible)]

    def clear_projects(self) -> None:
        self.set_projects([])

    # ------------------------------------------------------------ 右鍵選單

    def _show_context_menu(self, pos) -> None:
        row = self.rowAt(pos.y())
        if not 0 <= row < len(self._visible):
            return
        # 右鍵點在選取範圍外時只選它——與檔案總管一致，避免誤刪整批
        if row not in {index.row() for index in self.selectedIndexes()}:
            self.selectRow(row)

        projects = self.selected_projects()
        if not projects:
            return

        menu = QMenu(self)
        open_action = menu.addAction("開啟檔案資料夾")
        count = len(projects)
        remove_action = menu.addAction(f"移除（{count} 份）" if count > 1 else "移除")
        remove_action.setToolTip("只從清單移除，不會刪除本地檔案")

        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is open_action:
            self._open_folder(self._visible[row])
        elif chosen is remove_action:
            self.remove_requested.emit()

    @staticmethod
    def _open_folder(project: SpineProject) -> None:
        folder = project.folder
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
