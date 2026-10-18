"""Dialog „Server hinzufügen“: Weg A (einmalig als root) oder Weg B (vorhandener API-Token)."""
from __future__ import annotations

import socket

from PySide6.QtWidgets import (QCheckBox, QMessageBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from ..core.client import PbsClient
from ..core.models import Profile
from ..core.pbs_api import ApiError, PbsApi, TokenExistsError, fetch_fingerprint
from .workers import Worker


class AddServerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Server hinzufügen")
        self.setMinimumWidth(480)
        self.profile: Profile | None = None
        self.token = ""
        self._worker: Worker | None = None
        self._replace_token = False

        self.mode = QComboBox()
        self.mode.addItems(["A: Einmalig als root anmelden (Zugang automatisch anlegen)",
                            "B: Vorhandenen API-Token verwenden"])
        self.name = QLineEdit(socket.gethostname())
        self.server = QLineEdit()
        self.server.setPlaceholderText("pbs.example.org")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(8007)
        self.datastore = QLineEdit()
        self.namespace = QLineEdit()
        self.namespace.setPlaceholderText("optional")
        self.fp = QLineEdit(); self.fp.setReadOnly(True)
        self.fp_btn = QPushButton("Fingerprint abrufen")
        self.fp_ok = QCheckBox("Ich habe den Fingerprint geprüft und vertraue ihm")

        form = QFormLayout()
        for label, w in [("Modus", self.mode), ("Profilname", self.name), ("Server", self.server),
                         ("Port", self.port), ("Datastore", self.datastore), ("Namespace", self.namespace),
                         ("Fingerprint", self.fp), ("", self.fp_btn), ("", self.fp_ok)]:
            form.addRow(label, w)

        self.root_user = QLineEdit("root@pam")
        self.root_pw = QLineEdit(); self.root_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.tfa = QLineEdit(); self.tfa.setPlaceholderText("nur bei aktiver Zwei-Faktor-Anmeldung")
        a = QWidget(); fa = QFormLayout(a)
        fa.addRow("Root-Benutzer", self.root_user)
        fa.addRow("Root-Passwort", self.root_pw)
        fa.addRow("2FA-Code", self.tfa)
        fa.addRow(QLabel("Das Passwort wird nur für diese Einrichtung verwendet und nicht gespeichert."))
        self.auth_id = QLineEdit(); self.auth_id.setPlaceholderText("benutzer@pbs!tokenname")
        self.secret = QLineEdit(); self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        b = QWidget(); fb = QFormLayout(b)
        fb.addRow("Token-ID", self.auth_id)
        fb.addRow("Token-Secret", self.secret)
        self.stack = QStackedWidget(); self.stack.addWidget(a); self.stack.addWidget(b)

        self.status = QLabel(""); self.status.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Einrichten")
        self.buttons.accepted.connect(self._start)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form); lay.addWidget(self.stack); lay.addWidget(self.status); lay.addWidget(self.buttons)
        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.fp_btn.clicked.connect(self._fetch_fp)

    def _busy(self, text: str, busy: bool = True) -> None:
        self.status.setText(text)
        self.buttons.setEnabled(not busy)
        self.fp_btn.setEnabled(not busy)

    def _fetch_fp(self) -> None:
        host, port = self.server.text().strip(), self.port.value()
        if not host:
            self.status.setText("Bitte zuerst den Server eintragen.")
            return
        self._busy("Hole Fingerprint …")
        self._worker = Worker(lambda _l: fetch_fingerprint(host, port), self)
        self._worker.done.connect(lambda fp: (self.fp.setText(fp), self.fp_ok.setChecked(False),
                                              self._busy("Fingerprint abgerufen. Mit dem in PBS angezeigten "
                                                         "Wert vergleichen und bestätigen.", False)))
        self._worker.failed.connect(lambda m: self._busy(f"Fehler: {m}", False))
        self._worker.start()

    def _start(self) -> None:
        if not all([self.name.text().strip(), self.server.text().strip(), self.datastore.text().strip()]):
            self.status.setText("Name, Server und Datastore sind Pflicht.")
            return
        if not self.fp.text() or not self.fp_ok.isChecked():
            self.status.setText("Fingerprint abrufen und bestätigen (Schutz vor gefälschtem Server).")
            return
        host, port, ds = self.server.text().strip(), self.port.value(), self.datastore.text().strip()
        ns, fp, name = self.namespace.text().strip(), self.fp.text(), self.name.text().strip()
        hostname = socket.gethostname()

        if self.mode.currentIndex() == 0:
            user, pw, tfa = self.root_user.text().strip(), self.root_pw.text(), self.tfa.text().strip()
            self.root_pw.clear()  # Passwort sofort aus dem Widget entfernen
            if not user or not pw:
                self.status.setText("Root-Benutzer und Passwort eingeben.")
                return

            def work(_l):
                api = PbsApi(host, port, fp)
                api.login(user, pw, tfa)
                try:
                    res = api.provision(ds, hostname, ns, replace_token=self._replace_token)
                except TokenExistsError as e:
                    return e  # Rückfrage im GUI-Thread
                # Neue ACLs greifen bei PBS erst nach einigen Sekunden: darauf warten und prüfen
                check = PbsApi(host, port, fp)
                check.use_token(res.auth_id, res.secret)
                if not check.wait_for_backup_permission(ds, ns):
                    raise ApiError("Zugang wurde angelegt, die Rechte sind auf dem Server aber nach 30 s noch "
                                   "nicht wirksam. Bitte in PBS prüfen und es später erneut versuchen.")
                return res

            def done(res):
                if isinstance(res, TokenExistsError):
                    yes = QMessageBox.question(
                        self, "Token existiert bereits",
                        f"{res}.\n\nDas Secret eines vorhandenen Tokens ist nicht mehr abrufbar. Token löschen "
                        "und neu erzeugen? Andere Programme, die diesen Token nutzen, verlieren den Zugang."
                    ) == QMessageBox.StandardButton.Yes
                    self._replace_token = yes
                    self._busy("Bitte das Root-Passwort erneut eingeben und 'Einrichten' klicken (es wird nicht "
                               "gespeichert)." if yes else "Abgebrochen: Token wurde nicht ersetzt.", False)
                    return
                self._finish(Profile(name=name, server=host, port=port, datastore=ds, namespace=ns,
                                     auth_id=res.auth_id, fingerprint=fp, backup_id=hostname), res.secret)
        else:
            auth_id, secret = self.auth_id.text().strip(), self.secret.text()
            if not auth_id or not secret:
                self.status.setText("Token-ID und Secret eingeben.")
                return
            prof = Profile(name=name, server=host, port=port, datastore=ds, namespace=ns,
                           auth_id=auth_id, fingerprint=fp, backup_id=hostname)

            def work(_l):
                PbsClient(prof, secret).test_connection()  # prüft Zugangsdaten und Leserechte
                return None

            def done(_):
                self._finish(prof, secret)

        self._busy("Richte ein …")
        self._worker = Worker(work, self)
        self._worker.done.connect(done)
        self._worker.failed.connect(lambda m: self._busy(f"Fehler: {m}", False))
        self._worker.start()

    def _finish(self, profile: Profile, token: str) -> None:
        self.profile, self.token = profile, token
        self.root_pw.clear(); self.secret.clear()
        self.accept()
