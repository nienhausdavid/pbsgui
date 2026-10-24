"""Dateibrowser (Textual) für einen Snapshot: markieren, in Ordner wiederherstellen oder (nur Home) überschreiben."""
from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, Tree

from ..core.files import Catalog, FilesError, map_to_original
from ..core.manager import ProfileManager
from ..core.models import Profile, Snapshot
from .screens import ConfirmScreen, PromptScreen


def fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class FilesScreen(ModalScreen[None]):
    # Leertaste mit Vorrang: der Baum belegt sie sonst selbst (Auf-/Zuklappen). "m" geht ebenfalls.
    BINDINGS = [("escape", "close", "Schließen"), Binding("space", "toggle", "Markieren", priority=True),
                Binding("m", "toggle", "Markieren", show=False), ("r", "to_folder", "In Ordner"),
                ("o", "to_original", "Überschreiben (nur Home)"), ("slash", "search", "Suchen")]
    DEFAULT_CSS = """
    FilesScreen { align: center middle; }
    #files-box { width: 100; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
    #files-tree { height: 1fr; }
    """

    def __init__(self, mgr: ProfileManager, profile: Profile, snapshot: Snapshot):
        super().__init__()
        self.mgr, self.profile, self.snapshot = mgr, profile, snapshot
        self.cats: dict[str, Catalog] = {}
        self.marks: set[tuple[str, str]] = set()   # (Archiv, Pfad)

    def compose(self) -> ComposeResult:
        with Vertical(id="files-box"):
            yield Static(f"Dateien in {self.snapshot.path}", markup=False)
            yield Tree("Archive", id="files-tree")
            yield Static("Lade Dateiliste …", id="files-info", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        # erst nach dem Aufbau der Kind-Widgets (sonst manchmal NoMatches beim Einblenden des Screens)
        self.call_after_refresh(self._start)

    def _start(self) -> None:
        self.query_one("#files-tree", Tree).root.expand()
        self.run_worker(self._load, thread=True)

    # -- Laden / Baum ------------------------------------------------------------
    def _load(self) -> None:
        try:
            cats = self.mgr.catalogs(self.profile, self.snapshot)
        except Exception as e:
            self.app.call_from_thread(self._say, f"Fehler: {e}")
            return
        self.app.call_from_thread(self._loaded, {k: v for k, v in cats.items() if k != "recovery.pxar"})

    def _loaded(self, cats: dict[str, Catalog]) -> None:
        self.cats = cats
        tree = self.query_one("#files-tree", Tree)
        for name in sorted(cats):
            tree.root.add(Text(name), data=(name, "/"), allow_expand=True)
        self._say("Pfeile: bewegen, Enter: auf-/zuklappen, Leertaste: markieren" if cats
                  else "Dieser Snapshot enthält keine Datei-Archive.")

    def on_tree_node_expanded(self, e: Tree.NodeExpanded) -> None:
        node = e.node
        if node.data is None or node.children:
            return
        archive, path = node.data
        for entry in self.cats[archive].children(path):
            if entry.kind == "d":
                node.add(self._label(archive, entry.path, entry.name + "/"), data=(archive, entry.path), allow_expand=True)
            else:
                extra = f"  ({fmt_size(entry.size)})" if entry.size is not None else ""
                node.add_leaf(self._label(archive, entry.path, entry.name + extra), data=(archive, entry.path))

    def _label(self, archive: str, path: str, text: str) -> Text:
        return Text(("[x] " if (archive, path) in self.marks else "[ ] ") + text)

    def _say(self, text: str) -> None:
        self.query_one("#files-info", Static).update(text)

    # -- Auswahl ------------------------------------------------------------------
    def selected(self) -> tuple[str, list[str]] | None:
        if not self.marks:
            return None
        archive = next(iter(self.marks))[0]
        paths = sorted(p for a, p in self.marks if a == archive)
        cat = self.cats[archive]
        dirs = [p for p in paths if cat.by_path.get(p) and cat.by_path[p].kind == "d"]
        return archive, [p for p in paths if not any(p != d and p.startswith(d.rstrip("/") + "/") for d in dirs)]

    def action_toggle(self) -> None:
        node = self.query_one("#files-tree", Tree).cursor_node
        if not node or node.data is None or node.data[1] == "/":
            return
        key = tuple(node.data)
        if self.marks and next(iter(self.marks))[0] != key[0]:
            self.app.notify("Auswahl nur aus einem Archiv gleichzeitig", severity="warning")
            return
        self.marks.symmetric_difference_update({key})
        base = str(node.label)[4:]                        # Beschriftung ohne "[x] " / "[ ] "
        node.set_label(self._label(key[0], key[1], base))
        sel = self.selected()
        if sel:
            files, size = self.cats[sel[0]].count(sel[1])
            self._say(f"{len(sel[1])} Auswahl, {files} Dateien, {fmt_size(size)}")
        else:
            self._say("Nichts markiert.")

    def action_search(self) -> None:
        def got(text):
            if not text or not self.cats:
                return
            tree = self.query_one("#files-tree", Tree)
            for name, cat in self.cats.items():
                hits = cat.search(text)
                if hits:
                    node = tree.root.add(Text(f"Suche '{text}' in {name}: {len(hits)} Treffer"), allow_expand=True)
                    for h in hits[:200]:
                        node.add_leaf(self._label(name, h.path, h.path), data=(name, h.path))
                    node.expand()
        self.app.push_screen(PromptScreen("Dateien suchen", "", "Name oder Muster mit * und ?"), got)

    # -- Aktionen -----------------------------------------------------------------
    def _done(self, res, extra: str = "") -> None:
        text = f"{res.copied} Dateien wiederhergestellt ({fmt_size(res.bytes)}). {extra}"
        if res.safety_dir and res.safety_copies:
            text += f" {res.safety_copies} überschriebene Dateien gesichert in {res.safety_dir}."
        if res.skipped:
            text += f" Übersprungen: {len(res.skipped)}."
        self._say(text)
        self.app.notify(escape(text), timeout=12)

    def _fail(self, msg: str) -> None:
        self._say(f"Fehler: {msg}")
        self.app.notify(escape(msg), severity="error", timeout=12)

    def _restore(self, **kw) -> None:
        sel = self.selected()
        if not sel:
            return
        archive, paths = sel
        self._say("Stelle wieder her …")

        def work():
            try:
                res = self.mgr.restore_files(self.profile, self.snapshot, archive, paths, **kw)
            except Exception as e:
                self.app.call_from_thread(self._fail, str(e))
                return
            self.app.call_from_thread(self._done, res, f"Ziel: {kw['target']}." if kw.get("target") else "")

        self.run_worker(work, thread=True)

    def action_to_folder(self) -> None:
        if not self.selected():
            self.app.notify("Erst mit der Leertaste Dateien markieren", severity="warning")
            return
        default = str(Path.home() / "pbsgui-wiederhergestellt")
        self.app.push_screen(PromptScreen("Wiederherstellen in Ordner", default,
                                      "Nichts wird überschrieben; vorhandene Dateien werden übersprungen."),
                         lambda t: t and self._restore(target=t))

    def action_to_original(self) -> None:
        sel = self.selected()
        if not sel:
            self.app.notify("Erst mit der Leertaste Dateien markieren", severity="warning")
            return
        archive, paths = sel
        try:
            items = map_to_original(self.profile, archive, paths)
        except FilesError as e:
            self.app.notify(escape(str(e)), severity="error")
            return
        if not all(i.in_home for i in items):
            self.app.notify("Überschreiben ist nur im Home-Verzeichnis erlaubt", severity="error")
            return
        existing = sum(1 for i in items if i.exists)
        self.app.push_screen(ConfirmScreen(
            f"{len(items)} Pfad(e) im Home-Verzeichnis am Originalort überschreiben ({existing} existieren schon)? "
            "Alte Fassungen werden vorher nach ~/.local/share/pbsgui/overwritten/<Zeit>/ gesichert."),
            lambda yes: yes and self._restore(overwrite_home=True))

    def action_close(self) -> None:
        self.dismiss(None)
