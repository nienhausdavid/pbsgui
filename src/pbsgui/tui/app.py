"""Terminal-Oberfläche (Textual) auf Basis von pbsgui.core."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from rich.markup import escape
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, OptionList, ProgressBar, RichLog, Static, TabbedContent,
                             TabPane)
from textual.widgets.option_list import Option

from ..i18n import tr
from ..cli.insight import OVERVIEW_HEADER, overview_details, overview_row, run_row, usage_text
from ..cli.main import fmt_size, fmt_time, last_run_text, schedule_text
from ..core.manager import ProfileManager, can_use_admin
from ..core.models import METADATA, Profile
from ..core.modes import FULL, describe, plan_mounts
from ..core.paths import Paths
from ..core.recovery import needs_key_export, render_sheet
from ..core.runner import read_status
from ..core.scheduler import SchedulerError, validate_calendar
from .files import FilesScreen
from .insight import DiffScreen, RestoreTestScreen, SearchAllScreen
from .master import MasterScreen, RecoverKeyScreen
from .server import ServerScreen
from .screens import AddServerScreen, ConfirmScreen, DbDumpScreen, DetectScreen, KeysScreen, MountsScreen, PromptScreen


_ROW = re.compile(r"^([^:\n]{1,28}:)\s+(.*)$", re.S)
_HINT = re.compile(r"\((?:mit|with) \[.\][^)]*\)")


def overview_text(sections: list[tuple[str, list[str]]]) -> Table:
    """Übersicht als zwei Spalten mit Abschnitten: Bezeichnung in einer festen Breite, Auffälliges farbig."""
    rows = [[(m.group(1), m.group(2)) if (m := _ROW.match(line)) else ("", line) for line in lines] for _t, lines in sections]
    width = max((len(label) for sec in rows for label, _v in sec), default=0) + 2
    bad = (tr("NEIN"), tr("NOCH NICHT"), tr("FEHLER"), "FEHLER", tr("gesperrt"))
    grid = Table.grid(padding=(0, 0))
    grid.add_column(width=width, no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    first = True
    for (title, _lines), sec in zip(sections, rows):
        if not sec:
            continue
        if not first:
            grid.add_row("", "")
        first = False
        grid.add_row(Text(title.upper(), style="bold dim"), "")
        for label, value in sec:
            v = Text(value)
            v.highlight_regex(_HINT, "dim")
            v.highlight_words(bad, "bold red")
            v.highlight_regex(r"\bOK\b", "bold green")
            grid.add_row(Text(label, style="bold"), v)
    return grid


class PbsTui(App):
    TITLE = tr("pbsgui – Proxmox Backup")
    SUB_TITLE = ""
    CSS = """
    #profiles { width: 32; height: 1fr; border: round $primary; border-title-color: $text; border-title-style: bold; padding: 0 1; }
    #profiles:focus { border: round $accent; }
    #main { width: 1fr; height: 1fr; border: round $primary; padding: 0 1; }
    #overview { height: auto; margin-top: 1; }
    #key-buttons { height: auto; margin-top: 1; }
    #key-buttons Button { margin-right: 1; }
    #dashboard { margin-top: 1; }
    #hist-filter { height: 3; }
    #hist-text { width: 1fr; }
    #hist-filter Checkbox { width: auto; }
    #hist-table { height: 1fr; min-height: 4; }
    #hist-log { height: 1fr; border-top: solid $primary; }
    #bar { margin-top: 1; }
    #dialog { width: 90%; max-width: 100; height: auto; max-height: 90%; overflow-x: hidden; margin: 1 2; padding: 1 2;
              border: round $accent; border-title-color: $accent; border-title-style: bold; background: $surface; }
    #dialog Label { text-style: bold; margin-top: 1; }
    #dialog Input { border: round $primary; border-title-color: $text-muted; }
    #dialog Input:focus { border: round $accent; border-title-color: $accent; }
    #dialog Button { margin-top: 1; }
    .buttons { height: auto; align-horizontal: right; }
    .buttons Button { margin-left: 2; }
    ModalScreen { align: center middle; }
    .hint { color: $text-muted; }
    """
    # In der Fußzeile stehen nur die häufigen Tasten; alle zeigt [?] (KeysScreen), nach Gruppen sortiert.
    BINDINGS = [Binding("a", "add", tr("Hinzufügen")), Binding("b", "backup", "Backup"), Binding("r", "refresh", "Snapshots"),
                Binding("t", "restore", tr("Restore")), Binding("f", "files", tr("Dateien")), Binding("s", "schedule", tr("Zeitplan"), show=False),
                Binding("m", "scope", tr("Modus"), show=False), Binding("v", "encrypt", tr("Verschl."), show=False),
                Binding("k", "key", tr("Key sichern"), show=False), Binding("y", "copy_key", tr("Key kopieren"), show=False),
                Binding("M", "master", tr("Master-Key"), show=False), Binding("K", "recover_key", tr("Key zurückholen"), show=False),
                Binding("n", "sheet", tr("Notfallblatt"), show=False), Binding("o", "sources", tr("Quellen"), show=False),
                Binding("u", "mounts", tr("Laufwerke"), show=False), Binding("p", "dbdump", "DB-Dumps", show=False),
                Binding("c", "detect", tr("Änderungen"), show=False), Binding("i", "server", tr("Server ändern"), show=False),
                Binding("x", "diff", tr("Vergleich"), show=False), Binding("w", "restore_test", "Restore-Test", show=False),
                Binding("g", "find_all", tr("Suche alle"), show=False), Binding("e", "dashboard", "Dashboard", show=False),
                Binding("h", "history", tr("Verlauf"), show=False), Binding("d", "delete", tr("Entfernen"), show=False),
                Binding("question_mark", "keys", tr("Alle Tasten"), key_display="?"), Binding("q", "quit", tr("Beenden"))]
    KEY_GROUPS = ((tr("Backup und Snapshots"), ("backup", "refresh", "restore", "files", "diff", "restore_test", "find_all")),
                  (tr("Profil"), ("add", "server", "sources", "mounts", "detect", "dbdump", "schedule", "scope", "delete")),
                  (tr("Schlüssel"), ("encrypt", "copy_key", "key", "sheet", "master", "recover_key")),
                  (tr("Ansicht"), ("dashboard", "history", "keys", "quit")))

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
            lst = OptionList(id="profiles")
            lst.border_title = tr("Profile")
            yield lst
            with TabbedContent(id="main"):
                with TabPane(tr("Übersicht"), id="tab-overview"):
                    yield Static("", id="overview", markup=False)
                    with Horizontal(id="key-buttons"):
                        yield Button(tr("Schlüssel + Notfallblatt kopieren"), id="btn-copy-kit", variant="primary")
                        yield Button(tr("Schlüssel kopieren"), id="btn-copy-key")
                        yield Button(tr("Notfallblatt kopieren"), id="btn-copy-sheet")
                        yield Button(tr("In Datei speichern …"), id="btn-key-file")
                with TabPane("Snapshots", id="tab-snaps"):
                    yield DataTable(id="snaps", cursor_type="row")
                with TabPane("Dashboard", id="tab-dashboard"):
                    yield Static("", id="dashboard", markup=False)
                with TabPane(tr("Verlauf"), id="tab-history"):
                    with Horizontal(id="hist-filter", classes="hint"):
                        yield Input(placeholder=tr("Text in Meldung oder Ausgabe (Enter)"), id="hist-text")
                        yield Checkbox(tr("Nur Fehler"), id="hist-failed")
                        yield Checkbox(tr("Alle Profile"), id="hist-all")
                    yield DataTable(id="hist-table", cursor_type="row")
                    yield RichLog(id="hist-log", wrap=True, markup=False)
                with TabPane("Log", id="tab-log"):
                    yield ProgressBar(total=100, show_eta=False, id="bar")
                    yield Static("", id="bar-text", markup=False)
                    yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        from .. import __version__
        self.sub_title = tr("Version {0}", __version__)
        self._q("#snaps", DataTable).add_columns("#", tr("Zeit"), "ID", tr("Größe"), tr("Dateien"))
        self._q("#hist-table", DataTable).add_columns("Start", tr("Profil"), tr("Ergebnis"), tr("Dauer"), tr("Meldung"))
        self._runs: list = []
        self.reload()

    # -- Profile ------------------------------------------------------------------
    def reload(self, select: str | None = None) -> None:
        lst = self._q("#profiles", OptionList)
        profiles = self.mgr.profiles()
        lst.clear_options()
        lst.add_options([Option(self._profile_label(p), id=p.slug) for p in profiles])  # Text: kein Markup
        idx = next((i for i, p in enumerate(profiles) if p.name == select), 0) if profiles else None
        if idx is not None:
            lst.highlighted = idx
        self.current = profiles[idx] if idx is not None else None
        self.show_overview()

    def _refresh_dots(self) -> None:
        lst = self._q("#profiles", OptionList)
        for p in self.mgr.profiles():
            try:
                lst.replace_option_prompt(p.slug, self._profile_label(p))
            except Exception:  # nosec B110  (Profil inzwischen entfernt: die Liste wird ohnehin neu geladen)
                pass

    def _profile_label(self, p: Profile) -> Text:
        """Name mit Punkt für den letzten Lauf: grün = gelungen, rot = fehlgeschlagen, grau = noch keiner."""
        try:
            last = read_status(Paths.for_scope(p.scope), p)
        except Exception:
            last = None
        color = "grey50" if not last else ("green" if last.get("ok") else "red")
        return Text.assemble(("● ", color), p.name)

    def on_option_list_option_highlighted(self, e: OptionList.OptionHighlighted) -> None:
        if not self.screen_stack or e.option_list.id != "profiles":  # App fährt gerade herunter, oder Liste eines Dialogs
            return
        profiles = self.mgr.profiles()
        self.current = profiles[e.option_index] if 0 <= e.option_index < len(profiles) else None
        self._snaps = []
        self._q("#snaps", DataTable).clear()
        self.show_overview()

    def show_overview(self) -> None:
        p, w = self.current, self._q("#overview", Static)
        can_copy = bool(p and p.encrypt and not p.keyfile and self.mgr.secrets.exists(p.slug, "key"))
        self._q("#key-buttons").display = bool(p)
        for bid in ("#btn-copy-kit", "#btn-copy-key", "#btn-key-file"):      # ohne automatischen Schlüssel gibt es nichts zu kopieren
            self._q(bid, Button).display = can_copy
        if not p:
            w.update(tr("Keine Profile. Mit [a] einen Server hinzufügen."))
            return
        if p.keyfile:
            key = tr("eigene Schlüsseldatei {0}", p.keyfile)
        elif self.mgr.secrets.exists(p.slug, "key"):
            key = tr("automatischer Schlüssel, Fingerprint {0}", p.key_fingerprint)
        else:
            key = tr("kein Schlüssel")
        auto_key = p.encrypt and not p.keyfile
        sections = [
            (tr("Ziel"), [
                tr("Profil:        {0}", p.name), tr("Server:        {0}:{1}", p.server, p.port),
                tr("Datastore:     {0}", p.datastore) + (tr(" / Namespace {0}", p.namespace) if p.namespace else ""),
                tr("Zugang:        {0}", p.auth_id), tr("Backup-ID:     {0}", p.backup_id)]),
            (tr("Umfang"), [
                tr("Umfang:        {0}", describe(p.backup_mode)),
                tr("Quellen:       {0}", ', '.join(p.effective_sources) or '-'),
                *([tr("Laufwerke:     {0}", ', '.join(plan_mounts(p.full_mounts)[0]) or tr("keine (nur Root-Dateisystem)"))]
                  if p.backup_mode == FULL else []),
                tr("Ausschlüsse:   {0}", ', '.join(p.excludes) or '-'),
                tr("Lesen:        {0}  (mit [c] ändern)", tr("nur Geändertes (Metadaten-Vergleich)") if p.change_detection == METADATA else tr("alles jedes Mal (klassisch)")),
                *([tr("Prüflauf:     {0}", (tr("wöchentlich alles neu lesen: ") + p.data_schedule) if p.weekly_data else tr("aus"))
                   + ("" if p.schedule or not p.weekly_data else tr(" (braucht einen Zeitplan)"))]
                  if p.change_detection == METADATA else []),
                tr("Datenbank-Dumps: {0}  (mit [p] einrichten)", tr("an") if p.db_dump else tr("aus"))]),
            (tr("Verschlüsselung"), [
                tr("Verschlüsselt: {0}  ({1})", tr("ja") if p.encrypt else tr("NEIN – Backups liegen unverschlüsselt auf dem Server"), key),
                *([(tr("Schlüssel-Export: ") + (tr("am {0}", p.key_exported_at) if p.key_exported_at else
                    tr("NOCH NICHT") + (tr(" – Vollbackup gesperrt! Mit [k] sichern") if needs_key_export(p) else "")))]
                  if auto_key else []),
                *([tr("Master-Schlüssel: {0}  (mit [M] einrichten)", tr("ja, Fingerprint {0}; privater Teil gesichert: {1}", p.master_fingerprint[:23], p.master_key_ack_at or tr("NEIN")) if p.master_pubkey else tr("nein"))]
                  if auto_key else [])]),
            (tr("Zeitplan"), [
                tr("Zeitplan:      {0}  [{1}, {2}]", p.schedule or '-', p.scope, schedule_text(self.mgr, p))
                + (tr(" – wartet auf den Schlüssel-Export ([k])") if self._schedule_waits(p) else ""),
                tr("Letzter Lauf:  {0}", last_run_text(p))])]
        w.update(overview_text(sections))

    def _schedule_waits(self, p: Profile) -> bool:
        try:
            return self.mgr.schedule_pending(p) and needs_key_export(p)
        except Exception:
            return False

    def _activate_schedule(self, p: Profile) -> None:
        """Vermerkten Zeitplan einrichten (nach dem Anlegen bzw. nach dem Schlüssel-Export)."""
        try:
            if self.mgr.activate_pending_schedule(p):
                self._notify(tr("Zeitplan aktiv: {0}", p.schedule))
            elif self.mgr.schedule_pending(p):
                self._notify(tr("Zeitplan ist vermerkt und startet, sobald der Schlüssel exportiert ist ([k])."),
                             severity="warning", timeout=10)
        except Exception as e:
            self._notify(tr("Zeitplan nicht eingerichtet: {0}", e), severity="error", timeout=10)

    def write_log(self, text: str) -> None:  # App.log ist Textuals Debug-Logger, deshalb anderer Name
        self._q("#log", RichLog).write(text)

    def _need(self) -> Profile | None:
        if not self.current:
            self._notify(tr("Kein Profil ausgewählt"), severity="warning")
        return self.current

    def _start(self) -> bool:
        if self._busy:
            self._notify(tr("Es läuft bereits eine Aktion"), severity="warning")
            return False
        self._busy = True
        return True

    # -- Aktionen ------------------------------------------------------------------
    def action_add(self) -> None:
        def done(res):
            if not res:
                return
            p, token, start_now = res
            if not p.sources:
                p.sources = [str(Path.home())]
            try:
                self.mgr.save(p, token=token)
                created = self.mgr.enable_encryption(p)
            except Exception as e:
                self._notify(tr("Speichern fehlgeschlagen: {0}", e), severity="error")
                return
            self.reload(select=p.name)
            if created:
                self._notify(tr("Schlüssel erzeugt ({0}). Jetzt mit [k] sichern: ohne Kopie sind Backups bei Verlust unlesbar!", p.key_fingerprint), severity="warning", timeout=15)
            if p.schedule:
                self._activate_schedule(p)
                self.show_overview()
            after = (lambda: self._start_first_backup(p)) if start_now else None
            if p.db_dump:  # erst die Datenbanken und Zugänge zeigen, das Backup startet danach
                self.action_dbdump(after=after)
            elif after:
                after()
        self.push_screen(AddServerScreen(taken=self.mgr.name_taken), done)

    def action_backup(self) -> None:
        p = self._need()
        if p and needs_key_export(p):          # gesperrt, bis der Schlüssel gesichert ist: per Zwischenablage gleich erledigen
            def answered(yes):
                if yes:
                    self.copy_what("kit")
                    if not needs_key_export(self.mgr.profiles_store.load(p.name)):
                        self.current = self.mgr.profiles_store.load(p.name)
                        self.action_backup()
            self.push_screen(ConfirmScreen(tr("Dieses Vollbackup startet, sobald der Schlüssel gesichert ist. Schlüssel und Notfallblatt jetzt in die Zwischenablage kopieren (im Passwortmanager einfügen) und das Backup starten?")), answered)
            return
        if not p or not self._start():
            return
        self._q("#log", RichLog).clear()
        self._q(TabbedContent).active = "tab-log"
        self.write_log(tr("Starte Backup '{0}' …", p.name))
        self._q("#bar", ProgressBar).update(total=None)           # unbestimmt bis zur ersten Messung
        self._q("#bar-text", Static).update(tr("Starte …"))

        def work():
            try:
                code = self.mgr.run_backup(p, on_line=lambda l: self.call_from_thread(self.backup_line, l))
            except Exception as e:
                self.call_from_thread(self.write_log, tr("Fehler: {0}", e))
                code = 1
            self.call_from_thread(self._backup_done, code)

        self.run_worker(work, thread=True)

    def backup_line(self, line: str) -> None:
        """Fortschrittszeilen aktualisieren den Balken, alles andere kommt ins Log."""
        from ..core.progress import decode, describe
        prog = decode(line)
        if prog is None:
            self.write_log(line)
            return
        bar = self._q("#bar", ProgressBar)
        if prog.percent is None:
            bar.update(total=None)
        else:
            bar.update(total=100, progress=prog.percent)
        self._q("#bar-text", Static).update(describe(prog))

    def _backup_done(self, code: int) -> None:
        self._busy = False
        bar = self._q("#bar", ProgressBar)
        bar.update(total=100, progress=100 if code == 0 else bar.progress or 0)
        self._q("#bar-text", Static).update(tr("Fertig.") if code == 0 else tr("Fehlgeschlagen."))
        self._notify(tr("Backup erfolgreich") if code == 0 else tr("Backup fehlgeschlagen (Exitcode {0})", code),
                    severity="information" if code == 0 else "error")
        self._refresh_dots()
        self.show_overview()

    def action_refresh(self) -> None:
        p = self._need()
        if not p or not self._start():
            return
        self._q(TabbedContent).active = "tab-snaps"
        self._q("#snaps", DataTable).loading = True          # der Server braucht einige Sekunden: zeigen, dass etwas läuft

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
        self._q("#snaps", DataTable).loading = False
        self._notify(msg, severity="error", timeout=10)

    def _show_snaps(self, snaps: list) -> None:
        self._busy = False
        self._snaps = snaps
        t = self._q("#snaps", DataTable)
        t.loading = False
        t.clear()
        for i, s in enumerate(snaps):
            t.add_row(str(i), fmt_time(s.backup_time), s.backup_id, fmt_size(s.size), ", ".join(s.files))
        self._notify(tr("{0} Snapshots", len(snaps)))

    def action_files(self) -> None:
        p = self._need()
        t = self._q("#snaps", DataTable)
        if not p:
            return
        if not self._snaps or t.cursor_row >= len(self._snaps):
            self._notify(tr("Erst mit [r] Snapshots laden und einen auswählen"), severity="warning")
            return
        self.push_screen(FilesScreen(self.mgr, p, self._snaps[t.cursor_row]))

    def action_server(self) -> None:
        p = self._need()
        if not p:
            return

        def got(res):
            if res is None:
                return
            if res.changed:
                self._notify(tr("Verbindung geändert: ") + ", ".join(res.changed), timeout=10)
                for n in res.notes:
                    self._notify(n, severity="warning", timeout=15)
                self.reload(select=p.name)
        self.push_screen(ServerScreen(self.mgr, p), got)

    # -- Snapshots vergleichen, prüfen, durchsuchen --------------------------------------
    def _selected_snapshot(self):
        p = self._need()
        t = self._q("#snaps", DataTable)
        if not p:
            return None
        if not self._snaps or t.cursor_row >= len(self._snaps):
            self._notify(tr("Erst mit [r] Snapshots laden und einen auswählen"), severity="warning")
            return None
        return p, t.cursor_row

    def action_diff(self) -> None:
        sel = self._selected_snapshot()
        if not sel:
            return
        p, row = sel
        if row + 1 >= len(self._snaps):
            self._notify(tr("Zum Vergleichen braucht es einen älteren Snapshot"), severity="warning")
            return
        self.push_screen(DiffScreen(self.mgr, p, self._snaps[row + 1], self._snaps[row]))

    def action_restore_test(self) -> None:
        sel = self._selected_snapshot()
        if sel:
            self.push_screen(RestoreTestScreen(self.mgr, sel[0], self._snaps[sel[1]]))

    def action_find_all(self) -> None:
        p = self._need()
        if not p:
            return
        self.push_screen(PromptScreen(tr("In allen Snapshots suchen"), "", tr("Name oder Muster mit * und ?; findet auch gelöschte Dateien.")),
                         lambda text: text and text.strip() and self.push_screen(SearchAllScreen(self.mgr, p, text.strip())))

    # -- Dashboard und Verlauf --------------------------------------------------------------
    def action_dashboard(self) -> None:
        self._q(TabbedContent).active = "tab-dashboard"
        self._q("#dashboard", Static).update(tr("Lade …"))

        def work():
            try:
                profiles = self.mgr.profiles()
                ovs = [self.mgr.overview(p) for p in profiles]
                usages, seen = [], set()
                for p in profiles:
                    key = (p.server, p.port, p.datastore)
                    if key not in seen:
                        seen.add(key)
                        usages.append(usage_text(p, self.mgr.usage(p)))
                tests = {p.slug: self.mgr.last_restore_test(p) for p in profiles}
            except Exception as e:
                self.call_from_thread(self._q("#dashboard", Static).update, tr("Fehler: {0}", e))
                return
            self.call_from_thread(self._show_dashboard, ovs, usages, tests)

        self.run_worker(work, thread=True)

    def _show_dashboard(self, ovs, usages, tests) -> None:
        if not ovs:
            self._q("#dashboard", Static).update(tr("Keine Profile. Mit [a] einen Server hinzufügen."))
            return
        # Tabelle, die sich der Breite anpasst (als fester Text brach sie in schmalen Fenstern mitten in der Zeile um)
        grid = Table(box=None, padding=(0, 2, 0, 0), header_style="bold", expand=False)
        for i, head in enumerate(OVERVIEW_HEADER):
            grid.add_column(head, no_wrap=i != 0)
        for o in ovs:
            row = overview_row(o)
            color = {"ok": "green", "error": "bold red", "warning": "yellow", "running": "cyan"}.get(o.state, "")
            grid.add_row(Text(row[0], style="bold"), Text(row[1], style=color), *row[2:])
        lines = []
        for o in ovs:
            lines += overview_details(o)
            t = tests.get(o.profile.slug)
            lines.append(f"{o.profile.name}: " + (tr("letzter Restore-Test {0}: {1}", fmt_time(t['time']), t['summary']) if t
                                                   else tr("noch kein Restore-Test (mit [w] im Snapshot-Tab)")))
        self._dashboard_loaded = True
        self._q("#dashboard", Static).update(Group(grid, Text(""), Text("\n".join([*lines, "", *usages]))))

    def action_history(self) -> None:
        self._q(TabbedContent).active = "tab-history"
        self.reload_history()

    def reload_history(self) -> None:
        text = self._q("#hist-text", Input).value.strip()
        failed = self._q("#hist-failed", Checkbox).value
        everyone = self._q("#hist-all", Checkbox).value or self.current is None
        prof = None if everyone else self.current
        names = {p.slug: p.name for p in self.mgr.profiles()}
        self._runs = self.mgr.history(prof, failed_only=failed, text=text, limit=200, with_log=True)
        t = self._q("#hist-table", DataTable)
        t.clear()
        for r in self._runs:
            t.add_row(*self._run_cells(r, names))
        self._q("#hist-log", RichLog).clear()
        if self._runs:
            self._show_run_log(0)
        else:
            self._q("#hist-log", RichLog).write(tr("Keine passenden Läufe."))

    @staticmethod
    def _run_cells(r, names: dict) -> list:
        """Zeile im Verlauf (ohne die Lauf-Nummer): Fehlschläge rot, Erfolge mit grünem Ergebnis."""
        cells = run_row(r, names)[1:]
        return [Text(c, style="red") for c in cells] if not r.ok else [cells[0], cells[1], Text(cells[2], style="green"), *cells[3:]]

    def _show_run_log(self, row: int) -> None:
        log = self._q("#hist-log", RichLog)
        log.clear()
        if 0 <= row < len(self._runs):
            for line in self._runs[row].log:
                log.write(line)

    def on_data_table_row_highlighted(self, e: DataTable.RowHighlighted) -> None:
        if e.data_table.id == "hist-table":
            self._show_run_log(e.cursor_row)

    def on_tabbed_content_tab_activated(self, e: TabbedContent.TabActivated) -> None:
        if e.tabbed_content.id != "main" or not self.screen_stack or self._q("#hist-table", DataTable) is None:
            return
        if e.pane.id == "tab-history":
            self.reload_history()
        elif e.pane.id == "tab-dashboard" and not getattr(self, "_dashboard_loaded", False):
            self.action_dashboard()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "hist-text":
            self.reload_history()

    def on_checkbox_changed(self, e: Checkbox.Changed) -> None:
        if e.checkbox.id in ("hist-failed", "hist-all"):
            self.reload_history()

    def action_restore(self) -> None:
        p = self._need()
        t = self._q("#snaps", DataTable)
        if not p:
            return
        if not self._snaps or t.cursor_row >= len(self._snaps):
            self._notify(tr("Erst mit [r] Snapshots laden und einen auswählen"), severity="warning")
            return
        snap = self._snaps[t.cursor_row]
        archives = snap.archives
        if not archives:
            self._notify(tr("Snapshot enthält kein Datei-Archiv"), severity="error")
            return
        default = str(Path.home() / f"pbsgui-restore-{time.strftime('%Y%m%d-%H%M%S')}")

        def got(target):
            if not target or not self._start():
                return
            tp = Path(target).expanduser()
            try:
                occupied = tp.exists() and (not tp.is_dir() or any(tp.iterdir()))
            except OSError as e:
                self._fail(str(e))
                return
            if occupied:
                self._busy = False
                self._notify(tr("Zielordner ist nicht leer – bitte neuen wählen"), severity="error")
                return

            def work():
                try:
                    self.mgr.client(p).restore(snap, archives[0], str(tp))
                except Exception as e:
                    self.call_from_thread(self._fail, str(e))
                    return
                self.call_from_thread(self._restored, str(tp))
            self.run_worker(work, thread=True)

        self.push_screen(PromptScreen(tr("Wiederherstellen von {0} ({1}) nach:", snap.path, archives[0]), default,
                                      tr("Neuer oder leerer Ordner; das Original wird nie überschrieben.")), got)

    def _restored(self, target: str) -> None:
        self._busy = False
        self._notify(tr("Wiederhergestellt nach {0}", target))

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
            self._notify(tr("Zeitplan aktualisiert"))
            self.show_overview()

        self.push_screen(PromptScreen(tr("Zeitplan (systemd OnCalendar)"), p.schedule or "daily",
                                      tr("z. B. daily, hourly, Mon *-*-* 03:00:00. Leer = Zeitplan entfernen.")), got)

    def action_scope(self) -> None:
        p = self._need()
        if not p:
            return
        if p.backup_mode == FULL:
            self._notify(tr("Ein Vollbackup braucht den System-Modus und kann nicht gewechselt werden"), severity="warning")
            return
        new = "system" if p.scope == "user" else "user"
        if new == "system" and not can_use_admin():
            self._notify(tr("Modus 'system' braucht Administrator-Rechte"), severity="error")
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
        self.push_screen(ConfirmScreen(tr("Modus von '{0}' auf '{1}' wechseln? Ein vorhandener Zeitplan wird im neuen Modus neu eingerichtet.", p.scope, new)), got)

    def action_encrypt(self) -> None:
        p = self._need()
        if not p:
            return
        if p.encrypt:
            def off(yes):
                if yes:
                    p.encrypt = False
                    try:
                        self.mgr.save(p)
                    except Exception as e:
                        p.encrypt = True
                        self._notify(str(e), severity="error", timeout=10)
                    self.show_overview()
            self.push_screen(ConfirmScreen(tr("Verschlüsselung ausschalten? Neue Backups liegen dann UNVERSCHLÜSSELT auf dem Server.")), off)
            return
        try:
            created = self.mgr.enable_encryption(p)
            self.mgr.save(p)
        except Exception as e:
            self._notify(str(e), severity="error", timeout=10)
            return
        self.show_overview()
        if created:
            self._notify(tr("Schlüssel erzeugt ({0}). Jetzt mit [k] sichern!", p.key_fingerprint), severity="warning", timeout=15)

    def action_key(self) -> None:
        p = self._need()
        if not p:
            return
        if not self.mgr.secrets.exists(p.slug, "key"):
            self._notify(tr("Kein automatisch erzeugter Schlüssel vorhanden"), severity="warning")
            return

        def got(dest):
            if not dest:
                return
            d = Path(dest).expanduser()
            if d.exists():
                self._notify(tr("{0} existiert bereits", d), severity="error")
                return
            try:
                self.mgr.export_key(p, str(d))
            except Exception as e:
                self._notify(str(e), severity="error")
                return
            self._notify(tr("Schlüssel exportiert nach {0} (0600). Sicher aufbewahren!", d), severity="warning", timeout=10)
            self._activate_schedule(p)
            self.show_overview()
        self.push_screen(PromptScreen(tr("Schlüssel exportieren nach:"), str(Path.home() / f"{p.slug}-key.json"),
                                      tr("Ohne Kopie sind verschlüsselte Backups bei Verlust nicht wiederherstellbar.")), got)

    def action_master(self) -> None:
        p = self._need()
        if not p:
            return
        if not p.encrypt or p.keyfile:
            self._notify(tr("Der Master-Schlüssel gehört zu Profilen mit automatisch erzeugtem Schlüssel"), severity="warning")
            return
        if p.master_pubkey:
            self._notify(tr("Es ist schon ein Master-Schlüssel eingetragen (Fingerprint {0})", p.master_fingerprint), severity="warning")
            return
        self.push_screen(MasterScreen(self.mgr, p), lambda _done: self.reload(select=p.name))

    def action_recover_key(self) -> None:
        p = self._need()
        if not p:
            return
        if not p.encrypt or p.keyfile:
            self._notify(tr("Der Master-Schlüssel gehört zu Profilen mit automatisch erzeugtem Schlüssel"), severity="warning")
            return
        self.push_screen(RecoverKeyScreen(self.mgr, p), lambda done: done and self.reload(select=p.name))

    def action_copy_key(self) -> None:
        self.copy_what("kit")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        what = {"btn-copy-key": "key", "btn-copy-kit": "kit", "btn-copy-sheet": "sheet"}.get(e.button.id or "")
        if what:
            self.copy_what(what)
        elif e.button.id == "btn-key-file":
            self.action_key()

    def copy_text_out(self, text: str, then=None) -> None:
        """Text in die Zwischenablage; `then(gezeigt)` läuft danach.

        Mit eigener Anzeige (nicht per SSH): wl-copy/xclip/xsel. Sonst die Terminal-Zwischenablage (OSC 52), je nach Terminal:
        nimmt es OSC 52 sicher an, nur das; sicher nicht (GNOME Terminal, Terminal.app), dann den Text zum Markieren zeigen;
        unbekannt, dann beides."""
        from ..core.clipboard import ClipboardError, copy_text, over_ssh
        from .terminal import NO, YES, clipboard_support
        then = then or (lambda shown: None)
        if not over_ssh() and (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
            try:
                copy_text(text)
                then(False)
                return
            except ClipboardError:
                pass
        support = clipboard_support()
        if support != NO:
            self.copy_to_clipboard(text)           # Textual: Terminal-Zwischenablage (OSC 52)
        if support == YES:
            then(False)
            return
        self.show_for_copy(text)
        then(True)

    def show_for_copy(self, text: str) -> None:
        """TUI kurz verlassen und den Text als normale Terminal-Ausgabe zeigen: mit der Maus markieren und kopieren."""
        from textual.app import SuspendNotSupported
        try:
            with self.suspend():
                print("\n" + tr("Text mit der Maus markieren und kopieren (Strg+Umschalt+C, auf dem Mac ⌘C).") + "\n" + "-" * 40)
                print(text)
                print("-" * 40)
                try:
                    input(tr("Eingabetaste drücken, um zurückzukehren …"))
                except EOFError:
                    pass
        except SuspendNotSupported:
            self._notify(tr("Dieses Terminal kann den Text nicht anzeigen; bitte „In Datei speichern“ verwenden."), severity="error")

    def copy_what(self, what: str) -> None:
        """Schlüssel, Notfallblatt oder beides in die Zwischenablage; Schlüssel und Set zählen als gesicherter Export."""
        p = self._need()
        if not p:
            return
        try:
            text = {"key": lambda: self.mgr.key_text(p), "kit": lambda: self.mgr.emergency_kit(p), "sheet": lambda: render_sheet(p)}[what]()
        except Exception as e:
            self._notify(str(e), severity="error")
            return

        def done(shown: bool) -> None:
            if what == "sheet":
                if not shown:
                    self._notify(tr("Notfallblatt kopiert (enthält keine Geheimnisse)"))
                return
            self.mgr.mark_key_exported(p)
            if not shown:
                self._notify(tr("Schlüssel und Notfallblatt kopiert. Jetzt im Passwortmanager einfügen und speichern!") if what == "kit"
                             else tr("Schlüssel kopiert. Jetzt im Passwortmanager einfügen und speichern!"), severity="warning", timeout=12)
            self._activate_schedule(p)
            self.show_overview()

        self.copy_text_out(text, done)

    def action_sheet(self) -> None:
        p = self._need()
        if not p:
            return

        def got(dest):
            if not dest:
                return
            d = Path(dest).expanduser()
            if d.exists():
                self._notify(tr("{0} existiert bereits", d), severity="error")
                return
            try:
                d.write_text(render_sheet(p))
            except OSError as e:
                self._notify(str(e), severity="error", timeout=10)
                return
            self._notify(tr("Notfallblatt gespeichert: {0} (ohne Geheimnisse)", d))
        self.push_screen(PromptScreen(tr("Notfallblatt speichern nach:"), str(Path.home() / f"notfallblatt-{p.slug}.txt"),
                                      tr("Enthält Server, Fingerprint und Anleitung, aber keine Geheimnisse.")), got)

    def action_sources(self) -> None:
        p = self._need()
        if not p:
            return
        if p.backup_mode == FULL:
            self._notify(tr("Beim Vollbackup ist die Quelle fest `/` (Ausschlüsse im Profil anpassen)"), severity="warning")
            return

        def got(text):
            if text is None:
                return
            old, p.sources = p.sources, [s.strip() for s in text.split(",") if s.strip()]
            try:
                self.mgr.save(p)
            except Exception as e:
                p.sources = old
                self._notify(str(e), severity="error", timeout=10)
            self.show_overview()
        self.push_screen(PromptScreen(tr("Quellordner (durch Komma getrennt)"), ", ".join(p.sources)), got)

    def action_mounts(self) -> None:
        p = self._need()
        if not p:
            return
        if p.backup_mode != FULL:
            self._notify(tr("Laufwerke gibt es nur bei einem Vollbackup"), severity="warning")
            return

        def got(sel):
            if sel is None:
                return
            p.full_mounts = sel
            try:
                self.mgr.save(p)
            except Exception as e:
                self._notify(str(e), severity="error", timeout=10)
                return
            self.show_overview()
        self.push_screen(MountsScreen(p.full_mounts), got)

    def _start_first_backup(self, p: Profile) -> None:
        if needs_key_export(p):  # Vollbackup ist bis zum Schlüssel-Export gesperrt: erklären statt scheitern
            self._notify(tr("Das erste Backup startet noch nicht: erst den Schlüssel sichern ([y] kopieren oder [k] Datei), dann [b]."),
                         severity="warning", timeout=15)
            return
        self.action_backup()

    def action_dbdump(self, after=None) -> None:
        p = self._need()
        if not p:
            return

        def closed(saved):
            if saved:
                self.show_overview()
            if after:
                after()
        self.push_screen(DbDumpScreen(self.mgr, p), closed)

    def action_detect(self) -> None:
        p = self._need()
        if not p:
            return

        def got(res):
            if res is None:
                return
            try:
                self.mgr.apply_detection(res)
            except Exception as e:
                self._notify(str(e), severity="error", timeout=10)
                return
            self.show_overview()
            if res.change_detection == METADATA:
                self._notify(tr("Gespeichert. Das nächste Backup liest einmal alles, danach nur noch Geändertes."),
                             timeout=10)
        self.push_screen(DetectScreen(p), got)

    def action_keys(self) -> None:
        by_action = {b.action: b for b in self.BINDINGS}
        self.push_screen(KeysScreen([(title, [(by_action[a].key_display or by_action[a].key, by_action[a].description) for a in actions])
                                     for title, actions in self.KEY_GROUPS]))

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
        self.push_screen(ConfirmScreen(tr("Profil '{0}' samt Zeitplan und Geheimnissen entfernen? (Der Zugang auf dem PBS-Server bleibt bestehen.)", p.name)), got)


def run() -> None:
    from .terminal import probe
    probe()                                        # vor dem Start: danach liest Textual die Tastatur
    PbsTui().run()
