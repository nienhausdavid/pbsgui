"""Datenbank-Dumps: Erkennung, Zugangsdaten, Dump-Lauf und Einbindung in Runner/Client (Fake-docker)."""
import json
import os
import socket
import time
from pathlib import Path

import pytest

from pbsgui.core import dbdump, manager as manager_mod
from pbsgui.core.dbdump import (MARIADB, POSTGRES, Database, DumpError, check_credentials, classify,
                                create_dump_user, discover_sockets, find_databases, parse_credentials, remove_stale,
                                run_dumps)
from pbsgui.core.manager import ProfileManager
from pbsgui.core.runner import run_profile
from pbsgui.core.scheduler import render_service
from pbsgui.core.secrets import FileStore
from fake_docker_env import container, dock  # noqa: F401

def read_dump(path):
    return Path(path).read_text()


# -- Erkennung ----------------------------------------------------------------------------------------------
def test_classify_uses_data_mounts_not_names():
    assert classify(container("x", "/var/lib/postgresql/data", image="irgendwas")) == POSTGRES
    assert classify(container("x", "/var/lib/postgresql", image="irgendwas")) == POSTGRES
    assert classify(container("x", "/var/lib/mysql", image="irgendwas")) == MARIADB
    assert classify(container("x", "/data", image="postgres:16")) is None   # Name allein reicht nicht
    assert classify(container("x", "/data", image="valkey/valkey:9")) is None
    assert classify(container("postgres", image="postgres:16")) is None    # ohne Datenverzeichnis nichts zu sichern


def test_discover_sockets_finds_all_daemons_and_dedups(tmp_path):
    (tmp_path / "u1000").mkdir()
    (tmp_path / "u1001").mkdir()
    socks = []
    for p in (tmp_path / "u1000/docker.sock", tmp_path / "u1001/docker.sock", tmp_path / "root.sock"):
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(p))
        socks.append(s)
    (tmp_path / "link.sock").symlink_to(tmp_path / "root.sock")      # Duplikat wie /var/run -> /run
    (tmp_path / "u1000/regular.sock").write_text("keine Socket-Datei")
    found = discover_sockets([str(tmp_path / "u*/docker.sock"), str(tmp_path / "root.sock"),
                              str(tmp_path / "link.sock"), str(tmp_path / "u1000/regular.sock")])
    assert found == [str(tmp_path / "u1000/docker.sock"), str(tmp_path / "u1001/docker.sock"), str(tmp_path / "root.sock")]
    for s in socks:
        s.close()


def test_find_databases_over_all_sockets(dock):
    lines = []
    dbs = find_databases(dock.sockets, lines.append)
    assert sorted((d.container, d.engine) for d in dbs) == [
        ("firefly_db", MARIADB), ("immich_postgres", POSTGRES), ("pg_main", POSTGRES)]
    assert not lines


def test_unreachable_socket_is_reported_not_fatal(dock, tmp_path):
    lines = []
    dbs = find_databases([str(tmp_path / "weg.sock"), *dock.sockets], lines.append)
    assert len(dbs) == 3 and any("nicht erreichbar" in l for l in lines)


def test_missing_docker_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PBSGUI_DOCKER", "/nicht/da")
    with pytest.raises(DumpError, match="nicht gefunden"):
        find_databases([str(tmp_path / "x.sock")])


# -- Dumps ---------------------------------------------------------------------------------------------------
def test_run_dumps_postgres_without_secret_and_mariadb_needs_credentials(dock, tmp_path):
    out, lines = tmp_path / "dumps", []
    res = run_dumps(out, {}, lines.append, dock.sockets)
    assert sorted(d.container for d in res.ok) == ["immich_postgres", "pg_main"]
    assert [d.container for d, _ in res.skipped] == ["firefly_db"]               # kein Zugang -> nicht erraten
    names = sorted(p.name for p in out.iterdir())
    assert any(n.endswith("__immich_postgres.postgres.sql") for n in names) and len(names) == 2
    assert "fake postgres dump of immich_postgres" in read_dump(next(p for p in out.iterdir() if "immich" in p.name))
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in out.iterdir())
    assert any("kein Zugang" in l for l in lines)


def test_mariadb_dump_with_credentials_passes_password_via_stdin_only(dock, tmp_path):
    dock.update(mysql_users={"pbsgui_dump": "geheimes-pw"})
    creds = {next(d.key for d in find_databases(dock.sockets) if d.container == "firefly_db"):
             {"user": "pbsgui_dump", "password": "geheimes-pw"}}
    res = run_dumps(tmp_path / "d", creds, lambda _l: None, dock.sockets)
    assert len(res.ok) == 3 and not res.failed
    sql = read_dump(next((tmp_path / "d").glob("*firefly_db.mariadb.sql")))
    assert "fake mysql dump of firefly_db as pbsgui_dump" in sql
    assert "geheimes-pw" not in json.dumps(dock.data()["calls"])                  # nie in der Kommandozeile


def test_failed_dump_keeps_previous_good_dump(dock, tmp_path):
    out = tmp_path / "d"
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    target = next(out.glob("*immich_postgres.postgres.sql"))
    before = read_dump(target)
    dock.update(fail=["immich_postgres"])
    lines = []
    res = run_dumps(out, {}, lines.append, dock.sockets)
    assert [d.container for d, _ in res.failed] == ["immich_postgres"]
    assert read_dump(target) == before                                            # alter Dump bleibt
    assert not list(out.glob(".dump-*"))                                          # keine Temp-Reste
    assert any("letzter Dump bleibt" in l for l in lines)


def test_wrong_password_fails_without_touching_other_dumps(dock, tmp_path):
    key = next(d.key for d in find_databases(dock.sockets) if d.container == "firefly_db")
    res = run_dumps(tmp_path / "d", {key: {"user": "pbsgui_dump", "password": "falsch"}}, lambda _l: None, dock.sockets)
    assert [d.container for d, _ in res.failed] == ["firefly_db"] and len(res.ok) == 2
    assert "Access denied" in res.failed[0][1]


def test_no_databases_is_fine(dock, tmp_path):
    lines = []
    res = run_dumps(tmp_path / "d", {}, lines.append, [])
    assert not res.ok and any("keine laufenden" in l for l in lines)


# -- Zugang einrichten ------------------------------------------------------------------------------------------
def test_create_dump_user_uses_admin_once_and_grants_read_only(dock):
    db = next(d for d in find_databases(dock.sockets) if d.engine == MARIADB)
    cred = create_dump_user(db, "root", "adminpw")
    assert cred["user"] == "pbsgui_dump" and len(cred["password"]) >= 24
    sql = dock.data()["sql"][0]
    assert "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON *.*" in sql
    assert "ALL PRIVILEGES" not in sql and "GRANT OPTION" not in sql
    assert "adminpw" not in json.dumps(dock.data()["calls"])                      # Admin-Passwort nie in argv
    check_credentials(db, cred["user"], cred["password"])                         # neuer Benutzer funktioniert


def test_check_credentials_rejects_wrong_password(dock):
    db = next(d for d in find_databases(dock.sockets) if d.engine == MARIADB)
    with pytest.raises(DumpError, match="Access denied"):
        check_credentials(db, "root", "falsch")


def test_parse_credentials_tolerates_garbage():
    assert parse_credentials(None) == {} and parse_credentials("kein json") == {} and parse_credentials("[1]") == {}
    assert parse_credentials(json.dumps({"a/b": {"user": "u", "password": "p"}, "c": {"user": "x"}, "d": 1})) == {
        "a/b": {"user": "u", "password": "p"}}


# -- Manager, Runner, Client --------------------------------------------------------------------------------------
@pytest.fixture
def mgr(tmp_path, monkeypatch, profile, fake_env):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-s"))
    monkeypatch.setattr(manager_mod, "best_store", lambda scope, d: FileStore(d))
    m = ProfileManager()
    m.save(profile, token="geheim")
    return m


def test_manager_stores_credentials_encrypted_store_and_unit_loads_them(mgr, profile, dock):
    db = next(d for d in find_databases(dock.sockets) if d.engine == MARIADB)
    dock.update(mysql_users={"root": "adminpw", "u1": "p1"})
    mgr.set_db_credential(profile, db, "u1", "p1")
    assert mgr.db_credentials(profile) == {db.key: {"user": "u1", "password": "p1"}}
    assert "LoadCredential=dbdump:" in render_service(profile, mgr.secrets, "user")
    with pytest.raises(DumpError):                                                # falsches Passwort: nichts gespeichert
        mgr.set_db_credential(profile, db, "u1", "falsch")
    assert mgr.db_credentials(profile)[db.key]["password"] == "p1"
    assert mgr.remove_db_credential(profile, db.key) and mgr.db_credentials(profile) == {}
    assert not mgr.remove_db_credential(profile, db.key)


def test_manager_create_db_credential_stores_new_user_not_admin(mgr, profile, dock):
    db = next(d for d in find_databases(dock.sockets) if d.engine == MARIADB)
    mgr.create_db_credential(profile, db, "root", "adminpw")
    cred = mgr.db_credentials(profile)[db.key]
    assert cred["user"] == "pbsgui_dump" and "adminpw" not in mgr.secrets.get(profile.slug, "dbdump")


def test_run_profile_dumps_and_adds_archive_and_never_blocks_backup(mgr, profile, dock, fake_env, paths, monkeypatch):
    monkeypatch.setattr(dbdump, "discover_sockets", lambda *a, **k: dock.sockets)  # nie die echten Sockets des Hosts
    profile.db_dump = True
    lines = []
    assert run_profile(profile, paths, mgr.secrets, on_line=lines.append) == 0
    call = json.loads((fake_env / "calls.jsonl").read_text().splitlines()[-1])
    spec = next(a for a in call["argv"] if a.startswith("dbdumps.pxar:"))
    assert Path(spec.split(":", 1)[1]) == paths.state / "dumps" / profile.slug
    assert any("Gesichert: " in l for l in lines) and any("kein Zugang" in l for l in lines)


def test_run_profile_backup_runs_even_if_docker_is_missing(mgr, profile, fake_env, paths, monkeypatch):
    monkeypatch.setenv("PBSGUI_DOCKER", "/nicht/da")
    monkeypatch.setattr(dbdump, "discover_sockets", lambda *a, **k: ["/run/x.sock"])
    profile.db_dump = True
    lines = []
    assert run_profile(profile, paths, mgr.secrets, on_line=lines.append) == 0
    assert any("nicht ausgeführt" in l or "nicht gefunden" in l for l in lines)
    call = json.loads((fake_env / "calls.jsonl").read_text().splitlines()[-1])
    assert not any(a.startswith("dbdumps.pxar:") for a in call["argv"])           # nichts zu sichern, kein leeres Archiv


def test_profile_without_db_dump_never_touches_docker(mgr, profile, fake_env, paths, monkeypatch):
    monkeypatch.setenv("PBSGUI_DOCKER", "/nicht/da")
    assert profile.db_dump is False
    assert run_profile(profile, paths, mgr.secrets, on_line=lambda _l: None) == 0


# -- Aufräumen alter Dumps ----------------------------------------------------------------------------------------
def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_dumps_are_overwritten_not_accumulated(dock, tmp_path):
    out = tmp_path / "d"
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    first = sorted(p.name for p in out.iterdir())
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert sorted(p.name for p in out.iterdir()) == first                       # gleiche Dateinamen, nichts wächst


def test_stale_dump_of_vanished_container_is_removed_after_14_days(dock, tmp_path):
    out = tmp_path / "d"
    out.mkdir()
    old, young, foreign = out / "root__weg.postgres.sql", out / "root__neu_weg.postgres.sql", out / "notiz.txt"
    for f in (old, young, foreign):
        f.write_text("x")
    age(old, 20); age(young, 3); age(foreign, 400)
    lines = []
    res = run_dumps(out, {}, lines.append, dock.sockets)
    assert res.removed == ["root__weg.postgres.sql"] and not old.exists()
    assert young.exists() and foreign.exists()                                  # jung bzw. keine Dump-Datei
    assert any("Entfernt" in l and "weg.postgres" in l for l in lines)


def test_old_dump_of_detected_container_is_never_removed_even_when_failing(dock, tmp_path):
    out = tmp_path / "d"
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    target = next(out.glob("*immich_postgres.postgres.sql"))
    age(target, 90)
    dock.update(fail=["immich_postgres"])
    res = run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert target.exists() and res.removed == []                                 # letzter guter Stand bleibt


def test_remove_stale_without_directory(tmp_path):
    assert remove_stale(tmp_path / "gibtsnicht", []) == []


def test_stopped_container_dump_goes_after_14_days(dock, tmp_path):
    out = tmp_path / "d"
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    pg = next(out.glob("*pg_main.postgres.sql"))
    state = dock.data()
    state["sockets"][dock.sockets[0]] = [c for c in state["sockets"][dock.sockets[0]] if c["Name"] != "/pg_main"]
    dock.path.write_text(json.dumps(state))                                      # Container gestoppt/entfernt
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert pg.exists()                                                           # noch keine 14 Tage
    age(pg, 15)
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert not pg.exists()


# -- Unkomprimierte Dumps (PBS komprimiert und dedupliziert selbst) -------------------------------------------------------
def test_dumps_are_plain_text_not_gzip(dock, tmp_path):
    out = tmp_path / "d"
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    f = next(out.glob("*immich_postgres.postgres.sql"))
    raw = f.read_bytes()
    assert not raw.startswith(b"\x1f\x8b") and raw.startswith(b"-- fake postgres dump")        # kein gzip-Strom
    assert not list(out.glob("*.gz"))


def test_new_plain_dump_replaces_the_old_gzip_dump(dock, tmp_path):
    out = tmp_path / "d"
    out.mkdir()
    old = out / next(d for d in find_databases(dock.sockets) if d.container == "immich_postgres").legacy_filename
    old.write_bytes(b"\x1f\x8b alter gzip-Dump")
    run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert not old.exists() and (out / old.name.removesuffix(".gz")).exists()


def test_old_gzip_dump_stays_when_the_new_dump_fails(dock, tmp_path):
    out = tmp_path / "d"
    out.mkdir()
    old = out / next(d for d in find_databases(dock.sockets) if d.container == "immich_postgres").legacy_filename
    old.write_bytes(b"\x1f\x8b alter gzip-Dump")
    age(old, 60)                                                                   # sehr alt, aber der letzte gute Stand
    dock.update(fail=["immich_postgres"])
    res = run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert [d.container for d, _ in res.failed] == ["immich_postgres"]
    assert old.exists() and res.removed == []


def test_stale_cleanup_handles_both_name_forms(dock, tmp_path):
    out = tmp_path / "d"
    out.mkdir()
    gone_gz, gone_sql = out / "root__weg.postgres.sql.gz", out / "root__weg2.postgres.sql"
    for f in (gone_gz, gone_sql):
        f.write_text("x")
        age(f, 30)
    res = run_dumps(out, {}, lambda _l: None, dock.sockets)
    assert sorted(res.removed) == ["root__weg.postgres.sql.gz", "root__weg2.postgres.sql"]
