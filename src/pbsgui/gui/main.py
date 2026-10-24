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

    def follow_system(*_):   # Farbwechsel des Desktops übernehmen, solange der Modus „System“ ist
        if style.current_mode() == "system":
            style.apply(app, "system")
            win._refresh_chrome()
    try:
        app.styleHints().colorSchemeChanged.connect(follow_system)
    except AttributeError:
        pass
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
