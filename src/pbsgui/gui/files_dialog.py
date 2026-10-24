"""Dateibrowser für einen Snapshot: Dateien/Ordner wählen und in einen Ordner oder (nur Home) an den Originalort holen."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..core.files import Catalog, CatalogEntry, FilesError, map_to_original
from ..core.manager import ProfileManager
from ..core.models import Profile, Snapshot
from ..core.versions import PreviewSession
from .insight import PreviewPane, Runner, SearchAllDialog, VersionsDialog
from .workers import Worker


def fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class FilesDialog(QDialog):
    def __init__(self, mgr: ProfileManager, profile: Profile, snapshot: Snapshot, parent=None):
        super().__init__(parent)
        self.mgr, self.profile, self.snapshot = mgr, profile, snapshot
        self.cats: dict[str, Catalog] = {}
        self._worker: Worker | None = None
        self.setWindowTitle(f"Dateien in {snapshot.path}")
        self.resize(1100, 640)
        self._sessions: dict[str, PreviewSession] = {}
        self._closed = False

        self.archive = QComboBox()
        self.search = QLineEdit(); self.search.setPlaceholderText("Suchen (Namen, * und ? als Platzhalter), Enter")
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["Name", "Größe", "Datum"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 420)
        self.info = QLabel("Lade Dateiliste …"); self.info.setWordWrap(True)
        self.btn_folder = QPushButton("In Ordner wiederherstellen …")
        self.btn_over = QPushButton("Am Originalort überschreiben (nur Home) …")
        self.btn_versions = QPushButton("Fassungen …"); self.btn_versions.setToolTip("Alle Fassungen dieser Datei aus allen Snapshots")
        self.btn_all = QPushButton("In allen Snapshots suchen …")
        self.preview = PreviewPane()
        close = QPushButton("Schließen")
        top = QHBoxLayout(); top.addWidget(QLabel("Archiv:")); top.addWidget(self.archive, 1); top.addWidget(self.search, 2)
        bot = QHBoxLayout()
        for w in (self.btn_folder, self.btn_over, self.btn_versions, self.btn_all):
            bot.addWidget(w)
        bot.addStretch(); bot.addWidget(close)
        split = QSplitter(); split.addWidget(self.tree); split.addWidget(self.preview)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2); split.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        lay.addLayout(top); lay.addWidget(split, 1); lay.addWidget(self.info); lay.addLayout(bot)
        self._previews = Runner(self, lambda m: self.preview.clear(f"Vorschau nicht möglich: {m}"))

        self.archive.currentTextChanged.connect(self._show_root)
        self.search.returnPressed.connect(self._do_search)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.btn_folder.clicked.connect(self._restore_folder)
        self.btn_over.clicked.connect(self._restore_original)
        self.btn_versions.clicked.connect(self._versions)
        self.btn_all.clicked.connect(lambda: SearchAllDialog(self.mgr, self.profile, self.search.text().strip(), self).exec())
        self.tree.itemSelectionChanged.connect(self._preview_selected)
        self.finished.connect(lambda _r: self._close_sessions(final=True))
        close.clicked.connect(self.accept)
        self.btn_folder.setEnabled(False); self.btn_over.setEnabled(False); self.btn_versions.setEnabled(False)
        self._run(lambda _l: self.mgr.catalogs(self.profile, self.snapshot), self._loaded)

    # -- Hilfen -------------------------------------------------------------
    def _run(self, fn, done) -> None:
        self._worker = Worker(fn, self)
        self._worker.done.connect(done)
        self._worker.failed.connect(lambda m: (self.info.setText(f"Fehler: {m}"), QMessageBox.warning(self, "Fehler", m)))
        self._worker.start()

    def _cat(self) -> Catalog | None:
        return self.cats.get(self.archive.currentText())

    def _item(self, e: CatalogEntry, label: str | None = None) -> QTreeWidgetItem:
        it = QTreeWidgetItem([label or e.name, fmt_size(e.size) if e.size is not None else "", e.mtime[:10]])
        it.setData(0, Qt.ItemDataRole.UserRole, e.path)
        it.setData(0, Qt.ItemDataRole.UserRole + 1, e.kind)
        if e.kind == "d":
            it.addChild(QTreeWidgetItem(["…"]))      # Platzhalter, damit der Ordner aufklappbar ist
        return it

    def _loaded(self, cats) -> None:
        self.cats = {k: v for k, v in cats.items() if k != "recovery.pxar"}
        if not self.cats:
            self.info.setText("Dieser Snapshot enthält keine Datei-Archive.")
            return
        self.archive.blockSignals(True)
        self.archive.addItems(sorted(self.cats))
        self.archive.blockSignals(False)
        self._show_root()

    def _show_root(self, *_a) -> None:
        self._close_sessions()
        self.preview.clear()
        cat = self._cat()
        self.tree.clear()
        if cat:
            self.tree.addTopLevelItems([self._item(e) for e in cat.children("/")])
        self.info.setText("Dateien oder Ordner wählen (Strg/Umschalt für mehrere).")

    def _expand(self, item: QTreeWidgetItem) -> None:
        cat = self._cat()
        if cat and item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChild(0)
            item.addChildren([self._item(e) for e in cat.children(item.data(0, Qt.ItemDataRole.UserRole))])

    def _do_search(self) -> None:
        cat, text = self._cat(), self.search.text().strip()
        if not cat or not text:
            self._show_root()
            return
        self.tree.clear()
        found = cat.search(text)
        self.tree.addTopLevelItems([self._item(e, e.path) for e in found])
        self.info.setText(f"{len(found)} Treffer" + (" (Anzeige begrenzt)" if len(found) >= 500 else ""))

    def _close_sessions(self, final: bool = False) -> None:
        self._closed = self._closed or final
        sessions, self._sessions = list(self._sessions.values()), {}
        for s in sessions:
            s.close()

    def _single_file(self) -> tuple[str, str] | None:
        items = self.tree.selectedItems()
        if len(items) != 1:
            return None
        path, kind = items[0].data(0, Qt.ItemDataRole.UserRole), items[0].data(0, Qt.ItemDataRole.UserRole + 1)
        return (path, kind) if path and kind else None

    def _preview_selected(self) -> None:
        one = self._single_file()
        self.btn_versions.setEnabled(bool(one and one[1] != "d"))
        if one is None:
            self.preview.clear("Eine einzelne Datei wählen, um den Inhalt zu sehen.")
            return
        path, kind = one
        if kind == "d":
            self.preview.clear("Ordner")
            return
        archive = self.archive.currentText()
        self.preview.clear("Lade Vorschau …")

        def work(_l):
            ps = self._sessions.get(archive)
            if self._closed:
                raise RuntimeError("Dialog geschlossen")
            if ps is None:
                ps = self._sessions[archive] = self.mgr.preview_session(self.profile, self.snapshot, archive)
            return ps.preview(path)
        self._previews.run(work, self.preview.show_preview)

    def _versions(self) -> None:
        one = self._single_file()
        if one and one[1] != "d":
            VersionsDialog(self.mgr, self.profile, one[0], self.archive.currentText(), self).exec()

    def selected_paths(self) -> list[str]:
        paths = [i.data(0, Qt.ItemDataRole.UserRole) for i in self.tree.selectedItems()]
        # Ordner enthalten ihre Unterelemente schon: Auswahl darunter wäre doppelt
        dirs = [p for p in paths if (self._cat() and self._cat().by_path.get(p) and self._cat().by_path[p].kind == "d")]
        return [p for p in paths if not any(p != d and p.startswith(d.rstrip("/") + "/") for d in dirs)]

    def _selection_changed(self) -> None:
        paths, cat = self.selected_paths(), self._cat()
        self.btn_folder.setEnabled(bool(paths))
        ok_home, why = False, "Wähle Dateien oder Ordner aus."
        if paths and cat:
            files, size = cat.count(paths)
            self.info.setText(f"{len(paths)} Auswahl, {files} Dateien, {fmt_size(size)}")
            try:
                items = map_to_original(self.profile, self.archive.currentText(), paths)
                ok_home = all(i.in_home for i in items)
                why = "" if ok_home else "Überschreiben ist nur für Dateien im Home-Verzeichnis erlaubt."
            except FilesError as e:
                why = str(e)
        self.btn_over.setEnabled(ok_home)
        self.btn_over.setToolTip(why)

    # -- Aktionen -----------------------------------------------------------
    def _result_text(self, res) -> str:
        text = f"{res.copied} Dateien wiederhergestellt ({fmt_size(res.bytes)})."
        if res.safety_dir and res.safety_copies:
            text += f"\n{res.safety_copies} überschriebene Dateien vorher gesichert in:\n{res.safety_dir}"
        if res.skipped:
            text += f"\n\nÜbersprungen ({len(res.skipped)}):\n" + "\n".join(res.skipped[:15])
        return text

    def _restore_folder(self) -> None:
        paths = self.selected_paths()
        target = QFileDialog.getExistingDirectory(self, "Zielordner (nichts wird überschrieben)", str(Path.home()))
        if not paths or not target:
            return
        self.info.setText("Stelle wieder her …")
        arch = self.archive.currentText()
        self._run(lambda _l: self.mgr.restore_files(self.profile, self.snapshot, arch, paths, target=target),
                  lambda res: (self.info.setText("Fertig."), QMessageBox.information(self, "Wiederhergestellt",
                                                                                      self._result_text(res) + f"\n\nZiel: {target}")))

    def _restore_original(self) -> None:
        paths, arch = self.selected_paths(), self.archive.currentText()
        try:
            items = map_to_original(self.profile, arch, paths)
        except FilesError as e:
            QMessageBox.warning(self, "Nicht möglich", str(e))
            return
        if not all(i.in_home for i in items):
            QMessageBox.warning(self, "Nicht möglich", "Überschreiben ist nur im Home-Verzeichnis erlaubt.")
            return
        existing = sum(1 for i in items if i.exists)
        if QMessageBox.question(
                self, "Am Originalort überschreiben",
                f"{len(items)} Pfad(e) werden an ihren ursprünglichen Ort im Home-Verzeichnis zurückgeschrieben, "
                f"{existing} davon existieren schon.\n\nVorhandene Dateien werden durch die Fassung aus dem Backup "
                "ersetzt. Die alten Fassungen werden vorher nach ~/.local/share/pbsgui/overwritten/<Zeit>/ gesichert; "
                "nichts wird gelöscht.\n\nFortfahren?") != QMessageBox.StandardButton.Yes:
            return
        self.info.setText("Schreibe zurück …")
        self._run(lambda _l: self.mgr.restore_files(self.profile, self.snapshot, arch, paths, overwrite_home=True),
                  lambda res: (self.info.setText("Fertig."), QMessageBox.information(self, "Wiederhergestellt", self._result_text(res))))
