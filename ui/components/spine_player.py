"""
Spine 播放器元件

以內建 3.8 runtime 播放骨架動畫。支援：
* 動畫選單（含 Setup Pose）與 skin 選單（多 skin 時）
* 播放/暫停、時間軸拖曳
* 「原始 / 縮放後」貼圖切換——同一副骨架換貼圖庫，直接對比縮放品質
* 滾輪縮放、拖曳平移、自動取景
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QTransform
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.exceptions import SkeletonParseError
from core.spine.binary_parser import parse_skel
from core.spine.qt_renderer import SkeletonRenderer
from core.spine.runtime import Skeleton
from core.spine.texture_store import AtlasTextureStore
from models.atlas_data import AtlasFile
from ui.styles.indicators import pause_icon, play_icon

_FPS = 30
_SETUP_POSE = "（Setup Pose）"

# 深底視圖上的中性圖示色，兩種主題皆可讀
_ICON_COLOUR = "#8A93A5"

# 格線與座標軸配色（視圖底色固定深灰，不隨主題）
_GRID_COLOUR = QColor(56, 60, 70)
_AXIS_X_COLOUR = QColor(158, 96, 96)   # X 軸（y = 0）
_AXIS_Y_COLOUR = QColor(96, 150, 96)   # Y 軸（x = 0）


class _Viewport(QWidget):
    """實際繪製區：處理縮放、平移與每影格渲染"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.skeleton: Skeleton | None = None
        self.renderer: SkeletonRenderer | None = None
        self.message = "選擇左側專案以預覽"
        self.zoom = 1.0
        self.center_x = 0.0
        self.center_y = 0.0
        self.show_grid = True
        self._fitted = False
        self._drag_start = None
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(False)

    # ------------------------------------------------------------ 視圖

    def view_transform(self) -> QTransform:
        t = QTransform()
        t.translate(self.width() / 2, self.height() / 2)
        t.scale(self.zoom, -self.zoom)
        t.translate(-self.center_x, -self.center_y)
        return t

    def fit(self) -> None:
        if self.renderer is None or self.renderer.last_bounds is None:
            self._fitted = False
            self.update()
            return
        x0, y0, x1, y1 = self.renderer.last_bounds
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return
        self.center_x = (x0 + x1) / 2
        self.center_y = (y0 + y1) / 2
        self.zoom = min(self.width() / w, self.height() / h) * 0.85
        self._fitted = True
        self.update()

    # ------------------------------------------------------------ 事件

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(43, 45, 52))
        if self.skeleton is None or self.renderer is None:
            painter.setPen(QColor(150, 155, 165))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            painter.end()
            return
        if self.show_grid:
            self._draw_grid(painter)
        painter.setTransform(self.view_transform())
        self.renderer.render(painter, self.skeleton)
        painter.end()
        if not self._fitted and self.renderer.last_bounds is not None:
            self.fit()

    def _draw_grid(self, painter: QPainter) -> None:
        """世界座標格線 + X/Y 軸。以螢幕座標畫線，維持 1px 銳利。"""
        half_w = (self.width() / 2) / self.zoom
        half_h = (self.height() / 2) / self.zoom
        world_x0 = self.center_x - half_w
        world_x1 = self.center_x + half_w
        world_y0 = self.center_y - half_h
        world_y1 = self.center_y + half_h

        # 間距取 1/2/5 x 10^n，讓螢幕上約 60~150px 一格
        target = 60.0 / self.zoom
        exponent = math.floor(math.log10(max(target, 1e-9)))
        step = 10.0 ** exponent
        for mult in (1.0, 2.0, 5.0, 10.0):
            if 10.0 ** exponent * mult >= target:
                step = 10.0 ** exponent * mult
                break

        view = self.view_transform()

        def draw_world_line(wx0: float, wy0: float, wx1: float, wy1: float) -> None:
            painter.drawLine(view.map(QPointF(wx0, wy0)), view.map(QPointF(wx1, wy1)))

        painter.setPen(QPen(_GRID_COLOUR, 1))
        for i in range(math.floor(world_x0 / step), math.ceil(world_x1 / step) + 1):
            if i == 0:
                continue  # 軸線最後以強調色再畫
            draw_world_line(i * step, world_y0, i * step, world_y1)
        for j in range(math.floor(world_y0 / step), math.ceil(world_y1 / step) + 1):
            if j == 0:
                continue
            draw_world_line(world_x0, j * step, world_x1, j * step)

        if world_y0 <= 0 <= world_y1:
            painter.setPen(QPen(_AXIS_X_COLOUR, 1))
            draw_world_line(world_x0, 0, world_x1, 0)
        if world_x0 <= 0 <= world_x1:
            painter.setPen(QPen(_AXIS_Y_COLOUR, 1))
            draw_world_line(0, world_y0, 0, world_y1)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.skeleton is None:
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom = max(0.02, min(50.0, self.zoom * factor))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = (event.position(), self.center_x, self.center_y)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_start is not None and self.skeleton is not None:
            start, cx, cy = self._drag_start
            delta = event.position() - start
            self.center_x = cx - delta.x() / self.zoom
            self.center_y = cy + delta.y() / self.zoom
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_start = None


class SpinePlayer(QWidget):
    """完整播放器（工具列 + 繪製區）"""

    error = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._animations = []
        self._current_animation = None
        self._time = 0.0
        self._playing = True
        self._last_tick = time.perf_counter()
        self._stores: dict[str, AtlasTextureStore] = {}
        self._active_store_key = "original"
        self._slider_dragging = False

        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / _FPS))
        self._timer.timeout.connect(self._tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ---- 工具列
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.animation_combo = QComboBox()
        self.animation_combo.currentIndexChanged.connect(self._on_animation_changed)
        bar.addWidget(QLabel("動畫"))
        bar.addWidget(self.animation_combo, 2)

        self.skin_combo = QComboBox()
        self.skin_combo.currentIndexChanged.connect(self._on_skin_changed)
        self.skin_label = QLabel("Skin")
        bar.addWidget(self.skin_label)
        bar.addWidget(self.skin_combo, 1)

        self._icon_play = QIcon(play_icon(_ICON_COLOUR))
        self._icon_pause = QIcon(pause_icon(_ICON_COLOUR))
        self.play_button = QPushButton()
        self.play_button.setIcon(self._icon_pause)
        self.play_button.setFixedWidth(36)
        self.play_button.setToolTip("播放 / 暫停")
        self.play_button.clicked.connect(self.toggle_play)
        bar.addWidget(self.play_button)

        self.source_button = QPushButton("原始")
        self.source_button.setCheckable(False)
        self.source_button.setFixedWidth(88)
        self.source_button.setToolTip("尚未套用縮放設定")
        self.source_button.setEnabled(False)
        self.source_button.clicked.connect(self._toggle_source)
        bar.addWidget(self.source_button)

        fit_button = QPushButton("取景")
        fit_button.setFixedWidth(52)
        fit_button.clicked.connect(lambda: self.viewport.fit())
        bar.addWidget(fit_button)

        self.grid_check = QCheckBox("格線")
        self.grid_check.setChecked(True)
        self.grid_check.setToolTip("顯示座標軸與格線")
        self.grid_check.toggled.connect(self._on_grid_toggled)
        bar.addWidget(self.grid_check)
        layout.addLayout(bar)

        # ---- 繪製區
        self.viewport = _Viewport()
        layout.addWidget(self.viewport, 1)

        # ---- 時間軸
        slider_row = QHBoxLayout()
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setRange(0, 1000)
        self.time_slider.sliderPressed.connect(lambda: setattr(self, "_slider_dragging", True))
        self.time_slider.sliderReleased.connect(lambda: setattr(self, "_slider_dragging", False))
        self.time_slider.valueChanged.connect(self._on_slider)
        self.time_label = QLabel("0.00 / 0.00")
        self.time_label.setProperty("role", "hint")
        self.time_label.setFixedWidth(96)
        slider_row.addWidget(self.time_slider, 1)
        slider_row.addWidget(self.time_label)
        layout.addLayout(slider_row)

    # ------------------------------------------------------------ 載入

    def clear(self, message: str = "選擇左側專案以預覽") -> None:
        self._timer.stop()
        self.viewport.skeleton = None
        self.viewport.renderer = None
        self.viewport.message = message
        self.viewport._fitted = False
        self._stores.clear()
        self._animations = []
        self._current_animation = None
        self.animation_combo.blockSignals(True)
        self.animation_combo.clear()
        self.animation_combo.blockSignals(False)
        self.skin_combo.blockSignals(True)
        self.skin_combo.clear()
        self.skin_combo.blockSignals(False)
        self.skin_label.setVisible(False)
        self.skin_combo.setVisible(False)
        self.set_scaled_store(None, "")
        self.viewport.update()

    def load(self, skel_path: Path, atlas: AtlasFile, pages: dict[str, Image.Image]) -> bool:
        """載入骨架與原始貼圖。回傳是否成功。"""
        self.clear()
        try:
            data = parse_skel(skel_path)
        except (SkeletonParseError, Exception) as exc:  # noqa: BLE001
            self.viewport.message = f"無法播放：{exc}"
            self.viewport.update()
            self.error.emit(str(exc))
            return False

        skeleton = Skeleton(data)
        store = AtlasTextureStore(atlas, pages)
        self._stores = {"original": store}
        self._active_store_key = "original"
        self.source_button.setText("原始")

        self.viewport.skeleton = skeleton
        self.viewport.renderer = SkeletonRenderer(store)
        self.viewport._fitted = False

        self._animations = list(data.animations)
        self.animation_combo.blockSignals(True)
        self.animation_combo.addItem(_SETUP_POSE, None)
        for animation in self._animations:
            self.animation_combo.addItem(f"{animation.name}（{animation.duration:.2f}s）", animation)
        # 預設選第一個動畫
        self.animation_combo.setCurrentIndex(1 if self._animations else 0)
        self.animation_combo.blockSignals(False)

        named_skins = [s for s in data.skins if s.name != "default"]
        if named_skins:
            self.skin_combo.blockSignals(True)
            self.skin_combo.addItem("default", None)
            for skin in named_skins:
                self.skin_combo.addItem(skin.name, skin.name)
            self.skin_combo.blockSignals(False)
            self.skin_label.setVisible(True)
            self.skin_combo.setVisible(True)

        self._current_animation = self._animations[0] if self._animations else None
        self._time = 0.0
        self._playing = True
        self.play_button.setIcon(self._icon_pause)
        self._last_tick = time.perf_counter()
        self._timer.start()
        self._apply_pose()
        return True

    def set_scaled_store(self, store: AtlasTextureStore | None, label: str) -> None:
        """套用後由外部提供縮放後貼圖庫。None = 清除。"""
        if store is None:
            self._stores.pop("scaled", None)
            if self._active_store_key == "scaled":
                self._activate_store("original")
            self.source_button.setEnabled(False)
            self.source_button.setText("原始")
            self.source_button.setToolTip("尚未套用縮放設定")
            return
        self._stores["scaled"] = store
        self.source_button.setEnabled(True)
        self.source_button.setToolTip(f"切換原始 / {label} 貼圖")
        self._activate_store("scaled")

    def _activate_store(self, key: str) -> None:
        store = self._stores.get(key)
        if store is None or self.viewport.renderer is None:
            return
        self._active_store_key = key
        self.viewport.renderer.textures = store
        self.source_button.setText("縮放後" if key == "scaled" else "原始")
        self.viewport.update()

    def _toggle_source(self) -> None:
        self._activate_store("original" if self._active_store_key == "scaled" else "scaled")

    # ------------------------------------------------------------ 播放

    def toggle_play(self) -> None:
        self._playing = not self._playing
        self.play_button.setIcon(self._icon_pause if self._playing else self._icon_play)
        self._last_tick = time.perf_counter()

    def _on_grid_toggled(self, checked: bool) -> None:
        self.viewport.show_grid = checked
        self.viewport.update()

    def _duration(self) -> float:
        return self._current_animation.duration if self._current_animation else 0.0

    def _tick(self) -> None:
        if self.viewport.skeleton is None:
            return
        now = time.perf_counter()
        dt = now - self._last_tick
        self._last_tick = now
        if self._playing and not self._slider_dragging and self._duration() > 0:
            self._time = (self._time + dt) % self._duration()
        self._apply_pose()

    def _apply_pose(self) -> None:
        skeleton = self.viewport.skeleton
        if skeleton is None:
            return
        skeleton.set_to_setup_pose()
        if self._current_animation is not None:
            self._current_animation.apply(skeleton, self._time)
        skeleton.update_world_transform()
        self.viewport.update()
        duration = self._duration()
        self.time_label.setText(f"{self._time:.2f} / {duration:.2f}")
        if not self._slider_dragging:
            self.time_slider.blockSignals(True)
            self.time_slider.setValue(int(self._time / duration * 1000) if duration > 0 else 0)
            self.time_slider.blockSignals(False)

    def _on_slider(self, value: int) -> None:
        if not self._slider_dragging:
            return
        duration = self._duration()
        if duration > 0:
            self._time = value / 1000 * duration
            self._apply_pose()

    def _on_animation_changed(self) -> None:
        self._current_animation = self.animation_combo.currentData()
        self._time = 0.0
        self.viewport._fitted = False
        self._apply_pose()

    def _on_skin_changed(self) -> None:
        skeleton = self.viewport.skeleton
        if skeleton is None:
            return
        skeleton.set_skin(self.skin_combo.currentData())
        skeleton.set_to_setup_pose()
        self._apply_pose()

    def stop(self) -> None:
        self._timer.stop()

    def hideEvent(self, event) -> None:  # noqa: N802 - 隱藏時停止耗 CPU
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        if self.viewport.skeleton is not None:
            self._last_tick = time.perf_counter()
            self._timer.start()
        super().showEvent(event)
