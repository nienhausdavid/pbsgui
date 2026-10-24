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
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, RadioButton, RadioSet, RichLog, Select, Static

from ..i18n import tr
from ..core import rescue, update as updater, vmimage, vpn
from ..web import server as websrv
from ..core.pbs_api import ApiError, fetch_fingerprint
from ..core.rescue import RescueConfig, RescueError

CSS = """
Screen { align: center middle; }
#panel { width: 100; max-height: 95%; border: round $accent; padding: 1 2; }
.title { text-style: bold; color: $accent; margin-bottom: 1; }
.hint { color: $text-muted; margin-bottom: 1; }
.warn { color: $error; text-style: bold; }
DataTable { height: 10; margin-bottom: 1; }
/* Linux-Konsole: nur 16 ANSI-Farben und eine einfache Schrift. Eingabefelder und Knöpfe daher einzeilig mit
   Hintergrundfarbe statt Rahmen (spart Platz, damit "Weiter" ohne Scrollen sichtbar bleibt). */
Input { border: none; border-bottom: solid ansi_white; height: 2; padding: 0 1; margin-bottom: 0; background: ansi_default; color: ansi_bright_white; }
Input:focus { border-bottom: solid ansi_bright_cyan; background: ansi_default; }
Input > .input--placeholder { color: ansi_white; text-style: italic; }
Select { height: auto; margin-bottom: 1; }
SelectCurrent { border: none; border-bottom: solid ansi_white; height: 2; background: ansi_default; color: ansi_bright_white; padding: 0 1; }
SelectOverlay { background: ansi_black; color: ansi_bright_white; border: round ansi_bright_black; }
Button { border: none; height: 1; min-width: 14; padding: 0 2; background: ansi_blue; color: ansi_bright_white; margin-right: 2; margin-bottom: 1; margin-top: 1; }
Button:focus { background: ansi_cyan; color: ansi_black; text-style: bold; }
Button.-primary { background: ansi_green; color: ansi_bright_white; }
Button.-error { background: ansi_red; color: ansi_bright_white; }
DataTable { border: round ansi_bright_black; }
DataTable:focus { border: round ansi_bright_cyan; }
Checkbox { border: none; height: 1; padding: 0 1; background: ansi_default; margin-bottom: 1; }
#log { height: 22; }
"""


def fmt_size(n: int) -> str:
    return f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"


class Say(Static):
    """Statuszeile ohne Markup-Auswertung."""
    def __init__(self, **kw):
        super().__init__("", markup=False, **kw)


class ConnectScreen(Screen):
    # Funktionstasten statt Buchstaben: in einem Eingabefeld würde "n" oder "v" sonst einfach getippt
    BINDINGS = [Binding("f2", "network", tr("Netzwerk (nmtui)"), priority=True), Binding("f3", "vpn", "VPN", priority=True),
                Binding("f4", "update", tr("Updates"), priority=True), Binding("f5", "web", tr("Web-Oberfläche"), priority=True),
                Binding("n", "network", show=False),
                ("q", "app.quit", tr("Beenden"))]

    def compose(self) -> ComposeResult:
        c: RescueConfig = self.app.cfg
        with VerticalScroll(id="panel"):
            yield Label(tr("Schritt 1 von 4: Verbindung zum Backup-Server"), classes="title", markup=False)
            yield Static(tr("Wiederherstellung eines Vollbackups auf diesem Computer. Zuerst bei Bedarf Netzwerk (F2, WLAN/LAN), VPN (F3: WireGuard, OpenVPN, NetBird) und Updates (F4) einrichten; die Knöpfe gehen auch."),
                         classes="hint", markup=False)
            with Horizontal(id="tools"):
                yield Button(tr("Netzwerk (F2)"), id="t-net")
                yield Button(tr("VPN (F3)"), id="t-vpn")
                yield Button(tr("Updates (F4)"), id="t-upd")
                yield Button(tr("Web (F5)"), id="t-web")
            yield Input(value=c.server, placeholder=tr("Server, z. B. pbs.example.org"), id="server")
            yield Input(value=str(c.port), placeholder="Port", id="port")
            yield Input(value=c.datastore, placeholder="Datastore", id="datastore")
            yield Input(value=c.namespace, placeholder=tr("Namespace (leer = Wurzel)"), id="namespace")
            yield Input(value=c.backup_id, placeholder=tr("Backup-ID, z. B. der Rechnername (leer = alle)"), id="backup-id")
            yield Input(value=c.auth_id, placeholder=tr("Token-ID, z. B. benutzer@pbs!name"), id="auth-id")
            yield Input(placeholder="Token-Secret", password=True, id="token")
            yield Input(value=c.fingerprint, placeholder=tr("Fingerprint (leer lassen und abrufen)"), id="fingerprint")
            yield Button(tr("Fingerprint abrufen"), id="fp")
            yield Input(value=c.keyfile, placeholder=tr("Schlüsseldatei (verschlüsseltes Backup), z. B. /media/usb/key.json"),
                        id="keyfile")
            yield Button(tr("USB-Stick nach Schlüsseldatei durchsuchen"), id="usb")
            yield Checkbox(tr("VM-Abbild wiederherstellen (Proxmox-VE-Backup einer virtuellen Maschine auf eine Platte)"), id="vm-mode")
            yield Say(id="status")
            yield Button(tr("Weiter"), variant="primary", id="next")
        yield Footer()

    def on_mount(self) -> None:
        # Fokus auf das erste noch leere Pflichtfeld (bei vorausgefüllter rescue.json: das Token-Secret)
        self.call_after_refresh(self._focus_first_empty)

    def _focus_first_empty(self) -> None:
        for wid in ("server", "datastore", "auth-id", "token", "fingerprint"):
            if not self.query_one(f"#{wid}", Input).value.strip():
                self.query_one(f"#{wid}", Input).focus()
                return
        self.query_one("#next", Button).focus()

    def _v(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _say(self, text: str) -> None:
        self.query_one("#status", Say).update(text)

    def action_network(self) -> None:
        with self.app.suspend():
            subprocess.run(["nmtui"])

    def action_vpn(self) -> None:
        self.app.push_screen(VpnScreen())

    def action_update(self) -> None:
        self.app.push_screen(UpdateScreen())

    def action_web(self) -> None:
        self.app.push_screen(WebScreen())

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "t-net":
            self.action_network()
        elif e.button.id == "t-vpn":
            self.action_vpn()
        elif e.button.id == "t-upd":
            self.action_update()
        elif e.button.id == "t-web":
            self.action_web()
        elif e.button.id == "fp":
            self._say(tr("Hole Fingerprint …"))
            try:
                fp = await asyncio.to_thread(fetch_fingerprint, self._v("server"), int(self._v("port") or 8007))
            except (ApiError, ValueError) as ex:
                self._say(tr("Fehler: {0}", ex))
                return
            self.query_one("#fingerprint", Input).value = fp
            self._say(tr("Fingerprint übernommen. Mit dem in PBS angezeigten Wert vergleichen, dann „Weiter“."))
        elif e.button.id == "usb":
            self._say(tr("Suche auf USB-Sticks und anderen Datenträgern …"))
            keys = await asyncio.to_thread(rescue.find_key_files, None, True)
            if keys:
                self.query_one("#keyfile", Input).value = keys[0]
                self._say(tr("{0} Schlüsseldatei(en) gefunden, die erste wurde eingetragen: {1}", len(keys), keys[0]))
            else:
                self._say(tr("Keine Schlüsseldatei gefunden. USB-Stick einstecken und erneut suchen oder den Pfad von Hand eintragen."))
        elif e.button.id == "next":
            await self._next()

    async def _next(self) -> None:
        for wid, name in (("server", "Server"), ("datastore", "Datastore"), ("auth-id", "Token-ID"),
                          ("token", "Token-Secret"), ("fingerprint", "Fingerprint")):
            if not self.query_one(f"#{wid}", Input).value.strip():
                self._say(tr("{0} fehlt.", name))
                return
        c = self.app.cfg
        c.server, c.port = self._v("server"), int(self._v("port") or 8007)
        c.datastore, c.namespace, c.backup_id = self._v("datastore"), self._v("namespace"), self._v("backup-id")
        c.auth_id, c.fingerprint, c.keyfile = self._v("auth-id"), self._v("fingerprint"), self._v("keyfile")
        c.token = self.query_one("#token", Input).value
        if c.keyfile and not Path(c.keyfile).is_file():
            self._say(tr("Schlüsseldatei nicht gefunden: {0}", c.keyfile))
            return
        if self.query_one("#vm-mode", Checkbox).value:
            self._say(tr("Suche VM-Backups …"))
            try:
                vsnaps = await asyncio.to_thread(vmimage.list_vm_snapshots, c)
            except Exception as ex:
                self._say(tr("Fehler: {0}", ex))
                return
            if not vsnaps:
                self._say(tr("Kein VM-Backup mit Abbild gefunden (VM-Nummer als Backup-ID und Namespace prüfen)."))
                return
            self.app.vm_snaps, self.app.vm_plan = vsnaps, None
            self.app.push_screen(VmSnapshotScreen())
            return
        self.app.vm_plan = None
        self._say(tr("Suche Vollbackups …"))
        try:
            snaps = await asyncio.to_thread(rescue.list_full_snapshots, c)
        except Exception as ex:
            self._say(tr("Fehler: {0}", ex))
            return
        if not snaps:
            self._say(tr("Kein Vollbackup mit Wiederherstellungsdaten gefunden (Backup-ID und Namespace prüfen)."))
            return
        self.app.snaps = snaps
        self.app.push_screen(SnapshotScreen())


class VpnScreen(Screen):
    """VPN-Tunnel zum Backup-Server aufbauen, bevor die Verbindung geprüft wird."""
    BINDINGS = [("escape", "back", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        have = vpn.tools_available()
        with VerticalScroll(id="panel"):
            yield Label(tr("VPN zum Backup-Server"), classes="title", markup=False)
            yield Static(tr("Konfiguration vom USB-Stick (WireGuard .conf, OpenVPN .ovpn) oder NetBird mit Setup-Key. Danach zurück und die Verbindung zum Server prüfen."), classes="hint", markup=False)
            with RadioSet(id="kind"):
                for k in vpn.KINDS:
                    yield RadioButton(vpn.LABELS[k] + ("" if have.get(k) else tr(" (nicht installiert)")), value=(k == "wireguard"))
            yield Input(placeholder=tr("Konfigurationsdatei, z. B. /media/usb/wg0.conf"), id="file")
            yield Button(tr("Auf USB-Stick suchen"), id="find")
            yield Input(placeholder=tr("OpenVPN: Benutzername (leer = keiner)"), id="user")
            yield Input(placeholder=tr("OpenVPN: Passwort"), password=True, id="pw")
            yield Input(placeholder=tr("NetBird: Management-URL (leer = netbird.io)"), id="url")
            yield Input(placeholder=tr("NetBird: Setup-Key"), password=True, id="setupkey")
            yield Button(tr("Verbinden"), variant="primary", id="up")
            yield Button(tr("Trennen"), id="down")
            yield Button("Status", id="state")
            yield Say(id="status")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def _kind(self) -> str:
        idx = self.query_one("#kind", RadioSet).pressed_index
        return vpn.KINDS[idx if idx >= 0 else 0]

    def _v(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _say(self, text: str) -> None:
        self.query_one("#status", Say).update(text)

    def action_back(self) -> None:
        self.app.pop_screen()

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id
        if bid == "back":
            self.app.pop_screen()
        elif bid == "find":
            kind = self._kind()
            if kind == "netbird":
                self._say(tr("NetBird braucht keine Datei, nur Setup-Key (und bei eigener Instanz die Management-URL)."))
                return
            self._say(tr("Suche auf USB-Sticks …"))
            found = await asyncio.to_thread(vpn.find_configs, kind, None, True)
            if found:
                self.query_one("#file", Input).value = found[0]
                self._say(tr("{0} Konfiguration(en) gefunden, die erste wurde eingetragen: {1}", len(found), found[0]))
            else:
                self._say(tr("Keine passende Konfiguration gefunden. USB-Stick einstecken und erneut suchen oder Pfad eintragen."))
        elif bid == "state":
            st = await asyncio.to_thread(vpn.status)
            self._say("   ".join(f"{vpn.LABELS[k]}: {v}" for k, v in st.items()))
        elif bid == "down":
            self._say(tr("Trenne …"))
            kind = self._kind()
            fn = {"wireguard": vpn.wireguard_down, "openvpn": vpn.openvpn_down, "netbird": vpn.netbird_down}[kind]
            r = await asyncio.to_thread(fn)
            self._say((r.text or "getrennt")[-300:])
        elif bid == "up":
            await self._up()

    async def _up(self) -> None:
        kind = self._kind()
        self._say(tr("Verbinde {0} …", vpn.LABELS[kind]))
        try:
            if kind == "wireguard":
                r = await asyncio.to_thread(vpn.wireguard_up, self._v("file"))
            elif kind == "openvpn":
                r = await asyncio.to_thread(vpn.openvpn_up, self._v("file"), self._v("user"), self.query_one("#pw", Input).value)
            else:
                r = await asyncio.to_thread(vpn.netbird_up, self.query_one("#setupkey", Input).value.strip(), self._v("url"))
        except vpn.VpnError as ex:
            self._say(tr("Fehler: {0}", ex))
            return
        self.query_one("#pw", Input).value = ""
        self.query_one("#setupkey", Input).value = ""      # Geheimnisse nicht länger als nötig in der Oberfläche halten
        self._say((("Verbunden. " if r.ok else "Fehler: ") + (r.text or ""))[-400:])


class WebScreen(Screen):
    """Web-Oberfläche aktivieren: zeigt Adresse, zufälliges Passwort und Fingerabdruck; die Anmeldeereignisse laufen mit."""
    BINDINGS = [("escape", "back", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Label(tr("Web-Oberfläche für die Wiederherstellung"), classes="title", markup=False)
            yield Static(tr("Für Server ohne bequemen Konsolenzugang (VPS): Die Wiederherstellung lässt sich dann im Browser einrichten. Die Seite ist nur mit dem Passwort unten erreichbar, nutzt HTTPS und sperrt Angreifer."),
                         classes="hint", markup=False)
            yield Say(id="webinfo")
            yield Button(tr("Aktivieren"), variant="primary", id="web-on")
            yield Button(tr("Beenden"), id="web-off")
            yield Say(id="status")
            yield Label(tr("Ereignisse (Anmeldungen, Sperren, Köder):"), markup=False)
            yield Say(id="events")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        info = websrv.read_info()
        self.query_one("#web-on", Button).disabled = bool(info)
        self.query_one("#web-off", Button).disabled = not info
        if info:
            self.query_one("#webinfo", Say).update(websrv.describe(info))
            self.query_one("#events", Say).update("\n".join(websrv.events_tail()) or tr("(noch nichts)"))
        else:
            self.query_one("#webinfo", Say).update(tr("Aus."))
            self.query_one("#events", Say).update("")

    def action_back(self) -> None:
        self.app.pop_screen()

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
        elif e.button.id == "web-on":
            self.query_one("#status", Say).update(tr("Starte …"))
            info = await asyncio.to_thread(websrv.start_daemon)
            self.query_one("#status", Say).update("" if info else tr("Start fehlgeschlagen (siehe /run/pbsgui-web/web.log)."))
            self._refresh()
        elif e.button.id == "web-off":
            await asyncio.to_thread(websrv.stop_daemon)
            await asyncio.sleep(1.0)
            self._refresh()


class UpdateScreen(Screen):
    """Programme aktualisieren (pbstui, Proxmox-Client, VPN-Werkzeuge)."""
    BINDINGS = [("escape", "back", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Label(tr("Programme aktualisieren"), classes="title", markup=False)
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
            yield Say(id="status")
            yield Button(tr("Neu starten"), variant="primary", id="restart", disabled=True)
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._work, thread=True)

    def _log(self, line: str) -> None:
        self.app.call_from_thread(self.query_one("#log", RichLog).write, line)

    def _work(self) -> None:
        res = updater.update(on_line=self._log, wait=15)
        self._log(res.text)
        self.app.call_from_thread(self._done, res)

    def _done(self, res) -> None:
        self.query_one("#status", Say).update(
            tr("Der Assistent wurde aktualisiert: bitte neu starten.") if res.restart_needed else
            (tr("Kein Netzwerk: [n] Netzwerk oder [v] VPN einrichten, dann erneut versuchen.") if res.state == "offline" else
             (tr("Fehler beim Aktualisieren.") if res.state == "fehler" else tr("Fertig."))))
        self.query_one("#restart", Button).disabled = not res.restart_needed

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "restart":
            self.app.restart = True
            self.app.exit()
        else:
            self.app.pop_screen()


class SnapshotScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label(tr("Schritt 2 von 4: Snapshot wählen"), classes="title", markup=False)
            yield DataTable(id="snaps", cursor_type="row")
            yield Say(id="status")
            yield Button(tr("Weiter"), variant="primary", id="next")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        # Erst nach dem Aufbau der Kind-Widgets füllen (sonst manchmal NoMatches beim Einblenden des Screens)
        self.call_after_refresh(self._populate)

    def _populate(self) -> None:
        t = self.query_one("#snaps", DataTable)
        t.add_columns(tr("Zeitpunkt"), "Backup-ID", tr("Größe"))
        for s in self.app.snaps:
            t.add_row(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.backup_time)), s.backup_id, fmt_size(s.size))
        t.focus()

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        snap = self.app.snaps[self.query_one("#snaps", DataTable).cursor_row]
        self.query_one("#status", Say).update(tr("Lade Layout-Daten aus dem Backup …"))
        try:
            dest = Path(tempfile.mkdtemp(prefix="pbsgui-recovery-"))
            self.app.rec = await asyncio.to_thread(rescue.fetch_recovery, self.app.cfg, snap, dest)
        except Exception as ex:
            msg = str(ex)
            if "missing key" in msg:
                msg += tr(" — Schlüsseldatei im ersten Schritt angeben.")
            self.query_one("#status", Say).update(tr("Fehler: {0}", msg))
            return
        self.app.snap = snap
        self.app.push_screen(DiskScreen())


class VmSnapshotScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label(tr("Schritt 2 von 4: VM-Backup wählen"), classes="title", markup=False)
            yield DataTable(id="snaps", cursor_type="row")
            yield Say(id="status")
            yield Button(tr("Weiter"), variant="primary", id="next")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._populate)

    def _populate(self) -> None:
        t = self.query_one("#snaps", DataTable)
        t.add_columns(tr("Zeitpunkt"), "VM", tr("Platten"))
        for s in self.app.vm_snaps:
            disks = ", ".join(f"{d.key} {d.image_size / 2**30:.0f} GiB" for d in vmimage.vm_disks(s))
            t.add_row(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.backup_time)), s.backup_id, disks)
        t.focus()

    async def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        snap = self.app.vm_snaps[self.query_one("#snaps", DataTable).cursor_row]
        self.query_one("#status", Say).update(tr("Lade die VM-Konfiguration …"))
        try:
            self.app.vmcfg = await asyncio.to_thread(vmimage.fetch_config, self.app.cfg, snap)
        except Exception as ex:
            msg = str(ex)
            if "missing key" in msg:
                msg += tr(" — Schlüsseldatei im ersten Schritt angeben.")
            self.query_one("#status", Say).update(tr("Fehler: {0}", msg))
            return
        self.app.vm_snap = snap
        self.app.cfg.snapshot = snap.path
        self.app.push_screen(VmDiskScreen())


class VmDiskScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        self.targets = rescue.candidate_disks()
        disks = vmimage.vm_disks(self.app.vm_snap)
        boot = vmimage.boot_disk_key(self.app.vmcfg, disks)
        opts = [(f"{t['path']}  {t['size'] / 2**30:.0f} GiB  {t['model'] or '-'}", t["path"]) for t in self.targets]
        with VerticalScroll(id="panel"):
            yield Label(tr("Schritt 3 von 4: Ziel-Platten und Reparaturen"), classes="title", markup=False)
            yield Static(tr("Jede Platte der VM bekommt eine Ziel-Platte; ihr Inhalt wird KOMPLETT überschrieben. Platten ohne Ziel werden nicht wiederhergestellt."), classes="warn", markup=False)
            for d in disks:
                yield Label(f"{d.key}  ({d.image_size / 2**30:.1f} GiB)" + ("  Boot-Platte" if d.key == boot else ""), markup=False)
                yield Select(opts, allow_blank=True, prompt=tr("(nicht wiederherstellen)"), id=f"map-{d.key}")
            yield Label(tr("Danach automatisch reparieren"), classes="title", markup=False)
            yield Checkbox(tr("Partition und Dateisystem auf die Plattengröße vergrößern"), value=True, id="r-grow")
            yield Checkbox(tr("Initramfs mit allen Treibern neu erzeugen"), value=True, id="r-initramfs")
            yield Checkbox(tr("Bootloader neu installieren (UEFI mit Fallback oder BIOS)"), value=True, id="r-bootloader")
            yield Checkbox(tr("Netzwerk-Schnittstellennamen anpassen"), value=True, id="r-network")
            yield Checkbox(tr("qemu-guest-agent und cloud-init abschalten"), value=True, id="r-services")
            yield Input(placeholder=tr("LUKS-Passphrase (nur wenn das Abbild verschlüsselte Volumes hat)"), password=True, id="luks")
            yield Say(id="status")
            yield Button(tr("Weiter"), variant="primary", id="next")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        def preselect() -> None:       # Boot-Platte auf die erste freie Ziel-Platte vorbelegen, wenn es genau eine gibt
            disks = vmimage.vm_disks(self.app.vm_snap)
            boot = vmimage.boot_disk_key(self.app.vmcfg, disks)
            if len(self.targets) == 1 and boot:
                self.query_one(f"#map-{boot}", Select).value = self.targets[0]["path"]
        self.call_after_refresh(preselect)

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        say = self.query_one("#status", Say)
        mapping = {}
        for d in vmimage.vm_disks(self.app.vm_snap):
            v = self.query_one(f"#map-{d.key}", Select).value
            if isinstance(v, str):
                mapping[d.key] = v
        opts = vmimage.RepairOptions(**{k: self.query_one(f"#r-{k}", Checkbox).value
                                        for k in ("grow", "initramfs", "bootloader", "network", "services")})
        self.app.cfg.passphrase = self.query_one("#luks", Input).value
        try:
            self.app.vm_plan = vmimage.build_plan(self.app.cfg, self.app.vm_snap, self.app.vmcfg, mapping, self.targets, opts)
        except RescueError as ex:
            say.update(tr("Fehler: {0}", ex))
            return
        self.app.vm_targets = sorted(set(mapping.values()))
        self.app.push_screen(VmPlanScreen())


class VmPlanScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        p, vc = self.app.vm_plan, self.app.vmcfg
        devs = " ".join(self.app.vm_targets)
        with VerticalScroll(id="panel"):
            yield Label(tr("Schritt 4 von 4: Bestätigen"), classes="title", markup=False)
            yield Static(tr("VM-Backup:  {0}   ({1}, {2}, {3})\n", self.app.vm_snap.path, vc.name or 'ohne Namen', vc.ostype or '?', 'UEFI' if vc.uefi else 'BIOS') + "\n".join(p.summary) +
                         ("\n\nWarnungen:\n" + "\n".join("  " + w for w in p.warnings) if p.warnings else ""), markup=False)
            yield Static(tr("\nALLE DATEN AUF {0} GEHEN VERLOREN.\nZur Bestätigung alle Gerätenamen eintippen (mit Leerzeichen):", devs),
                         classes="warn", markup=False)
            yield Input(placeholder=devs, id="confirm")
            yield Say(id="status")
            yield Button(tr("Wiederherstellen"), variant="error", id="go")
            yield Button(tr("Trockenlauf (nur anzeigen)"), id="dry")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.query_one("#confirm", Input).focus())

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        dry = e.button.id == "dry"
        typed = sorted(set(self.query_one("#confirm", Input).value.split()))
        if not dry and typed != self.app.vm_targets:
            self.query_one("#status", Say).update(tr("Bestätigung stimmt nicht überein. Es wurde nichts verändert."))
            return
        self.app.dry_run = dry
        self.app.push_screen(RunScreen())


class DiskScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label(tr("Schritt 3 von 4: Ziel-Platte wählen"), classes="title", markup=False)
            yield Static(tr("Die gewählte Platte wird KOMPLETT überschrieben. Eingehängte Platten und das Rettungsmedium werden nicht angeboten."), classes="warn", markup=False)
            yield DataTable(id="disks", cursor_type="row")
            yield Checkbox(tr("Größe anpassen (für eine kleinere Platte)"), id="shrink")
            yield Static(tr("Partitionen und Volumes werden verkleinert."), classes="hint", markup=False)
            yield Label(tr("Verschlüsselung der neuen Platte"), classes="title", markup=False)
            orig = tr("verschlüsselt (LUKS)") if rescue.original_encrypted(self.app.rec) else tr("unverschlüsselt")
            with RadioSet(id="enc"):
                yield RadioButton(tr("Wie im Original ({0})", orig), value=True)
                yield RadioButton(tr("Neu mit LUKS verschlüsseln (neue Passphrase)"))
                yield RadioButton(tr("Ohne Verschlüsselung"))
            yield Input(placeholder=tr("LUKS-Passphrase des Originalsystems"), password=True, id="pass")
            yield Input(placeholder=tr("Neue Passphrase (mindestens 8 Zeichen)"), password=True, id="newpass")
            yield Input(placeholder=tr("Neue Passphrase wiederholen"), password=True, id="newpass2")
            yield Say(id="status")
            yield Button(tr("Weiter"), variant="primary", id="next")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.disks = []
        self.call_after_refresh(self._populate)

    def _populate(self) -> None:
        self.disks = rescue.candidate_disks()
        t = self.query_one("#disks", DataTable)
        t.add_columns(tr("Gerät"), tr("Größe"), tr("Modell"), tr("Anschluss"))
        for d in self.disks:
            t.add_row(d["path"], fmt_size(d["size"]), d["model"] or "-",
                      (d["tran"] or "-") + (tr(" (Wechseldatenträger)") if d["removable"] else ""))
        self._sync_enc()
        self._sync_shrink()
        if not self.disks:
            self.query_one("#status", Say).update(tr("Keine zulässige Ziel-Platte gefunden."))
        t.focus()

    def _mode(self) -> str:
        return ("keep", "new", "none")[self.query_one("#enc", RadioSet).pressed_index or 0]

    def _selected(self) -> dict | None:
        if not self.disks:
            return None
        return self.disks[min(self.query_one("#disks", DataTable).cursor_row, len(self.disks) - 1)]

    def _sync_shrink(self) -> None:
        """Die Option „Größe anpassen“ wird nur angeboten, wenn die gewählte Platte kleiner als das Original ist."""
        d = self._selected()
        smaller = bool(d) and d["size"] < self.app.rec.root_disk().size
        box = self.query_one("#shrink", Checkbox)
        box.display = smaller
        if smaller:
            self.query_one("#status", Say).update(
                tr("Die Platte ({0}) ist kleiner als das Original ({1}): „Größe anpassen“ ankreuzen, um fortzufahren.", fmt_size(d['size']), fmt_size(self.app.rec.root_disk().size)))
        elif not d or d["size"] >= self.app.rec.root_disk().size:
            box.value = False

    def on_data_table_row_highlighted(self, _e) -> None:
        if self.disks:
            self._sync_shrink()

    def _sync_enc(self) -> None:
        mode = self._mode()
        self.query_one("#pass").display = rescue.needs_passphrase(self.app.rec, mode)
        self.query_one("#newpass").display = mode == "new"
        self.query_one("#newpass2").display = mode == "new"

    def on_radio_set_changed(self, _e) -> None:
        self._sync_enc()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        if not self.disks:
            return
        d = self.disks[self.query_one("#disks", DataTable).cursor_row]
        c = self.app.cfg
        c.target_disk = d["path"]
        c.encryption = self._mode()
        c.passphrase = self.query_one("#pass", Input).value
        c.new_passphrase = self.query_one("#newpass", Input).value
        if c.encryption == "new":
            if c.new_passphrase != self.query_one("#newpass2", Input).value:
                self.query_one("#status", Say).update(tr("Die neuen Passphrasen stimmen nicht überein."))
                return
            if len(c.new_passphrase) < 8:
                self.query_one("#status", Say).update(tr("Die neue Passphrase ist zu kurz (mindestens 8 Zeichen)."))
                return
        try:
            self.app.plan = rescue.build_layout_plan(self.app.rec, d["path"], c.passphrase or None,
                                                     target_size=d["size"], encryption=c.encryption,
                                                     new_passphrase=c.new_passphrase or None,
                                                     shrink=self.query_one("#shrink", Checkbox).value,
                                                     snapshot_size=self.app.snap.size or None)
        except RescueError as ex:
            self.query_one("#status", Say).update(tr("Fehler: {0}", ex))
            return
        self.app.push_screen(PlanScreen())


class PlanScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", tr("Zurück"))]

    def compose(self) -> ComposeResult:
        p, c = self.app.plan, self.app.cfg
        with VerticalScroll(id="panel"):
            yield Label(tr("Schritt 4 von 4: Bestätigen"), classes="title", markup=False)
            yield Static(tr("Snapshot:  {0}\n", self.app.snap.path) + "\n".join(p.summary) +
                         ("\n\nWarnungen:\n" + "\n".join("  " + w for w in p.warnings) if p.warnings else ""),
                         markup=False)
            yield Static(tr("\nALLE DATEN AUF {0} GEHEN VERLOREN.\nZur Bestätigung den Gerätenamen eintippen:", c.target_disk),
                         classes="warn", markup=False)
            yield Input(placeholder=c.target_disk, id="confirm")
            yield Say(id="status")
            yield Button(tr("Wiederherstellen"), variant="error", id="go")
            yield Button(tr("Trockenlauf (nur anzeigen)"), id="dry")
            yield Button(tr("Zurück"), id="back")
        yield Footer()

    def on_mount(self) -> None:
        # Tastatureingaben sollen direkt im Bestätigungsfeld landen (nicht im scrollbaren Container)
        self.call_after_refresh(lambda: self.query_one("#confirm", Input).focus())

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "back":
            self.app.pop_screen()
            return
        dry = e.button.id == "dry"
        if not dry and self.query_one("#confirm", Input).value.strip() != self.app.cfg.target_disk:
            self.query_one("#status", Say).update(tr("Bestätigung stimmt nicht überein. Es wurde nichts verändert."))
            return
        self.app.dry_run = dry
        self.app.push_screen(RunScreen())


class RunScreen(Screen):
    BINDINGS = [("q", "app.quit", tr("Beenden"))]

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Label(tr("Wiederherstellung läuft …") if not self.app.dry_run else tr("Trockenlauf"), classes="title",
                        markup=False, id="run-title")
            yield RichLog(id="log", wrap=True, markup=False)
            yield Say(id="status")
            yield Button(tr("Neu starten"), variant="primary", id="reboot", disabled=True)
            yield Button(tr("Beenden"), id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.run_worker(self._work, thread=True))

    def _work(self) -> None:
        log = lambda line: self.app.call_from_thread(self.query_one("#log", RichLog).write, line)
        ok = False
        try:
            if self.app.vm_plan is not None:
                ok = vmimage.execute(self.app.cfg, self.app.vm_plan, log, dry_run=self.app.dry_run)
            else:
                ok = rescue.execute(self.app.cfg, self.app.snap, self.app.rec, self.app.plan, log, dry_run=self.app.dry_run)
        except Exception as ex:
            log(tr("FEHLER: {0}", ex))
        self.app.call_from_thread(self._finished, ok)

    def _finished(self, ok: bool) -> None:
        self.query_one("#run-title", Label).update(tr("Fertig") if ok else tr("Fehlgeschlagen"))
        self.query_one("#status", Say).update(
            (tr("Trockenlauf beendet, nichts wurde verändert.") if self.app.dry_run else
             tr("Wiederherstellung abgeschlossen. Rettungsmedium entfernen und neu starten.")) if ok else
            tr("Die Wiederherstellung ist fehlgeschlagen. Meldungen oben prüfen; die Ziel-Platte ist evtl. unvollständig."))
        self.query_one("#reboot", Button).disabled = not ok or self.app.dry_run

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "reboot":
            subprocess.run(["systemctl", "reboot"])
        else:
            self.app.exit()


class RescueApp(App):
    TITLE = tr("pbsgui Wiederherstellung")
    CSS = CSS

    def __init__(self, cfg: RescueConfig | None = None):
        super().__init__()
        self.cfg = cfg or RescueConfig(server="", datastore="", auth_id="")
        self.snaps, self.snap, self.rec, self.plan, self.dry_run = [], None, None, None, False
        self.vm_snaps, self.vm_snap, self.vmcfg, self.vm_plan, self.vm_targets = [], None, None, None, []
        self.restart = False      # nach einem Update von pbstui: Assistent mit dem neuen Code neu starten

    def on_mount(self) -> None:
        self.push_screen(ConnectScreen())


def run() -> None:
    from ..cli.rescue_cli import DEFAULT_CONFIG
    cfg = RescueConfig(server="", datastore="", auth_id="")
    if Path(DEFAULT_CONFIG).exists():  # personalisierte ISO: Verbindungsdaten (ohne Geheimnisse) vorausfüllen
        try:
            cfg = RescueConfig.from_json(DEFAULT_CONFIG)
        except (OSError, ValueError, TypeError, KeyError) as e:   # defekte Vorgabe: leer starten, aber nicht verschweigen
            print(tr("Hinweis: {0} nicht lesbar ({1}); Felder bleiben leer.", DEFAULT_CONFIG, e))
    if os.geteuid() != 0:
        print(tr("Hinweis: Ohne root ist nur der Trockenlauf möglich."))
    app = RescueApp(cfg)
    app.run()
    if app.restart:
        os.execvp("pbsgui", ["pbsgui", "rescue"])  # noqa: S606  # nosec B606 - feste Argumente
