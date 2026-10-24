"""Dialoge der Terminal-Oberfläche: Vorschau, Dateiversionen, Suche in allen Snapshots, Vergleich, Restore-Test."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, RichLog, Static

from ..i18n import tr
from ..cli.insight import diff_lines, restore_test_lines
from ..core.manager import ProfileManager
from ..core.models import Profile, Snapshot
from ..core.versions import Preview
from .screens import PromptScreen


def fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_time(ts: int) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class WorkScreen(ModalScreen[None]):
    """Basis: Hintergrund-Arbeit starten und Ergebnisse sicher in die Oberfläche geben (auch wenn der Dialog schon zu ist)."""
    BINDINGS = [("escape", "close", tr("Schließen"))]
    DEFAULT_CSS = """
    WorkScreen { align: center middle; }
    .box { width: 100; max-width: 95%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
    .grow { height: 1fr; }
    """

    def work(self, fn: Callable[[], object], done: Callable[[object], None]) -> None:
        def run() -> None:
            try:
                res = fn()
            except Exception as e:
                self._later(self.say, tr("Fehler: {0}", e))
                return
            self._later(done, res)
        self.run_worker(run, thread=True)

    def _later(self, fn, *a) -> None:
        try:
            self.app.call_from_thread(fn, *a)
        except Exception:  # nosec B110  (Dialog inzwischen geschlossen oder App beendet)
            return

    def say(self, text: str) -> None:
        self.query_one("#info", Static).update(Text(text))

    def action_close(self) -> None:
        self.dismiss(None)


class TextScreen(WorkScreen):
    """Scrollbarer Text, der nach dem Öffnen im Hintergrund geladen wird."""

    def __init__(self, title: str, loader: Callable[[], list[str]] | None = None, lines: list[str] | None = None):
        super().__init__()
        self._title, self._loader, self._lines = title, loader, lines or []

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(Text(self._title), id="title")
            yield RichLog(id="text", wrap=False, markup=False, classes="grow")
            yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        for line in self._lines:
            self.append(line)
        if self._loader:
            self.say(tr("Lade …"))
            self.work(self._loader, self._loaded)

    def append(self, line: str, style: str = "") -> None:
        self.query_one("#text", RichLog).write(Text(line, style=style))

    def _loaded(self, lines) -> None:
        self.say("")
        for line in lines:
            self.append(line)


def preview_lines(pv: Preview) -> list[str]:
    if pv.kind in ("text", "binary"):
        out = pv.text.split("\n")
        if pv.kind == "binary":
            out = [pv.note, "", *out]
        if pv.truncated:
            out += ["", tr("… gekürzt, die Datei hat {0}", fmt_size(pv.size))]
        return out
    if pv.kind == "image":
        return [tr("Bild, {0}.", fmt_size(pv.size)), tr("Die Bildvorschau gibt es in der grafischen Oberfläche; mit [r] lässt sich die Datei wiederherstellen.")]
    if pv.kind == "link":
        return [tr("Symbolischer Link auf {0}", pv.text)]
    return [pv.note or tr("Keine Vorschau möglich.")]


class PreviewScreen(TextScreen):
    def __init__(self, path: str, fetch: Callable[[], Preview]):
        super().__init__(tr("Vorschau: {0}", path), loader=lambda: preview_lines(fetch()))


class DiffScreen(TextScreen):
    def __init__(self, mgr: ProfileManager, profile: Profile, old: Snapshot, new: Snapshot):
        self.mgr, self.profile, self.old, self.new = mgr, profile, old, new
        super().__init__(tr("Vergleich {0}  →  {1}", fmt_time(old.backup_time), fmt_time(new.backup_time)), loader=self._load)

    def _load(self) -> list[str]:
        out: list[str] = []
        for r in self.mgr.diff(self.profile, self.old, self.new):
            c = r.counts
            out += [tr("{0}: {1} neu, {2} geändert, {3} gelöscht", r.archive, c['added'], c['changed'], c['removed']), *diff_lines(r, 500), ""]
        return out or [tr("Keine Archive im Snapshot.")]

    def append(self, line: str, style: str = "") -> None:
        style = {"+": "green", "-": "red", "~": "yellow"}.get(line[:1], style)
        super().append(line, style)


class RestoreTestScreen(TextScreen):
    def __init__(self, mgr: ProfileManager, profile: Profile, snapshot: Snapshot, count: int = 5):
        self.mgr, self.profile, self.snapshot, self.count = mgr, profile, snapshot, count
        super().__init__(tr("Restore-Test: {0}, {1} zufällige Dateien je Archiv", snapshot.path, count), loader=self._load)
        self.ok = None

    def _load(self) -> list[str]:
        out, ok = [], True
        archives = [a for a in self.snapshot.archives if a != "recovery.pxar"]
        if not archives:
            return [tr("Der Snapshot enthält keine Datei-Archive.")]
        for archive in archives:
            self._later(self.say, tr("Teste {0} …", archive))
            res = self.mgr.run_restore_test(self.profile, self.snapshot, archive, self.count)
            ok &= res.ok
            out += [archive, *restore_test_lines(res), ""]
        self.ok = ok
        return out

    def append(self, line: str, style: str = "") -> None:
        if line.startswith((tr("ABWEICHUNG"), tr("GRÖSSE"), tr("FEHLER"))):
            style = "red"
        elif line.startswith("OK"):
            style = "green"
        super().append(line, style)


class VersionsScreen(WorkScreen):
    """Alle Fassungen einer Datei: Enter holt die gewählte in einen Ordner, p zeigt sie an."""
    BINDINGS = [("escape", "close", tr("Schließen")), ("p", "preview", tr("Vorschau"))]

    def __init__(self, mgr: ProfileManager, profile: Profile, path: str, archive: str | None = None):
        super().__init__()
        self.mgr, self.profile, self.path, self.archive = mgr, profile, path, archive
        self.result = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(Text(tr("Fassungen von {0}", self.path)), id="title")
            yield DataTable(id="versions", cursor_type="row", classes="grow")
            yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#versions", DataTable).add_columns("Nr.", tr("Geändert"), tr("Größe"), "Snapshots", tr("Neuester Snapshot"))
        self.say(tr("Suche in allen Snapshots …"))
        self.work(lambda: self.mgr.file_versions(self.profile, self.path, self.archive), self._loaded)

    def _loaded(self, res) -> None:
        self.result = res
        if not res.versions:
            self.say(tr("{0} kommt in keinem der {1} Snapshots vor.", res.path, res.checked))
            return
        t = self.query_one("#versions", DataTable)
        for i, v in enumerate(res.versions):
            t.add_row(str(i + 1), v.entry.mtime or "-", fmt_size(v.entry.size or 0), str(len(v.snapshots)), fmt_time(v.newest.backup_time))
        self.say(tr("{0} Fassung(en) in {1} Snapshots (gleiche Größe und Zeit = dieselbe Fassung). Enter: in Ordner holen, p: ansehen.", len(res.versions), res.checked))

    def on_data_table_row_selected(self, _e: DataTable.RowSelected) -> None:      # Enter in der Tabelle
        self.action_restore()

    def _current(self):
        t = self.query_one("#versions", DataTable)
        if not self.result or not self.result.versions or t.row_count == 0:
            return None
        return self.result.versions[t.cursor_row]

    def action_preview(self) -> None:
        v = self._current()
        if v is None:
            return
        res = self.result

        def fetch() -> Preview:
            with self.mgr.preview_session(self.profile, v.newest, res.archive) as ps:
                return ps.preview(res.path)
        self.app.push_screen(PreviewScreen(res.path, fetch))

    def action_restore(self) -> None:
        v = self._current()
        if v is None:
            return
        res = self.result
        default = str(Path.home() / "pbsgui-wiederhergestellt")

        def got(target):
            if not target:
                return
            self.say(tr("Stelle wieder her …"))
            self.work(lambda: self.mgr.restore_files(self.profile, v.newest, res.archive, [res.path], target=target),
                      lambda r: self.say(tr("Fertig: {0} Datei(en) nach {1}", r.copied, target)
                                         + (tr(", übersprungen: {0}", ', '.join(r.skipped)) if r.skipped else "")))
        self.app.push_screen(PromptScreen(tr("Fassung vom {0} holen nach:", v.entry.mtime or '?'), default,
                                          tr("Nichts wird überschrieben; vorhandene Dateien werden übersprungen.")), got)


class SearchAllScreen(WorkScreen):
    """Treffer aus allen Snapshots; Enter zeigt die Fassungen der gewählten Datei."""
    BINDINGS = [("escape", "close", tr("Schließen"))]

    def __init__(self, mgr: ProfileManager, profile: Profile, pattern: str):
        super().__init__()
        self.mgr, self.profile, self.pattern = mgr, profile, pattern
        self.result = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(Text(tr("Suche '{0}' in allen Snapshots von {1}", self.pattern, self.profile.name)), id="title")
            yield DataTable(id="hits", cursor_type="row", classes="grow")
            yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hits", DataTable).add_columns(tr("Pfad"), tr("Archiv"), "Snapshots", tr("Fassungen"), tr("Zuletzt gesehen"), tr("Im neuesten"))
        self.say(tr("Suche … (der erste Durchlauf lädt die Dateilisten und dauert länger)"))
        self.work(lambda: self.mgr.search_all(self.profile, self.pattern), self._loaded)

    def _loaded(self, res) -> None:
        self.result = res
        t = self.query_one("#hits", DataTable)
        for h in res.hits:
            t.add_row(h.path + ("/" if h.kind == "d" else ""), h.archive, str(len(h.snapshots)), str(h.versions),
                      fmt_time(h.snapshots[0].backup_time), tr("ja") if h.in_newest else tr("nein (gelöscht)"))
        self.say(tr("{0} Treffer in {1} Snapshots", len(res.hits), res.checked) + (tr(" (Anzeige begrenzt)") if res.truncated else "")
                 + tr(". Enter zeigt alle Fassungen."))

    def on_data_table_row_selected(self, _e: DataTable.RowSelected) -> None:      # Enter in der Tabelle
        self.action_versions()

    def action_versions(self) -> None:
        t = self.query_one("#hits", DataTable)
        if not self.result or not self.result.hits or t.row_count == 0:
            return
        h = self.result.hits[t.cursor_row]
        if h.kind != "d":
            self.app.push_screen(VersionsScreen(self.mgr, self.profile, h.path, h.archive))
