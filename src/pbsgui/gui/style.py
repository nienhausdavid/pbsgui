"""Aussehen: heller und dunkler Modus mit hohem Kontrast (Text mindestens 7:1, Bedienelemente mindestens 4,5:1).

Alle Farben stehen in `THEMES`; Palette und Stylesheet entstehen daraus, damit auch Dialoge und Standardfenster
(Dateiauswahl, Meldungen) dieselben Farben haben. Der Modus ist „system“ (folgt dem Desktop), „light“ oder „dark“
und wird in QSettings gemerkt.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication
from ..i18n import tr

MODES = ("system", "light", "dark")
MODE_LABELS = {"system": tr("System"), "light": tr("Hell"), "dark": tr("Dunkel")}

THEMES = {
    "light": {
        "bg": "#eef0f4", "surface": "#ffffff", "alt": "#f3f5f9", "text": "#0f172a", "muted": "#475569",
        "border": "#6b7587", "soft": "#c3cad8", "accent": "#1d4ed8", "accent_hover": "#1e40af", "accent_text": "#ffffff",
        "button": "#e2e7ef", "button_hover": "#d3dae6", "disabled": "#6b7585", "danger": "#b91c1c",
        "titlebar": "#dde2ec", "tab_sel": "#1d4ed8",
    },
    "dark": {
        "bg": "#14171c", "surface": "#1c2027", "alt": "#222731", "text": "#f1f5f9", "muted": "#b6c0d0",
        "border": "#6b7686", "soft": "#394150", "accent": "#3b82f6", "accent_hover": "#5b9bff", "accent_text": "#0b1220",
        "button": "#2b3240", "button_hover": "#363f50", "disabled": "#8a94a6", "danger": "#ff7b7b",
        "titlebar": "#0f1216", "tab_sel": "#6aa3ff",
    },
}

STYLE = """
* {{ font-size: 10.5pt; }}
QMainWindow, QDialog, QWidget#shell {{ background: {bg}; color: {text}; }}
QFrame#frame {{ background: {bg}; border: 1px solid {border}; }}
QLabel {{ color: {text}; background: transparent; }}
QLabel#pageTitle {{ font-size: 17pt; font-weight: 600; }}
QLabel#sectionTitle {{ font-weight: 700; color: {muted}; font-size: 9pt; letter-spacing: 1px; }}
QLabel#muted {{ color: {muted}; }}
QCheckBox, QRadioButton {{ color: {text}; spacing: 8px; }}
QCheckBox:disabled, QLabel:disabled {{ color: {disabled}; }}
QCheckBox::indicator, QListWidget::indicator {{ width: 16px; height: 16px; border: 2px solid {border}; border-radius: 4px; background: {surface}; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {accent}; border-color: {accent}; image: url("{check}"); }}
QCheckBox::indicator:disabled, QListWidget::indicator:disabled {{ border-color: {soft}; background: {alt}; }}
QGroupBox {{ border: 1px solid {border}; border-radius: 8px; margin-top: 14px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; font-weight: 700; color: {text}; background: {bg}; }}
QPushButton {{ padding: 6px 14px; border-radius: 6px; border: 1px solid {border}; background: {button}; color: {text}; }}
QPushButton:hover {{ background: {button_hover}; }}
QPushButton:disabled {{ color: {disabled}; border-color: {soft}; background: {alt}; }}
QPushButton#primary {{ background: {accent}; color: {accent_text}; border-color: {accent}; font-weight: 700; }}
QPushButton#primary:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#primary:disabled {{ background: {soft}; color: {disabled}; border-color: {soft}; }}
QLineEdit, QPlainTextEdit, QComboBox, QListWidget, QTableWidget, QTreeWidget, QTextEdit, QSpinBox {{
    border: 1px solid {border}; border-radius: 6px; padding: 4px 6px; background: {surface}; color: {text};
    selection-background-color: {accent}; selection-color: {accent_text}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border: 2px solid {accent}; }}
QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled {{ color: {disabled}; background: {alt}; }}
QComboBox QAbstractItemView {{ background: {surface}; color: {text}; border: 1px solid {border};
    selection-background-color: {accent}; selection-color: {accent_text}; }}
QListWidget#profiles {{ padding: 4px; outline: 0; }}
QListWidget#profiles::item {{ padding: 8px 10px; border-radius: 6px; }}
QListWidget#profiles::item:selected {{ background: {accent}; color: {accent_text}; }}
QListWidget::item:selected, QTableWidget::item:selected, QTreeWidget::item:selected {{ background: {accent}; color: {accent_text}; }}
QPlainTextEdit#log {{ font-family: monospace; font-size: 9.5pt; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {border}; }}
QTabBar::tab {{ padding: 8px 18px; border: none; border-bottom: 3px solid transparent; color: {muted}; background: transparent; }}
QTabBar::tab:selected {{ border-bottom-color: {tab_sel}; font-weight: 700; color: {text}; }}
QTabBar::tab:hover:!selected {{ border-bottom-color: {border}; color: {text}; }}
QProgressBar {{ border: 1px solid {border}; border-radius: 6px; text-align: center; height: 18px; background: {surface}; color: {text}; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}
QHeaderView::section {{ padding: 6px 8px; border: none; border-bottom: 1px solid {border}; background: {bg}; color: {text}; font-weight: 700; }}
QTableWidget {{ alternate-background-color: {alt}; gridline-color: {soft}; }}
QSplitter::handle {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ width: 12px; background: transparent; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ height: 12px; background: transparent; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; margin: 2px; }}
QStatusBar {{ color: {muted}; background: {bg}; }}
QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border}; padding: 4px; }}
QMenu {{ background: {surface}; color: {text}; border: 1px solid {border}; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background: {accent}; color: {accent_text}; }}
QWidget#titlebar {{ background: {titlebar}; border-bottom: 1px solid {border}; }}
QLabel#apptitle {{ font-weight: 700; }}
QToolButton#tbtn {{ border: none; border-radius: 6px; padding: 0; min-width: 40px; min-height: 30px; background: transparent; }}
QToolButton#tbtn:hover {{ background: {button_hover}; }}
QToolButton#tbtnClose:hover {{ background: #c42b1c; }}
"""


def effective(mode: str) -> str:
    """Löst „system“ in „light“ oder „dark“ auf (Qt 6.5+: Farbschema des Desktops)."""
    if mode in ("light", "dark"):
        return mode
    try:
        scheme = QApplication.styleHints().colorScheme()
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"
    except Exception:
        return "light"


def palette(theme: str) -> QPalette:
    t = THEMES[theme]
    p = QPalette()
    c = QColor
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
                      (QPalette.ColorRole.AlternateBase, "alt"), (QPalette.ColorRole.Button, "button"),
                      (QPalette.ColorRole.ToolTipBase, "surface"), (QPalette.ColorRole.Highlight, "accent")):
        p.setColor(role, c(t[key]))
    for role, key in ((QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Text, "text"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.ToolTipText, "text"),
                      (QPalette.ColorRole.HighlightedText, "accent_text"), (QPalette.ColorRole.PlaceholderText, "muted"),
                      (QPalette.ColorRole.Link, "accent")):
        p.setColor(role, c(t[key]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(t["disabled"]))
    return p


def current_mode() -> str:
    m = QSettings("pbsgui", "pbsgui").value("theme", "system")
    return m if m in MODES else "system"


def _check_icon(theme: str) -> str:
    """Zeichnet das Häkchen (Farbe passend zur Akzentfläche) und liefert den Dateipfad für das Stylesheet."""
    d = Path(tempfile.gettempdir()) / f"pbsgui-style-{Path.home().name}"
    d.mkdir(exist_ok=True)
    f = d / f"check-{theme}.png"
    pm = QPixmap(32, 32); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(THEMES[theme]["accent_text"]), 4); pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPointF(8, 17), QPointF(14, 23), QPointF(24, 10)])
    p.end()
    pm.save(str(f))
    return f.as_posix()


def apply(app: QApplication, mode: str | None = None) -> str:
    """Wendet den Modus an und gibt das tatsächlich verwendete Thema („light“/„dark“) zurück."""
    mode = mode if mode in MODES else current_mode()
    theme = effective(mode)
    app.setStyle("Fusion")
    app.setPalette(palette(theme))
    app.setStyleSheet(STYLE.format(check=_check_icon(theme), **THEMES[theme]))
    app.setProperty("pbsgui_theme", theme)
    return theme


def save_mode(mode: str) -> None:
    QSettings("pbsgui", "pbsgui").setValue("theme", mode)
