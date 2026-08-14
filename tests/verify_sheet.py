"""
合圖版面驗證腳本 — 可直接對自己的素材重跑

用法：
    py -3 tests/verify_sheet.py <spine 素材資料夾> [縮放百分比]

例如：
    py -3 tests/verify_sheet.py "D:/game/assets/spine" 50

合圖版面模式（每個元件各自等比縮放 + 重新排版到最小頁面）比整頁等比縮放
多了兩個風險，這裡就是專門驗這兩件事：

1. **共用一致性**：一張合圖常被多份 atlas 共用。重排版後每一份 atlas 的
   同名區塊座標必須完全一致，也必須拿到同一個頁面尺寸——只要有一份沒跟上，
   那一份播出來就是破圖。
2. **元件聯集**：輸出的貼圖必須含**所有** atlas 需要的像素。若照著其中一份
   atlas 的區塊清單重繪，另一份獨有的區塊會變成空白。

另外照樣量化：

* 頂點偏移（spine-runtimes 的 RegionAttachment 算式，單位為原始像素）
* 頁面宣告尺寸 vs 實際輸出 PNG 尺寸
* 區塊重疊與超出頁面（走內建 validator）
* 面積變化（合圖重排會順便回收原本版面的空隙）

不會動到原始素材：所有處理都在系統暫存目錄的副本上進行。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import PAGE_ALIGN_NONE
from core.atlas_parser import parse_atlas_file
from core.pipeline import RenderedPage, process_asset
from core.project_scanner import scan_projects
from core.sheet_group import build_sheet_groups
from models.process_options import ProcessOptions
from models.sheet_layout import LayoutStore


def local_quad(att_w: float, att_h: float, region) -> tuple[float, float, float, float]:
    """
    spine-runtimes RegionAttachment.updateOffset() 的核心算式。

    attachment 尺寸取「縮放前的 orig」，算出來的座標就是原始像素單位，
    前後相減即為頂點偏移（與 tests/verify.py 同一個量法）。
    """
    size_w, size_h = region.size
    orig_w, orig_h = region.orig
    off_x, off_y = region.offset
    if not orig_w or not orig_h:
        return 0.0, 0.0, 0.0, 0.0
    scale_x, scale_y = att_w / orig_w, att_h / orig_h
    local_x = -att_w / 2 + off_x * scale_x
    local_y = -att_h / 2 + off_y * scale_y
    return local_x, local_y, local_x + size_w * scale_x, local_y + size_h * scale_y


def folders_with_atlas(root: Path) -> list[Path]:
    """含 atlas 的資料夾（一個資料夾當成一批素材處理）"""
    found = {p.parent for p in root.rglob("*.atlas")}
    found |= {p.parent for p in root.rglob("*.atlas.txt")}
    return sorted(found)


def snapshot(projects) -> dict[tuple[str, str], tuple]:
    """處理前的每個區塊：頂點四角 + attachment 尺寸"""
    result: dict[tuple[str, str], tuple] = {}
    for project in projects:
        for asset in project.atlases:
            if not asset.is_loadable:
                continue
            for region in parse_atlas_file(asset.atlas_path).regions:
                att = region.orig
                if not att[0] or not att[1]:
                    continue
                result[(asset.atlas_path.name, region.name)] = (
                    local_quad(att[0], att[1], region), att
                )
    return result


def verify_folder(folder: Path, scale: float) -> dict:
    """處理一個資料夾並回報統計；errors 非空即為失敗"""
    stats = {
        "name": folder.name,
        "sheets": 0,
        "regions": 0,
        "src_px": 0,
        "dst_px": 0,
        "drift": 0.0,
        "pack_ms": 0.0,
        "errors": [],
    }
    work = Path(tempfile.mkdtemp(prefix="jrsheet_"))
    try:
        target = work / folder.name
        shutil.copytree(folder, target)
        projects = scan_projects([target])
        groups = [g for g in build_sheet_groups(projects) if g.can_edit]
        if not groups:
            return stats

        started = time.perf_counter()
        layouts = LayoutStore()
        for group in groups:
            layouts.put(group.build_layout(scale=scale, padding=2, align=PAGE_ALIGN_NONE))
        stats["pack_ms"] = (time.perf_counter() - started) * 1000

        stats["sheets"] = len(groups)
        stats["regions"] = sum(g.region_count for g in groups)
        stats["src_px"] = sum(g.src_canvas[0] * g.src_canvas[1] for g in groups)
        stats["dst_px"] = sum(
            layout.canvas[0] * layout.canvas[1]
            for layout in (layouts.get(g.page_path) for g in groups)
            if layout is not None
        )

        before = snapshot(projects)
        options = ProcessOptions(resize_enabled=True, scale_percent=scale * 100)
        rendered: dict[Path, RenderedPage] = {}
        for project in projects:
            for asset in project.atlases:
                if not asset.is_loadable or asset.missing_pages:
                    continue
                result = process_asset(asset, options, rendered_pages=rendered, layouts=layouts)
                if not result.ok:
                    detail = result.error or (
                        result.report.errors[0].message if result.report.errors else "驗證未通過"
                    )
                    stats["errors"].append(f"{asset.atlas_path.name}: {detail}")

        # ---- 共用一致性 + 尺寸 + 頂點偏移
        shared: dict[str, dict[str, tuple]] = {}
        for project in projects:
            for asset in project.atlases:
                if not asset.is_loadable or asset.missing_pages:
                    continue
                atlas = parse_atlas_file(asset.atlas_path)
                for page in atlas.pages:
                    png = asset.atlas_path.parent / page.name
                    if png.is_file():
                        with Image.open(png) as img:
                            if img.size != page.size:
                                stats["errors"].append(
                                    f"{page.name}: atlas 宣告 {page.size}、實際 {img.size}"
                                )
                    slot = shared.setdefault(page.name, {})
                    for region in page.regions:
                        value = (region.xy, region.size, region.orig,
                                 region.offset, page.size)
                        if region.name in slot and slot[region.name] != value:
                            stats["errors"].append(
                                f"{page.name} 的 {region.name} 在不同 atlas 中座標不一致"
                            )
                        slot[region.name] = value

                        key = (asset.atlas_path.name, region.name)
                        if key in before:
                            old, att = before[key]
                            new = local_quad(att[0], att[1], region)
                            stats["drift"] = max(
                                stats["drift"],
                                max(abs(a - b) for a, b in zip(old, new)),
                            )
        return stats
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"找不到資料夾：{root}")
        return 2
    percent = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0
    scale = percent / 100.0

    folders = folders_with_atlas(root)
    if not folders:
        print(f"{root} 底下找不到任何 .atlas")
        return 2

    print(f"合圖版面驗證：{len(folders)} 個資料夾、縮放 {percent:g}%")
    print(f"\n{'資料夾':30s} {'合圖':>4s} {'元件':>5s} {'面積':>6s} {'頂點偏移':>9s}  結果")
    print("-" * 72)

    failed = 0
    totals = {"sheets": 0, "src": 0, "dst": 0, "drift": 0.0}
    slowest = (0.0, "")

    for folder in folders:
        stats = verify_folder(folder, scale)
        if not stats["sheets"]:
            continue
        ratio = stats["dst_px"] / stats["src_px"] * 100 if stats["src_px"] else 0.0
        status = "OK" if not stats["errors"] else f"FAIL（{len(stats['errors'])}）"
        print(
            f"{stats['name'][:30]:30s} {stats['sheets']:4d} {stats['regions']:5d} "
            f"{ratio:5.0f}% {stats['drift']:8.2f}px  {status}"
        )
        for message in stats["errors"][:3]:
            print(f"    ✕ {message}")
        if stats["errors"]:
            failed += 1
        totals["sheets"] += stats["sheets"]
        totals["src"] += stats["src_px"]
        totals["dst"] += stats["dst_px"]
        totals["drift"] = max(totals["drift"], stats["drift"])
        if stats["pack_ms"] > slowest[0]:
            slowest = (stats["pack_ms"], f"{stats['name']}（{stats['regions']} 元件）")

    print("-" * 72)
    area = totals["dst"] / totals["src"] * 100 if totals["src"] else 0.0
    print(
        f"合圖 {totals['sheets']} 張、總面積 {area:.1f}%（原本 100%）、"
        f"最大頂點偏移 {totals['drift']:.2f} 原始像素"
    )
    print(f"排版最慢：{slowest[1]} {slowest[0]:.0f} ms")
    print("失敗 0 個，全部通過" if not failed else f"\n失敗 {failed} 個資料夾")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
