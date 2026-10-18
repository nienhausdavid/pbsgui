"""Modale Dialoge der TUI."""
from __future__ import annotations

import asyncio
import os

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet, Static

from ..core.models import Profile
from ..core.onboarding import (connect_root, connect_token, namespace_message, namespace_state,
                               provision_and_verify, verify_token_profile)
from ..core.pbs_api import ApiError, PbsApi, TokenExistsError, fetch_fingerprint


class PromptScreen(ModalScreen[str | None]):
    """Einzeiliger Eingabedialog; liefert den Text oder None bei Abbruch."""
    BINDINGS = [("escape", "cancel", "Abbrechen")]

    def __init__(self, title: str, value: str = "", hint: str = ""):
        super().__init__()
        self._title, self._value, self._hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, markup=False)
            if self._hint:
                yield Static(self._hint, classes="hint", markup=False)
            yield Input(value=self._value, id="prompt-input")
            yield Button("OK", variant="primary", id="ok")
            yield Button("Abbrechen", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, _e) -> None:
        self.dismiss(self.query_one(Input).value)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if e.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "no", "Nein")]

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._text, markup=False)
            yield Button("Ja", variant="warning", id="yes")
            yield Button("Nein", id="no")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class AddServerScreen(ModalScreen["tuple[Profile, str] | None"]):
    """Server hinzufügen: Fingerprint -> Zugang -> Verbindung prüfen -> Datastore/Namespace -> Einrichten."""
    BINDINGS = [("escape", "cancel", "Abbrechen")]

    def __init__(self):
        super().__init__()
        self._api: PbsApi | None = None
        self._namespaces: list[str] | None = None
        self._replace_token = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Server hinzufügen", markup=False)
            yield Input(value=os.uname().nodename, placeholder="Profilname", id="name")
            yield Input(placeholder="Server, z. B. pbs.example.org", id="server")
            yield Input(value="8007", placeholder="Port", id="port")
            yield Button("Fingerprint abrufen", id="fp-btn")
            yield Static("", id="fp", markup=False)
            yield Checkbox("Fingerprint geprüft und vertrauenswürdig", id="fp-ok")
            with RadioSet(id="mode"):
                yield RadioButton("A: Einmalig als root anmelden (Zugang automatisch anlegen)", value=True)
                yield RadioButton("B: Vorhandenen API-Token verwenden")
            yield Input(value="root@pam", placeholder="Root-Benutzer (Weg A)", id="root-user")
            yield Input(placeholder="Root-Passwort (Weg A, wird nicht gespeichert)", password=True, id="root-pw")
            yield Input(placeholder="2FA-Code (optional)", id="tfa")
            yield Input(placeholder="Token-ID benutzer@pbs!name (Weg B)", id="token-id")
            yield Input(placeholder="Token-Secret (Weg B)", password=True, id="token-secret")
            yield Button("Verbindung prüfen & Datastores laden", id="check")
            yield Input(placeholder="Datastore", id="datastore")
            yield Static("", id="stores", markup=False)
            yield Input(placeholder="Namespace (leer = Wurzel)", id="namespace")
            yield Static("", id="ns-info", markup=False)
            yield Static("", id="status", markup=False)
            yield Button("Einrichten", variant="primary", id="setup")
            yield Button("Abbrechen", id="cancel")

    # -- Hilfen ---------------------------------------------------------------
    def _v(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _say(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    @property
    def weg_a(self) -> bool:
        return self.query_one("#mode", RadioSet).pressed_index == 0

    def _update_ns(self) -> None:
        state = namespace_state(self._namespaces, self._v("namespace"))
        self.query_one("#ns-info", Static).update(namespace_message(state, self.weg_a))

    def _invalidate(self) -> None:
        self._api, self._namespaces = None, None

    # -- Ereignisse --------------------------------------------------------------
    def on_input_changed(self, e: Input.Changed) -> None:
        if e.input.id in ("server", "port", "token-id", "root-user"):
            self._invalidate()
        if e.input.id == "namespace":
            self._update_ns()

    def on_radio_set_changed(self, _e) -> None:
        self._invalidate()
        self._update_ns()

    async def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "datastore":
            await self._load_namespaces()

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "cancel":
            self.dismiss(None)
        elif bid == "fp-btn":
            await self._fetch_fp()
        elif bid == "check":
            await self._check()
        elif bid == "setup":
            await self._setup()

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def _fetch_fp(self) -> None:
        host = self._v("server")
        if not host:
            self._say("Bitte zuerst den Server eintragen.")
            return
        self._say("Hole Fingerprint …")
        try:
            fp = await asyncio.to_thread(fetch_fingerprint, host, int(self._v("port") or 8007))
        except (ApiError, ValueError) as ex:
            self._say(f"Fehler: {ex}")
            return
        self.query_one("#fp", Static).update(fp)
        self.query_one("#fp-ok", Checkbox).value = False
        self._invalidate()
        self._say("Fingerprint mit dem in PBS angezeigten Wert vergleichen und bestätigen.")

    def _fp(self) -> str:
        fp = str(self.query_one("#fp", Static).render())
        if not fp.strip() or not self.query_one("#fp-ok", Checkbox).value:
            self._say("Fingerprint abrufen und bestätigen (Schutz vor gefälschtem Server).")
            return ""
        return fp.strip()

    async def _check(self) -> None:
        fp = self._fp()
        host = self._v("server")
        if not fp or not host:
            return
        port = int(self._v("port") or 8007)
        if self.weg_a:
            user, pw, tfa = self._v("root-user"), self.query_one("#root-pw", Input).value, self._v("tfa")
            if not user or not pw:
                self._say("Root-Benutzer und Passwort eingeben.")
                return
            self.query_one("#root-pw", Input).value = ""  # Passwort sofort aus dem Widget entfernen
            make = lambda: connect_root(host, port, fp, user, pw, tfa)
        else:
            tid, sec = self._v("token-id"), self.query_one("#token-secret", Input).value
            if not tid or not sec:
                self._say("Token-ID und Secret eingeben.")
                return
            make = lambda: connect_token(host, port, fp, tid, sec)

        def work():
            api = make()
            return api, api.list_datastores()

        self._say("Prüfe Verbindung …")
        try:
            self._api, stores = await asyncio.to_thread(work)
        except ApiError as ex:
            self._say(f"Fehler: {ex}")
            return
        self.query_one("#stores", Static).update("Verfügbar: " + (", ".join(stores) if stores else "(keine sichtbar)"))
        if stores and not self._v("datastore"):
            self.query_one("#datastore", Input).value = stores[0]
        self._say(f"Verbunden. {len(stores)} Datastore(s) gefunden.")
        await self._load_namespaces()

    async def _load_namespaces(self) -> None:
        ds, api = self._v("datastore"), self._api
        self._namespaces = None
        if api and ds:
            try:
                self._namespaces = await asyncio.to_thread(api.list_namespaces, ds)
            except ApiError as ex:
                self.query_one("#ns-info", Static).update(f"Namespaces nicht lesbar: {ex}")
                return
        self._update_ns()

    async def _setup(self) -> None:
        fp, host, ds = self._fp(), self._v("server"), self._v("datastore")
        ns, name = self._v("namespace").strip("/"), self._v("name")
        if not (fp and host and ds and name):
            self._say("Name, Server, Datastore und bestätigter Fingerprint sind Pflicht.")
            return
        if not self._api:
            self._say("Bitte zuerst „Verbindung prüfen“ ausführen.")
            return
        weg_a, api, port = self.weg_a, self._api, int(self._v("port") or 8007)
        hostname = os.uname().nodename
        if not weg_a and namespace_state(self._namespaces, ns) == "new":
            self._say("Dieser Namespace existiert nicht und lässt sich mit Weg B nicht anlegen.")
            return
        self._say("Richte ein …")
        try:
            if weg_a:
                try:
                    res = await asyncio.to_thread(provision_and_verify, api, host, port, fp, ds, ns,
                                                  self._replace_token, hostname)
                except TokenExistsError as ex:
                    self._say(f"{ex}. Zum Ersetzen (löscht den Token!) erneut „Einrichten“ drücken.")
                    self._replace_token = True
                    return
                auth_id, secret = res.auth_id, res.secret
            else:
                auth_id, secret = self._v("token-id"), self.query_one("#token-secret", Input).value
                prof = Profile(name=name, server=host, port=port, datastore=ds, namespace=ns, auth_id=auth_id,
                               fingerprint=fp, backup_id=hostname)
                await asyncio.to_thread(verify_token_profile, prof, secret)
        except Exception as ex:  # Fehler anzeigen, Dialog bleibt offen
            self._say(f"Fehler: {ex}")
            return
        self.dismiss((Profile(name=name, server=host, port=port, datastore=ds, namespace=ns, auth_id=auth_id,
                              fingerprint=fp, backup_id=hostname), secret))
