"""Modale Dialoge der TUI."""
from __future__ import annotations

import asyncio
import os

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, ScreenResultType
from textual.widgets import Button, Checkbox, Input, Label, OptionList, RadioButton, RadioSet, Select, SelectionList, Static
from textual.widgets.option_list import Option

from ..i18n import tr
from ..core.manager import can_use_admin
from ..core.models import LEGACY, METADATA, Profile
from ..core.modes import FULL, HOME, mount_choices
from ..core.onboarding import (connect_root, connect_token, namespace_message, namespace_state,
                               provision_and_verify, verify_token_profile)
from ..core.pbs_api import ApiError, PbsApi, TokenExistsError, fetch_fingerprint
from ..core.scheduler import CUSTOM_SCHEDULE, SCHEDULE_PRESETS, SchedulerError, next_elapse, validate_calendar

NEW_NAMESPACE = "__new__"
ROOT_NAMESPACE = ""


def mount_selection(selected: list[str] | None, **kw) -> SelectionList:
    """Auswahlliste der erkannten Laufwerke (Bind-Duplikate gesperrt, USB nur auf ausdrückliche Wahl)."""
    items = [(f"{m.mountpoint}  ({m.fstype}, {m.kind})", m.mountpoint, on) for m, on in mount_choices(selected)
             if not m.bind]
    return SelectionList[str](*items, **kw)


class Dialog(ModalScreen[ScreenResultType]):
    """Basis der Dialoge: der Titel steht im Rahmen, und jedes Eingabefeld trägt seinen Namen im Rahmen (der Platzhalter
    verschwindet beim Tippen, danach wäre sonst nicht mehr zu sehen, wofür das Feld ist)."""
    TITLE_TEXT = ""

    def on_mount(self) -> None:
        for box in self.query("#dialog"):
            if self.TITLE_TEXT:
                box.border_title = escape(self.TITLE_TEXT)
        for inp in self.query(Input):
            if inp.placeholder and not inp.border_title:
                inp.border_title = escape(inp.placeholder)

    def alive(self) -> bool:
        """Ist der Dialog noch offen? Ergebnisse aus Hintergrund-Arbeit kommen manchmal erst nach dem Schließen an."""
        return self.is_attached


class KeysScreen(Dialog[None]):
    """Alle Tasten der Hauptansicht, nach Gruppen."""
    BINDINGS = [("escape", "close", tr("Schließen")), ("question_mark", "close", tr("Schließen")), ("q", "close", tr("Schließen"))]
    TITLE_TEXT = tr("Alle Tasten")

    def __init__(self, groups: list[tuple[str, list[tuple[str, str]]]]):
        super().__init__()
        self._groups = groups

    def compose(self) -> ComposeResult:
        text = Text()
        for title, keys in self._groups:
            text.append(("\n" if text else "") + title.upper() + "\n", style="bold dim")
            for key, what in keys:
                text.append(f"  {key:<3}", style="bold")
                text.append(f" {what}\n")
        text.rstrip()
        with VerticalScroll(id="dialog"):
            yield Static(text)
            with Horizontal(classes="buttons"):
                yield Button(tr("Schließen"), variant="primary", id="close")

    def on_button_pressed(self, _e: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class MountsScreen(Dialog["list[str] | None"]):
    """Laufwerke eines Vollbackup-Profils wählen (Leertaste = an/aus)."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]
    TITLE_TEXT = tr("Laufwerke mitsichern (Leertaste = an/aus)")

    def __init__(self, selected: list[str] | None):
        super().__init__()
        self._selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(tr("USB-Laufwerke werden nur gesichert, wenn du sie ausdrücklich auswählst. Bind-Mount-Duplikate werden nie doppelt gesichert."), classes="hint", markup=False)
            yield mount_selection(self._selected, id="mounts")
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="cancel")
                yield Button(tr("Speichern"), variant="primary", id="ok")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(list(self.query_one("#mounts", SelectionList).selected) if e.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PromptScreen(Dialog[str | None]):
    """Einzeiliger Eingabedialog; liefert den Text oder None bei Abbruch."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]

    def __init__(self, title: str, value: str = "", hint: str = ""):
        super().__init__()
        self._title, self._value, self._hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, markup=False)
            if self._hint:
                yield Static(self._hint, classes="hint", markup=False)
            yield Input(value=self._value, id="prompt-input")
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="cancel")
                yield Button("OK", variant="primary", id="ok")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, _e) -> None:
        self.dismiss(self.query_one(Input).value)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if e.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(Dialog[bool]):
    """Rückfrage. Der Fokus steht auf „Nein“: ein versehentliches Enter bestätigt nichts."""
    BINDINGS = [("escape", "no", tr("Nein")), ("n", "no", tr("Nein")), ("j", "yes", tr("Ja")), ("y", "yes", tr("Ja"))]

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._text, markup=False)
            with Horizontal(classes="buttons"):
                yield Button(tr("Nein"), id="no")
                yield Button(tr("Ja"), variant="warning", id="yes")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class AddServerScreen(Dialog["tuple[Profile, str, bool] | None"]):
    """Server hinzufügen: Fingerprint -> Zugang -> Verbindung prüfen -> Datastore/Namespace -> Einrichten.

    Ergebnis: (Profil, Token-Secret, „Backup direkt starten“).
    """
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]
    TITLE_TEXT = tr("Server hinzufügen")

    def __init__(self, taken=None):
        super().__init__()
        self._taken = taken or (lambda _name: False)      # gibt es schon ein Profil mit diesem Namen?
        self._api: PbsApi | None = None
        self._namespaces: list[str] | None = None
        self._replace_token = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Input(value=os.uname().nodename, placeholder=tr("Profilname"), id="name")
            yield Input(placeholder=tr("Server, z. B. pbs.example.org"), id="server")
            yield Input(value="8007", placeholder="Port", id="port")
            yield Button(tr("Fingerprint abrufen"), id="fp-btn")
            yield Static("", id="fp", markup=False)
            yield Checkbox(tr("Fingerprint geprüft und vertrauenswürdig"), id="fp-ok")
            with RadioSet(id="mode"):
                yield RadioButton(tr("A: Einmalig als root anmelden (Zugang automatisch anlegen)"), value=True)
                yield RadioButton(tr("B: Vorhandenen API-Token verwenden"))
            yield Input(value="root@pam", placeholder=tr("Root-Benutzer (Weg A)"), id="root-user")
            yield Input(placeholder=tr("Root-Passwort (Weg A, wird nicht gespeichert)"), password=True, id="root-pw")
            yield Input(placeholder=tr("2FA-Code (optional)"), id="tfa")
            yield Input(placeholder=tr("Token-ID benutzer@pbs!name (Weg B)"), id="token-id")
            yield Input(placeholder=tr("Token-Secret (Weg B)"), password=True, id="token-secret")
            yield Label(tr("Umfang"), markup=False)
            with RadioSet(id="scope"):
                yield RadioButton(tr("Nur mein Home-Verzeichnis (empfohlen, kein sudo nötig)"), value=True)
                yield RadioButton(tr("Vollständiges System, Dateiebene (braucht Administrator-Rechte)"), id="scope-full",
                                  disabled=not can_use_admin())
            yield Label(tr("Laufwerke (nur bei Vollbackup; Leertaste = an/aus)"), markup=False, id="mounts-label")
            yield mount_selection(None, id="mounts")
            yield Button(tr("Verbindung prüfen & Datastores laden"), id="check")
            yield Label("Datastore", markup=False)
            yield Select([], prompt=tr("Datastore wählen …"), id="datastore-select")
            yield Input(placeholder=tr("Datastore (oder hier von Hand eintragen)"), id="datastore")
            yield Static("", id="stores", markup=False)
            yield Label("Namespace", markup=False)
            yield Select([], prompt=tr("Namespace wählen …"), id="namespace-select")
            yield Input(placeholder=tr("Namespace (leer = Wurzel; neuer Name wird angelegt)"), id="namespace")
            yield Static("", id="ns-info", markup=False)
            yield Label(tr("Zeitplan"), markup=False)
            yield Select([(text, expr) for text, expr in SCHEDULE_PRESETS] + [(tr("Eigener Ausdruck …"), CUSTOM_SCHEDULE)],
                         value="", allow_blank=False, id="schedule-select")
            yield Input(placeholder=tr("systemd-OnCalendar, z. B. Mon..Fri *-*-* 03:00:00 (Enter prüft)"), id="schedule-custom")
            yield Static("", id="schedule-info", markup=False)
            yield Checkbox(tr("Datenbanken laufender Container vor dem Backup dumpen"), id="db-dump")
            yield Static(tr("Postgres und MariaDB/MySQL. Optional. Ein Dump ist ein sauberer, prüfbarer Stand; ohne ihn werden die Datenbankdateien im laufenden Betrieb gelesen. MariaDB/MySQL-Zugänge richtest du danach mit [p] ein."),
                         classes="hint", markup=False)
            yield Checkbox(tr("Nur geänderte Dateien lesen (schneller)"), value=True,
                           id="only-changed")
            yield Static(tr("Der erste Lauf liest einmal alles."), classes="hint", markup=False)
            yield Checkbox(tr("Wöchentlich alles neu lesen und prüfen (empfohlen)"), value=True,
                           id="weekly-data")
            yield Static(tr("Modus data, sonntags 03:00. Fängt Änderungen ab, die der Metadaten-Vergleich nicht bemerkt, und Lesefehler in selten benutzten Dateien. Gilt nur mit einem Zeitplan."), classes="hint", markup=False)
            yield Checkbox(tr("Direkt nach dem Anlegen das erste Backup starten"), id="start-now")
            yield Static("", id="status", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="cancel")
                yield Button(tr("Einrichten"), variant="primary", id="setup")

    # -- Hilfen ---------------------------------------------------------------
    def _v(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _say(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _port(self) -> int | None:
        """Port als Zahl; None (mit Meldung), wenn das Feld etwas anderes enthält."""
        try:
            port = int(self._v("port") or 8007)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            self._say(tr("Ungültiger Port"))
            return None
        return port

    @property
    def weg_a(self) -> bool:
        return self.query_one("#mode", RadioSet).pressed_index == 0

    def _update_ns(self) -> None:
        state = namespace_state(self._namespaces, self._v("namespace"))
        self.query_one("#ns-info", Static).update(namespace_message(state, self.weg_a))

    def _invalidate(self) -> None:
        self._api, self._namespaces = None, None

    # -- Dropdowns -------------------------------------------------------------------------------------
    @staticmethod
    def _chosen(select: Select) -> str | None:
        """Gewählter Wert oder None, wenn nichts gewählt ist (Select.BLANK/NULL je nach Textual-Version)."""
        return select.value if isinstance(select.value, str) else None

    def _set_select(self, select: Select, value: str | None) -> None:
        """Wert setzen, ohne dass das Select-Ereignis die Eingabefelder wieder überschreibt."""
        with self.prevent(Select.Changed):
            if value is None:
                select.clear()
            else:
                select.value = value

    def _fill_datastores(self, stores: list[str]) -> None:
        sel = self.query_one("#datastore-select", Select)
        with self.prevent(Select.Changed):
            sel.set_options([(s, s) for s in stores])
        sel.display = bool(stores)
        cur = self._v("datastore")
        self._set_select(sel, cur if cur in stores else None)

    def _fill_namespaces(self) -> None:
        """Namespace-Dropdown aus den gelesenen Namespaces; nur, wenn die Liste verfügbar ist."""
        sel = self.query_one("#namespace-select", Select)
        if self._namespaces is None:
            sel.display = False
            return
        options = [(tr("(Wurzel des Datastores, kein Namespace)"), ROOT_NAMESPACE),
                   *[(ns, ns) for ns in self._namespaces]]
        if self.weg_a:  # nur mit Weg A lässt sich ein Namespace anlegen
            options.append((tr("Neuen Namespace anlegen …"), NEW_NAMESPACE))
        with self.prevent(Select.Changed):
            sel.set_options(options)
        sel.display = True
        self._sync_namespace_select()

    def _sync_namespace_select(self) -> None:
        sel = self.query_one("#namespace-select", Select)
        ns = self._v("namespace").strip("/")
        known = self._namespaces or []
        if not ns:
            self._set_select(sel, ROOT_NAMESPACE)
        elif ns in known:
            self._set_select(sel, ns)
        else:
            self._set_select(sel, NEW_NAMESPACE if self.weg_a else None)

    def _schedule_expr(self) -> str | None:
        """Gewählter Zeitplan ('' = keiner). None bei ungültigem Ausdruck (Meldung steht dann im Status)."""
        val = self._chosen(self.query_one("#schedule-select", Select)) or ""
        if val != CUSTOM_SCHEDULE:
            return val
        expr = self._v("schedule-custom")
        if not expr:
            self._say(tr("Eigenen Zeitplan eintragen oder eine Vorgabe wählen."))
            return None
        try:
            validate_calendar(expr)
        except SchedulerError as ex:
            self._say(tr("Zeitplan ungültig: {0}", ex))
            return None
        return expr

    def _update_schedule_info(self) -> None:
        val = self._chosen(self.query_one("#schedule-select", Select)) or ""
        custom = val == CUSTOM_SCHEDULE
        self.query_one("#schedule-custom").display = custom
        expr = self._v("schedule-custom") if custom else val
        info = self.query_one("#schedule-info", Static)
        if not expr:
            info.update(tr("Kein Zeitplan: das Backup startest du von Hand (b)."))
            return
        nxt = next_elapse(expr)
        info.update(tr("Nächster Lauf: {0}", nxt) if nxt else tr("Zeitplan nicht erkannt (systemd-analyze)."))

    # -- Ereignisse --------------------------------------------------------------
    def on_checkbox_changed(self, e: Checkbox.Changed) -> None:
        if e.checkbox.id == "only-changed":   # der wöchentliche Prüflauf ergibt nur mit „nur Geändertes lesen“ Sinn
            self.query_one("#weekly-data", Checkbox).disabled = not e.value

    def on_input_changed(self, e: Input.Changed) -> None:
        if e.input.id in ("server", "port", "token-id", "root-user"):
            self._invalidate()
        if e.input.id == "namespace":
            self._update_ns()
            self._sync_namespace_select()

    async def on_select_changed(self, e: Select.Changed) -> None:
        value = self._chosen(e.select)
        if e.select.id == "schedule-select":
            self._update_schedule_info()
        elif e.select.id == "datastore-select" and value:
            self.query_one("#datastore", Input).value = value
            await self._load_namespaces()
        elif e.select.id == "namespace-select" and value is not None:
            ns = self.query_one("#namespace", Input)
            ns.value = "" if value in (ROOT_NAMESPACE, NEW_NAMESPACE) else value
            if value == NEW_NAMESPACE:
                ns.focus()

    def on_mount(self) -> None:
        self._sync_mounts()
        for wid in ("datastore-select", "namespace-select", "schedule-custom"):
            self.query_one(f"#{wid}").display = False   # erst sichtbar, wenn es etwas zu wählen gibt

    def _sync_mounts(self) -> None:
        full = self.query_one("#scope", RadioSet).pressed_index == 1
        self.query_one("#mounts").display = full
        self.query_one("#mounts-label").display = full

    def on_radio_set_changed(self, e) -> None:
        if e.radio_set.id == "scope":
            self._sync_mounts()
            return
        self._invalidate()
        self._update_ns()

    async def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "datastore":
            await self._load_namespaces()
        elif e.input.id == "schedule-custom":
            if self._schedule_expr() is not None:
                self._say("")
                self._update_schedule_info()

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
            self._say(tr("Bitte zuerst den Server eintragen."))
            return
        port = self._port()
        if port is None:
            return
        self._say(tr("Hole Fingerprint …"))
        try:
            fp = await asyncio.to_thread(fetch_fingerprint, host, port)
        except (ApiError, ValueError, OSError) as ex:
            self._say(tr("Fehler: {0}", ex))
            return
        self.query_one("#fp", Static).update(fp)
        self.query_one("#fp-ok", Checkbox).value = False
        self._invalidate()
        self._say(tr("Fingerprint mit dem in PBS angezeigten Wert vergleichen und bestätigen."))

    def _fp(self) -> str:
        fp = str(self.query_one("#fp", Static).render())
        if not fp.strip() or not self.query_one("#fp-ok", Checkbox).value:
            self._say(tr("Fingerprint abrufen und bestätigen (Schutz vor gefälschtem Server)."))
            return ""
        return fp.strip()

    async def _check(self) -> None:
        fp = self._fp()
        host = self._v("server")
        port = self._port() if fp and host else None
        if port is None:
            return
        if self.weg_a:
            user, pw, tfa = self._v("root-user"), self.query_one("#root-pw", Input).value, self._v("tfa")
            if not user or not pw:
                self._say(tr("Root-Benutzer und Passwort eingeben."))
                return
            self.query_one("#root-pw", Input).value = ""  # Passwort sofort aus dem Widget entfernen
            make = lambda: connect_root(host, port, fp, user, pw, tfa)
        else:
            tid, sec = self._v("token-id"), self.query_one("#token-secret", Input).value
            if not tid or not sec:
                self._say(tr("Token-ID und Secret eingeben."))
                return
            make = lambda: connect_token(host, port, fp, tid, sec)

        def work():
            api = make()
            return api, api.list_datastores()

        self._say(tr("Prüfe Verbindung …"))
        try:
            self._api, stores = await asyncio.to_thread(work)
        except (ApiError, OSError) as ex:
            if self.alive():
                self._say(tr("Fehler: {0}", ex))
            return
        if not self.alive():
            return
        self.query_one("#stores", Static).update(tr("Verfügbar: ") + (", ".join(stores) if stores else tr("(keine sichtbar, Namen von Hand eintragen)")))
        if stores and not self._v("datastore"):
            self.query_one("#datastore", Input).value = stores[0]
        self._fill_datastores(stores)
        self._say(tr("Verbunden. {0} Datastore(s) gefunden.", len(stores)))
        await self._load_namespaces()

    async def _load_namespaces(self) -> None:
        ds, api = self._v("datastore"), self._api
        self._namespaces = None
        if api and ds:
            try:
                self._namespaces = await asyncio.to_thread(api.list_namespaces, ds)
            except ApiError as ex:
                self.query_one("#ns-info", Static).update(tr("Namespaces nicht lesbar: {0}", ex))
                self._fill_namespaces()
                return
        self._update_ns()
        self._fill_namespaces()

    async def _setup(self) -> None:
        fp, host, ds = self._fp(), self._v("server"), self._v("datastore")
        ns, name = self._v("namespace").strip("/"), self._v("name")
        if not (fp and host and ds and name):
            self._say(tr("Name, Server, Datastore und bestätigter Fingerprint sind Pflicht."))
            return
        if self._taken(name):      # vor dem Anlegen des Zugangs: sonst überschriebe das neue Profil das vorhandene samt Token
            self._say(tr("Es gibt schon ein Profil mit dem Namen '{0}': bitte einen anderen Profilnamen wählen.", name))
            return
        if not self._api:
            self._say(tr("Bitte zuerst „Verbindung prüfen“ ausführen."))
            return
        port = self._port()
        if port is None:
            return
        weg_a, api = self.weg_a, self._api
        hostname = os.uname().nodename
        if not weg_a and namespace_state(self._namespaces, ns) == "new":
            self._say(tr("Dieser Namespace existiert nicht und lässt sich mit Weg B nicht anlegen."))
            return
        schedule = self._schedule_expr()
        if schedule is None:
            return
        self._say(tr("Richte ein …"))
        try:
            if weg_a:
                try:
                    res = await asyncio.to_thread(provision_and_verify, api, host, port, fp, ds, ns,
                                                  self._replace_token, hostname)
                except TokenExistsError as ex:
                    self._say(tr("{0}. Zum Ersetzen (löscht den Token!) erneut „Einrichten“ drücken.", ex))
                    self._replace_token = True
                    return
                auth_id, secret = res.auth_id, res.secret
            else:
                auth_id, secret = self._v("token-id"), self.query_one("#token-secret", Input).value
                prof = Profile(name=name, server=host, port=port, datastore=ds, namespace=ns, auth_id=auth_id,
                               fingerprint=fp, backup_id=hostname)
                await asyncio.to_thread(verify_token_profile, prof, secret)
        except Exception as ex:  # Fehler anzeigen, Dialog bleibt offen
            self._say(tr("Fehler: {0}", ex))
            return
        full = self.query_one("#scope", RadioSet).pressed_index == 1
        self.dismiss((Profile(name=name, server=host, port=port, datastore=ds, namespace=ns, auth_id=auth_id,
                              fingerprint=fp, backup_id=hostname, backup_mode=FULL if full else HOME,
                              scope="system" if full else "user", sources=["/"] if full else [],
                              schedule=schedule, db_dump=self.query_one("#db-dump", Checkbox).value,
                              change_detection=METADATA if self.query_one("#only-changed", Checkbox).value else LEGACY,
                              weekly_data=bool(self.query_one("#only-changed", Checkbox).value
                                               and self.query_one("#weekly-data", Checkbox).value),
                              full_mounts=list(self.query_one("#mounts", SelectionList).selected) if full else None),
                      secret, self.query_one("#start-now", Checkbox).value))


class DbCredScreen(Dialog[bool]):
    """Zugang zu einem MariaDB/MySQL-Container festlegen: vorhandener Benutzer oder Dump-Benutzer anlegen lassen."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]

    def __init__(self, mgr, profile: Profile, db):
        super().__init__()
        self._mgr, self._profile, self._db = mgr, profile, db

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(tr("Datenbank-Zugang: {0} ({1})", self._db.container, self._db.owner), markup=False)
            yield Static(tr("Der Zugang wird verschlüsselt gespeichert und nie aus .env-Dateien oder der Container-Umgebung gelesen. Empfohlen: ein eigener Benutzer nur mit Leserechten."), classes="hint",
                         markup=False)
            yield Input(placeholder=tr("Vorhandener Benutzer mit Leserechten"), id="cred-user")
            yield Input(placeholder=tr("Passwort"), password=True, id="cred-pw")
            yield Checkbox(tr("Stattdessen Dump-Benutzer (nur lesen) automatisch anlegen"), id="cred-create")
            yield Input(value="root", placeholder=tr("Admin-Benutzer (wird nur einmal benutzt)"), id="admin-user")
            yield Input(placeholder=tr("Admin-Passwort (wird nicht gespeichert)"), password=True, id="admin-pw")
            yield Static("", id="cred-status", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="cancel")
                yield Button(tr("Speichern & prüfen"), variant="primary", id="save")

    def _say(self, text: str) -> None:
        self.query_one("#cred-status", Static).update(text)

    def action_cancel(self) -> None:
        self.dismiss(False)

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "cancel":
            self.dismiss(False)
            return
        create = self.query_one("#cred-create", Checkbox).value
        user, pw = self.query_one("#cred-user", Input).value.strip(), self.query_one("#cred-pw", Input).value
        admin, admin_pw = self.query_one("#admin-user", Input).value.strip(), self.query_one("#admin-pw", Input).value
        for wid in ("cred-pw", "admin-pw"):   # Passwörter sofort aus den Widgets entfernen
            self.query_one(f"#{wid}", Input).value = ""
        if create and not (admin and admin_pw):
            self._say(tr("Admin-Benutzer und -Passwort eingeben."))
            return
        if not create and not (user and pw):
            self._say(tr("Benutzer und Passwort eingeben."))
            return
        self._say(tr("Prüfe …"))
        try:
            if create:
                await asyncio.to_thread(self._mgr.create_db_credential, self._profile, self._db, admin, admin_pw)
            else:
                await asyncio.to_thread(self._mgr.set_db_credential, self._profile, self._db, user, pw)
        except Exception as ex:   # Dialog bleibt offen
            if self.alive():
                self._say(tr("Fehler: {0}", ex))
            return
        if self.alive():
            self.dismiss(True)


class DbDumpScreen(Dialog[bool]):
    """Datenbank-Dumps des Profils: ein/aus, erkannte Datenbanken, Zugänge der MariaDB/MySQL-Container."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]
    TITLE_TEXT = tr("Datenbank-Dumps")

    def __init__(self, mgr, profile: Profile):
        super().__init__()
        self._mgr, self._profile = mgr, profile
        self._dbs: dict[str, object] = {}
        self._states: dict[str, str] = {}
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static(tr("Vor jedem Backup werden laufende Datenbank-Container gedumpt und als eigenes Archiv (dbdumps.pxar) mitgesichert. Gestoppte Container nicht: ihre Dateien sind konsistent und im normalen Backup enthalten."), classes="hint", markup=False)
            yield Checkbox(tr("Dumps vor jedem Backup anlegen"), value=self._profile.db_dump, id="dump-on")
            yield Static(tr("Suche Datenbank-Container …"), id="db-status", markup=False)
            yield OptionList(id="db-list")
            yield Button(tr("Ohne Passwort einrichten … (empfohlen, Container steht kurz still)"), id="socket", disabled=True)
            yield Button(tr("Zugang mit Passwort festlegen …"), id="cred", disabled=True)
            yield Button(tr("Zugang entfernen"), id="cred-del", disabled=True)
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="cancel")
                yield Button(tr("Speichern"), variant="primary", id="ok")

    def on_mount(self) -> None:
        self.run_worker(self._scan, thread=True)

    def _scan(self) -> None:
        from ..core.dbdump import access_state, find_databases
        try:
            dbs, err = find_databases(), ""
            creds = self._mgr.db_credentials(self._profile)
            states = {d.key: access_state(d, creds) for d in dbs}
        except Exception as ex:      # auch Unerwartetes anzeigen statt die Oberfläche zu beenden
            dbs, err, states = [], str(ex), {}
        self.app.call_from_thread(self._show, dbs, err, states)

    def _label(self, db) -> Text:
        how = self._states.get(db.key, "none" if db.engine != "mariadb" else "missing")
        if how == "none":
            state = tr("Postgres, kein Zugang nötig")
        elif how == "socket":
            state = tr("MariaDB/MySQL, Zugang: ohne Passwort (Socket)")
        elif how == "password":
            cred = self._mgr.db_credentials(self._profile).get(db.key) or {}
            state = tr("MariaDB/MySQL, Zugang: {0}", cred.get('user', '?'))
        else:
            state = tr("MariaDB/MySQL, Zugang FEHLT")
        return Text(f"{db.container} ({db.owner}): {state}")

    def _show(self, dbs: list, err: str = "", states: dict | None = None) -> None:
        if not self.alive():
            return
        self._states = states if states is not None else self._states
        self._dbs = {d.key: d for d in dbs}
        lst = self.query_one("#db-list", OptionList)
        lst.clear_options()
        lst.add_options([Option(self._label(d), id=d.key) for d in dbs])
        self.query_one("#db-status", Static).update(
            err or (tr("{0} Datenbank-Container gefunden.", len(dbs)) if dbs else
                    tr("Keine laufenden Datenbank-Container gefunden (oder kein Zugriff auf deren Docker-Socket).")))

    def on_option_list_option_highlighted(self, e: OptionList.OptionHighlighted) -> None:
        from ..core.dbdump import MARIADB
        self._current = e.option.id
        db = self._dbs.get(self._current or "")
        maria = bool(db and db.engine == MARIADB)
        self.query_one("#socket", Button).disabled = not (maria and self._states.get(self._current or "") != "socket")
        self.query_one("#cred", Button).disabled = not maria
        self.query_one("#cred-del", Button).disabled = not (
            maria and self._current in self._mgr.db_credentials(self._profile))

    def _setup_access(self, db) -> None:
        from ..core.dbdump import setup_socket_access
        failed = False
        try:
            setup_socket_access(db)
            msg = tr("{0}: passwortloser Lesezugang eingerichtet", db.container)
        except Exception as ex:
            msg, failed = tr("Fehler: {0}", ex), True
        self.app.call_from_thread(self.app.notify, escape_markup(msg), severity="error" if failed else "information", timeout=15)
        self._scan()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "cancel":
            self.dismiss(False)
        elif bid == "socket" and self._current in self._dbs:
            db = self._dbs[self._current]

            def go(yes):
                if yes:
                    self.query_one("#db-status", Static).update(tr("Richte {0} ein (Container steht kurz still) …", db.container))
                    self.run_worker(lambda: self._setup_access(db), thread=True)
            self.app.push_screen(ConfirmScreen(
                tr("{0} wird einmalig für etwa 15 bis 40 Sekunden gestoppt (Anwendungen, die ihn nutzen, verlieren kurz die Datenbank). Es wird kein Passwort gebraucht und keines gespeichert. Fortfahren?", db.container)), go)
        elif bid == "cred" and self._current in self._dbs:
            self.app.push_screen(DbCredScreen(self._mgr, self._profile, self._dbs[self._current]),
                                 lambda ok: self.run_worker(self._scan, thread=True) if ok else None)
        elif bid == "cred-del" and self._current:
            try:
                self._mgr.remove_db_credential(self._profile, self._current)
            except Exception as ex:
                self.app.notify(escape_markup(str(ex)), severity="error")
                return
            self.run_worker(self._scan, thread=True)
        elif bid == "ok":
            self._profile.db_dump = self.query_one("#dump-on", Checkbox).value
            try:
                self._mgr.save(self._profile)
            except Exception as ex:
                self.app.notify(escape_markup(str(ex)), severity="error")
                return
            self.dismiss(True)


def escape_markup(text: str) -> str:
    return escape(text)


class DetectScreen(Dialog["Profile | None"]):
    """Änderungserkennung eines Profils: nur Geändertes lesen und wöchentlicher Prüflauf (Modus data)."""
    BINDINGS = [("escape", "cancel", tr("Abbrechen"))]
    TITLE_TEXT = tr("Änderungserkennung")

    def __init__(self, profile: Profile):
        super().__init__()
        self._p = profile

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(tr("Nur Geändertes lesen: Der Client liest nur Dateien, deren Metadaten sich geändert haben. Das nächste Backup nach dem Einschalten liest einmal alles. Der PBS-Server muss Version 3.3 oder neuer sein."), classes="hint", markup=False)
            yield Checkbox(tr("Nur geänderte Dateien lesen"), value=self._p.change_detection == METADATA, id="det-changed")
            yield Checkbox(tr("Zusätzlich wöchentlich alles neu lesen (Modus data)"), value=self._p.weekly_data,
                           id="det-weekly", disabled=self._p.change_detection != METADATA)
            yield Input(value=self._p.data_schedule, placeholder=tr("Zeitpunkt (OnCalendar)"), id="det-schedule")
            yield Static("", id="det-status", markup=False)
            with Horizontal(classes="buttons"):
                yield Button(tr("Abbrechen"), id="cancel")
                yield Button(tr("Speichern"), variant="primary", id="ok")

    def on_checkbox_changed(self, e: Checkbox.Changed) -> None:
        if e.checkbox.id == "det-changed":
            self.query_one("#det-weekly", Checkbox).disabled = not e.value

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "cancel":
            self.dismiss(None)
            return
        sched = self.query_one("#det-schedule", Input).value.strip() or self._p.data_schedule
        try:
            validate_calendar(sched)
        except SchedulerError as ex:
            self.query_one("#det-status", Static).update(tr("Zeitplan ungültig: {0}", ex))
            return
        self._p.change_detection = METADATA if self.query_one("#det-changed", Checkbox).value else LEGACY
        self._p.weekly_data = bool(self.query_one("#det-weekly", Checkbox).value)
        self._p.data_schedule = sched
        self.dismiss(self._p)
