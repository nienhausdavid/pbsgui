"""GUI-Tests (offscreen) mit Fake-Client und gestubbter PBS-API."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from pbsgui.core.manager import ProfileManager  # noqa: E402
from pbsgui.core.secrets import FileStore  # noqa: E402
from pbsgui.gui import window as window_mod  # noqa: E402
from pbsgui.gui.add_server import AddServerDialog  # noqa: E402
from pbsgui.gui.db_dialog import DbCredDialog, DbDumpDialog  # noqa: E402
from fake_docker_env import dock  # noqa: E402,F401


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


class StubApi:
    def list_namespaces(self, ds):
        return ["a", "a/b"] if ds == "store1" else []


def make_dialog(app, weg):
    d = AddServerDialog()
    d.mode.setCurrentIndex(weg)
    d._api = StubApi()
    d.datastore.addItems(["store1"])
    d.datastore.setCurrentText("store1")
    assert pump(app, lambda: d._namespaces is not None)
    return d


def test_namespace_states_weg_a(app):
    d = make_dialog(app, 0)
    for text, state in [("", "root"), ("a", "exists"), ("a/b", "exists"), ("/a/", "exists"), ("neu", "new")]:
        d.namespace.setEditText(text)
        assert d.namespace_state() == state, text
    assert "neu angelegt" in d.ns_info.text()


def test_weg_b_blocks_missing_namespace(app):
    d = make_dialog(app, 1)
    d.server.setText("s"); d.name.setText("n")
    d.fp.setText("AA"); d.fp_ok.setChecked(True)
    d._api = StubApi()  # Eingaben oben invalidieren die Sitzung: Namespaces neu laden
    d._namespaces = None
    d._datastore_changed("store1")
    assert pump(app, lambda: d._namespaces is not None)
    d.namespace.setEditText("gibtsnicht")
    d._start()
    assert "nicht anlegen" in d.status.text()
    assert d.profile is None


def test_unknown_state_without_connection(app):
    d = AddServerDialog()
    d.namespace.setEditText("x")
    assert d.namespace_state() == "unknown"


def test_changing_server_invalidates_session(app):
    d = make_dialog(app, 0)
    d.server.setText("anderer-server")
    assert d._api is None and d._namespaces is None


def test_save_creates_key_automatically(app, fake_env, profile, paths, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(paths.config.parent))
    mgr = ProfileManager(paths, FileStore(paths.creds))
    profile.encrypt = True
    mgr.save(profile, token="t")
    offered = []
    monkeypatch.setattr(window_mod.MainWindow, "_offer_key_export", lambda self, p: offered.append(p.name))
    w = window_mod.MainWindow(mgr)
    assert w.current.encrypt is True
    assert w.save_profile() is True
    assert offered == ["Laptop"]
    assert mgr.secrets.get("laptop", "key")
    assert mgr.profiles_store.load("Laptop").key_fingerprint == "aa:bb:cc:dd"
    assert "aa:bb:cc:dd" in w.key_info.text() and w.key_btn.isEnabled()
    first = mgr.secrets.get("laptop", "key")
    assert w.save_profile() is True and offered == ["Laptop"]  # zweites Speichern erzeugt keinen neuen Key
    assert mgr.secrets.get("laptop", "key") == first


def test_encryption_off_shows_warning(app, fake_env, profile, paths):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    profile.encrypt = False
    mgr.save(profile, token="t")
    w = window_mod.MainWindow(mgr)
    assert "unverschlüsselt" in w.key_info.text() and not w.key_btn.isEnabled()


def test_dialog_full_scope_disabled_without_admin(app):
    d = AddServerDialog(admin_ok=False)
    assert not d.scope_full.isEnabled() and d.scope_home.isChecked()
    assert d.backup_mode() == "home"


def test_dialog_scope_sets_mode_sources_and_scope(app):
    from pbsgui.core.models import Profile
    d = AddServerDialog(admin_ok=True)
    assert d.scope_full.isEnabled()
    p = d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a"))
    assert p.backup_mode == "home" and p.scope == "user" and p.sources == [str(__import__("pathlib").Path.home())]
    d.scope_full.setChecked(True)
    p = d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a"))
    assert p.backup_mode == "full" and p.scope == "system" and p.sources == ["/"]


def test_window_full_profile_locks_sources_and_scope(app, fake_env, profile, paths):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    profile.backup_mode, profile.scope, profile.sources = "full", "user", []
    mgr.profiles_store.save(profile)  # direkt speichern (System-Kopie braucht pkexec)
    w = window_mod.MainWindow(mgr)
    assert w.sources.count() == 1 and w.sources.item(0).text() == "/"
    assert not w.sources.isEnabled() and not w.src_add.isEnabled() and not w.f_scope.isEnabled()
    assert "Vollständiges System" in w.mode_info.text()


def test_mount_list_defaults_and_selection(app, fake_mounts):
    from pbsgui.gui.mounts_widget import MountList
    ml = MountList()
    ml.load(None)
    assert ml.selected() == ["/boot", "/data"]  # USB und Duplikat nicht angehakt
    ml.load(["/data", "/media/david/STICK", "/srv/dup"])
    assert ml.selected() == ["/data", "/media/david/STICK"]  # Duplikat bleibt gesperrt (nicht in der Auswahl)
    assert not (ml.item(3).flags() & __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ItemFlag.ItemIsEnabled)


def test_dialog_full_shows_drive_list_and_stores_selection(app, fake_mounts):
    from PySide6.QtCore import Qt
    from pbsgui.core.models import Profile
    d = AddServerDialog(admin_ok=True)
    d.show()
    assert d.mount_list.isHidden()  # nur bei Vollbackup sichtbar
    d.scope_full.setChecked(True)
    assert not d.mount_list.isHidden() and d.mount_list.selected() == ["/boot", "/data"]
    d.mount_list.item(1).setCheckState(Qt.CheckState.Unchecked)  # /data abwählen
    d.mount_list.item(2).setCheckState(Qt.CheckState.Checked)    # USB ausdrücklich dazu
    p = d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a"))
    assert p.full_mounts == ["/boot", "/media/david/STICK"] and p.sources == ["/"]
    d.scope_home.setChecked(True)
    p = d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a"))
    assert p.full_mounts is None


def test_window_full_profile_roundtrips_mount_selection(app, fake_env, fake_mounts, profile, paths):
    from PySide6.QtCore import Qt
    mgr = ProfileManager(paths, FileStore(paths.creds))
    profile.backup_mode, profile.scope, profile.sources, profile.full_mounts = "full", "user", [], ["/data"]
    mgr.profiles_store.save(profile)
    w = window_mod.MainWindow(mgr)
    assert w.mount_list.selected() == ["/data"]
    w.mount_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert w._collect().full_mounts == ["/boot", "/data"]


# -- Dateibrowser ------------------------------------------------------------------------------------
def make_files_dialog(app, fake_env, tmp_path, monkeypatch, sources=None):
    import shutil
    from pbsgui.core.manager import ProfileManager as PM
    from pbsgui.core.models import Profile, Snapshot
    home = tmp_path / "heim"
    (home / "Dokumente").mkdir(parents=True)
    (home / "Dokumente/a.txt").write_text("LOKAL ALT")
    prof = Profile(name="p", server="s", datastore="d", auth_id="a", sources=sources or [str(home)], encrypt=False)
    name = prof.archive_specs()[0].split(":")[0]
    arch = tmp_path / "pxar" / name
    (arch / "Dokumente/sub").mkdir(parents=True)
    (arch / "Dokumente/a.txt").write_text("AUS BACKUP")
    (arch / "Dokumente/sub/b.txt").write_text("B")
    (arch / "notiz.txt").write_text("N")
    monkeypatch.setenv("FAKE_PBS_TREE", str(tmp_path / "pxar"))
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")
    monkeypatch.setenv("HOME", str(home))
    mgr = PM(Paths("user", tmp_path / "c", tmp_path / "u", tmp_path / "c/creds", tmp_path / "s"), FileStore(tmp_path / "c/creds"))
    mgr.save(prof, token="t")
    from pbsgui.gui.files_dialog import FilesDialog
    d = FilesDialog(mgr, prof, Snapshot("host", "pc", 1000, files=[name]))
    assert pump(app, lambda: d.tree.topLevelItemCount() > 0)
    return d, home, tmp_path


from pbsgui.core.paths import Paths  # noqa: E402


def test_files_dialog_lazy_tree_selection_and_buttons(app, fake_env, tmp_path, monkeypatch):
    d, home, _ = make_files_dialog(app, fake_env, tmp_path, monkeypatch)
    top = [d.tree.topLevelItem(i).text(0) for i in range(d.tree.topLevelItemCount())]
    assert top == ["Dokumente", "notiz.txt"]                                    # Ordner zuerst
    folder = d.tree.topLevelItem(0)
    assert folder.child(0).text(0) == "…"                                       # noch nicht geladen
    d.tree.expandItem(folder)
    assert [folder.child(i).text(0) for i in range(folder.childCount())] == ["sub", "a.txt"]
    assert not d.btn_folder.isEnabled() and not d.btn_over.isEnabled()
    folder.setSelected(True)
    assert d.btn_folder.isEnabled() and d.btn_over.isEnabled()                  # Quelle liegt im Home
    assert "2 Dateien" in d.info.text() or "Dateien" in d.info.text()


def test_files_dialog_selecting_folder_and_child_counts_only_the_folder(app, fake_env, tmp_path, monkeypatch):
    d, home, _ = make_files_dialog(app, fake_env, tmp_path, monkeypatch)
    folder = d.tree.topLevelItem(0)
    d.tree.expandItem(folder)
    folder.setSelected(True)
    folder.child(1).setSelected(True)                                           # a.txt zusätzlich
    assert d.selected_paths() == ["/Dokumente"]                                  # keine doppelte Auswahl


def test_files_dialog_overwrite_disabled_outside_home(app, fake_env, tmp_path, monkeypatch):
    d, home, tmp = make_files_dialog(app, fake_env, tmp_path, monkeypatch, sources=["/etc"])
    d.tree.topLevelItem(0).setSelected(True)
    assert d.btn_folder.isEnabled() and not d.btn_over.isEnabled()
    assert "nur für Dateien im Home" in d.btn_over.toolTip()


def test_files_dialog_search(app, fake_env, tmp_path, monkeypatch):
    d, *_ = make_files_dialog(app, fake_env, tmp_path, monkeypatch)
    d.search.setText("b.txt")
    d._do_search()
    assert d.tree.topLevelItemCount() == 1 and d.tree.topLevelItem(0).text(0) == "/Dokumente/sub/b.txt"
    d.search.setText("")
    d._do_search()
    assert d.tree.topLevelItemCount() == 2


# -- Zeitplan, Datenbank-Dumps und „Backup direkt starten“ beim Anlegen ------------------------------------------------
needs_analyze = pytest.mark.skipif(not __import__("shutil").which("systemd-analyze"), reason="systemd-analyze fehlt")


def test_dialog_offers_schedule_presets_and_defaults_to_none(app):
    d = AddServerDialog(admin_ok=True)
    values = [d.schedule.itemData(i) for i in range(d.schedule.count())]
    assert values[0] == "" and "00/8:00" in values and values[-1] == "__custom__"
    assert d.schedule_expr() == "" and d.db_dump.isChecked() is False and d.start_now.isChecked() is False
    assert d.schedule_custom.isHidden()


@needs_analyze
def test_dialog_preset_custom_and_invalid_schedule(app):
    d = AddServerDialog(admin_ok=True)
    d.schedule.setCurrentIndex(d.schedule.findData("00/8:00"))
    assert d.schedule_expr() == "00/8:00" and "Nächster Lauf" in d.schedule_info.text()
    d.schedule.setCurrentIndex(d.schedule.findData("__custom__"))
    assert not d.schedule_custom.isHidden()
    assert d.schedule_expr() is None and "Eigenen Zeitplan" in d.status.text()      # leer
    d.schedule_custom.setText("quatsch")
    assert d.schedule_expr() is None and "ungültig" in d.status.text()
    d.schedule_custom.setText("Mon..Fri *-*-* 03:00:00")
    assert d.schedule_expr() == "Mon..Fri *-*-* 03:00:00"


@needs_analyze
def test_invalid_schedule_blocks_setup_before_anything_is_created(app):
    d = make_dialog(app, 1)
    d.server.setText("s"); d.name.setText("n"); d.fp.setText("AA"); d.fp_ok.setChecked(True)
    d._api = StubApi(); d._namespaces = ["a"]
    d.namespace.setEditText("a")
    d.schedule.setCurrentIndex(d.schedule.findData("__custom__"))
    d.schedule_custom.setText("quatsch")
    d._start()
    assert "ungültig" in d.status.text() and d.profile is None and d._worker is None


def test_scoped_profile_carries_schedule_and_db_dump(app):
    from pbsgui.core.models import Profile
    d = AddServerDialog(admin_ok=True)
    d._schedule = "00/8:00"
    d.db_dump.setChecked(True)
    p = d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a"))
    assert p.schedule == "00/8:00" and p.db_dump is True


def test_window_backup_tab_roundtrips_db_dump_flag(app, fake_env, profile, paths):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    w = window_mod.MainWindow(mgr)
    assert w.f_dbdump.isChecked() is False
    w.f_dbdump.setChecked(True)
    assert w.save_profile() is True
    assert mgr.profiles_store.load("Laptop").db_dump is True
    assert window_mod.MainWindow(mgr).f_dbdump.isChecked() is True


def test_db_dialog_lists_databases_and_creates_dump_user(app, fake_env, profile, paths, dock):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    d = DbDumpDialog(mgr, mgr.profiles()[0])
    assert pump(app, lambda: len(d.dbs) == 3)
    labels = [d.list.item(i).text() for i in range(3)]
    assert any("kein Zugang nötig" in l for l in labels) and any("Zugang FEHLT" in l for l in labels)
    maria = next(i for i, l in enumerate(labels) if "firefly_db" in l)
    pg = next(i for i, l in enumerate(labels) if "kein Zugang nötig" in l)
    d.list.setCurrentRow(pg)
    assert not d.cred_btn.isEnabled()                                    # Postgres braucht keinen Zugang
    d.list.setCurrentRow(maria)
    assert d.cred_btn.isEnabled() and not d.del_btn.isEnabled()
    c = DbCredDialog(mgr, d.profile, d.dbs[maria])
    c.create.setChecked(True)
    assert not c.user.isEnabled() and c.admin_pw.isEnabled()
    c.admin_pw.setText("adminpw")
    c.save()
    assert c.admin_pw.text() == "" and pump(app, lambda: c.result() == 1)   # QDialog.Accepted
    assert mgr.db_credentials(d.profile)[d.dbs[maria].key]["user"] == "pbsgui_dump"
    d.states.clear()                                                      # wie edit_credential(): Zustand neu ermitteln
    d._show(d.dbs)
    assert "Zugang: pbsgui_dump" in d.list.item(maria).text() and d.del_btn.isEnabled()
    d.remove_credential()
    assert mgr.db_credentials(d.profile) == {} and "FEHLT" in d.list.item(maria).text()


def test_db_credential_dialog_rejects_wrong_password_and_stays_open(app, fake_env, profile, paths, dock):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    d = DbDumpDialog(mgr, mgr.profiles()[0])
    assert pump(app, lambda: len(d.dbs) == 3)
    db = next(x for x in d.dbs if x.container == "firefly_db")
    c = DbCredDialog(mgr, d.profile, db)
    c.user.setText("nobody"); c.pw.setText("falsch")
    c.save()
    assert pump(app, lambda: "Fehler" in c.status.text())
    assert c.result() == 0 and c.buttons.isEnabled() and mgr.db_credentials(d.profile) == {}


def test_db_dialog_save_stores_flag(app, fake_env, profile, paths, dock):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    d = DbDumpDialog(mgr, mgr.profiles()[0])
    d.enabled.setChecked(True)
    d.save()
    assert mgr.profiles_store.load("Laptop").db_dump is True and d.result() == 1


@needs_analyze
def test_after_create_activates_schedule_opens_db_dialog_then_starts_backup(app, fake_env, profile, paths, monkeypatch):
    import subprocess
    from pbsgui.core import scheduler as scheduler_mod
    calls, order = [], []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw) if cmd[0] == "systemd-analyze" else subprocess.CompletedProcess(cmd, 0, "ActiveState=active\n", "")
    monkeypatch.setattr(scheduler_mod, "_default_run", fake_run)
    mgr = ProfileManager(paths, FileStore(paths.creds))
    profile.schedule, profile.db_dump = "00/8:00", True
    mgr.save(profile, token="t")
    w = window_mod.MainWindow(mgr)
    monkeypatch.setattr(window_mod.MainWindow, "db_dialog", lambda self: order.append("db"))
    monkeypatch.setattr(window_mod.MainWindow, "backup_now", lambda self: order.append("backup"))
    w._after_create(w.current, True)
    assert ["systemctl", "--user", "enable", "--now", "pbsgui-laptop.timer"] in calls
    assert order == ["db", "backup"]                                     # erst die Zugänge, dann das erste Backup


def test_after_create_without_options_does_nothing(app, fake_env, profile, paths, monkeypatch):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    w = window_mod.MainWindow(mgr)
    seen = []
    monkeypatch.setattr(window_mod.MainWindow, "db_dialog", lambda self: seen.append("db"))
    monkeypatch.setattr(window_mod.MainWindow, "backup_now", lambda self: seen.append("backup"))
    w._after_create(w.current, False)
    assert seen == []


# -- Änderungserkennung --------------------------------------------------------------------------------------------------
def test_dialog_only_changed_is_on_by_default_and_sets_profile(app):
    from pbsgui.core.models import Profile
    d = AddServerDialog(admin_ok=True)
    assert d.only_changed.isChecked() is True
    assert d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a")).change_detection == "metadata"
    d.only_changed.setChecked(False)
    assert d._scoped(Profile(name="x", server="s", datastore="d", auth_id="a")).change_detection == "legacy"


def test_window_backup_tab_roundtrips_change_detection(app, fake_env, profile, paths):
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")                                       # bestehendes Profil: klassisch
    w = window_mod.MainWindow(mgr)
    assert w.f_changed.isChecked() is False
    w.f_changed.setChecked(True)
    assert w.save_profile() is True
    assert mgr.profiles_store.load("Laptop").change_detection == "metadata"
    assert window_mod.MainWindow(mgr).f_changed.isChecked() is True


def test_window_restore_offers_logical_archive_names_for_split_snapshots(app, fake_env, profile, paths):
    from pbsgui.core.models import Snapshot
    snap = Snapshot("host", "laptop", 1, files=["root.mpxar.didx", "root.ppxar.didx", "catalog.pcat1.didx"])
    assert snap.archives == ["root.pxar"]


def test_db_dialog_passwordless_setup(app, fake_env, profile, paths, dock, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    d = DbDumpDialog(mgr, mgr.profiles()[0])
    assert pump(app, lambda: len(d.dbs) == 3)
    labels = [d.list.item(i).text() for i in range(3)]
    maria = next(i for i, l in enumerate(labels) if "firefly_db" in l)
    pg = next(i for i, l in enumerate(labels) if "kein Zugang nötig" in l)
    d.list.setCurrentRow(pg)
    assert not d.socket_btn.isEnabled()
    d.list.setCurrentRow(maria)
    assert d.socket_btn.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    d.setup_access()
    assert dock.data()["events"] == []                                          # Nein: nichts gestoppt
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    d.setup_access()
    assert pump(app, lambda: "ohne Passwort (Socket)" in d.list.item(maria).text())
    assert dock.data()["events"][0] == "stop firefly_db" and dock.data()["events"][-1] == "start firefly_db"
    assert mgr.db_credentials(d.profile) == {} and not d.socket_btn.isEnabled()
