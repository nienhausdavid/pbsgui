"""Dialoge für den Master-Schlüssel: einrichten (privaten Teil sichern lassen) und den Schlüssel aus einem Snapshot zurückholen."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from ..core.keys import write_private
from ..core.manager import ProfileManager
from ..core.models import Profile, Snapshot
from ..i18n import tr
from .insight import Runner

CLEAR_MS = 120_000


_pending: set[str] = set()      # kopierte Geheimnisse, die noch in der Zwischenablage liegen können


def _clear_if_ours(text: str) -> None:
    _pending.discard(text)
    cb = QApplication.clipboard()
    if cb.text() == text:
        cb.clear()


def copy_secret(text: str, clear_ms: int = CLEAR_MS) -> None:
    """Geheimnis in die Zwischenablage legen. Es wird nach zwei Minuten und beim Beenden des Programms wieder entfernt, falls
    dort noch unser Text steht, und ist für Zwischenablage-Verläufe (KDE Klipper und andere) als Geheimnis markiert, damit
    sie es nicht aufbewahren."""
    app = QApplication.instance()
    if not app.property("pbsgui_clip_hook"):
        app.setProperty("pbsgui_clip_hook", True)
        app.aboutToQuit.connect(lambda: [_clear_if_ours(t) for t in list(_pending)])
    mime = QMimeData()
    mime.setText(text)
    mime.setData("x-kde-passwordManagerHint", b"secret")
    QApplication.clipboard().setMimeData(mime)
    _pending.add(text)
    QTimer.singleShot(clear_ms, lambda: _clear_if_ours(text))


def save_secret(parent, text: str, title: str, default_name: str) -> str | None:
    """Geheimnis in eine gewählte Datei schreiben (0600, einem Link am Ziel wird nicht gefolgt). None bei Abbruch oder Fehler."""
    dest, _ = QFileDialog.getSaveFileName(parent, title, str(Path.home() / default_name))
    if not dest:
        return None
    try:
        write_private(dest, text)
    except OSError as e:
        QMessageBox.warning(parent, tr("Speichern fehlgeschlagen"), str(e))
        return None
    return dest


class MasterKeySetupDialog(QDialog):
    """Erzeugt das Schlüsselpaar und besteht darauf, dass der PRIVATE Teil gesichert wird, bevor der Dialog schließt."""

    def __init__(self, mgr: ProfileManager, profile: Profile, parent=None):
        super().__init__(parent)
        self.mgr, self.profile = mgr, profile
        self.key = None
        self.saved = False
        self.setWindowTitle(tr("Master-Schlüssel einrichten"))
        self.setMinimumWidth(560)
        intro = QLabel(tr("Der Master-Schlüssel ist die Versicherung gegen Schlüsselverlust: Jedes Backup enthält danach eine Kopie des Verschlüsselungs-Schlüssels, die nur der PRIVATE Master-Schlüssel öffnet. Den privaten Teil und seine Passphrase bewahrst du getrennt vom Rechner auf (Passwortmanager, Tresor). Er wird nie von pbsgui gespeichert."))
        intro.setWordWrap(True)
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2 = QLineEdit(); self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self.all = QCheckBox(tr("Gleichen Master-Schlüssel für alle verschlüsselten Profile verwenden")); self.all.setChecked(True)
        form = QFormLayout()
        form.addRow(tr("Passphrase (mindestens 8 Zeichen, leer = ohne Passphrase)"), self.pw); form.addRow(tr("Passphrase wiederholen"), self.pw2)
        self.make = QPushButton(tr("Erzeugen")); self.make.setObjectName("primary")
        self.status = QLabel(""); self.status.setWordWrap(True)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True); self.out.setVisible(False); self.out.setMaximumHeight(120)
        self.copy_btn = QPushButton(tr("Privaten Schlüssel kopieren")); self.file_btn = QPushButton(tr("In Datei speichern …"))
        self.ack = QCheckBox(tr("Ich habe den privaten Master-Schlüssel (und die Passphrase, falls vergeben) gesichert"))
        self.finish_btn = QPushButton(tr("Fertig")); self.finish_btn.setEnabled(False)
        for w in (self.copy_btn, self.file_btn, self.ack):
            w.setVisible(False)
        row = QHBoxLayout(); row.addWidget(self.copy_btn); row.addWidget(self.file_btn); row.addStretch()
        lay = QVBoxLayout(self)
        for w in (intro,):
            lay.addWidget(w)
        lay.addLayout(form); lay.addWidget(self.all); lay.addWidget(self.make); lay.addWidget(self.status); lay.addWidget(self.out)
        lay.addLayout(row); lay.addWidget(self.ack); lay.addWidget(self.finish_btn)
        self.runner = Runner(self, lambda m: (self.status.setText(tr("Fehler: {0}", m)), self.make.setEnabled(True)))
        self.make.clicked.connect(self._make)
        self.copy_btn.clicked.connect(self._copy)
        self.file_btn.clicked.connect(self._save)
        self.ack.toggled.connect(self._update_done)
        self.finish_btn.clicked.connect(self._finish)

    def _make(self) -> None:
        if self.pw.text() != self.pw2.text():
            self.status.setText(tr("Die Passphrasen stimmen nicht überein"))
            return
        if not self.pw.text() and QMessageBox.question(
                self, tr("Ohne Passphrase"),
                tr("Ohne Passphrase schützt nur die Datei selbst den privaten Master-Schlüssel: Wer sie hat, kann alle deine Backups entschlüsseln. Sie muss deshalb besonders gut verwahrt werden (Passwortmanager, Tresor). Trotzdem ohne Passphrase erzeugen?")) != QMessageBox.StandardButton.Yes:
            return
        pw, share = self.pw.text(), self.all.isChecked()
        self.make.setEnabled(False)
        self.status.setText(tr("Erzeuge den Master-Schlüssel (4096 Bit, dauert einen Moment) …"))
        self.runner.run(lambda _l: self.mgr.setup_master_key(self.profile, pw, share), self._made)

    def _made(self, key) -> None:
        self.key = key
        self.pw.clear(); self.pw2.clear()
        self.out.setPlainText(key.private_pem); self.out.setVisible(True)
        for w in (self.copy_btn, self.file_btn, self.ack):
            w.setVisible(True)
        self.status.setText(tr("Fertig erzeugt (Fingerprint {0}). Jetzt den PRIVATEN Schlüssel sichern und die Passphrase (falls vergeben) getrennt notieren.", key.fingerprint))

    def _copy(self) -> None:
        if self.key:
            copy_secret(self.key.private_pem)
            self.saved = True
            self.status.setText(tr("Kopiert. Jetzt im Passwortmanager einfügen und speichern (die Zwischenablage wird in 2 Minuten geleert)."))
            self._update_done()

    def _save(self) -> None:
        if self.key:
            dest = save_secret(self, self.key.private_pem, tr("Privaten Master-Schlüssel speichern"), "master-private.pem")
            if dest:
                self.saved = True
                self.status.setText(tr("Gespeichert in {0} (Rechte 0600). Am besten auf einen USB-Stick oder in den Tresor verschieben.", dest))
                self._update_done()

    def _update_done(self, *_a) -> None:
        self.finish_btn.setEnabled(bool(self.key and self.saved and self.ack.isChecked()))

    def _finish(self) -> None:
        try:
            self.mgr.confirm_master_key_backup(self.profile)
        except Exception as e:
            QMessageBox.warning(self, tr("Fehler"), str(e))
            return
        self.accept()

    def reject(self) -> None:
        if self.key and self.finish_btn.isEnabled():      # gesichert und bestätigt, nur per Fenster-X geschlossen: gilt als fertig
            self._finish()
            return
        if self.key and not self.finish_btn.isEnabled() and QMessageBox.question(
                self, tr("Master-Schlüssel nicht gesichert"),
                tr("Der private Master-Schlüssel wurde noch nicht gesichert. Wenn du jetzt schließt, ist er verloren (die Passphrase allein reicht nicht). Trotzdem schließen?")) != QMessageBox.StandardButton.Yes:
            return
        if self.key and self.result() != QDialog.DialogCode.Accepted:      # Sichern nie bestätigt: nicht als eingerichtet stehen lassen
            try:
                self.mgr.discard_master_key(self.key.fingerprint)
            except Exception as e:
                QMessageBox.warning(self, tr("Fehler"), str(e))
        super().reject()


class RecoverKeyDialog(QDialog):
    """Verschlüsselungs-Schlüssel mit dem privaten Master-Schlüssel aus einem Snapshot zurückholen."""

    def __init__(self, mgr: ProfileManager, profile: Profile, snapshots: list[Snapshot], parent=None):
        super().__init__(parent)
        from ..core.masterkey import ENCRYPTED_NAMES
        self.mgr, self.profile = mgr, profile
        self.snaps = [s for s in snapshots if any(n in s.files for n in ENCRYPTED_NAMES)]
        self.key_json = ""
        self.setWindowTitle(tr("Schlüssel zurückholen"))
        self.setMinimumWidth(520)
        info = QLabel(tr("Holt den Verschlüsselungs-Schlüssel mit dem privaten Master-Schlüssel aus einem Snapshot zurück. Es gibt nur in Snapshots eine Schlüsselkopie, die mit eingerichtetem Master-Schlüssel erstellt wurden."))
        info.setWordWrap(True)
        self.snap = QComboBox()
        import time
        for s in self.snaps:
            self.snap.addItem(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.backup_time)))
        self.priv = QLineEdit(); self.priv.setPlaceholderText(tr("Datei mit dem privaten Master-Schlüssel (master-private.pem)"))
        pick = QPushButton(tr("Datei wählen …"))
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.go = QPushButton(tr("Schlüssel zurückholen")); self.go.setObjectName("primary"); self.go.setEnabled(bool(self.snaps))
        self.status = QLabel(tr("Kein Snapshot mit Schlüsselkopie vorhanden.") if not self.snaps else ""); self.status.setWordWrap(True)
        self.copy_btn = QPushButton(tr("Schlüssel kopieren")); self.file_btn = QPushButton(tr("In Datei speichern …"))
        self.install_btn = QPushButton(tr("Im Profil einsetzen"))
        for w in (self.copy_btn, self.file_btn, self.install_btn):
            w.setVisible(False)
        form = QFormLayout()
        prow = QHBoxLayout(); prow.addWidget(self.priv, 1); prow.addWidget(pick)
        form.addRow(tr("Snapshot"), self.snap); form.addRow(tr("Privater Master-Schlüssel"), prow); form.addRow(tr("Passphrase (leer, wenn keine vergeben)"), self.pw)
        brow = QHBoxLayout(); brow.addWidget(self.copy_btn); brow.addWidget(self.file_btn); brow.addWidget(self.install_btn); brow.addStretch()
        lay = QVBoxLayout(self); lay.addWidget(info); lay.addLayout(form); lay.addWidget(self.go); lay.addWidget(self.status); lay.addLayout(brow)
        self.runner = Runner(self, lambda m: (self.status.setText(tr("Fehler: {0}", m)), self.go.setEnabled(True)))
        pick.clicked.connect(self._pick)
        self.go.clicked.connect(self._go)
        self.copy_btn.clicked.connect(lambda: (copy_secret(self.key_json), self.status.setText(tr("Kopiert (die Zwischenablage wird in 2 Minuten geleert)."))))
        self.file_btn.clicked.connect(self._save)
        self.install_btn.clicked.connect(self._install)

    def _pick(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, tr("Privater Master-Schlüssel"), str(Path.home()), "PEM (*.pem);;*")
        if f:
            self.priv.setText(f)

    def _go(self) -> None:
        try:
            pem = Path(self.priv.text()).expanduser().read_text()
        except (OSError, ValueError) as e:          # ValueError: keine Textdatei
            self.status.setText(tr("Fehler: {0}", e))
            return
        snap, pw = self.snaps[self.snap.currentIndex()], self.pw.text()
        self.go.setEnabled(False); self.status.setText(tr("Hole den Schlüssel …"))
        self.runner.run(lambda _l: self.mgr.recover_key(self.profile, snap, pem, pw), self._got)

    def _got(self, key_json: str) -> None:
        self.key_json = key_json
        self.pw.clear()
        self.go.setEnabled(True)
        for w in (self.copy_btn, self.file_btn, self.install_btn):
            w.setVisible(True)
        self.status.setText(tr("Schlüssel zurückgeholt. Jetzt sichern (kopieren oder als Datei) oder im Profil einsetzen."))

    def _save(self) -> None:
        dest = save_secret(self, self.key_json, tr("Schlüssel speichern"), f"{self.profile.slug}-key.json")
        if dest:
            self.status.setText(tr("Gespeichert in {0} (Rechte 0600).", dest))

    def _install(self) -> None:
        try:
            self.mgr.install_recovered_key(self.profile, self.key_json)
        except Exception as e:
            QMessageBox.warning(self, tr("Fehler"), str(e))
            return
        self.status.setText(tr("Schlüssel im Profil eingesetzt."))
        self.accept()
