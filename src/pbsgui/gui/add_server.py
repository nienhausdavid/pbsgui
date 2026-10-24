"""Dialog „Server hinzufügen“: Weg A (einmalig als root) oder Weg B (vorhandener API-Token).

Ablauf: Fingerprint prüfen -> Zugangsdaten -> „Verbindung prüfen“ (lädt Datastores) -> Datastore und
Namespace wählen (Namespaces werden live geprüft) -> Zeitplan, Datenbank-Dumps und „Backup direkt starten“ wählen
-> Einrichten.
"""
from __future__ import annotations

import socket

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..i18n import tr
from ..core.client import PbsClient
from pathlib import Path

from ..core.manager import can_use_admin
from ..core.models import LEGACY, METADATA, Profile
from ..core.modes import FULL, HOME
from ..core.onboarding import namespace_message, namespace_state
from ..core.pbs_api import ApiError, PbsApi, TokenExistsError, fetch_fingerprint
from ..core.scheduler import CUSTOM_SCHEDULE, SCHEDULE_PRESETS, SchedulerError, next_elapse, validate_calendar
from .mounts_widget import MountList
from .workers import Worker


class AddServerDialog(QDialog):
    def __init__(self, parent=None, admin_ok: bool | None = None):
        super().__init__(parent)
        self.admin_ok = can_use_admin() if admin_ok is None else admin_ok
        self.setWindowTitle(tr("Server hinzufügen"))
        self.setMinimumWidth(520)
        self.profile: Profile | None = None
        self.token = ""
        self._worker: Worker | None = None
        self._ns_worker: Worker | None = None
        self._api: PbsApi | None = None          # angemeldete Sitzung (nur im Speicher)
        self._namespaces: list[str] | None = None  # None = unbekannt
        self._replace_token = False
        self._schedule = ""

        self.mode = QComboBox()
        self.mode.addItems([tr("A: Einmalig als root anmelden (Zugang automatisch anlegen)"),
                            tr("B: Vorhandenen API-Token verwenden")])
        self.name = QLineEdit(socket.gethostname())
        self.server = QLineEdit(); self.server.setPlaceholderText("pbs.example.org")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(8007)
        self.fp = QLineEdit(); self.fp.setReadOnly(True)
        self.fp_btn = QPushButton(tr("Fingerprint abrufen"))
        self.fp_ok = QCheckBox(tr("Ich habe den Fingerprint geprüft und vertraue ihm"))

        form = QFormLayout()
        for label, w in [(tr("Modus"), self.mode), (tr("Profilname"), self.name), ("Server", self.server),
                         ("Port", self.port), ("Fingerprint", self.fp), ("", self.fp_btn), ("", self.fp_ok)]:
            form.addRow(label, w)

        self.root_user = QLineEdit("root@pam")
        self.root_pw = QLineEdit(); self.root_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.tfa = QLineEdit(); self.tfa.setPlaceholderText(tr("nur bei aktiver Zwei-Faktor-Anmeldung"))
        a = QWidget(); fa = QFormLayout(a)
        fa.addRow("Root-Benutzer", self.root_user); fa.addRow("Root-Passwort", self.root_pw); fa.addRow("2FA-Code", self.tfa)
        fa.addRow(QLabel(tr("Das Passwort wird nur für die Anmeldung verwendet und nicht gespeichert.")))
        self.auth_id = QLineEdit(); self.auth_id.setPlaceholderText("benutzer@pbs!tokenname")
        self.secret = QLineEdit(); self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        b = QWidget(); fb = QFormLayout(b)
        fb.addRow("Token-ID", self.auth_id); fb.addRow("Token-Secret", self.secret)
        self.stack = QStackedWidget(); self.stack.addWidget(a); self.stack.addWidget(b)

        self.check_btn = QPushButton(tr("Verbindung prüfen && Datastores laden"))
        self.datastore = QComboBox(); self.datastore.setEditable(True)
        self.namespace = QComboBox(); self.namespace.setEditable(True)
        self.namespace.lineEdit().setPlaceholderText(tr("leer = Wurzel des Datastores"))
        self.ns_info = QLabel(""); self.ns_info.setWordWrap(True)
        sel = QFormLayout()
        sel.addRow("", self.check_btn); sel.addRow(tr("Datastore"), self.datastore)
        sel.addRow(tr("Namespace"), self.namespace); sel.addRow("", self.ns_info)

        self.scope_home = QRadioButton(tr("Nur mein Home-Verzeichnis sichern (empfohlen für Desktop, kein sudo nötig)"))
        self.scope_full = QRadioButton(tr("Vollständiges System sichern (Dateiebene, braucht Administrator-Rechte)"))
        self.scope_home.setChecked(True)
        if not self.admin_ok:
            self.scope_full.setEnabled(False)
            self.scope_full.setToolTip(tr("Keine Administrator-Rechte erkannt (sudo/pkexec)"))
        self.scope_note = QLabel(tr("Ein Vollbackup sichert `/` plus lokale Dateisysteme wie /boot (ohne /proc, /sys, /tmp, Wechselmedien). Es ist KEIN Abbild: für eine Neuinstallation brauchst du Partitionierung und Bootloader separat."))
        self.scope_note.setWordWrap(True)
        self.mount_label = QLabel(tr("Laufwerke mitsichern (Haken = ja; USB-Laufwerke nur auf ausdrücklichen Haken):"))
        self.mount_list = MountList(); self.mount_list.setMaximumHeight(130)
        box = QGroupBox(tr("Umfang")); bl = QVBoxLayout(box)
        for w in (self.scope_home, self.scope_full, self.scope_note, self.mount_label, self.mount_list):
            bl.addWidget(w)
        self.scope_full.toggled.connect(self._scope_toggled)
        self._scope_toggled(False)

        self.schedule = QComboBox()
        for text, expr in SCHEDULE_PRESETS:
            self.schedule.addItem(text, expr)
        self.schedule.addItem(tr("Eigener Ausdruck …"), CUSTOM_SCHEDULE)
        self.schedule_custom = QLineEdit()
        self.schedule_custom.setPlaceholderText(tr("systemd-OnCalendar, z. B. Mon..Fri *-*-* 03:00:00"))
        self.schedule_custom.setVisible(False)
        self.schedule_info = QLabel(tr("Kein Zeitplan: das Backup startest du von Hand.")); self.schedule_info.setWordWrap(True)
        self.db_dump = QCheckBox(tr("Datenbanken laufender Container vor dem Backup dumpen (Postgres, MariaDB/MySQL)"))
        self.db_note = QLabel(tr("Optional. Ein Dump ist ein sauberer, prüfbarer Stand; ohne ihn werden die Datenbankdateien im laufenden Betrieb gelesen. MariaDB/MySQL-Zugänge richtest du danach ein."))
        self.db_note.setWordWrap(True)
        self.only_changed = QCheckBox(tr("Nur geänderte Dateien lesen (schneller; der erste Lauf liest einmal alles)"))
        self.only_changed.setChecked(True)
        self.weekly_data = QCheckBox(tr("Wöchentlich alles neu lesen und prüfen (Modus data, sonntags 03:00; empfohlen)"))
        self.weekly_data.setChecked(True)
        self.weekly_note = QLabel(tr("Fängt Änderungen ab, die der Metadaten-Vergleich nicht bemerkt, und Lesefehler in selten benutzten Dateien. Gilt nur mit einem Zeitplan."))
        self.weekly_note.setWordWrap(True)
        self.only_changed.toggled.connect(self.weekly_data.setEnabled)
        self.start_now = QCheckBox(tr("Direkt nach dem Anlegen das erste Backup starten"))
        run_box = QGroupBox(tr("Ablauf")); rl = QFormLayout(run_box)
        rl.addRow(tr("Zeitplan"), self.schedule); rl.addRow("", self.schedule_custom); rl.addRow("", self.schedule_info)
        rl.addRow(self.only_changed); rl.addRow(self.weekly_data); rl.addRow(self.weekly_note); rl.addRow(self.db_dump); rl.addRow(self.db_note); rl.addRow(self.start_now)
        self.schedule.currentIndexChanged.connect(self._schedule_changed)
        self.schedule_custom.editingFinished.connect(self._update_schedule_info)

        self.status = QLabel(""); self.status.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Einrichten"))
        self.buttons.accepted.connect(self._start)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form); lay.addWidget(self.stack); lay.addLayout(sel); lay.addWidget(box)
        lay.addWidget(run_box)
        lay.addWidget(self.status); lay.addWidget(self.buttons)

        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.fp_btn.clicked.connect(self._fetch_fp)
        self.check_btn.clicked.connect(self._check)
        for w in (self.server, self.auth_id, self.root_user):
            w.textChanged.connect(self._invalidate)
        self.port.valueChanged.connect(self._invalidate)
        self.datastore.currentTextChanged.connect(self._datastore_changed)
        self.namespace.currentTextChanged.connect(self._update_ns_info)

    # -- Zustand ------------------------------------------------------------
    def _busy(self, text: str, busy: bool = True) -> None:
        self.status.setText(text)
        for w in (self.buttons, self.fp_btn, self.check_btn):
            w.setEnabled(not busy)

    def _invalidate(self, *_a) -> None:
        """Server oder Zugang geändert: gespeicherte Sitzung und Listen verwerfen."""
        self._api, self._namespaces = None, None

    def _mode_changed(self, i: int) -> None:
        self.stack.setCurrentIndex(i)
        self._invalidate()
        self._update_ns_info()

    # -- Fingerprint --------------------------------------------------------
    def _fetch_fp(self) -> None:
        host, port = self.server.text().strip(), self.port.value()
        if not host:
            self.status.setText(tr("Bitte zuerst den Server eintragen."))
            return
        self._busy(tr("Hole Fingerprint …"))
        self._worker = Worker(lambda _l: fetch_fingerprint(host, port), self)
        self._worker.done.connect(lambda fp: (self.fp.setText(fp), self.fp_ok.setChecked(False), self._invalidate(),
                                              self._busy(tr("Fingerprint mit dem in PBS angezeigten Wert vergleichen und bestätigen."), False)))
        self._worker.failed.connect(lambda m: self._busy(tr("Fehler: {0}", m), False))
        self._worker.start()

    def _fp_confirmed(self) -> bool:
        if not self.fp.text() or not self.fp_ok.isChecked():
            self.status.setText(tr("Fingerprint abrufen und bestätigen (Schutz vor gefälschtem Server)."))
            return False
        return True

    # -- Verbindung prüfen, Datastores und Namespaces ------------------------
    def _check(self) -> None:
        if not self.server.text().strip() or not self._fp_confirmed():
            return
        host, port, fp = self.server.text().strip(), self.port.value(), self.fp.text()
        if self.mode.currentIndex() == 0:
            user, pw, tfa = self.root_user.text().strip(), self.root_pw.text(), self.tfa.text().strip()
            if not user or not pw:
                self.status.setText(tr("Root-Benutzer und Passwort eingeben."))
                return
            self.root_pw.clear()  # Passwort sofort aus dem Widget entfernen; die Sitzung bleibt im Speicher

            def work(_l):
                api = PbsApi(host, port, fp)
                api.login(user, pw, tfa)
                return api, api.list_datastores()
        else:
            auth_id, secret = self.auth_id.text().strip(), self.secret.text()
            if not auth_id or not secret:
                self.status.setText(tr("Token-ID und Secret eingeben."))
                return

            def work(_l):
                api = PbsApi(host, port, fp)
                api.use_token(auth_id, secret)
                return api, api.list_datastores()

        def done(res):
            self._api, stores = res
            self.datastore.blockSignals(True)
            self.datastore.clear(); self.datastore.addItems(stores)
            self.datastore.blockSignals(False)
            msg = (tr("Verbunden. {0} Datastore(s) gefunden.", len(stores)) if stores else
                   tr("Verbunden, aber keine Datastores sichtbar (fehlende Audit-Rechte?). Namen von Hand eingeben."))
            self._busy(msg, False)
            self._datastore_changed(self.datastore.currentText())

        self._busy(tr("Prüfe Verbindung …"))
        self._worker = Worker(work, self)
        self._worker.done.connect(done)
        self._worker.failed.connect(lambda m: self._busy(tr("Fehler: {0}", m), False))
        self._worker.start()

    def _datastore_changed(self, ds: str) -> None:
        self._namespaces = None
        self._set_ns_items([])
        if not self._api or not ds.strip():
            self._update_ns_info()
            return
        api = self._api
        self._ns_worker = Worker(lambda _l: api.list_namespaces(ds.strip()), self)
        self._ns_worker.done.connect(lambda ns: self._ns_loaded(ds, ns))
        self._ns_worker.failed.connect(lambda m: self.ns_info.setText(tr("Namespaces nicht lesbar: {0}", m)))
        self._ns_worker.start()

    def _ns_loaded(self, ds: str, namespaces: list[str]) -> None:
        if ds.strip() != self.datastore.currentText().strip():
            return  # veraltete Antwort
        self._namespaces = namespaces
        self._set_ns_items(namespaces)
        self._update_ns_info()

    def _set_ns_items(self, items: list[str]) -> None:
        current = self.namespace.currentText()
        self.namespace.blockSignals(True)
        self.namespace.clear(); self.namespace.addItems(items)
        self.namespace.setEditText(current)
        self.namespace.blockSignals(False)

    def namespace_state(self) -> str:
        return namespace_state(self._namespaces, self.namespace.currentText())

    def _scope_toggled(self, full: bool) -> None:
        self.mount_label.setVisible(full)
        self.mount_list.setVisible(full)
        if full:
            self.mount_list.load(None)  # Standardauswahl: interne Laufwerke

    def backup_mode(self) -> str:
        return FULL if self.scope_full.isChecked() else HOME

    def _scoped(self, profile: Profile) -> Profile:
        """Setzt Umfang, Modus (System/Benutzer) und Quellen gemäß der Auswahl."""
        profile.backup_mode = self.backup_mode()
        profile.schedule, profile.db_dump = self._schedule, self.db_dump.isChecked()
        profile.change_detection = METADATA if self.only_changed.isChecked() else LEGACY
        profile.weekly_data = self.only_changed.isChecked() and self.weekly_data.isChecked()
        if profile.backup_mode == FULL:
            profile.scope, profile.sources = "system", ["/"]
            profile.full_mounts = self.mount_list.selected()
        else:
            profile.scope, profile.sources, profile.full_mounts = "user", [str(Path.home())], None
        return profile

    # -- Zeitplan -------------------------------------------------------------
    def _schedule_changed(self, *_a) -> None:
        self.schedule_custom.setVisible(self.schedule.currentData() == CUSTOM_SCHEDULE)
        self._update_schedule_info()

    def _update_schedule_info(self) -> None:
        custom = self.schedule.currentData() == CUSTOM_SCHEDULE
        expr = self.schedule_custom.text().strip() if custom else (self.schedule.currentData() or "")
        if not expr:
            self.schedule_info.setText(tr("Kein Zeitplan: das Backup startest du von Hand."))
            return
        nxt = next_elapse(expr)
        self.schedule_info.setText(tr("Nächster Lauf: {0}", nxt) if nxt else tr("Zeitplan nicht erkannt (systemd-analyze)."))

    def schedule_expr(self) -> str | None:
        """Gewählter Zeitplan ('' = keiner). None bei ungültigem Ausdruck (Meldung steht dann im Status)."""
        val = self.schedule.currentData() or ""
        if val != CUSTOM_SCHEDULE:
            return val
        expr = self.schedule_custom.text().strip()
        if not expr:
            self.status.setText(tr("Eigenen Zeitplan eintragen oder eine Vorgabe wählen."))
            return None
        try:
            validate_calendar(expr)
        except SchedulerError as e:
            self.status.setText(tr("Zeitplan ungültig: {0}", e))
            return None
        return expr

    def _update_ns_info(self, *_a) -> None:
        self.ns_info.setText(namespace_message(self.namespace_state(), self.mode.currentIndex() == 0))

    # -- Einrichten -----------------------------------------------------------
    def _start(self) -> None:
        ds, ns = self.datastore.currentText().strip(), self.namespace.currentText().strip().strip("/")
        if not (self.name.text().strip() and self.server.text().strip() and ds):
            self.status.setText(tr("Name, Server und Datastore sind Pflicht."))
            return
        if not self._fp_confirmed():
            return
        if not self._api:
            self.status.setText(tr("Bitte zuerst „Verbindung prüfen“ ausführen."))
            return
        weg_a = self.mode.currentIndex() == 0
        if not weg_a and self.namespace_state() == "new":
            self.status.setText(tr("Dieser Namespace existiert nicht und lässt sich mit Weg B nicht anlegen."))
            return
        schedule = self.schedule_expr()   # vor dem Anlegen des Zugangs: ein Tippfehler soll nichts halb einrichten
        if schedule is None:
            return
        self._schedule = schedule
        host, port, fp, name = self.server.text().strip(), self.port.value(), self.fp.text(), self.name.text().strip()
        hostname, api = socket.gethostname(), self._api

        if weg_a:
            def work(_l):
                try:
                    res = api.provision(ds, hostname, ns, replace_token=self._replace_token)
                except TokenExistsError as e:
                    return e  # Rückfrage im GUI-Thread
                # Neue ACLs greifen bei PBS erst nach einigen Sekunden: darauf warten und prüfen
                check = PbsApi(host, port, fp)
                check.use_token(res.auth_id, res.secret)
                if not check.wait_for_backup_permission(ds, ns):
                    raise ApiError(tr("Zugang wurde angelegt, die Rechte sind auf dem Server aber nach 30 s noch nicht wirksam. Bitte in PBS prüfen und es später erneut versuchen."))
                return res

            def done(res):
                if isinstance(res, TokenExistsError):
                    yes = QMessageBox.question(
                        self, tr("Token existiert bereits"),
                        tr("{0}.\n\nDas Secret eines vorhandenen Tokens ist nicht mehr abrufbar. Token löschen und neu erzeugen? Andere Programme, die diesen Token nutzen, verlieren den Zugang.", res)
                    ) == QMessageBox.StandardButton.Yes
                    self._replace_token = yes
                    self._busy(tr("Bitte erneut auf „Einrichten“ klicken.") if yes else
                               tr("Abgebrochen: Token wurde nicht ersetzt."), False)
                    return
                self._finish(self._scoped(Profile(name=name, server=host, port=port, datastore=ds, namespace=ns,
                                                  auth_id=res.auth_id, fingerprint=fp, backup_id=hostname)),
                             res.secret)
        else:
            auth_id, secret = self.auth_id.text().strip(), self.secret.text()
            prof = Profile(name=name, server=host, port=port, datastore=ds, namespace=ns,
                           auth_id=auth_id, fingerprint=fp, backup_id=hostname)

            def work(_l):
                PbsClient(prof, secret).test_connection()  # prüft Zugangsdaten und Leserechte
                return None

            def done(_):
                self._finish(self._scoped(prof), secret)

        self._busy(tr("Richte ein …"))
        self._worker = Worker(work, self)
        self._worker.done.connect(done)
        self._worker.failed.connect(lambda m: self._busy(tr("Fehler: {0}", m), False))
        self._worker.start()

    def _finish(self, profile: Profile, token: str) -> None:
        self.profile, self.token = profile, token
        self.root_pw.clear(); self.secret.clear()
        self._api = None
        self.accept()
