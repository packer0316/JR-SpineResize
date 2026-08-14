"""背景執行緒：掃描、處理、縮放預覽建構，都不阻塞介面"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal

from config.constants import MODE_RESCALE, PNG_FORMAT_PALETTE
from core.atlas_parser import parse_atlas_file
from core.page_renderer import RenderSettings, render_page
from core.pipeline import BatchResult, process_asset
from core.project_scanner import scan_projects
from core.rect_mapper import align_up, apply_page_mapping, build_page_mapping, round_half_up
from core.spine.texture_store import AtlasTextureStore
from models.process_options import ProcessOptions
from models.spine_project import STATUS_DONE, STATUS_FAILED, SpineProject
from utils.image_utils import quantize_to_palette


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
    依套用的設定在記憶體中產生「縮放後」貼圖庫，供播放器切換對比。

    走與正式處理完全相同的路徑（build_page_mapping / render_page /
    apply_page_mapping），所以預覽畫面就是輸出結果。
    """

    built = pyqtSignal(object, object, str)   # project, AtlasTextureStore, label
    failed = pyqtSignal(object, str)          # project, error

    def __init__(self, project: SpineProject, options: ProcessOptions, parent=None) -> None:
        super().__init__(parent)
        self._project = project
        self._options = options

    def run(self) -> None:
        project = self._project
        options = self._options
        asset = project.primary_atlas
        if asset is None or not asset.is_loadable:
            self.failed.emit(project, "沒有可用的 atlas")
            return
        if options.mode != MODE_RESCALE:
            self.failed.emit(project, "「只重算 atlas」模式不提供縮放預覽")
            return
        try:
            atlas = parse_atlas_file(asset.atlas_path)
            settings = RenderSettings(
                resample=options.resample,
                alpha_mode=options.alpha_mode,
                bleed=options.bleed,
                bleed_px=options.bleed_px,
            )
            scale = options.scale
            pages: dict[str, Image.Image] = {}
            for page in atlas.pages:
                src_path = asset.pages.get(page.name)
                if src_path is None:
                    continue
                with Image.open(src_path) as source_img:
                    source_mode = source_img.mode
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
                # 若輸出會量化成調色盤，預覽也量化，讓對比忠實
                wants_palette = options.png_format == PNG_FORMAT_PALETTE or (
                    options.png_format != "rgba" and source_mode in ("P", "PA")
                )
                if wants_palette:
                    rendered = quantize_to_palette(rendered, dithering=options.dithering)[0].convert("RGBA")
                apply_page_mapping(mapping)
                pages[page.name] = rendered

            store = AtlasTextureStore(atlas, pages)
            self.built.emit(project, store, f"{options.scale_percent:g}%")
        except Exception as exc:  # noqa: BLE001 - 預覽失敗回報即可
            self.failed.emit(project, str(exc))
