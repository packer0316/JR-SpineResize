"""
合圖群組編輯器

以「一張合圖」為單位編輯：左邊是所有合圖（標出被幾份 atlas / 專案共用），
中間是可拖曳的版面，右邊是比例與排版設定。

為什麼要以合圖為單位：一張合圖常被多份 atlas 共用（實測素材裡有三個 .skel
指向同一張 png）。版面存的是「這張貼圖」的排版結果，套用後共用它的每一份
atlas 都會拿到同一組座標——這正是「避免合圖給多個 skel 用導致輸出壞掉」
的作法。所以這裡不提供「只改其中一份 atlas」的選項，那必然是壞的。

元件只能等比縮放：Spine 算頂點用的是同一個區塊自己的 size/orig 與
offset/orig 比值，x 與 y 同比例縮才守得住；拉成長方形一定破圖。
"""
from __future__ import annotations

import copy

from PIL import Image
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.constants import PAGE_ALIGN_4, PAGE_ALIGN_NONE, PAGE_ALIGN_POT
from core.sheet_group import DEFAULT_PADDING, SheetGroup, repack
from models.sheet_layout import MAX_REGION_SCALE, MIN_REGION_SCALE, LayoutStore, Placement, SheetLayout
from ui.components.sheet_canvas import SheetCanvas
from ui.styles.theme import DELTA_DOWN_COLOUR, DELTA_UP_COLOUR
from utils.file_utils import format_bytes

_SHEET_COLUMNS = ("合圖", "尺寸", "元件", "共用", "版面")

_ALIGN_OPTIONS = [
    ("最小尺寸", PAGE_ALIGN_NONE),
    ("4 的倍數", PAGE_ALIGN_4),
    ("2 的次方", PAGE_ALIGN_POT),
]


class SheetEditorDialog(QDialog):
    """合圖群組編輯器（改動只在按下「套用版面」後才生效）"""

    def __init__(
        self,
        groups: list[SheetGroup],
        layouts: LayoutStore,
        default_scale: float = 1.0,
        initial_key: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("合圖群組編輯器")
        self.resize(1280, 820)
        self.setSizeGripEnabled(True)

        self._groups = [g for g in groups if g.can_edit]
        self._blocked = [g for g in groups if not g.can_edit]
        self._default_scale = default_scale
        # 工作副本：取消時原本的版面完全不動
        self._working: dict[str, SheetLayout] = {}
        self._sources: dict[str, Image.Image | None] = {}
        self._touched: set[str] = set()
        # 開啟這個對話框時「已經套用過」的合圖。點開來看會建一份預覽版面放進
        # _working，但那還不算自訂——只有按過「套用版面」的才算，
        # 所以狀態欄要看這個集合，不能看 _working 有沒有東西。
        self._committed: set[str] = {
            group.key for group in self._groups if layouts.has(group.page_path)
        }
        self._current: SheetGroup | None = None
        self._syncing = False

        for group in self._groups:
            existing = layouts.get(group.page_path)
            if existing is not None:
                clone = copy.deepcopy(existing)
                notes = group.sync_layout(clone)
                self._working[group.key] = clone
                if notes:
                    # 素材變了才需要重新對齊，這種情況算「已改動」，
                    # 否則使用者按下套用時會以為沒事發生
                    self._touched.add(group.key)

        self._build_ui()
        self._fill_sheet_table()

        start = 0
        if initial_key:
            start = next(
                (i for i, g in enumerate(self._groups) if g.key == initial_key), 0
            )
        if self._groups:
            self.sheet_table.selectRow(start)
        else:
            self._show_group(None)

    # ------------------------------------------------------------ 介面

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        header = QLabel(
            "以合圖為單位重新排版：元件可拖曳角落等比縮放，版面會自動縮到最小尺寸，"
            "atlas 座標同步更新。<br>"
            "<b>共用同一張合圖的所有 atlas 會一起套用同一份版面</b>——"
            "這樣才不會有一份改了、另一份沒改而輸出壞掉。"
            "清單可 Ctrl／Shift／Ctrl+A 多選，一次重排或還原多張。"
        )
        header.setWordWrap(True)
        header.setProperty("role", "hint")
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_sheet_list())
        splitter.addWidget(self._build_canvas_column())
        splitter.addWidget(self._build_controls())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 6)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([300, 640, 330])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton(
            "套用版面", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.apply_button.setProperty("role", "primary")
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        footer = QHBoxLayout()
        self.footer_label = QLabel("")
        self.footer_label.setProperty("role", "hint")
        self.footer_label.setWordWrap(True)
        footer.addWidget(self.footer_label, 1)
        footer.addWidget(buttons)
        layout.addLayout(footer)

    def _build_sheet_list(self) -> QWidget:
        column = QWidget()
        column.setMinimumWidth(250)
        box = QVBoxLayout(column)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        title = QLabel("合圖")
        title.setProperty("role", "heading")
        box.addWidget(title)

        self.sheet_table = QTableWidget(0, len(_SHEET_COLUMNS))
        self.sheet_table.setHorizontalHeaderLabels(_SHEET_COLUMNS)
        self.sheet_table.verticalHeader().setVisible(False)
        self.sheet_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 支援 Ctrl / Shift / Ctrl+A 多選：「重新排版」與「還原原始版面」
        # 會一次套用到所有選取的合圖
        self.sheet_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sheet_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sheet_table.setShowGrid(False)
        header = self.sheet_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index, width in ((1, 78), (2, 44), (3, 44), (4, 52)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(index, width)
        self.sheet_table.itemSelectionChanged.connect(self._on_sheet_selected)
        box.addWidget(self.sheet_table, 1)

        self.multi_label = QLabel("")
        self.multi_label.setProperty("role", "hint")
        self.multi_label.setWordWrap(True)
        self.multi_label.setVisible(False)
        box.addWidget(self.multi_label)

        self.blocked_label = QLabel("")
        self.blocked_label.setProperty("role", "hint")
        self.blocked_label.setWordWrap(True)
        self.blocked_label.setVisible(bool(self._blocked))
        if self._blocked:
            names = "、".join(g.name for g in self._blocked[:4])
            more = f" 等 {len(self._blocked)} 張" if len(self._blocked) > 4 else ""
            self.blocked_label.setText(f"無法編輯：{names}{more}（宣告尺寸不一致或貼圖缺失）")
        box.addWidget(self.blocked_label)
        return column

    def _build_canvas_column(self) -> QWidget:
        column = QWidget()
        box = QVBoxLayout(column)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        row = QHBoxLayout()
        self.canvas_title = QLabel("")
        self.canvas_title.setProperty("role", "heading")
        row.addWidget(self.canvas_title, 1)

        self.names_check = QCheckBox("顯示名稱")
        self.names_check.toggled.connect(lambda on: self.canvas.set_show_names(on))
        row.addWidget(self.names_check)

        fit_button = QPushButton("符合視窗")
        fit_button.setProperty("chip", True)
        fit_button.clicked.connect(lambda: self.canvas.fit_to_view())
        row.addWidget(fit_button)
        box.addLayout(row)

        self.canvas = SheetCanvas()
        self.canvas.selection_changed.connect(self._sync_selection_panel)
        # 拖曳中：只刷新讀數（不重排、不算重疊），拖起來才不會頓
        self.canvas.editing.connect(self._sync_selection_panel)
        self.canvas.layout_changed.connect(self._on_canvas_edited)
        self.canvas.zoom_changed.connect(lambda _scale: self._sync_canvas_title())
        box.addWidget(self.canvas, 1)

        hint = QLabel(
            "拖曳角落＝等比縮放（可多選一起縮）　拖曳元件＝搬移並固定位置　"
            "空白處拉框＝多選　Ctrl+A＝全選　方向鍵＝微調　滾輪＝縮放檢視"
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        box.addWidget(hint)
        return column

    def _build_controls(self) -> QWidget:
        column = QWidget()
        column.setMinimumWidth(310)
        column.setMaximumWidth(380)
        box = QVBoxLayout(column)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        box.addWidget(self._build_usage_box())
        box.addWidget(self._build_group_box())
        box.addWidget(self._build_selection_box())
        box.addWidget(self._build_pack_box())
        box.addStretch(1)
        return column

    def _build_usage_box(self) -> QWidget:
        group = QGroupBox("誰在用這張合圖")
        box = QVBoxLayout(group)
        self.usage_label = QLabel("")
        self.usage_label.setWordWrap(True)
        box.addWidget(self.usage_label)
        return group

    def _build_group_box(self) -> QWidget:
        group = QGroupBox("整張合圖")
        box = QVBoxLayout(group)
        box.setSpacing(6)

        self.revert_button = QPushButton("還原原始版面")
        self.revert_button.setToolTip(
            "回到來源 atlas 原本的樣子：比例 100%、位置與頁面尺寸都照原檔\n"
            "輸出的 atlas 會與原檔 byte-identical，貼圖像素也維持原樣\n"
            "（等於「這張合圖不要動」——全域的縮放比例對它不生效，\n"
            "壓縮設定仍然生效）\n"
            "清單多選時會一次還原所有選取的合圖"
        )
        self.revert_button.clicked.connect(self._revert_selected)
        box.addWidget(self.revert_button)
        return group

    def _build_selection_box(self) -> QWidget:
        group = QGroupBox("選取的元件")
        box = QVBoxLayout(group)
        box.setSpacing(6)

        self.selection_label = QLabel("未選取任何元件")
        self.selection_label.setWordWrap(True)
        box.addWidget(self.selection_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("比例"))
        self.item_spin = QDoubleSpinBox()
        # 下限刻意留 0：選取的元件比例不一致時用 0 顯示「多個比例」，
        # 使用者輸入任何有效值才會一起改成同一個比例
        self.item_spin.setRange(0.0, MAX_REGION_SCALE * 100)
        self.item_spin.setSpecialValueText("多個比例")
        self.item_spin.setDecimals(1)
        self.item_spin.setSuffix(" %")
        self.item_spin.setValue(100.0)
        self.item_spin.valueChanged.connect(self._on_item_scale)
        row.addWidget(self.item_spin, 1)
        box.addLayout(row)

        buttons = QHBoxLayout()
        self.unpin_button = QPushButton("取消固定")
        self.unpin_button.setToolTip("讓自動排版重新決定這些元件的位置")
        self.unpin_button.clicked.connect(self._unpin_selection)
        buttons.addWidget(self.unpin_button)

        self.select_all_button = QPushButton("全選")
        self.select_all_button.clicked.connect(lambda: self.canvas.select_all())
        buttons.addWidget(self.select_all_button)
        box.addLayout(buttons)
        return group

    def _build_pack_box(self) -> QWidget:
        group = QGroupBox("排版")
        box = QVBoxLayout(group)
        box.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("間距"))
        self.padding_spin = QSpinBox()
        self.padding_spin.setRange(0, 16)
        self.padding_spin.setSuffix(" px")
        self.padding_spin.setValue(DEFAULT_PADDING)
        self.padding_spin.setToolTip(
            "元件之間保留的透明間距。等比縮小後原本的間距會跟著變小，"
            "擋不住 GPU 取樣跨過邊界吃到隔壁的圖，所以要補回來"
        )
        self.padding_spin.valueChanged.connect(self._on_pack_option)
        row.addWidget(self.padding_spin)

        row.addWidget(QLabel("畫布"))
        self.align_combo = QComboBox()
        for name, value in _ALIGN_OPTIONS:
            self.align_combo.addItem(name, value)
        self.align_combo.currentIndexChanged.connect(self._on_pack_option)
        row.addWidget(self.align_combo, 1)
        box.addLayout(row)

        self.auto_check = QCheckBox("調整後自動重新排版（縮到最小）")
        self.auto_check.setChecked(True)
        box.addWidget(self.auto_check)

        self.pack_button = QPushButton("重新排版（縮到最小）")
        self.pack_button.setProperty("role", "primary")
        self.pack_button.setToolTip("清單多選時會一次重排所有選取的合圖")
        self.pack_button.clicked.connect(self._repack_selected)
        box.addWidget(self.pack_button)

        self.unpin_all_button = QPushButton("取消全部固定並重排")
        self.unpin_all_button.setToolTip(
            "被固定的元件不參與自動排版，累積幾個之後版面就縮不下去了。\n"
            "這個按鈕把整張合圖的固定全部解除，再重新排一次。"
        )
        self.unpin_all_button.clicked.connect(self._unpin_all)
        box.addWidget(self.unpin_all_button)

        self.canvas_label = QLabel("")
        self.canvas_label.setProperty("role", "stat")
        self.canvas_label.setWordWrap(True)
        box.addWidget(self.canvas_label)

        self.warn_label = QLabel("")
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet(f"color: {DELTA_UP_COLOUR};")
        box.addWidget(self.warn_label)
        return group

    # ------------------------------------------------------------ 合圖清單

    def _fill_sheet_table(self) -> None:
        table = self.sheet_table
        table.setRowCount(len(self._groups))
        for row, group in enumerate(self._groups):
            table.setItem(row, 0, self._sheet_name_item(group))
            table.setItem(row, 1, QTableWidgetItem(
                f"{group.src_canvas[0]}x{group.src_canvas[1]}"
            ))
            table.setItem(row, 2, QTableWidgetItem(str(group.region_count)))
            shared = len(group.atlas_names)
            item = QTableWidgetItem(f"×{shared}" if shared > 1 else "—")
            if shared > 1:
                item.setToolTip("、".join(group.atlas_names))
            table.setItem(row, 3, item)
            table.setItem(row, 4, self._sheet_state_item(group))

    def _sheet_name_item(self, group: SheetGroup) -> QTableWidgetItem:
        item = QTableWidgetItem(group.name)
        item.setToolTip(
            f"{group.page_path}\n{group.describe_usage()}\n"
            f"{format_bytes(group.source_bytes)}"
        )
        return item

    def _sheet_state_item(self, group: SheetGroup) -> QTableWidgetItem:
        """
        版面欄的三種狀態：

        * ``已改動``  這次動過、按下「套用版面」才會生效
        * ``自訂``    之前已經套用過的版面
        * ``—``       沒有自訂版面（照全域比例）——只是點開來看也是這個
        """
        layout = self._working.get(group.key)
        if group.key in self._touched:
            text = "已改動"
        elif group.key in self._committed:
            text = "自訂"
        else:
            return QTableWidgetItem("—")

        item = QTableWidgetItem(text)
        if layout is not None:
            item.setToolTip(f"{layout.describe()}（面積 {layout.area_ratio() * 100:.0f}%）")
        return item

    def _refresh_current_row(self) -> None:
        if self._current is not None:
            self._refresh_row(self._current)

    def _refresh_row(self, group: SheetGroup) -> None:
        row = next((i for i, g in enumerate(self._groups) if g is group), None)
        if row is not None:
            self.sheet_table.setItem(row, 4, self._sheet_state_item(group))

    def _selected_groups(self) -> list[SheetGroup]:
        """清單上被選取的合圖（依清單順序）"""
        rows = sorted({index.row() for index in self.sheet_table.selectedIndexes()})
        return [self._groups[r] for r in rows if 0 <= r < len(self._groups)]

    def _on_sheet_selected(self) -> None:
        groups = self._selected_groups()
        if not groups:
            self._sync_control_states()
            return
        # 畫布顯示「目前列」（最後點到的那一列）；Ctrl+A 之類沒動 current 時維持原樣
        row = self.sheet_table.currentRow()
        target = (
            self._groups[row]
            if 0 <= row < len(self._groups) and any(g is self._groups[row] for g in groups)
            else groups[0]
        )
        if target is not self._current:
            self._show_group(target)
        self._sync_control_states()

    # ------------------------------------------------------------ 載入單張合圖

    def _show_group(self, group: SheetGroup | None) -> None:
        self._current = group
        if group is None:
            self.canvas.set_sheet(None, None)
            self.canvas_title.setText("")
            self.usage_label.setText("清單上沒有可編輯的合圖。")
            self._sync_control_states()
            return

        layout = self._ensure_layout(group)
        self.canvas.set_sheet(layout, self._source_for(group))
        self.canvas.fit_to_view()

        self._syncing = True
        try:
            self.padding_spin.setValue(layout.padding)
            index = self.align_combo.findData(layout.align)
            if index >= 0:
                self.align_combo.setCurrentIndex(index)
        finally:
            self._syncing = False

        self._sync_control_states()
        self._sync_usage(group)
        self._sync_canvas_title()
        self._sync_selection_panel()
        self._sync_stats()
        self._refresh_current_row()

    def _ensure_layout(self, group: SheetGroup, scale: float | None = None) -> SheetLayout:
        """
        取這張合圖的工作副本；還沒有就建一份。

        第一次看（或第一次批次處理）這張合圖時，用目前的全域比例當起點，
        先照原樣排一次——與點開清單看到的起點一致。
        """
        layout = self._working.get(group.key)
        if layout is None:
            layout = group.build_layout(
                scale=self._default_scale if scale is None else scale,
                padding=self.padding_spin.value(),
                align=self.align_combo.currentData(),
            )
            self._working[group.key] = layout
        return layout

    def _source_for(self, group: SheetGroup) -> Image.Image | None:
        if group.key in self._sources:
            return self._sources[group.key]
        image: Image.Image | None = None
        try:
            with Image.open(group.page_path) as handle:
                image = handle.convert("RGBA")
        except OSError:
            image = None
        self._sources[group.key] = image
        return image

    def _sync_control_states(self) -> None:
        """
        依清單選取狀態切換控制項。

        多選時只留「重新排版」與「還原原始版面」（一次套用到所有選取的合圖）；
        其他調整都是「單張」的概念，多選時一律停用，畫布也改成只供檢視。
        （item_spin 由 _sync_selection_panel 依畫布選取決定，不在這裡管。）
        """
        count = len(self._selected_groups())
        multi = count > 1
        single = self._current is not None and not multi
        for widget in (
            self.padding_spin, self.align_combo, self.auto_check,
            self.unpin_button, self.select_all_button, self.unpin_all_button,
            self.names_check,
        ):
            widget.setEnabled(single)

        active = self._current is not None
        self.pack_button.setEnabled(active)
        self.revert_button.setEnabled(active)
        suffix = f"（{count} 張）" if multi else ""
        self.pack_button.setText(f"重新排版（縮到最小）{suffix}")
        self.revert_button.setText(f"還原原始版面{suffix}")

        self.canvas.set_read_only(multi)
        self.multi_label.setVisible(multi)
        if multi:
            self.multi_label.setText(
                f"已選 {count} 張合圖：「重新排版」與「還原原始版面」"
                "會一次套用到全部選取"
            )

    def _sync_usage(self, group: SheetGroup) -> None:
        lines = [f"<b>{group.name}</b>　{format_bytes(group.source_bytes)}"]
        if group.is_shared:
            lines.append(
                f"<span style='color:{DELTA_UP_COLOUR}'>"
                f"{group.describe_usage()}——套用後全部一起更新</span>"
            )
        else:
            lines.append(group.describe_usage())
        lines.append("")
        for member_name in group.atlas_names:
            lines.append(f"· {member_name}")
        projects = group.project_names
        if projects:
            lines.append("")
            lines.append("專案：" + "、".join(projects))
        self.usage_label.setText("<br>".join(lines))

    def _sync_canvas_title(self) -> None:
        layout = self.canvas.layout
        if layout is None:
            self.canvas_title.setText("")
            return
        self.canvas_title.setText(
            f"{layout.canvas[0]} x {layout.canvas[1]}　"
            f"檢視 {self.canvas.view_scale * 100:.0f}%"
        )

    # ------------------------------------------------------------ 編輯

    def _mark_touched(self) -> None:
        if self._current is not None:
            self._mark_group_touched(self._current)

    def _mark_group_touched(self, group: SheetGroup) -> None:
        self._touched.add(group.key)
        self._refresh_row(group)

    def _on_item_scale(self, value: float) -> None:
        if self._syncing or value < MIN_REGION_SCALE * 100:
            return  # 0 是「多個比例」的顯示值，不是真的要縮到 0
        selected = self.canvas.selected()
        if not selected:
            return
        for placement in selected:
            placement.set_scale(value / 100.0)
        self._mark_touched()
        self._repack(refit=False)
        self._sync_selection_panel()

    def _on_canvas_edited(self, resized: bool) -> None:
        """畫布上拖曳完成：只重排，檢視維持原樣（免得畫面跟著跳）"""
        self._mark_touched()
        if resized:
            self._repack(refit=False)
        else:
            self._sync_stats()
        self._sync_selection_panel()

    def _on_pack_option(self) -> None:
        if self._syncing:
            return
        layout = self.canvas.layout
        if layout is None:
            return
        layout.padding = self.padding_spin.value()
        layout.align = self.align_combo.currentData()
        self._mark_touched()
        self._repack(force=True)

    def _unpin_selection(self) -> None:
        selected = self.canvas.selected()
        if not selected:
            return
        for placement in selected:
            placement.pinned = False
        self._mark_touched()
        self._repack(force=True)

    def _unpin_all(self) -> None:
        """整張合圖取消固定並重排（版面縮不下去時的救命按鈕）"""
        count = self.canvas.unpin_all()
        if not count:
            self.warn_label.setText("這張合圖沒有被固定的元件")
            return
        self._mark_touched()
        self._repack(force=True)
        if not self.warn_label.text():
            self.warn_label.setText(f"已取消 {count} 個元件的固定並重新排版")

    def _revert_selected(self) -> None:
        """
        還原成來源 atlas 原本的版面（清單多選時一次還原全部選取）。

        刻意**不**接著重新排版：重排會把畫布縮到內容邊界，就不是原檔的樣子了。
        還原後的版面是「恆等版面」，輸出的 atlas 與貼圖都與原檔相同。
        """
        groups = self._selected_groups()
        if not groups:
            if self._current is None:
                return
            groups = [self._current]
        for group in groups:
            # 還沒有工作副本的直接以 100% 建：build_layout 在 100% 就是
            # 原始版面本身，不用先排一次再還原
            layout = self._ensure_layout(group, scale=1.0)
            layout.reset_to_source()
            self._mark_group_touched(group)

        if self._current is not None and any(g is self._current for g in groups):
            self.canvas.fit_to_view()
            self._sync_stats()
            self._sync_selection_panel()
        self._sync_footer()

    def _repack_selected(self) -> None:
        """
        重新排版（縮到最小）；清單多選時逐張處理，全部標成「已改動」。

        目前顯示的那張走 _repack（會同步檢視、統計與重疊警告），
        其他張直接重排工作副本即可。
        """
        groups = self._selected_groups()
        if not groups:
            if self._current is None:
                return
            groups = [self._current]

        overflowed: list[str] = []
        for group in groups:
            if group is self._current:
                continue
            layout = self._ensure_layout(group)
            if repack(layout, hint_width=layout.src_canvas[0]):
                overflowed.append(group.name)
            self._mark_group_touched(group)

        if self._current is not None and any(g is self._current for g in groups):
            self._mark_touched()
            self._repack(force=True)
        self._sync_footer()
        if overflowed:
            self.warn_label.setText(
                f"{len(overflowed)} 張合圖有元件排不進頁面上限，請縮小比例："
                + "、".join(overflowed[:4])
            )

    def _repack(self, force: bool = False, refit: bool = True) -> None:
        """
        重新排版並更新統計；auto 關閉時只在 force 時真的重排。

        ``refit`` 為 False 時保持目前的檢視（縮放與位置都不動）。改動整組比例、
        間距、對齊這類「整體」操作重新置中是合理的，但單獨拖一個元件時把檢視
        縮放整個換掉會讓畫面一直跳，反而看不出自己改了什麼。
        """
        layout = self.canvas.layout
        if layout is None:
            return
        before = layout.canvas
        if force or self.auto_check.isChecked():
            overflow = repack(layout, hint_width=layout.src_canvas[0])
            if overflow:
                self.warn_label.setText(
                    f"{len(overflow)} 個元件排不進頁面上限，請縮小比例"
                )
        if layout.canvas != before and (refit or not self.canvas.canvas_fits()):
            # refit=False 時只有「畫布長大到看不完整」才被動重新置中，
            # 免得改個元件就看不到自己在改哪裡
            self.canvas.fit_to_view()
        else:
            self.canvas.update()
        self._sync_stats()
        self._refresh_current_row()

    # ------------------------------------------------------------ 狀態同步

    def _sync_selection_panel(self) -> None:
        selected = self.canvas.selected()
        if not selected:
            self.selection_label.setText("未選取任何元件（點一下元件或拉框多選）")
            self.item_spin.setEnabled(False)
            return
        self.item_spin.setEnabled(True)

        if len(selected) == 1:
            placement = selected[0]
            src_w, src_h = placement.src_size
            dst_w, dst_h = placement.dst_size
            pin = "　（位置已固定）" if placement.pinned else ""
            names = "、".join(placement.names[:4]) or "（無名稱）"
            more = f" 等 {len(placement.names)} 個" if len(placement.names) > 4 else ""
            self.selection_label.setText(
                f"{names}{more}<br>{src_w}x{src_h} → <b>{dst_w}x{dst_h}</b>{pin}"
            )
        else:
            pinned = sum(1 for p in selected if p.pinned)
            src_area = sum(p.src_size[0] * p.src_size[1] for p in selected)
            dst_area = sum(p.dst_size[0] * p.dst_size[1] for p in selected)
            ratio = (dst_area / src_area * 100) if src_area else 100
            self.selection_label.setText(
                f"選取 {len(selected)} 個元件（固定 {pinned} 個）<br>"
                f"面積合計 {ratio:.0f}%"
            )

        scales = {round(p.scale, 6) for p in selected}
        self._syncing = True
        try:
            self.item_spin.setValue(next(iter(scales)) * 100 if len(scales) == 1 else 0.0)
        finally:
            self._syncing = False

    def _sync_stats(self) -> None:
        layout = self.canvas.layout
        if layout is None:
            self.canvas_label.setText("")
            return

        src_w, src_h = layout.src_canvas
        new_w, new_h = layout.canvas
        ratio = layout.area_ratio()
        fill = layout.used_area / (new_w * new_h) * 100 if new_w and new_h else 0.0
        if layout.is_identity:
            # 恆等版面：講清楚它同時也是「不吃全域比例」的意思，
            # 否則使用者會以為右側設定的 50% 還是會生效。
            # 壓縮設定仍然生效，這點要一起講，不能只說「與原檔相同」
            self.canvas_label.setText(
                f"{src_w}x{src_h}　<b>原始版面</b><br>"
                f"<span style='color:{DELTA_DOWN_COLOUR}'>"
                "atlas 座標與貼圖像素維持原樣</span>"
                "<br>不吃右側的縮放比例（壓縮設定仍然生效）"
            )
        else:
            colour = DELTA_DOWN_COLOUR if ratio <= 1.0 else DELTA_UP_COLOUR
            arrow = "↓" if ratio <= 1.0 else "↑"
            pinned = sum(1 for p in layout.placements if p.pinned)
            pin_text = f"　固定 {pinned} 個" if pinned else ""
            self.canvas_label.setText(
                f"{src_w}x{src_h} → {new_w}x{new_h}<br>"
                f"<span style='color:{colour}'>面積 {arrow}{abs(1 - ratio) * 100:.0f}%</span>"
                f"　填充 {fill:.0f}%{pin_text}"
            )

        overlaps = _overlapping(layout)
        self.canvas.set_overlapping(overlaps)
        messages = []
        if overlaps:
            messages.append(
                f"{len(overlaps)} 個元件互相重疊（多半是固定位置後又改了大小）——"
                "按「重新排版」或「取消固定」修正，重疊的版面不能套用"
            )
        tiny = [p for p in layout.placements if min(p.dst_size) <= 2]
        if tiny:
            messages.append(f"{len(tiny)} 個元件縮到只剩 1~2 px，畫面上會看不出內容")
        # 填充率很低幾乎都是「固定的元件把畫布撐開」造成的，直接指路
        pinned = sum(1 for p in layout.placements if p.pinned)
        if pinned and fill < 55 and not layout.is_identity:
            messages.append(
                f"填充只有 {fill:.0f}%：有 {pinned} 個元件位置被固定，"
                "畫布縮不下去——可按「取消全部固定並重排」"
            )
        self.warn_label.setText("　".join(messages))
        self._sync_canvas_title()
        self._sync_footer()

    def _sync_footer(self) -> None:
        self.footer_label.setText(
            f"已改動 {len(self._touched)} 張合圖（按「套用版面」才會生效）"
            if self._touched else "尚未改動任何合圖"
        )

    # ------------------------------------------------------------ 套用

    def _on_accept(self) -> None:
        bad: list[str] = []
        for key in self._touched:
            layout = self._working.get(key)
            if layout is None:
                continue
            if not layout.is_packed:
                bad.append(f"{_name_of(layout)}：尚未排版")
            elif _overlapping(layout):
                bad.append(f"{_name_of(layout)}：有元件重疊")
        if bad:
            QMessageBox.warning(
                self, "合圖群組編輯器",
                "以下合圖還不能套用：\n\n" + "\n".join(bad[:6]),
            )
            return
        self.accept()

    def result_layouts(self) -> tuple[list[SheetLayout], set[str]]:
        """
        回傳（要寫入的版面, 要移除版面的貼圖鍵）。

        只回報真的動過的——沒改的合圖維持原狀，不會因為「打開看過」
        就被塞進一份自訂版面。「移除自訂版面」的入口已拿掉（要回到原檔
        請用「還原原始版面」），第二個值恆為空——保留它讓呼叫端流程不變。
        """
        layouts = [
            self._working[key] for key in self._touched if key in self._working
        ]
        return layouts, set()


# ---------------------------------------------------------------- 輔助


def _name_of(layout: SheetLayout) -> str:
    return layout.page_path.name


def _overlapping(layout: SheetLayout) -> list[Placement]:
    """
    找出互相重疊的元件。

    完全相同的矩形是合法的（打包器去重讓多個名稱共用同一塊像素），
    這裡只抓「部分重疊」——那才會讓 UV 取到別張圖。
    """
    placed = [p for p in layout.placements if p.pos is not None]
    bad: set[int] = set()
    result: list[Placement] = []
    for index, first in enumerate(placed):
        ax, ay, aw, ah = first.dst_rect
        for second in placed[index + 1:]:
            bx, by, bw, bh = second.dst_rect
            if (ax, ay, aw, ah) == (bx, by, bw, bh):
                continue
            if bx >= ax + aw or bx + bw <= ax or by >= ay + ah or by + bh <= ay:
                continue
            for placement in (first, second):
                if id(placement) not in bad:
                    bad.add(id(placement))
                    result.append(placement)
    return result
