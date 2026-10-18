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
