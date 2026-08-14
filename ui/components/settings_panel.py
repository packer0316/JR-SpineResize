"""處理設定面板（卡片式版面，與 JR-Img-Compresser 一致以降低學習成本）"""
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
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
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
    COMPRESSION_EFFORTS,
    DEFAULT_PNG_QUALITY,
    MAX_SCALE_PERCENT,
    MAX_TARGET_SIZE_KB,
    MIN_SCALE_PERCENT,
    MIN_TARGET_SIZE_KB,
    MODE_REMAP_ONLY,
    MODE_RESCALE,
    OUTPUT_CUSTOM,
    OUTPUT_INPLACE,
    OUTPUT_SUBFOLDER,
    PAGE_ALIGN_4,
    PAGE_ALIGN_NONE,
    PAGE_ALIGN_POT,
    PNG_COLOR_FORMATS,
    PNG_MODES,
    RESAMPLE_FILTERS,
)
from models.compression_options import (
    CompressionEffort,
    CompressionOptions,
    PngColorFormat,
    PngMode,
)
from models.process_options import ProcessOptions

# 表單標籤欄寬度（與 JR-Img-Compresser 相同，控制項左緣對齊）
_LABEL_WIDTH = 70


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "hint")
    label.setWordWrap(True)
    # 說明文字要能跟著面板寬度縮，但不能用 Ignored——那會讓版面以「最窄寬度」
    # 去推算換行後的高度，把整個群組撐得很高。
    label.setMinimumWidth(1)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


def _accent(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "accent")
    label.setWordWrap(True)
    label.setMinimumWidth(1)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


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
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_mode_card())
        layout.addWidget(self._build_compression_card())
        layout.addWidget(self._build_resize_card())
        layout.addWidget(self._build_advanced_card())
        layout.addWidget(self._build_output_card())
        layout.addStretch(1)

        self.setWidget(container)
        self._connect_signals()
        self._sync_enabled()

    # ------------------------------------------------------------ 卡片建構輔助

    def _make_card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("settingsCard")
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setProperty("role", "cardTitle")
        layout.addWidget(title_label)
        return card, layout

    def _form_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setFixedWidth(_LABEL_WIDTH)
        label.setProperty("role", "formLabel")
        return label

    def _form_row(self, text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._form_label(text))
        row.addWidget(widget, 1)
        return row

    def _slider_row(self, text: str, default: int, minimum: int = 1,
                    maximum: int = 100) -> tuple[QHBoxLayout, QSlider, QLabel]:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._form_label(text))

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(default)
        row.addWidget(slider, 1)

        value_label = QLabel(str(default))
        value_label.setFixedWidth(32)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_label.setProperty("role", "valueLabel")
        row.addWidget(value_label)

        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))
        return row, slider, value_label

    def _chip_buttons(self, values: list[int], callback, suffix: str = "") -> QWidget:
        """快速選擇按鈕列（與標籤欄對齊）"""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(_LABEL_WIDTH + 8, 0, 0, 0)
        layout.setSpacing(4)
        for value in values:
            button = QPushButton(f"{value}{suffix}")
            button.setProperty("chip", True)
            button.setFixedHeight(22)
            button.setMinimumWidth(40)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _, v=value: callback(v))
            layout.addWidget(button, 1)
        return widget

    @staticmethod
    def _separator() -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setProperty("role", "separator")
        return sep

    # ------------------------------------------------------------ 卡片 1：處理模式

    def _build_mode_card(self) -> QFrame:
        card, layout = self._make_card("🧭 處理模式")
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
            _hint("沿用你原本用 JR-Img-Compresser 縮圖的流程，本工具只負責把 atlas 數值對齊；"
                  "貼圖會原樣複製，壓縮與尺寸設定不參與。")
        )

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
            _hint("勾選時會直接用「新貼圖尺寸 ÷ atlas 宣告尺寸」當比例，不受尺寸調整數值影響。")
        )
        layout.addWidget(self.prescaled_widget)
        return card

    def _browse_prescaled(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "選擇已縮好的貼圖資料夾")
        if folder:
            self.prescaled_edit.setText(folder)

    # ------------------------------------------------------------ 卡片 2：壓縮設定

    def _build_compression_card(self) -> QFrame:
        card, layout = self._make_card("🗜️ 壓縮設定")
        self.compression_card = card

        self.png_mode_combo = _shrinkable(QComboBox())
        for key, name in PNG_MODES.items():
            self.png_mode_combo.addItem(name, key)
        self.png_mode_combo.setToolTip(
            "智慧有損：256 色量化（同 TinyPNG），檔案可縮小 60-80%\n"
            "無損：像素完全不變，適合要再進遊戲引擎的素材"
        )
        layout.addLayout(self._form_row("模式", self.png_mode_combo))

        self.png_color_format_combo = _shrinkable(QComboBox())
        for key, name in PNG_COLOR_FORMATS.items():
            self.png_color_format_combo.addItem(name, key)
        self.png_color_format_combo.setToolTip(
            "模擬素材進遊戲引擎轉成 16-bit 貼圖後的實際畫面\n"
            "（PNG 規格存不出 4444/565，這裡是量化像素後仍存成合法 PNG）\n\n"
            "RGBA8888：不量化，保留原始色彩\n"
            "RGBA5551：透明度只剩鏤空／不鏤空，適合去背圖示\n"
            "RGBA4444：半透明貼圖最常用，漸層易斷階\n"
            "RGB565：不含透明通道，透明區以白底合成"
        )
        layout.addLayout(self._form_row("色彩格式", self.png_color_format_combo))

        self.png_format_dither_check = QCheckBox("量化抖動（減少漸層斷階）")
        self.png_format_dither_check.setToolTip(
            "以 Bayer 有序抖動平滑量化後的漸層，減少色帶\n"
            "代價是產生高頻雜訊，平塗類素材檔案可能變成約兩倍大\n"
            "關閉時＝引擎未開抖動的結果（要對照引擎畫面請關閉）"
        )
        self.png_format_dither_check.setVisible(False)  # RGBA8888 時不需要
        layout.addWidget(self.png_format_dither_check)

        # 有損子選項
        self.png_lossy_box = QWidget()
        lossy_layout = QVBoxLayout(self.png_lossy_box)
        lossy_layout.setContentsMargins(0, 0, 0, 0)
        lossy_layout.setSpacing(8)

        row, self.png_quality_slider, _ = self._slider_row("品質", DEFAULT_PNG_QUALITY)
        lossy_layout.addLayout(row)
        lossy_layout.addWidget(self._chip_buttons(
            [50, 65, 80, 90, 100], lambda v: self.png_quality_slider.setValue(v)
        ))

        row, self.png_dither_slider, _ = self._slider_row("漸層抖動", 100, 0, 100)
        self.png_dither_slider.setToolTip("抖動可平滑漸層；純色 UI 圖可調低使檔案更小")
        lossy_layout.addLayout(row)

        self.png_lossy_box.setVisible(False)  # 預設無損模式
        layout.addWidget(self.png_lossy_box)

        self.effort_combo = _shrinkable(QComboBox())
        for key, name in COMPRESSION_EFFORTS.items():
            self.effort_combo.addItem(name, key)
        self.effort_combo.setCurrentIndex(1)  # 標準
        self.effort_combo.setToolTip("oxipng 無損最佳化強度：越高檔案越小，處理越慢")
        layout.addLayout(self._form_row("最佳化強度", self.effort_combo))

        layout.addWidget(_accent("✨ 同 TinyPNG 演算法 + oxipng 二次無損最佳化"))
        layout.addWidget(self._separator())

        # === 通用選項 ===
        self.remove_exif_check = QCheckBox("移除中繼資料（EXIF）")
        self.remove_exif_check.setChecked(True)
        self.remove_exif_check.setToolTip("移除相機資訊等中繼資料，減少檔案大小")
        layout.addWidget(self.remove_exif_check)

        target_row = QHBoxLayout()
        target_row.setSpacing(8)
        self.target_size_check = QCheckBox("目標檔案大小")
        self.target_size_check.setToolTip(
            "自動搜尋符合目標大小的最高品質\n（適用於智慧有損模式；品質滑桿將被忽略）"
        )
        target_row.addWidget(self.target_size_check)

        self.target_size_spin = QSpinBox()
        self.target_size_spin.setRange(MIN_TARGET_SIZE_KB, MAX_TARGET_SIZE_KB)
        self.target_size_spin.setValue(500)
        self.target_size_spin.setSuffix(" KB")
        self.target_size_spin.setEnabled(False)
        target_row.addWidget(self.target_size_spin)
        target_row.addStretch(1)
        layout.addLayout(target_row)

        return card

    # ------------------------------------------------------------ 卡片 3：尺寸調整

    def _build_resize_card(self) -> QFrame:
        card, layout = self._make_card("📐 尺寸調整")
        self.resize_card = card

        self.resize_enabled_check = QCheckBox("啟用 Resize")
        self.resize_enabled_check.setChecked(True)
        self.resize_enabled_check.setToolTip("關閉時比例固定 100%，只做壓縮不縮放")
        layout.addWidget(self.resize_enabled_check)

        self.resize_options_box = QWidget()
        box_layout = QVBoxLayout(self.resize_options_box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(10)

        # Spine 的座標系統要求整份專案等比縮放，因此只有「按百分比」一種模式
        self.resize_mode_combo = _shrinkable(QComboBox())
        self.resize_mode_combo.addItem("按百分比（綁定整個 Spine）", "percentage")
        self.resize_mode_combo.setToolTip(
            "Spine 的 atlas 座標必須與貼圖同比例縮放，\n因此縮放比例套用到整份專案的所有貼圖與 atlas 數值"
        )
        box_layout.addLayout(self._form_row("縮放模式", self.resize_mode_combo))

        value_row = QHBoxLayout()
        value_row.setSpacing(8)
        value_row.addWidget(self._form_label("數值"))
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(MIN_SCALE_PERCENT, MAX_SCALE_PERCENT)
        self.scale_spin.setDecimals(2)
        self.scale_spin.setSuffix(" %")
        self.scale_spin.setValue(50.0)
        value_row.addWidget(self.scale_spin, 1)
        box_layout.addLayout(value_row)

        box_layout.addWidget(self._chip_buttons(
            [10, 25, 50, 75, 90], lambda v: self.scale_spin.setValue(float(v)), "%"
        ))

        self.resample_combo = _shrinkable(QComboBox())
        for key, label in RESAMPLE_FILTERS.items():
            self.resample_combo.addItem(label, key)
        box_layout.addLayout(self._form_row("插值演算法", self.resample_combo))

        layout.addWidget(self.resize_options_box)
        return card

    # ------------------------------------------------------------ 卡片 4：進階

    def _build_advanced_card(self) -> QFrame:
        card, layout = self._make_card("🎛️ 進階")
        self.advanced_card = card

        self.alpha_combo = _shrinkable(QComboBox())
        self.alpha_combo.addItem("預乘後縮放（避免透明邊黑框）", ALPHA_MODE_PREMULTIPLY)
        self.alpha_combo.addItem("直接縮放", ALPHA_MODE_NONE)
        layout.addLayout(self._form_row("透明處理", self.alpha_combo))

        bleed_row = QHBoxLayout()
        bleed_row.setSpacing(8)
        bleed_row.addWidget(self._form_label("邊緣填充"))
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
        layout.addLayout(bleed_row)

        self.align_combo = _shrinkable(QComboBox())
        self.align_combo.addItem("不變（等比縮放）", PAGE_ALIGN_NONE)
        self.align_combo.addItem("補到 4 的倍數", PAGE_ALIGN_4)
        self.align_combo.addItem("補到 2 的次方", PAGE_ALIGN_POT)
        layout.addLayout(self._form_row("畫布對齊", self.align_combo))

        layout.addWidget(_hint("對齊只會把畫布補大，不會改變縮放比例，因此不影響播放結果。"))
        return card

    # ------------------------------------------------------------ 卡片 5：輸出

    def _build_output_card(self) -> QFrame:
        card, layout = self._make_card("📤 輸出")
        layout.setSpacing(6)

        self.out_inplace = QRadioButton("覆蓋原檔")
        self.out_subfolder = QRadioButton("輸出到子資料夾")
        self.out_custom = QRadioButton("輸出到指定路徑")
        self.output_group = QButtonGroup(self)
        for button in (self.out_inplace, self.out_subfolder, self.out_custom):
            self.output_group.addButton(button)
        self.out_inplace.setChecked(True)

        layout.addWidget(self.out_inplace)

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
        return card

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
            self.png_mode_combo,
            self.png_color_format_combo,
            self.effort_combo,
            self.resize_mode_combo,
            self.resample_combo,
            self.alpha_combo,
            self.bleed_combo,
            self.align_combo,
        ):
            combo.currentIndexChanged.connect(self._on_changed)
        for check in (
            self.png_format_dither_check,
            self.remove_exif_check,
            self.target_size_check,
            self.resize_enabled_check,
            self.derive_check,
            self.copy_skeleton_check,
        ):
            check.toggled.connect(self._on_changed)
        for slider in (self.png_quality_slider, self.png_dither_slider):
            slider.valueChanged.connect(self._on_changed)
        self.scale_spin.valueChanged.connect(self._on_changed)
        self.bleed_spin.valueChanged.connect(self._on_changed)
        self.target_size_spin.valueChanged.connect(self._on_changed)
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

        # 模式 B 沿用外部貼圖：壓縮與進階設定不參與
        self.compression_card.setEnabled(rescale)
        self.advanced_card.setEnabled(rescale)

        # 壓縮卡內部連動
        lossy = self.png_mode_combo.currentData() == PngMode.LOSSY.value
        self.png_lossy_box.setVisible(lossy)
        self.png_format_dither_check.setVisible(
            self.png_color_format_combo.currentData() != PngColorFormat.RGBA8888.value
        )
        self.target_size_check.setEnabled(lossy)
        self.target_size_spin.setEnabled(lossy and self.target_size_check.isChecked())

        # 尺寸調整卡
        if rescale:
            self.resize_card.setEnabled(True)
            self.resize_enabled_check.setEnabled(True)
            self.resize_options_box.setEnabled(self.resize_enabled_check.isChecked())
        else:
            # 模式 B：比例僅在「不由貼圖推算」時使用
            self.resize_card.setEnabled(True)
            self.resize_enabled_check.setEnabled(False)
            self.resize_options_box.setEnabled(not self.derive_check.isChecked())

        self.bleed_spin.setEnabled(rescale and self.bleed_combo.currentData() != BLEED_NONE)
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

        compression = CompressionOptions(
            png_mode=PngMode(self.png_mode_combo.currentData()),
            png_quality=self.png_quality_slider.value(),
            png_dithering=self.png_dither_slider.value() / 100.0,
            png_color_format=PngColorFormat(self.png_color_format_combo.currentData()),
            png_format_dither=self.png_format_dither_check.isChecked(),
            effort=CompressionEffort(self.effort_combo.currentData()),
            remove_exif=self.remove_exif_check.isChecked(),
            target_size_enabled=self.target_size_check.isChecked(),
            target_size_kb=self.target_size_spin.value(),
        )

        return ProcessOptions(
            mode=MODE_RESCALE if self.mode_rescale.isChecked() else MODE_REMAP_ONLY,
            resize_enabled=self.resize_enabled_check.isChecked(),
            scale_percent=self.scale_spin.value(),
            resample=self.resample_combo.currentData(),
            alpha_mode=self.alpha_combo.currentData(),
            bleed=self.bleed_combo.currentData(),
            bleed_px=self.bleed_spin.value(),
            page_align=self.align_combo.currentData(),
            compression=compression,
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
            self.resize_enabled_check.setChecked(options.resize_enabled)
            self.scale_spin.setValue(options.scale_percent)
            self._select_data(self.resample_combo, options.resample)
            self._select_data(self.alpha_combo, options.alpha_mode)
            self._select_data(self.bleed_combo, options.bleed)
            self.bleed_spin.setValue(options.bleed_px)
            self._select_data(self.align_combo, options.page_align)

            compression = options.compression
            self._select_data(self.png_mode_combo, compression.png_mode.value)
            self._select_data(self.png_color_format_combo, compression.png_color_format.value)
            self.png_format_dither_check.setChecked(compression.png_format_dither)
            self.png_quality_slider.setValue(compression.png_quality)
            self.png_dither_slider.setValue(round(compression.png_dithering * 100))
            self._select_data(self.effort_combo, compression.effort.value)
            self.remove_exif_check.setChecked(compression.remove_exif)
            self.target_size_check.setChecked(compression.target_size_enabled)
            self.target_size_spin.setValue(compression.target_size_kb)

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
            self.resize_enabled_check,
            self.scale_spin,
            self.resize_mode_combo,
            self.resample_combo,
            self.alpha_combo,
            self.bleed_combo,
            self.bleed_spin,
            self.align_combo,
            self.png_mode_combo,
            self.png_color_format_combo,
            self.png_format_dither_check,
            self.png_quality_slider,
            self.png_dither_slider,
            self.effort_combo,
            self.remove_exif_check,
            self.target_size_check,
            self.target_size_spin,
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
