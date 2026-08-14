"""
處理紀錄（log）匯出

把一次批次處理的每一筆改動寫成純文字紀錄：貼圖檔名、來源與輸出的絕對路徑、
尺寸與容量的變化幅度、實際使用的編碼，以及未通過的驗證訊息。

不相依 Qt，可在背景執行緒或批次腳本中直接呼叫。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config.version import VERSION
from core.pipeline import AssetResult, BatchResult
from utils.file_utils import format_bytes

_SEPARATOR = "=" * 78


def log_filename(stamp: datetime | None = None) -> str:
    stamp = stamp or datetime.now()
    return f"JR-SpineResize_log_{stamp:%Y%m%d-%H%M%S}.txt"


def _pct(before: int, after: int) -> str:
    """容量或面積的增減幅度"""
    if before <= 0:
        return "—"
    ratio = after / before
    return f"縮小 {1 - ratio:.1%}" if ratio <= 1 else f"增加 {ratio - 1:.1%}"


def _size_block(page) -> list[str]:
    src_w, src_h = page.src_size
    dst_w, dst_h = page.dst_size
    lines = [f"        檔名: {page.page_name}"]
    if page.src_path is not None:
        lines.append(f"        來源: {page.src_path.resolve()}")
    if page.dst_path is not None:
        lines.append(f"        輸出: {page.dst_path.resolve()}")

    area_before = src_w * src_h
    area_after = dst_w * dst_h
    edge = f"{dst_w / src_w:.1%}" if src_w else "—"
    lines.append(
        f"        尺寸: {src_w}x{src_h} → {dst_w}x{dst_h}"
        f"（長寬各 {edge}、像素量 {_pct(area_before, area_after)}）"
    )
    lines.append(
        f"        容量: {format_bytes(page.src_bytes)} → {format_bytes(page.dst_bytes)}"
        f"（{_pct(page.src_bytes, page.dst_bytes)}）"
    )
    lines.append(f"        編碼: {page.encoding or '—'}")
    if page.reused:
        lines.append("        備註: 與其他 atlas 共用，本批次只產生一次")
    return lines


def _asset_block(index: int, result: AssetResult) -> list[str]:
    report = result.report
    if result.error:
        status = f"失敗：{result.error}"
    elif report.errors:
        status = f"未輸出（{len(report.errors)} 項錯誤）"
    elif report.warnings:
        status = f"完成（{len(report.warnings)} 項警告）"
    else:
        status = "完成"

    lines = [
        "",
        _SEPARATOR,
        f"[{index}] {result.asset.name}　—　{status}",
        f"    atlas 來源: {result.asset.atlas_path.resolve()}",
    ]
    if result.atlas_out is not None:
        lines.append(f"    atlas 輸出: {result.atlas_out.resolve()}")
    if result.asset.skeleton_path is not None:
        note = "（原樣複製）" if result.skeleton_out is not None else "（未複製）"
        lines.append(f"    骨架檔　　: {result.asset.skeleton_path.resolve()} {note}")
    if report.total_regions:
        lines.append(
            f"    區塊: {report.total_regions} 個，座標完全精確 {report.exact_regions} 個，"
            f"最大幾何偏移 {report.max_drift_px:.2f} 原始像素"
        )
    lines.append(f"    耗時: {result.elapsed:.2f} 秒")

    for page_index, page in enumerate(result.pages, start=1):
        lines.append(f"    貼圖 {page_index}/{len(result.pages)}:")
        lines.extend(_size_block(page))

    issues = report.sorted_issues()
    if issues:
        lines.append("    驗證訊息:")
        for issue in issues:
            lines.append(f"        {issue.icon} {issue.message}")
    return lines


def build_log_text(
    batch: BatchResult,
    skipped: list[str] | None = None,
    stamp: datetime | None = None,
) -> str:
    stamp = stamp or datetime.now()
    ok = len(batch.succeeded)
    failed = len(batch.failed)
    total_pages = sum(len(r.pages) for r in batch.results)

    lines = [
        f"JR-SpineResize {VERSION} 處理紀錄",
        f"產生時間: {stamp:%Y-%m-%d %H:%M:%S}",
        "",
        f"資產: 完成 {ok} 份" + (f"、失敗 {failed} 份" if failed else ""),
        f"貼圖: {total_pages} 張",
    ]
    if batch.src_bytes:
        lines.append(
            f"容量: {format_bytes(batch.src_bytes)} → {format_bytes(batch.dst_bytes)}"
            f"（{_pct(batch.src_bytes, batch.dst_bytes)}）"
        )
    if skipped:
        lines.append(f"略過（未套用設定）: {len(skipped)} 份 — {'、'.join(skipped)}")

    for index, result in enumerate(batch.results, start=1):
        lines.extend(_asset_block(index, result))

    lines.append("")
    return "\n".join(lines)


def resolve_log_dir(batch: BatchResult) -> Path | None:
    """紀錄檔要放的資料夾：第一份有輸出的資產所在位置"""
    for result in batch.results:
        if result.atlas_out is not None:
            return result.atlas_out.parent
    for result in batch.results:
        for page in result.pages:
            if page.dst_path is not None:
                return page.dst_path.parent
    # 全部失敗時退回第一份資產的來源資料夾
    if batch.results:
        return batch.results[0].asset.folder
    return None


def write_process_log(
    batch: BatchResult,
    skipped: list[str] | None = None,
    directory: Path | None = None,
) -> Path | None:
    """寫出紀錄檔，回傳實際路徑（無處可寫時回傳 None）。"""
    target_dir = directory or resolve_log_dir(batch)
    if target_dir is None:
        return None
    stamp = datetime.now()
    path = target_dir / log_filename(stamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Windows 上用記事本或 Excel 開啟時中文才不會變亂碼
    path.write_text(build_log_text(batch, skipped, stamp), encoding="utf-8-sig")
    return path
