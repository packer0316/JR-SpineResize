"""
處理管線（不含任何 Qt 相依，方便測試與批次重用）

兩種模式：

``MODE_RESCALE``
    讀原始貼圖，逐圖塊裁切→縮放→放回新頁面，同時重算 atlas。品質最好。

``MODE_REMAP_ONLY``
    貼圖已在外部（例如 JR-Img-Compresser）縮好，本工具只負責把 atlas 的數值
    重算到與新貼圖對齊。縮放比例直接由「新貼圖 / atlas 宣告尺寸」推得。

無論哪一種模式，``.skel`` 都只會被原樣複製，絕不修改。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from config.constants import (
    MODE_RESCALE,
    OUTPUT_CUSTOM,
    OUTPUT_INPLACE,
    OUTPUT_SUBFOLDER,
)
from core.asset_scanner import resolve_page
from core.atlas_parser import parse_atlas_file, write_atlas_file
from core.compressor import Compressor, describe_encoding, format_for_suffix
from core.exceptions import AtlasParseError, PageImageError, ProcessError
from core.page_renderer import RenderSettings, render_page, render_sheet
from core.rect_mapper import (
    align_up,
    apply_page_mapping,
    build_layout_mapping,
    build_page_mapping,
    round_half_up,
)
from core.validator import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARNING,
    ValidationReport,
    validate_against_skeleton,
    validate_missing_pages,
    validate_page,
    validate_region_names,
    validate_source_page,
)
from models.atlas_data import AtlasFile
from models.compression_options import CompressionOptions
from models.process_options import ProcessOptions
from models.sheet_layout import LayoutStore, SheetLayout
from models.size_estimate import PageEstimate, SizeEstimate
from models.spine_asset import SpineAsset
from utils.file_utils import copy_file, longest_matching_root

ProgressCallback = Callable[[str], None]


@dataclass
class PageOutput:
    page_name: str
    src_path: Path | None
    dst_path: Path | None
    src_size: tuple[int, int] = (0, 0)
    dst_size: tuple[int, int] = (0, 0)
    src_bytes: int = 0
    dst_bytes: int = 0
    reused: bool = False
    source_mode: str = ""   # 來源貼圖的色彩模式（P = 8-bit 調色盤）
    encoding: str = ""      # 實際輸出的編碼


@dataclass
class AssetResult:
    asset: SpineAsset
    report: ValidationReport = field(default_factory=ValidationReport)
    pages: list[PageOutput] = field(default_factory=list)
    atlas_out: Path | None = None
    skeleton_out: Path | None = None
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and self.report.ok

    @property
    def src_bytes(self) -> int:
        return sum(p.src_bytes for p in self.pages)

    @property
    def dst_bytes(self) -> int:
        return sum(p.dst_bytes for p in self.pages)

    @property
    def saved_ratio(self) -> float:
        return 1.0 - (self.dst_bytes / self.src_bytes) if self.src_bytes else 0.0


@dataclass
class BatchResult:
    results: list[AssetResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[AssetResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[AssetResult]:
        return [r for r in self.results if not r.ok]

    @property
    def src_bytes(self) -> int:
        return sum(r.src_bytes for r in self.results)

    @property
    def dst_bytes(self) -> int:
        return sum(r.dst_bytes for r in self.results)

    @property
    def max_drift_pct(self) -> float:
        return max((r.report.max_drift_pct for r in self.results), default=0.0)


# ---------------------------------------------------------------- 輸出路徑


def resolve_output_dir(asset: SpineAsset, options: ProcessOptions) -> Path:
    if options.output_mode == OUTPUT_INPLACE:
        return asset.folder
    if options.output_mode == OUTPUT_SUBFOLDER:
        return asset.folder / (options.subfolder_name or "resized")
    if options.output_mode == OUTPUT_CUSTOM and options.output_dir:
        root = longest_matching_root(asset.folder, options.source_roots)
        if root is not None:
            relative = asset.folder.relative_to(root)
            return options.output_dir / relative
        return options.output_dir / asset.folder.name
    return asset.folder / "resized"


def _output_name(original: str, suffix: str) -> str:
    """在副檔名前插入後綴（``a.png`` + ``_half`` -> ``a_half.png``）。"""
    if not suffix:
        return original
    path = Path(original)
    return str(path.with_name(f"{path.stem}{suffix}{path.suffix}"))


# 壓縮引擎無內部狀態，共用一個實例即可
_COMPRESSOR = Compressor()


def compress_texture(
    image: Image.Image,
    suffix: str,
    compression: CompressionOptions,
    fast: bool = False,
) -> tuple[Image.Image, bytes, str]:
    """
    以 JR-Img-Compresser 的壓縮引擎編碼貼圖。

    Returns:
        (壓縮後的預覽影像, 檔案 bytes, 編碼描述)
    """
    fmt = format_for_suffix(suffix)
    preview, data = _COMPRESSOR.compress(image, compression, fmt, fast=fast)
    return preview, data, describe_encoding(compression, fmt)


def _encode_texture(
    image: Image.Image,
    dst_path: Path,
    src_path: Path,
    scale: float,
    compression: CompressionOptions,
) -> tuple[bytes | Path, str, int]:
    """
    壓縮貼圖但不落地，回傳（寫出負載, 編碼描述, 輸出大小）。

    負載是 bytes（壓縮結果）或 Path（沿用該來源檔）。實際寫出延後到
    整份資產驗證通過之後——覆蓋模式已不做 .bak，先寫再驗證失敗會把
    原檔毀掉而無從回復。

    絕不變大保護（同 TinyPNG / JR-Img-Compresser 行為）：
    比例 100% 且未做有損/色彩格式量化時，輸出像素與原圖等值——
    若壓縮結果反而比原檔大，直接沿用原檔 bytes。
    """
    _, data, encoding = compress_texture(image, dst_path.suffix, compression)

    if scale == 1.0 and not compression.alters_pixels:
        try:
            src_bytes = src_path.stat().st_size
        except OSError:
            src_bytes = 0
        if src_bytes and len(data) >= src_bytes:
            return src_path, "沿用原檔（壓縮無收益）", src_bytes

    return data, encoding, len(data)


def _flush_writes(pending_writes: list[tuple[Path, bytes | Path]]) -> None:
    """把延後的貼圖寫出全部落地（驗證通過後才呼叫）"""
    for dst_path, payload in pending_writes:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, Path):
            copy_file(payload, dst_path)
        else:
            dst_path.write_bytes(payload)


# ---------------------------------------------------------------- 共用貼圖


@dataclass
class RenderedPage:
    """
    本批次已經產生過的貼圖。

    多個 atlas 指向同一張貼圖很常見（實測素材裡有三個 atlas 共用一張 png），
    第二份之後就沿用這筆紀錄：不重新渲染，也不再檢查來源尺寸——
    覆蓋模式下來源已經是我們自己縮好的圖，再檢查一定不符。
    """

    src_path: Path
    canvas: tuple[int, int]
    # (區塊名稱, 目標矩形) 集合；用來確認這張圖真的容得下另一份 atlas 的所有區塊
    region_keys: set[tuple[str, tuple[int, int, int, int]]]
    src_bytes: int
    dst_bytes: int
    source_mode: str
    encoding: str
    owner: str  # 第一個產生它的 atlas 檔名（訊息用）
    # 產生它的合圖版面指紋；有版面時用它判斷能不能沿用（比逐區塊比對更直接）
    layout_fingerprint: tuple | None = None


def _region_keys(mapping) -> set[tuple[str, tuple[int, int, int, int]]]:
    return {(item.region.name, item.dst_rect) for item in mapping.regions}


def _reuse_blocker(
    record: RenderedPage,
    src_path: Path,
    canvas: tuple[int, int],
    mapping,
    layout: SheetLayout | None = None,
) -> str:
    """回傳不能沿用的原因；空字串表示可以沿用。"""
    if record.src_path != src_path.resolve():
        return (
            f"另一份 atlas（{record.owner}）已經輸出到同一個檔名，但來源貼圖不同，"
            "請改用不同的檔名後綴或輸出資料夾"
        )
    if layout is not None:
        # 合圖版面模式：版面是整張貼圖的屬性，指紋相同就代表兩份 atlas 拿到
        # 的是同一份排版結果，不必再逐區塊比對
        if record.layout_fingerprint != layout.fingerprint():
            return (
                f"與 {record.owner} 共用同一張貼圖，但兩者的合圖版面不同——"
                "請重新開啟合圖編輯器套用同一份版面"
            )
        return ""
    if record.layout_fingerprint is not None:
        return (
            f"與 {record.owner} 共用同一張貼圖，但那一份用了自訂合圖版面、"
            "這一份沒有；請對整個合圖群組套用同一種設定"
        )
    if record.canvas != canvas:
        return (
            f"與 {record.owner} 共用同一張貼圖，但它算出的頁面尺寸是 "
            f"{record.canvas[0]}x{record.canvas[1]}，本份需要 {canvas[0]}x{canvas[1]}"
        )
    missing = sorted(
        name for name, rect in _region_keys(mapping) if (name, rect) not in record.region_keys
    )
    if missing:
        shown = "、".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        return (
            f"與 {record.owner} 共用同一張貼圖，但這份 atlas 的區塊版面不同"
            f"（{len(missing)} 個區塊對不上：{shown}）"
        )
    return ""


# ---------------------------------------------------------------- 主流程


def process_asset(
    asset: SpineAsset,
    options: ProcessOptions,
    rendered_pages: dict[Path, RenderedPage] | None = None,
    progress: ProgressCallback | None = None,
    layouts: LayoutStore | None = None,
) -> AssetResult:
    """
    處理單一 Spine 資產。

    ``rendered_pages`` 記錄同一批次內已經產生過的貼圖（輸出路徑 -> RenderedPage）。
    多個 atlas 共用同一張貼圖是常見作法（實測素材中就有三個 atlas 指向同一張 png），
    第二份之後直接沿用，不重複渲染，也不會因為「來源已經被自己縮過」而誤判失敗。

    ``layouts`` 是合圖版面庫（以貼圖路徑為鍵）。有版面的頁面完全依版面輸出，
    全域的縮放比例對它不生效——版面本身已經記著每個元件要縮多少。
    """
    started = time.perf_counter()
    result = AssetResult(asset=asset)
    rendered_pages = rendered_pages if rendered_pages is not None else {}

    def notify(message: str) -> None:
        if progress:
            progress(message)

    try:
        # 每次都從磁碟重新解析，避免同一份物件被縮放兩次
        atlas = parse_atlas_file(asset.atlas_path)
    except AtlasParseError as exc:
        result.error = str(exc)
        result.elapsed = time.perf_counter() - started
        return result

    names_before = {r.name for r in atlas.regions}
    out_dir = resolve_output_dir(asset, options)
    in_place = out_dir.resolve() == asset.folder.resolve()

    settings = RenderSettings(
        resample=options.resample,
        alpha_mode=options.alpha_mode,
        bleed=options.bleed,
        bleed_px=options.bleed_px,
    )

    missing = [p.name for p in atlas.pages if asset.pages.get(p.name) is None]
    if missing:
        result.report.extend(validate_missing_pages(missing, asset.atlas_path))
        result.error = "缺少貼圖頁面"
        result.elapsed = time.perf_counter() - started
        return result

    # 貼圖先壓縮進記憶體，等整份資產驗證通過才落地——
    # 覆蓋模式不做備份，先寫再驗證失敗會毀掉原檔
    pending_writes: list[tuple[Path, bytes | Path]] = []
    fresh_pages: dict[Path, RenderedPage] = {}

    for page in atlas.pages:
        src_path = asset.pages[page.name]
        assert src_path is not None
        notify(f"{asset.name} → {page.name}")

        try:
            page_output = _process_page(
                page=page,
                src_path=src_path,
                out_dir=out_dir,
                options=options,
                settings=settings,
                report=result.report,
                rendered_pages=rendered_pages,
                atlas=atlas,
                pending_writes=pending_writes,
                fresh_pages=fresh_pages,
                owner=asset.atlas_path.name,
                layout=layouts.get(src_path) if layouts is not None else None,
            )
        except (PageImageError, ProcessError) as exc:
            result.report.add(LEVEL_ERROR, f"[{page.name}] {exc}")
            result.error = str(exc)
            break
        if page_output is not None:
            result.pages.append(page_output)

    if result.error:
        result.elapsed = time.perf_counter() - started
        return result

    # ---- 驗證 -------------------------------------------------------
    names_after = {r.name for r in atlas.regions}
    result.report.extend(validate_region_names(names_before, names_after, asset.atlas_path.name))
    result.report.extend(
        validate_against_skeleton(asset.skeleton, names_after, asset.atlas_path.name)
    )

    if not result.report.ok:
        result.elapsed = time.perf_counter() - started
        return result

    # ---- 驗證通過，貼圖與 atlas 一起落地（覆蓋模式直接改寫原檔）--------
    _flush_writes(pending_writes)
    # 檔案真的寫出去了，才讓後面共用同一張貼圖的 atlas 沿用
    rendered_pages.update(fresh_pages)
    atlas_name = _output_name(asset.atlas_path.name, options.filename_suffix)
    atlas_out = out_dir / atlas_name
    write_atlas_file(atlas, atlas_out)
    result.atlas_out = atlas_out

    # ---- 複製骨架（永不修改內容）------------------------------------
    if options.copy_skeleton and asset.skeleton_path is not None and not in_place:
        skeleton_name = _output_name(asset.skeleton_path.name, options.filename_suffix)
        skeleton_out = out_dir / skeleton_name
        copy_file(asset.skeleton_path, skeleton_out)
        result.skeleton_out = skeleton_out

    result.elapsed = time.perf_counter() - started
    return result


def _process_page(
    page,
    src_path: Path,
    out_dir: Path,
    options: ProcessOptions,
    settings: RenderSettings,
    report: ValidationReport,
    rendered_pages: dict[Path, RenderedPage],
    atlas,
    pending_writes: list[tuple[Path, bytes | Path]],
    fresh_pages: dict[Path, RenderedPage],
    owner: str,
    layout: SheetLayout | None = None,
) -> PageOutput | None:
    """處理單一頁面：算出縮放比例、產生新貼圖、把座標寫回 atlas。"""
    declared = page.size
    dst_name = _output_name(page.name, options.filename_suffix)
    dst_path = out_dir / dst_name

    if options.mode == MODE_RESCALE:
        if layout is not None and not layout.is_packed:
            report.add(
                LEVEL_ERROR,
                f"[{page.name}] 合圖版面尚未排版完成，請在合圖編輯器裡重新排版",
            )
            return None

        if layout is not None:
            # 版面模式沒有單一比例，用 0 讓「絕不變大保護」失效（像素一定變了）。
            # 例外是「原始版面」：輸出與原檔逐像素相同，這時就該比照 100% 處理，
            # 壓縮沒收益時直接沿用原檔。
            scale = 1.0 if layout.is_identity else 0.0
            canvas = layout.canvas
            mapping, unmatched = build_layout_mapping(page, layout)
            if unmatched:
                shown = "、".join(unmatched[:5]) + (" …" if len(unmatched) > 5 else "")
                report.add(
                    LEVEL_ERROR,
                    f"[{page.name}] 合圖版面與 atlas 不同步，"
                    f"{len(unmatched)} 個區塊找不到對應元件：{shown}",
                )
                return None
        else:
            scale = options.scale
            canvas = (
                max(1, align_up(round_half_up(declared[0] * scale), options.page_align)),
                max(1, align_up(round_half_up(declared[1] * scale), options.page_align)),
            )
            mapping = build_page_mapping(page, scale, scale, canvas)

        # 先看這張貼圖本批次是不是已經產生過（多個 atlas 共用一張圖）。
        # 這個判斷必須在讀圖之前：覆蓋模式下來源已被我們改成縮好的圖，
        # 若先去檢查「宣告尺寸 vs 實際尺寸」一定會誤判成二次縮放而失敗。
        record = rendered_pages.get(dst_path.resolve())
        if record is not None:
            blocker = _reuse_blocker(record, src_path, canvas, mapping, layout)
            if blocker:
                report.add(LEVEL_ERROR, f"[{page.name}] {blocker}")
                return None
            report.add(
                LEVEL_INFO,
                f"[{page.name}] 與 {record.owner} 共用同一張貼圖，沿用本批次已產生的結果",
            )
            report.extend(validate_page(mapping, canvas))
            apply_page_mapping(mapping)
            return PageOutput(
                page_name=page.name,
                src_path=src_path,
                dst_path=dst_path,
                src_size=declared,
                dst_size=canvas,
                src_bytes=record.src_bytes,
                dst_bytes=record.dst_bytes,
                reused=True,
                source_mode=record.source_mode,
                encoding="共用（已產生）",
            )

        try:
            with Image.open(src_path) as image:
                actual = image.size
                source_mode = image.mode
                image.load()
                source = image.convert("RGBA")
        except OSError as exc:
            raise PageImageError(f"無法讀取貼圖 {src_path.name}：{exc}") from exc

        # 宣告尺寸與實際圖檔不一致，多半代表貼圖已經被縮過一次；
        # 這時再縮一次就會二次劣化，直接擋下來。
        source_check = validate_source_page(page.name, declared, actual)
        if not source_check.ok:
            report.extend(source_check)
            return None

        src_bytes = src_path.stat().st_size
        if layout is not None and layout.is_identity:
            # 原始版面＝這張合圖不要動：連重繪都跳過，直接拿來源像素去壓縮。
            # 走重繪路徑的話邊緣滲出會改到透明區的 RGB，就不是「原封不動」了。
            image = source
            report.add(
                LEVEL_INFO,
                f"[{page.name}] 使用原始版面，貼圖像素與 atlas 座標維持原樣（只做壓縮）",
            )
        elif layout is not None:
            # 版面模式畫的是「版面上的所有元件」，不是這一份 atlas 的區塊清單——
            # 共用這張合圖的其他 atlas 需要的像素也必須在圖裡
            render = render_sheet(source, layout, settings, page.is_premultiplied)
            image = render.image
            for note in render.notes:
                report.add(LEVEL_INFO, f"[{page.name}] {note}")
        else:
            render = render_page(source, mapping, settings)
            image = render.image
            for note in render.notes:
                report.add(LEVEL_INFO, f"[{page.name}] {note}")

        payload, encoding, dst_bytes = _encode_texture(
            image,
            dst_path,
            src_path,
            scale,
            options.compression,
        )
        pending_writes.append((dst_path, payload))
        # 只先記在本份資產的暫存表；等驗證通過、檔案真的寫出去了才併進批次共用表，
        # 否則後面共用同一張圖的 atlas 會沿用一個根本沒被寫出的結果
        fresh_pages[dst_path.resolve()] = RenderedPage(
            src_path=src_path.resolve(),
            canvas=canvas,
            region_keys=_region_keys(mapping),
            src_bytes=src_bytes,
            dst_bytes=dst_bytes,
            source_mode=source_mode,
            encoding=encoding,
            owner=owner,
            layout_fingerprint=layout.fingerprint() if layout is not None else None,
        )

        report.extend(validate_page(mapping, canvas))
        apply_page_mapping(mapping)

        return PageOutput(
            page_name=page.name,
            src_path=src_path,
            dst_path=dst_path,
            src_size=declared,
            dst_size=canvas,
            src_bytes=src_bytes,
            dst_bytes=dst_bytes,
            reused=False,
            source_mode=source_mode,
            encoding=encoding,
        )

    # ---------------------------------------------------------------- 只重算 atlas
    if layout is not None:
        # 這個模式的貼圖是外部工具縮好的，本工具不重繪，自然也套不上版面。
        # 靜靜忽略會讓人以為版面生效了，所以明講一句。
        report.add(
            LEVEL_WARNING,
            f"[{page.name}] 「只重算 atlas」模式不會重新排版，自訂合圖版面已略過",
        )

    scaled_path = _find_prescaled_page(page.name, src_path, options)
    if scaled_path is None:
        raise PageImageError(
            f"找不到已縮放的貼圖 {page.name}，請確認「已縮好的貼圖資料夾」設定是否正確"
        )

    try:
        with Image.open(scaled_path) as image:
            actual = image.size
    except OSError as exc:
        raise PageImageError(f"無法讀取貼圖 {scaled_path.name}：{exc}") from exc

    if declared[0] <= 0 or declared[1] <= 0:
        raise ProcessError(f"[{page.name}] atlas 未宣告頁面尺寸，無法推算縮放比例")

    if options.derive_scale_from_image:
        scale_x = actual[0] / declared[0]
        scale_y = actual[1] / declared[1]
    else:
        scale_x = scale_y = options.scale

    if abs(scale_x - scale_y) > 0.002:
        report.add(
            LEVEL_WARNING,
            f"[{page.name}] 寬高縮放比例不一致（{scale_x:.4f} / {scale_y:.4f}），"
            "非等比縮放會讓貼圖變形",
        )

    mapping = build_page_mapping(page, scale_x, scale_y, actual)
    report.extend(validate_page(mapping, actual))
    apply_page_mapping(mapping)

    if scaled_path.resolve() != dst_path.resolve():
        pending_writes.append((dst_path, scaled_path))

    return PageOutput(
        page_name=page.name,
        src_path=scaled_path,
        dst_path=dst_path,
        src_size=declared,
        dst_size=actual,
        src_bytes=src_path.stat().st_size if src_path.exists() else 0,
        dst_bytes=scaled_path.stat().st_size,
    )


def _find_prescaled_page(page_name: str, fallback: Path, options: ProcessOptions) -> Path | None:
    """在指定的資料夾中尋找已經縮好的貼圖。"""
    if options.prescaled_dir is not None:
        found = resolve_page(options.prescaled_dir, page_name)
        if found is not None:
            return found
        # 外部工具常會把整個資料夾結構攤平，再用檔名找一次
        target = Path(page_name).name.lower()
        if options.prescaled_dir.is_dir():
            for entry in options.prescaled_dir.rglob("*"):
                if entry.is_file() and entry.name.lower() == target:
                    return entry
        return None
    return fallback if fallback.is_file() else None


# ---------------------------------------------------------------- 預覽與大小估算


@dataclass
class PreviewBuild:
    """``build_preview`` 的結果"""

    estimate: SizeEstimate
    # preview_asset 指定時：座標已重算的 atlas 與壓縮後貼圖（供播放器建貼圖庫）
    atlas: AtlasFile | None = None
    pages: dict[str, Image.Image] = field(default_factory=dict)


def build_preview(
    assets: list[SpineAsset],
    options: ProcessOptions,
    preview_asset: SpineAsset | None = None,
    layouts: LayoutStore | None = None,
    fingerprint: tuple | None = None,
) -> PreviewBuild:
    """
    估算處理後的貼圖大小，必要時一併產生預覽貼圖。

    走與正式輸出完全相同的路徑（build_page_mapping / build_layout_mapping →
    render → compress_texture），只是壓縮用快速模式（oxipng level 1）加速，
    所以數字與畫面都忠於實際輸出。

    ``preview_asset`` 指定時會保留該 atlas 的壓縮後貼圖；其餘 atlas 只算大小，
    不留影像（批次估算數十份專案時記憶體才不會爆）。

    ``fingerprint`` 是給估算結果的快取鍵；省略時只用 options 的指紋
    （呼叫端若有合圖版面，要把版面指紋一起算進來）。

    Raises:
        ProcessError: 貼圖實際尺寸與 atlas 宣告不符（可能已經被縮過一次）。
    """
    settings = RenderSettings(
        resample=options.resample,
        alpha_mode=options.alpha_mode,
        bleed=options.bleed,
        bleed_px=options.bleed_px,
    )
    scale = options.scale
    build = PreviewBuild(
        estimate=SizeEstimate(
            fingerprint=fingerprint if fingerprint is not None else options.render_fingerprint()
        )
    )
    seen_sources: set[Path] = set()

    for asset in assets:
        if not asset.is_loadable or asset.missing_pages:
            continue
        wants_pages = asset is preview_asset
        atlas = parse_atlas_file(asset.atlas_path)

        for page in atlas.pages:
            src_path = asset.pages.get(page.name)
            if src_path is None:
                continue
            with Image.open(src_path) as source_img:
                source = source_img.convert("RGBA")
            declared = page.size
            if source.size != declared:
                raise ProcessError(
                    f"{page.name} 實際尺寸與 atlas 宣告不符，可能已被縮放過"
                )

            layout = layouts.get(src_path) if layouts is not None else None
            if layout is not None and layout.is_packed:
                canvas = layout.canvas
                mapping, unmatched = build_layout_mapping(page, layout)
                if unmatched:
                    raise ProcessError(
                        f"{page.name} 的合圖版面與 atlas 不同步"
                        f"（{len(unmatched)} 個區塊找不到對應元件）"
                    )
                # 與 _process_page 一致：原始版面不重繪，直接用來源像素
                rendered = source if layout.is_identity else render_sheet(
                    source, layout, settings, page.is_premultiplied
                ).image
                # 版面模式像素一定變了，不套用「絕不變大」保護；
                # 「原始版面」除外（輸出與原檔逐像素相同）
                page_scale = 1.0 if layout.is_identity else 0.0
            else:
                canvas = (
                    max(1, align_up(round_half_up(declared[0] * scale), options.page_align)),
                    max(1, align_up(round_half_up(declared[1] * scale), options.page_align)),
                )
                mapping = build_page_mapping(page, scale, scale, canvas)
                rendered = render_page(source, mapping, settings).image
                page_scale = scale

            preview_img, data, _ = compress_texture(
                rendered, src_path.suffix, options.compression, fast=True
            )
            apply_page_mapping(mapping)
            if wants_pages:
                build.pages[page.name] = preview_img.convert("RGBA")

            key = src_path.resolve()
            if key in seen_sources:
                continue  # 多份 atlas 共用同一張貼圖，容量只計一次
            seen_sources.add(key)

            src_bytes = src_path.stat().st_size if src_path.exists() else 0
            est_bytes = len(data)
            # 與 _encode_texture 的「絕不變大保護」一致
            if page_scale == 1.0 and not options.compression.alters_pixels and src_bytes:
                est_bytes = min(est_bytes, src_bytes)
            build.estimate.pages.append(PageEstimate(
                src_path=key,
                src_bytes=src_bytes,
                est_bytes=est_bytes,
                src_size=declared,
                dst_size=canvas,
            ))

        if wants_pages:
            build.atlas = atlas

    return build


def process_batch(
    assets: list[SpineAsset],
    options: ProcessOptions,
    progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    layouts: LayoutStore | None = None,
) -> BatchResult:
    """批次處理。同一批次內共用貼圖的 atlas 只會渲染一次。"""
    batch = BatchResult()
    rendered_pages: dict[Path, RenderedPage] = {}
    total = len(assets)

    for index, asset in enumerate(assets):
        if should_cancel and should_cancel():
            break
        if progress:
            progress(index, total, asset.name)
        batch.results.append(
            process_asset(
                asset,
                options,
                rendered_pages=rendered_pages,
                progress=lambda msg: progress(index, total, msg) if progress else None,
                layouts=layouts,
            )
        )

    if progress:
        progress(total, total, "完成")
    return batch
