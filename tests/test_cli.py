"""Tests für das Kommandozeilen-Tool `pbsgui` (Fake-Client + lokaler TLS-Testserver)."""
import json
import os

import pytest

from fake_pbs_api import EXISTS, LOG, pytestmark, server  # noqa: F401
from pbsgui.cli.main import main
from pbsgui.core import manager as manager_mod
from pbsgui.core import scheduler as scheduler_mod
from pbsgui.core.pbs_api import fetch_fingerprint
from pbsgui.core.secrets import FileStore
from test_scheduler_secrets import FakeRun


@pytest.fixture
def env(fake_env, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-s"))
    monkeypatch.setattr(manager_mod, "best_store", lambda scope, d: FileStore(d))
    monkeypatch.setenv("PBSGUI_TOKEN_SECRET", "geheim")
    monkeypatch.setenv("PBSGUI_ROOT_PASSWORD", "rootpw")
    return tmp_path


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def add_b(capsys, server, *extra, ns="a", name="Test"):
    fp = fetch_fingerprint("127.0.0.1", server)
    return run(capsys, "add", "--name", name, "--server", "127.0.0.1", "--port", str(server), "--fingerprint", fp,
               "--datastore", "store1", "--namespace", ns, "--token-id", "u@pbs!t", "--backup-id", "host",
               "--source", "/etc", *extra)


def test_list_empty(env, capsys):
    code, out, _ = run(capsys, "list")
    assert code == 0 and "Keine Profile" in out


def test_add_weg_b_creates_profile_and_key(env, capsys, server):
    code, out, err = add_b(capsys, server)
    assert code == 0, err
    assert "✔ Namespace existiert" in out and "Verschlüsselungs-Schlüssel erzeugt" in out
    code, out, _ = run(capsys, "list")
    assert "Test" in out and "store1/a" in out and "ja" in out  # Verschlüsselung an
    code, out, _ = run(capsys, "key", "info", "Test")
    assert "Fingerprint aa:bb:cc:dd" in out
    dest = env / "key.json"
    assert run(capsys, "key", "export", "Test", str(dest))[0] == 0
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    assert run(capsys, "key", "export", "Test", str(dest))[0] == 1  # nie überschreiben


def test_add_weg_b_missing_namespace_blocked(env, capsys, server):
    code, _, err = add_b(capsys, server, ns="gibtsnicht")
    assert code == 2 and "nicht anlegen" in err
    assert "Keine Profile" in run(capsys, "list")[1]


def test_add_unknown_datastore(env, capsys, server):
    fp = fetch_fingerprint("127.0.0.1", server)
    code, _, err = run(capsys, "add", "--name", "x", "--server", "127.0.0.1", "--port", str(server), "--fingerprint", fp,
                       "--datastore", "nope", "--token-id", "u@pbs!t")
    assert code == 1 and "store1, store2" in err


def test_add_weg_a_provisions_nested_namespace(env, capsys, server):
    fp = fetch_fingerprint("127.0.0.1", server)
    code, out, err = run(capsys, "add", "--name", "A", "--server", "127.0.0.1", "--port", str(server),
                         "--fingerprint", fp, "--datastore", "store1", "--namespace", "a/neu", "--backup-id", "host",
                         "--no-encrypt")
    assert code == 0, err
    assert "neu angelegt" in out and "Zugang angelegt: pbsgui-host@pbs!backup" in out
    posts = [f for m, p, f, h in LOG if m == "POST" and p.endswith("/namespace")]
    assert posts == [{"name": "neu", "parent": "a"}] or posts[-1] == {"name": "neu", "parent": "a"}
    p = json.loads(run(capsys, "list", "--json")[1])[0]
    assert p["auth_id"] == "pbsgui-host@pbs!backup" and p["encrypt"] is False


def test_add_weg_a_existing_token_needs_flag(env, capsys, server):
    EXISTS.add("/access/users/pbsgui-host%40pbs/token/backup")
    fp = fetch_fingerprint("127.0.0.1", server)
    base = ["add", "--name", "A", "--server", "127.0.0.1", "--port", str(server), "--fingerprint", fp,
            "--datastore", "store1", "--backup-id", "host", "--no-encrypt"]
    code, _, err = run(capsys, *base)
    assert code == 3 and "--replace-token" in err
    assert run(capsys, *base, "--replace-token")[0] == 0


def test_add_duplicate_name_rejected(env, capsys, server):
    assert add_b(capsys, server)[0] == 0
    assert add_b(capsys, server)[0] == 1


def test_probe_lists_datastores_and_namespaces(env, capsys, server):
    fp = fetch_fingerprint("127.0.0.1", server)
    code, out, _ = run(capsys, "probe", "--server", "127.0.0.1", "--port", str(server), "--fingerprint", fp,
                       "--token-id", "u@pbs!t")
    assert code == 0 and "store1" in out and "└ a/b" in out


def test_backup_snapshots_restore(env, capsys, server):
    add_b(capsys, server)
    code, out, _ = run(capsys, "backup", "Test")
    assert code == 0 and "Backup erfolgreich" in out
    snaps = json.loads(run(capsys, "snapshots", "Test", "--json")[1])
    assert len(snaps) == 1 and snaps[0]["files"] == ["etc.pxar"]
    target = env / "restore"
    code, out, _ = run(capsys, "restore", "Test", "--target", str(target))
    assert code == 0 and (target / "restored.txt").exists()
    code, _, err = run(capsys, "restore", "Test", "--target", str(target))  # Ziel nicht leer
    assert code == 1 and "nicht leer" in err
    assert run(capsys, "restore", "Test", "--target", str(target), "--force")[0] == 0
    assert run(capsys, "restore", "Test", "--snapshot", "7", "--target", str(env / "x"))[0] == 1
    assert "OK" in run(capsys, "list")[1]  # Laufstatus erscheint


def test_schedule_and_status(env, capsys, server, monkeypatch):
    add_b(capsys, server)
    fake = FakeRun()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    code, out, _ = run(capsys, "schedule", "Test", "--calendar", "daily", "--require-ac")
    assert code == 0 and "Zeitplan aktiv (user)" in out
    assert ["systemctl", "--user", "enable", "--now", "pbsgui-test.timer"] in fake.calls
    assert "ConditionACPower=true" in (env / "xdg-c/systemd/user/pbsgui-test.service").read_text()
    code, out, _ = run(capsys, "status", "Test")
    assert "aktiv" in out
    assert run(capsys, "schedule", "Test", "--remove")[0] == 0
    assert not (env / "xdg-c/systemd/user/pbsgui-test.timer").exists()
    assert run(capsys, "schedule", "Test")[0] == 1  # weder --calendar noch --remove


def test_remove(env, capsys, server):
    add_b(capsys, server)
    assert run(capsys, "remove", "Test")[0] == 1  # ohne --yes und ohne TTY
    assert run(capsys, "remove", "Test", "--yes")[0] == 0
    assert "Keine Profile" in run(capsys, "list")[1]
    assert run(capsys, "show", "Test")[0] == 1


def test_secrets_not_accepted_as_args(env, capsys, server):
    with pytest.raises(SystemExit):
        main(["add", "--name", "x", "--server", "s", "--datastore", "d", "--password", "x"])
