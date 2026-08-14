"""
設定紀錄匯出

把「目前已套用設定」的專案寫成一份純文字紀錄：每一組設定的完整內容、
套用到哪些專案，以及每張貼圖的絕對路徑、尺寸與容量的預估變化。

設定相同的專案會歸成同一組（套用到全部時最常見），不會把同一份設定重印幾十次。
不相依 Qt，可在批次腳本中直接呼叫。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config.constants import (
    ALPHA_MODE_PREMULTIPLY,
    BLEED_NONE,
    COMPRESSION_EFFORTS,
    MODE_RESCALE,
    OUTPUT_CUSTOM,
    OUTPUT_INPLACE,
    OUTPUT_SUBFOLDER,
    PAGE_ALIGN_4,
    PAGE_ALIGN_POT,
    PNG_COLOR_FORMATS,
    PNG_MODES,
    RESAMPLE_FILTERS,
)
from config.version import VERSION
from models.compression_options import PngColorFormat, PngMode
from models.process_options import ProcessOptions
from models.sheet_layout import LayoutStore
from models.size_estimate import aggregate_estimates
from models.spine_project import SpineProject
from utils.file_utils import format_bytes

_SEPARATOR = "=" * 78
_NONE = "（無）"


def log_filename(stamp: datetime | None = None) -> str:
    stamp = stamp or datetime.now()
    return f"JR-SpineResize_設定紀錄_{stamp:%Y%m%d-%H%M%S}.txt"


def _pct(before: int, after: int) -> str:
    if before <= 0:
        return "—"
    ratio = after / before
    return f"縮小 {1 - ratio:.1%}" if ratio <= 1 else f"增加 {ratio - 1:.1%}"


def _align_text(value: int) -> str:
    if value == PAGE_ALIGN_4:
        return "補到 4 的倍數"
    if value == PAGE_ALIGN_POT:
        return "補到 2 的次方"
    return "不變（等比縮放）"


def _output_text(options: ProcessOptions) -> str:
    if options.output_mode == OUTPUT_INPLACE:
        return "覆蓋原檔（不建立備份）"
    if options.output_mode == OUTPUT_SUBFOLDER:
        return f"輸出到子資料夾「{options.subfolder_name}」"
    if options.output_mode == OUTPUT_CUSTOM:
        return f"輸出到指定路徑：{options.output_dir or _NONE}"
    return options.output_mode


def describe_options(options: ProcessOptions) -> list[str]:
    """把一組設定攤成人看得懂的條列（欄位順序對應介面卡片）"""
    compression = options.compression
    lines = [
        f"處理模式　　: {'縮放貼圖並重寫 atlas' if options.mode == MODE_RESCALE else '只重算 atlas（貼圖已在外部縮好）'}",
    ]

    if options.mode == MODE_RESCALE:
        if options.resize_enabled:
            lines.append(
                f"尺寸調整　　: 啟用，按百分比 {options.scale_percent:g}%"
                f"（插值 {RESAMPLE_FILTERS.get(options.resample, options.resample)}）"
            )
        else:
            lines.append("尺寸調整　　: 關閉（只壓縮，尺寸不變）")

        lines.append(
            f"壓縮 - 模式　: {PNG_MODES.get(compression.png_mode.value, compression.png_mode.value)}"
        )
        if compression.png_mode == PngMode.LOSSY:
            lines.append(f"　　品質　　　: {compression.png_quality}")
            lines.append(f"　　漸層抖動　: {compression.png_dithering * 100:.0f}")
        lines.append(
            "壓縮 - 色彩格式: "
            + PNG_COLOR_FORMATS.get(
                compression.png_color_format.value, compression.png_color_format.value
            )
        )
        if compression.png_color_format != PngColorFormat.RGBA8888:
            lines.append(f"　　量化抖動　: {'開啟' if compression.png_format_dither else '關閉'}")
        lines.append(
            "壓縮 - 最佳化強度: "
            + COMPRESSION_EFFORTS.get(compression.effort.value, compression.effort.value)
        )
        lines.append(f"移除中繼資料: {'是' if compression.remove_exif else '否'}")
        lines.append(
            "目標檔案大小: "
            + (f"{compression.target_size_kb} KB" if compression.target_size_enabled else "未啟用")
        )

        lines.append(
            f"透明處理　　: {'預乘後縮放' if options.alpha_mode == ALPHA_MODE_PREMULTIPLY else '直接縮放'}"
        )
        bleed = {
            "rgb": "滲出顏色",
            "full": "連 alpha 一起外擴",
            BLEED_NONE: "不處理",
        }.get(options.bleed, options.bleed)
        bleed_text = bleed if options.bleed == BLEED_NONE else f"{bleed} {options.bleed_px} px"
        lines.append(f"邊緣填充　　: {bleed_text}")
        lines.append(f"畫布對齊　　: {_align_text(options.page_align)}")
    else:
        lines.append(f"已縮好的貼圖: {options.prescaled_dir or '與 atlas 同一層'}")
        lines.append(
            f"縮放比例來源: {'由貼圖實際尺寸推算' if options.derive_scale_from_image else f'指定 {options.scale_percent:g}%'}"
        )

    lines.append(f"輸出　　　　: {_output_text(options)}")
    lines.append(f"檔名後綴　　: {options.filename_suffix or _NONE}")
    lines.append(f"複製骨架檔　: {'是' if options.copy_skeleton else '否'}")
    return lines


def _layout_block(layouts: LayoutStore) -> list[str]:
    """
    自訂合圖版面的清單。

    有版面的合圖不吃上面那組設定的縮放比例（版面自己記著每個元件的比例），
    紀錄裡一定要講清楚，否則會以為它也是照「縮放 50%」處理的。
    """
    items = layouts.layouts()
    if not items:
        return []
    lines = [
        "",
        _SEPARATOR,
        f"自訂合圖版面　—　{len(items)} 張合圖",
        _SEPARATOR,
        "這些合圖由版面決定輸出（畫布尺寸與每個元件的位置、大小），",
        "上面各組設定的「縮放比例」對它們不生效；壓縮設定照樣生效。",
        "",
    ]
    for index, layout in enumerate(sorted(items, key=lambda item: item.key), start=1):
        scale = layout.uniform_scale
        scale_text = f"整組 {scale * 100:g}%" if scale is not None else "各元件不同比例"
        pinned = sum(1 for p in layout.placements if p.pinned)
        lines.append(f"  [{index}] {layout.page_path.name}")
        lines.append(f"      路徑: {layout.page_path}")
        lines.append(
            f"      版面: {layout.src_canvas[0]}x{layout.src_canvas[1]} → "
            f"{layout.canvas[0]}x{layout.canvas[1]}"
            f"（面積 {layout.area_ratio() * 100:.0f}%）"
        )
        lines.append(
            f"      元件: {len(layout.placements)} 個、{scale_text}"
            + (f"、固定位置 {pinned} 個" if pinned else "")
        )
        lines.append(f"      排版: 間距 {layout.padding}px、畫布 {_align_text(layout.align)}")
        lines.append("")
    return lines


def _project_block(index: int, project: SpineProject, layouts: LayoutStore | None = None) -> list[str]:
    lines = [f"  [{index}] {project.name}"]
    if project.skeleton_path is not None:
        version = f"（Spine {project.spine_version}）" if project.spine_version else ""
        lines.append(f"      骨架 : {project.skeleton_path.resolve()}{version}")
    for asset in project.atlases:
        detail = f"（{asset.region_count} 區塊）" if asset.atlas else "（無法解析）"
        lines.append(f"      atlas: {asset.atlas_path.resolve()}{detail}")

    estimate = project.size_estimate
    seen: set[Path] = set()
    for asset in project.atlases:
        for page_name, path in asset.pages.items():
            if path is None:
                lines.append(f"      貼圖 : {page_name} —— 找不到檔案")
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            layout = layouts.get(path) if layouts is not None else None
            mark = "（自訂合圖版面）" if layout is not None else ""
            lines.append(f"      貼圖 : {path.name}{mark}")
            lines.append(f"             路徑: {resolved}")
            page = estimate.page(path.name) if estimate is not None else None
            if page is not None:
                src_w, src_h = page.src_size
                dst_w, dst_h = page.dst_size
                if layout is not None:
                    scale = layout.uniform_scale
                    detail = (
                        f"整組 {scale * 100:g}%" if scale is not None else "各元件不同比例"
                    )
                    lines.append(
                        f"             尺寸: {src_w}x{src_h} → {dst_w}x{dst_h}"
                        f"（重新排版，{len(layout.placements)} 個元件、{detail}）"
                    )
                else:
                    edge = f"{dst_w / src_w:.1%}" if src_w else "—"
                    lines.append(
                        f"             尺寸: {src_w}x{src_h} → {dst_w}x{dst_h}（長寬各 {edge}）"
                    )
                lines.append(
                    f"             容量: {format_bytes(page.src_bytes)} → 預估 "
                    f"{format_bytes(page.est_bytes)}（{_pct(page.src_bytes, page.est_bytes)}）"
                )
            else:
                try:
                    lines.append(f"             容量: {format_bytes(path.stat().st_size)}（尚未估算）")
                except OSError:
                    pass
    return lines


def build_settings_log(
    projects: list[SpineProject],
    total_count: int | None = None,
    stamp: datetime | None = None,
    layouts: LayoutStore | None = None,
) -> str:
    """
    產生設定紀錄文字。

    Args:
        projects: 已套用設定的專案
        total_count: 清單中的專案總數（用來標示有多少份未套用）
        stamp: 產生時間
        layouts: 合圖版面庫（有版面的貼圖會另外列出並標示）
    """
    stamp = stamp or datetime.now()
    applied = [p for p in projects if p.applied_options is not None]

    lines = [
        f"JR-SpineResize {VERSION} 設定紀錄",
        f"產生時間: {stamp:%Y-%m-%d %H:%M:%S}",
        "",
    ]
    if total_count is not None and total_count > len(applied):
        lines.append(f"已套用設定: {len(applied)} 份（清單共 {total_count} 份，其餘未套用）")
    else:
        lines.append(f"已套用設定: {len(applied)} 份")

    estimates = [p.size_estimate for p in applied if p.size_estimate is not None]
    if estimates:
        src_total, est_total = aggregate_estimates(estimates)
        pending = len(applied) - len(estimates)
        note = f"，另 {pending} 份尚未估算" if pending else ""
        lines.append(
            f"預估容量　: {format_bytes(src_total)} → {format_bytes(est_total)}"
            f"（{_pct(src_total, est_total)}）{note}"
        )
        lines.append("　　　　　　（共用貼圖只計一次，與實際寫出的檔案量一致）")

    if layouts is not None and len(layouts):
        lines.append(f"自訂合圖版面: {len(layouts)} 張（詳見下方獨立區塊）")

    if not applied:
        lines.append("")
        lines.append("沒有任何專案已套用設定。")
        lines.append("")
        return "\n".join(lines)

    # 設定相同的專案歸成一組：套用到全部時不必把同一份設定重印幾十次
    groups: list[tuple[ProcessOptions, list[SpineProject]]] = []
    for project in applied:
        options = project.applied_options
        assert options is not None
        key = describe_options(options)
        for existing_options, members in groups:
            if describe_options(existing_options) == key:
                members.append(project)
                break
        else:
            groups.append((options, [project]))

    for group_index, (options, members) in enumerate(groups, start=1):
        lines.append("")
        lines.append(_SEPARATOR)
        title = f"設定 {group_index}" if len(groups) > 1 else "設定"
        lines.append(f"{title}　—　套用於 {len(members)} 份專案")
        lines.append(_SEPARATOR)
        lines.extend(describe_options(options))
        lines.append("")
        lines.append(f"套用的專案（{len(members)} 份）:")
        for index, project in enumerate(members, start=1):
            lines.extend(_project_block(index, project, layouts))
            lines.append("")

    if layouts is not None:
        lines.extend(_layout_block(layouts))

    return "\n".join(lines)


def write_settings_log(
    projects: list[SpineProject],
    path: Path,
    total_count: int | None = None,
    layouts: LayoutStore | None = None,
) -> Path:
    """寫出設定紀錄，回傳實際路徑。"""
    stamp = datetime.now()
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Windows 上用記事本或 Excel 開啟時中文才不會變亂碼
    path.write_text(
        build_settings_log(projects, total_count, stamp, layouts), encoding="utf-8-sig"
    )
    return path
