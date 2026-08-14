"""處理結果報告"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from core.pipeline import BatchResult
from core.validator import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARNING
from utils.file_utils import format_bytes

_COLUMNS = ("資產", "結果", "頁面尺寸", "貼圖大小", "貼圖編碼", "精確區塊", "最大偏移")
_LEVEL_COLOUR = {LEVEL_ERROR: "#dc2626", LEVEL_WARNING: "#d97706", LEVEL_INFO: "#6b7280"}


class ReportDialog(QDialog):
    """處理完成後的總結：每份資產一列，下方列出所有訊息"""

    def __init__(self, batch: BatchResult, skipped: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._batch = batch
        self._skipped = skipped or []
        self.setWindowTitle("處理報告")
        self.setMinimumSize(880, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(self._build_summary())
        if self._skipped:
            skipped_label = QLabel(
                f"略過 {len(self._skipped)} 份未套用設定的專案："
                f"{'、'.join(self._skipped[:6])}{' …' if len(self._skipped) > 6 else ''}"
            )
            skipped_label.setProperty("role", "hint")
            skipped_label.setWordWrap(True)
            layout.addWidget(skipped_label)
        layout.addWidget(self._build_table(), 3)
        layout.addWidget(QLabel("訊息"))
        layout.addWidget(self._build_messages(), 2)
        layout.addLayout(self._build_buttons())

    # ------------------------------------------------------------ 區塊

    def _build_summary(self) -> QLabel:
        batch = self._batch
        ok = len(batch.succeeded)
        failed = len(batch.failed)
        saved = (
            f"{format_bytes(batch.src_bytes)} → {format_bytes(batch.dst_bytes)}"
            f"（{_delta_text(batch.src_bytes, batch.dst_bytes)}）"
            if batch.src_bytes
            else "—"
        )
        worst_px = max((r.report.max_drift_px for r in batch.results), default=0.0)

        text = (
            f"<b>完成 {ok} 份</b>"
            + (f"　<span style='color:#dc2626'><b>失敗 {failed} 份</b></span>" if failed else "")
            + f"　　貼圖總量 {saved}"
            + f"　　最大幾何偏移 {worst_px:.2f} 原始像素"
        )
        label = QLabel(text)
        label.setProperty("role", "heading")
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    def _build_table(self) -> QTableWidget:
        batch = self._batch
        table = QTableWidget(len(batch.results), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        for row, result in enumerate(batch.results):
            report = result.report
            if result.error:
                status, colour = f"失敗：{result.error[:50]}", "#dc2626"
            elif report.errors:
                status, colour = f"未輸出（{len(report.errors)} 項錯誤）", "#dc2626"
            elif report.warnings:
                status, colour = f"完成（{len(report.warnings)} 項警告）", "#d97706"
            else:
                status, colour = "完成", "#16a34a"

            sizes = " / ".join(
                f"{p.src_size[0]}x{p.src_size[1]}→{p.dst_size[0]}x{p.dst_size[1]}"
                for p in result.pages
            )
            bytes_text = (
                f"{format_bytes(result.src_bytes)} → {format_bytes(result.dst_bytes)}"
                f"（{_delta_text(result.src_bytes, result.dst_bytes)}）"
                if result.src_bytes
                else "—"
            )
            encodings = sorted({p.encoding for p in result.pages if p.encoding})
            values = (
                result.asset.name,
                status,
                sizes or "—",
                bytes_text,
                "、".join(encodings) or "—",
                f"{report.exact_regions}/{report.total_regions}" if report.total_regions else "—",
                f"{report.max_drift_px:.2f} px" if report.total_regions else "—",
            )
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setForeground(QColor(colour))
                if col == 0 and result.atlas_out:
                    item.setToolTip(str(result.atlas_out))
                table.setItem(row, col, item)

        return table

    def _build_messages(self) -> QTextEdit:
        view = QTextEdit()
        view.setReadOnly(True)
        lines: list[str] = []
        for result in self._batch.results:
            issues = result.report.sorted_issues()
            if not issues:
                continue
            lines.append(f"<b>{result.asset.name}</b>")
            for issue in issues:
                colour = _LEVEL_COLOUR[issue.level]
                lines.append(
                    f"<span style='color:{colour}'>&nbsp;&nbsp;{issue.icon}</span> "
                    f"{_escape(issue.message)}"
                )
            lines.append("")
        view.setHtml("<br>".join(lines) if lines else "<i>沒有任何訊息，全部順利完成。</i>")
        return view

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        open_button = QPushButton("開啟輸出資料夾")
        open_button.clicked.connect(self._open_output)
        open_button.setEnabled(any(r.atlas_out for r in self._batch.results))
        row.addWidget(open_button)
        row.addStretch(1)
        close = QPushButton("關閉")
        close.setProperty("role", "primary")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        return row

    def _open_output(self) -> None:
        for result in self._batch.results:
            if result.atlas_out:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.atlas_out.parent)))
                return


def _delta_text(before: int, after: int) -> str:
    """檔案大小的增減。變大時要明講「增加」，不要顯示成「省 -68%」。"""
    if not before:
        return "—"
    ratio = after / before
    return f"省 {1 - ratio:.0%}" if ratio <= 1 else f"增加 {ratio - 1:.0%}"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
