"""Einheitliches Aussehen: Fusion-Stil und ein Stylesheet, das nur Palette-Farben nutzt (hell und dunkel)."""
from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

STYLE = """
* { font-size: 10.5pt; }
QMainWindow, QDialog { background: palette(window); }
QLabel#pageTitle { font-size: 17pt; font-weight: 600; }
QLabel#sectionTitle { font-weight: 600; color: palette(mid); text-transform: uppercase; font-size: 9pt; }
QLabel#muted { color: palette(placeholder-text); }
QGroupBox { border: 1px solid palette(midlight); border-radius: 8px; margin-top: 14px; padding: 12px 10px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; font-weight: 600; }
QPushButton { padding: 6px 14px; border-radius: 6px; border: 1px solid palette(mid); background: palette(button); }
QPushButton:hover { background: palette(light); }
QPushButton:disabled { color: palette(placeholder-text); border-color: palette(midlight); }
QPushButton#primary { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); font-weight: 600; }
QPushButton#primary:hover { background: palette(highlight); border-color: palette(text); }
QPushButton#primary:disabled { background: palette(midlight); color: palette(placeholder-text); border-color: palette(midlight); }
QLineEdit, QPlainTextEdit, QComboBox, QListWidget, QTableWidget { border: 1px solid palette(mid); border-radius: 6px; padding: 4px 6px; background: palette(base); }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: palette(highlight); }
QListWidget#profiles { padding: 4px; outline: 0; }
QListWidget#profiles::item { padding: 8px 10px; border-radius: 6px; }
QListWidget#profiles::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QPlainTextEdit#log { font-family: monospace; font-size: 9.5pt; }
QTabWidget::pane { border: none; border-top: 1px solid palette(midlight); }
QTabBar::tab { padding: 8px 18px; border: none; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { border-bottom-color: palette(highlight); font-weight: 600; }
QTabBar::tab:hover:!selected { border-bottom-color: palette(mid); }
QProgressBar { border: 1px solid palette(mid); border-radius: 6px; text-align: center; height: 18px; background: palette(base); }
QProgressBar::chunk { background: palette(highlight); border-radius: 5px; }
QHeaderView::section { padding: 6px 8px; border: none; border-bottom: 1px solid palette(mid); background: palette(window); font-weight: 600; }
QSplitter::handle { background: transparent; }
QScrollArea { background: transparent; }
QStatusBar { color: palette(placeholder-text); }
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
