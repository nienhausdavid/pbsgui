"""Tray-Icon: zeigt „Backup läuft“ und „Fehler“, startet ein Backup mit „Jetzt sichern“ und meldet neue Fehler.

Die Zeitpläne laufen unabhängig von der Oberfläche (systemd); das Icon liest nur den lokalen Verlauf und die
Sperrdateien (keine Netzwerkabfrage), deshalb kostet das regelmäßige Nachsehen fast nichts.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.dashboard import Overview
from ..core.manager import ProfileManager

POLL_MS = 15000
COLORS = {"ok": "#16a34a", "running": "#2563eb", "error": "#dc2626", "warning": "#d97706", "none": "#6b7280"}
LABELS = {"ok": "Alles in Ordnung", "running": "Backup läuft", "error": "Letztes Backup fehlgeschlagen",
          "warning": "Warnung", "none": "Noch kein Backup"}


def aggregate(states: list[str]) -> str:
    """Ein Zustand für alle Profile: läuft > Fehler > Warnung > ok > keins."""
    for s in ("running", "error", "warning"):
        if s in states:
            return s
    return "ok" if "ok" in states else "none"


def status_icon(state: str, size: int = 64) -> QIcon:
    """Blaues Sicherungs-Symbol mit farbigem Punkt für den Zustand."""
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#1d4ed8")); p.setPen(QPen(QColor("#ffffff"), size / 20))
    p.drawRoundedRect(size * 0.08, size * 0.08, size * 0.84, size * 0.84, size * 0.18, size * 0.18)
    p.setPen(QPen(QColor("#ffffff"), size / 10))
    c = size / 2
    p.drawLine(int(c), int(size * 0.72), int(c), int(size * 0.28))               # Pfeil nach oben (Upload)
    p.drawLine(int(c), int(size * 0.28), int(size * 0.34), int(size * 0.42))
    p.drawLine(int(c), int(size * 0.28), int(size * 0.66), int(size * 0.42))
    r = size * 0.17
    p.setBrush(QColor(COLORS.get(state, COLORS["none"]))); p.setPen(QPen(QColor("#ffffff"), size / 24))
    p.drawEllipse(int(size - 2 * r - 1), int(size - 2 * r - 1), int(2 * r), int(2 * r))
    p.end()
    return QIcon(pm)


class TrayController(QObject):
    """Verwaltet Icon, Menü und Benachrichtigungen. `window` ist das Hauptfenster (MainWindow)."""

    stateChanged = Signal(str)

    def __init__(self, window, mgr: ProfileManager, parent=None):
        super().__init__(parent)
        self.window, self.mgr = window, mgr
        self.state = "none"
        self.overviews: list[Overview] = []
        self._seen_failures: dict[str, str] = {}
        self._first = True
        self.tray = QSystemTrayIcon(status_icon("none"), self)
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self.show_history)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.setInterval(POLL_MS)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.poll()
        if self.available:
            self.tray.show()
            self.timer.start()

    # -- Zustand ------------------------------------------------------------
    def poll(self) -> None:
        """Liest Verlauf und Sperren neu (lokal, kein Netzwerk) und aktualisiert Icon, Hinweistext und Menü."""
        try:
            profiles = self.mgr.profiles()
            ovs = [self.mgr.overview(p, online=False) for p in profiles]
        except Exception:
            return
        busy = getattr(self.window, "running_profiles", set())
        for o in ovs:
            if o.profile.slug in busy or o.profile.name in busy:
                o.running = True
        self.overviews = ovs
        self.state = aggregate([o.state for o in ovs])
        self._notify_new_failures(ovs)
        self.tray.setIcon(status_icon(self.state))
        self.tray.setToolTip(self.tooltip())
        self._build_menu()
        self.stateChanged.emit(self.state)

    def tooltip(self) -> str:
        lines = [f"pbsgui – {LABELS[self.state]}"]
        for o in self.overviews:
            lines.append(f"{o.profile.name}: " + ("läuft" if o.running else
                         ("fehlgeschlagen" if o.state == "error" else "ok" if o.state == "ok" else "–")))
        return "\n".join(lines)

    def _notify_new_failures(self, ovs: list[Overview]) -> None:
        for o in ovs:
            run = o.last_run
            if run is None or run.ok:
                self._seen_failures.pop(o.profile.slug, None)
                continue
            if self._seen_failures.get(o.profile.slug) != run.id:
                self._seen_failures[o.profile.slug] = run.id
                if not self._first and self.available:      # beim Start nur merken, nicht alte Fehler melden
                    self.tray.showMessage(f"Backup fehlgeschlagen: {o.profile.name}", run.message or f"Exitcode {run.code}",
                                          QSystemTrayIcon.MessageIcon.Critical, 15000)
        self._first = False

    # -- Menü ---------------------------------------------------------------
    def _build_menu(self) -> None:
        m = self.menu
        m.clear()
        head = QAction(LABELS[self.state], m); head.setEnabled(False); m.addAction(head)
        m.addSeparator()
        m.addAction("Fenster öffnen", self.show_window)
        sub = m.addMenu("Jetzt sichern")
        for o in self.overviews:
            act = sub.addAction(o.profile.name)
            act.setEnabled(not o.running)
            act.triggered.connect(lambda _c=False, n=o.profile.name: self.backup_now(n))
        if not self.overviews:
            sub.setEnabled(False)
        m.addAction("Verlauf anzeigen", self.show_history)
        m.addAction("Überblick anzeigen", self.show_dashboard)
        m.addSeparator()
        auto = m.addAction("Beim Anmelden im Hintergrund starten"); auto.setCheckable(True); auto.setChecked(autostart_enabled())
        auto.toggled.connect(set_autostart)
        bg = m.addAction("Beim Schließen im Hintergrund weiterlaufen"); bg.setCheckable(True); bg.setChecked(background_enabled())
        bg.toggled.connect(set_background)
        m.addSeparator()
        m.addAction("Beenden", self.quit)

    # -- Aktionen -----------------------------------------------------------
    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        w = self.window
        w.showNormal() if w.isMinimized() else w.show()
        w.raise_(); w.activateWindow()

    def show_history(self) -> None:
        self.show_window(); self.window.show_history()

    def show_dashboard(self) -> None:
        self.show_window(); self.window.show_dashboard()

    def backup_now(self, name: str) -> None:
        self.show_window()
        self.window.start_backup_of(name)
        QTimer.singleShot(1500, self.poll)

    def quit(self) -> None:
        self.window.allow_close = True
        self.tray.hide()
        self.window.close()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()


# -- Einstellungen ---------------------------------------------------------------------------------------------
def background_enabled() -> bool:
    return str(QSettings("pbsgui", "pbsgui").value("tray/background", "false")).lower() == "true"


def set_background(on: bool) -> None:
    QSettings("pbsgui", "pbsgui").setValue("tray/background", "true" if on else "false")


def autostart_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "pbsgui-tray.desktop"


def autostart_enabled() -> bool:
    return autostart_path().exists()


def set_autostart(on: bool) -> None:
    """Legt einen XDG-Autostart-Eintrag an oder entfernt ihn: die Oberfläche startet beim Anmelden nur als Tray-Icon."""
    f = autostart_path()
    try:
        if on:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("[Desktop Entry]\nType=Application\nName=pbsgui (Hintergrund)\n"
                         f"Exec={shutil.which('pbsgui') or 'pbsgui'} tray\n"
                         "Icon=pbsgui\nX-GNOME-Autostart-enabled=true\nTerminal=false\n")
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass
