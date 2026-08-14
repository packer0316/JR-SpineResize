"""處理進度對話框"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


class ProgressDialog(QDialog):
    """顯示批次進度，可中止"""

    cancelled = pyqtSignal()

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 16)

        self.heading = QLabel(title)
        self.heading.setProperty("role", "heading")
        layout.addWidget(self.heading)

        self.detail = QLabel("準備中…")
        self.detail.setProperty("role", "hint")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)

        row = QHBoxLayout()
        self.counter = QLabel("")
        self.counter.setProperty("role", "hint")
        row.addWidget(self.counter)
        row.addStretch(1)
        self.cancel_button = QPushButton("中止")
        self.cancel_button.clicked.connect(self._on_cancel)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

    def update_progress(self, current: int, total: int, message: str) -> None:
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(current, total))
            self.counter.setText(f"{min(current, total)} / {total}")
        self.detail.setText(message)

    def _on_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("中止中…")
        self.cancelled.emit()
