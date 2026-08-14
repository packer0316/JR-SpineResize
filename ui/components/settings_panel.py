"""處理設定面板"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.constants import (
    ALPHA_MODE_NONE,
    ALPHA_MODE_PREMULTIPLY,
    BLEED_FULL,
    BLEED_NONE,
    BLEED_RGB,
    MAX_SCALE_PERCENT,
    MIN_SCALE_PERCENT,
    MODE_REMAP_ONLY,
    MODE_RESCALE,
    OUTPUT_CUSTOM,
    OUTPUT_INPLACE,
    OUTPUT_SUBFOLDER,
    PAGE_ALIGN_4,
    PAGE_ALIGN_NONE,
    PAGE_ALIGN_POT,
    PNG_FORMAT_MATCH,
    PNG_FORMAT_PALETTE,
    PNG_FORMAT_RGBA,
    RESAMPLE_FILTERS,
)
from models.process_options import ProcessOptions


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "hint")
    label.setWordWrap(True)
    # 說明文字要能跟著面板寬度縮，但不能用 Ignored——那會讓版面以「最窄寬度」
    # 去推算換行後的高度，把整個群組撐得很高。
    label.setMinimumWidth(1)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


_PNG_FORMAT_HINTS = {
    PNG_FORMAT_MATCH: (
        "來源是 8-bit 調色盤 PNG（被 pngquant / TinyPNG 壓過）就輸出調色盤，"
        "否則維持 RGBA。這是檔案大小的關鍵——存成 32-bit 的話，尺寸砍半也不會變小。"
    ),
    PNG_FORMAT_RGBA: (
        "一律輸出 32-bit RGBA，零額外損失，但已量化過的素材會明顯變大；"
        "適合之後再交給 JR-Img-Compresser 壓縮。"
    ),
    PNG_FORMAT_PALETTE: (
        "一律量化成 8-bit 調色盤（imagequant），檔案最小，但對原本是全彩的素材是有損的。"
    ),
}


def _compact(group: QGroupBox) -> QGroupBox:
    """群組只佔用需要的高度，不要被垂直拉伸"""
    group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return group


def _shrinkable(combo: QComboBox) -> QComboBox:
    """讓下拉選單不要因為最長的項目文字而把面板撐寬"""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(8)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return combo


class SettingsPanel(QScrollArea):
    """所有處理選項；改動時發出 options_changed"""

    options_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        # 單選鈕與勾選框的文字不會自動換行，視窗被拉很窄時寧可出現捲軸也不要裁掉內容
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_mode_group())
        layout.addWidget(self._build_scale_group())
        layout.addWidget(self._build_advanced_group())
        layout.addWidget(self._build_output_group())
        layout.addStretch(1)

        self.setWidget(container)
        self._connect_signals()
        self._sync_enabled()

    # ------------------------------------------------------------ 模式

    def _build_mode_group(self) -> QGroupBox:
        group = QGroupBox("處理模式")
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        self.mode_rescale = QRadioButton("縮放貼圖並重寫 atlas（推薦）")
        self.mode_remap = QRadioButton("只重算 atlas（貼圖已在外部縮好）")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_rescale)
        self.mode_group.addButton(self.mode_remap)
        self.mode_rescale.setChecked(True)

        layout.addWidget(self.mode_rescale)
        layout.addWidget(
            _hint("逐圖塊裁切、各自縮放後放回原位置，不會有滲色與接縫，品質最好。")
        )
        layout.addSpacing(6)
        layout.addWidget(self.mode_remap)
        layout.addWidget(
            _hint("沿用你原本用 JR-Img-Compresser 縮圖的流程，本工具只負責把 atlas 數值對齊。")
        )
        return _compact(group)

    # ------------------------------------------------------------ 縮放

    def _build_scale_group(self) -> QGroupBox:
        group = QGroupBox("縮放")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        scale_row = QHBoxLayout()
        scale_row.setSpacing(6)
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(MIN_SCALE_PERCENT, MAX_SCALE_PERCENT)
        self.scale_spin.setDecimals(2)
        self.scale_spin.setSuffix(" %")
        self.scale_spin.setValue(50.0)
        self.scale_spin.setFixedWidth(118)  # 數值 + 後綴 + 上下鈕
        scale_row.addWidget(self.scale_spin)
        for preset in (25, 50, 75):
            button = QPushButton(f"{preset}%")
            button.setProperty("compact", True)
            button.setFixedWidth(48)
            button.clicked.connect(lambda _, v=preset: self.scale_spin.setValue(float(v)))
            scale_row.addWidget(button)
        scale_row.addStretch(1)
        form.addRow("縮放比例", scale_row)

        self.resample_combo = _shrinkable(QComboBox())
        for key, label in RESAMPLE_FILTERS.items():
            self.resample_combo.addItem(label, key)
        form.addRow("重取樣", self.resample_combo)
        layout.addLayout(form)

        # ---- 只重算 atlas 模式專用 ----
        self.prescaled_widget = QWidget()
        pre_layout = QVBoxLayout(self.prescaled_widget)
        pre_layout.setContentsMargins(0, 8, 0, 0)
        pre_layout.setSpacing(4)

        pre_layout.addWidget(QLabel("已縮好的貼圖資料夾"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.prescaled_edit = QLineEdit()
        self.prescaled_edit.setPlaceholderText("留空 = 與 atlas 同一層")
        self.prescaled_edit.setMinimumWidth(80)
        browse = QPushButton("瀏覽…")
        browse.setFixedWidth(64)
        browse.clicked.connect(self._browse_prescaled)
        row.addWidget(self.prescaled_edit, 1)
        row.addWidget(browse)
        pre_layout.addLayout(row)

        self.derive_check = QCheckBox("由貼圖實際尺寸推算縮放比例（建議）")
        self.derive_check.setChecked(True)
        pre_layout.addWidget(self.derive_check)
        pre_layout.addWidget(
            _hint("勾選時會直接用「新貼圖尺寸 ÷ atlas 宣告尺寸」當比例，不受上方數值影響。")
        )
        layout.addWidget(self.prescaled_widget)
        return _compact(group)

    def _browse_prescaled(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "選擇已縮好的貼圖資料夾")
        if folder:
            self.prescaled_edit.setText(folder)

    # ------------------------------------------------------------ 進階

    def _build_advanced_group(self) -> QGroupBox:
        group = QGroupBox("進階")
        outer = QVBoxLayout(group)
        outer.setSpacing(6)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.alpha_combo = _shrinkable(QComboBox())
        self.alpha_combo.addItem("預乘後縮放（避免透明邊黑框）", ALPHA_MODE_PREMULTIPLY)
        self.alpha_combo.addItem("直接縮放", ALPHA_MODE_NONE)
        form.addRow("透明處理", self.alpha_combo)

        bleed_row = QHBoxLayout()
        self.bleed_combo = _shrinkable(QComboBox())
        self.bleed_combo.addItem("滲出顏色（推薦）", BLEED_RGB)
        self.bleed_combo.addItem("連 alpha 一起外擴", BLEED_FULL)
        self.bleed_combo.addItem("不處理", BLEED_NONE)
        self.bleed_spin = QSpinBox()
        self.bleed_spin.setRange(0, 8)
        self.bleed_spin.setValue(2)
        self.bleed_spin.setSuffix(" px")
        self.bleed_spin.setFixedWidth(84)
        bleed_row.addWidget(self.bleed_combo, 1)
        bleed_row.addWidget(self.bleed_spin)
        form.addRow("邊緣填充", bleed_row)

        self.align_combo = _shrinkable(QComboBox())
        self.align_combo.addItem("不變（等比縮放）", PAGE_ALIGN_NONE)
        self.align_combo.addItem("補到 4 的倍數", PAGE_ALIGN_4)
        self.align_combo.addItem("補到 2 的次方", PAGE_ALIGN_POT)
        form.addRow("畫布對齊", self.align_combo)

        self.png_format_combo = _shrinkable(QComboBox())
        self.png_format_combo.addItem("跟隨來源（推薦）", PNG_FORMAT_MATCH)
        self.png_format_combo.addItem("32-bit RGBA（最高品質）", PNG_FORMAT_RGBA)
        self.png_format_combo.addItem("8-bit 調色盤（最小檔案）", PNG_FORMAT_PALETTE)
        form.addRow("貼圖編碼", self.png_format_combo)

        outer.addLayout(form)
        outer.addWidget(_hint("對齊只會把畫布補大，不會改變縮放比例，因此不影響播放結果。"))
        self.png_format_hint = _hint("")
        outer.addWidget(self.png_format_hint)
        return _compact(group)

    # ------------------------------------------------------------ 輸出

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("輸出")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        self.out_subfolder = QRadioButton("輸出到子資料夾")
        self.out_custom = QRadioButton("輸出到指定路徑")
        self.out_inplace = QRadioButton("原地覆蓋（會先建立 .bak 備份）")
        self.output_group = QButtonGroup(self)
        for button in (self.out_subfolder, self.out_custom, self.out_inplace):
            self.output_group.addButton(button)
        self.out_subfolder.setChecked(True)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(6)
        sub_row.addWidget(self.out_subfolder)
        self.subfolder_edit = QLineEdit("resized")
        self.subfolder_edit.setMinimumWidth(80)
        self.subfolder_edit.setMaximumWidth(140)
        sub_row.addWidget(self.subfolder_edit)
        sub_row.addStretch(1)
        layout.addLayout(sub_row)

        layout.addWidget(self.out_custom)
        custom_row = QHBoxLayout()
        custom_row.setSpacing(6)
        custom_row.setContentsMargins(20, 0, 0, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("選擇輸出資料夾")
        self.output_edit.setMinimumWidth(80)
        browse = QPushButton("瀏覽…")
        browse.setFixedWidth(64)
        browse.clicked.connect(self._browse_output)
        custom_row.addWidget(self.output_edit, 1)
        custom_row.addWidget(browse)
        layout.addLayout(custom_row)

        layout.addWidget(self.out_inplace)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("例如 _half（留空 = 不改檔名）")
        self.suffix_edit.setMinimumWidth(80)
        form.addRow("檔名後綴", self.suffix_edit)
        layout.addLayout(form)

        self.copy_skeleton_check = QCheckBox("一併複製 .skel / .json 到輸出資料夾")
        self.copy_skeleton_check.setChecked(True)
        layout.addWidget(self.copy_skeleton_check)
        layout.addWidget(
            _hint("骨架檔只會被原樣複製，內容絕不修改——等比縮貼圖時骨架本來就不該改動。")
        )
        return _compact(group)

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾")
        if folder:
            self.output_edit.setText(folder)
            self.out_custom.setChecked(True)

    # ------------------------------------------------------------ 連動

    def _connect_signals(self) -> None:
        for widget in (
            self.mode_rescale,
            self.mode_remap,
            self.out_subfolder,
            self.out_custom,
            self.out_inplace,
        ):
            widget.toggled.connect(self._on_changed)
        for combo in (
            self.resample_combo,
            self.alpha_combo,
            self.bleed_combo,
            self.align_combo,
            self.png_format_combo,
        ):
            combo.currentIndexChanged.connect(self._on_changed)
        self.scale_spin.valueChanged.connect(self._on_changed)
        self.bleed_spin.valueChanged.connect(self._on_changed)
        self.derive_check.toggled.connect(self._on_changed)
        self.copy_skeleton_check.toggled.connect(self._on_changed)
        for edit in (
            self.subfolder_edit,
            self.output_edit,
            self.suffix_edit,
            self.prescaled_edit,
        ):
            edit.textChanged.connect(self._on_changed)

    def _on_changed(self) -> None:
        self._sync_enabled()
        self.options_changed.emit()

    def _sync_enabled(self) -> None:
        rescale = self.mode_rescale.isChecked()
        self.prescaled_widget.setVisible(not rescale)
        self.resample_combo.setEnabled(rescale)
        self.alpha_combo.setEnabled(rescale)
        self.bleed_combo.setEnabled(rescale)
        self.bleed_spin.setEnabled(rescale and self.bleed_combo.currentData() != BLEED_NONE)
        self.align_combo.setEnabled(rescale)
        self.png_format_combo.setEnabled(rescale)
        self.png_format_hint.setText(_PNG_FORMAT_HINTS[self.png_format_combo.currentData()])
        self.scale_spin.setEnabled(rescale or not self.derive_check.isChecked())
        self.subfolder_edit.setEnabled(self.out_subfolder.isChecked())
        self.output_edit.setEnabled(self.out_custom.isChecked())

    # ------------------------------------------------------------ 存取

    def get_options(self) -> ProcessOptions:
        if self.out_inplace.isChecked():
            output_mode = OUTPUT_INPLACE
        elif self.out_custom.isChecked():
            output_mode = OUTPUT_CUSTOM
        else:
            output_mode = OUTPUT_SUBFOLDER

        prescaled = self.prescaled_edit.text().strip()
        output_dir = self.output_edit.text().strip()

        return ProcessOptions(
            mode=MODE_RESCALE if self.mode_rescale.isChecked() else MODE_REMAP_ONLY,
            scale_percent=self.scale_spin.value(),
            resample=self.resample_combo.currentData(),
            alpha_mode=self.alpha_combo.currentData(),
            bleed=self.bleed_combo.currentData(),
            bleed_px=self.bleed_spin.value(),
            page_align=self.align_combo.currentData(),
            png_format=self.png_format_combo.currentData(),
            prescaled_dir=Path(prescaled) if prescaled else None,
            derive_scale_from_image=self.derive_check.isChecked(),
            output_mode=output_mode,
            output_dir=Path(output_dir) if output_dir else None,
            subfolder_name=self.subfolder_edit.text().strip() or "resized",
            filename_suffix=self.suffix_edit.text().strip(),
            copy_skeleton=self.copy_skeleton_check.isChecked(),
        )

    def set_options(self, options: ProcessOptions) -> None:
        blockers = [w.blockSignals(True) for w in self._all_inputs()]
        try:
            self.mode_rescale.setChecked(options.mode == MODE_RESCALE)
            self.mode_remap.setChecked(options.mode == MODE_REMAP_ONLY)
            self.scale_spin.setValue(options.scale_percent)
            self._select_data(self.resample_combo, options.resample)
            self._select_data(self.alpha_combo, options.alpha_mode)
            self._select_data(self.bleed_combo, options.bleed)
            self.bleed_spin.setValue(options.bleed_px)
            self._select_data(self.align_combo, options.page_align)
            self._select_data(self.png_format_combo, options.png_format)
            self.prescaled_edit.setText(str(options.prescaled_dir) if options.prescaled_dir else "")
            self.derive_check.setChecked(options.derive_scale_from_image)
            self.out_subfolder.setChecked(options.output_mode == OUTPUT_SUBFOLDER)
            self.out_custom.setChecked(options.output_mode == OUTPUT_CUSTOM)
            self.out_inplace.setChecked(options.output_mode == OUTPUT_INPLACE)
            self.output_edit.setText(str(options.output_dir) if options.output_dir else "")
            self.subfolder_edit.setText(options.subfolder_name)
            self.suffix_edit.setText(options.filename_suffix)
            self.copy_skeleton_check.setChecked(options.copy_skeleton)
        finally:
            for widget, blocked in zip(self._all_inputs(), blockers):
                widget.blockSignals(blocked)
        self._sync_enabled()

    def _all_inputs(self) -> list:
        return [
            self.mode_rescale,
            self.mode_remap,
            self.scale_spin,
            self.resample_combo,
            self.alpha_combo,
            self.bleed_combo,
            self.bleed_spin,
            self.align_combo,
            self.png_format_combo,
            self.prescaled_edit,
            self.derive_check,
            self.out_subfolder,
            self.out_custom,
            self.out_inplace,
            self.output_edit,
            self.subfolder_edit,
            self.suffix_edit,
            self.copy_skeleton_check,
        ]

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
