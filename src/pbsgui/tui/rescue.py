"""Geführter Wiederherstellungs-Assistent (Textual) für die Rettungs-ISO.

Schritte: Verbindung -> Snapshot -> Ziel-Platte -> Vorschau und Bestätigung -> Wiederherstellung.
Die Logik liegt in pbsgui.core.rescue; hier ist nur die Oberfläche.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Static

from ..core import rescue
from ..core.pbs_api import ApiError, fetch_fingerprint
from ..core.rescue import RescueConfig, RescueError

CSS = """
Screen { align: center middle; }
#panel { width: 100; max-height: 95%; border: round $accent; padding: 1 2; }
.title { text-style: bold; color: $accent; margin-bottom: 1; }
.hint { color: $text-muted; margin-bottom: 1; }
.warn { color: $error; text-style: bold; }
DataTable { height: 10; margin-bottom: 1; }
Button { margin-right: 1; margin-top: 1; }
#log { height: 22; }
"""


def fmt_size(n: int) -> str:
    return f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"


class Say(Static):
    """Statuszeile ohne Markup-Auswertung."""
    def __init__(self, **kw):
        super().__init__("", markup=False, **kw)


class ConnectScreen(Screen):
    BINDINGS = [("n", "network", "Netzwerk (nmtui)"), ("q", "app.quit", "Beenden")]

    def compose(self) -> ComposeResult:
        c: RescueConfig = self.app.cfg
        with VerticalScroll(id="panel"):
            yield Label("Schritt 1 von 4: Verbindung zum Backup-Server", classes="title", markup=False)
            yield Static("Wiederherstellung eines Vollbackups auf diesem Computer. Mit [n] richtest du zuerst das "
                         "Netzwerk ein (WLAN/LAN).", classes="hint", markup=False)
            yield Input(value=c.server, placeholder="Server, z. B. pbs.example.org", id="server")
            yield Input(value=str(c.port), placeholder="Port", id="port")
            yield Input(value=c.datastore, placeholder="Datastore", id="datastore")
            yield Input(value=c.namespace, placeholder="Namespace (leer = Wurzel)", id="namespace")
            yield Input(value=c.backup_id, placeholder="Backup-ID, z. B. der Rechnername (leer = alle)", id="backup-id")
            yield Input(value=c.auth_id, placeholder="Token-ID, z. B. benutzer@pbs!name", id="auth-id")
            yield Input(placeholder="Token-Secret", password=True, id="token")
            yield Input(value=c.fingerprint, placeholder="Fingerprint (leer lassen und abrufen)", id="fingerprint")
            yield Button("Fingerprint abrufen", id="fp")
            yield Input(value=c.keyfile, placeholder="Schlüsseldatei (verschlüsseltes Backup), z. B. /media/usb/key.json",
                        id="keyfile")
            yield Say(id="status")
            yield Button("Weiter", variant="primary", id="next")
        yield Footer()

    def _v(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _say(self, text: str) -> None:
        self.query_one("#status", Say).update(text)

    def action_network(self) -> None:
        with self.app.suspend():
            subprocess.run(["nmtui"])

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "fp":
            self._say("Hole Fingerprint …")
            try:
                fp = await asyncio.to_thread(fetch_fingerprint, self._v("server"), int(self._v("port") or 8007))
            except (ApiError, ValueError) as ex:
                self._say(f"Fehler: {ex}")
                return
            self.query_one("#fingerprint", Input).value = fp
            self._say("Fingerprint übernommen. Mit dem in PBS angezeigten Wert vergleichen, dann „Weiter“.")
        elif e.button.id == "next":
            await self._next()

    async def _next(self) -> None:
        for wid, name in (("server", "Server"), ("datastore", "Datastore"), ("auth-id", "Token-ID"),
                          ("token", "Token-Secret"), ("fingerprint", "Fingerprint")):
            if not self.query_one(f"#{wid}", Input).value.strip():
                self._say(f"{name} fehlt.")
                return
        c = self.app.cfg
        c.server, c.port = self._v("server"), int(self._v("port") or 8007)
        c.datastore, c.namespace, c.backup_id = self._v("datastore"), self._v("namespace"), self._v("backup-id")
        c.auth_id, c.fingerprint, c.keyfile = self._v("auth-id"), self._v("fingerprint"), self._v("keyfile")
        c.token = self.query_one("#token", Input).value
        if c.keyfile and not Path(c.keyfile).is_file():
            self._say(f"Schlüsseldatei nicht gefunden: {c.keyfile}")
            return
        self._say("Suche Vollbackups …")
        try:
            snaps = await asyncio.to_thread(rescue.list_full_snapshots, c)
        except Exception as ex:
            self._say(f"Fehler: {ex}")
            return
        if not snaps:
            self._say("Kein Vollbackup mit Wiederherstellungsdaten gefunden (Backup-ID und Namespace prüfen).")
            return
        self.app.snaps = snaps
        self.app.push_screen(SnapshotScreen())


class SnapshotScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Zurück")]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label("Schritt 2 von 4: Snapshot wählen", classes="title", markup=False)
            yield DataTable(id="snaps", cursor_type="row")
            yield Say(id="status")
            yield Button("Weiter", variant="primary", id="next")
            yield Button("Zurück", id="back")
        yield Footer()

    def on_mount(self) -> None:
        # Erst nach dem Aufbau der Kind-Widgets füllen (sonst manchmal NoMatches beim Einblenden des Screens)
        self.call_after_refresh(self._populate)

    def _populate(self) -> None:
        t = self.query_one("#snaps", DataTable)
        t.add_columns("Zeitpunkt", "Backup-ID", "Größe")
        for s in self.app.snaps:
            t.add_row(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.backup_time)), s.backup_id, fmt_size(s.size))
        t.focus()

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        snap = self.app.snaps[self.query_one("#snaps", DataTable).cursor_row]
        self.query_one("#status", Say).update("Lade Layout-Daten aus dem Backup …")
        try:
            dest = Path(tempfile.mkdtemp(prefix="pbsgui-recovery-"))
            self.app.rec = await asyncio.to_thread(rescue.fetch_recovery, self.app.cfg, snap, dest)
        except Exception as ex:
            msg = str(ex)
            if "missing key" in msg:
                msg += " — Schlüsseldatei im ersten Schritt angeben."
            self.query_one("#status", Say).update(f"Fehler: {msg}")
            return
        self.app.snap = snap
        self.app.push_screen(DiskScreen())


class DiskScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Zurück")]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label("Schritt 3 von 4: Ziel-Platte wählen", classes="title", markup=False)
            yield Static("Die gewählte Platte wird KOMPLETT überschrieben. Eingehängte Platten und das "
                         "Rettungsmedium werden nicht angeboten.", classes="warn", markup=False)
            yield DataTable(id="disks", cursor_type="row")
            yield Input(placeholder="LUKS-Passphrase des Originalsystems", password=True, id="pass")
            yield Say(id="status")
            yield Button("Weiter", variant="primary", id="next")
            yield Button("Zurück", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.disks = []
        self.call_after_refresh(self._populate)

    def _populate(self) -> None:
        self.disks = rescue.candidate_disks()
        t = self.query_one("#disks", DataTable)
        t.add_columns("Gerät", "Größe", "Modell", "Anschluss")
        for d in self.disks:
            t.add_row(d["path"], fmt_size(d["size"]), d["model"] or "-",
                      (d["tran"] or "-") + (" (Wechseldatenträger)" if d["removable"] else ""))
        need = rescue.needs_passphrase(self.app.rec)
        self.query_one("#pass").display = need
        if not self.disks:
            self.query_one("#status", Say).update("Keine zulässige Ziel-Platte gefunden.")
        t.focus()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        if not self.disks:
            return
        d = self.disks[self.query_one("#disks", DataTable).cursor_row]
        c = self.app.cfg
        c.target_disk = d["path"]
        c.passphrase = self.query_one("#pass", Input).value
        try:
            self.app.plan = rescue.build_layout_plan(self.app.rec, d["path"], c.passphrase or None,
                                                     target_size=d["size"])
        except RescueError as ex:
            self.query_one("#status", Say).update(f"Fehler: {ex}")
            return
        self.app.push_screen(PlanScreen())


class PlanScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Zurück")]

    def compose(self) -> ComposeResult:
        p, c = self.app.plan, self.app.cfg
        with VerticalScroll(id="panel"):
            yield Label("Schritt 4 von 4: Bestätigen", classes="title", markup=False)
            yield Static(f"Snapshot:  {self.app.snap.path}\n" + "\n".join(p.summary) +
                         ("\n\nWarnungen:\n" + "\n".join("  " + w for w in p.warnings) if p.warnings else ""),
                         markup=False)
            yield Static(f"\nALLE DATEN AUF {c.target_disk} GEHEN VERLOREN.\nZur Bestätigung den Gerätenamen eintippen:",
                         classes="warn", markup=False)
            yield Input(placeholder=c.target_disk, id="confirm")
            yield Say(id="status")
            yield Button("Wiederherstellen", variant="error", id="go")
            yield Button("Trockenlauf (nur anzeigen)", id="dry")
            yield Button("Zurück", id="back")
        yield Footer()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        dry = e.button.id == "dry"
        if not dry and self.query_one("#confirm", Input).value.strip() != self.app.cfg.target_disk:
            self.query_one("#status", Say).update("Bestätigung stimmt nicht überein. Es wurde nichts verändert.")
            return
        self.app.dry_run = dry
        self.app.push_screen(RunScreen())


class RunScreen(Screen):
    BINDINGS = [("q", "app.quit", "Beenden")]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label("Wiederherstellung läuft …" if not self.app.dry_run else "Trockenlauf", classes="title",
                        markup=False, id="run-title")
            yield RichLog(id="log", wrap=True, markup=False)
            yield Say(id="status")
            yield Button("Neu starten", variant="primary", id="reboot", disabled=True)
            yield Button("Beenden", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.run_worker(self._work, thread=True))

    def _work(self) -> None:
        log = lambda line: self.app.call_from_thread(self.query_one("#log", RichLog).write, line)
        ok = False
        try:
            ok = rescue.execute(self.app.cfg, self.app.snap, self.app.rec, self.app.plan, log,
                                dry_run=self.app.dry_run)
        except Exception as ex:
            log(f"FEHLER: {ex}")
        self.app.call_from_thread(self._finished, ok)

    def _finished(self, ok: bool) -> None:
        self.query_one("#run-title", Label).update("Fertig" if ok else "Fehlgeschlagen")
        self.query_one("#status", Say).update(
            ("Trockenlauf beendet, nichts wurde verändert." if self.app.dry_run else
             "Wiederherstellung abgeschlossen. Rettungsmedium entfernen und neu starten.") if ok else
            "Die Wiederherstellung ist fehlgeschlagen. Meldungen oben prüfen; die Ziel-Platte ist evtl. unvollständig.")
        self.query_one("#reboot", Button).disabled = not ok or self.app.dry_run

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "reboot":
            subprocess.run(["systemctl", "reboot"])
        else:
            self.app.exit()


class RescueApp(App):
    TITLE = "pbsgui Wiederherstellung"
    CSS = CSS

    def __init__(self, cfg: RescueConfig | None = None):
        super().__init__()
        self.cfg = cfg or RescueConfig(server="", datastore="", auth_id="")
        self.snaps, self.snap, self.rec, self.plan, self.dry_run = [], None, None, None, False

    def on_mount(self) -> None:
        self.push_screen(ConnectScreen())


def run() -> None:
    from ..cli.rescue_cli import DEFAULT_CONFIG
    cfg = RescueConfig(server="", datastore="", auth_id="")
    if Path(DEFAULT_CONFIG).exists():  # personalisierte ISO: Verbindungsdaten (ohne Geheimnisse) vorausfüllen
        try:
            cfg = RescueConfig.from_json(DEFAULT_CONFIG)
        except Exception:
            pass
    if os.geteuid() != 0:
        print("Hinweis: Ohne root ist nur der Trockenlauf möglich.")
    RescueApp(cfg).run()
