"""處理後檔案大小的估算結果"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PageEstimate:
    """單張貼圖的估算"""

    src_path: Path          # 來源貼圖（已 resolve，用於跨專案去重）
    src_bytes: int
    est_bytes: int
    src_size: tuple[int, int]
    dst_size: tuple[int, int]

    @property
    def name(self) -> str:
        return self.src_path.name


@dataclass
class SizeEstimate:
    """一份專案的估算；fingerprint 用來判斷設定是否已經改過"""

    fingerprint: tuple = ()
    pages: list[PageEstimate] = field(default_factory=list)

    @property
    def src_total(self) -> int:
        return sum(p.src_bytes for p in self.pages)

    @property
    def est_total(self) -> int:
        return sum(p.est_bytes for p in self.pages)

    @property
    def delta_bytes(self) -> int:
        return self.est_total - self.src_total

    def page(self, name: str) -> PageEstimate | None:
        return next((p for p in self.pages if p.name == name), None)


def aggregate_estimates(estimates: Iterable[SizeEstimate]) -> tuple[int, int]:
    """
    跨專案合計（原始, 處理後）。

    多份 atlas / 多個專案共用同一張貼圖是常見作法（實測素材中就有兩個 .skel
    指向同一張 PNG），處理時也只會寫出一次，所以合計必須以來源路徑去重，
    否則會高估節省的容量。
    """
    seen: set[Path] = set()
    src_total = est_total = 0
    for estimate in estimates:
        for page in estimate.pages:
            if page.src_path in seen:
                continue
            seen.add(page.src_path)
            src_total += page.src_bytes
            est_total += page.est_bytes
    return src_total, est_total
