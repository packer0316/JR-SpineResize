"""
合圖編輯器的互動驗證 — 不需要任何素材，自己造一份合圖

用法：
    py -3 tests/verify_editor.py

驗的是編輯器的行為必須是**可預測**的：

1. 滑鼠停在同一個位置，元件大小永遠一樣
2. 沿同一個方向拖，大小單調變化（不會忽大忽小）
3. 拖遠再拖回同一點，回到完全一樣的大小
4. 任何時候都保持等比（同一元件的 x/y 比例一致，否則 Spine 頂點會跑掉）
5. 多選一起拖時每個元件都同步等比
6. 排版後沒有元件重疊
7. 只點開來看不算「自訂」——沒動過就不會被套用
8. 「還原原始版面」真的回到原檔：atlas byte-identical、貼圖像素不變
9. 點一下選取**不會**把元件固定；改整組比例會取消固定並排到最小

第 1 ~ 3 點是為了鎖住一個修過的 bug：縮放比例原本以「當下的選取範圍」為基準，
元件變大會讓基準跟著變大 → 比例變小 → 元件縮回去，滑鼠沒動也會在兩個尺寸
之間來回跳。現在一律以「按下滑鼠那一刻的範圍」為基準，並把位移投影到當時的
對角線上，所以位置與大小是一對一的關係。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pipeline import process_asset
from core.project_scanner import scan_projects
from core.sheet_group import build_sheet_groups
from models.process_options import ProcessOptions
from models.sheet_layout import LayoutStore
from ui.dialogs.sheet_editor import SheetEditorDialog

COLS, ROWS, BLOCK, GAP = 4, 3, 80, 6
PAGE_W = COLS * (BLOCK + GAP) + GAP
PAGE_H = ROWS * (BLOCK + GAP) + GAP


def build_sheet(folder: Path) -> None:
    """造一張 12 個色塊的合圖（未裁切，方便看出等比是否被破壞）"""
    image = Image.new("RGBA", (PAGE_W, PAGE_H), (0, 0, 0, 0))
    lines = ["", "sheet.png", f"size: {PAGE_W},{PAGE_H}",
             "format: RGBA8888", "filter: Linear,Linear", "repeat: none"]
    for index in range(COLS * ROWS):
        col, row = index % COLS, index // COLS
        x, y = GAP + col * (BLOCK + GAP), GAP + row * (BLOCK + GAP)
        image.paste(Image.new("RGBA", (BLOCK, BLOCK), (30 + index * 15, 90, 200, 255)), (x, y))
        lines += [f"block_{index}", "  rotate: false", f"  xy: {x}, {y}",
                  f"  size: {BLOCK}, {BLOCK}", f"  orig: {BLOCK}, {BLOCK}",
                  "  offset: 0, 0", "  index: -1"]
    image.save(folder / "sheet.png")
    (folder / "sheet.atlas").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------ 假滑鼠事件


def _event(kind, pos, button, buttons):
    return QMouseEvent(kind, QPointF(pos), QPointF(pos), button, buttons,
                       Qt.KeyboardModifier.NoModifier)


def press(canvas, pos) -> None:
    canvas.mousePressEvent(_event(
        QMouseEvent.Type.MouseButtonPress, pos,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))


def move(canvas, pos) -> None:
    canvas.mouseMoveEvent(_event(
        QMouseEvent.Type.MouseMove, pos,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))


def release(canvas, pos) -> None:
    canvas.mouseReleaseEvent(_event(
        QMouseEvent.Type.MouseButtonRelease, pos,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))


def bottom_right(canvas, placement) -> QPoint:
    x, y, w, h = placement.dst_rect
    return canvas._to_view(x + w, y + h).toPoint()


# ------------------------------------------------------------ 檢查


def main() -> int:
    app = QApplication(sys.argv)
    work = Path(tempfile.mkdtemp(prefix="jreditor_"))
    failed = 0

    def check(ok: bool, message: str) -> None:
        nonlocal failed
        print(f"    {'OK  ' if ok else '✕   '}{message}")
        if not ok:
            failed += 1

    try:
        build_sheet(work)
        projects = scan_projects([work])
        groups = build_sheet_groups(projects)
        if not groups:
            print("造出來的合圖掃不到，環境有問題")
            return 2

        dialog = SheetEditorDialog(
            groups=groups, layouts=LayoutStore(), default_scale=1.0
        )
        dialog.resize(1100, 700)
        dialog.show()
        app.processEvents()
        # 關掉自動重排，才看得出拖曳本身的行為
        dialog.auto_check.setChecked(False)

        canvas = dialog.canvas
        layout = canvas.layout
        assert layout is not None
        placement = layout.placements[0]

        print(f"合圖 {PAGE_W}x{PAGE_H}、{len(layout.placements)} 個元件")

        print("\n[1] 滑鼠停在同一位置，大小不變")
        canvas.select([placement])
        start = bottom_right(canvas, placement)
        press(canvas, start)
        target = start + QPoint(40, 40)
        repeated = []
        for _ in range(6):
            move(canvas, target)
            repeated.append(placement.dst_size)
        release(canvas, target)
        check(len(set(repeated)) == 1, f"送 6 次相同座標 -> {sorted(set(repeated))}")

        print("\n[2] 往外拖單調變大、往內拖單調變小")
        canvas.select([placement])
        start = bottom_right(canvas, placement)
        press(canvas, start)
        grow = []
        for step in range(1, 9):
            move(canvas, start + QPoint(step * 8, step * 8))
            grow.append(placement.dst_size[0])
        release(canvas, start + QPoint(64, 64))
        check(all(b >= a for a, b in zip(grow, grow[1:])) and grow[-1] > grow[0],
              f"往外 8 步 -> {grow}")

        canvas.select([placement])
        start = bottom_right(canvas, placement)
        press(canvas, start)
        shrink = []
        for step in range(1, 9):
            move(canvas, start - QPoint(step * 9, step * 9))
            shrink.append(placement.dst_size[0])
        release(canvas, start - QPoint(72, 72))
        check(all(b <= a for a, b in zip(shrink, shrink[1:])) and min(shrink) >= 1,
              f"往內 8 步 -> {shrink}")

        print("\n[3] 拖遠再拖回同一點，大小相同")
        canvas.select([placement])
        start = bottom_right(canvas, placement)
        press(canvas, start)
        move(canvas, start + QPoint(50, 50))
        far = placement.dst_size
        move(canvas, start + QPoint(10, 10))
        near = placement.dst_size
        move(canvas, start + QPoint(50, 50))
        back = placement.dst_size
        release(canvas, start + QPoint(50, 50))
        check(far == back and near[0] < far[0], f"遠 {far} → 近 {near} → 遠 {back}")

        print("\n[4] 全程保持等比")
        worst = 0.0
        canvas.select([placement])
        start = bottom_right(canvas, placement)
        press(canvas, start)
        for step in range(-6, 7):
            move(canvas, start + QPoint(step * 11, step * 7))  # 刻意斜拖
            src_w, src_h = placement.src_size
            dst_w, dst_h = placement.dst_size
            worst = max(worst, abs(dst_w / src_w - dst_h / src_h))
        release(canvas, start)
        check(worst < 0.02, f"斜拖 13 步，x/y 比例最大差 {worst:.4f}")

        print("\n[5] 多選一起等比縮放")
        picked = layout.placements[:4]
        for item in picked:
            item.set_scale(1.0)
        canvas.select(picked)
        bounds = canvas._selection_bounds()
        assert bounds is not None
        start = canvas._to_view(bounds[0] + bounds[2], bounds[1] + bounds[3]).toPoint()
        press(canvas, start)
        series = []
        for step in range(1, 7):
            move(canvas, start + QPoint(step * 10, step * 10))
            series.append([p.dst_size[0] for p in picked])
        release(canvas, start + QPoint(60, 60))
        stable = all(
            all(b >= a for a, b in zip(column, column[1:]))
            for column in ([row[i] for row in series] for i in range(len(picked)))
        )
        same = len({round(p.scale, 4) for p in picked}) == 1
        check(stable and same, f"{series[0]} → {series[-1]}，比例一致 {same}")

        print("\n[6] 排版後沒有元件重疊")
        dialog.auto_check.setChecked(True)
        dialog.group_spin.setValue(50.0)
        app.processEvents()
        after = canvas.layout
        check(after.is_packed, f"重排完成 -> {after.canvas}")
        overlaps = _overlaps(after)
        check(not overlaps, f"沒有元件重疊（{len(overlaps)} 個）")
        dialog.close()

        print("\n[7] 只點開來看不算自訂")
        store = LayoutStore()
        viewer = SheetEditorDialog(groups=groups, layouts=store, default_scale=0.5)
        viewer.show()
        app.processEvents()
        for row in range(viewer.sheet_table.rowCount()):
            viewer.sheet_table.selectRow(row)
            app.processEvents()
        states = {viewer.sheet_table.item(r, 4).text()
                  for r in range(viewer.sheet_table.rowCount())}
        pending, dropped = viewer.result_layouts()
        check(states == {"—"} and not pending and not dropped,
              f"點過全部 -> 狀態 {sorted(states)}、要套用 {len(pending)} 張")
        viewer.close()

        print("\n[8] 還原原始版面 -> 與原檔完全相同")
        atlas_path = projects[0].atlases[0].atlas_path
        png_path = groups[0].page_path
        atlas_before = atlas_path.read_bytes()
        with Image.open(png_path) as handle:
            px_before = np.asarray(handle.convert("RGBA")).copy()

        editor = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=0.5)
        editor.show()
        app.processEvents()
        editor.group_spin.setValue(40.0)       # 先亂改
        app.processEvents()
        editor._revert_layout()                # 再還原
        app.processEvents()
        reverted = editor.canvas.layout
        check(reverted.is_identity and reverted.canvas == reverted.src_canvas,
              f"還原後為原始版面 -> {reverted.canvas}")
        committed, _ = editor.result_layouts()
        editor.close()

        store = LayoutStore()
        for item in committed:
            store.put(item)
        # 全域故意給 50%，原始版面必須擋掉它
        options = ProcessOptions(resize_enabled=True, scale_percent=50)
        rendered: dict[Path, object] = {}
        failures = []
        for project in projects:
            for asset in project.atlases:
                if not asset.is_loadable or asset.missing_pages:
                    continue
                result = process_asset(
                    asset, options, rendered_pages=rendered, layouts=store  # type: ignore[arg-type]
                )
                if not result.ok:
                    failures.append(asset.atlas_path.name)
        check(not failures, f"處理成功（失敗 {len(failures)} 份）")
        check(atlas_path.read_bytes() == atlas_before, "atlas byte-identical")
        with Image.open(png_path) as handle:
            px_after = np.asarray(handle.convert("RGBA"))
        same_px = px_after.shape == px_before.shape and np.array_equal(px_after, px_before)
        check(same_px, f"貼圖像素完全相同（{px_before.shape[1]}x{px_before.shape[0]}）")

        print("\n[9] 點選不會固定元件；改整組比例會排到最小")
        pins = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=1.0)
        pins.resize(1100, 700)
        pins.show()
        app.processEvents()
        pin_canvas = pins.canvas
        pin_layout = pin_canvas.layout
        assert pin_layout is not None

        # 只是點一下選取（真實滑鼠按下一定伴隨 move 事件）
        for placement in pin_layout.placements[:4]:
            x, y, w, h = placement.dst_rect
            spot = pin_canvas._to_view(x + w / 2, y + h / 2).toPoint()
            press(pin_canvas, spot)
            move(pin_canvas, spot)
            release(pin_canvas, spot)
            app.processEvents()
        pinned = sum(1 for p in pin_layout.placements if p.pinned)
        check(pinned == 0, f"點過 4 個元件後被固定的數量 = {pinned}")

        # 真的拖曳才會固定
        moved = pin_layout.placements[0]
        x, y, w, h = moved.dst_rect
        spot = pin_canvas._to_view(x + w / 2, y + h / 2).toPoint()
        press(pin_canvas, spot)
        move(pin_canvas, spot + QPoint(30, 20))
        release(pin_canvas, spot + QPoint(30, 20))
        app.processEvents()
        check(moved.pinned, "真的拖曳後才被固定")

        # 改整組比例：固定要被取消，畫布要縮到接近理論最小
        pins.group_spin.setValue(17.0)
        app.processEvents()
        small = pin_canvas.layout
        pinned_after = sum(1 for p in small.placements if p.pinned)
        fill = small.used_area / (small.canvas[0] * small.canvas[1]) * 100
        check(pinned_after == 0 and fill >= 55,
              f"整組 17% -> {small.canvas}、填充 {fill:.0f}%、固定 {pinned_after} 個")
        pins.close()
        print("\n全部通過" if not failed else f"\n失敗 {failed} 項")
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _overlaps(layout) -> list:
    placed = [p for p in layout.placements if p.pos is not None]
    bad = []
    for index, first in enumerate(placed):
        ax, ay, aw, ah = first.dst_rect
        for second in placed[index + 1:]:
            bx, by, bw, bh = second.dst_rect
            if (ax, ay, aw, ah) == (bx, by, bw, bh):
                continue
            if bx >= ax + aw or bx + bw <= ax or by >= ay + ah or by + bh <= ay:
                continue
            bad.append((first, second))
    return bad


if __name__ == "__main__":
    sys.exit(main())
