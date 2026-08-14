"""
驗證腳本 — 可直接對自己的素材重跑

用法：
    py -3 tests/verify.py <spine 素材資料夾> [縮放百分比]

例如：
    py -3 tests/verify.py "D:/game/assets/spine" 50

會依序執行四項檢查：

1. **atlas round-trip**：每份 atlas 讀進來再寫出去必須 byte-identical，
   確認解析器沒有吃掉或改寫任何格式細節。
2. **頂點一致性**：直接複製 spine-runtimes 的 RegionAttachment 頂點算式，
   比較縮放前後同一個 attachment 的四角座標差多少。這是「播放會不會跑掉」
   的直接證據。
3. **像素品質**：以「單獨縮放該圖塊」為基準真值，比較「整張 PNG 一次縮小」
   與本工具「逐圖塊縮小」的色差，量化滲色問題。
4. **輸出驗證**：確認每份輸出的 atlas 都通過內建的完整性檢查。

不會動到原始素材：所有處理都在系統暫存目錄的副本上進行。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.constants import ALPHA_MODE_PREMULTIPLY, MODE_RESCALE, OUTPUT_SUBFOLDER
from core.asset_scanner import find_atlas_files, load_asset
from core.atlas_parser import parse_atlas_file
from core.page_renderer import RenderSettings, render_page
from core.pipeline import process_asset
from core.rect_mapper import build_page_mapping, round_half_up
from models.process_options import ProcessOptions
from utils.image_utils import premultiply, resize_block, to_rgba_array


# ------------------------------------------------------------ 1. round-trip


def check_round_trip(atlas_files: list[Path]) -> int:
    """
    讀進來再寫出去必須 byte-identical。

    刻意比 **bytes** 而不是 ``read_text()``：後者會做 universal newlines，
    把 CRLF 正規化成 LF，於是「換行符被改掉」這種差異會整個看不到
    （實際上就曾經漏掉過一次）。
    """
    print("\n[1] atlas round-trip（讀進來再寫出去必須 byte-identical）")
    failed = 0
    for path in atlas_files:
        try:
            atlas = parse_atlas_file(path)
        except Exception as exc:  # noqa: BLE001 - 驗證腳本要回報所有失敗
            print(f"    ✕ 解析失敗 {path.name}: {exc}")
            failed += 1
            continue
        original = path.read_bytes()
        produced = atlas.to_text().encode("utf-8")
        if produced != original:
            hint = ""
            if produced.replace(b"\r\n", b"\n") == original.replace(b"\r\n", b"\n"):
                hint = "（只差在換行符）"
            print(f"    ✕ round-trip 不一致 {path.name}{hint}")
            failed += 1
    print(f"    {len(atlas_files) - failed} / {len(atlas_files)} 通過")
    return failed


# ------------------------------------------------------------ 2. 頂點一致性


def local_quad(att_w: float, att_h: float, region) -> tuple[float, float, float, float]:
    """spine-runtimes RegionAttachment.updateOffset() 的核心算式"""
    size_w, size_h = region.size
    orig_w, orig_h = region.orig
    off_x, off_y = region.offset
    if orig_w == 0 or orig_h == 0:
        return 0.0, 0.0, 0.0, 0.0
    scale_x = att_w / orig_w
    scale_y = att_h / orig_h
    local_x = -att_w / 2 + off_x * scale_x
    local_y = -att_h / 2 + off_y * scale_y
    return local_x, local_y, local_x + size_w * scale_x, local_y + size_h * scale_y


def check_vertices(before, after) -> tuple[float, str]:
    """回傳（最大頂點偏移像素, 最差區塊名稱）"""
    lookup: dict[str, list] = {}
    for region in after.regions:
        lookup.setdefault(region.name, []).append(region)

    worst, worst_name = 0.0, ""
    for old in before.regions:
        candidates = lookup.get(old.name)
        if not candidates:
            continue
        att_w, att_h = old.orig  # attachment 尺寸來自 .skel，縮放前後不變
        if att_w == 0 or att_h == 0:
            continue
        diff = max(
            abs(a - b)
            for a, b in zip(local_quad(att_w, att_h, old), local_quad(att_w, att_h, candidates[0]))
        )
        if diff > worst:
            worst, worst_name = diff, old.name
    return worst, worst_name


# ------------------------------------------------------------ 3. 像素品質


def check_pixels(atlas_path: Path, page_path: Path, page, scale: float) -> tuple[float, float]:
    """回傳（整張縮小的平均最大色差, 逐圖塊縮小的平均最大色差）"""
    with Image.open(page_path) as image:
        source = image.convert("RGBA")
    src = to_rgba_array(source)

    canvas = (round_half_up(page.size[0] * scale), round_half_up(page.size[1] * scale))
    mapping = build_page_mapping(page, scale, scale, canvas)

    naive = np.asarray(source.resize(canvas, Image.Resampling.LANCZOS), dtype=np.uint8)
    mine = to_rgba_array(render_page(source, mapping, RenderSettings()).image)

    naive_total = mine_total = 0.0
    count = 0
    for item in mapping.regions:
        dx, dy, dw, dh = item.dst_rect
        sx, sy, sw, sh = item.src_rect
        if dw < 2 or dh < 2:
            continue
        truth = resize_block(
            src[sy : sy + sh, sx : sx + sw],
            dw,
            dh,
            Image.Resampling.LANCZOS,
            alpha_mode=ALPHA_MODE_PREMULTIPLY,
        )
        naive_total += _max_diff(naive[dy : dy + dh, dx : dx + dw], truth)
        mine_total += _max_diff(mine[dy : dy + dh, dx : dx + dw], truth)
        count += 1
    if not count:
        return 0.0, 0.0
    return naive_total / count, mine_total / count


def _max_diff(a: np.ndarray, b: np.ndarray) -> float:
    """在預乘 alpha 空間比較（透明像素的 RGB 沒有視覺意義）"""
    return float(np.abs(premultiply(a).astype(np.int16) - premultiply(b).astype(np.int16)).max())


# ------------------------------------------------------------ 主流程


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    source = Path(sys.argv[1])
    percent = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0
    if not source.is_dir():
        print(f"找不到資料夾：{source}")
        return 2

    atlas_files = find_atlas_files([source])
    if not atlas_files:
        print(f"在 {source} 中找不到任何 .atlas")
        return 2

    print(f"素材：{source}")
    print(f"縮放：{percent:g}%　找到 {len(atlas_files)} 份 atlas")

    failures = check_round_trip(atlas_files)

    workspace = Path(tempfile.mkdtemp(prefix="spineresize_verify_"))
    scale = percent / 100.0
    options = ProcessOptions(
        mode=MODE_RESCALE,
        resize_enabled=True,   # 這支腳本就是在驗縮放，明確開啟（預設是關的）
        scale_percent=percent,
        output_mode=OUTPUT_SUBFOLDER,
        subfolder_name="out",
    )

    print("\n[2][3][4] 端對端處理、頂點一致性、像素品質、輸出驗證")
    header = f"    {'資產':<26}{'頂點偏移':>10}{'整張縮小':>10}{'逐圖塊':>8}{'精確區塊':>10}  驗證"
    print(header)
    print("    " + "-" * (len(header) - 4))

    worst_overall = 0.0
    try:
        for atlas_path in atlas_files:
            case = workspace / atlas_path.parent.name / atlas_path.stem
            case.mkdir(parents=True, exist_ok=True)
            for item in atlas_path.parent.iterdir():
                if item.is_file():
                    shutil.copy2(item, case / item.name)

            asset = load_asset(case / atlas_path.name)
            if not asset.is_loadable or asset.missing_pages:
                print(f"    {asset.name:<26}{'略過（缺貼圖或解析失敗）':>30}")
                continue

            before = parse_atlas_file(case / atlas_path.name)
            page = before.pages[0]
            page_path = asset.pages[page.name]
            naive_diff, mine_diff = check_pixels(atlas_path, page_path, page, scale)

            result = process_asset(asset, options)
            if result.error or result.report.errors:
                detail = result.error or f"{len(result.report.errors)} 項錯誤"
                print(f"    {asset.name:<26}{'':>28}  ✕ {detail}")
                for issue in result.report.errors[:3]:
                    print(f"        {issue.message}")
                failures += 1
                continue

            after = parse_atlas_file(result.atlas_out)
            drift_px, drift_name = check_vertices(before, after)
            worst_overall = max(worst_overall, drift_px)

            report = result.report
            status = "OK" if not report.warnings else f"OK（{len(report.warnings)} 警告）"
            print(
                f"    {asset.name:<26}{drift_px:>9.2f}px{naive_diff:>10.0f}{mine_diff:>8.0f}"
                f"{report.exact_regions:>6}/{report.total_regions:<4}  {status}"
            )

            if mine_diff > 0.5:
                print(f"        ! 逐圖塊縮放的色差不為 0（{mine_diff:.2f}），請檢查裁切對位")
                failures += 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    print(f"\n最大頂點偏移：{worst_overall:.2f} 原始像素")
    print("失敗 0 項，全部通過" if failures == 0 else f"失敗 {failures} 項")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
