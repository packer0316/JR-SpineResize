"""
合圖編輯器的互動驗證 — 不需要任何素材，自己造一份合圖

用法：
    py -3 tests/verify_editor.py

驗的是編輯器的行為必須是**可預測**的：

1. 滑鼠停在同一個位置，元件大小永遠一樣
2. 沿同一個方向拖，大小單調變化（不會忽大忽小）
3. 拖遠再拖回同一點，回到完全一樣的大小
4. 任何時候都保持等比（同一元件的 x/y 比例一致，否則 Spine 頂點會跑掉）
5. 多選一起拖時每個元件都同步等比，而且**不會**被固定
6. 排版後沒有元件重疊
7. 只點開來看不算「自訂」——沒動過就不會被套用
8. 「還原原始版面」真的回到原檔：atlas byte-identical、貼圖像素不變
9. 搬移「放開就不固定」：拖曳過程跟著滑鼠，放開後元件不留固定狀態；
   壓到別的元件會被自動排開（見 18）；改比例會取消固定並排到最小
10. 拖曳「進行中」右側的尺寸就要跟著更新，不是放開才跳一次
11. 100% 時排版**永不變大**：初始就是原始版面（面積 0% 變化），
    按「重新排版」或從其他比例調回 100% 也不會比原檔大——
    「原始版面等比縮小」永遠是排版的候選之一，比啟發式差就不採用
12. 清單可多選合圖：「重新排版」與「還原原始版面」一次套用到全部選取，
    其他控制項停用、畫布唯讀
13. 專案檔 round-trip：編輯器做得出來的每一種版面（混合比例、固定位置、
    自訂間距、還原的恆等版面）與套用的設定，存檔重開後逐欄位相同
14. Ctrl+Z 復原／Ctrl+Y 重做：比例、位置、固定狀態、還原原始版面
    都能一步一步退回（每張合圖各自的歷史）
15. 覆蓋輸出後記憶體要跟上磁碟：atlas 資料重新解析、已消耗的版面移除，
    處理完回編輯器不再拿舊座標裁新貼圖（跑版）；同步前開編輯器要警告
16. 「還原原始版面」不固定元件——還原後再調整任何散圖，自動重排要能
    把版面縮下去（以前全被固定，調了也縮不動，像自動重排壞掉）
17. 拆分合圖：新增頁（預設名 XXXX_2、空白時與原圖同尺寸）、跨頁拖曳搬移
    （多選一起搬、不固定、丟入後自動縮排、可復原）、套用時空頁自動捨棄；
    輸出多張貼圖與多頁 atlas，共用同一張圖的兩份 atlas 拆分結果一致，
    拆分頁的像素與原圖對應區塊完全相同
18. **元件不可重疊（最嚴重的規定）**：把元件放到別人身上，放開時剛放下的
    保住位置、被壓到的自動排開；任何操作結束後整份版面零重疊

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

from core.atlas_parser import parse_atlas_file
from core.pipeline import BatchResult, process_asset, refresh_overwritten_sources
from core.project_file import load_project_file, save_project_file
from core.sheet_group import overlapping_placements
from models.sheet_layout import SheetLayout
from core.project_scanner import scan_projects
from core.sheet_group import build_sheet_groups
from models.process_options import ProcessOptions
from models.sheet_layout import LayoutStore
from ui.dialogs.sheet_editor import SheetEditorDialog

COLS, ROWS, BLOCK, GAP = 4, 3, 80, 6
PAGE_W = COLS * (BLOCK + GAP) + GAP
PAGE_H = ROWS * (BLOCK + GAP) + GAP


def build_sheet(folder: Path, name: str = "sheet") -> None:
    """造一張 12 個色塊的合圖（未裁切，方便看出等比是否被破壞）"""
    image = Image.new("RGBA", (PAGE_W, PAGE_H), (0, 0, 0, 0))
    lines = ["", f"{name}.png", f"size: {PAGE_W},{PAGE_H}",
             "format: RGBA8888", "filter: Linear,Linear", "repeat: none"]
    for index in range(COLS * ROWS):
        col, row = index % COLS, index // COLS
        x, y = GAP + col * (BLOCK + GAP), GAP + row * (BLOCK + GAP)
        image.paste(Image.new("RGBA", (BLOCK, BLOCK), (30 + index * 15, 90, 200, 255)), (x, y))
        lines += [f"block_{index}", "  rotate: false", f"  xy: {x}, {y}",
                  f"  size: {BLOCK}, {BLOCK}", f"  orig: {BLOCK}, {BLOCK}",
                  "  offset: 0, 0", "  index: -1"]
    image.save(folder / f"{name}.png")
    (folder / f"{name}.atlas").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        check(sum(1 for p in picked if p.pinned) == 0, "角落縮放不會把元件固定")

        print("\n[6] 排版後沒有元件重疊")
        dialog.auto_check.setChecked(True)
        canvas.select_all()
        dialog.item_spin.setValue(50.0)     # 全選 + 比例 = 整組縮放
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
        editor.canvas.select_all()
        editor.item_spin.setValue(40.0)        # 先亂改
        app.processEvents()
        editor._revert_selected()              # 再還原
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

        print("\n[9] 搬移放開不固定；改比例會取消固定並排到最小")
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

        # 真的拖曳：放開後也不固定，壓到的鄰居會被自動排開
        moved = pin_layout.placements[0]
        x, y, w, h = moved.dst_rect
        spot = pin_canvas._to_view(x + w / 2, y + h / 2).toPoint()
        press(pin_canvas, spot)
        move(pin_canvas, spot + QPoint(30, 20))
        release(pin_canvas, spot + QPoint(30, 20))
        app.processEvents()
        check(not moved.pinned, "拖曳放開後不固定")
        check(not overlapping_placements(pin_layout), "壓到的鄰居已自動排開（零重疊）")

        # 全選改比例：固定要被自動取消（位置是在舊比例下挑的），
        # 畫布才縮得到接近理論最小——「還原原始版面」後整張都被固定，
        # 沒有這條規則的話重排就永遠縮不下去
        pin_canvas.select_all()
        pins.item_spin.setValue(17.0)
        app.processEvents()
        small = pin_canvas.layout
        pinned_after = sum(1 for p in small.placements if p.pinned)
        fill = small.used_area / (small.canvas[0] * small.canvas[1]) * 100
        check(pinned_after == 0 and fill >= 55,
              f"全選改 17% -> {small.canvas}、填充 {fill:.0f}%、固定 {pinned_after} 個")
        pins.close()

        print("\n[10] 拖曳中右側讀數就要跟著動")
        live = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=1.0)
        live.resize(1100, 700)
        live.show()
        app.processEvents()
        live.auto_check.setChecked(False)
        live_canvas = live.canvas
        target = live_canvas.layout.placements[0]
        live_canvas.select([target])
        app.processEvents()

        start = bottom_right(live_canvas, target)
        press(live_canvas, start)
        readouts = []
        for step in range(1, 5):
            move(live_canvas, start + QPoint(step * 12, step * 12))
            app.processEvents()
            # 面板顯示的尺寸必須等於元件當下的真實尺寸（放開之前就要對）
            width, height = target.dst_size
            readouts.append((
                f"{width}x{height}" in live.selection_label.text().replace("<b>", "").replace("</b>", ""),
                round(live.item_spin.value(), 1) == round(target.scale * 100, 1),
                f"{width}x{height}",
            ))
        release(live_canvas, start + QPoint(48, 48))
        sizes = [r[2] for r in readouts]
        check(all(r[0] for r in readouts), f"拖曳中尺寸讀數同步 -> {sizes}")
        check(all(r[1] for r in readouts), "拖曳中比例讀數同步")
        check(len(set(sizes)) == len(sizes), f"每一步的讀數都不同（真的有在更新）-> {sizes}")
        live.close()

        print("\n[11] 100% 排版永不變大")
        base = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=1.0)
        base.resize(1100, 700)
        base.show()
        app.processEvents()
        base_layout = base.canvas.layout
        assert base_layout is not None
        src_area = base_layout.src_canvas[0] * base_layout.src_canvas[1]
        check(base_layout.canvas == base_layout.src_canvas and base_layout.is_identity,
              f"初始＝原始版面 {base_layout.src_canvas}（面積 0% 變化）")
        base._repack(force=True)
        app.processEvents()
        area_repacked = base_layout.canvas[0] * base_layout.canvas[1]
        check(area_repacked <= src_area,
              f"按「重新排版」-> {base_layout.canvas}（面積 {area_repacked / src_area * 100:.0f}%）")
        base.canvas.select_all()
        base.item_spin.setValue(50.0)
        app.processEvents()
        base.item_spin.setValue(100.0)
        app.processEvents()
        round_trip = base.canvas.layout
        area_back = round_trip.canvas[0] * round_trip.canvas[1]
        check(area_back <= src_area,
              f"50% → 100% 後 -> {round_trip.canvas}（面積 {area_back / src_area * 100:.0f}%）")
        base.close()

        print("\n[12] 清單多選：批次重排與還原")
        work2 = Path(tempfile.mkdtemp(prefix="jreditor2_"))
        try:
            build_sheet(work2, "sheet")
            build_sheet(work2, "extra")
            groups2 = build_sheet_groups(scan_projects([work2]))
            multi = SheetEditorDialog(groups=groups2, layouts=LayoutStore(), default_scale=0.5)
            multi.show()
            app.processEvents()
            check(multi.sheet_table.rowCount() == 2, f"兩張合圖 -> {multi.sheet_table.rowCount()} 列")

            multi.sheet_table.selectAll()      # 等同 Ctrl+A
            app.processEvents()
            check(
                not multi.padding_spin.isEnabled() and not multi.unpin_all_button.isEnabled()
                and multi.pack_button.isEnabled() and multi.revert_button.isEnabled(),
                "多選時只留「重新排版」與「還原原始版面」",
            )
            check(multi.canvas._read_only, "多選時畫布唯讀")

            multi._repack_selected()
            app.processEvents()
            packed = [multi._working[g.key] for g in multi._groups]
            check(
                len(multi._touched) == 2 and all(item.is_packed for item in packed),
                f"一次重排 2 張 -> {[item.canvas for item in packed]}",
            )

            multi._revert_selected()
            app.processEvents()
            check(
                all(multi._working[g.key].is_identity for g in multi._groups),
                "一次還原 2 張為原始版面",
            )
            committed, dropped = multi.result_layouts()
            check(len(committed) == 2 and not dropped, f"套用時回報 {len(committed)} 張")

            # 回到單選：控制項要恢復
            multi.sheet_table.selectRow(0)
            app.processEvents()
            check(multi.padding_spin.isEnabled() and not multi.canvas._read_only,
                  "回到單選後控制項恢復")
            multi.close()
        finally:
            shutil.rmtree(work2, ignore_errors=True)

        print("\n[13] 專案檔完整還原合圖版面與設定")
        work3 = Path(tempfile.mkdtemp(prefix="jreditor3_"))
        try:
            build_sheet(work3, "sheet")
            build_sheet(work3, "extra")
            projects3 = scan_projects([work3])
            groups3 = build_sheet_groups(projects3)
            store3 = LayoutStore()
            editor3 = SheetEditorDialog(groups=groups3, layouts=store3, default_scale=1.0)
            editor3.show()
            app.processEvents()

            # 第一張：混合比例 + 方向鍵搬移（會固定）+ 自訂間距——把能改的都改
            editor3.sheet_table.selectRow(0)
            app.processEvents()
            layout_a = editor3.canvas.layout
            editor3.canvas.select([layout_a.placements[0]])
            editor3.item_spin.setValue(37.0)
            app.processEvents()
            editor3.canvas.select([layout_a.placements[1]])
            editor3.canvas.nudge(3, 2)
            app.processEvents()
            # 介面已不再產生固定狀態；直接標一個，驗證舊專案檔的固定照樣存讀
            layout_a.placements[1].pinned = True
            editor3.padding_spin.setValue(5)
            app.processEvents()

            # 第二張：還原原始版面（恆等版面也要能存能讀）
            editor3.sheet_table.selectRow(1)
            app.processEvents()
            editor3._revert_selected()
            app.processEvents()
            identity_path = editor3.canvas.layout.page_path

            committed3, _ = editor3.result_layouts()
            editor3.close()
            for item in committed3:
                store3.put(item)
            check(len(store3) == 2, f"編輯 2 張版面 -> {len(store3)} 張")

            wanted = ProcessOptions(resize_enabled=True, scale_percent=50)
            for project in projects3:
                project.applied_options = ProcessOptions(resize_enabled=True, scale_percent=50)

            saved_path = save_project_file(projects3, work3 / "roundtrip", [work3], store3)
            loaded = load_project_file(saved_path)
            check(
                len(loaded.projects) == len(projects3) and loaded.applied == len(projects3),
                f"專案與設定還原 -> {len(loaded.projects)} 份、已套用 {loaded.applied} 份",
            )
            check(
                all(
                    p.applied_options is not None
                    and p.applied_options.render_fingerprint() == wanted.render_fingerprint()
                    for p in loaded.projects
                ),
                "套用的設定逐欄位相同",
            )

            mismatch = [
                item.page_path.name
                for item in store3
                if (restored := loaded.layouts.get(item.page_path)) is None
                or restored.to_dict() != item.to_dict()
            ]
            check(not mismatch, f"版面逐欄位相同（含比例/位置/固定/名稱/間距/對齊）{mismatch or ''}")

            restored_a = loaded.layouts.get(layout_a.page_path)
            check(
                restored_a is not None
                and any(p.pinned for p in restored_a.placements)
                and restored_a.padding == 5,
                "固定位置與自訂間距有存進專案檔",
            )
            restored_identity = loaded.layouts.get(identity_path)
            check(
                restored_identity is not None and restored_identity.is_identity,
                "「還原原始版面」的恆等版面照樣還原",
            )
        finally:
            shutil.rmtree(work3, ignore_errors=True)

        print("\n[14] Ctrl+Z 復原／Ctrl+Y 重做")
        undo_dlg = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=1.0)
        undo_dlg.resize(1100, 700)
        undo_dlg.show()
        app.processEvents()
        u_layout = undo_dlg.canvas.layout
        first = u_layout.placements[0]
        scale_before = first.scale
        canvas_before = u_layout.canvas

        undo_dlg.canvas.select([first])
        undo_dlg.item_spin.setValue(37.0)
        app.processEvents()
        check(abs(first.scale - 0.37) < 1e-6, f"改比例 -> {first.scale * 100:.0f}%")
        undo_dlg._undo()
        app.processEvents()
        check(
            abs(first.scale - scale_before) < 1e-6 and u_layout.canvas == canvas_before,
            f"Ctrl+Z 還原比例與畫布 -> {u_layout.canvas}",
        )
        undo_dlg._redo()
        app.processEvents()
        check(abs(first.scale - 0.37) < 1e-6, "Ctrl+Y 重做")
        undo_dlg._undo()
        app.processEvents()

        second = u_layout.placements[1]
        pos_before = second.pos
        undo_dlg.canvas.select([second])
        undo_dlg.canvas.nudge(5, 3)
        app.processEvents()
        check(second.pos != pos_before and not second.pinned,
              "微調後位置改變（放開不固定）")
        undo_dlg._undo()
        app.processEvents()
        check(second.pos == pos_before, "Ctrl+Z 還原微調位置")

        undo_dlg.canvas.select_all()
        undo_dlg.item_spin.setValue(50.0)
        app.processEvents()
        canvas_small = u_layout.canvas
        undo_dlg._revert_selected()
        app.processEvents()
        check(u_layout.is_identity, "還原原始版面 -> 恆等版面")
        undo_dlg._undo()
        app.processEvents()
        check(
            u_layout.canvas == canvas_small and not u_layout.is_identity,
            f"「還原原始版面」也能 Ctrl+Z 退回 -> {u_layout.canvas}",
        )
        undo_dlg.close()

        print("\n[15] 覆蓋輸出後記憶體與磁碟同步")
        work4 = Path(tempfile.mkdtemp(prefix="jreditor4_"))
        try:
            build_sheet(work4)
            projects4 = scan_projects([work4])
            groups4 = build_sheet_groups(projects4)
            store4 = LayoutStore()

            # 造一份 50% 的自訂版面
            editor4 = SheetEditorDialog(groups=groups4, layouts=store4, default_scale=1.0)
            editor4.show()
            app.processEvents()
            editor4.canvas.select_all()
            editor4.item_spin.setValue(50.0)
            app.processEvents()
            committed4, _ = editor4.result_layouts()
            editor4.close()
            for item in committed4:
                store4.put(item)
            new_canvas = committed4[0].canvas

            # 覆蓋輸出（預設就是 inplace、無後綴）——來源檔被改寫
            batch4 = BatchResult()
            rendered4: dict = {}
            for project in projects4:
                for asset in project.atlases:
                    if not asset.is_loadable or asset.missing_pages:
                        continue
                    batch4.results.append(process_asset(
                        asset, ProcessOptions(), rendered_pages=rendered4, layouts=store4
                    ))
            check(bool(batch4.results) and all(r.ok for r in batch4.results),
                  f"覆蓋輸出成功（{len(batch4.results)} 份）")

            # 同步之前：記憶體還是舊座標，開編輯器必須警告尺寸不符（跑版的成因）
            stale = SheetEditorDialog(groups=groups4, layouts=store4, default_scale=1.0)
            stale.show()
            app.processEvents()
            check(bool(stale._size_mismatch),
                  "同步前開編輯器 -> 偵測到貼圖與 atlas 宣告不符並警告")
            stale.close()

            # 同步：重新解析被覆蓋的 atlas、移除已消耗的版面
            overwritten = refresh_overwritten_sources(batch4, store4)
            asset4 = next(a for p in projects4 for a in p.atlases if a.is_loadable)
            check(bool(overwritten) and asset4.atlas.pages[0].size == new_canvas,
                  f"atlas 記憶體資料已更新 -> {asset4.atlas.pages[0].size}")
            check(len(store4) == 0, "已消耗的自訂版面移除")

            # 重建群組再開編輯器：尺寸相符、內容乾淨（不再跑版）
            groups4b = build_sheet_groups(projects4)
            viewer4 = SheetEditorDialog(groups=groups4b, layouts=store4, default_scale=1.0)
            viewer4.show()
            app.processEvents()
            v_layout = viewer4.canvas.layout
            check(
                v_layout is not None and v_layout.src_canvas == new_canvas
                and not viewer4._size_mismatch,
                f"同步後編輯器與磁碟一致 -> {v_layout.src_canvas}、無警告",
            )
            viewer4.close()
        finally:
            shutil.rmtree(work4, ignore_errors=True)

        print("\n[16] 還原原始版面後再調整，自動重排要生效")
        rv = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=1.0)
        rv.resize(1100, 700)
        rv.show()
        app.processEvents()
        rv._revert_selected()
        app.processEvents()
        r_layout = rv.canvas.layout
        check(
            r_layout.is_identity and not any(p.pinned for p in r_layout.placements),
            "還原後為恆等版面且元件不被固定",
        )
        rv.canvas.select([r_layout.placements[0]])
        rv.item_spin.setValue(40.0)   # 只調一個散圖，其餘要跟著重排縮下去
        app.processEvents()
        area = r_layout.canvas[0] * r_layout.canvas[1]
        src_area = r_layout.src_canvas[0] * r_layout.src_canvas[1]
        check(
            r_layout.is_packed and area < src_area,
            f"調整單一散圖後自動重排 -> {r_layout.canvas}"
            f"（面積 {area / src_area * 100:.0f}%）",
        )
        rv.close()

        print("\n[17] 拆分合圖：新增頁、跨頁搬移、輸出多張貼圖")
        work5 = Path(tempfile.mkdtemp(prefix="jreditor5_"))
        try:
            build_sheet(work5)
            # 第二份 atlas 共用同一張貼圖：拆分結果必須兩份一致
            (work5 / "sheet_b.atlas").write_text(
                (work5 / "sheet.atlas").read_text(encoding="utf-8"), encoding="utf-8"
            )
            projects5 = scan_projects([work5])
            groups5 = build_sheet_groups(projects5)
            check(
                len(groups5) == 1 and len(groups5[0].atlas_names) == 2,
                f"兩份 atlas 共用一張合圖 -> {len(groups5)} 組、{len(groups5[0].atlas_names)} 份",
            )
            with Image.open(groups5[0].page_path) as handle:
                src_px5 = np.asarray(handle.convert("RGBA")).copy()

            store5 = LayoutStore()
            editor5 = SheetEditorDialog(groups=groups5, layouts=store5, default_scale=1.0)
            editor5.resize(1100, 700)
            editor5.show()
            app.processEvents()
            layout5 = editor5.canvas.layout
            assert layout5 is not None

            editor5._add_page()
            app.processEvents()
            check(
                layout5.page_count == 2 and layout5.page_name(1) == "sheet_2.png",
                f"新增頁預設名 -> {layout5.page_name(1)}",
            )
            check(layout5.page_canvas(1) == layout5.src_canvas, "空白頁先與原圖同尺寸")

            # 多選 4 個元件，一起拖進新頁（模擬真實滑鼠拖放）
            picked5 = layout5.placements[:4]
            editor5.canvas.select(picked5)
            app.processEvents()
            fx, fy, fw, fh = picked5[0].dst_rect
            start5 = editor5.canvas._to_view(fx + fw / 2, fy + fh / 2).toPoint()
            page1_origin = editor5.canvas._page_origin(1)
            target5 = editor5.canvas._to_view(
                page1_origin[0] + 40, page1_origin[1] + 40
            ).toPoint()
            press(editor5.canvas, start5)
            move(editor5.canvas, target5)
            release(editor5.canvas, target5)
            app.processEvents()
            check({p.page for p in picked5} == {1}, "多選 4 個一起拖進新頁")
            check(all(not p.pinned for p in picked5), "跨頁搬移不固定，自動重排接手")
            page1_canvas = layout5.page_canvas(1)
            check(
                layout5.is_packed and page1_canvas != layout5.src_canvas,
                f"丟入元件後新頁自動縮排 -> {page1_canvas}",
            )

            editor5._undo()
            app.processEvents()
            check(
                all(p.page == 0 for p in picked5)
                and layout5.page_canvas(1) == layout5.src_canvas,
                "Ctrl+Z 退回搬移前（新頁回到空白原尺寸）",
            )
            editor5._redo()
            app.processEvents()
            check({p.page for p in picked5} == {1}, "Ctrl+Y 重做搬移")

            # 再加一頁但不放東西：套用時要自動捨棄
            editor5._add_page()
            app.processEvents()
            check(layout5.page_count == 3, "再新增一頁（保持空白）")
            editor5._on_accept()
            check(layout5.page_count == 2, "套用時空白頁自動捨棄")
            committed5, _ = editor5.result_layouts()
            editor5.close()
            check(len(committed5) == 1, "拆分版面要套用")
            round5 = SheetLayout.from_dict(committed5[0].to_dict())
            check(
                round5 is not None and round5.to_dict() == committed5[0].to_dict(),
                "拆分版面序列化 round-trip 逐欄位相同",
            )

            for item in committed5:
                store5.put(item)
            batch5 = BatchResult()
            rendered5: dict = {}
            for project in projects5:
                for asset in project.atlases:
                    if not asset.is_loadable or asset.missing_pages:
                        continue
                    batch5.results.append(process_asset(
                        asset, ProcessOptions(), rendered_pages=rendered5, layouts=store5
                    ))
            check(
                len(batch5.results) == 2 and all(r.ok for r in batch5.results),
                f"兩份共用 atlas 都處理成功（{len(batch5.results)} 份）",
            )
            check((work5 / "sheet_2.png").is_file(), "輸出拆分頁 sheet_2.png")

            def region_map(parsed):
                return {
                    r.name: (r.xy, r.size, page.name)
                    for page in parsed.pages for r in page.regions
                }

            parsed_maps = []
            final = committed5[0]
            for atlas_name in ("sheet.atlas", "sheet_b.atlas"):
                parsed = parse_atlas_file(work5 / atlas_name)
                names = [p.name for p in parsed.pages]
                check(
                    names == ["sheet.png", "sheet_2.png"],
                    f"{atlas_name} 的頁面 -> {names}",
                )
                check(
                    parsed.pages[0].size == final.canvas
                    and parsed.pages[1].size == final.page_canvas(1),
                    f"{atlas_name} 頁面尺寸 -> {parsed.pages[0].size} / {parsed.pages[1].size}",
                )
                check(
                    parsed.region_count == 12 and len(parsed.pages[1].regions) == 4,
                    f"{atlas_name} 區塊分佈 -> 共 {parsed.region_count}、"
                    f"拆分頁 {len(parsed.pages[1].regions)} 個",
                )
                parsed_maps.append(region_map(parsed))
            check(parsed_maps[0] == parsed_maps[1], "兩份共用 atlas 的拆分結果一致")

            with Image.open(work5 / "sheet_2.png") as handle:
                split_px = np.asarray(handle.convert("RGBA"))
            moved0 = next(p for p in final.placements if p.page == 1)
            sx, sy, sw, sh = moved0.src_rect
            dx, dy = moved0.pos
            check(
                np.array_equal(split_px[dy:dy + sh, dx:dx + sw],
                               src_px5[sy:sy + sh, sx:sx + sw]),
                "拆分頁像素與原圖對應區塊完全相同",
            )

            # 覆蓋輸出後的記憶體同步也要認得新頁
            refresh_overwritten_sources(batch5, store5)
            asset5 = next(a for p in projects5 for a in p.atlases if a.is_loadable)
            check(
                "sheet_2.png" in asset5.pages and asset5.pages["sheet_2.png"] is not None,
                "同步後 asset 認得拆分頁（不會被當成缺圖）",
            )
            check(len(store5) == 0, "已消耗的拆分版面移除")
        finally:
            shutil.rmtree(work5, ignore_errors=True)

        print("\n[18] 元件不可重疊：放到別人身上會自動排開")
        ov = SheetEditorDialog(groups=groups, layouts=LayoutStore(), default_scale=1.0)
        ov.resize(1100, 700)
        ov.show()
        app.processEvents()
        o_layout = ov.canvas.layout
        assert o_layout is not None
        a18, b18 = o_layout.placements[0], o_layout.placements[1]
        bx, by, bw, bh = b18.dst_rect
        target_centre = (bx + bw / 2, by + bh / 2)

        ov.canvas.select([a18])
        ax, ay, aw, ah = a18.dst_rect
        start18 = ov.canvas._to_view(ax + aw / 2, ay + ah / 2).toPoint()
        end18 = ov.canvas._to_view(*target_centre).toPoint()
        press(ov.canvas, start18)
        move(ov.canvas, end18)
        release(ov.canvas, end18)
        app.processEvents()
        check(not overlapping_placements(o_layout), "放開後整份版面零重疊")
        adx, ady, adw, adh = a18.dst_rect
        check(
            adx <= target_centre[0] <= adx + adw and ady <= target_centre[1] <= ady + adh,
            f"剛放下的元件保住位置（蓋住目標點）-> pos {a18.pos}",
        )
        check(not a18.pinned and not b18.pinned, "沒有任何元件被固定")
        ov.close()

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
