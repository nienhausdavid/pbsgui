"""Dialog „Server ändern“ der Terminal-Oberfläche."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from ..i18n import tr
from ..core.connection import change_connection
from ..core.manager import ProfileManager
from ..core.models import Profile
from ..core.pbs_api import fetch_fingerprint


class ServerScreen(ModalScreen["object | None"]):
    """Liefert das ConnectionResult bei Erfolg, sonst None."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]

    def __init__(self, mgr: ProfileManager, profile: Profile):
        super().__init__()
        self.mgr, self.profile = mgr, profile

    def compose(self) -> ComposeResult:
        p = self.profile
        with Vertical(id="dialog"):
            yield Label(tr("Server ändern – {0}", p.name), markup=False)
            yield Static(tr("Wird mit dem Client geprüft, bevor gespeichert wird. Vorhandene Snapshots bleiben auf dem alten Ziel; das nächste Backup lädt am neuen Ziel alles neu hoch."), classes="hint", markup=False)
            yield Label("Server", markup=False); yield Input(p.server, id="srv-server")
            yield Label("Port", markup=False); yield Input(str(p.port), id="srv-port")
            yield Label("Fingerprint", markup=False); yield Input(p.fingerprint, id="srv-fp", disabled=True)
            yield Button(tr("Fingerprint vom Server abrufen"), id="srv-fetch")
            yield Checkbox(tr("Neuen Fingerprint geprüft und vertraut"), id="srv-fp-ok", disabled=True)
            yield Label("Datastore", markup=False); yield Input(p.datastore, id="srv-datastore")
            yield Label(tr("Namespace (leer = Wurzel)"), markup=False); yield Input(p.namespace, id="srv-namespace")
            yield Label("Token-ID", markup=False); yield Input(p.auth_id, id="srv-auth")
            yield Label(tr("Token-Secret (leer = behalten; bei neuer Token-ID nötig)"), markup=False)
            yield Input("", id="srv-secret", password=True)
            yield Static("", id="srv-status", markup=False)
            yield Button(tr("Prüfen und speichern"), variant="primary", id="srv-ok")
            yield Button(tr("Abbrechen"), id="srv-cancel")

    def _say(self, text: str) -> None:
        self.query_one("#srv-status", Static).update(text)

    def _v(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "srv-cancel":
            self.dismiss(None)
        elif e.button.id == "srv-fetch":
            self._say(tr("Hole Fingerprint …"))
            host, port = self._v("srv-server"), self._v("srv-port")
            self.run_worker(lambda: self._fetch(host, port), thread=True)
        elif e.button.id == "srv-ok":
            self._save()

    def _fetch(self, host: str, port: str) -> None:
        try:
            fp = fetch_fingerprint(host, int(port))
        except Exception as ex:
            self.app.call_from_thread(self._say, tr("Fehler: {0}", ex))
            return
        self.app.call_from_thread(self._got_fp, fp)

    def _got_fp(self, fp: str) -> None:
        self.query_one("#srv-fp", Input).value = fp
        ok = self.query_one("#srv-fp-ok", Checkbox)
        ok.disabled = fp == self.profile.fingerprint
        ok.value = False
        self._say(tr("Fingerprint unverändert.") if fp == self.profile.fingerprint else tr("Mit dem Wert in PBS vergleichen und bestätigen."))

    def _save(self) -> None:
        fp = self._v("srv-fp")
        if fp != self.profile.fingerprint and not self.query_one("#srv-fp-ok", Checkbox).value:
            self._say(tr("Bitte den neuen Fingerprint bestätigen."))
            return
        try:
            kw = dict(server=self._v("srv-server"), port=int(self._v("srv-port")), fingerprint=fp, datastore=self._v("srv-datastore"),
                      namespace=self._v("srv-namespace"), auth_id=self._v("srv-auth"),
                      token=self.query_one("#srv-secret", Input).value or None)
        except ValueError:
            self._say(tr("Ungültiger Port"))
            return
        self._say(tr("Prüfe Verbindung …"))

        def work() -> None:
            try:
                res = change_connection(self.mgr, self.profile, **kw)
            except Exception as ex:
                self.app.call_from_thread(self._say, tr("Fehler: {0}", ex))
                return
            self.app.call_from_thread(self.dismiss, res)
        self.run_worker(work, thread=True)
