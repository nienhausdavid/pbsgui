from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import style
from .tray import TrayController
from .window import MainWindow


def _install_qt_translation(app: QApplication) -> None:
    """Qts eigene Texte (Ja/Nein/Abbrechen, Dateiauswahl) in der Sprache der Oberfläche; fehlen die Dateien, bleibt es Englisch."""
    from PySide6.QtCore import QLibraryInfo, QTranslator
    from ..i18n import language
    lang = language()
    if lang == "en":
        return
    tr_ = QTranslator(app)
    if tr_.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(tr_)


def main(start_hidden: bool = False) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("pbsgui")
    app.setDesktopFileName("pbsgui")  # Wayland: ordnet Fenster dem Starter und Icon zu
    _install_qt_translation(app)
    style.apply(app)
    app.setQuitOnLastWindowClosed(False)   # das Fenster kann im Hintergrund weiterlaufen; MainWindow beendet das Programm selbst
    win = MainWindow()
    win.tray = TrayController(win, win.mgr)

    def follow_system(*_):   # Farbwechsel des Desktops übernehmen, solange der Modus „System“ ist
        if style.current_mode() == "system":
            style.apply(app, "system")
            win._refresh_chrome()
    try:
        app.styleHints().colorSchemeChanged.connect(follow_system)
    except AttributeError:
        pass
    if not (start_hidden and win.tray.available):
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
