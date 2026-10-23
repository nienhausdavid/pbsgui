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
from fake_docker_env import dock  # noqa: F401


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


def add_b(capsys, server, *extra, ns="a", name="Test", source=True):
    fp = fetch_fingerprint("127.0.0.1", server)
    return run(capsys, "add", "--name", name, "--server", "127.0.0.1", "--port", str(server), "--fingerprint", fp,
               "--datastore", "store1", "--namespace", ns, "--token-id", "u@pbs!t", "--backup-id", "host",
               *(["--source", "/etc"] if source else []), *extra)


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


def test_add_full_mode_needs_admin(env, capsys, server, monkeypatch):
    monkeypatch.setattr("pbsgui.cli.main.can_use_admin", lambda: False)
    code, _, err = add_b(capsys, server, "--mode", "full", source=False)
    assert code == 1 and "Administrator-Rechte" in err
    assert "Keine Profile" in run(capsys, "list")[1]


def test_add_full_mode_creates_system_profile(env, capsys, server, monkeypatch):
    monkeypatch.setattr("pbsgui.cli.main.can_use_admin", lambda: True)
    pushed = []
    monkeypatch.setattr(manager_mod.ProfileManager, "_admin", lambda self, req: pushed.append(req))
    code, out, err = add_b(capsys, server, "--mode", "full", source=False)
    assert code == 0, err
    assert "Vollständiges System" in out and "kein Systemabbild" in out
    p = json.loads(run(capsys, "list", "--json")[1])[0]
    assert p["backup_mode"] == "full" and p["scope"] == "system" and p["sources"] == ["/"]
    assert pushed and pushed[0]["profile"]["backup_mode"] == "full"  # System-Kopie wurde angefordert
    assert "System" in run(capsys, "list")[1]


def test_add_full_mode_rejects_sources(env, capsys, server, monkeypatch):
    monkeypatch.setattr("pbsgui.cli.main.can_use_admin", lambda: True)
    code, _, err = add_b(capsys, server, "--mode", "full", "--source", "/home", source=False)
    assert code == 1 and "--source" in err


@pytest.fixture
def full_env(env, monkeypatch, fake_mounts):
    monkeypatch.setattr("pbsgui.cli.main.can_use_admin", lambda: True)
    monkeypatch.setattr(manager_mod.ProfileManager, "_admin", lambda self, req: None)
    return env


def test_mounts_lists_with_defaults(full_env, capsys):
    code, out, _ = run(capsys, "mounts")
    assert code == 0
    row = {l.split()[0]: l for l in out.splitlines() if l.startswith("/")}
    assert "ja" in row["/boot"] and "ja" in row["/data"]
    assert "USB-Laufwerk" in row["/media/david/STICK"] and "nein" in row["/media/david/STICK"]  # USB nicht Standard
    assert "Duplikat" in row["/srv/dup"] and "nein" in row["/srv/dup"]


def test_add_full_default_mounts_and_summary(full_env, capsys, server):
    code, out, err = add_b(capsys, server, "--mode", "full", source=False)
    assert code == 0, err
    assert "Mitgesicherte Laufwerke: /boot, /data" in out and "Bind-Mount-Duplikate (übersprungen): /srv/dup" in out
    assert json.loads(run(capsys, "list", "--json")[1])[0]["full_mounts"] is None  # None = Standardauswahl


def test_add_full_explicit_usb_mount(full_env, capsys, server):
    code, out, err = add_b(capsys, server, "--mode", "full", "--mount", "/boot", "--mount", "/media/david/STICK",
                           source=False)
    assert code == 0, err
    assert "Mitgesicherte Laufwerke: /boot, /media/david/STICK" in out  # USB nur auf ausdrückliche Wahl
    assert json.loads(run(capsys, "list", "--json")[1])[0]["full_mounts"] == ["/boot", "/media/david/STICK"]


@pytest.mark.parametrize("bad", ["/nicht/da", "/srv/dup"])  # unbekannt und Bind-Duplikat
def test_add_full_rejects_unknown_or_duplicate_mount(full_env, capsys, server, bad):
    code, _, err = add_b(capsys, server, "--mode", "full", "--mount", bad, source=False)
    assert code == 1 and "Kein auswählbares Laufwerk" in err


def test_add_mount_requires_full_mode(full_env, capsys, server):
    code, _, err = add_b(capsys, server, "--mount", "/boot")
    assert code == 1 and "--mode full" in err


def test_mounts_set_auto_none(full_env, capsys, server):
    add_b(capsys, server, "--mode", "full", source=False)
    show = lambda: json.loads(run(capsys, "list", "--json")[1])[0]["full_mounts"]
    assert run(capsys, "mounts", "Test", "--set", "/data")[0] == 0 and show() == ["/data"]
    code, out, _ = run(capsys, "mounts", "Test", "--none")
    assert show() == [] and "keine (nur das Root-Dateisystem)" in out
    assert run(capsys, "mounts", "Test", "--auto")[0] == 0 and show() is None
    assert run(capsys, "mounts", "Test", "--set", "/nope")[0] == 1


def test_mounts_set_rejected_for_home_profile(env, capsys, server, fake_mounts):
    add_b(capsys, server)
    code, _, err = run(capsys, "mounts", "Test", "--set", "/boot")
    assert code == 1 and "Vollbackup" in err


def test_recovery_checklist_and_sheet(full_env, capsys, server, monkeypatch):
    add_b(capsys, server, "--mode", "full", source=False)
    code, out, _ = run(capsys, "recovery", "Test")
    assert code == 0 and "[ ] Verschlüsselungs-Schlüssel extern gesichert" in out and "PFLICHT" in out
    assert "gesperrt" in out
    code, _, err = run(capsys, "backup", "Test")
    assert code == 4  # Vollbackup ohne Schlüssel-Export
    sheet = env_path = full_env / "blatt.txt"
    code, out, _ = run(capsys, "recovery", "Test", "--sheet", str(sheet))
    assert code == 0 and "root.pxar" in sheet.read_text() and "geheim" not in sheet.read_text()
    assert oct(sheet.stat().st_mode & 0o777) == "0o600"
    assert run(capsys, "recovery", "Test", "--sheet", str(sheet))[0] == 1  # nie überschreiben
    key = full_env / "key.json"
    assert run(capsys, "key", "export", "Test", str(key))[0] == 0
    out = run(capsys, "recovery", "Test")[1]
    assert "[x] Verschlüsselungs-Schlüssel extern gesichert" in out and "gesperrt" not in out
    assert "Extern gesichert: ja" in run(capsys, "key", "info", "Test")[1]


# -- Zeitplan und Datenbank-Dumps beim Anlegen ------------------------------------------------------------------
def test_add_with_schedule_installs_timer(env, capsys, server, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    code, out, err = add_b(capsys, server, "--schedule", "00/8:00")
    assert code == 0, err
    assert "Zeitplan aktiv (user): 00/8:00" in out
    assert ["systemctl", "--user", "enable", "--now", "pbsgui-test.timer"] in fake.calls
    assert "OnCalendar=00/8:00" in (env / "xdg-c/systemd/user/pbsgui-test.timer").read_text()
    assert json.loads(run(capsys, "list", "--json")[1])[0]["schedule"] == "00/8:00"


def test_add_with_invalid_schedule_creates_nothing(env, capsys, server, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "_default_run", FakeRun(fail_calendar=True))
    code, _, err = add_b(capsys, server, "--schedule", "quatsch")
    assert code == 1 and "Zeitplan ungültig" in err
    assert "Keine Profile" in run(capsys, "list")[1]
    assert not any(m == "POST" and "/access" in path for m, path, _f, _h in LOG)   # kein Zugang angelegt


def test_full_backup_schedule_waits_for_key_export_then_activates(full_env, capsys, server, monkeypatch):
    pushed = []
    monkeypatch.setattr(manager_mod.ProfileManager, "_admin", lambda self, req: pushed.append(req))
    code, out, err = add_b(capsys, server, "--mode", "full", "--schedule", "daily", source=False)
    assert code == 0, err
    assert "ist vermerkt und startet, sobald der Schlüssel exportiert ist" in out
    assert not any(r.get("schedule") == "install" for r in pushed)               # noch nichts installiert
    assert json.loads(run(capsys, "list", "--json")[1])[0]["schedule"] == "daily"
    code, out, _ = run(capsys, "key", "export", "Test", str(full_env / "k.json"))
    assert code == 0 and "Zeitplan aktiv (system): daily" in out
    assert pushed[-1]["schedule"] == "install" and pushed[-1]["profile"]["schedule"] == "daily"


def test_full_backup_with_export_key_flag_activates_schedule_immediately(full_env, capsys, server, monkeypatch):
    pushed = []
    monkeypatch.setattr(manager_mod.ProfileManager, "_admin", lambda self, req: pushed.append(req))
    code, out, err = add_b(capsys, server, "--mode", "full", "--schedule", "daily", "--export-key",
                           str(full_env / "k.json"), source=False)
    assert code == 0, err
    assert "Zeitplan aktiv (system): daily" in out and pushed[-1]["schedule"] == "install"


def test_add_with_db_dump_flag(env, capsys, server):
    code, out, err = add_b(capsys, server, "--db-dump")
    assert code == 0, err
    assert "Datenbank-Dumps sind an" in out
    assert json.loads(run(capsys, "list", "--json")[1])[0]["db_dump"] is True


def test_dbdump_lists_databases_and_missing_access(env, capsys, server, dock):
    add_b(capsys, server)
    code, out, _ = run(capsys, "dbdump", "Test")
    assert code == 0
    assert "pg_main" in out and "kein Zugang nötig" in out
    assert "firefly_db" in out and "Zugang FEHLT" in out
    assert "web" not in out and "valkey" not in out                              # keine Datenbanken
    assert "Dumps vor jedem Backup: aus" in out


def test_dbdump_on_off(env, capsys, server, dock):
    add_b(capsys, server)
    assert "an" in run(capsys, "dbdump", "Test", "--on")[1]
    assert json.loads(run(capsys, "list", "--json")[1])[0]["db_dump"] is True
    run(capsys, "dbdump", "Test", "--off")
    assert json.loads(run(capsys, "list", "--json")[1])[0]["db_dump"] is False


def test_dbdump_set_access_verifies_and_stores_encrypted_store(env, capsys, server, dock, monkeypatch):
    add_b(capsys, server)
    dock.update(mysql_users={"root": "adminpw", "leser": "pw1"})
    monkeypatch.setenv("PBSGUI_DB_PASSWORD", "falsch")
    code, _, err = run(capsys, "dbdump", "Test", "--set", "firefly_db", "--user", "leser")
    assert code == 1 and "Access denied" in err
    monkeypatch.setenv("PBSGUI_DB_PASSWORD", "pw1")
    code, out, err = run(capsys, "dbdump", "Test", "--set", "firefly_db", "--user", "leser")
    assert code == 0, err
    assert "Zugang: leser" in run(capsys, "dbdump", "Test")[1]
    assert run(capsys, "dbdump", "Test", "--set", "firefly_db")[0] == 1          # --user fehlt
    assert run(capsys, "dbdump", "Test", "--set", "pg_main", "--user", "x")[0] == 1  # Postgres braucht keinen Zugang
    assert run(capsys, "dbdump", "Test", "--set", "gibtsnicht", "--user", "x")[0] == 1
    assert "entfernt" in run(capsys, "dbdump", "Test", "--remove", "firefly_db")[1]
    assert "Zugang FEHLT" in run(capsys, "dbdump", "Test")[1]


def test_dbdump_create_user_uses_admin_password_once(env, capsys, server, dock, monkeypatch):
    add_b(capsys, server)
    monkeypatch.setenv("PBSGUI_DB_ADMIN_PASSWORD", "adminpw")
    code, out, err = run(capsys, "dbdump", "Test", "--create-user", "firefly_db")
    assert code == 0, err
    assert "Dump-Benutzer" in out and "Zugang: pbsgui_dump" in run(capsys, "dbdump", "Test")[1]
    stored = (env / "xdg-c/pbsgui/creds").glob("test.dbdump*")
    assert "adminpw" not in "".join(p.read_text() for p in stored)
    assert "ALL PRIVILEGES" not in "".join(dock.data()["sql"])


def test_dbdump_run_writes_dumps_and_reports_failures(env, capsys, server, dock, tmp_path):
    add_b(capsys, server)
    dest = tmp_path / "dumps"
    code, out, _ = run(capsys, "dbdump", "Test", "--run", "--dest", str(dest))
    assert code == 0 and "2 gesichert, 1 übersprungen, 0 fehlgeschlagen" in out     # MariaDB ohne Zugang übersprungen
    assert len(list(dest.glob("*.postgres.sql.gz"))) == 2
    dock.update(fail=["pg_main"])
    code, out, _ = run(capsys, "dbdump", "Test", "--run", "--dest", str(dest))
    assert code == 1 and "1 fehlgeschlagen" in out


def test_dbdump_ambiguous_container_name(env, capsys, server, dock):
    add_b(capsys, server)
    state = dock.data()
    state["sockets"][dock.sockets[0]].append({"Id": "id-x", "Name": "/firefly_db", "Config": {"Image": "x"},
                                              "Mounts": [{"Destination": "/var/lib/mysql"}]})
    dock.path.write_text(json.dumps(state))
    code, _, err = run(capsys, "dbdump", "Test", "--remove", "firefly_db")
    assert code == 1 and "nicht eindeutig" in err
