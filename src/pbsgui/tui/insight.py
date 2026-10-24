"""Dialoge der Terminal-Oberfläche: Vorschau, Dateiversionen, Suche in allen Snapshots, Vergleich, Restore-Test."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, RichLog, Static

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
    BINDINGS = [("escape", "close", "Schließen")]
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
                self._later(self.say, f"Fehler: {e}")
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
            self.say("Lade …")
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
            out += ["", f"… gekürzt, die Datei hat {fmt_size(pv.size)}"]
        return out
    if pv.kind == "image":
        return [f"Bild, {fmt_size(pv.size)}.", "Die Bildvorschau gibt es in der grafischen Oberfläche; mit [r] lässt sich die Datei wiederherstellen."]
    if pv.kind == "link":
        return [f"Symbolischer Link auf {pv.text}"]
    return [pv.note or "Keine Vorschau möglich."]


class PreviewScreen(TextScreen):
    def __init__(self, path: str, fetch: Callable[[], Preview]):
        super().__init__(f"Vorschau: {path}", loader=lambda: preview_lines(fetch()))


class DiffScreen(TextScreen):
    def __init__(self, mgr: ProfileManager, profile: Profile, old: Snapshot, new: Snapshot):
        self.mgr, self.profile, self.old, self.new = mgr, profile, old, new
        super().__init__(f"Vergleich {fmt_time(old.backup_time)}  →  {fmt_time(new.backup_time)}", loader=self._load)

    def _load(self) -> list[str]:
        out: list[str] = []
        for r in self.mgr.diff(self.profile, self.old, self.new):
            c = r.counts
            out += [f"{r.archive}: {c['added']} neu, {c['changed']} geändert, {c['removed']} gelöscht", *diff_lines(r, 500), ""]
        return out or ["Keine Archive im Snapshot."]

    def append(self, line: str, style: str = "") -> None:
        style = {"+": "green", "-": "red", "~": "yellow"}.get(line[:1], style)
        super().append(line, style)


class RestoreTestScreen(TextScreen):
    def __init__(self, mgr: ProfileManager, profile: Profile, snapshot: Snapshot, count: int = 5):
        self.mgr, self.profile, self.snapshot, self.count = mgr, profile, snapshot, count
        super().__init__(f"Restore-Test: {snapshot.path}, {count} zufällige Dateien je Archiv", loader=self._load)
        self.ok = None

    def _load(self) -> list[str]:
        out, ok = [], True
        archives = [a for a in self.snapshot.archives if a != "recovery.pxar"]
        if not archives:
            return ["Der Snapshot enthält keine Datei-Archive."]
        for archive in archives:
            self._later(self.say, f"Teste {archive} …")
            res = self.mgr.run_restore_test(self.profile, self.snapshot, archive, self.count)
            ok &= res.ok
            out += [archive, *restore_test_lines(res), ""]
        self.ok = ok
        return out

    def append(self, line: str, style: str = "") -> None:
        if line.startswith(("ABWEICHUNG", "GRÖSSE", "FEHLER")):
            style = "red"
        elif line.startswith("OK"):
            style = "green"
        super().append(line, style)


class VersionsScreen(WorkScreen):
    """Alle Fassungen einer Datei: Enter holt die gewählte in einen Ordner, p zeigt sie an."""
    BINDINGS = [("escape", "close", "Schließen"), ("p", "preview", "Vorschau")]

    def __init__(self, mgr: ProfileManager, profile: Profile, path: str, archive: str | None = None):
        super().__init__()
        self.mgr, self.profile, self.path, self.archive = mgr, profile, path, archive
        self.result = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(Text(f"Fassungen von {self.path}"), id="title")
            yield DataTable(id="versions", cursor_type="row", classes="grow")
            yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#versions", DataTable).add_columns("Nr.", "Geändert", "Größe", "Snapshots", "Neuester Snapshot")
        self.say("Suche in allen Snapshots …")
        self.work(lambda: self.mgr.file_versions(self.profile, self.path, self.archive), self._loaded)

    def _loaded(self, res) -> None:
        self.result = res
        if not res.versions:
            self.say(f"{res.path} kommt in keinem der {res.checked} Snapshots vor.")
            return
        t = self.query_one("#versions", DataTable)
        for i, v in enumerate(res.versions):
            t.add_row(str(i + 1), v.entry.mtime or "-", fmt_size(v.entry.size or 0), str(len(v.snapshots)), fmt_time(v.newest.backup_time))
        self.say(f"{len(res.versions)} Fassung(en) in {res.checked} Snapshots (gleiche Größe und Zeit = dieselbe Fassung). "
                 "Enter: in Ordner holen, p: ansehen.")

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
            self.say("Stelle wieder her …")
            self.work(lambda: self.mgr.restore_files(self.profile, v.newest, res.archive, [res.path], target=target),
                      lambda r: self.say(f"Fertig: {r.copied} Datei(en) nach {target}"
                                         + (f", übersprungen: {', '.join(r.skipped)}" if r.skipped else "")))
        self.app.push_screen(PromptScreen(f"Fassung vom {v.entry.mtime or '?'} holen nach:", default,
                                          "Nichts wird überschrieben; vorhandene Dateien werden übersprungen."), got)


class SearchAllScreen(WorkScreen):
    """Treffer aus allen Snapshots; Enter zeigt die Fassungen der gewählten Datei."""
    BINDINGS = [("escape", "close", "Schließen")]

    def __init__(self, mgr: ProfileManager, profile: Profile, pattern: str):
        super().__init__()
        self.mgr, self.profile, self.pattern = mgr, profile, pattern
        self.result = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(Text(f"Suche '{self.pattern}' in allen Snapshots von {self.profile.name}"), id="title")
            yield DataTable(id="hits", cursor_type="row", classes="grow")
            yield Static("", id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hits", DataTable).add_columns("Pfad", "Archiv", "Snapshots", "Fassungen", "Zuletzt gesehen", "Im neuesten")
        self.say("Suche … (der erste Durchlauf lädt die Dateilisten und dauert länger)")
        self.work(lambda: self.mgr.search_all(self.profile, self.pattern), self._loaded)

    def _loaded(self, res) -> None:
        self.result = res
        t = self.query_one("#hits", DataTable)
        for h in res.hits:
            t.add_row(h.path + ("/" if h.kind == "d" else ""), h.archive, str(len(h.snapshots)), str(h.versions),
                      fmt_time(h.snapshots[0].backup_time), "ja" if h.in_newest else "nein (gelöscht)")
        self.say(f"{len(res.hits)} Treffer in {res.checked} Snapshots" + (" (Anzeige begrenzt)" if res.truncated else "")
                 + ". Enter zeigt alle Fassungen.")

    def on_data_table_row_selected(self, _e: DataTable.RowSelected) -> None:      # Enter in der Tabelle
        self.action_versions()

    def action_versions(self) -> None:
        t = self.query_one("#hits", DataTable)
        if not self.result or not self.result.hits or t.row_count == 0:
            return
        h = self.result.hits[t.cursor_row]
        if h.kind != "d":
            self.app.push_screen(VersionsScreen(self.mgr, self.profile, h.path, h.archive))
