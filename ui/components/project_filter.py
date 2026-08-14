"""
專案清單的篩選與排序

版面與互動比照 JR-Img-Compresser：一個「🔍 篩選」切換鈕展開／收合面板，
面板裡是檔名搜尋加上一組兩欄的下拉選單，生效的條件數會標在按鈕上。

篩選條件本身（``FilterCriteria``）不相依 Qt，方便單獨測試。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.sheet_group import cluster_projects
from models.spine_project import (
    STATUS_APPLIED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IDLE,
    SpineProject,
)

# ---------------------------------------------------------------- 選項定義
# 每項為（顯示名稱, 資料值）；資料值 None 一律代表「全部」（不篩選）

# 頁面尺寸（最長邊像素範圍）
PAGE_SIZE_FILTERS: list[tuple[str, tuple[int, int | None] | None]] = [
    ("全部", None),
    ("≤ 512", (0, 512)),
    ("513 – 1024", (513, 1024)),
    ("1025 – 2048", (1025, 2048)),
    ("> 2048", (2049, None)),
]

# 貼圖總容量（KB 下限）
TEXTURE_SIZE_FILTERS: list[tuple[str, int | None]] = [
    ("全部", None),
    ("> 100 KB", 100),
    ("> 300 KB", 300),
    ("> 500 KB", 500),
    ("> 1 MB", 1024),
    ("> 5 MB", 5120),
]

# 套用／處理狀態
STATUS_FILTERS: list[tuple[str, str | None]] = [
    ("全部", None),
    ("已套用", STATUS_APPLIED),
    ("未套用", STATUS_IDLE),
    ("完成", STATUS_DONE),
    ("失敗", STATUS_FAILED),
]

# 是否能預覽播放（需要 3.8 binary skel）
PREVIEW_FILTERS: list[tuple[str, str | None]] = [
    ("全部", None),
    ("可預覽", "yes"),
    ("不可預覽", "no"),
]

# 頁面尺寸是否為 2 的次方
POT_FILTERS: list[tuple[str, str | None]] = [
    ("全部", None),
    ("2 的次方", "pot"),
    ("非 2 的次方", "npot"),
]

# 是否與其他專案共用貼圖
SHARED_FILTERS: list[tuple[str, str | None]] = [
    ("全部", None),
    ("共用貼圖", "shared"),
    ("獨立貼圖", "solo"),
]

# 排序。預設「共用貼圖」：用到同一張合圖的專案一定相鄰，才不會改了一份
# 卻漏掉另外兩份（共用貼圖只要有一份沒跟著改，輸出就是壞的）
SORT_OPTIONS: list[tuple[str, str]] = [
    ("共用貼圖（同組相鄰）", "shared"),
    ("名稱", "name"),
    ("容量 大→小", "size_desc"),
    ("容量 小→大", "size_asc"),
    ("尺寸 大→小", "dim_desc"),
    ("尺寸 小→大", "dim_asc"),
    ("區塊 多→少", "region_desc"),
    ("變化 大→小", "delta_desc"),
    ("變化 小→大", "delta_asc"),
]


@dataclass
class FilterCriteria:
    """一組篩選與排序條件"""

    search: str = ""
    page_size: tuple[int, int | None] | None = None
    texture_min_kb: int | None = None
    status: str | None = None
    preview: str | None = None
    pot: str | None = None
    shared: str | None = None
    sort: str = "shared"

    # 判斷「共用貼圖」需要跟整份清單比對，不能只看單一專案，
    # 所以 matches() 前先由 apply() 算好這張表。這是快取不是條件，
    # 每次 apply() 都會重算，不參與比較與顯示。
    _shared_ids: frozenset[int] = field(
        default=frozenset(), compare=False, repr=False
    )

    @property
    def active_count(self) -> int:
        """生效的篩選條件數（排序不算）"""
        return sum(
            1
            for value in (
                self.search.strip(),
                self.page_size,
                self.texture_min_kb,
                self.status,
                self.preview,
                self.pot,
                self.shared,
            )
            if value
        )

    @property
    def is_active(self) -> bool:
        return self.active_count > 0

    # ------------------------------------------------------------ 比對

    def matches(self, project: SpineProject) -> bool:
        search = self.search.strip().lower()
        if search:
            # 名稱或所在路徑任一命中都算（同名專案散在多個資料夾時很有用）
            if search not in f"{project.name}\n{project.folder}".lower():
                return False

        if self.page_size is not None:
            low, high = self.page_size
            edge = project.max_page_edge
            if edge < low or (high is not None and edge > high):
                return False

        if self.texture_min_kb is not None:
            if project.source_bytes <= self.texture_min_kb * 1024:
                return False

        if self.status is not None and project.status != self.status:
            return False

        if self.preview is not None:
            if (self.preview == "yes") != project.can_preview:
                return False

        if self.pot is not None:
            if (self.pot == "pot") != project.pages_are_pot:
                return False

        if self.shared is not None:
            is_shared = id(project) in self._shared_ids
            if (self.shared == "shared") != is_shared:
                return False

        return True

    # ------------------------------------------------------------ 排序

    def apply(self, projects: list[SpineProject]) -> list[SpineProject]:
        """回傳篩選並排序後的清單"""
        clusters = cluster_projects(projects)
        counts: dict[int, int] = {}
        for cluster_id in clusters.values():
            counts[cluster_id] = counts.get(cluster_id, 0) + 1
        self._shared_ids = frozenset(
            key for key, cluster_id in clusters.items() if counts[cluster_id] > 1
        )

        result = [p for p in projects if self.matches(p)]

        def delta_ratio(project: SpineProject) -> float:
            """容量降幅比例（增幅為負；還沒估算的視為 0）"""
            estimate = project.size_estimate
            if estimate is None or estimate.src_total <= 0:
                return 0.0
            return (estimate.src_total - estimate.est_total) / estimate.src_total

        keys = {
            "size_desc": (lambda p: p.source_bytes, True),
            "size_asc": (lambda p: p.source_bytes, False),
            "dim_desc": (lambda p: p.max_page_edge, True),
            "dim_asc": (lambda p: p.max_page_edge, False),
            "region_desc": (lambda p: p.region_count, True),
            "delta_desc": (delta_ratio, True),
            "delta_asc": (delta_ratio, False),
        }
        if self.sort == "shared":
            result.sort(key=self._shared_sort_key(result, clusters))
        elif self.sort in keys:
            key, reverse = keys[self.sort]
            result.sort(key=key, reverse=reverse)
        else:  # 名稱：同名時用資料夾當第二鍵，順序才穩定
            result.sort(key=lambda p: (p.name.lower(), str(p.folder).lower()))
        return result

    @staticmethod
    def _shared_sort_key(projects: list[SpineProject], clusters: dict[int, int]):
        """
        共用貼圖排序：整體仍接近字母序，但共用同一張貼圖的專案會被拉到
        同一群的第一份後面，保證相鄰。

        群的排序鍵取「群內最小的（資料夾, 名稱）」——所以群的位置就是它
        第一份成員原本會出現的位置，不會為了分群而把順序整個打亂。
        """
        def own_key(project: SpineProject) -> tuple[str, str]:
            return str(project.folder).lower(), project.name.lower()

        cluster_key: dict[int, tuple[str, str]] = {}
        for project in projects:
            cluster_id = clusters[id(project)]
            key = own_key(project)
            if cluster_id not in cluster_key or key < cluster_key[cluster_id]:
                cluster_key[cluster_id] = key

        return lambda p: (cluster_key[clusters[id(p)]], own_key(p))


class ProjectFilterBar(QWidget):
    """篩選鈕 + 可收合的條件面板；條件改動時發出 changed"""

    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ---- 標題列：篩選鈕 + 數量
        row = QHBoxLayout()
        row.setSpacing(6)
        self.filter_button = QPushButton("🔍 篩選")
        self.filter_button.setCheckable(True)
        self.filter_button.setProperty("chip", True)
        self.filter_button.setFixedHeight(24)
        self.filter_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.filter_button.toggled.connect(self._on_toggled)
        row.addWidget(self.filter_button)

        self.count_label = QLabel("")
        self.count_label.setProperty("role", "hint")
        row.addWidget(self.count_label)
        row.addStretch(1)
        layout.addLayout(row)

        # ---- 條件面板（預設收合）
        self.panel = QWidget()
        self.panel.setObjectName("settingsCard")
        self.panel.setVisible(False)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        panel_layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜尋專案名稱或路徑…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._emit_changed)
        search_row.addWidget(self.search_edit, 1)

        self.reset_button = QPushButton("清除")
        self.reset_button.setProperty("chip", True)
        self.reset_button.setFixedHeight(24)
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.clicked.connect(self.reset)
        search_row.addWidget(self.reset_button)
        panel_layout.addLayout(search_row)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.page_size_combo = self._make_combo(PAGE_SIZE_FILTERS)
        self.texture_combo = self._make_combo(TEXTURE_SIZE_FILTERS)
        self.status_combo = self._make_combo(STATUS_FILTERS)
        self.preview_combo = self._make_combo(PREVIEW_FILTERS)
        self.pot_combo = self._make_combo(POT_FILTERS)
        self.shared_combo = self._make_combo(SHARED_FILTERS)
        self.shared_combo.setToolTip("是否與清單上其他專案共用同一張貼圖")
        self.sort_combo = self._make_combo(SORT_OPTIONS)

        rows = (
            ("尺寸", self.page_size_combo, "容量", self.texture_combo),
            ("狀態", self.status_combo, "預覽", self.preview_combo),
            ("規格", self.pot_combo, "共用", self.shared_combo),
            ("排序", self.sort_combo, "", None),
        )
        for index, (label_a, combo_a, label_b, combo_b) in enumerate(rows):
            grid.addWidget(self._label(label_a), index, 0)
            grid.addWidget(combo_a, index, 1)
            if combo_b is None:  # 最後一列只有左半邊（排序）
                continue
            grid.addWidget(self._label(label_b), index, 2)
            grid.addWidget(combo_b, index, 3)
        panel_layout.addLayout(grid)
        layout.addWidget(self.panel)

    # ------------------------------------------------------------ 建構輔助

    def _make_combo(self, options) -> QComboBox:
        combo = QComboBox()
        for name, value in options:
            combo.addItem(name, value)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(6)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        combo.currentIndexChanged.connect(self._emit_changed)
        return combo

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "formLabel")
        label.setFixedWidth(34)
        return label

    # ------------------------------------------------------------ 狀態

    def _all_inputs(self) -> list:
        return [
            self.search_edit,
            self.page_size_combo,
            self.texture_combo,
            self.status_combo,
            self.preview_combo,
            self.pot_combo,
            self.shared_combo,
            self.sort_combo,
        ]

    def criteria(self) -> FilterCriteria:
        return FilterCriteria(
            search=self.search_edit.text(),
            page_size=self.page_size_combo.currentData(),
            texture_min_kb=self.texture_combo.currentData(),
            status=self.status_combo.currentData(),
            preview=self.preview_combo.currentData(),
            pot=self.pot_combo.currentData(),
            shared=self.shared_combo.currentData(),
            sort=self.sort_combo.currentData() or "shared",
        )

    def reset(self) -> None:
        """清除所有條件（含排序）——只發一次 changed"""
        widgets = self._all_inputs()
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.search_edit.clear()
            for combo in widgets[1:]:
                combo.setCurrentIndex(0)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._emit_changed()

    def _on_toggled(self, checked: bool) -> None:
        self.panel.setVisible(checked)

    def _emit_changed(self) -> None:
        count = self.criteria().active_count
        self.filter_button.setText(f"🔍 篩選（{count}）" if count else "🔍 篩選")
        self.changed.emit()

    def set_counts(self, visible: int, total: int) -> None:
        """更新「顯示 N / 共 M」；沒有篩選時只顯示總數"""
        if total == 0:
            self.count_label.setText("")
        elif visible == total:
            self.count_label.setText(f"共 {total} 份專案")
        else:
            self.count_label.setText(f"顯示 {visible} / 共 {total} 份")
