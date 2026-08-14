"""
應用程式樣式

設計語言與 JR-Img-Compresser 一致（Slate & Indigo）：
* 三層背景：視窗 < 卡片；輸入框比卡片更深
* 文字與勾選類控制項一律透明底，不出現色塊
* CheckBox / RadioButton 的勾與圓點用 PIL 產生的圖示（抗鋸齒）
"""
from __future__ import annotations

from dataclasses import dataclass

from ui.styles.indicators import arrow_icon, checkmark_icon, radio_dot_icon


@dataclass(frozen=True)
class Palette:
    name: str
    bg_window: str
    bg_panel: str
    bg_input: str
    bg_hover: str
    bg_selected: str
    primary: str
    primary_hover: str
    primary_light: str
    border: str
    border_focus: str
    text: str
    text_secondary: str
    text_disabled: str
    ok: str
    warn: str
    error: str


LIGHT = Palette(
    name="標準",
    bg_window="#EEF1F6",
    bg_panel="#FFFFFF",
    bg_input="#F8FAFC",
    bg_hover="#F1F5F9",
    bg_selected="#EEF2FF",
    primary="#4F46E5",
    primary_hover="#4338CA",
    primary_light="#EEF2FF",
    border="#E2E8F0",
    border_focus="#818CF8",
    text="#1E293B",
    text_secondary="#64748B",
    text_disabled="#CBD5E1",
    ok="#059669",
    warn="#D97706",
    error="#DC2626",
)

DARK = Palette(
    name="暗黑",
    bg_window="#0F172A",
    bg_panel="#1E293B",
    bg_input="#172033",
    bg_hover="#334155",
    bg_selected="#1E3A5F",
    primary="#6366F1",
    primary_hover="#818CF8",
    primary_light="#312E81",
    border="#334155",
    border_focus="#6366F1",
    text="#F1F5F9",
    text_secondary="#94A3B8",
    text_disabled="#475569",
    ok="#34D399",
    warn="#FBBF24",
    error="#F87171",
)

THEMES = {"light": LIGHT, "dark": DARK}

# 表格內以 QColor 直接上色的容量變化（兩種主題共用，綠降紅升）
DELTA_DOWN_COLOUR = "#16a34a"
DELTA_UP_COLOUR = "#dc2626"


def build_stylesheet(p: Palette) -> str:
    check_url = checkmark_icon(p.primary)
    dot_url = radio_dot_icon(p.primary)
    arrow_down = arrow_icon(p.text_secondary, "down")
    arrow_up = arrow_icon(p.text_secondary, "up")

    return f"""
QWidget {{
    background-color: {p.bg_window};
    color: {p.text};
    font-family: 'Segoe UI', 'Microsoft JhengHei UI', sans-serif;
    font-size: 13px;
}}

/* ===== 文字與勾選類：一律透明底，不出現色塊 ===== */
QLabel {{
    background-color: transparent;
    border: none;
    color: {p.text};
}}
QLabel[role="hint"] {{
    color: {p.text_secondary};
    font-size: 12px;
}}
QLabel[role="heading"] {{
    font-size: 15px;
    font-weight: 600;
}}
QLabel[role="stat"] {{
    color: {p.text_secondary};
    font-size: 12px;
}}
QLabel[role="cardTitle"] {{
    font-size: 14px;
    font-weight: bold;
}}
QLabel[role="formLabel"] {{
    color: {p.text_secondary};
}}
QLabel[role="valueLabel"] {{
    color: {p.primary};
    font-weight: bold;
}}
QLabel[role="accent"] {{
    color: {p.primary};
    font-size: 11px;
}}
QLabel[role="delta-down"] {{
    color: {p.ok};
    font-weight: 600;
}}
QLabel[role="delta-up"] {{
    color: {p.warn};
    font-weight: 600;
}}

/* ===== 設定卡片（與 JR-Img-Compresser 同款）===== */
QFrame#settingsCard {{
    background-color: {p.bg_panel};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QFrame#settingsCard .QWidget {{ background-color: transparent; }}
QFrame#settingsCard .QFrame {{ background-color: transparent; }}
QFrame#settingsCard QCheckBox {{ background-color: transparent; }}
QFrame#settingsCard QRadioButton {{ background-color: transparent; }}
QFrame#settingsCard QSlider {{ background-color: transparent; }}

/* 快速選擇小按鈕（25% / 50%…） */
QPushButton[chip="true"] {{
    background-color: {p.bg_hover};
    color: {p.text_secondary};
    border: 1px solid {p.border};
    border-radius: 4px;
    font-size: 11px;
    padding: 2px 4px;
    min-height: 16px;
}}
QPushButton[chip="true"]:hover {{
    background-color: {p.primary};
    color: #FFFFFF;
    border-color: {p.primary};
}}
/* 篩選鈕展開時保持高亮，讓人一眼看出面板是開著的 */
QPushButton[chip="true"]:checked {{
    background-color: {p.primary};
    color: #FFFFFF;
    border-color: {p.primary};
}}

/* ===== GroupBox 卡片 ===== */
QGroupBox {{
    background-color: {p.bg_panel};
    border: 1px solid {p.border};
    border-radius: 10px;
    margin-top: 12px;
    padding: 12px;
    padding-top: 20px;
    font-weight: bold;
    color: {p.text};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 5px;
    background-color: {p.bg_panel};
    color: {p.text};
}}

/* ===== 按鈕 ===== */
QPushButton {{
    background-color: {p.bg_panel};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 6px 12px;
    min-height: 18px;
}}
QPushButton:hover {{ background-color: {p.bg_hover}; }}
QPushButton:pressed {{ background-color: {p.bg_selected}; }}
QPushButton:disabled {{
    color: {p.text_disabled};
    background-color: {p.bg_window};
}}
QPushButton[compact="true"] {{ padding: 6px 4px; }}
QPushButton[role="primary"] {{
    background-color: {p.primary};
    color: #FFFFFF;
    border: none;
    font-weight: 600;
    padding: 8px 20px;
}}
QPushButton[role="primary"]:hover {{ background-color: {p.primary_hover}; }}
QPushButton[role="primary"]:disabled {{
    background-color: {p.border};
    color: {p.text_disabled};
}}

/* ===== 輸入控制：淺於卡片的深底 + 細框 + 聚焦主色 ===== */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {p.bg_input};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 5px 8px;
    min-height: 20px;
    selection-background-color: {p.primary};
    selection-color: #FFFFFF;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {p.text_disabled};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {p.border_focus};
    background-color: {p.bg_panel};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {p.text_disabled};
    background-color: {p.bg_window};
}}

QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    background-color: transparent;
    border: none;
    border-top-right-radius: 6px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    background-color: transparent;
    border: none;
    border-bottom-right-radius: 6px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: {p.bg_hover};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({arrow_up});
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({arrow_down});
    width: 10px;
    height: 10px;
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: url({arrow_down});
    width: 10px;
    height: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.bg_panel};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 6px;
    selection-background-color: {p.bg_selected};
    selection-color: {p.text};
    outline: none;
}}

/* ===== CheckBox / RadioButton：透明底 + 圖示指示 ===== */
QCheckBox {{
    color: {p.text};
    spacing: 8px;
    background-color: transparent;
}}
QCheckBox:disabled {{ color: {p.text_disabled}; }}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1.5px solid {p.text_disabled};
    border-radius: 4px;
    background-color: {p.bg_panel};
}}
QCheckBox::indicator:hover {{ border-color: {p.primary}; }}
QCheckBox::indicator:checked {{
    background-color: {p.bg_panel};
    image: url({check_url});
}}

QRadioButton {{
    color: {p.text};
    spacing: 8px;
    background-color: transparent;
}}
QRadioButton:disabled {{ color: {p.text_disabled}; }}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1.5px solid {p.text_disabled};
    border-radius: 9px;
    background-color: {p.bg_panel};
}}
QRadioButton::indicator:hover {{ border-color: {p.primary}; }}
QRadioButton::indicator:checked {{
    background-color: {p.bg_panel};
    image: url({dot_url});
}}

/* ===== 表格 ===== */
QTableWidget {{
    background-color: {p.bg_panel};
    alternate-background-color: {p.bg_input};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    gridline-color: transparent;
    outline: none;
}}
QTableWidget::item {{ padding: 2px 6px; }}
QTableWidget::item:selected {{
    background-color: {p.bg_selected};
    color: {p.text};
}}
QHeaderView::section {{
    background-color: {p.bg_panel};
    color: {p.text_secondary};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 6px 8px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background-color: {p.bg_panel};
    border: none;
}}

/* ===== Slider ===== */
QSlider {{
    background-color: transparent;
    min-height: 22px;
}}
QSlider::groove:horizontal {{
    height: 5px;
    background-color: {p.border};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background-color: {p.primary};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background-color: #FFFFFF;
    border: 1.5px solid {p.primary};
}}
QSlider::handle:horizontal:hover {{ background-color: {p.primary_light}; }}

/* ===== 進度條 ===== */
QProgressBar {{
    background-color: {p.bg_input};
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {p.primary};
    border-radius: 5px;
}}

/* ===== 捲軸 ===== */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_disabled}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ===== 其他 ===== */
QSplitter {{ background-color: {p.bg_window}; }}
QSplitter::handle {{ background-color: transparent; }}
QTextEdit, QTextBrowser {{
    background-color: {p.bg_panel};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
}}
QToolTip {{
    background-color: {p.bg_panel};
    color: {p.text};
    border: 1px solid {p.border};
    padding: 5px;
}}
QFrame[role="separator"] {{
    background-color: {p.border};
    max-height: 1px;
    border: none;
}}
"""
