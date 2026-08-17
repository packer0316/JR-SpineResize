"""
合圖畫布：把版面畫出來，並讓元件可以拖曳縮放／搬移

互動約定（刻意不提供「刪除元件」——atlas 少一個區塊就是破圖）：

* 點一下選取，Ctrl 加選，在空白處拉框多選
* 拖曳角落控制點 = 等比縮放（只能等比：同一個區塊的 x/y 比例必須一致，
  拉成長方形會讓 Spine 算出來的頂點跑掉）
* 拖曳元件本體 = 搬移，搬過的元件會被「固定」，自動排版時不再被移動
* 方向鍵微調位置、Shift + 方向鍵一次 10px
* 滾輪縮放檢視、中鍵或空白鍵拖曳平移
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PyQt6.QtWidgets import QWidget

from models.sheet_layout import Placement, SheetLayout
from utils.image_utils import to_rgba_array

_HANDLE = 7           # 控制點的邊長（畫面像素）
_MIN_VIEW_SCALE = 0.05
_MAX_VIEW_SCALE = 12.0


@dataclass
class _Drag:
    """進行中的拖曳"""

    mode: str                        # "move" / "resize" / "pan" / "rubber"
    origin: QPoint                   # 起點（畫面座標）
    corner: str = ""
    # 起始狀態（用來算增量，避免累積誤差）
    start_positions: dict[int, tuple[int, int]] | None = None
    start_scales: dict[int, float] | None = None
    start_offset: QPointF | None = None
    # 按下時的選取範圍：縮放比例一律以「這個」為基準算。
    # 若改用當下的範圍，元件變大會讓基準跟著變大 → 比例又變小 → 元件縮回去，
    # 滑鼠沒動也會在兩個尺寸之間來回跳（就是「大小不受控」的成因）。
    start_bounds: tuple[int, int, int, int] | None = None
    anchor: tuple[int, int] = (0, 0)  # resize 時固定不動的角
    moved: bool = False


class SheetCanvas(QWidget):
    """單一合圖的版面編輯畫布"""

    selection_changed = pyqtSignal()
    layout_changed = pyqtSignal(bool)   # 拖曳結束；True 代表尺寸變了（需要重新排版）
    # 拖曳「進行中」每一步都會發：右側的尺寸要邊拖邊更新，不能等放開才跳一次。
    # 只用來刷新讀數，不觸發重新排版，所以拖再快也不會卡。
    editing = pyqtSignal()
    zoom_changed = pyqtSignal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._layout: SheetLayout | None = None
        self._source: QImage | None = None
        self._crops: dict[tuple[int, int, int, int], QPixmap] = {}
        self._selected: list[Placement] = []
        self._drag: _Drag | None = None
        self._rubber: QRect | None = None
        self._overlapping: set[int] = set()   # id(placement)

        self._view_scale = 1.0
        self._offset = QPointF(0.0, 0.0)     # 畫布原點在畫面上的位置
        self._fit_pending = True
        self._show_names = False
        # 清單多選合圖時畫布只供檢視：批次動作以合圖為單位，
        # 這時還讓人拖元件會以為改到全部選取，實際只動到顯示中的那張
        self._read_only = False

    # ------------------------------------------------------------ 載入

    def set_sheet(self, layout: SheetLayout | None, source: Image.Image | None) -> None:
        """換一張合圖（source 是原始貼圖，用來畫元件內容）"""
        self._layout = layout
        self._crops.clear()
        self._selected = []
        self._drag = None
        self._overlapping = set()
        self._source = _to_qimage(source) if source is not None else None
        self._fit_pending = True
        self.selection_changed.emit()
        self.update()

    @property
    def layout(self) -> SheetLayout | None:
        return self._layout

    def selected(self) -> list[Placement]:
        return list(self._selected)

    def select(self, placements: list[Placement]) -> None:
        self._selected = list(placements)
        self.selection_changed.emit()
        self.update()

    def select_all(self) -> None:
        if self._layout is not None and not self._read_only:
            self.select(list(self._layout.placements))

    def set_read_only(self, read_only: bool) -> None:
        """只供檢視：平移與縮放照常，選取與編輯全部關閉"""
        if read_only == self._read_only:
            return
        self._read_only = read_only
        if read_only:
            self._drag = None
            self._rubber = None
            if self._selected:
                self._selected = []
                self.selection_changed.emit()
        self.update()

    def unpin_all(self) -> int:
        """取消所有元件的位置固定；回傳原本被固定的數量"""
        if self._layout is None:
            return 0
        pinned = [p for p in self._layout.placements if p.pinned]
        for placement in pinned:
            placement.pinned = False
        return len(pinned)

    def set_show_names(self, enabled: bool) -> None:
        self._show_names = enabled
        self.update()

    def set_overlapping(self, placements: list[Placement]) -> None:
        """標紅重疊的元件（固定位置的元件互相壓到時）"""
        self._overlapping = {id(p) for p in placements}
        self.update()

    # ------------------------------------------------------------ 檢視

    def fit_to_view(self) -> None:
        layout = self._layout
        if layout is None or layout.canvas[0] <= 0 or layout.canvas[1] <= 0:
            return
        margin = 24
        available_w = max(1, self.width() - margin * 2)
        available_h = max(1, self.height() - margin * 2)
        scale = min(available_w / layout.canvas[0], available_h / layout.canvas[1])
        self._view_scale = max(_MIN_VIEW_SCALE, min(_MAX_VIEW_SCALE, scale))
        content_w = layout.canvas[0] * self._view_scale
        content_h = layout.canvas[1] * self._view_scale
        self._offset = QPointF(
            (self.width() - content_w) / 2, (self.height() - content_h) / 2
        )
        self._fit_pending = False
        self.zoom_changed.emit(self._view_scale)
        self.update()

    @property
    def view_scale(self) -> float:
        return self._view_scale

    def canvas_fits(self) -> bool:
        """目前的檢視是否看得到整張合圖（重排後畫布長大時用來判斷要不要重新置中）"""
        layout = self._layout
        if layout is None or layout.canvas[0] <= 0:
            return True
        rect = self._view_rect((0, 0, *layout.canvas))
        return self.rect().contains(rect.toRect())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_pending:
            self.fit_to_view()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        factor = 1.15 ** steps
        target = max(_MIN_VIEW_SCALE, min(_MAX_VIEW_SCALE, self._view_scale * factor))
        if target == self._view_scale:
            return
        # 以滑鼠位置為中心縮放
        cursor = QPointF(event.position())
        before = (cursor - self._offset) / self._view_scale
        self._view_scale = target
        self._offset = cursor - before * self._view_scale
        self.zoom_changed.emit(self._view_scale)
        self.update()

    # ------------------------------------------------------------ 座標換算

    def _to_view(self, x: float, y: float) -> QPointF:
        return QPointF(self._offset.x() + x * self._view_scale,
                       self._offset.y() + y * self._view_scale)

    def _to_sheet(self, point: QPointF | QPoint) -> tuple[float, float]:
        p = QPointF(point)
        return (
            (p.x() - self._offset.x()) / self._view_scale,
            (p.y() - self._offset.y()) / self._view_scale,
        )

    def _view_rect(self, rect: tuple[int, int, int, int]) -> QRectF:
        x, y, w, h = rect
        top_left = self._to_view(x, y)
        return QRectF(top_left.x(), top_left.y(), w * self._view_scale, h * self._view_scale)

    # ------------------------------------------------------------ 命中測試

    def _at(self, point: QPointF) -> Placement | None:
        """畫面座標下的元件（由上層往下找，後畫的優先）"""
        if self._layout is None:
            return None
        for placement in reversed(self._layout.placements):
            if placement.pos is None:
                continue
            if self._view_rect(placement.dst_rect).contains(point):
                return placement
        return None

    def _handle_at(self, point: QPointF) -> str:
        """選取範圍的角落控制點（回傳 "" 代表沒碰到）"""
        bounds = self._selection_bounds()
        if bounds is None:
            return ""
        x, y, w, h = bounds
        rect = self._view_rect((x, y, w, h))
        spots = {
            "tl": rect.topLeft(),
            "tr": rect.topRight(),
            "bl": rect.bottomLeft(),
            "br": rect.bottomRight(),
        }
        for name, centre in spots.items():
            box = QRectF(centre.x() - _HANDLE, centre.y() - _HANDLE, _HANDLE * 2, _HANDLE * 2)
            if box.contains(point):
                return name
        return ""

    def _selection_bounds(self) -> tuple[int, int, int, int] | None:
        rects = [p.dst_rect for p in self._selected if p.pos is not None]
        if not rects:
            return None
        left = min(r[0] for r in rects)
        top = min(r[1] for r in rects)
        right = max(r[0] + r[2] for r in rects)
        bottom = max(r[1] + r[3] for r in rects)
        return left, top, right - left, bottom - top

    # ------------------------------------------------------------ 滑鼠

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._layout is None:
            return
        position = QPointF(event.position())

        if event.button() == Qt.MouseButton.MiddleButton:
            self._drag = _Drag(mode="pan", origin=event.position().toPoint(),
                               start_offset=QPointF(self._offset))
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return

        if event.button() != Qt.MouseButton.LeftButton or self._read_only:
            return

        corner = self._handle_at(position)
        if corner:
            bounds = self._selection_bounds()
            assert bounds is not None
            self._drag = _Drag(
                mode="resize",
                origin=event.position().toPoint(),
                corner=corner,
                start_scales={id(p): p.scale for p in self._selected},
                start_positions={id(p): p.pos for p in self._selected if p.pos},  # type: ignore[misc]
                start_bounds=bounds,
                anchor=_anchor_of(bounds, corner),
            )
            return

        hit = self._at(position)
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if hit is None:
            if not ctrl:
                self._selected = []
                self.selection_changed.emit()
            self._drag = _Drag(mode="rubber", origin=event.position().toPoint())
            self._rubber = QRect(self._drag.origin, self._drag.origin)
            self.update()
            return

        if ctrl:
            if any(p is hit for p in self._selected):
                self._selected = [p for p in self._selected if p is not hit]
            else:
                self._selected.append(hit)
            self.selection_changed.emit()
        elif not any(p is hit for p in self._selected):
            self._selected = [hit]
            self.selection_changed.emit()

        self._drag = _Drag(
            mode="move",
            origin=event.position().toPoint(),
            start_positions={id(p): p.pos for p in self._selected if p.pos},  # type: ignore[misc]
        )
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        position = QPointF(event.position())
        drag = self._drag
        if drag is None:
            self._update_cursor(position)
            return

        delta = event.position().toPoint() - drag.origin
        if abs(delta.x()) > 1 or abs(delta.y()) > 1:
            drag.moved = True

        if drag.mode == "pan" and drag.start_offset is not None:
            self._offset = drag.start_offset + QPointF(delta)
            self.update()
            return

        if drag.mode == "rubber":
            self._rubber = QRect(drag.origin, event.position().toPoint()).normalized()
            self.update()
            return

        if drag.mode == "move":
            self._apply_move(delta, drag)
            return

        if drag.mode == "resize":
            self._apply_resize(position, drag)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        drag = self._drag
        self._drag = None
        self.unsetCursor()
        if drag is None:
            return

        if drag.mode == "rubber":
            rubber, self._rubber = self._rubber, None
            if rubber is not None and self._layout is not None and drag.moved:
                picked = [
                    p for p in self._layout.placements
                    if p.pos is not None
                    and self._view_rect(p.dst_rect).intersects(QRectF(rubber))
                ]
                ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self._selected = (self._selected + picked) if ctrl else picked
                self.selection_changed.emit()
            self.update()
            return

        if drag.moved and drag.mode in ("move", "resize"):
            self.layout_changed.emit(drag.mode == "resize")
        self.update()

    def _update_cursor(self, position: QPointF) -> None:
        if self._read_only:
            self.unsetCursor()
            return
        corner = self._handle_at(position)
        if corner in ("tl", "br"):
            self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
        elif corner in ("tr", "bl"):
            self.setCursor(QCursor(Qt.CursorShape.SizeBDiagCursor))
        elif self._at(position) is not None:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        else:
            self.unsetCursor()

    # ------------------------------------------------------------ 編輯動作

    def _apply_move(self, delta: QPoint, drag: _Drag) -> None:
        if self._layout is None or not drag.start_positions:
            return
        dx = round(delta.x() / self._view_scale)
        dy = round(delta.y() / self._view_scale)
        if dx == 0 and dy == 0:
            # 只是點一下選取（真實滑鼠按下就會伴隨 move 事件），位置沒變就不能
            # 把元件標成「固定」——被固定的元件不參與自動排版，累積幾個之後
            # 版面就縮不下去了，使用者完全不知道自己何時固定過。
            return
        canvas_w, canvas_h = self._layout.canvas
        for placement in self._selected:
            start = drag.start_positions.get(id(placement))
            if start is None:
                continue
            w, h = placement.dst_size
            placement.pos = (
                max(0, min(canvas_w - w, start[0] + dx)),
                max(0, min(canvas_h - h, start[1] + dy)),
            )
            placement.pinned = True
        self.editing.emit()
        self.update()

    def _apply_resize(self, position: QPointF, drag: _Drag) -> None:
        """
        以固定角為錨點等比縮放選取的元件。

        比例＝把「滑鼠相對錨點的位移」投影到「按下時那條對角線」上的長度比。
        兩個關鍵：

        * 基準一律用 ``drag.start_bounds``（按下時的範圍），不是當下的範圍。
          用當下的範圍會形成回饋迴路：元件變大 → 基準變大 → 比例變小 →
          元件縮回去，滑鼠不動也會一直跳。
        * 投影而不是「兩軸取較大的變化量」：後者會在斜拖時於 x/y 之間切換，
          每次切換就跳一下。投影是連續的，而且等比縮放時控制點本來就只能
          沿著對角線走，投影點正是對角線上離滑鼠最近的位置。
        """
        if self._layout is None or not drag.start_scales or drag.start_bounds is None:
            return
        anchor_x, anchor_y = drag.anchor
        corner_x, corner_y = _corner_of(drag.start_bounds, drag.corner)
        diag_x, diag_y = corner_x - anchor_x, corner_y - anchor_y
        span = diag_x * diag_x + diag_y * diag_y
        if span <= 0:
            return

        sheet_x, sheet_y = self._to_sheet(position)
        ratio = ((sheet_x - anchor_x) * diag_x + (sheet_y - anchor_y) * diag_y) / span
        ratio = max(0.02, min(20.0, ratio))

        for placement in self._selected:
            base = drag.start_scales.get(id(placement))
            if base is None:
                continue
            placement.set_scale(base * ratio)
            # 多選時各自以錨點為基準等比移動，整組看起來就像一起縮放
            start = (drag.start_positions or {}).get(id(placement))
            if start is not None and len(self._selected) > 1:
                placement.pos = (
                    max(0, anchor_x + round((start[0] - anchor_x) * ratio)),
                    max(0, anchor_y + round((start[1] - anchor_y) * ratio)),
                )
                placement.pinned = True
        self.editing.emit()
        self.update()

    def nudge(self, dx: int, dy: int) -> None:
        """方向鍵微調（會把元件固定住）"""
        if self._layout is None or not self._selected or self._read_only:
            return
        canvas_w, canvas_h = self._layout.canvas
        for placement in self._selected:
            if placement.pos is None:
                continue
            w, h = placement.dst_size
            placement.pos = (
                max(0, min(canvas_w - w, placement.pos[0] + dx)),
                max(0, min(canvas_h - h, placement.pos[1] + dy)),
            )
            placement.pinned = True
        self.layout_changed.emit(False)
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        moves = {
            Qt.Key.Key_Left: (-step, 0),
            Qt.Key.Key_Right: (step, 0),
            Qt.Key.Key_Up: (0, -step),
            Qt.Key.Key_Down: (0, step),
        }
        if event.key() in moves:
            self.nudge(*moves[event.key()])
            return
        if event.key() == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 繪製

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#141B2D"))

        layout = self._layout
        if layout is None or layout.canvas[0] <= 0:
            painter.setPen(QColor("#94A3B8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "沒有可編輯的合圖")
            return

        canvas_rect = self._view_rect((0, 0, *layout.canvas))
        _draw_checker(painter, canvas_rect)
        painter.setPen(QPen(QColor("#475569"), 1))
        painter.drawRect(canvas_rect)

        selected_ids = {id(p) for p in self._selected}
        for placement in layout.placements:
            if placement.pos is None:
                continue
            rect = self._view_rect(placement.dst_rect)
            pixmap = self._crop(placement.src_rect)
            if pixmap is not None:
                painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))

            if id(placement) in self._overlapping:
                pen = QPen(QColor("#F87171"), 2)
            elif id(placement) in selected_ids:
                pen = QPen(QColor("#818CF8"), 2)
            elif placement.pinned:
                pen = QPen(QColor("#FBBF24"), 1)
            else:
                pen = QPen(QColor(255, 255, 255, 40), 1)
            painter.setPen(pen)
            painter.drawRect(rect)

            if self._show_names and rect.width() > 28 and rect.height() > 12:
                painter.setPen(QColor(255, 255, 255, 190))
                painter.drawText(
                    rect.adjusted(2, 1, -2, -1),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    placement.label,
                )

        self._draw_handles(painter)

        if self._rubber is not None:
            painter.setPen(QPen(QColor("#818CF8"), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(129, 140, 248, 40))
            painter.drawRect(QRectF(self._rubber))

    def _draw_handles(self, painter: QPainter) -> None:
        bounds = self._selection_bounds()
        if bounds is None:
            return
        rect = self._view_rect(bounds)
        painter.setPen(QPen(QColor("#818CF8"), 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        painter.setPen(QPen(QColor("#0F172A"), 1))
        painter.setBrush(QColor("#818CF8"))
        for centre in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
            painter.drawRect(
                QRectF(centre.x() - _HANDLE / 2, centre.y() - _HANDLE / 2, _HANDLE, _HANDLE)
            )

    def _crop(self, src_rect: tuple[int, int, int, int]) -> QPixmap | None:
        """取（並快取）元件在原始貼圖上的內容"""
        if self._source is None:
            return None
        cached = self._crops.get(src_rect)
        if cached is not None:
            return cached
        x, y, w, h = src_rect
        x0, y0 = max(0, x), max(0, y)
        x1 = min(self._source.width(), x + w)
        y1 = min(self._source.height(), y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        pixmap = QPixmap.fromImage(self._source.copy(QRect(x0, y0, x1 - x0, y1 - y0)))
        self._crops[src_rect] = pixmap
        return pixmap


# ---------------------------------------------------------------- 輔助


def _anchor_of(bounds: tuple[int, int, int, int], corner: str) -> tuple[int, int]:
    """拖某個角時，對面的角就是固定不動的錨點"""
    x, y, w, h = bounds
    return {
        "tl": (x + w, y + h),
        "tr": (x, y + h),
        "bl": (x + w, y),
        "br": (x, y),
    }[corner]


def _corner_of(bounds: tuple[int, int, int, int], corner: str) -> tuple[int, int]:
    x, y, w, h = bounds
    return {
        "tl": (x, y),
        "tr": (x + w, y),
        "bl": (x, y + h),
        "br": (x + w, y + h),
    }[corner]


def _to_qimage(image: Image.Image) -> QImage:
    arr = np.ascontiguousarray(to_rgba_array(image))
    height, width = arr.shape[:2]
    qimage = QImage(arr.data, width, height, width * 4, QImage.Format.Format_RGBA8888)
    return qimage.copy()  # 脫離 numpy buffer 的生命週期


def _draw_checker(painter: QPainter, rect: QRectF, cell: int = 8) -> None:
    """透明底的棋盤格（看得出元件邊界在哪）"""
    painter.save()
    painter.setClipRect(rect)
    painter.fillRect(rect, QColor("#1E293B"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#243044"))
    top = int(rect.top())
    left = int(rect.left())
    for row in range(0, int(rect.height()) + cell, cell):
        for col in range(0, int(rect.width()) + cell, cell):
            if (row // cell + col // cell) % 2:
                painter.drawRect(QRectF(left + col, top + row, cell, cell))
    painter.restore()
