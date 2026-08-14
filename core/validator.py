"""
輸出驗證

縮放完的 atlas 只要有一項對不上，Spine 播放時就會破圖。這裡把所有「會真的
造成破圖」的條件都檢查一遍，並量化幾何漂移，讓使用者知道這次縮放到底精不精確。

檢查項目：

* 頁面宣告尺寸與實際輸出的 PNG 尺寸一致
* 每個區塊都完整落在頁面範圍內
* 區塊之間沒有重疊（重疊代表 UV 會取到別張圖）

  例外：多個區塊指向**完全相同**的矩形是合法的——Spine 打包器會把內容一樣的
  圖塊去重，讓不同名稱共用同一塊像素（實測素材中就有這種情況）。只有部分
  重疊才是真的問題。

* ``offset + size <= orig``（違反時 runtime 算出的頂點會超出 attachment）
* 區塊尺寸不為 0
* 區塊名稱（name + index）沒有重複
* 縮放前後的區塊名稱集合完全相同
* 骨架需要的區塊在 atlas 中都存在
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.rect_mapper import PageMapping
from core.skeleton_reader import SkeletonInfo

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"

_LEVEL_ORDER = {LEVEL_ERROR: 0, LEVEL_WARNING: 1, LEVEL_INFO: 2}


@dataclass
class Issue:
    level: str
    message: str

    @property
    def icon(self) -> str:
        return {LEVEL_ERROR: "✕", LEVEL_WARNING: "!", LEVEL_INFO: "·"}[self.level]


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    total_regions: int = 0
    exact_regions: int = 0
    max_size_drift_pct: float = 0.0
    max_offset_drift_pct: float = 0.0
    max_drift_px: float = 0.0
    worst_region: str = ""

    def add(self, level: str, message: str) -> None:
        self.issues.append(Issue(level, message))

    def extend(self, other: "ValidationReport") -> None:
        self.issues.extend(other.issues)
        self.total_regions += other.total_regions
        self.exact_regions += other.exact_regions
        self.max_size_drift_pct = max(self.max_size_drift_pct, other.max_size_drift_pct)
        self.max_offset_drift_pct = max(self.max_offset_drift_pct, other.max_offset_drift_pct)
        if other.max_drift_px > self.max_drift_px:
            self.max_drift_px = other.max_drift_px
            self.worst_region = other.worst_region or self.worst_region
        if not self.worst_region:
            self.worst_region = other.worst_region

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == LEVEL_WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def max_drift_pct(self) -> float:
        return max(self.max_size_drift_pct, self.max_offset_drift_pct)

    def sorted_issues(self) -> list[Issue]:
        return sorted(self.issues, key=lambda i: _LEVEL_ORDER[i.level])


def validate_page(mapping: PageMapping, actual_size: tuple[int, int] | None = None) -> ValidationReport:
    """驗證單一頁面的縮放結果。``actual_size`` 為實際輸出圖檔的像素尺寸。"""
    report = ValidationReport()
    page_name = mapping.page.name
    canvas_w, canvas_h = mapping.dst_canvas

    if actual_size is not None and actual_size != (canvas_w, canvas_h):
        report.add(
            LEVEL_ERROR,
            f"[{page_name}] atlas 宣告 {canvas_w}x{canvas_h}，"
            f"實際貼圖為 {actual_size[0]}x{actual_size[1]}",
        )

    occupancy = np.zeros((canvas_h, canvas_w), dtype=bool) if canvas_w * canvas_h <= 64_000_000 else None
    overlaps: list[str] = []
    seen_keys: dict[tuple[str, str], int] = {}
    # 來源與目標矩形都一模一樣 = 打包器去重產生的別名，不是重疊
    seen_rects: set[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]] = set()
    aliases = 0

    report.total_regions = len(mapping.regions)

    for item in mapping.regions:
        region = item.region
        x, y, w, h = item.dst_rect

        if w <= 0 or h <= 0:
            report.add(LEVEL_ERROR, f"[{page_name}] 區塊 {region.name} 縮放後尺寸為 0")
            continue

        if x < 0 or y < 0 or x + w > canvas_w or y + h > canvas_h:
            report.add(
                LEVEL_ERROR,
                f"[{page_name}] 區塊 {region.name} 超出頁面範圍："
                f"({x},{y},{w},{h}) 超過 {canvas_w}x{canvas_h}",
            )
            continue

        off_x, off_y = item.new_offset
        size_w, size_h = item.new_size
        orig_w, orig_h = item.new_orig
        if off_x + size_w > orig_w or off_y + size_h > orig_h:
            report.add(
                LEVEL_ERROR,
                f"[{page_name}] 區塊 {region.name} 的 offset+size 超過 orig："
                f"{off_x}+{size_w} > {orig_w} 或 {off_y}+{size_h} > {orig_h}",
            )

        index = region.prop("index")
        key = (region.name, index.values[0] if index and index.values else "-1")
        seen_keys[key] = seen_keys.get(key, 0) + 1

        rect_key = (item.src_rect, item.dst_rect)
        is_alias = rect_key in seen_rects
        if is_alias:
            aliases += 1
        else:
            seen_rects.add(rect_key)
            if occupancy is not None:
                window = occupancy[y : y + h, x : x + w]
                if window.any():
                    overlaps.append(region.name)
                window[:] = True

        if item.is_exact:
            report.exact_regions += 1
        size_drift = max(abs(item.size_drift_pct[0]), abs(item.size_drift_pct[1]))
        offset_drift = max(abs(item.offset_drift_pct[0]), abs(item.offset_drift_pct[1]))
        report.max_size_drift_pct = max(report.max_size_drift_pct, size_drift)
        report.max_offset_drift_pct = max(report.max_offset_drift_pct, offset_drift)
        if item.max_drift_px > report.max_drift_px:
            report.max_drift_px = item.max_drift_px
            report.worst_region = region.name

    for (name, index), count in seen_keys.items():
        if count > 1:
            report.add(
                LEVEL_WARNING,
                f"[{page_name}] 區塊 {name}（index {index}）出現 {count} 次，Spine 會只取到其中一個",
            )

    if overlaps:
        preview = "、".join(overlaps[:5])
        more = f" 等 {len(overlaps)} 個" if len(overlaps) > 5 else ""
        report.add(LEVEL_ERROR, f"[{page_name}] 區塊互相重疊：{preview}{more}")

    if aliases:
        report.add(
            LEVEL_INFO,
            f"[{page_name}] {aliases} 個區塊與其他區塊共用同一塊像素（打包器去重，屬正常情況）",
        )

    return report


def validate_region_names(
    before: set[str],
    after: set[str],
    atlas_name: str,
) -> ValidationReport:
    """縮放不應該增刪任何區塊。"""
    report = ValidationReport()
    lost = before - after
    gained = after - before
    if lost:
        report.add(LEVEL_ERROR, f"[{atlas_name}] 縮放後遺失區塊：{'、'.join(sorted(lost)[:5])}")
    if gained:
        report.add(LEVEL_ERROR, f"[{atlas_name}] 縮放後多出區塊：{'、'.join(sorted(gained)[:5])}")
    return report


def validate_against_skeleton(
    skeleton: SkeletonInfo | None,
    atlas_regions: set[str],
    atlas_name: str,
) -> ValidationReport:
    """比對骨架與 atlas 的區塊名稱。"""
    report = ValidationReport()
    if skeleton is None:
        report.add(LEVEL_WARNING, f"[{atlas_name}] 找不到對應的 .skel / .json，無法比對骨架")
        return report

    if skeleton.region_names is not None:
        missing = skeleton.region_names - atlas_regions
        if missing:
            preview = "、".join(sorted(missing)[:5])
            more = f" 等 {len(missing)} 個" if len(missing) > 5 else ""
            report.add(LEVEL_ERROR, f"[{atlas_name}] 骨架需要但 atlas 沒有的區塊：{preview}{more}")
        unused = atlas_regions - skeleton.region_names
        if unused:
            report.add(LEVEL_INFO, f"[{atlas_name}] atlas 有 {len(unused)} 個區塊未被骨架使用")
    elif skeleton.string_pool:
        # binary 3.8 只能拿到字串池，僅能做「有沒有出現過」的弱參考，不當作錯誤
        unused = atlas_regions - skeleton.string_pool
        if unused:
            report.add(
                LEVEL_INFO,
                f"[{atlas_name}] atlas 有 {len(unused)} 個區塊名稱未出現在骨架字串中（可能未使用）",
            )

    return report


def validate_source_page(
    page_name: str,
    declared: tuple[int, int],
    actual: tuple[int, int],
) -> ValidationReport:
    """處理前先確認來源 atlas 宣告的頁面尺寸與實際圖檔一致。"""
    report = ValidationReport()
    if declared != actual:
        report.add(
            LEVEL_ERROR,
            f"[{page_name}] 來源 atlas 宣告 {declared[0]}x{declared[1]}，"
            f"但貼圖實際為 {actual[0]}x{actual[1]}，請先確認素材是否同步",
        )
    return report


def validate_missing_pages(missing: list[str], atlas_path: Path) -> ValidationReport:
    report = ValidationReport()
    for name in missing:
        report.add(LEVEL_ERROR, f"[{atlas_path.name}] 找不到貼圖頁面：{name}")
    return report
