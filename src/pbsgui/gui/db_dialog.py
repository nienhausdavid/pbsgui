"""Dialoge für Datenbank-Dumps: erkannte Container und die Zugänge der MariaDB/MySQL-Container.

Postgres braucht keinen Zugang. Für MariaDB/MySQL wird ein Zugang ausdrücklich hinterlegt (verschlüsselt) oder ein
Dump-Benutzer mit reinen Leserechten angelegt; gelesen wird nie aus .env-Dateien oder der Container-Umgebung.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

from ..core.dbdump import MARIADB, Database, DumpError, find_databases
from ..core.manager import ProfileManager
from ..core.models import Profile
from .workers import Worker


class DbCredDialog(QDialog):
    """Zugang eines MariaDB/MySQL-Containers: vorhandener Benutzer oder Dump-Benutzer anlegen lassen."""

    def __init__(self, mgr: ProfileManager, profile: Profile, db: Database, parent=None):
        super().__init__(parent)
        self.mgr, self.profile, self.db = mgr, profile, db
        self._worker: Worker | None = None
        self.setWindowTitle(f"Datenbank-Zugang: {db.container}")
        self.setMinimumWidth(460)
        self.user = QLineEdit(); self.user.setPlaceholderText("Vorhandener Benutzer mit Leserechten")
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.create = QCheckBox("Stattdessen Dump-Benutzer (nur lesen) automatisch anlegen")
        self.admin_user = QLineEdit("root")
        self.admin_pw = QLineEdit(); self.admin_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel(""); self.status.setWordWrap(True)
        note = QLabel("Der Zugang wird verschlüsselt gespeichert und nie aus .env-Dateien oder der Container-Umgebung "
                      "gelesen. Empfohlen: ein eigener Benutzer nur mit Leserechten. Der Admin-Zugang wird nur "
                      "einmal zum Anlegen benutzt und nicht gespeichert.")
        note.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Benutzer", self.user); form.addRow("Passwort", self.pw)
        form.addRow(self.create); form.addRow("Admin-Benutzer", self.admin_user); form.addRow("Admin-Passwort", self.admin_pw)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Speichern && prüfen")
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(note); lay.addLayout(form); lay.addWidget(self.status); lay.addWidget(self.buttons)
        self.create.toggled.connect(self._sync)
        self._sync(False)

    def _sync(self, create: bool) -> None:
        self.user.setEnabled(not create); self.pw.setEnabled(not create)
        self.admin_user.setEnabled(create); self.admin_pw.setEnabled(create)

    def save(self) -> None:
        create = self.create.isChecked()
        user, pw = self.user.text().strip(), self.pw.text()
        admin, admin_pw = self.admin_user.text().strip(), self.admin_pw.text()
        self.pw.clear(); self.admin_pw.clear()   # Passwörter sofort aus den Widgets entfernen
        if create and not (admin and admin_pw):
            self.status.setText("Admin-Benutzer und -Passwort eingeben.")
            return
        if not create and not (user and pw):
            self.status.setText("Benutzer und Passwort eingeben.")
            return
        mgr, profile, db = self.mgr, self.profile, self.db

        def work(_l):
            if create:
                mgr.create_db_credential(profile, db, admin, admin_pw)
            else:
                mgr.set_db_credential(profile, db, user, pw)

        self.buttons.setEnabled(False)
        self.status.setText("Prüfe …")
        self._worker = Worker(work, self)
        self._worker.done.connect(lambda _r: self.accept())
        self._worker.failed.connect(lambda m: (self.status.setText(f"Fehler: {m}"), self.buttons.setEnabled(True)))
        self._worker.start()


class DbDumpDialog(QDialog):
    """Datenbank-Dumps eines Profils: ein/aus, erkannte Datenbanken und Zugänge."""

    def __init__(self, mgr: ProfileManager, profile: Profile, parent=None):
        super().__init__(parent)
        self.mgr, self.profile = mgr, profile
        self.dbs: list[Database] = []
        self._worker: Worker | None = None
        self.setWindowTitle("Datenbank-Dumps")
        self.setMinimumWidth(560)
        note = QLabel("Vor jedem Backup werden laufende Datenbank-Container gedumpt und als eigenes Archiv "
                      "(dbdumps.pxar) mitgesichert. Gestoppte Container nicht: ihre Dateien sind konsistent und im "
                      "normalen Backup enthalten. Dumps von Containern, die länger als 14 Tage nicht mehr erkannt "
                      "werden, räumt pbsgui auf.")
        note.setWordWrap(True)
        self.enabled = QCheckBox("Dumps vor jedem Backup anlegen"); self.enabled.setChecked(profile.db_dump)
        self.status = QLabel("Suche Datenbank-Container …"); self.status.setWordWrap(True)
        self.list = QListWidget()
        self.cred_btn = QPushButton("Zugang festlegen …"); self.cred_btn.setEnabled(False)
        self.del_btn = QPushButton("Zugang entfernen"); self.del_btn.setEnabled(False)
        self.cred_btn.clicked.connect(self.edit_credential)
        self.del_btn.clicked.connect(self.remove_credential)
        self.list.currentRowChanged.connect(self._row_changed)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        for w in (note, self.enabled, self.status, self.list, self.cred_btn, self.del_btn, self.buttons):
            lay.addWidget(w)
        self.scan()

    # -- Erkennung ------------------------------------------------------------
    def scan(self) -> None:
        def work(_l):
            try:
                return find_databases(), ""
            except DumpError as e:
                return [], str(e)

        self._worker = Worker(work, self)
        self._worker.done.connect(lambda res: self._show(*res))
        self._worker.failed.connect(lambda m: self._show([], m))
        self._worker.start()

    def label(self, db: Database) -> str:
        if db.engine != MARIADB:
            return f"{db.container} ({db.owner}): Postgres, kein Zugang nötig"
        cred = self.mgr.db_credentials(self.profile).get(db.key)
        return f"{db.container} ({db.owner}): MariaDB/MySQL, " + (f"Zugang: {cred['user']}" if cred else "Zugang FEHLT")

    def _show(self, dbs: list[Database], err: str = "") -> None:
        self.dbs = dbs
        current = self.list.currentRow()
        self.list.clear()
        for db in dbs:
            self.list.addItem(QListWidgetItem(self.label(db)))
        if 0 <= current < len(dbs):
            self.list.setCurrentRow(current)
        self.status.setText(err or (f"{len(dbs)} Datenbank-Container gefunden." if dbs else
                                    "Keine laufenden Datenbank-Container gefunden (oder kein Zugriff auf deren Docker-Socket)."))
        self._row_changed(self.list.currentRow())

    def _selected(self) -> Database | None:
        row = self.list.currentRow()
        return self.dbs[row] if 0 <= row < len(self.dbs) else None

    def _row_changed(self, _row: int) -> None:
        db = self._selected()
        maria = bool(db and db.engine == MARIADB)
        self.cred_btn.setEnabled(maria)
        self.del_btn.setEnabled(bool(maria and db.key in self.mgr.db_credentials(self.profile)))

    # -- Zugänge ----------------------------------------------------------------
    def edit_credential(self) -> None:
        db = self._selected()
        if db and DbCredDialog(self.mgr, self.profile, db, self).exec():
            self._show(self.dbs)

    def remove_credential(self) -> None:
        db = self._selected()
        if not db:
            return
        try:
            self.mgr.remove_db_credential(self.profile, db.key)
        except Exception as e:
            QMessageBox.warning(self, "Fehler", str(e))
            return
        self._show(self.dbs)

    def save(self) -> None:
        self.profile.db_dump = self.enabled.isChecked()
        try:
            self.mgr.save(self.profile)
        except Exception as e:
            QMessageBox.warning(self, "Speichern fehlgeschlagen", str(e))
            return
        self.accept()
