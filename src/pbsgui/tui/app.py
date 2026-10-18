"""Terminal-Oberfläche (Textual) auf Basis von pbsgui.core."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, OptionList, RichLog, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from ..cli.main import fmt_size, fmt_time, last_run_text, schedule_text
from ..core.manager import ProfileManager, can_use_admin
from ..core.models import Profile
from ..core.paths import Paths
from ..core.runner import run_profile
from ..core.scheduler import SchedulerError, validate_calendar
from .screens import AddServerScreen, ConfirmScreen, PromptScreen


class PbsTui(App):
    TITLE = "pbsgui – Proxmox Backup"
    CSS = """
    #profiles { width: 30; border: round $primary; }
    #main { border: round $primary; }
    #dialog { width: 80; max-height: 90%; margin: 1 2; padding: 1 2; border: thick $accent; background: $surface; }
    ModalScreen { align: center middle; }
    .hint { color: $text-muted; }
    """
    BINDINGS = [("a", "add", "Hinzufügen"), ("b", "backup", "Backup"), ("r", "refresh", "Snapshots"),
                ("t", "restore", "Restore"), ("s", "schedule", "Zeitplan"), ("m", "scope", "Modus"),
                ("v", "encrypt", "Verschl."), ("k", "key", "Key sichern"), ("o", "sources", "Quellen"),
                ("d", "delete", "Entfernen"), ("q", "quit", "Beenden")]

    def __init__(self, manager: ProfileManager | None = None):
        super().__init__()
        self.mgr = manager or ProfileManager()
        self.current: Profile | None = None
        self._snaps: list = []
        self._busy = False

    def _notify(self, message: str, **kw) -> None:
        """notify() ohne Markup-Auswertung (Fehlertexte können eckige Klammern enthalten)."""
        self.notify(escape(message), **kw)

    def _q(self, selector, expect_type=None):
        """Widget der Hauptansicht, auch wenn gerade ein modaler Dialog darüber liegt.

        App.query_one fragt den *aktiven* Screen ab; während ein Dialog schließt, ist das noch der Dialog.
        """
        return self.screen_stack[0].query_one(selector, expect_type) if expect_type else self.screen_stack[0].query_one(selector)

    # -- Aufbau ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="profiles")
            with TabbedContent(id="main"):
                with TabPane("Übersicht", id="tab-overview"):
                    yield Static("", id="overview", markup=False)
                with TabPane("Snapshots", id="tab-snaps"):
                    yield DataTable(id="snaps", cursor_type="row")
                with TabPane("Log", id="tab-log"):
                    yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._q("#snaps", DataTable).add_columns("#", "Zeit", "ID", "Größe", "Dateien")
        self.reload()

    # -- Profile ------------------------------------------------------------------
    def reload(self, select: str | None = None) -> None:
        lst = self._q("#profiles", OptionList)
        profiles = self.mgr.profiles()
        lst.clear_options()
        lst.add_options([Option(Text(p.name), id=p.slug) for p in profiles])  # Text: kein Markup
        idx = next((i for i, p in enumerate(profiles) if p.name == select), 0) if profiles else None
        if idx is not None:
            lst.highlighted = idx
        self.current = profiles[idx] if idx is not None else None
        self.show_overview()

    def on_option_list_option_highlighted(self, e: OptionList.OptionHighlighted) -> None:
        if not self.screen_stack:  # App fährt gerade herunter
            return
        profiles = self.mgr.profiles()
        self.current = profiles[e.option_index] if 0 <= e.option_index < len(profiles) else None
        self._snaps = []
        self._q("#snaps", DataTable).clear()
        self.show_overview()

    def show_overview(self) -> None:
        p, w = self.current, self._q("#overview", Static)
        if not p:
            w.update("Keine Profile. Mit [a] einen Server hinzufügen.")
            return
        if p.keyfile:
            key = f"eigene Schlüsseldatei {p.keyfile}"
        elif self.mgr.secrets.get(p.slug, "key"):
            key = f"automatischer Schlüssel, Fingerprint {p.key_fingerprint}"
        else:
            key = "kein Schlüssel"
        w.update("\n".join([
            f"Profil:        {p.name}", f"Server:        {p.server}:{p.port}",
            f"Datastore:     {p.datastore}" + (f" / Namespace {p.namespace}" if p.namespace else ""),
            f"Zugang:        {p.auth_id}", f"Backup-ID:     {p.backup_id}",
            f"Quellen:       {', '.join(p.sources) or '-'}", f"Ausschlüsse:   {', '.join(p.excludes) or '-'}",
            f"Verschlüsselt: {'ja' if p.encrypt else 'NEIN – Backups liegen unverschlüsselt auf dem Server'}  ({key})",
            f"Zeitplan:      {p.schedule or '-'}  [{p.scope}, {schedule_text(self.mgr, p)}]",
            f"Letzter Lauf:  {last_run_text(p)}"]))

    def write_log(self, text: str) -> None:  # App.log ist Textuals Debug-Logger, deshalb anderer Name
        self._q("#log", RichLog).write(text)

    def _need(self) -> Profile | None:
        if not self.current:
            self._notify("Kein Profil ausgewählt", severity="warning")
        return self.current

    def _start(self) -> bool:
        if self._busy:
            self._notify("Es läuft bereits eine Aktion", severity="warning")
            return False
        self._busy = True
        return True

    # -- Aktionen ------------------------------------------------------------------
    def action_add(self) -> None:
        def done(res):
            if not res:
                return
            p, token = res
            p.sources = [str(Path.home())]
            try:
                self.mgr.save(p, token=token)
                created = self.mgr.enable_encryption(p)
            except Exception as e:
                self._notify(f"Speichern fehlgeschlagen: {e}", severity="error")
                return
            self.reload(select=p.name)
            if created:
                self._notify(f"Schlüssel erzeugt ({p.key_fingerprint}). Jetzt mit [k] sichern: ohne Kopie sind "
                            "Backups bei Verlust unlesbar!", severity="warning", timeout=15)
        self.push_screen(AddServerScreen(), done)

    def action_backup(self) -> None:
        p = self._need()
        if not p or not self._start():
            return
        self._q("#log", RichLog).clear()
        self._q(TabbedContent).active = "tab-log"
        self.write_log(f"Starte Backup '{p.name}' …")

        def work():
            code = run_profile(p, Paths.for_scope("user"), self.mgr.secrets,
                               on_line=lambda l: self.call_from_thread(self.write_log, l))
            self.call_from_thread(self._backup_done, code)

        self.run_worker(work, thread=True)

    def _backup_done(self, code: int) -> None:
        self._busy = False
        self._notify("Backup erfolgreich" if code == 0 else f"Backup fehlgeschlagen (Exitcode {code})",
                    severity="information" if code == 0 else "error")
        self.show_overview()

    def action_refresh(self) -> None:
        p = self._need()
        if not p or not self._start():
            return
        self._q(TabbedContent).active = "tab-snaps"

        def work():
            try:
                snaps = self.mgr.snapshots(p)
            except Exception as e:
                self.call_from_thread(self._fail, str(e))
                return
            self.call_from_thread(self._show_snaps, snaps)

        self.run_worker(work, thread=True)

    def _fail(self, msg: str) -> None:
        self._busy = False
        self._notify(msg, severity="error", timeout=10)

    def _show_snaps(self, snaps: list) -> None:
        self._busy = False
        self._snaps = snaps
        t = self._q("#snaps", DataTable)
        t.clear()
        for i, s in enumerate(snaps):
            t.add_row(str(i), fmt_time(s.backup_time), s.backup_id, fmt_size(s.size), ", ".join(s.files))
        self._notify(f"{len(snaps)} Snapshots")

    def action_restore(self) -> None:
        p = self._need()
        t = self._q("#snaps", DataTable)
        if not p:
            return
        if not self._snaps or t.cursor_row >= len(self._snaps):
            self._notify("Erst mit [r] Snapshots laden und einen auswählen", severity="warning")
            return
        snap = self._snaps[t.cursor_row]
        archives = [f.removesuffix(".didx") for f in snap.files if f.endswith((".pxar", ".pxar.didx"))]
        if not archives:
            self._notify("Snapshot enthält kein Datei-Archiv", severity="error")
            return
        default = str(Path.home() / f"pbsgui-restore-{time.strftime('%Y%m%d-%H%M%S')}")

        def got(target):
            if not target or not self._start():
                return
            tp = Path(target).expanduser()
            if tp.exists() and any(tp.iterdir()):
                self._busy = False
                self._notify("Zielordner ist nicht leer – bitte neuen wählen", severity="error")
                return

            def work():
                try:
                    self.mgr.client(p).restore(snap, archives[0], str(tp))
                except Exception as e:
                    self.call_from_thread(self._fail, str(e))
                    return
                self.call_from_thread(self._restored, str(tp))
            self.run_worker(work, thread=True)

        self.push_screen(PromptScreen(f"Wiederherstellen von {snap.path} ({archives[0]}) nach:", default,
                                      "Neuer oder leerer Ordner; das Original wird nie überschrieben."), got)

    def _restored(self, target: str) -> None:
        self._busy = False
        self._notify(f"Wiederhergestellt nach {target}")

    def action_schedule(self) -> None:
        p = self._need()
        if not p:
            return

        def got(expr):
            if expr is None:
                return
            try:
                if not expr.strip():
                    self.mgr.save(p, schedule="remove")
                    p.schedule = ""
                    self.mgr.save(p)
                else:
                    validate_calendar(expr)
                    p.schedule = expr.strip()
                    self.mgr.save(p, schedule="install")
            except (SchedulerError, Exception) as e:
                self._notify(str(e), severity="error", timeout=10)
                return
            self._notify("Zeitplan aktualisiert")
            self.show_overview()

        self.push_screen(PromptScreen("Zeitplan (systemd OnCalendar)", p.schedule or "daily",
                                      "z. B. daily, hourly, Mon *-*-* 03:00:00. Leer = Zeitplan entfernen."), got)

    def action_scope(self) -> None:
        p = self._need()
        if not p:
            return
        new = "system" if p.scope == "user" else "user"
        if new == "system" and not can_use_admin():
            self._notify("Modus 'system' braucht Administrator-Rechte", severity="error")
            return

        def got(yes):
            if not yes:
                return
            try:
                self.mgr.move_scope(p, new)
                self.mgr.save(p, schedule="install" if p.schedule else "keep")
            except Exception as e:
                self._notify(str(e), severity="error", timeout=10)
                return
            self.show_overview()
        self.push_screen(ConfirmScreen(f"Modus von '{p.scope}' auf '{new}' wechseln? Ein vorhandener Zeitplan "
                                       "wird im neuen Modus neu eingerichtet."), got)

    def action_encrypt(self) -> None:
        p = self._need()
        if not p:
            return
        if p.encrypt:
            def off(yes):
                if yes:
                    p.encrypt = False
                    self.mgr.save(p)
                    self.show_overview()
            self.push_screen(ConfirmScreen("Verschlüsselung ausschalten? Neue Backups liegen dann UNVERSCHLÜSSELT "
                                           "auf dem Server."), off)
            return
        try:
            created = self.mgr.enable_encryption(p)
            self.mgr.save(p)
        except Exception as e:
            self._notify(str(e), severity="error", timeout=10)
            return
        self.show_overview()
        if created:
            self._notify(f"Schlüssel erzeugt ({p.key_fingerprint}). Jetzt mit [k] sichern!", severity="warning", timeout=15)

    def action_key(self) -> None:
        p = self._need()
        if not p:
            return
        if not self.mgr.secrets.get(p.slug, "key"):
            self._notify("Kein automatisch erzeugter Schlüssel vorhanden", severity="warning")
            return

        def got(dest):
            if not dest:
                return
            d = Path(dest).expanduser()
            if d.exists():
                self._notify(f"{d} existiert bereits", severity="error")
                return
            try:
                self.mgr.export_key(p, str(d))
            except Exception as e:
                self._notify(str(e), severity="error")
                return
            self._notify(f"Schlüssel exportiert nach {d} (0600). Sicher aufbewahren!", severity="warning", timeout=10)
        self.push_screen(PromptScreen("Schlüssel exportieren nach:", str(Path.home() / f"{p.slug}-key.json"),
                                      "Ohne Kopie sind verschlüsselte Backups bei Verlust nicht wiederherstellbar."), got)

    def action_sources(self) -> None:
        p = self._need()
        if not p:
            return

        def got(text):
            if text is None:
                return
            p.sources = [s.strip() for s in text.split(",") if s.strip()]
            self.mgr.save(p)
            self.show_overview()
        self.push_screen(PromptScreen("Quellordner (durch Komma getrennt)", ", ".join(p.sources)), got)

    def action_delete(self) -> None:
        p = self._need()
        if not p:
            return

        def got(yes):
            if yes:
                try:
                    self.mgr.delete(p)
                except Exception as e:
                    self._notify(str(e), severity="error")
                self.reload()
        self.push_screen(ConfirmScreen(f"Profil '{p.name}' samt Zeitplan und Geheimnissen entfernen? "
                                       "(Der Zugang auf dem PBS-Server bleibt bestehen.)"), got)


def run() -> None:
    PbsTui().run()
