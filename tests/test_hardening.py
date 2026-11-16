"""Absicherungen aus der Code-Durchsicht: Pfade aus dem Netz, Geheimnisse in der Umgebung, Web-Oberfläche, Master-Schlüssel."""
import json
import os
import socket
import threading
import time

import pytest

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core import rescue
from pbsgui.core.files import FilesError, copy_selection
from pbsgui.core.models import Profile, Snapshot, slugify
from pbsgui.core.versions import CatalogCache
from test_cli import add_b, env, run  # noqa: F401


@pytest.fixture(autouse=True)
def _no_pkexec(monkeypatch):
    from pbsgui.core.manager import ProfileManager
    monkeypatch.setattr(ProfileManager, "_admin", lambda self, request: None)


# -- Namen und Pfade ---------------------------------------------------------------------------------
@pytest.mark.parametrize("name", [".", "..", "...", "", "///"])
def test_slug_is_never_a_directory_reference(name):
    assert slugify(name) == "profile"


def test_slug_keeps_ordinary_names():
    assert slugify("Mein Laptop") == "mein-laptop" and slugify("v1.2") == "v1.2" and slugify(".config") == ".config"


@pytest.mark.parametrize("typ, bid", [("--keyfile=/etc/shadow", "pc"), ("host", "../../etc"), ("host", "-x"), ("host", "a/b"), ("host", "")])
def test_snapshot_with_bad_type_or_id_is_rejected(typ, bid):
    with pytest.raises(ValueError):
        Snapshot.from_json({"backup-type": typ, "backup-id": bid, "backup-time": 1})


def test_client_skips_bad_snapshots_from_the_server(fake_env, profile):
    from pbsgui.core.client import PbsClient
    fake_env.mkdir(parents=True, exist_ok=True)
    (fake_env / "snapshots.json").write_text(json.dumps([
        {"backup-type": "host", "backup-id": "laptop", "backup-time": 10, "files": []},
        {"backup-type": "--help", "backup-id": "laptop", "backup-time": 20, "files": []},
        {"backup-type": "host", "backup-id": "../x", "backup-time": 30, "files": []}]))
    assert [s.backup_time for s in PbsClient(profile, "geheim").list_snapshots()] == [10]


def test_catalog_cache_file_stays_in_its_directory(tmp_path, profile):
    snap = Snapshot("host", "../../../evil", 5)        # so nur denkbar, wenn die Prüfung in from_json umgangen würde
    f = CatalogCache(tmp_path / "cache")._file(profile, snap)
    assert f.parent == tmp_path / "cache" / profile.slug and "/" not in f.name


def test_copy_never_leaves_the_archive_or_the_target(tmp_path):
    src, outside, dest = tmp_path / "mnt", tmp_path / "geheim", tmp_path / "ziel"
    (src / "a").mkdir(parents=True)
    (src / "a" / "datei").write_text("x")
    outside.mkdir()
    (outside / "privat").write_text("lokal")
    (src / "link").symlink_to(outside)                 # Ordner-Link im Archiv, der auf diesen Rechner zeigt
    with pytest.raises(FilesError):
        copy_selection(src, ["/../geheim/privat"], dest)
    with pytest.raises(FilesError):
        copy_selection(src, ["/link/privat"], dest)       # würde eine lokale Datei statt einer aus dem Archiv lesen
    assert not dest.exists() or not any(dest.rglob("privat"))
    res = copy_selection(src, ["/a", "/link"], dest)       # der Link selbst wird als Link kopiert, nicht verfolgt
    assert res.copied == 2 and (dest / "link").is_symlink() and (dest / "a/datei").read_text() == "x"
    dest2 = tmp_path / "ziel2"
    dest2.mkdir()
    (dest2 / "a").symlink_to(outside)                      # im Zielordner liegt schon ein Link, der herausführt
    with pytest.raises(FilesError):
        copy_selection(src, ["/a"], dest2)
    assert not (outside / "datei").exists()


# -- Wiederherstellung: Geheimnisse und Datenträger --------------------------------------------------------
def test_secrets_reach_only_the_step_that_talks_to_the_server(monkeypatch, tmp_path):
    monkeypatch.setenv("PBSGUI_TOKEN_SECRET", "aus-der-umgebung")
    monkeypatch.setenv("PBSGUI_LUKS_PASSPHRASE", "luks-pw")
    seen = []

    class P:
        def __init__(self, argv, env=None, **kw):
            seen.append((argv[0], dict(env or {})))
            self.stdin, self.stdout = None, iter(())

        def wait(self):
            return 0
    cfg = rescue.RescueConfig(server="s", datastore="d", auth_id="u@pbs!t", token="geheim", fingerprint="AA", target_disk="/dev/sda")
    plan = rescue.Plan(steps=[rescue.Step("leeren", ["wipefs", "-a", "/dev/sda"])], cleanup=[rescue.Step("aus", ["umount", "/mnt"])])
    rec = rescue.RecoveryData([], {}, {}, {}, {}, {})
    snap = Snapshot("host", "pc", 5)
    assert rescue.execute(cfg, snap, rec, plan, lambda l: None, popen=P)
    by_tool = {tool: e for tool, e in seen}
    client = next(e for tool, e in seen if "restore" in tool or tool == os.environ.get("PBSGUI_CLIENT", "proxmox-backup-client"))
    assert client.get("PBS_PASSWORD") == "geheim"
    for tool in ("wipefs", "chroot", "umount", "sh"):
        assert "PBS_PASSWORD" not in by_tool[tool] and "PBSGUI_TOKEN_SECRET" not in by_tool[tool] and "PBSGUI_LUKS_PASSPHRASE" not in by_tool[tool]


def test_rescue_config_never_takes_secrets_from_a_file():
    cfg = rescue.RescueConfig.from_dict({"server": "s", "datastore": "d", "auth_id": "a", "token": "t", "passphrase": "p", "quatsch": 1})
    assert cfg.token == "" and cfg.passphrase == "" and cfg.server == "s"


def test_luks_header_is_removed_when_the_backup_is_no_longer_encrypted(tmp_path, profile):
    from pbsgui.core.recovery import collect_recovery
    dest = tmp_path / "rec"
    dest.mkdir()
    (dest / "luks-header-sda3.img").write_bytes(b"alter header mit schluessel-slots")
    profile.encrypt = False
    collect_recovery(dest, profile, run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(), which=lambda t: None)
    assert not list(dest.glob("luks-header-*.img"))


def test_vm_plan_passes_devices_as_arguments_not_as_shell_text():
    import inspect

    from pbsgui.core import vmimage
    text = inspect.getsource(vmimage)
    assert '"partprobe " +' not in text and 'f"partprobe {' not in text


# -- VPN und Datenbank -------------------------------------------------------------------------------------
def test_netbird_requires_https_and_openvpn_rejects_line_breaks(tmp_path, monkeypatch):
    from pbsgui.core import vpn
    with pytest.raises(vpn.VpnError):
        vpn.netbird_up("key", "http://netbird.example.org", run=lambda *a, **k: pytest.fail("nicht starten"))
    conf = tmp_path / "c.ovpn"
    conf.write_text("client\nremote x 1194\n")
    monkeypatch.setattr(vpn, "RUN_DIR", str(tmp_path / "run"))
    with pytest.raises(vpn.VpnError):
        vpn.openvpn_up(str(conf), "user", "pw\nzweite-zeile", run=lambda *a, **k: pytest.fail("nicht starten"))
    assert not (tmp_path / "run" / "auth.txt").exists()


def test_database_password_with_line_break_is_rejected():
    from pbsgui.core import dbdump
    db = dbdump.Database("/run/docker.sock", "root", "db", dbdump.MARIADB)
    with pytest.raises(dbdump.DumpError):
        dbdump.check_credentials(db, "root", "pw\nDROP DATABASE x;")


# -- Web-Oberfläche ----------------------------------------------------------------------------------------
def test_guard_log_strips_terminal_control_characters():
    from pbsgui.web import auth
    g = auth.Guard("pw")
    g.honeypot("1.2.3.4", "GET /admin\x1b[2J\x1b]0;titel\x07\r\nneue zeile")
    assert all("\x1b" not in e.detail and "\n" not in e.detail and "\r" not in e.detail and "\x07" not in e.detail for e in g.events)
    assert auth.clean_text("x" * 500) == "x" * 200


def test_session_and_csrf_checks_survive_non_ascii_input():
    from pbsgui.web import auth
    s = auth.Sessions()
    s.create("1.2.3.4")
    assert s.get("käse-ß", "1.2.3.4") is None


@pytest.fixture
def svc(tmp_path):
    from pbsgui.web.service import RescueService
    return RescueService(tmp_path / "work")


@pytest.mark.parametrize("name", ["../../etc/x", "/etc/x", "a/b", "wg 0"])
def test_wireguard_name_cannot_leave_the_work_directory(svc, tmp_path, monkeypatch, name):
    from pbsgui.core import vpn
    from pbsgui.web.service import ServiceError
    monkeypatch.setattr(vpn, "wireguard_up", lambda *a, **k: pytest.fail("nicht starten"))
    with pytest.raises(ServiceError):
        svc.vpn_up({"kind": "wireguard", "name": name, "config": "[Interface]\nPrivateKey = x\n"})
    assert not list(tmp_path.rglob("*.conf"))


def test_dry_run_keeps_the_access_for_the_real_run_and_a_real_run_clears_it(svc, monkeypatch):
    from pbsgui.web.service import ServiceError
    runs = []
    monkeypatch.setattr(rescue, "execute", lambda cfg, snap, rec, plan, log, dry_run=False: runs.append((dry_run, cfg.token)) or True)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    svc.cfg = rescue.RescueConfig(server="s", datastore="d", auth_id="a", token="geheim", fingerprint="AA")
    svc.snap, svc.rec, svc.plan, svc.target = Snapshot("host", "pc", 5), object(), rescue.Plan(), "/dev/sda"

    def wait():
        for _ in range(200):
            if svc.run.state != "running":
                return
            time.sleep(0.01)
    svc.start({"dry_run": True}); wait()
    assert svc.cfg.token == "geheim"                              # sonst würde der echte Lauf die Platte leeren und dann scheitern
    svc.start({"confirm": "/dev/sda"}); wait()
    assert runs == [(True, "geheim"), (False, "geheim")] and svc.cfg.token == ""
    with pytest.raises(ServiceError) as e:                        # ohne Zugang kein weiterer Lauf: erst neu verbinden
        svc.start({"confirm": "/dev/sda"})
    assert e.value.code == 409 and len(runs) == 2


def test_silent_connection_does_not_hold_a_slot_forever(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("openssl fehlt")
    from pbsgui.web import server as srv
    from pbsgui.web.service import RescueService
    monkeypatch.setattr(srv, "HANDSHAKE_TIMEOUT", 0.3)
    app = srv.App(passphrase="a1-b2-c3-d4", service=RescueService(tmp_path / "work"), sleep=lambda s: None, tarpit=(0, 0))
    web = srv.WebServer("127.0.0.1", 0, directory=tmp_path / "web", app=app, cert_names=["127.0.0.1"])
    t = threading.Thread(target=web.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        conns = [socket.create_connection(("127.0.0.1", web.port), timeout=5) for _ in range(3)]     # verbinden und schweigen
        time.sleep(1.0)
        for c in conns:
            c.settimeout(2)
            assert c.recv(1) == b""                                  # der Server hat die Verbindung geschlossen
            c.close()
    finally:
        web.httpd.shutdown(); web.httpd.server_close()


# -- Master-Schlüssel ----------------------------------------------------------------------------------------
def mgr_for(env):  # noqa: F811
    from pbsgui.core.manager import ProfileManager
    from pbsgui.core.paths import Paths
    return ProfileManager(Paths.for_scope("user"))


def test_master_key_is_not_left_behind_when_the_private_part_never_arrived(env, capsys, server, tmp_path):   # noqa: F811
    assert add_b(capsys, server)[0] == 0
    existing = tmp_path / "schon-da.pem"
    existing.write_text("x")
    code, _, err = run(capsys, "key", "master-create", "Test", "--output", str(existing), "--no-passphrase")
    assert code != 0 and "existiert" in err
    assert mgr_for(env).profiles()[0].master_pubkey == "" and existing.read_text() == "x"
    # Ziel ist ein Verzeichnis ohne Schreibrecht: erst nach dem Erzeugen scheitert das Speichern -> wieder ausgetragen
    locked = tmp_path / "zu"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("läuft als root")
        code, _, err = run(capsys, "key", "master-create", "Test", "--output", str(locked / "m.pem"), "--no-passphrase")
    finally:
        locked.chmod(0o700)
    p = mgr_for(env).profiles()[0]
    assert code != 0 and p.master_pubkey == "" and p.master_fingerprint == ""
    assert not list(mgr_for(env).master_dir().glob("master-public-*.pem"))


def test_discard_keeps_a_confirmed_master_key(env, capsys, server):   # noqa: F811
    assert add_b(capsys, server)[0] == 0
    mgr = mgr_for(env)
    p = mgr.profiles()[0]
    key = mgr.setup_master_key(p, "")
    mgr.confirm_master_key_backup(mgr.profiles()[0])
    assert mgr.discard_master_key(key.fingerprint) == 0 and mgr.profiles()[0].master_pubkey
    assert mgr.discard_master_key("") == 0


# -- Zweite Durchsicht: Schlüsseldateien, Profilnamen, LVM-Daten, Sperrlisten ----------------------------------------
def test_secret_files_do_not_follow_a_link(tmp_path):
    from pbsgui.core.keys import export_key, write_private
    victim = tmp_path / "fremd.txt"
    victim.write_text("bleibt")
    link = tmp_path / "key.json"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        export_key("geheim", str(link))
    assert victim.read_text() == "bleibt"
    dest = tmp_path / "neu.json"
    dest.write_text("alt")
    os.chmod(dest, 0o644)
    write_private(str(dest), "geheim")
    assert dest.read_text() == "geheim" and dest.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private(str(dest), "x", exclusive=True)


def test_a_second_profile_with_the_same_slug_is_recognised(env, capsys, server):   # noqa: F811
    assert add_b(capsys, server)[0] == 0
    mgr = mgr_for(env)
    name = mgr.profiles()[0].name
    assert mgr.name_taken(name) and mgr.name_taken(name.upper()) and mgr.name_taken(f" {name}!")      # derselbe Slug
    assert not mgr.name_taken("etwas-anderes")


@pytest.mark.parametrize("text", ["vg0 {", "vg0 { physical_volumes { pv0 { dev_size = ", "vg0 { extent_size = [1, 2",
                                  'vg0 { extent_size = 8192 physical_volumes { pv0 { pe_start = "x" pe_count = 1 dev_size = 1 } } }',
                                  "vg0 { physical_volumes { pv0 { } } }"])
def test_broken_lvm_metadata_is_an_ordinary_error(text):
    from pbsgui.core import shrink
    with pytest.raises(rescue.RescueError):
        shrink.read_vg(text, "vg0")


def test_guard_forgets_expired_addresses(monkeypatch):
    from pbsgui.web import auth
    now = [0.0]
    g = auth.Guard("richtig", clock=lambda: now[0], fails_before_lock=1, lock_seconds=10, honeypot_ban_seconds=10)
    monkeypatch.setattr(g, "MAX_TRACKED", 50)
    for i in range(200):
        g.check_login(f"10.0.{i // 250}.{i % 250}", "falsch")
        now[0] += 1000                                    # jede Sperre ist längst abgelaufen, wenn die nächste Adresse kommt
    assert len(g._locks) <= 51 and len(g._fails) <= 51
    g.honeypot("10.9.9.9", "/admin")
    assert g.blocked_for("10.9.9.9") > 0                  # eine laufende Sperre bleibt


def test_tui_refuses_to_save_a_secret_over_an_existing_file_or_into_nowhere(tmp_path):
    from pbsgui.tui.master import save_secret
    old = tmp_path / "da.pem"
    old.write_text("alt")
    for bad in ("", "   ", str(old)):
        with pytest.raises(ValueError):
            save_secret(bad, "geheim")
    with pytest.raises(OSError):
        save_secret(str(tmp_path / "gibts" / "nicht" / "k.pem"), "geheim")
    dest = save_secret(str(tmp_path / "neu.pem"), "geheim")
    assert dest.read_text() == "geheim" and dest.stat().st_mode & 0o777 == 0o600 and old.read_text() == "alt"


async def test_tui_confirmation_starts_on_no_and_takes_letters():
    from textual.widgets import Button
    from pbsgui.tui.app import PbsTui
    from pbsgui.tui.screens import ConfirmScreen
    got = []
    async with PbsTui().run_test(size=(120, 40)) as pilot:
        pilot.app.push_screen(ConfirmScreen("Wirklich?"), got.append)
        await pilot.pause()
        assert pilot.app.screen.focused is pilot.app.screen.query_one("#no", Button)
        await pilot.press("enter")                        # ein versehentliches Enter bestätigt nichts
        await pilot.pause()
        pilot.app.push_screen(ConfirmScreen("Wirklich?"), got.append)
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
    assert got == [False, True]


async def test_tui_add_dialog_rejects_a_bad_port_and_a_taken_name():
    from textual.widgets import Checkbox, Input, Static
    from pbsgui.tui.app import PbsTui
    from pbsgui.tui.screens import AddServerScreen
    async with PbsTui().run_test(size=(120, 50)) as pilot:
        scr = AddServerScreen(taken=lambda name: name == "Laptop")
        pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one("#server", Input).value = "pbs.example"
        scr.query_one("#port", Input).value = "abc"
        await scr._fetch_fp()                              # früher: ValueError erst im Hintergrund, bei „Prüfen“ ein Absturz
        assert "Port" in str(scr.query_one("#status", Static).render())
        scr.query_one("#fp", Static).update("AA:BB")
        scr.query_one("#fp-ok", Checkbox).value = True
        await scr._check()
        assert "Port" in str(scr.query_one("#status", Static).render())
        scr.query_one("#port", Input).value = "8007"
        scr.query_one("#name", Input).value = "Laptop"
        scr.query_one("#datastore", Input).value = "store1"
        await pilot.pause()
        await scr._setup()
        assert "schon ein Profil" in str(scr.query_one("#status", Static).render())
