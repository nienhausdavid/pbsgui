"""Master-Schlüssel in der Terminal-Oberfläche: einrichten und den privaten Teil sichern lassen."""
from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from ..core.manager import ProfileManager
from ..core.models import Profile
from ..i18n import tr


class MasterScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]

    def __init__(self, mgr: ProfileManager, profile: Profile):
        super().__init__()
        self.mgr, self.profile = mgr, profile
        self.key = None
        self.saved = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(tr("Master-Schlüssel einrichten"), markup=False)
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
            yield Button(tr("Fertig"), id="mk-done", disabled=True)
            yield Button(tr("Abbrechen"), id="mk-cancel")

    def _say(self, text: str) -> None:
        self.query_one("#mk-status", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_checkbox_changed(self, e: Checkbox.Changed) -> None:
        self._update_done()

    def _update_done(self) -> None:
        self.query_one("#mk-done", Button).disabled = not (self.key and self.saved and self.query_one("#mk-ack", Checkbox).value)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "mk-cancel":
            self.dismiss(False)
        elif bid == "mk-make":
            self._make()
        elif bid == "mk-copy" and self.key:
            from ..core.clipboard import ClipboardError, copy_text
            try:
                copy_text(self.key.private_pem)
            except ClipboardError:
                self.app.copy_to_clipboard(self.key.private_pem)
            self.saved = True
            self._say(tr("Kopiert. Jetzt im Passwortmanager einfügen und speichern."))
            self._update_done()
        elif bid == "mk-save" and self.key:
            dest = Path(self.query_one("#mk-path", Input).value).expanduser()
            if not str(dest) or dest.exists():
                self._say(tr("Neue Datei angeben (die Datei darf noch nicht existieren)"))
                return
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.key.private_pem)
            self.saved = True
            self._say(tr("Gespeichert in {0} (Rechte 0600).", dest))
            self._update_done()
        elif bid == "mk-done":
            self.mgr.confirm_master_key_backup(self.profile)
            self.dismiss(True)

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
        self._say(tr("Fehler: {0}", msg))
        self.query_one("#mk-make", Button).disabled = False

    def _made(self, key) -> None:
        self.key = key
        self.query_one("#mk-pw", Input).value = ""
        self.query_one("#mk-pw2", Input).value = ""
        out = self.query_one("#mk-out", TextArea)
        out.disabled = False
        out.text = key.private_pem
        for wid in ("#mk-copy", "#mk-path", "#mk-save", "#mk-ack"):
            self.query_one(wid).disabled = False
        self._say(tr("Fertig erzeugt (Fingerprint {0}). Jetzt den PRIVATEN Schlüssel sichern und die Passphrase (falls vergeben) getrennt notieren.", key.fingerprint))
