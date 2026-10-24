"""Dashboard, Verlauf, Vorschau, Dateiversionen, Suche in allen Snapshots, Vergleich und Restore-Test (Qt)."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..cli.insight import OVERVIEW_HEADER, overview_details, overview_row, restore_test_lines, usage_text
from ..core.history import RunRecord, fmt_duration
from ..core.manager import ProfileManager
from ..core.models import Profile, Snapshot
from ..core.versions import Preview
from . import style
from .workers import Worker


def fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def theme_color(name: str) -> QColor:
    from PySide6.QtWidgets import QApplication
    theme = QApplication.instance().property("pbsgui_theme") or "light"
    return QColor(style.THEMES[theme][name])


def make_table(headers: list[str], stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    hh = t.horizontalHeader()
    hh.setStretchLastSection(stretch_last)
    for i in range(len(headers) - (1 if stretch_last else 0)):
        hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    return t


def fill_table(t: QTableWidget, rows: list[list[str]]) -> None:
    t.setRowCount(len(rows))
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            t.setItem(i, j, QTableWidgetItem(v))


class Runner:
    """Führt blockierende Aufrufe in einem Worker aus; veraltete Ergebnisse (neuere Anfrage läuft) werden verworfen."""

    def __init__(self, owner: QWidget, on_error):
        self.owner, self.on_error, self._serial = owner, on_error, 0

    def run(self, fn, done) -> Worker:
        self._serial += 1
        mine = self._serial
        w = Worker(fn, self.owner)
        w.done.connect(lambda r: done(r) if mine == self._serial else None)
        w.failed.connect(lambda m: self.on_error(m) if mine == self._serial else None)
        w.start()
        return w


# -- Vorschau --------------------------------------------------------------------------------------------
class PreviewPane(QWidget):
    """Zeigt Text, Bild, Hexdump oder einen Hinweis zu einer Datei im Snapshot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.info = QLabel("Datei wählen, um den Inhalt zu sehen."); self.info.setObjectName("muted"); self.info.setWordWrap(True)
        self.text = QPlainTextEdit(); self.text.setReadOnly(True)
        mono = QFont("monospace"); mono.setStyleHint(QFont.StyleHint.Monospace); self.text.setFont(mono)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.image = QLabel(); self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget(); self.stack.addWidget(self.text); self.stack.addWidget(self.image)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.info); lay.addWidget(self.stack, 1)
        self._pixmap: QPixmap | None = None

    def clear(self, message: str = "Datei wählen, um den Inhalt zu sehen.") -> None:
        self.info.setText(message); self.text.clear(); self._pixmap = None; self.image.clear()
        self.stack.setCurrentWidget(self.text)

    def show_preview(self, pv: Preview) -> None:
        self._pixmap = None
        if pv.kind == "image":
            pm = QPixmap()
            if pm.loadFromData(pv.data):
                self._pixmap = pm
                self.info.setText(f"Bild {pm.width()}×{pm.height()}, {fmt_size(pv.size)}")
                self.stack.setCurrentWidget(self.image)
                self._fit()
                return
            pv = Preview("binary", note="Bild nicht lesbar", size=pv.size)
        self.stack.setCurrentWidget(self.text)
        if pv.kind in ("text", "binary"):
            self.text.setPlainText(pv.text)
            more = f" – gekürzt, Datei hat {fmt_size(pv.size)}" if pv.truncated else ""
            self.info.setText((pv.note or f"Text, {fmt_size(pv.size)}") + (more if pv.kind == "text" else ""))
        elif pv.kind == "link":
            self.text.setPlainText(pv.text); self.info.setText("Symbolischer Link auf:")
        else:
            self.text.clear(); self.info.setText(pv.note or "Keine Vorschau möglich.")

    def _fit(self) -> None:
        if self._pixmap is not None:
            self.image.setPixmap(self._pixmap.scaled(self.stack.size() - QSize(8, 8), Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fit()


# -- Dateiversionen --------------------------------------------------------------------------------------
class VersionsDialog(QDialog):
    def __init__(self, mgr: ProfileManager, profile: Profile, path: str, archive: str | None = None, parent=None):
        super().__init__(parent)
        self.mgr, self.profile, self.path, self.archive = mgr, profile, path, archive
        self.result = None
        self.setWindowTitle(f"Fassungen von {path}")
        self.resize(900, 560)
        self.info = QLabel("Suche in allen Snapshots …"); self.info.setWordWrap(True)
        self.bar = QProgressBar(); self.bar.setRange(0, 0)
        self.table = make_table(["Nr.", "Geändert", "Größe", "Snapshots", "Neuester Snapshot"])
        self.preview = PreviewPane()
        self.btn_restore = QPushButton("Diese Fassung wiederherstellen …"); self.btn_restore.setEnabled(False)
        close = QPushButton("Schließen")
        split = QSplitter(Qt.Orientation.Vertical); split.addWidget(self.table); split.addWidget(self.preview)
        split.setSizes([260, 240])
        bot = QHBoxLayout(); bot.addWidget(self.btn_restore); bot.addStretch(); bot.addWidget(close)
        lay = QVBoxLayout(self); lay.addWidget(self.info); lay.addWidget(self.bar); lay.addWidget(split, 1); lay.addLayout(bot)
        self.runner = Runner(self, lambda m: (self.info.setText(f"Fehler: {m}"), self.bar.setRange(0, 1)))
        self.prev_runner = Runner(self, lambda m: self.preview.clear(f"Vorschau nicht möglich: {m}"))
        close.clicked.connect(self.accept)
        self.btn_restore.clicked.connect(self._restore)
        self.table.itemSelectionChanged.connect(self._selected)
        self.runner.run(lambda _l: mgr.file_versions(profile, path, archive), self._loaded)

    def _loaded(self, res) -> None:
        self.result = res
        self.bar.setRange(0, 1); self.bar.setValue(1)
        if not res.versions:
            self.info.setText(f"{res.path} kommt in keinem der {res.checked} Snapshots vor.")
            return
        self.info.setText(f"{res.path} in {res.archive}: {len(res.versions)} Fassung(en) in {res.checked} Snapshots. "
                          "Gleiche Größe und Änderungszeit gelten als dieselbe Fassung."
                          + (f" {len(res.errors)} Snapshot(s) nicht lesbar." if res.errors else ""))
        fill_table(self.table, [[str(i + 1), v.entry.mtime or "-", fmt_size(v.entry.size or 0), str(len(v.snapshots)),
                                 fmt_time(v.newest.backup_time)] for i, v in enumerate(res.versions)])
        self.table.selectRow(0)

    def _current(self):
        rows = self.table.selectionModel().selectedRows()
        return self.result.versions[rows[0].row()] if rows and self.result else None

    def _selected(self) -> None:
        v = self._current()
        self.btn_restore.setEnabled(v is not None)
        if v is None:
            return
        self.preview.clear("Lade Vorschau …")

        def work(_l):
            with self.mgr.preview_session(self.profile, v.newest, self.result.archive) as ps:
                return ps.preview(self.result.path)
        self.prev_runner.run(work, self.preview.show_preview)

    def _restore(self) -> None:
        v = self._current()
        if v is None or not self.result:
            return
        target = QFileDialog.getExistingDirectory(self, "Zielordner (nichts wird überschrieben)", str(Path.home()))
        if not target:
            return
        arch, path = self.result.archive, self.result.path
        self.info.setText("Stelle wieder her …")
        self.runner.run(lambda _l: self.mgr.restore_files(self.profile, v.newest, arch, [path], target=target),
                        lambda r: (self.info.setText(f"Fertig: {r.copied} Datei(en) nach {target}"),
                                   QMessageBox.information(self, "Wiederhergestellt", f"{r.copied} Datei(en) nach\n{target}"
                                                           + ("\n\nÜbersprungen: " + ", ".join(r.skipped) if r.skipped else ""))))


# -- Suche in allen Snapshots ---------------------------------------------------------------------------------
class SearchAllDialog(QDialog):
    def __init__(self, mgr: ProfileManager, profile: Profile, pattern: str = "", parent=None):
        super().__init__(parent)
        self.mgr, self.profile = mgr, profile
        self.result = None
        self.setWindowTitle(f"Suche in allen Snapshots – {profile.name}")
        self.resize(900, 520)
        self.edit = QLineEdit(pattern); self.edit.setPlaceholderText("Name oder Muster (* und ?), Enter")
        go = QPushButton("Suchen"); go.setObjectName("primary")
        self.info = QLabel("Sucht in allen Snapshots, auch nach gelöschten Dateien. Der erste Durchlauf lädt die Dateilisten und dauert länger.")
        self.info.setWordWrap(True)
        self.bar = QProgressBar(); self.bar.setRange(0, 1); self.bar.setValue(1)
        self.table = make_table(["Pfad", "Archiv", "Snapshots", "Fassungen", "Zuletzt gesehen", "Im neuesten"], stretch_last=False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.btn_versions = QPushButton("Fassungen …"); self.btn_versions.setEnabled(False)
        close = QPushButton("Schließen")
        top = QHBoxLayout(); top.addWidget(self.edit, 1); top.addWidget(go)
        bot = QHBoxLayout(); bot.addWidget(self.btn_versions); bot.addStretch(); bot.addWidget(close)
        lay = QVBoxLayout(self); lay.addLayout(top); lay.addWidget(self.info); lay.addWidget(self.bar); lay.addWidget(self.table, 1); lay.addLayout(bot)
        self.runner = Runner(self, lambda m: (self.info.setText(f"Fehler: {m}"), self.bar.setRange(0, 1)))
        go.clicked.connect(self.search); self.edit.returnPressed.connect(self.search)
        close.clicked.connect(self.accept)
        self.btn_versions.clicked.connect(self._versions)
        self.table.itemSelectionChanged.connect(lambda: self.btn_versions.setEnabled(self._hit() is not None))
        self.table.itemDoubleClicked.connect(lambda _i: self._versions())
        if pattern:
            QTimer.singleShot(0, self.search)

    def search(self) -> None:
        text = self.edit.text().strip()
        if not text:
            return
        self.info.setText("Suche …"); self.bar.setRange(0, 0)
        self.table.setRowCount(0)
        self.runner.run(lambda _l: self.mgr.search_all(self.profile, text), self._done)

    def _done(self, res) -> None:
        self.result = res
        self.bar.setRange(0, 1); self.bar.setValue(1)
        self.info.setText(f"{len(res.hits)} Treffer in {res.checked} Snapshots" + (" (Anzeige begrenzt)" if res.truncated else "")
                          + (f" – {len(res.errors)} Snapshot(s) nicht lesbar" if res.errors else ""))
        fill_table(self.table, [[h.path + ("/" if h.kind == "d" else ""), h.archive, str(len(h.snapshots)), str(h.versions),
                                 fmt_time(h.snapshots[0].backup_time), "ja" if h.in_newest else "nein (gelöscht)"] for h in res.hits])

    def _hit(self):
        rows = self.table.selectionModel().selectedRows()
        return self.result.hits[rows[0].row()] if rows and self.result else None

    def _versions(self) -> None:
        h = self._hit()
        if h is not None and h.kind != "d":
            VersionsDialog(self.mgr, self.profile, h.path, h.archive, self).exec()


# -- Vergleich zweier Snapshots ------------------------------------------------------------------------------------
class DiffDialog(QDialog):
    def __init__(self, mgr: ProfileManager, profile: Profile, snapshots: list[Snapshot], new_index: int, parent=None):
        super().__init__(parent)
        self.mgr, self.profile, self.snaps = mgr, profile, snapshots
        self.setWindowTitle(f"Snapshots vergleichen – {profile.name}")
        self.resize(900, 560)
        labels = [fmt_time(s.backup_time) for s in snapshots]
        self.old = QComboBox(); self.old.addItems(labels)
        self.new = QComboBox(); self.new.addItems(labels)
        self.new.setCurrentIndex(new_index)
        self.old.setCurrentIndex(min(new_index + 1, len(snapshots) - 1))        # Snapshots sind neueste zuerst
        go = QPushButton("Vergleichen"); go.setObjectName("primary")
        self.info = QLabel(""); self.info.setWordWrap(True)
        self.table = make_table(["", "Pfad", "Archiv", "Dateien", "Größe"], stretch_last=False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        close = QPushButton("Schließen")
        top = QHBoxLayout()
        for w in (QLabel("Älter:"), self.old, QLabel("Neuer:"), self.new, go):
            top.addWidget(w)
        top.addStretch()
        bot = QHBoxLayout(); bot.addStretch(); bot.addWidget(close)
        lay = QVBoxLayout(self); lay.addLayout(top); lay.addWidget(self.info); lay.addWidget(self.table, 1); lay.addLayout(bot)
        self.runner = Runner(self, lambda m: self.info.setText(f"Fehler: {m}"))
        go.clicked.connect(self.compare); close.clicked.connect(self.accept)
        if len(snapshots) >= 2 and self.old.currentIndex() != self.new.currentIndex():
            QTimer.singleShot(0, self.compare)
        else:
            self.info.setText("Zum Vergleichen werden mindestens zwei Snapshots gebraucht.")

    def compare(self) -> None:
        a, b = self.snaps[self.old.currentIndex()], self.snaps[self.new.currentIndex()]
        if a is b:
            self.info.setText("Bitte zwei verschiedene Snapshots wählen.")
            return
        if a.backup_time > b.backup_time:
            a, b = b, a
        self.info.setText("Vergleiche …")
        self.runner.run(lambda _l: self.mgr.diff(self.profile, a, b), self._done)

    def _done(self, results) -> None:
        rows, colors = [], []
        sym = {"added": "+", "changed": "~", "removed": "−"}
        col = {"added": "accent", "changed": "muted", "removed": "danger"}
        total = {"added": 0, "changed": 0, "removed": 0}
        for r in results:
            for k, v in r.counts.items():
                total[k] += v
            for it in r.items[:2000]:
                size = (f"{fmt_size(it.old.size or 0)} → {fmt_size(it.new.size or 0)}" if it.change == "changed" and it.old and it.new
                        else fmt_size(it.size))
                rows.append([sym[it.change], it.path + ("/" if it.kind == "d" else ""), r.archive,
                             str(it.files) if it.kind == "d" else "", size])
                colors.append(col[it.change])
        fill_table(self.table, rows)
        for i, c in enumerate(colors):
            for j in range(self.table.columnCount()):
                self.table.item(i, j).setForeground(QBrush(theme_color(c)))
        self.info.setText(f"{total['added']} neu, {total['changed']} geändert, {total['removed']} gelöscht"
                          + (" (Anzeige auf 2000 Zeilen begrenzt)" if len(rows) >= 2000 else ""))


# -- Restore-Test ------------------------------------------------------------------------------------------------
class RestoreTestDialog(QDialog):
    def __init__(self, mgr: ProfileManager, profile: Profile, snapshot: Snapshot, parent=None):
        super().__init__(parent)
        self.mgr, self.profile, self.snapshot = mgr, profile, snapshot
        self.setWindowTitle(f"Restore-Test – {snapshot.path}")
        self.resize(760, 460)
        self.archive = QComboBox(); self.archive.addItems([a for a in snapshot.archives if a != "recovery.pxar"])
        self.count = QSpinBox(); self.count.setRange(1, 50); self.count.setValue(5)
        self.go = QPushButton("Test starten"); self.go.setObjectName("primary")
        self.info = QLabel("Holt zufällige Dateien aus dem Snapshot in einen Wegwerf-Ordner und prüft sie: lesbar, Größe, und "
                           "SHA-256 gegen das Original, sofern dieses unverändert ist."); self.info.setWordWrap(True)
        self.bar = QProgressBar(); self.bar.setRange(0, 1); self.bar.setValue(0)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True)
        close = QPushButton("Schließen")
        top = QHBoxLayout()
        for w in (QLabel("Archiv:"), self.archive, QLabel("Dateien:"), self.count, self.go):
            top.addWidget(w)
        top.addStretch()
        bot = QHBoxLayout(); bot.addStretch(); bot.addWidget(close)
        lay = QVBoxLayout(self); lay.addLayout(top); lay.addWidget(self.info); lay.addWidget(self.bar); lay.addWidget(self.out, 1); lay.addLayout(bot)
        self.runner = Runner(self, self._failed)
        self.go.clicked.connect(self.start); close.clicked.connect(self.accept)
        self.result = None
        self.go.setEnabled(self.archive.count() > 0)
        if not self.archive.count():
            self.info.setText("Dieser Snapshot enthält keine Datei-Archive.")

    def _failed(self, m: str) -> None:
        self.bar.setRange(0, 1); self.go.setEnabled(True)
        self.info.setText(f"Fehler: {m}")

    def start(self) -> None:
        self.go.setEnabled(False); self.bar.setRange(0, 0); self.out.clear()
        self.info.setText("Teste …")
        archive, count = self.archive.currentText(), self.count.value()
        self.runner.run(lambda line: self.mgr.run_restore_test(self.profile, self.snapshot, archive, count), self._done)

    def _done(self, res) -> None:
        self.result = res
        self.go.setEnabled(True); self.bar.setRange(0, 1); self.bar.setValue(1)
        self.info.setText(("✔ " if res.ok else "✘ ") + res.summary())
        self.out.setPlainText("\n".join(restore_test_lines(res)))


# -- Dashboard -----------------------------------------------------------------------------------------------------
class DashboardTab(QWidget):
    profileChosen = Signal(str)

    REFRESH_AFTER = 30     # Sekunden: beim Einblenden nur dann neu laden

    def __init__(self, mgr: ProfileManager, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self._loaded_at = 0.0
        self.ovs = []
        refresh = QPushButton("Aktualisieren"); refresh.clicked.connect(self.refresh)
        self.info = QLabel("Status aller Profile. Doppelklick wählt ein Profil."); self.info.setObjectName("muted")
        self.table = make_table(OVERVIEW_HEADER)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(170)
        self.usage_box = QVBoxLayout()
        top = QHBoxLayout(); top.addWidget(refresh); top.addWidget(self.info, 1)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 10, 0, 0)
        lay.addLayout(top); lay.addWidget(self.table, 1); lay.addLayout(self.usage_box); lay.addWidget(self.details)
        self.runner = Runner(self, lambda m: self.info.setText(f"Fehler: {m}"))
        self.table.itemDoubleClicked.connect(lambda i: self._choose(i.row()))

    def _choose(self, row: int) -> None:
        if 0 <= row < len(self.ovs):
            self.profileChosen.emit(self.ovs[row].profile.name)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if time.time() - self._loaded_at > self.REFRESH_AFTER:
            self.refresh()

    def refresh(self) -> None:
        self.info.setText("Lade …")
        self._loaded_at = time.time()

        def work(_l):
            profiles = self.mgr.profiles()
            ovs = [self.mgr.overview(p) for p in profiles]
            usages, seen = [], set()
            for p in profiles:
                key = (p.server, p.port, p.datastore)
                if key not in seen:
                    seen.add(key)
                    usages.append((p, self.mgr.usage(p)))
            tests = {p.slug: self.mgr.last_restore_test(p) for p in profiles}
            return ovs, usages, tests
        self.runner.run(work, self._show)

    def _show(self, data) -> None:
        self.ovs, usages, tests = data
        fill_table(self.table, [overview_row(o) for o in self.ovs])
        for i, o in enumerate(self.ovs):
            color = {"error": "danger", "running": "accent", "warning": "danger"}.get(o.state)
            if color:
                self.table.item(i, 1).setForeground(QBrush(theme_color(color)))
        while self.usage_box.count():
            w = self.usage_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        for p, u in usages:
            label = QLabel(usage_text(p, u)); label.setWordWrap(True)
            self.usage_box.addWidget(label)
            if u:
                bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(int(u.percent)); bar.setFormat(f"{u.percent:.0f} % belegt")
                self.usage_box.addWidget(bar)
        lines = []
        for o in self.ovs:
            lines += overview_details(o)
            t = tests.get(o.profile.slug)
            lines.append(f"{o.profile.name}: " + (f"letzter Restore-Test {fmt_time(t['time'])}: {t['summary']}" if t
                                                   else "noch kein Restore-Test (Snapshots → „Restore-Test …“)"))
        self.details.setPlainText("\n".join(lines))
        self.info.setText(f"Stand {time.strftime('%H:%M:%S')} – Doppelklick wählt ein Profil.")


# -- Verlauf -------------------------------------------------------------------------------------------------------
class HistoryTab(QWidget):
    def __init__(self, mgr: ProfileManager, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self.runs: list[RunRecord] = []
        self.names: dict[str, str] = {}
        self.profile_box = QComboBox()
        self.text = QLineEdit(); self.text.setPlaceholderText("Text in Meldung oder Ausgabe suchen …")
        self.failed = QCheckBox("Nur Fehler")
        self.period = QComboBox(); self.period.addItems(["Alle", "7 Tage", "30 Tage"])
        refresh = QPushButton("Aktualisieren")
        self.table = make_table(["Start", "Profil", "Ergebnis", "Dauer", "Meldung"])
        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        mono = QFont("monospace"); mono.setStyleHint(QFont.StyleHint.Monospace); self.log.setFont(mono)
        self.info = QLabel(""); self.info.setObjectName("muted")
        top = QHBoxLayout()
        for w in (self.profile_box, self.text, self.failed, self.period, refresh):
            top.addWidget(w)
        top.setStretch(1, 1)
        split = QSplitter(Qt.Orientation.Vertical); split.addWidget(self.table); split.addWidget(self.log); split.setSizes([260, 260])
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 10, 0, 0)
        lay.addLayout(top); lay.addWidget(self.info); lay.addWidget(split, 1)
        self.runner = Runner(self, lambda m: self.info.setText(f"Fehler: {m}"))
        refresh.clicked.connect(self.refresh)
        self.text.returnPressed.connect(self.refresh)
        self.failed.toggled.connect(self.refresh)
        self.period.currentIndexChanged.connect(self.refresh)
        self.profile_box.currentIndexChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._selected)
        self._reload_profiles()

    def _reload_profiles(self, select: str | None = None) -> None:
        self.profile_box.blockSignals(True)
        cur = select or self.profile_box.currentText()
        self.profile_box.clear()
        self.profile_box.addItem("Alle Profile")
        profiles = self.mgr.profiles()
        self.names = {p.slug: p.name for p in profiles}
        self.profile_box.addItems([p.name for p in profiles])
        i = self.profile_box.findText(cur)
        self.profile_box.setCurrentIndex(max(i, 0))
        self.profile_box.blockSignals(False)

    def show_profile(self, name: str | None) -> None:
        self._reload_profiles(name or "Alle Profile")
        self.refresh()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self.refresh()

    def refresh(self) -> None:
        name = self.profile_box.currentText()
        prof = next((p for p in self.mgr.profiles() if p.name == name), None)
        days = {1: 7, 2: 30}.get(self.period.currentIndex())
        since = time.time() - days * 86400 if days else None
        kw = dict(failed_only=self.failed.isChecked(), text=self.text.text().strip(), since=since, limit=200, with_log=True)
        self.info.setText("Lade …")
        self.runner.run(lambda _l: self.mgr.history(prof, **kw), self._show)

    def _show(self, runs) -> None:
        self.runs = runs
        fill_table(self.table, [[fmt_time(r.start), self.names.get(r.profile, r.profile), "OK" if r.ok else "FEHLER",
                                 fmt_duration(r.duration) + (" (data)" if r.mode == "data" else ""), r.message] for r in runs])
        for i, r in enumerate(runs):
            if not r.ok:
                for j in range(self.table.columnCount()):
                    self.table.item(i, j).setForeground(QBrush(theme_color("danger")))
        self.log.clear()
        self.info.setText(f"{len(runs)} Läufe" + (" (die neuesten 200)" if len(runs) >= 200 else ""))
        if runs:
            self.table.selectRow(0)

    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self.runs):
            r = self.runs[rows[0].row()]
            self.log.setPlainText("\n".join(r.log))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
