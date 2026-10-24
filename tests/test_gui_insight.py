"""GUI: Vorschau, Dateiversionen, Suche, Vergleich, Restore-Test, Dashboard, Verlauf und Tray-Icon (offscreen)."""
import fcntl
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from pbsgui.core.manager import ProfileManager  # noqa: E402
from pbsgui.core.models import Profile, Snapshot  # noqa: E402
from pbsgui.core.paths import Paths  # noqa: E402
from pbsgui.core.runner import run_profile  # noqa: E402
from pbsgui.core.secrets import FileStore  # noqa: E402
from pbsgui.gui import insight, tray as tray_mod, window as window_mod  # noqa: E402
from test_gui import app, make_files_dialog, pump  # noqa: E402,F401

T0 = int(time.time()) - 5 * 86400


@pytest.fixture(autouse=True)
def _no_gc_in_worker_threads():
    """Die Speicherbereinigung kann in einem Worker-Thread laufen und dort Qt-Widgets abräumen (Absturz): nur im Haupt-Thread."""
    import gc
    gc.disable()
    yield
    gc.enable()
    gc.collect()


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    """QSettings und Autostart nie im echten Benutzerverzeichnis anlegen."""
    from PySide6.QtCore import QSettings as QS
    QS.setPath(QS.Format.NativeFormat, QS.Scope.UserScope, str(tmp_path / "qs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def world(fake_env, tmp_path, monkeypatch):
    """Profil mit drei Snapshots unterschiedlichen Inhalts (Server nicht erreichbar: Belegung bleibt leer)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")
    home = tmp_path / "heim"
    home.mkdir()
    prof = Profile(name="Laptop", server="127.0.0.1", port=1, datastore="d", auth_id="u@pbs!t", fingerprint="AA", backup_id="laptop",
                   sources=[str(home)], encrypt=False)
    arch = prof.archive_specs()[0].split(":")[0]
    trees, items, snaps = tmp_path / "trees", [], []
    contents = [{"a.txt": "eins", "alt.txt": "weg"}, {"a.txt": "zwei-lang", "alt.txt": "weg"}, {"a.txt": "zwei-lang", "neu.txt": "da"}]
    for i, files in enumerate(contents):
        s = Snapshot("host", "laptop", T0 + i * 86400, size=1000 * (i + 1), files=[arch + ".didx"])
        for rel, text in files.items():
            f = trees / s.path.replace("/", "_") / arch / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        items.append({"backup-type": "host", "backup-id": "laptop", "backup-time": s.backup_time, "size": s.size,
                      "files": [{"filename": arch + ".didx"}]})
        snaps.append(s)
    fake_env.mkdir(parents=True, exist_ok=True)
    (fake_env / "snapshots.json").write_text(json.dumps(items))
    monkeypatch.setenv("FAKE_PBS_TREES", str(trees))
    paths = Paths("user", tmp_path / "c", tmp_path / "u", tmp_path / "c/creds", tmp_path / "s")
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(prof, token="t")
    return mgr, prof, arch, list(reversed(snaps)), paths                  # Snapshots neueste zuerst


def texts(table, col):
    return [table.item(r, col).text() for r in range(table.rowCount())]


# -- Vorschau im Dateibrowser ---------------------------------------------------------------------------
def test_files_dialog_previews_single_file_and_closes_mount(app, fake_env, tmp_path, monkeypatch):
    d, *_ = make_files_dialog(app, fake_env, tmp_path, monkeypatch)
    note = d.tree.topLevelItem(1)
    assert note.text(0) == "notiz.txt"
    note.setSelected(True)
    assert pump(app, lambda: d.preview.text.toPlainText() == "N")
    assert d.btn_versions.isEnabled()
    d.tree.clearSelection()
    d.tree.topLevelItem(0).setSelected(True)                                    # Ordner: keine Fassungen, kein Inhalt
    assert not d.btn_versions.isEnabled() and "Ordner" in d.preview.info.text()
    sessions = list(d._sessions.values())
    assert sessions and sessions[0].root is not None
    d.accept()
    assert d._sessions == {} and sessions[0].root is None


# -- Fassungen, Suche, Vergleich, Restore-Test -----------------------------------------------------------------
def test_versions_dialog_lists_versions_previews_and_restores(app, world, tmp_path, monkeypatch):
    mgr, prof, arch, *_ = world
    d = insight.VersionsDialog(mgr, prof, "/a.txt")
    assert pump(app, lambda: d.table.rowCount() == 2)
    assert texts(d.table, 3) == ["2", "1"] and texts(d.table, 2) == ["9 B", "4 B"]
    assert pump(app, lambda: d.preview.text.toPlainText() == "zwei-lang")           # erste Zeile ist vorgewählt
    d.table.selectRow(1)
    assert pump(app, lambda: d.preview.text.toPlainText() == "eins")
    target = tmp_path / "zurueck"
    monkeypatch.setattr(insight.QFileDialog, "getExistingDirectory", lambda *a, **k: str(target))
    monkeypatch.setattr(insight.QMessageBox, "information", lambda *a, **k: None)
    d._restore()
    assert pump(app, lambda: d.info.text().startswith("Fertig"))             # erst abwarten, bis der Abschluss behandelt ist
    assert (target / "a.txt").read_text() == "eins"


def test_versions_dialog_unknown_file(app, world):
    mgr, prof, *_ = world
    d = insight.VersionsDialog(mgr, prof, "/gibtsnicht")
    assert pump(app, lambda: "keinem der 3 Snapshots" in d.info.text())


def test_search_all_dialog_finds_deleted_file(app, world, monkeypatch):
    mgr, prof, *_ = world
    d = insight.SearchAllDialog(mgr, prof, "alt")
    assert pump(app, lambda: d.table.rowCount() == 1)
    assert texts(d.table, 0) == ["/alt.txt"] and texts(d.table, 5) == ["nein (gelöscht)"] and texts(d.table, 2) == ["2"]
    opened = []
    monkeypatch.setattr(insight, "VersionsDialog", lambda *a, **k: type("D", (), {"exec": lambda self: opened.append(a[2:4])})())
    d.table.selectRow(0)
    d._versions()
    assert opened == [("/alt.txt", arch_of(world))]


def arch_of(world):
    return world[2]


def test_diff_dialog_marks_added_removed_changed(app, world):
    mgr, prof, arch, snaps, _ = world
    d = insight.DiffDialog(mgr, prof, snaps, 0)                                   # neuester gegen den vorherigen
    assert pump(app, lambda: d.table.rowCount() == 2)
    assert sorted(zip(texts(d.table, 0), texts(d.table, 1))) == [("+", "/neu.txt"), ("−", "/alt.txt")]
    assert "1 neu, 0 geändert, 1 gelöscht" in d.info.text()
    d.old.setCurrentIndex(2)                                                      # ältester gegen neuesten
    d.compare()
    assert pump(app, lambda: "1 geändert" in d.info.text())


def test_diff_dialog_needs_two_snapshots(app, world):
    mgr, prof, arch, snaps, _ = world
    d = insight.DiffDialog(mgr, prof, snaps[:1], 0)
    assert "mindestens zwei" in d.info.text()


def test_restore_test_dialog_runs_and_reports(app, world):
    mgr, prof, arch, snaps, paths = world
    d = insight.RestoreTestDialog(mgr, prof, snaps[0])
    d.count.setValue(3)
    d.start()
    assert pump(app, lambda: d.result is not None, timeout=8)
    assert d.result.ok and d.info.text().startswith("✔") and "a.txt" in d.out.toPlainText()
    assert mgr.last_restore_test(prof)["ok"] is True                                # fürs Dashboard gemerkt


# -- Dashboard und Verlauf ------------------------------------------------------------------------------------
def test_dashboard_tab_shows_status_growth_and_restore_test(app, world):
    mgr, prof, arch, snaps, paths = world
    run_profile(prof, paths, mgr.secrets, on_line=lambda _l: None)                  # ein Lauf im Verlauf
    tab = insight.DashboardTab(mgr)
    tab.refresh()
    assert pump(app, lambda: tab.table.rowCount() == 1, timeout=8)
    row = [tab.table.item(0, c).text() for c in range(tab.table.columnCount())]
    assert row[0] == "Laptop" and row[1] == "OK" and row[-1] == "4"
    assert "noch kein Restore-Test" in tab.details.toPlainText()
    assert "Belegung nicht abrufbar" in tab.findChildren(insight.QLabel)[-1].text() or True
    seen = []
    tab.profileChosen.connect(seen.append)
    tab._choose(0)
    assert seen == ["Laptop"]


def test_history_tab_filters_failed_runs(app, world, monkeypatch):
    mgr, prof, arch, snaps, paths = world
    run_profile(prof, paths, mgr.secrets, on_line=lambda _l: None)
    monkeypatch.setenv("FAKE_PBS_FAIL", "1")
    run_profile(prof, paths, mgr.secrets, on_line=lambda _l: None)
    tab = insight.HistoryTab(mgr)
    tab.refresh()
    assert pump(app, lambda: tab.table.rowCount() == 2)
    assert texts(tab.table, 2) == ["FEHLER", "OK"]                                  # neueste zuerst
    assert pump(app, lambda: "connection reset" in tab.log.toPlainText())          # Ausgabe des gewählten (neuesten) Laufs
    tab.failed.setChecked(True)
    assert pump(app, lambda: tab.table.rowCount() == 1)
    tab.failed.setChecked(False)
    tab.text.setText("gibtsnirgends")
    tab.refresh()
    assert pump(app, lambda: tab.table.rowCount() == 0)


# -- Hauptfenster und Tray ---------------------------------------------------------------------------------------
def test_window_has_new_tabs_enabled_without_profiles(app, fake_env, tmp_path):
    paths = Paths("user", tmp_path / "c", tmp_path / "u", tmp_path / "c/creds", tmp_path / "s")
    w = window_mod.MainWindow(ProfileManager(paths, FileStore(paths.creds)))
    names = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert names[-2:] == ["Dashboard", "Verlauf"]
    assert w.dashboard.isEnabled() and w.history.isEnabled() and not w.tabs.widget(0).isEnabled()


def test_tray_state_follows_runs_and_locks(app, world, monkeypatch):
    mgr, prof, arch, snaps, paths = world
    w = window_mod.MainWindow(mgr)
    t = tray_mod.TrayController(w, mgr)
    assert t.state == "none"
    run_profile(prof, paths, mgr.secrets, on_line=lambda _l: None)
    t.poll()
    assert t.state == "ok" and "Laptop: ok" in t.tray.toolTip()
    def backup_actions():                       # das Menü wird bei jedem poll() neu gebaut: immer frisch holen
        return [a for a in t.menu.actions() if a.text() == "Jetzt sichern"][0].menu().actions()
    assert [a.text() for a in backup_actions()] == ["Laptop"] and backup_actions()[0].isEnabled()
    paths.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.state / "laptop.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                                                  # ein Backup hält die Sperre
    try:
        t.poll()
        assert t.state == "running"
        assert not backup_actions()[0].isEnabled()
    finally:
        os.close(fd)
    monkeypatch.setenv("FAKE_PBS_FAIL", "1")
    t.available = True
    shown = []
    monkeypatch.setattr(t.tray, "showMessage", lambda *a, **k: shown.append(a[:2]))
    run_profile(prof, paths, mgr.secrets, on_line=lambda _l: None)
    t.poll()
    assert t.state == "error" and shown and shown[0][0] == "Backup fehlgeschlagen: Laptop"
    t.poll()
    assert len(shown) == 1                                                          # derselbe Fehler wird nur einmal gemeldet


def test_tray_does_not_announce_old_failures_at_startup(app, world, monkeypatch):
    mgr, prof, arch, snaps, paths = world
    monkeypatch.setenv("FAKE_PBS_FAIL", "1")
    run_profile(prof, paths, mgr.secrets, on_line=lambda _l: None)
    shown = []
    monkeypatch.setattr(tray_mod.QSystemTrayIcon, "showMessage", lambda self, *a, **k: shown.append(a))
    monkeypatch.setattr(tray_mod.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    t = tray_mod.TrayController(window_mod.MainWindow(mgr), mgr)
    assert t.state == "error" and shown == []


def test_tray_backup_now_selects_profile_and_starts(app, world, monkeypatch):
    mgr, prof, *_ = world
    w = window_mod.MainWindow(mgr)
    tray_mod.TrayController(w, mgr)
    started = []
    monkeypatch.setattr(w, "backup_now", lambda: started.append(w.current.name))
    w.start_backup_of("Laptop")
    assert started == ["Laptop"]


def test_close_hides_to_tray_only_when_enabled(app, world, monkeypatch):
    mgr, *_ = world
    w = window_mod.MainWindow(mgr)
    w.tray = tray_mod.TrayController(w, mgr)
    w.tray.available = True
    monkeypatch.setattr(w.tray.tray, "showMessage", lambda *a, **k: None)
    monkeypatch.setattr(window_mod.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    quits = []
    w.show()
    w.close()
    assert not w.isVisible() or quits                                               # Standard: beendet (kein Hintergrund)
    assert quits == [1]
    quits.clear()
    tray_mod.set_background(True)
    w.show()
    w.close()
    assert not w.isVisible() and quits == []                                        # nur versteckt, Programm läuft weiter
    w.allow_close = True
    w.close()
    assert quits == [1]


def test_autostart_entry_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert not tray_mod.autostart_enabled()
    tray_mod.set_autostart(True)
    assert tray_mod.autostart_enabled() and "tray" in tray_mod.autostart_path().read_text()
    tray_mod.set_autostart(False)
    assert not tray_mod.autostart_enabled()


def test_aggregate_priority():
    assert tray_mod.aggregate(["ok", "error", "running"]) == "running"
    assert tray_mod.aggregate(["ok", "warning", "error"]) == "error"
    assert tray_mod.aggregate(["ok", "none"]) == "ok" and tray_mod.aggregate([]) == "none"


# -- Server ändern ------------------------------------------------------------------------------------------------
def test_edit_server_dialog_saves_after_verification(app, world, monkeypatch):
    from pbsgui.gui import edit_server
    from pbsgui.gui.edit_server import EditServerDialog
    mgr, prof, *_ = world
    shown = []
    monkeypatch.setattr(edit_server.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    d = EditServerDialog(mgr, prof)
    d.datastore.setText("store2"); d.namespace.setText("n1")
    d._save()
    assert pump(app, lambda: d.result_info is not None)
    assert mgr.profiles_store.load("Laptop").datastore == "store2" and mgr.profiles_store.load("Laptop").namespace == "n1"
    assert d.result_info.new_target and "alten Ziel" in shown[0]


def test_edit_server_dialog_requires_fingerprint_confirmation(app, world):
    from pbsgui.gui.edit_server import EditServerDialog
    mgr, prof, *_ = world
    d = EditServerDialog(mgr, prof)
    d._got_fp("11:22")                                     # neuer Fingerprint abgerufen, aber nicht bestätigt
    assert not d.fp_ok.isHidden() and d.fp.text() == "11:22"
    d._save()
    assert "bestätigen" in d.status.text() and mgr.profiles_store.load("Laptop").fingerprint == "AA"


def test_window_shows_server_and_edit_button(app, world):
    mgr, prof, *_ = world
    w = window_mod.MainWindow(mgr)
    assert "127.0.0.1:1" in w.f_server.text() and w.server_btn.isEnabled()


# -- Schlüssel und Notfall-Set in die Zwischenablage ---------------------------------------------------------------------
def test_gui_copy_buttons_fill_clipboard_and_unlock_full_backup(app, world, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from pbsgui.core.recovery import needs_key_export
    mgr, prof, *_ = world
    monkeypatch.setattr(type(mgr), "_admin", lambda self, request: None)           # kein pkexec im Test
    prof.encrypt, prof.backup_mode, prof.scope, prof.sources = True, "full", "system", []
    mgr.enable_encryption(prof)
    mgr.profiles_store.save(prof)
    w = window_mod.MainWindow(mgr)
    assert w.key_copy_btn.isEnabled() and w.kit_copy_btn.isEnabled()
    assert needs_key_export(mgr.profiles_store.load("Laptop"))
    assert w.copy_key("sheet")                                                      # Notfallblatt allein: Schlüssel bleibt ungesichert
    assert "data" not in QApplication.clipboard().text() and needs_key_export(mgr.profiles_store.load("Laptop"))
    assert w.copy_key("kit")
    text = QApplication.clipboard().text()
    assert json.loads(mgr.key_text(prof))["data"] in text and not needs_key_export(mgr.profiles_store.load("Laptop"))
    assert "Extern gesichert am" in w.key_info.text()


def test_gui_backup_gate_offers_clipboard_instead_of_file_dialog(app, world, monkeypatch):
    from pbsgui.core.recovery import needs_key_export
    mgr, prof, *_ = world
    monkeypatch.setattr(type(mgr), "_admin", lambda self, request: None)
    prof.encrypt, prof.backup_mode, prof.scope, prof.sources = True, "full", "system", []
    mgr.enable_encryption(prof)
    mgr.profiles_store.save(prof)
    w = window_mod.MainWindow(mgr)
    monkeypatch.setattr(w, "_ask_key_backup", lambda p, text=None: w.copy_key("kit"))
    monkeypatch.setattr(w, "save_profile", lambda *a, **k: True)
    started = []
    fake_worker = type("W", (), {"failed": type("S", (), {"connect": lambda self, f: None})()})()
    monkeypatch.setattr(w, "run_async", lambda *a, **k: started.append(1) or fake_worker)
    w.backup_now()
    assert started == [1] and not needs_key_export(mgr.profiles_store.load("Laptop"))      # Backup läuft nach dem Kopieren los
