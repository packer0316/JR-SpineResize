"""
JR-SpineResize — Spine 貼圖等比縮放工具

程式進入點
"""
import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from config.constants import APP_NAME
from ui.main_window import MainWindow
from utils.resource_utils import get_app_icon_path


def _set_windows_app_id() -> None:
    """設定 Windows AppUserModelID，讓工作列使用本程式圖示而非預設圖示"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JR.SpineResize.App")
    except Exception:
        pass  # 設定失敗不影響程式運作


def main() -> None:
    _set_windows_app_id()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    icon_path = get_app_icon_path()
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
