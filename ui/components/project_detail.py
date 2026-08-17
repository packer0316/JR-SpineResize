"""
選定專案的詳細面板：檔案清單（含處理後大小預估）+ Spine 播放預覽

兩塊預設上下疊；主視窗會呼叫 :meth:`ProjectDetail.detach_files_panel`
把檔案清單搬到左欄下方，中欄就整個留給預覽。
"""
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

from models.sheet_layout import layout_key
from models.spine_project import SpineProject
from ui.components.spine_player import SpinePlayer
from ui.styles.theme import DELTA_DOWN_COLOUR, DELTA_UP_COLOUR
from utils.file_utils import format_bytes, format_size_delta

_FILE_COLUMNS = ("類型", "檔案", "資訊", "處理後")


def _delta_colour(increased: bool) -> str:
    return DELTA_UP_COLOUR if increased else DELTA_DOWN_COLOUR


class ProjectDetail(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: SpineProject | None = None
        # 有自訂合圖版面的貼圖（由主視窗同步進來）
        self._custom_layouts: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.files_group = QGroupBox("檔案")
        files_layout = QVBoxLayout(self.files_group)
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
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.files_table.setMaximumHeight(150)
        self.files_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        files_layout.addWidget(self.files_table)

        # 套用後的大小預估總結（原始 → 處理後 ± %）
        self.size_summary = QLabel("")
        self.size_summary.setProperty("role", "stat")
        self.size_summary.setWordWrap(True)
        files_layout.addWidget(self.size_summary)
        layout.addWidget(self.files_group)

        preview_group = QGroupBox("Spine 預覽")
        preview_layout = QVBoxLayout(preview_group)
        self.player = SpinePlayer()
        preview_layout.addWidget(self.player)
        self.preview_hint = QLabel("")
        self.preview_hint.setProperty("role", "hint")
        self.preview_hint.setWordWrap(True)
        preview_layout.addWidget(self.preview_hint)
        layout.addWidget(preview_group, 1)

    def detach_files_panel(self) -> QGroupBox:
        """
        把「檔案」面板從自己的排版取出來交給呼叫端自己擺。

        呼叫端一定要接著 ``addWidget`` 收下它（addWidget 會自動接管親子關係），
        否則它會變成沒被排版的孤兒 widget，疊在預覽上面。剩下的預覽區會吃掉
        整個高度，而檔案清單的資料流（``show_project`` / ``apply_estimate``）
        完全不受影響——那些都只碰 ``files_table``，跟它擺在哪一欄無關。
        """
        self.layout().removeWidget(self.files_group)
        return self.files_group

    # ------------------------------------------------------------ 載入

    def set_custom_layouts(self, keys: set[str]) -> None:
        """更新「哪些貼圖有自訂合圖版面」（檔案清單會標示出來）"""
        if keys == self._custom_layouts:
            return
        self._custom_layouts = set(keys)
        self._fill_files(self._project)
        self.apply_estimate(self._project)

    def show_project(self, project: SpineProject | None) -> None:
        self._project = project
        self._fill_files(project)
        self.apply_estimate(project)
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

    def apply_estimate(self, project: SpineProject | None) -> None:
        """把估算的處理後大小填進檔案表與總結列"""
        if project is not self._project:
            return
        estimate = project.size_estimate if project is not None else None
        if estimate is None or not estimate.pages:
            self.size_summary.setText("")
            self._clear_estimate_column()
            return

        table = self.files_table
        for row in range(table.rowCount()):
            name_item = table.item(row, 1)
            if name_item is None:
                continue
            page = estimate.page(name_item.text())
            if page is None:
                continue
            text, increased = format_size_delta(page.src_bytes, page.est_bytes)
            dst_w, dst_h = page.dst_size
            item = QTableWidgetItem(f"{dst_w}x{dst_h}  {text}")
            item.setForeground(QColor(_delta_colour(increased)))
            table.setItem(row, 3, item)

        text, increased = format_size_delta(estimate.src_total, estimate.est_total)
        self.size_summary.setText(
            f"處理後預估：{format_bytes(estimate.src_total)} → {text}"
        )
        self.size_summary.setStyleSheet(f"color: {_delta_colour(increased)};")

    def _clear_estimate_column(self) -> None:
        for row in range(self.files_table.rowCount()):
            self.files_table.setItem(row, 3, QTableWidgetItem(""))

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
            table.setItem(row, 3, QTableWidgetItem(""))

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
                custom = layout_key(path) in self._custom_layouts
                # 有自訂版面時要講清楚：這張圖不吃全域比例，改設定也不會變
                add_row(
                    "貼圖" if not custom else "合圖",
                    path.name,
                    f"{info}  ✎自訂版面" if custom else info,
                )
