"""
合圖畫布：把版面畫出來，並讓元件可以拖曳縮放／搬移

版面可以拆成多個輸出頁（特效一張、材質一張），畫布把所有頁**水平並排**
同時顯示；元件拖到另一頁的範圍內就是搬到那張圖。

互動約定（刻意不提供「刪除元件」——atlas 少一個區塊就是破圖）：

* 點一下選取，Ctrl 加選，在空白處拉框多選（可跨頁選）
* 拖曳角落控制點 = 等比縮放（只能等比：同一個區塊的 x/y 比例必須一致，
  拉成長方形會讓 Spine 算出來的頂點跑掉）；改了比例會取消該元件的固定；
  跨頁選取時不提供角落縮放（用右側的比例調整）
* 拖曳元件本體 = 搬移；**放開後不固定**（拖曳過程跟著滑鼠就夠了），
  拖到**另一頁**＝搬到那張圖。放下時壓到別的元件，上層會自動把對方排開——
  元件不可重疊是最嚴重的規定
* 方向鍵微調位置、Shift + 方向鍵一次 10px
* 滾輪縮放檢視、中鍵拖曳平移
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
_PAGE_GAP = 48        # 多頁並排時頁與頁之間的間隔（合圖像素）


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
    anchor: tuple[int, int] = (0, 0)  # resize 時固定不動的角（頁內座標）
    page_origin: tuple[int, int] = (0, 0)  # resize 時選取所在頁的全域原點
    moved: bool = False


class SheetCanvas(QWidget):
    """單一合圖的版面編輯畫布"""

    selection_changed = pyqtSignal()
    layout_changed = pyqtSignal(bool)   # 拖曳結束；True 代表尺寸變了（需要重新排版）
    # 拖曳「進行中」每一步都會發：右側的尺寸要邊拖邊更新，不能等放開才跳一次。
    # 只用來刷新讀數，不觸發重新排版，所以拖再快也不會卡。
    editing = pyqtSignal()
    # 一次拖曳（搬移／縮放）或微調「真的要改到版面」的那一刻發出，
    # 且在第一筆修改之前——上層在這裡拍快照，Ctrl+Z 才回得到拖曳前的狀態
    edit_started = pyqtSignal()
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

    # ------------------------------------------------------------ 多頁座標

    def _page_origin(self, index: int) -> tuple[int, int]:
        """頁面左上角在「全域合圖座標」的位置（多頁水平並排）"""
        layout = self._layout
        if layout is None:
            return 0, 0
        x = 0
        for i in range(min(index, layout.page_count)):
            x += max(1, layout.page_canvas(i)[0]) + _PAGE_GAP
        return x, 0

    def _union_size(self) -> tuple[int, int]:
        """所有頁並排後的整體尺寸（fit 與捲動範圍用）"""
        layout = self._layout
        if layout is None:
            return 0, 0
        width = height = 0
        for i in range(layout.page_count):
            w, h = layout.page_canvas(i)
            width += max(1, w) + (_PAGE_GAP if i else 0)
            height = max(height, h)
        return width, height

    def _global_rect(self, placement: Placement) -> tuple[int, int, int, int]:
        """元件在全域座標的矩形（頁內座標 + 該頁原點）"""
        ox, oy = self._page_origin(placement.page)
        x, y, w, h = placement.dst_rect
        return ox + x, oy + y, w, h

    def _page_at(self, sheet_x: float) -> int:
        """全域 x 座標落在哪一頁（頁與頁的空隙取較近的一頁）"""
        layout = self._layout
        if layout is None:
            return 0
        for i in range(layout.page_count):
            ox, _ = self._page_origin(i)
            width = max(1, layout.page_canvas(i)[0])
            if sheet_x < ox + width + _PAGE_GAP / 2:
                return i
        return layout.page_count - 1

    # ------------------------------------------------------------ 檢視

    def fit_to_view(self) -> None:
        layout = self._layout
        if layout is None or layout.canvas[0] <= 0 or layout.canvas[1] <= 0:
            return
        union_w, union_h = self._union_size()
        if union_w <= 0 or union_h <= 0:
            return
        margin = 24
        available_w = max(1, self.width() - margin * 2)
        available_h = max(1, self.height() - margin * 2)
        scale = min(available_w / union_w, available_h / union_h)
        self._view_scale = max(_MIN_VIEW_SCALE, min(_MAX_VIEW_SCALE, scale))
        content_w = union_w * self._view_scale
        content_h = union_h * self._view_scale
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
        """目前的檢視是否看得到整份版面（重排後畫布長大時用來判斷要不要重新置中）"""
        layout = self._layout
        if layout is None or layout.canvas[0] <= 0:
            return True
        rect = self._view_rect((0, 0, *self._union_size()))
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
            if self._view_rect(self._global_rect(placement)).contains(point):
                return placement
        return None

    def _handle_at(self, point: QPointF) -> str:
        """選取範圍的角落控制點（回傳 "" 代表沒碰到）"""
        frame = self._selection_frame()
        if frame is None:
            return ""
        (x, y, w, h), (ox, oy) = frame
        rect = self._view_rect((ox + x, oy + y, w, h))
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
        """
        選取範圍（**頁內**座標）。

        跨頁選取時回傳 None：角落縮放的錨點數學只在同一頁內成立，
        跨頁請改用右側的比例調整（各元件各自縮放後逐頁重排）。
        """
        placed = [p for p in self._selected if p.pos is not None]
        if not placed or len({p.page for p in placed}) != 1:
            return None
        rects = [p.dst_rect for p in placed]
        left = min(r[0] for r in rects)
        top = min(r[1] for r in rects)
        right = max(r[0] + r[2] for r in rects)
        bottom = max(r[1] + r[3] for r in rects)
        return left, top, right - left, bottom - top

    def _selection_frame(self) -> tuple[tuple[int, int, int, int], tuple[int, int]] | None:
        """（選取範圍（頁內）, 該頁的全域原點）；跨頁選取回傳 None"""
        bounds = self._selection_bounds()
        if bounds is None:
            return None
        page = next(p.page for p in self._selected if p.pos is not None)
        return bounds, self._page_origin(page)

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
            frame = self._selection_frame()
            assert frame is not None
            bounds, page_origin = frame
            self._drag = _Drag(
                mode="resize",
                origin=event.position().toPoint(),
                corner=corner,
                start_scales={id(p): p.scale for p in self._selected},
                start_positions={id(p): p.pos for p in self._selected if p.pos},  # type: ignore[misc]
                start_bounds=bounds,
                anchor=_anchor_of(bounds, corner),
                page_origin=page_origin,
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
            if not drag.moved and drag.mode in ("move", "resize"):
                self.edit_started.emit()   # 第一筆修改之前，讓上層拍復原快照
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
                    and self._view_rect(self._global_rect(p)).intersects(QRectF(rubber))
                ]
                ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self._selected = (self._selected + picked) if ctrl else picked
                self.selection_changed.emit()
            self.update()
            return

        if drag.moved and drag.mode == "move":
            crossed = self._resolve_move_drop(QPointF(event.position()))
            self.layout_changed.emit(crossed)
        elif drag.moved and drag.mode == "resize":
            self.layout_changed.emit(True)
        self.update()

    def _resolve_move_drop(self, cursor: QPointF) -> bool:
        """
        搬移放開：決定每個元件落在哪一頁。

        目標頁取「放開時滑鼠所在的頁」——多選一起拖時全部進同一頁，
        符合「把這幾個丟到那張圖」的直覺。放開一律**不固定**；
        跨頁的讓自動重排替新頁決定位置（空白新頁也因此開始縮排），
        留在原頁的維持拖曳位置（壓到別的元件時由上層自動排開）。

        Returns:
            是否有元件換了頁（呼叫端據此觸發重新排版）。
        """
        layout = self._layout
        if layout is None:
            return False
        sheet_x, _sheet_y = self._to_sheet(cursor)
        target = self._page_at(sheet_x)
        target_origin = self._page_origin(target)
        crossed = False
        for placement in self._selected:
            if placement.pos is None:
                continue
            gx, gy, w, h = self._global_rect(placement)
            if placement.page != target:
                canvas_w, canvas_h = layout.page_canvas(target)
                placement.page = target
                placement.pos = (
                    max(0, min(max(0, canvas_w - w), gx - target_origin[0])),
                    max(0, min(max(0, canvas_h - h), gy - target_origin[1])),
                )
                placement.pinned = False
                crossed = True
            else:
                # 拖曳中不夾邊界（要能拖出頁面去別頁），放開留在原頁才夾回來
                canvas_w, canvas_h = layout.page_canvas(placement.page)
                placement.pos = (
                    max(0, min(max(0, canvas_w - w), placement.pos[0])),
                    max(0, min(max(0, canvas_h - h), placement.pos[1])),
                )
        return crossed

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
            return  # 只是點一下選取（真實滑鼠按下就會伴隨 move 事件），不算搬移
        # 拖曳中不夾頁面邊界：元件要能被拖出頁面、放到另一頁；
        # 放開時 _resolve_move_drop 才決定落在哪一頁並夾回來。
        # 搬移**不**固定元件：拖曳過程跟著滑鼠就夠了，放開就只是「先放這裡」，
        # 之後的重排一樣可以重新安排它
        for placement in self._selected:
            start = drag.start_positions.get(id(placement))
            if start is None:
                continue
            placement.pos = (start[0] + dx, start[1] + dy)
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
        # 錨點與位置都是頁內座標，游標要先扣掉該頁的全域原點
        sheet_x -= drag.page_origin[0]
        sheet_y -= drag.page_origin[1]
        ratio = ((sheet_x - anchor_x) * diag_x + (sheet_y - anchor_y) * diag_y) / span
        ratio = max(0.02, min(20.0, ratio))

        for placement in self._selected:
            base = drag.start_scales.get(id(placement))
            if base is None:
                continue
            placement.set_scale(base * ratio)
            # 改了比例就取消固定：固定的位置是在舊比例下挑的，留著只會讓
            # 重排縮不下去（71 個全固定的版面就是這樣卡死的）
            placement.pinned = False
            # 多選時各自以錨點為基準等比移動，整組看起來就像一起縮放；
            # 放開後自動重排會重新決定位置
            start = (drag.start_positions or {}).get(id(placement))
            if start is not None and len(self._selected) > 1:
                placement.pos = (
                    max(0, anchor_x + round((start[0] - anchor_x) * ratio)),
                    max(0, anchor_y + round((start[1] - anchor_y) * ratio)),
                )
        self.editing.emit()
        self.update()

    def nudge(self, dx: int, dy: int) -> None:
        """方向鍵微調（不固定；只在元件自己那一頁內移動，壓到別人由上層排開）"""
        if self._layout is None or not self._selected or self._read_only:
            return
        self.edit_started.emit()
        for placement in self._selected:
            if placement.pos is None:
                continue
            canvas_w, canvas_h = self._layout.page_canvas(placement.page)
            w, h = placement.dst_size
            placement.pos = (
                max(0, min(max(0, canvas_w - w), placement.pos[0] + dx)),
                max(0, min(max(0, canvas_h - h), placement.pos[1] + dy)),
            )
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

        for index in range(layout.page_count):
            ox, oy = self._page_origin(index)
            page_w, page_h = layout.page_canvas(index)
            page_rect = self._view_rect((ox, oy, max(1, page_w), max(1, page_h)))
            _draw_checker(painter, page_rect)
            painter.setPen(QPen(QColor("#475569"), 1))
            painter.drawRect(page_rect)
            if layout.page_count > 1:
                # 多頁時標出每一頁的檔名與尺寸，才知道拖進去的是哪張圖
                painter.setPen(QColor("#94A3B8"))
                painter.drawText(
                    QPointF(page_rect.left(), page_rect.top() - 6),
                    f"{layout.page_name(index)}　{page_w}x{page_h}",
                )

        selected_ids = {id(p) for p in self._selected}
        for placement in layout.placements:
            if placement.pos is None:
                continue
            rect = self._view_rect(self._global_rect(placement))
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
        frame = self._selection_frame()
        if frame is None:
            return
        (x, y, w, h), (ox, oy) = frame
        rect = self._view_rect((ox + x, oy + y, w, h))
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
