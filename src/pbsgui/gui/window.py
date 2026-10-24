"""Hauptfenster: Profilliste und Tabs Backup, Snapshots, Zeitplan."""
from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGroupBox, QHeaderView, QProgressBar, QCheckBox, QComboBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QApplication, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ..core.manager import ProfileManager, can_use_admin
from ..core.modes import DEFAULT_FULL_EXCLUDES, FULL
from ..core.recovery import needs_key_export, render_sheet
from ..core.models import LEGACY, METADATA, Profile
from ..core.paths import Paths
from ..core.runner import read_status
from ..core.scheduler import enable_linger, linger_enabled
from .add_server import AddServerDialog
from .db_dialog import DbDumpDialog
from .files_dialog import FilesDialog
from .mounts_widget import MountList
from . import style
from .titlebar import Shell, TitleBar
from .workers import Worker

PRESETS = [("Vorlage wählen …", ""), ("Täglich 02:00", "*-*-* 02:00:00"), ("Stündlich", "hourly"), ("Wöchentlich (Mo 03:00)", "Mon *-*-* 03:00:00"),
           ("Beim Start + täglich", "daily"), ("Eigener Ausdruck", "")]


def fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class MainWindow(QMainWindow):
    def __init__(self, manager: ProfileManager | None = None):
        super().__init__()
        from .. import __version__
        self.setWindowTitle(f"pbsgui {__version__} – Proxmox Backup")
        self.setMinimumSize(640, 440)             # klein genug zum bequemen Verkleinern (Inhalt scrollt)
        self.resize(1080, 720)
        self.mgr = manager or ProfileManager()
        self.admin_ok = can_use_admin()
        self.current: Profile | None = None
        self._workers: set[Worker] = set()

        self.plist = QListWidget(); self.plist.setObjectName("profiles")
        add = QPushButton("＋  Server hinzufügen"); add.setObjectName("primary"); add.clicked.connect(self.add_server)
        rem = QPushButton("Entfernen"); rem.clicked.connect(self.remove_profile)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(12, 12, 6, 12); ll.setSpacing(8)
        title = QLabel("Profile"); title.setObjectName("sectionTitle")
        ll.addWidget(title); ll.addWidget(self.plist, 1); ll.addWidget(add); ll.addWidget(rem)
        left.setMinimumWidth(180)

        self.head = QLabel(""); self.head.setObjectName("pageTitle")
        self.subhead = QLabel(""); self.subhead.setObjectName("muted"); self.subhead.setWordWrap(True)
        self.tabs = QTabWidget(); self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_backup_tab(), "Backup")
        self.tabs.addTab(self._build_snapshots_tab(), "Snapshots")
        self.tabs.addTab(self._build_schedule_tab(), "Zeitplan")
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(6, 12, 12, 8); rl.setSpacing(2)
        rl.addWidget(self.head); rl.addWidget(self.subhead); rl.addSpacing(6); rl.addWidget(self.tabs, 1)

        split = QSplitter(); split.addWidget(left); split.addWidget(right)
        split.setStretchFactor(0, 0); split.setStretchFactor(1, 1); split.setSizes([230, 850])
        split.setChildrenCollapsible(False); split.setHandleWidth(3)
        self._native = bool(os.environ.get("PBSGUI_NATIVE_FRAME"))
        if self._native:
            self.setCentralWidget(split); self.titlebar = None
        else:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)   # eigene Titelleiste mit Fensterknöpfen
            shell = Shell(self); sl = QVBoxLayout(shell); sl.setSpacing(0)
            frame = QFrame(); frame.setObjectName("frame"); fl = QVBoxLayout(frame); fl.setContentsMargins(0, 0, 0, 0); fl.setSpacing(0)
            self.titlebar = TitleBar(self, f"pbsgui {__version__}", style.MODE_LABELS)
            self.titlebar.modeChosen.connect(self.set_theme_mode)
            fl.addWidget(self.titlebar); fl.addWidget(split, 1)
            sl.addWidget(frame); self._shell = shell
            shell.set_margins(False)
            self.setCentralWidget(shell)
        self.statusBar().showMessage("Bereit")
        self.plist.currentRowChanged.connect(self._select)
        self.reload()
        geo = QSettings("pbsgui", "pbsgui").value("geometry")   # Fenstergröße und -lage merken
        if geo:
            self.restoreGeometry(geo)

    def set_theme_mode(self, mode: str) -> None:
        """Farbmodus wählen (system/hell/dunkel), anwenden und merken."""
        style.save_mode(mode)
        style.apply(QApplication.instance(), mode)
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        if self.titlebar is not None:
            theme = QApplication.instance().property("pbsgui_theme") or "light"
            self.titlebar.refresh_icons(theme, style.current_mode())
            self._shell.set_margins(self.isMaximized())

    def changeEvent(self, ev) -> None:
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.WindowStateChange:
            self._refresh_chrome()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._refresh_chrome()

    def closeEvent(self, ev) -> None:
        QSettings("pbsgui", "pbsgui").setValue("geometry", self.saveGeometry())
        super().closeEvent(ev)

    # -- Hilfen -------------------------------------------------------------
    def run_async(self, fn, on_done=None, on_line=None) -> Worker:
        w = Worker(fn, self)
        self._workers.add(w)
        w.finished.connect(lambda: self._workers.discard(w))
        if on_done:
            w.done.connect(on_done)
        if on_line:
            w.line.connect(on_line)
        w.failed.connect(lambda m: (self.statusBar().showMessage(f"Fehler: {m}"),
                                    QMessageBox.warning(self, "Fehler", m)))
        w.start()
        return w

    def reload(self, select: str | None = None) -> None:
        self.plist.blockSignals(True)
        self.plist.clear()
        profiles = self.mgr.profiles()
        for p in profiles:
            self.plist.addItem(p.name)
        names = [p.name for p in profiles]
        self.plist.setCurrentRow(names.index(select) if select in names else (0 if names else -1))
        self.plist.blockSignals(False)      # erst jetzt wieder melden, sonst wird zweimal geladen
        self._select(self.plist.currentRow())

    def _select(self, row: int) -> None:
        profiles = self.mgr.profiles()
        self.current = profiles[row] if 0 <= row < len(profiles) else None
        for i in range(self.tabs.count()):
            self.tabs.widget(i).setEnabled(self.current is not None)
        p = self.current
        if not p:
            self.head.setText("Kein Profil"); self.subhead.setText("Mit „Server hinzufügen“ ein Backup-Ziel einrichten.")
            return
        full = p.backup_mode == FULL
        self.head.setText(p.name)
        self.subhead.setText(f"{p.server} · {p.datastore}{'/' + p.namespace if p.namespace else ''} · "
                             f"{'Ganzes System' if full else 'Nur Home'}")
        self.f_id.setText(p.backup_id); self.f_ns.setText(p.namespace)
        self.sources.clear(); self.sources.addItems(p.effective_sources)
        self.sources.setEnabled(not full); self.src_add.setEnabled(not full); self.src_rm.setEnabled(not full)
        self.mode_info.setText(("Vollständiges System (Dateiebene): Quelle `/` plus lokale Dateisysteme wie /boot. "
                                f"Standard-Ausschlüsse: {', '.join(DEFAULT_FULL_EXCLUDES)}. Läuft mit "
                                "Administrator-Rechten.") if full else "Nur das eigene Home-Verzeichnis (und weitere "
                                "gewählte Ordner).")
        self.f_scope.setEnabled(not full)
        self.mount_label.setVisible(full); self.mount_list.setVisible(full)
        if full:
            self.mount_list.load(p.full_mounts)
        self.excludes.setPlainText("\n".join(p.excludes))
        self.f_enc.setChecked(p.encrypt); self.f_key.setText(p.keyfile); self.f_master.setText(p.master_pubkey)
        self.f_dbdump.setChecked(p.db_dump)
        self.f_changed.setChecked(p.change_detection == METADATA)
        self.f_weekly.setChecked(p.weekly_data); self.f_weekly.setEnabled(p.change_detection == METADATA)
        self.f_data_sched.setText(p.data_schedule)
        self.refresh_key_info()
        self.f_sched.setText(p.schedule); self.f_ac.setChecked(p.require_ac)
        self.f_scope.setCurrentIndex(1 if p.scope == "system" else 0)
        self.snap_table.setRowCount(0)
        self.refresh_status()

    # -- Tab Backup ---------------------------------------------------------
    def _build_backup_tab(self) -> QWidget:
        w = QWidget(); outer = QVBoxLayout(w); outer.setContentsMargins(0, 8, 0, 0); outer.setSpacing(8)

        # Aktionsleiste: immer sichtbar, unabhängig vom Scrollen
        save = QPushButton("Speichern"); save.clicked.connect(self.save_profile)
        self.backup_btn = QPushButton("Jetzt sichern"); self.backup_btn.setObjectName("primary")
        self.backup_btn.clicked.connect(self.backup_now)
        bar = QHBoxLayout(); bar.addWidget(self.backup_btn); bar.addWidget(save); bar.addStretch()
        outer.addLayout(bar)

        # Einstellungen (scrollbar, damit das Fenster klein werden darf)
        self.f_id = QLineEdit(); self.f_ns = QLineEdit()
        self.f_enc = QCheckBox("Clientseitig verschlüsseln (empfohlen)"); self.f_key = QLineEdit()
        self.f_key.setPlaceholderText("optional – leer = automatisch erzeugter Schlüssel")
        self.f_master = QLineEdit(); self.f_master.setPlaceholderText("optional – RSA-Master-Public-Key (.pem)")
        self.key_info = QLabel(""); self.key_info.setWordWrap(True)
        self.key_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.key_btn = QPushButton("Schlüssel exportieren …"); self.key_btn.clicked.connect(self.export_key_dialog)
        self.sheet_btn = QPushButton("Notfallblatt speichern …"); self.sheet_btn.clicked.connect(self.save_sheet)
        self.f_changed = QCheckBox("Nur geänderte Dateien lesen (schneller)")
        self.f_changed.setToolTip("Metadaten-Vergleich. Nach dem Umstellen liest das nächste Backup einmal alles, danach nur "
                                  "noch Geändertes. Der PBS-Server muss Version 3.3 oder neuer sein.")
        self.f_weekly = QCheckBox("Zusätzlich wöchentlich alles neu lesen (Modus data)")
        self.f_weekly.setToolTip("Prüft auch Dateien, deren Metadaten unverändert sind, und meldet Lesefehler. "
                                 "Gilt nur mit einem Zeitplan.")
        self.f_data_sched = QLineEdit(); self.f_data_sched.setPlaceholderText("Zeitpunkt, z. B. Sun *-*-* 03:00:00")
        self.f_changed.toggled.connect(self.f_weekly.setEnabled)
        self.f_dbdump = QCheckBox("Datenbank-Dumps vor dem Backup anlegen")
        self.db_btn = QPushButton("Datenbanken und Zugänge …"); self.db_btn.clicked.connect(self.db_dialog)

        g_id = QGroupBox("Ziel"); f = QFormLayout(g_id)
        f.addRow("Backup-ID", self.f_id); f.addRow("Namespace", self.f_ns)

        g_chg = QGroupBox("Geschwindigkeit"); cl = QVBoxLayout(g_chg)
        cl.addWidget(self.f_changed); cl.addWidget(self.f_weekly)
        cf = QFormLayout(); cf.addRow("Prüflauf", self.f_data_sched); cl.addLayout(cf)

        self.sources = QListWidget()
        self.sources.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sources.setMinimumHeight(60); self.sources.setMaximumHeight(110)
        self.src_add = QPushButton("Ordner hinzufügen …"); self.src_add.clicked.connect(self.add_source)
        self.src_rm = QPushButton("Entfernen"); self.src_rm.clicked.connect(self.remove_source)
        self.mode_info = QLabel(""); self.mode_info.setWordWrap(True); self.mode_info.setObjectName("muted")
        self.mount_label = QLabel("Laufwerke mitsichern (Haken = ja):")
        self.mount_list = MountList(); self.mount_list.setMaximumHeight(110)
        self.excludes = QPlainTextEdit(); self.excludes.setMaximumHeight(80)
        self.excludes.setPlaceholderText("Ein Muster pro Zeile, z. B. *.tmp")
        g_src = QGroupBox("Was wird gesichert?"); sl = QVBoxLayout(g_src)
        srow = QHBoxLayout(); srow.addWidget(self.src_add); srow.addWidget(self.src_rm); srow.addStretch()
        sl.addWidget(self.mode_info); sl.addWidget(self.sources); sl.addLayout(srow)
        sl.addWidget(self.mount_label); sl.addWidget(self.mount_list)
        sl.addWidget(QLabel("Ausschlüsse")); sl.addWidget(self.excludes)

        g_enc = QGroupBox("Verschlüsselung"); el = QVBoxLayout(g_enc)
        el.addWidget(self.f_enc); el.addWidget(self.key_info)
        krow = QHBoxLayout(); krow.addWidget(self.key_btn); krow.addWidget(self.sheet_btn); krow.addStretch()
        el.addLayout(krow)
        kf = QFormLayout(); kf.addRow("Eigene Schlüsseldatei", self.f_key); kf.addRow("Master-Public-Key", self.f_master)
        el.addLayout(kf)

        g_db = QGroupBox("Datenbanken"); dl = QVBoxLayout(g_db)
        hint = QLabel("Postgres- und MariaDB/MySQL-Container werden vor dem Backup gedumpt."); hint.setObjectName("muted")
        hint.setWordWrap(True)
        dl.addWidget(self.f_dbdump); dl.addWidget(hint)
        drow = QHBoxLayout(); drow.addWidget(self.db_btn); drow.addStretch(); dl.addLayout(drow)

        inner = QWidget(); il = QVBoxLayout(inner); il.setContentsMargins(0, 0, 8, 0); il.setSpacing(10)
        for g in (g_id, g_chg, g_src, g_enc, g_db):
            il.addWidget(g)
        il.addStretch()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(inner)

        # Fortschritt und Protokoll
        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setVisible(False)
        self.bar_text = QLabel(""); self.bar_text.setVisible(False); self.bar_text.setObjectName("muted")
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setObjectName("log")
        self.log.setPlaceholderText("Hier erscheint das Protokoll des Backups.")
        lp = QWidget(); ll = QVBoxLayout(lp); ll.setContentsMargins(0, 0, 0, 0); ll.setSpacing(4)
        ll.addWidget(self.bar); ll.addWidget(self.bar_text); ll.addWidget(self.log, 1)

        split = QSplitter(Qt.Orientation.Vertical); split.setChildrenCollapsible(False)
        split.addWidget(scroll); split.addWidget(lp)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 1); split.setSizes([420, 160])
        outer.addWidget(split, 1)
        return w

    def add_source(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Quellordner wählen", str(Path.home()))
        if d and d not in [self.sources.item(i).text() for i in range(self.sources.count())]:
            self.sources.addItem(d)

    def remove_source(self) -> None:
        for it in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(it))

    def _collect(self) -> Profile | None:
        p = self.current
        if not p:
            return None
        p.backup_id = self.f_id.text().strip() or p.backup_id
        p.namespace = self.f_ns.text().strip()
        if p.backup_mode != FULL:  # beim Vollbackup ist die Quelle fest `/`
            p.sources = [self.sources.item(i).text() for i in range(self.sources.count())]
        else:
            p.full_mounts = self.mount_list.selected()
        p.excludes = [l.strip() for l in self.excludes.toPlainText().splitlines() if l.strip()]
        p.encrypt, p.keyfile = self.f_enc.isChecked(), self.f_key.text().strip()
        p.master_pubkey = self.f_master.text().strip()
        p.db_dump = self.f_dbdump.isChecked()
        p.change_detection = METADATA if self.f_changed.isChecked() else LEGACY
        p.weekly_data = self.f_weekly.isChecked()
        p.data_schedule = self.f_data_sched.text().strip() or p.data_schedule
        p.schedule, p.require_ac = self.f_sched.text().strip(), self.f_ac.isChecked()
        return p

    def save_profile(self, schedule: str = "keep") -> bool:
        p = self._collect()
        if not p:
            return False
        try:
            stored = self.mgr.profiles_store.load(p.name)
            if (stored.change_detection, stored.weekly_data, stored.data_schedule) != \
                    (p.change_detection, p.weekly_data, p.data_schedule) and p.schedule and \
                    self.mgr.schedule_status(p)["installed"] and schedule == "keep":
                schedule = "install"   # wöchentlichen data-Timer angleichen (in demselben Speichern)
            if p.encrypt and not self.mgr.has_key(p):
                if self.mgr.enable_encryption(p):  # erzeugt und speichert den Key automatisch
                    self._offer_key_export(p)
                    self.refresh_key_info()
            self.mgr.save(p, schedule=schedule if isinstance(schedule, str) else "keep")
        except Exception as e:
            QMessageBox.warning(self, "Speichern fehlgeschlagen", str(e))
            return False
        self.statusBar().showMessage("Gespeichert")
        return True

    def db_dialog(self) -> None:
        p = self._collect()
        if p and DbDumpDialog(self.mgr, p, self).exec():
            self.f_dbdump.setChecked(p.db_dump)
            self.statusBar().showMessage("Datenbank-Dumps gespeichert")

    # -- Schlüssel ----------------------------------------------------------
    def refresh_key_info(self) -> None:
        p = self.current
        if not p:
            return
        if p.keyfile:
            text, can_export = f"Eigene Schlüsseldatei: {p.keyfile}", False
        elif self.mgr.secrets.exists(p.slug, "key"):
            text, can_export = f"Automatisch erzeugter Schlüssel, Fingerprint: {p.key_fingerprint}", True
            text += (f"\nExtern gesichert am {p.key_exported_at}." if p.key_exported_at else
                     "\nNOCH NICHT exportiert" + (" – das Vollbackup ist bis dahin gesperrt!" if needs_key_export(p)
                                                    else "."))
        elif p.encrypt:
            text, can_export = "Der Schlüssel wird beim Speichern automatisch erzeugt.", False
        else:
            text, can_export = "Verschlüsselung ist aus: Backups liegen unverschlüsselt auf dem Server.", False
        self.key_info.setText(text)
        self.key_btn.setEnabled(can_export)
        self.sheet_btn.setVisible(p.backup_mode == FULL)

    def _offer_key_export(self, p: Profile) -> None:
        """Ohne Schlüssel sind verschlüsselte Backups unwiederbringlich verloren: Kopie anbieten."""
        QMessageBox.information(
            self, "Schlüssel sichern",
            f"Für '{p.name}' wurde ein Verschlüsselungs-Schlüssel erzeugt (Fingerprint {p.key_fingerprint}).\n\n"
            "Geht er verloren (z. B. Festplattendefekt), sind alle Backups UNWIDERRUFLICH unlesbar. "
            "Bitte jetzt eine Kopie an einem sicheren Ort ablegen (Passwortmanager, USB-Stick).")
        self.export_key_dialog()

    def save_sheet(self) -> None:
        p = self.current
        if not p:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Notfallblatt speichern", str(Path.home() / f"notfallblatt-{p.slug}.txt"))
        if dest:
            Path(dest).write_text(render_sheet(p))
            self.statusBar().showMessage(f"Notfallblatt gespeichert: {dest} (enthält keine Geheimnisse)")

    def export_key_dialog(self) -> None:
        p = self.current
        if not p:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Schlüssel exportieren", str(Path.home() / f"{p.slug}-key.json"))
        if not dest:
            self.statusBar().showMessage("Schlüssel NICHT exportiert – bitte später nachholen")
            return
        try:
            self.mgr.export_key(p, dest)
            self.statusBar().showMessage(f"Schlüssel exportiert nach {dest}")
            self.refresh_key_info()
        except Exception as e:
            QMessageBox.warning(self, "Export fehlgeschlagen", str(e))

    def backup_now(self) -> None:
        p = self._collect()
        if not p or not self.save_profile():
            return
        if needs_key_export(p):   # Vollbackup ist gesperrt, bis der Schlüssel exportiert ist: erklären und Export anbieten
            QMessageBox.information(
                self, "Schlüssel zuerst sichern",
                "Dieses verschlüsselte Vollbackup startet erst, wenn der Verschlüsselungs-Schlüssel exportiert wurde. "
                "Ohne Kopie ließe sich das System auf einem neuen PC nicht wiederherstellen.\n\n"
                "Im nächsten Schritt wählst du, wohin die Schlüsseldatei gespeichert wird (sicherer Ort, nicht nur "
                "dieser Rechner).")
            self.export_key_dialog()
            if needs_key_export(self.mgr.profiles_store.load(p.name)):
                return                                       # Export abgebrochen: nichts starten
            p = self.mgr.profiles_store.load(p.name)
            self.current = p
        self.log.clear()
        self.backup_btn.setEnabled(False)
        self.bar.setVisible(True); self.bar_text.setVisible(True)
        self.bar.setRange(0, 0)                       # unbestimmt, bis die erste Messung da ist
        self.bar_text.setText("Starte …")

        def work(on_line):
            on_line(f"Starte Backup '{p.name}' …")
            return self.mgr.run_backup(p, on_line)

        def done(code):
            self.backup_btn.setEnabled(True)
            self.bar.setRange(0, 100); self.bar.setValue(100 if code == 0 else self.bar.value())
            self.bar_text.setText("Fertig." if code == 0 else "Fehlgeschlagen.")
            self.log.appendPlainText("Backup erfolgreich" if code == 0 else f"Backup fehlgeschlagen (Exitcode {code})")
            self.statusBar().showMessage("Backup fertig" if code == 0 else "Backup fehlgeschlagen")
            self.refresh_snapshots()

        w = self.run_async(work, done, self.backup_line)
        w.failed.connect(lambda _m: (self.backup_btn.setEnabled(True), self.bar.setVisible(False), self.bar_text.setVisible(False)))

    def backup_line(self, line: str) -> None:
        """Fortschrittszeilen aktualisieren den Balken, alles andere kommt ins Log."""
        from ..core.progress import decode, describe
        prog = decode(line)
        if prog is None:
            self.log.appendPlainText(line)
            return
        if prog.percent is None:
            self.bar.setRange(0, 0)                   # Gesamtmenge unbekannt: laufender Balken
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(prog.percent))
        self.bar_text.setText(describe(prog))

    # -- Tab Snapshots ------------------------------------------------------
    def _build_snapshots_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 10, 0, 0)
        row = QHBoxLayout()
        r = QPushButton("Aktualisieren"); r.clicked.connect(self.refresh_snapshots)
        rs = QPushButton("Wiederherstellen …"); rs.clicked.connect(self.restore_selected)
        rf = QPushButton("Dateien durchsuchen …"); rf.clicked.connect(self.browse_files)
        row.addWidget(r); row.addWidget(rs); row.addWidget(rf); row.addStretch()
        self.snap_table = QTableWidget(0, 4)
        self.snap_table.setHorizontalHeaderLabels(["Zeit", "ID", "Größe", "Archive"])
        hh = self.snap_table.horizontalHeader(); hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.snap_table.verticalHeader().setVisible(False); self.snap_table.setAlternatingRowColors(True)
        self.snap_table.setShowGrid(False)
        self.snap_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.snap_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addLayout(row); lay.addWidget(self.snap_table)
        self._snaps = []
        return w

    def refresh_snapshots(self) -> None:
        p = self.current
        if not p:
            return
        self.statusBar().showMessage("Lade Snapshots …")

        def done(snaps):
            self._snaps = snaps
            self.snap_table.setRowCount(len(snaps))
            for i, s in enumerate(snaps):
                vals = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.backup_time)), s.backup_id,
                        fmt_size(s.size), ", ".join(s.files)]
                for j, v in enumerate(vals):
                    self.snap_table.setItem(i, j, QTableWidgetItem(v))
            self.statusBar().showMessage(f"{len(snaps)} Snapshots")

        self.run_async(lambda _l: self.mgr.snapshots(p), done)

    def browse_files(self) -> None:
        row = self.snap_table.currentRow()
        if not self.current or not (0 <= row < len(self._snaps)):
            QMessageBox.information(self, "Dateien durchsuchen", "Bitte einen Snapshot auswählen.")
            return
        FilesDialog(self.mgr, self.current, self._snaps[row], self).exec()

    def restore_selected(self) -> None:
        row = self.snap_table.currentRow()
        if not self.current or not (0 <= row < len(self._snaps)):
            QMessageBox.information(self, "Wiederherstellen", "Bitte einen Snapshot auswählen.")
            return
        snap = self._snaps[row]
        archives = snap.archives or snap.files
        archive, ok = QInputDialog.getItem(self, "Archiv wählen", "Archiv:", archives, 0, False)
        if not ok:
            return
        target = QFileDialog.getExistingDirectory(self, "Zielordner (leer / neu empfohlen)", str(Path.home()))
        if not target:
            return
        p = self.current
        self.statusBar().showMessage("Stelle wieder her …")
        self.run_async(lambda _l: self.mgr.client(p).restore(snap, archive, target),
                       lambda _r: (self.statusBar().showMessage("Wiederherstellung fertig"),
                                   QMessageBox.information(self, "Fertig", f"Wiederhergestellt nach:\n{target}")))

    # -- Tab Zeitplan -------------------------------------------------------
    def _build_schedule_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 10, 0, 0); lay.setSpacing(10)
        self.f_scope = QComboBox()
        self.f_scope.addItems(["Benutzer – sichert nur deine Daten, kein sudo nötig",
                               "System – ganzes System, braucht Administrator-Rechte"])
        if not self.admin_ok:
            self.f_scope.model().item(1).setEnabled(False)  # ausgegraut ohne Admin-Rechte
            self.f_scope.setItemData(1, "Keine Administrator-Rechte erkannt", Qt.ItemDataRole.ToolTipRole)
        self.f_preset = QComboBox()
        for cb in (self.f_scope, self.f_preset):   # lange Einträge dürfen das Fenster nicht aufweiten
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(18)
        for label, _v in PRESETS:
            self.f_preset.addItem(label)
        self.f_preset.activated.connect(lambda i: PRESETS[i][1] and self.f_sched.setText(PRESETS[i][1]))
        self.f_sched = QLineEdit(); self.f_sched.setPlaceholderText("systemd OnCalendar, z. B. *-*-* 02:00:00")
        self.f_ac = QCheckBox("Nur bei Netzbetrieb (ConditionACPower)")
        g = QGroupBox("Zeitplan"); form = QFormLayout(g)
        form.addRow("Modus", self.f_scope); form.addRow("Vorlage", self.f_preset)
        form.addRow("Zeitplan", self.f_sched); form.addRow(self.f_ac)
        lay.addWidget(g)
        row = QHBoxLayout()
        inst = QPushButton("Zeitplan aktivieren"); inst.setObjectName("primary"); inst.clicked.connect(self.install_schedule)
        rm = QPushButton("Zeitplan entfernen"); rm.clicked.connect(self.remove_schedule)
        row.addWidget(inst); row.addWidget(rm); row.addStretch()
        lay.addLayout(row)
        self.sched_status = QLabel(); self.sched_status.setWordWrap(True)
        self.sched_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gs = QGroupBox("Status"); sl = QVBoxLayout(gs); sl.addWidget(self.sched_status)
        self.linger_box = QPushButton("Timer auch ohne Login erlauben (linger)")
        self.linger_box.clicked.connect(self.do_linger)
        sl.addWidget(self.linger_box, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(gs); lay.addStretch()
        return w

    def refresh_status(self) -> None:
        p = self.current
        if not p:
            return
        try:
            st = self.mgr.schedule_status(p)
            last = read_status(Paths.for_scope(p.scope), p)
        except Exception as e:
            self.sched_status.setText(f"Status nicht lesbar: {e}")
            return
        parts = [f"Zeitplan: {'aktiv' if st['active'] else ('installiert, inaktiv' if st['installed'] else 'nicht eingerichtet')}"]
        if st["next"] and st["next"] != "n/a":
            parts.append(f"Nächster Lauf: {st['next']}")
        if last:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["time"]))
            parts.append(f"Letzter Lauf: {when} – {'OK' if last['ok'] else 'FEHLER: ' + last['message']}")
        self.sched_status.setText("\n".join(parts))
        self.linger_box.setVisible(p.scope == "user" and not linger_enabled())

    def _apply_scope(self) -> Profile | None:
        p = self._collect()
        if not p:
            return None
        old_scope = p.scope
        p.scope = "system" if self.f_scope.currentIndex() == 1 else "user"
        if old_scope != p.scope:  # Moduswechsel: alten Zeitplan entfernen
            try:
                old = Profile.from_dict({**p.to_dict(), "scope": old_scope})
                self.mgr.save(old, schedule="remove")
            except Exception as e:
                QMessageBox.warning(self, "Moduswechsel", f"Alten Zeitplan nicht entfernt: {e}")
        return p

    def install_schedule(self) -> None:
        p = self._apply_scope()
        if not p:
            return
        if not p.schedule:
            QMessageBox.warning(self, "Zeitplan", "Bitte einen Zeitplan angeben.")
            return
        self.run_async(lambda _l: self.mgr.save(p, schedule="install"),
                       lambda _r: (self.statusBar().showMessage("Zeitplan aktiviert"), self.refresh_status()))

    def remove_schedule(self) -> None:
        p = self._apply_scope()
        if p:
            self.run_async(lambda _l: self.mgr.save(p, schedule="remove"),
                           lambda _r: (self.statusBar().showMessage("Zeitplan entfernt"), self.refresh_status()))

    def do_linger(self) -> None:
        try:
            enable_linger()
        except Exception as e:
            QMessageBox.warning(self, "Linger", str(e))
        self.refresh_status()

    # -- Profilverwaltung ---------------------------------------------------
    def add_server(self) -> None:
        dlg = AddServerDialog(self)
        if dlg.exec() and dlg.profile:
            p = dlg.profile
            try:
                self.mgr.save(p, token=dlg.token)  # Vollbackup: System-Kopie per pkexec (Passwortabfrage)
                created = self.mgr.enable_encryption(p)  # Verschlüsselung ist Standard: Key automatisch erzeugen
            except Exception as e:
                QMessageBox.warning(self, "Speichern fehlgeschlagen", str(e))
                return
            self.reload(select=p.name)
            if created:
                self._offer_key_export(p)
                self.refresh_key_info()
            self._after_create(self.current or p, dlg.start_now.isChecked())

    def _after_create(self, p: Profile, start_now: bool) -> None:
        """Zeitplan einrichten, Datenbank-Zugänge zeigen und auf Wunsch das erste Backup starten."""
        if p.schedule:
            try:
                if self.mgr.activate_pending_schedule(p):
                    self.statusBar().showMessage(f"Zeitplan aktiv: {p.schedule}")
                elif self.mgr.schedule_pending(p):
                    QMessageBox.information(
                        self, "Zeitplan wartet",
                        f"Der Zeitplan '{p.schedule}' ist vermerkt und startet, sobald der Verschlüsselungs-Schlüssel "
                        "exportiert wurde (Backup-Tab: „Schlüssel exportieren …“).")
            except Exception as e:
                QMessageBox.warning(self, "Zeitplan nicht eingerichtet", str(e))
            self.refresh_status()
        if p.db_dump:   # erst Datenbanken und Zugänge, das erste Backup startet danach
            self.db_dialog()
        if start_now:
            self.backup_now()   # fragt bei gesperrtem Vollbackup den Schlüssel-Export ab

    def remove_profile(self) -> None:
        p = self.current
        if not p:
            return
        if QMessageBox.question(self, "Entfernen", f"Profil '{p.name}' samt Zeitplan und gespeichertem Token entfernen?\n"
                                "(Der Zugang auf dem PBS-Server bleibt bestehen.)") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.mgr.delete(p)
        except Exception as e:
            QMessageBox.warning(self, "Fehler", str(e))
        self.reload()
