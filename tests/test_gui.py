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
