"""Eigene Titelleiste (Minimieren, Maximieren, Schließen, Farbmodus) und Rahmen zum Verschieben und Vergrößern.

GNOME zeigt Minimieren und Maximieren nur auf Wunsch an; deshalb zeichnet pbsgui die Leiste selbst. Verschieben und
Größe ändern übergibt Qt an den Compositor (startSystemMove/startSystemResize, funktioniert unter Wayland und X11).
Mit der Umgebungsvariable PBSGUI_NATIVE_FRAME=1 bleibt der Rahmen des Systems.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

EDGE = 6          # Breite des Greifbereichs zum Vergrößern in Pixeln


def _icon(kind: str, color: QColor, size: int = 16) -> QIcon:
    pm = QPixmap(size * 2, size * 2); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(2, 2)
    pen = QPen(color, 1.4); pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    c = size / 2
    if kind == "min":
        p.drawLine(QPointF(4, c + 3), QPointF(size - 4, c + 3))
    elif kind == "max":
        p.drawRoundedRect(QRectF(4, 4, size - 8, size - 8), 1.5, 1.5)
    elif kind == "restore":
        p.drawRoundedRect(QRectF(4, 6, size - 10, size - 10), 1.5, 1.5)
        p.drawPolyline([QPointF(6, 4), QPointF(size - 4, 4), QPointF(size - 4, size - 6)])
    elif kind == "close":
        p.drawLine(QPointF(4.5, 4.5), QPointF(size - 4.5, size - 4.5)); p.drawLine(QPointF(size - 4.5, 4.5), QPointF(4.5, size - 4.5))
    elif kind == "sun":
        p.drawEllipse(QPointF(c, c), 2.8, 2.8)
        import math
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(c + 4.6 * math.cos(a), c + 4.6 * math.sin(a)), QPointF(c + 6.4 * math.cos(a), c + 6.4 * math.sin(a)))
    elif kind == "moon":
        path = QPainterPath(); path.addEllipse(QPointF(c, c), 5.5, 5.5)
        cut = QPainterPath(); cut.addEllipse(QPointF(c + 3, c - 2.5), 5, 5)
        p.setBrush(color); p.drawPath(path.subtracted(cut))
    elif kind == "auto":      # halb hell, halb dunkel
        p.drawEllipse(QPointF(c, c), 5.5, 5.5)
        half = QPainterPath(); half.moveTo(c, c - 5.5); half.arcTo(QRectF(c - 5.5, c - 5.5, 11, 11), 90, -180); half.closeSubpath()
        p.setBrush(color); p.drawPath(half)
    p.end()
    return QIcon(pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class TitleBar(QWidget):
    """Leiste oben: Name links, rechts Farbmodus, Minimieren, Maximieren/Wiederherstellen, Schließen."""
    modeChosen = Signal(str)

    def __init__(self, window, title: str, modes: dict[str, str], parent=None):
        super().__init__(parent)
        self.setObjectName("titlebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._win = window
        lay = QHBoxLayout(self); lay.setContentsMargins(12, 2, 4, 2); lay.setSpacing(2)
        self.title = QLabel(title); self.title.setObjectName("apptitle")
        lay.addWidget(self.title); lay.addStretch()
        self.theme_btn = self._button("", "Farbmodus: System, Hell oder Dunkel")
        menu = QMenu(self)
        for key, label in modes.items():
            menu.addAction(label, lambda k=key: self.modeChosen.emit(k))
        self.theme_btn.setMenu(menu); self.theme_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.min_btn = self._button("min", "Minimieren"); self.max_btn = self._button("max", "Maximieren")
        self.close_btn = self._button("close", "Schließen"); self.close_btn.setObjectName("tbtnClose")
        self.min_btn.clicked.connect(window.showMinimized)
        self.max_btn.clicked.connect(self.toggle_max)
        self.close_btn.clicked.connect(window.close)
        for b in (self.theme_btn, self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)
        self._kinds = {self.min_btn: "min", self.close_btn: "close"}
        self.setFixedHeight(38)

    def _button(self, kind: str, tip: str) -> QToolButton:
        b = QToolButton(self); b.setObjectName("tbtn"); b.setToolTip(tip); b.setAutoRaise(True)
        b.setIconSize(QSize(16, 16)); b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setProperty("kind", kind)
        return b

    def refresh_icons(self, theme: str, mode: str) -> None:
        """Symbole in der Textfarbe des Themas neu zeichnen und das Maximieren-Symbol dem Zustand anpassen."""
        col = QColor("#f1f5f9" if theme == "dark" else "#0f172a")
        self.min_btn.setIcon(_icon("min", col))
        self.max_btn.setIcon(_icon("restore" if self._win.isMaximized() else "max", col))
        self.max_btn.setToolTip("Wiederherstellen" if self._win.isMaximized() else "Maximieren")
        self.close_btn.setIcon(_icon("close", col))
        self.theme_btn.setIcon(_icon({"system": "auto", "light": "sun", "dark": "moon"}[mode], col))

    def toggle_max(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    # Ziehen verschiebt das Fenster, Doppelklick maximiert
    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and self._win.windowHandle():
            self._win.windowHandle().startSystemMove()
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.toggle_max()


class Shell(QWidget):
    """Zentrales Widget mit schmalem Rand, an dem das Fenster vergrößert werden kann (Cursor und Compositor-Resize)."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.setObjectName("shell")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self._win = window

    def set_margins(self, maximized: bool) -> None:
        m = 0 if maximized else EDGE
        self.layout().setContentsMargins(m, m, m, m)

    def _edges(self, pos) -> Qt.Edge:
        if self._win.isMaximized() or self._win.isFullScreen():
            return Qt.Edge(0)
        e = Qt.Edge(0); w, h = self.width(), self.height()
        if pos.x() <= EDGE: e |= Qt.Edge.LeftEdge
        if pos.x() >= w - EDGE: e |= Qt.Edge.RightEdge
        if pos.y() <= EDGE: e |= Qt.Edge.TopEdge
        if pos.y() >= h - EDGE: e |= Qt.Edge.BottomEdge
        return e

    def mouseMoveEvent(self, ev) -> None:
        e = self._edges(ev.position().toPoint())
        L, R, T, B = Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge
        if e in (L | T, R | B):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif e in (R | T, L | B):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif e in (L, R):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif e in (T, B):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, ev) -> None:
        e = self._edges(ev.position().toPoint())
        if e and ev.button() == Qt.MouseButton.LeftButton and self._win.windowHandle():
            self._win.windowHandle().startSystemResize(e)
            ev.accept()
        else:
            super().mousePressEvent(ev)
