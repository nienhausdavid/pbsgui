"""Dialog „Server ändern“: Verbindung eines vorhandenen Profils nachträglich anpassen."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout)

from ..i18n import tr
from ..core.connection import change_connection
from ..core.manager import ProfileManager
from ..core.models import Profile
from ..core.pbs_api import fetch_fingerprint
from .workers import Worker


class EditServerDialog(QDialog):
    def __init__(self, mgr: ProfileManager, profile: Profile, parent=None):
        super().__init__(parent)
        self.mgr, self.profile = mgr, profile
        self.result_info = None
        self._worker: Worker | None = None
        self.setWindowTitle(tr("Server ändern – {0}", profile.name))
        self.setMinimumWidth(520)
        self.server = QLineEdit(profile.server)
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(profile.port)
        self.fp = QLineEdit(profile.fingerprint); self.fp.setReadOnly(True)
        self.fp_btn = QPushButton(tr("Fingerprint vom Server abrufen"))
        self.fp_ok = QCheckBox(tr("Ich habe den neuen Fingerprint geprüft und vertraue ihm")); self.fp_ok.setVisible(False)
        self.datastore = QLineEdit(profile.datastore)
        self.namespace = QLineEdit(profile.namespace); self.namespace.setPlaceholderText(tr("leer = Wurzel des Datastores"))
        self.auth_id = QLineEdit(profile.auth_id)
        self.secret = QLineEdit(); self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText(tr("leer = gespeichertes Secret behalten (nur bei geänderter Token-ID nötig)"))
        form = QFormLayout()
        for label, w in (("Server", self.server), ("Port", self.port), ("Fingerprint", self.fp), ("", self.fp_btn), ("", self.fp_ok),
                         (tr("Datastore"), self.datastore), (tr("Namespace"), self.namespace), ("Token-ID", self.auth_id),
                         ("Token-Secret", self.secret)):
            form.addRow(label, w)
        self.info = QLabel(tr("Die neuen Angaben werden mit dem Client geprüft, bevor etwas gespeichert wird. Profilname, Schlüssel, Quellen und Zeitplan bleiben. Vorhandene Snapshots bleiben auf dem alten Ziel; das nächste Backup lädt am neuen Ziel alles neu hoch."))
        self.info.setWordWrap(True); self.info.setObjectName("muted")
        self.status = QLabel(""); self.status.setWordWrap(True)
        self.ok_btn = QPushButton(tr("Prüfen && speichern")); self.ok_btn.setObjectName("primary")
        cancel = QPushButton(tr("Abbrechen"))
        row = QHBoxLayout(); row.addStretch(); row.addWidget(cancel); row.addWidget(self.ok_btn)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addWidget(self.info); lay.addWidget(self.status); lay.addLayout(row)
        self.fp_btn.clicked.connect(self._fetch_fp)
        self.ok_btn.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        self.server.textChanged.connect(self._endpoint_changed); self.port.valueChanged.connect(self._endpoint_changed)

    def _endpoint_changed(self, *_a) -> None:
        moved = (self.server.text().strip(), self.port.value()) != (self.profile.server, self.profile.port)
        if moved and self.fp.text() == self.profile.fingerprint:
            self.status.setText(tr("Server oder Port geändert: Fingerprint des neuen Servers abrufen, falls sein Zertifikat ein anderes ist."))

    def _fetch_fp(self) -> None:
        host, port = self.server.text().strip(), self.port.value()
        self.status.setText(tr("Hole Fingerprint …"))
        self._worker = Worker(lambda _l: fetch_fingerprint(host, port), self)
        self._worker.done.connect(self._got_fp)
        self._worker.failed.connect(lambda m: self.status.setText(tr("Fehler: {0}", m)))
        self._worker.start()

    def _got_fp(self, fp: str) -> None:
        self.fp.setText(fp)
        self.fp_ok.setVisible(fp != self.profile.fingerprint); self.fp_ok.setChecked(False)
        self.status.setText(tr("Fingerprint abgerufen. Bitte mit dem Wert in PBS vergleichen (Dashboard → Zertifikat).")
                            if fp != self.profile.fingerprint else tr("Der Fingerprint ist unverändert."))

    def _save(self) -> None:
        fp = self.fp.text().strip()
        if fp != self.profile.fingerprint and not self.fp_ok.isChecked():
            self.status.setText(tr("Bitte den neuen Fingerprint bestätigen (Haken setzen)."))
            return
        kw = dict(server=self.server.text(), port=self.port.value(), fingerprint=fp, datastore=self.datastore.text(),
                  namespace=self.namespace.text(), auth_id=self.auth_id.text(), token=self.secret.text() or None)
        self.ok_btn.setEnabled(False); self.status.setText(tr("Prüfe Verbindung …"))
        self._worker = Worker(lambda _l: change_connection(self.mgr, self.profile, **kw), self)
        self._worker.done.connect(self._saved)
        self._worker.failed.connect(lambda m: (self.ok_btn.setEnabled(True), self.status.setText(tr("Fehler: {0}", m))))
        self._worker.start()

    def _saved(self, res) -> None:
        self.result_info = res
        self.ok_btn.setEnabled(True)
        if not res.changed:
            self.status.setText(tr("Nichts geändert."))
            return
        if res.notes:
            QMessageBox.information(self, tr("Gespeichert"), tr("Verbindung geändert: ") + ", ".join(res.changed) + "\n\n" + "\n\n".join(res.notes))
        self.accept()
