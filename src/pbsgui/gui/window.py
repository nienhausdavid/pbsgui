"""Hauptfenster: Profilliste und Tabs Backup, Snapshots, Zeitplan."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from ..core.manager import ProfileManager, can_use_admin
from ..core.modes import DEFAULT_FULL_EXCLUDES, FULL
from ..core.models import Profile
from ..core.paths import Paths
from ..core.runner import read_status
from ..core.scheduler import enable_linger, linger_enabled
from .add_server import AddServerDialog
from .mounts_widget import MountList
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
        self.setWindowTitle("pbsgui – Proxmox Backup")
        self.resize(1000, 640)
        self.mgr = manager or ProfileManager()
        self.admin_ok = can_use_admin()
        self.current: Profile | None = None
        self._workers: set[Worker] = set()

        self.plist = QListWidget()
        add = QPushButton("Server hinzufügen …"); add.clicked.connect(self.add_server)
        rem = QPushButton("Entfernen"); rem.clicked.connect(self.remove_profile)
        left = QWidget(); ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Profile")); ll.addWidget(self.plist); ll.addWidget(add); ll.addWidget(rem)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_backup_tab(), "Backup")
        self.tabs.addTab(self._build_snapshots_tab(), "Snapshots")
        self.tabs.addTab(self._build_schedule_tab(), "Zeitplan")
        split = QSplitter(); split.addWidget(left); split.addWidget(self.tabs)
        split.setStretchFactor(1, 1); split.setSizes([240, 760])
        self.setCentralWidget(split)
        self.statusBar().showMessage("Bereit")
        self.plist.currentRowChanged.connect(self._select)
        self.reload()

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
        self.plist.blockSignals(False)
        names = [p.name for p in profiles]
        self.plist.setCurrentRow(names.index(select) if select in names else (0 if names else -1))
        self._select(self.plist.currentRow())

    def _select(self, row: int) -> None:
        profiles = self.mgr.profiles()
        self.current = profiles[row] if 0 <= row < len(profiles) else None
        for i in range(self.tabs.count()):
            self.tabs.widget(i).setEnabled(self.current is not None)
        p = self.current
        if not p:
            return
        full = p.backup_mode == FULL
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
        self.refresh_key_info()
        self.f_sched.setText(p.schedule); self.f_ac.setChecked(p.require_ac)
        self.f_scope.setCurrentIndex(1 if p.scope == "system" else 0)
        self.snap_table.setRowCount(0)
        self.refresh_status()

    # -- Tab Backup ---------------------------------------------------------
    def _build_backup_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        form = QFormLayout()
        self.f_id = QLineEdit(); self.f_ns = QLineEdit()
        self.f_enc = QCheckBox("Clientseitig verschlüsseln (empfohlen)"); self.f_key = QLineEdit()
        self.f_key.setPlaceholderText("optional – leer = automatisch erzeugter Schlüssel")
        self.f_master = QLineEdit(); self.f_master.setPlaceholderText("optional – RSA-Master-Public-Key (.pem)")
        self.key_info = QLabel(""); self.key_info.setWordWrap(True)
        self.key_btn = QPushButton("Schlüssel exportieren …"); self.key_btn.clicked.connect(self.export_key_dialog)
        form.addRow("Backup-ID", self.f_id); form.addRow("Namespace", self.f_ns)
        form.addRow(self.f_enc); form.addRow("Eigene Schlüsseldatei", self.f_key)
        form.addRow("Master-Public-Key", self.f_master)
        form.addRow(self.key_info); form.addRow("", self.key_btn)
        lay.addLayout(form)
        lay.addWidget(QLabel("Quellordner"))
        self.sources = QListWidget()
        self.sources.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        row = QHBoxLayout()
        self.src_add = QPushButton("Ordner hinzufügen …"); self.src_add.clicked.connect(self.add_source)
        self.src_rm = QPushButton("Entfernen"); self.src_rm.clicked.connect(self.remove_source)
        row.addWidget(self.src_add); row.addWidget(self.src_rm); row.addStretch()
        self.mode_info = QLabel(""); self.mode_info.setWordWrap(True)
        self.mount_label = QLabel("Laufwerke mitsichern (Haken = ja):")
        self.mount_list = MountList(); self.mount_list.setMaximumHeight(120)
        lay.addWidget(self.mode_info); lay.addWidget(self.sources); lay.addLayout(row)
        lay.addWidget(self.mount_label); lay.addWidget(self.mount_list)
        lay.addWidget(QLabel("Ausschlüsse (ein Muster pro Zeile)"))
        self.excludes = QPlainTextEdit(); self.excludes.setMaximumHeight(70)
        lay.addWidget(self.excludes)
        row2 = QHBoxLayout()
        save = QPushButton("Speichern"); save.clicked.connect(self.save_profile)
        self.backup_btn = QPushButton("Jetzt sichern"); self.backup_btn.clicked.connect(self.backup_now)
        row2.addWidget(save); row2.addWidget(self.backup_btn); row2.addStretch()
        lay.addLayout(row2)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
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
        p.schedule, p.require_ac = self.f_sched.text().strip(), self.f_ac.isChecked()
        return p

    def save_profile(self, schedule: str = "keep") -> bool:
        p = self._collect()
        if not p:
            return False
        try:
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

    # -- Schlüssel ----------------------------------------------------------
    def refresh_key_info(self) -> None:
        p = self.current
        if not p:
            return
        if p.keyfile:
            text, can_export = f"Eigene Schlüsseldatei: {p.keyfile}", False
        elif self.mgr.secrets.get(p.slug, "key"):
            text, can_export = f"Automatisch erzeugter Schlüssel, Fingerprint: {p.key_fingerprint}", True
        elif p.encrypt:
            text, can_export = "Der Schlüssel wird beim Speichern automatisch erzeugt.", False
        else:
            text, can_export = "Verschlüsselung ist aus: Backups liegen unverschlüsselt auf dem Server.", False
        self.key_info.setText(text)
        self.key_btn.setEnabled(can_export)

    def _offer_key_export(self, p: Profile) -> None:
        """Ohne Schlüssel sind verschlüsselte Backups unwiederbringlich verloren: Kopie anbieten."""
        QMessageBox.information(
            self, "Schlüssel sichern",
            f"Für '{p.name}' wurde ein Verschlüsselungs-Schlüssel erzeugt (Fingerprint {p.key_fingerprint}).\n\n"
            "Geht er verloren (z. B. Festplattendefekt), sind alle Backups UNWIDERRUFLICH unlesbar. "
            "Bitte jetzt eine Kopie an einem sicheren Ort ablegen (Passwortmanager, USB-Stick).")
        self.export_key_dialog()

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
        except Exception as e:
            QMessageBox.warning(self, "Export fehlgeschlagen", str(e))

    def backup_now(self) -> None:
        p = self._collect()
        if not p or not self.save_profile():
            return
        self.log.clear()
        self.backup_btn.setEnabled(False)

        def work(on_line):
            on_line(f"Starte Backup '{p.name}' …")
            return self.mgr.run_backup(p, on_line)

        def done(code):
            self.backup_btn.setEnabled(True)
            self.log.appendPlainText("Backup erfolgreich" if code == 0 else f"Backup fehlgeschlagen (Exitcode {code})")
            self.statusBar().showMessage("Backup fertig" if code == 0 else "Backup fehlgeschlagen")
            self.refresh_snapshots()

        w = self.run_async(work, done, self.log.appendPlainText)
        w.failed.connect(lambda _m: self.backup_btn.setEnabled(True))

    # -- Tab Snapshots ------------------------------------------------------
    def _build_snapshots_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        row = QHBoxLayout()
        r = QPushButton("Aktualisieren"); r.clicked.connect(self.refresh_snapshots)
        rs = QPushButton("Wiederherstellen …"); rs.clicked.connect(self.restore_selected)
        row.addWidget(r); row.addWidget(rs); row.addStretch()
        self.snap_table = QTableWidget(0, 4)
        self.snap_table.setHorizontalHeaderLabels(["Zeit", "ID", "Größe", "Archive"])
        self.snap_table.horizontalHeader().setStretchLastSection(True)
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

    def restore_selected(self) -> None:
        row = self.snap_table.currentRow()
        if not self.current or not (0 <= row < len(self._snaps)):
            QMessageBox.information(self, "Wiederherstellen", "Bitte einen Snapshot auswählen.")
            return
        snap = self._snaps[row]
        archives = [f for f in snap.files if f.endswith(".pxar")] or snap.files
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
        w = QWidget(); lay = QVBoxLayout(w); form = QFormLayout()
        self.f_scope = QComboBox()
        self.f_scope.addItems(["Benutzer – sichert nur deine Daten, kein sudo nötig",
                               "System – ganzes System, braucht Administrator-Rechte"])
        if not self.admin_ok:
            self.f_scope.model().item(1).setEnabled(False)  # ausgegraut ohne Admin-Rechte
            self.f_scope.setItemData(1, "Keine Administrator-Rechte erkannt", Qt.ItemDataRole.ToolTipRole)
        self.f_preset = QComboBox()
        for label, _v in PRESETS:
            self.f_preset.addItem(label)
        self.f_preset.activated.connect(lambda i: PRESETS[i][1] and self.f_sched.setText(PRESETS[i][1]))
        self.f_sched = QLineEdit(); self.f_sched.setPlaceholderText("systemd OnCalendar, z. B. *-*-* 02:00:00")
        self.f_ac = QCheckBox("Nur bei Netzbetrieb (ConditionACPower)")
        form.addRow("Modus", self.f_scope); form.addRow("Vorlage", self.f_preset)
        form.addRow("Zeitplan", self.f_sched); form.addRow(self.f_ac)
        lay.addLayout(form)
        row = QHBoxLayout()
        inst = QPushButton("Zeitplan aktivieren"); inst.clicked.connect(self.install_schedule)
        rm = QPushButton("Zeitplan entfernen"); rm.clicked.connect(self.remove_schedule)
        row.addWidget(inst); row.addWidget(rm); row.addStretch()
        lay.addLayout(row)
        self.sched_status = QLabel(); self.sched_status.setWordWrap(True)
        self.linger_box = QPushButton("Timer auch ohne Login erlauben (linger)")
        self.linger_box.clicked.connect(self.do_linger)
        lay.addWidget(self.sched_status); lay.addWidget(self.linger_box); lay.addStretch()
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
