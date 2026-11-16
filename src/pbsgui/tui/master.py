"""Master-Schlüssel in der Terminal-Oberfläche: einrichten und den privaten Teil sichern lassen."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from ..core.keys import write_private
from ..core.manager import ProfileManager
from ..core.models import Profile
from ..i18n import tr
from .screens import Dialog


def save_secret(path_text: str, content: str) -> Path:
    """Geheimnis in eine NEUE Datei mit Rechten 0600 schreiben; ValueError/OSError mit lesbarer Meldung, wenn das nicht geht."""
    if not path_text.strip():
        raise ValueError(tr("Neue Datei angeben (die Datei darf noch nicht existieren)"))
    dest = Path(path_text.strip()).expanduser()
    if dest.exists() or dest.is_symlink():
        raise ValueError(tr("Neue Datei angeben (die Datei darf noch nicht existieren)"))
    write_private(str(dest), content, exclusive=True)
    return dest


class MasterScreen(Dialog[bool]):
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]
    TITLE_TEXT = tr("Master-Schlüssel einrichten")

    def __init__(self, mgr: ProfileManager, profile: Profile):
        super().__init__()
        self.mgr, self.profile = mgr, profile
        self.key = None
        self.saved = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(tr("Versicherung gegen Schlüsselverlust: Jedes Backup enthält danach eine Kopie des Verschlüsselungs-Schlüssels, die nur der PRIVATE Master-Schlüssel öffnet. Den privaten Teil und die Passphrase getrennt vom Rechner aufbewahren (Passwortmanager, Tresor); pbsgui speichert ihn nie."), classes="hint", markup=False)
            yield Label(tr("Passphrase (mindestens 8 Zeichen, leer = ohne Passphrase)"), markup=False); yield Input("", id="mk-pw", password=True)
            yield Label(tr("Passphrase wiederholen"), markup=False); yield Input("", id="mk-pw2", password=True)
            yield Checkbox(tr("Gleichen Master-Schlüssel für alle verschlüsselten Profile verwenden"), True, id="mk-all")
            yield Button(tr("Erzeugen"), variant="primary", id="mk-make")
            yield Static("", id="mk-status", markup=False)
            yield TextArea("", id="mk-out", read_only=True, disabled=True)
            yield Button(tr("Privaten Schlüssel kopieren"), id="mk-copy", disabled=True)
            yield Input("", id="mk-path", placeholder=tr("Datei zum Speichern (neue Datei, Rechte 0600)"), disabled=True)
            yield Button(tr("In Datei speichern"), id="mk-save", disabled=True)
            yield Checkbox(tr("Ich habe den privaten Master-Schlüssel (und die Passphrase, falls vergeben) gesichert"), False, id="mk-ack", disabled=True)
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="mk-cancel")
                yield Button(tr("Fertig"), id="mk-done", variant="primary", disabled=True)

    def _say(self, text: str) -> None:
        self.query_one("#mk-status", Static).update(text)

    def action_cancel(self) -> None:
        if self.key and not self.query_one("#mk-done", Button).disabled:      # gesichert und bestätigt: gilt als fertig
            self._finish()
            return
        if self.key:      # erzeugt, aber das Sichern nie bestätigt: nicht als eingerichtet stehen lassen
            try:
                self.mgr.discard_master_key(self.key.fingerprint)
            except Exception as ex:
                self._say(tr("Fehler: {0}", ex))
                return
        self.dismiss(False)

    def _finish(self) -> None:
        try:
            self.mgr.confirm_master_key_backup(self.profile)
        except Exception as ex:
            self._say(tr("Fehler: {0}", ex))
            return
        self.dismiss(True)

    def on_checkbox_changed(self, e: Checkbox.Changed) -> None:
        self._update_done()

    def _update_done(self) -> None:
        self.query_one("#mk-done", Button).disabled = not (self.key and self.saved and self.query_one("#mk-ack", Checkbox).value)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "mk-cancel":
            self.action_cancel()
        elif bid == "mk-make":
            self._make()
        elif bid == "mk-copy" and self.key:
            def done(shown: bool) -> None:
                self.saved = True
                self._say(tr("Jetzt im Passwortmanager einfügen und speichern.") if shown else tr("Kopiert. Jetzt im Passwortmanager einfügen und speichern."))
                self._update_done()

            self.app.copy_text_out(self.key.private_pem, done)
        elif bid == "mk-save" and self.key:
            try:
                dest = save_secret(self.query_one("#mk-path", Input).value, self.key.private_pem)
            except (OSError, ValueError) as ex:      # Ordner fehlt, keine Rechte …: melden, der Schlüssel bleibt im Dialog
                self._say(tr("Fehler: {0}", ex))
                return
            self.saved = True
            self._say(tr("Gespeichert in {0} (Rechte 0600).", dest))
            self._update_done()
        elif bid == "mk-done":
            self._finish()

    def _make(self) -> None:
        pw, pw2 = self.query_one("#mk-pw", Input).value, self.query_one("#mk-pw2", Input).value
        if pw != pw2:
            self._say(tr("Die Passphrasen stimmen nicht überein"))
            return
        share = self.query_one("#mk-all", Checkbox).value
        self.query_one("#mk-make", Button).disabled = True
        self._say(tr("Erzeuge den Master-Schlüssel (4096 Bit, dauert einen Moment) …")
                  + ("\n" + tr("Ohne Passphrase schützt nur die Datei selbst den privaten Schlüssel: besonders gut verwahren.") if not pw else ""))

        def work() -> None:
            try:
                key = self.mgr.setup_master_key(self.profile, pw, share)
            except Exception as ex:
                self.app.call_from_thread(self._failed, str(ex))
                return
            self.app.call_from_thread(self._made, key)
        self.run_worker(work, thread=True)

    def _failed(self, msg: str) -> None:
        if not self.alive():
            return
        self._say(tr("Fehler: {0}", msg))
        self.query_one("#mk-make", Button).disabled = False

    def _made(self, key) -> None:
        if not self.alive():      # Dialog während des Erzeugens geschlossen: nicht als eingerichtet stehen lassen
            try:
                self.mgr.discard_master_key(key.fingerprint)
            except Exception:  # nosec B110
                pass
            return
        self.key = key
        self.query_one("#mk-pw", Input).value = ""
        self.query_one("#mk-pw2", Input).value = ""
        out = self.query_one("#mk-out", TextArea)
        out.disabled = False
        out.text = key.private_pem
        for wid in ("#mk-copy", "#mk-path", "#mk-save", "#mk-ack"):
            self.query_one(wid).disabled = False
        self._say(tr("Fertig erzeugt (Fingerprint {0}). Jetzt den PRIVATEN Schlüssel sichern und die Passphrase (falls vergeben) getrennt notieren.", key.fingerprint))


class RecoverKeyScreen(Dialog[bool]):
    """Verschlüsselungs-Schlüssel mit dem privaten Master-Schlüssel aus dem neuesten Snapshot mit Schlüsselkopie zurückholen."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]
    TITLE_TEXT = tr("Schlüssel zurückholen")

    def __init__(self, mgr: ProfileManager, profile: Profile):
        super().__init__()
        self.mgr, self.profile = mgr, profile
        self.key_json = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(tr("Holt den Verschlüsselungs-Schlüssel mit dem privaten Master-Schlüssel aus einem Snapshot zurück. Es gibt nur in Snapshots eine Schlüsselkopie, die mit eingerichtetem Master-Schlüssel erstellt wurden."), classes="hint", markup=False)
            yield Label(tr("Privater Master-Schlüssel"), markup=False)
            yield Input("", id="rk-pem", placeholder=tr("Datei mit dem privaten Master-Schlüssel (master-private.pem)"))
            yield Label(tr("Passphrase (leer, wenn keine vergeben)"), markup=False); yield Input("", id="rk-pw", password=True)
            yield Button(tr("Schlüssel zurückholen"), variant="primary", id="rk-go")
            yield Static("", id="rk-status", markup=False)
            yield Button(tr("Schlüssel kopieren"), id="rk-copy", disabled=True)
            yield Input("", id="rk-path", placeholder=tr("Datei zum Speichern (neue Datei, Rechte 0600)"), disabled=True)
            yield Button(tr("In Datei speichern"), id="rk-save", disabled=True)
            yield Button(tr("Im Profil einsetzen"), id="rk-install", disabled=True)
            yield Button(tr("Schließen"), id="rk-close")

    def _say(self, text: str) -> None:
        self.query_one("#rk-status", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "rk-close":
            self.dismiss(False)
        elif bid == "rk-go":
            self._go()
        elif bid == "rk-copy" and self.key_json:
            self.app.copy_text_out(self.key_json, lambda shown: self._say(
                tr("Jetzt im Passwortmanager einfügen und speichern.") if shown else tr("Kopiert. Jetzt im Passwortmanager einfügen und speichern.")))
        elif bid == "rk-save" and self.key_json:
            try:
                dest = save_secret(self.query_one("#rk-path", Input).value, self.key_json)
            except (OSError, ValueError) as ex:
                self._say(tr("Fehler: {0}", ex))
                return
            self._say(tr("Gespeichert in {0} (Rechte 0600).", dest))
        elif bid == "rk-install" and self.key_json:
            try:
                self.mgr.install_recovered_key(self.profile, self.key_json)
            except Exception as ex:
                self._say(tr("Fehler: {0}", ex))
                return
            self.dismiss(True)

    def _go(self) -> None:
        from ..core.masterkey import ENCRYPTED_NAMES
        try:
            pem = Path(self.query_one("#rk-pem", Input).value.strip()).expanduser().read_text()
        except (OSError, ValueError) as ex:          # ValueError: keine Textdatei
            self._say(tr("Fehler: {0}", ex))
            return
        pw_input = self.query_one("#rk-pw", Input)
        pw, pw_input.value = pw_input.value, ""          # Geheimnisse nicht länger als nötig in der Oberfläche halten
        self.query_one("#rk-go", Button).disabled = True
        self._say(tr("Hole den Schlüssel …"))

        def work() -> None:
            try:
                snaps = [s for s in self.mgr.snapshots(self.profile) if any(n in s.files for n in ENCRYPTED_NAMES)]
                if not snaps:
                    raise RuntimeError(tr("Kein Snapshot mit Schlüsselkopie vorhanden."))
                key_json = self.mgr.recover_key(self.profile, max(snaps, key=lambda s: s.backup_time), pem, pw)
            except Exception as ex:
                self.app.call_from_thread(self._failed, str(ex))
                return
            self.app.call_from_thread(self._got, key_json)
        self.run_worker(work, thread=True)

    def _failed(self, msg: str) -> None:
        if not self.alive():
            return
        self._say(tr("Fehler: {0}", msg))
        self.query_one("#rk-go", Button).disabled = False

    def _got(self, key_json: str) -> None:
        if not self.alive():
            return
        self.key_json = key_json
        self.query_one("#rk-go", Button).disabled = False
        for wid in ("#rk-copy", "#rk-path", "#rk-save", "#rk-install"):
            self.query_one(wid).disabled = False
        self.query_one("#rk-path", Input).value = str(Path.home() / f"{self.profile.slug}-key.json")
        self._say(tr("Schlüssel zurückgeholt. Jetzt sichern (kopieren oder als Datei) oder im Profil einsetzen."))
