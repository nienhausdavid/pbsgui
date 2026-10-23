from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import style
from .window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("pbsgui")
    app.setDesktopFileName("pbsgui")  # Wayland: ordnet Fenster dem Starter und Icon zu
    style.apply(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
