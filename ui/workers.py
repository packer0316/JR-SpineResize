"""背景執行緒：掃描、處理、縮放預覽建構，都不阻塞介面"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal

from config.constants import MODE_RESCALE
from core.atlas_parser import parse_atlas_file
from core.page_renderer import RenderSettings, render_page
from core.pipeline import BatchResult, compress_texture, process_asset
from core.project_scanner import scan_projects
from core.rect_mapper import align_up, apply_page_mapping, build_page_mapping, round_half_up
from core.spine.texture_store import AtlasTextureStore
from models.process_options import ProcessOptions
from models.spine_project import STATUS_DONE, STATUS_FAILED, SpineProject


class ScanWorker(QThread):
    """掃描拖入的路徑，組成以 skel 為單位的專案"""

    finished_scan = pyqtSignal(list)

    def __init__(self, paths: list[Path], parent=None) -> None:
        super().__init__(parent)
        self._paths = paths

    def run(self) -> None:
        self.finished_scan.emit(scan_projects(self._paths))


class ProcessWorker(QThread):
    """批次處理所有已套用設定的專案"""

    progress = pyqtSignal(int, int, str)
    project_done = pyqtSignal(object)          # SpineProject
    finished_process = pyqtSignal(object, list)  # BatchResult, skipped names

    def __init__(self, projects: list[SpineProject], parent=None) -> None:
        super().__init__(parent)
        self._projects = projects
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        batch = BatchResult()
        rendered_pages: dict[Path, tuple[Path, int]] = {}
        todo = [p for p in self._projects if p.applied_options is not None and p.can_process]
        skipped = [p.name for p in self._projects if p.applied_options is None]
        total = len(todo)

        for index, project in enumerate(todo):
            if self._cancelled:
                break
            self.progress.emit(index, total, project.name)
            options = project.applied_options
            assert options is not None
            ok = True
            detail = ""
            for asset in project.atlases:
                if not asset.is_loadable or asset.missing_pages:
                    continue
                result = process_asset(
                    asset,
                    options,
                    rendered_pages=rendered_pages,
                    progress=lambda msg, i=index: self.progress.emit(i, total, msg),
                )
                batch.results.append(result)
                if not result.ok:
                    ok = False
                    detail = result.error or (
                        result.report.errors[0].message if result.report.errors else "驗證未通過"
                    )
            project.status = STATUS_DONE if ok else STATUS_FAILED
            project.status_detail = detail
            self.project_done.emit(project)

        self.progress.emit(total, total, "完成")
        self.finished_process.emit(batch, skipped)


class PreviewWorker(QThread):
    """
    依套用的設定在記憶體中產生「縮放後」貼圖庫，供播放器切換對比，
    並順便以壓縮引擎（快速模式）估算每張貼圖處理後的檔案大小。

    走與正式處理完全相同的路徑（build_page_mapping / render_page /
    compress_texture / apply_page_mapping），所以預覽畫面就是輸出結果。
    """

    built = pyqtSignal(object, object, str)   # project, AtlasTextureStore, label
    estimated = pyqtSignal(object, object)    # project, 估算結果 dict
    failed = pyqtSignal(object, str)          # project, error

    def __init__(self, project: SpineProject, options: ProcessOptions, parent=None) -> None:
        super().__init__(parent)
        self._project = project
        self._options = options

    def run(self) -> None:
        project = self._project
        options = self._options
        primary = project.primary_atlas
        if primary is None or not primary.is_loadable:
            self.failed.emit(project, "沒有可用的 atlas")
            return
        if options.mode != MODE_RESCALE:
            self.failed.emit(project, "「只重算 atlas」模式不提供縮放預覽")
            return
        try:
            settings = RenderSettings(
                resample=options.resample,
                alpha_mode=options.alpha_mode,
                bleed=options.bleed,
                bleed_px=options.bleed_px,
            )
            scale = options.scale
            store = None
            page_estimates: list[dict] = []
            seen_paths: set = set()

            for asset in project.atlases:
                if not asset.is_loadable or asset.missing_pages:
                    continue
                atlas = parse_atlas_file(asset.atlas_path)
                pages: dict[str, Image.Image] = {}
                for page in atlas.pages:
                    src_path = asset.pages.get(page.name)
                    if src_path is None:
                        continue
                    with Image.open(src_path) as source_img:
                        source = source_img.convert("RGBA")
                    declared = page.size
                    if source.size != declared:
                        self.failed.emit(
                            project,
                            f"{page.name} 實際尺寸與 atlas 宣告不符，可能已被縮放過",
                        )
                        return
                    canvas = (
                        max(1, align_up(round_half_up(declared[0] * scale), options.page_align)),
                        max(1, align_up(round_half_up(declared[1] * scale), options.page_align)),
                    )
                    mapping = build_page_mapping(page, scale, scale, canvas)
                    rendered = render_page(source, mapping, settings).image
                    # 與正式輸出同一顆壓縮引擎（快速模式）：
                    # 預覽影像即壓縮後畫面，bytes 長度即預估檔案大小
                    preview_img, data, _ = compress_texture(
                        rendered, src_path.suffix, options.compression, fast=True
                    )
                    apply_page_mapping(mapping)
                    if asset is primary:
                        pages[page.name] = preview_img.convert("RGBA")

                    key = src_path.resolve()
                    if key in seen_paths:
                        continue  # 多份 atlas 共用同一張貼圖，只計一次
                    seen_paths.add(key)
                    src_bytes = src_path.stat().st_size if src_path.exists() else 0
                    est_bytes = len(data)
                    # 與 pipeline 的「絕不變大保護」一致：無縮放且無量化時不會輸出更大的檔案
                    if scale == 1.0 and not options.compression.alters_pixels and src_bytes:
                        est_bytes = min(est_bytes, src_bytes)
                    page_estimates.append({
                        "name": src_path.name,
                        "src_bytes": src_bytes,
                        "est_bytes": est_bytes,
                        "src_size": declared,
                        "dst_size": canvas,
                    })

                if asset is primary:
                    store = AtlasTextureStore(atlas, pages)

            label = f"{options.scale_percent:g}%" if options.resize_enabled else "壓縮後"
            if store is not None:
                self.built.emit(project, store, label)
            estimate = {
                "fingerprint": options.render_fingerprint(),
                "pages": page_estimates,
                "src_total": sum(p["src_bytes"] for p in page_estimates),
                "est_total": sum(p["est_bytes"] for p in page_estimates),
            }
            self.estimated.emit(project, estimate)
        except Exception as exc:  # noqa: BLE001 - 預覽失敗回報即可
            self.failed.emit(project, str(exc))
