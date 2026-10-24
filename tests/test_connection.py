"""Verbindung eines Profils nachträglich ändern (Core und Kommandozeile)."""
import json

import pytest

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core.connection import ConnectionChangeError, change_connection
from pbsgui.core.manager import ProfileManager
from test_cli import add_b, env, run  # noqa: F401


@pytest.fixture
def prof(env, capsys, server):
    assert add_b(capsys, server)[0] == 0
    capsys.readouterr()
    return server


def stored(env_):
    from pbsgui.core.paths import Paths
    mgr = ProfileManager(Paths.for_scope("user"))
    return mgr, mgr.profiles()[0]


def test_show_current_connection(prof, capsys):
    code, out, _ = run(capsys, "server", "Test")
    assert code == 0 and "Datastore:   store1" in out and "Namespace:   a" in out and "u@pbs!t" in out


def test_change_datastore_and_namespace_verified_and_saved(env, prof, capsys):
    code, out, err = run(capsys, "server", "Test", "--datastore", "store2", "--namespace", "x/y")
    assert code == 0, err
    assert "Geändert: datastore, namespace" in out and "Snapshots bleiben auf dem alten Ziel" in out
    _, p = stored(env)
    assert (p.datastore, p.namespace, p.server) == ("store2", "x/y", "127.0.0.1")
    assert p.backup_id == "host" and p.sources == ["/etc"]                      # alles andere bleibt


def test_change_server_port_and_fingerprint(env, prof, capsys):
    code, out, err = run(capsys, "server", "Test", "--server", "pbs2.example", "--port", "9000", "--fingerprint", "AA:BB")
    assert code == 0, err
    _, p = stored(env)
    assert (p.server, p.port, p.fingerprint) == ("pbs2.example", 9000, "AA:BB")
    assert "Hinweis" in out


def test_failed_check_changes_nothing(env, prof, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_PBS_EXPECT_PASSWORD", "anderes-secret")            # Token passt am "neuen" Server nicht
    code, _, err = run(capsys, "server", "Test", "--datastore", "store2")
    assert code == 1 and "nichts wurde geändert" in err
    _, p = stored(env)
    assert p.datastore == "store1"
    monkeypatch.delenv("FAKE_PBS_EXPECT_PASSWORD")
    assert run(capsys, "server", "Test", "--datastore", "store2", "--no-check")[0] == 0   # ohne Prüfung geht es trotzdem


def test_new_token_id_needs_secret_and_replaces_it(env, prof, capsys, monkeypatch):
    mgr, p = stored(env)
    with pytest.raises(ConnectionChangeError, match="Token-Secret"):
        change_connection(mgr, p, auth_id="neu@pbs!tok")
    monkeypatch.setenv("PBSGUI_TOKEN_SECRET", "neues-geheimnis")
    code, out, err = run(capsys, "server", "Test", "--token-id", "neu@pbs!tok")
    assert code == 0, err
    mgr, p = stored(env)
    assert p.auth_id == "neu@pbs!tok" and mgr.secrets.get(p.slug, "token") == "neues-geheimnis"


def test_unchanged_and_invalid_input(env, prof, capsys):
    mgr, p = stored(env)
    assert change_connection(mgr, p, datastore="store1").changed == []
    with pytest.raises(ConnectionChangeError):
        change_connection(mgr, p, port=70000)
    with pytest.raises(ConnectionChangeError):
        change_connection(mgr, p, server=" ")
    code, out, _ = run(capsys, "server", "Test", "--datastore", "store1")
    assert "Nichts geändert" in out
