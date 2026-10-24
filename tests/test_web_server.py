"""Web-Oberfläche der Rettungs-ISO: echter HTTPS-Server auf localhost, Fake-Client und abgefangene Ausführung."""
import hashlib
import http.client
import json
import shutil
import ssl
import threading
import time

import pytest

from pbsgui.web import auth, server as srv
from pbsgui.web.service import RescueService
from test_rescue_tui import lab  # noqa: F401  (Fake-Client, Snapshots, abgefangene Ausführung)
from test_rescue import snap, put_snapshots  # noqa: F401
from test_rescue_shrink import rec_dir  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl fehlt")
PASS = "tisch47-wolke12-kerze83-anker66"


@pytest.fixture
def web(tmp_path, monkeypatch):
    app = srv.App(passphrase=PASS, service=RescueService(tmp_path / "work"), sleep=lambda s: None, tarpit=(0, 0),
                  guard_kw={"fails_before_lock": 3, "lock_seconds": 60})
    server = srv.WebServer("127.0.0.1", 0, directory=tmp_path / "web", app=app, cert_names=["127.0.0.1"])
    t = threading.Thread(target=server.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield server
    server.httpd.shutdown(); server.httpd.server_close()


class Client:
    def __init__(self, server):
        self.port, self.cookie, self.csrf = server.port, "", ""
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def req(self, method, path, body=None, raw=None, headers=None):
        c = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ctx, timeout=10)
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
            if self.csrf:
                h.setdefault("X-CSRF-Token", self.csrf)
        if data is not None:
            h["Content-Length"] = str(len(data))
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        out = {}
        if "json" in (r.getheader("Content-Type") or ""):
            out = json.loads(text)
        c.close()
        return r.status, r, out if out or "json" in (r.getheader("Content-Type") or "") else text

    def login(self, pw=PASS, **extra):
        status, r, d = self.req("POST", "/api/login", {"password": pw, **extra})
        if status == 200:
            self.cookie = r.getheader("Set-Cookie").split(";")[0]
            self.csrf = d["csrf"]
        return status, r, d


def test_index_has_csp_nonce_honeypot_field_and_no_server_version(web):
    status, r, html = Client(web).req("GET", "/")
    assert status == 200 and "pbsgui" in html and 'name="website"' in html
    csp = r.getheader("Content-Security-Policy")
    nonce = html.split('<script nonce="')[1].split('"')[0]
    assert f"'nonce-{nonce}'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.getheader("Server") == "pbsgui" and "Python" not in r.getheader("Server")
    assert r.getheader("X-Frame-Options") == "DENY" and r.getheader("Cache-Control") == "no-store"
    assert "__NONCE__" not in html and "__HP__" not in html


def test_served_certificate_matches_announced_fingerprint(web):
    import socket
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", web.port)) as s, ctx.wrap_socket(s) as t:
        der = t.getpeercert(binary_form=True)
        version = t.version()
    h = hashlib.sha256(der).hexdigest().upper()
    assert ":".join(h[i:i + 2] for i in range(0, len(h), 2)) == web.fingerprint
    assert version in ("TLSv1.3", "TLSv1.2")


def test_login_cookie_flags_and_session_requirements(web):
    c = Client(web)
    assert c.req("GET", "/api/status")[0] == 401                       # ohne Anmeldung nichts
    assert c.login("falsch")[0] == 401
    status, r, d = c.login()
    cookie = r.getheader("Set-Cookie")
    assert status == 200 and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie
    assert c.req("GET", "/api/status")[0] == 200
    c.csrf = ""                                                        # ohne CSRF-Token wird jede Änderung abgelehnt
    assert c.req("POST", "/api/fingerprint", {"server": "x"})[0] == 403
    c.csrf = "falsch"
    assert c.req("POST", "/api/fingerprint", {"server": "x"})[0] == 403


def test_session_is_bound_to_one_login_new_login_replaces_old(web):
    a, b = Client(web), Client(web)
    a.login(); b.login()
    assert b.req("GET", "/api/status")[0] == 200
    assert a.req("GET", "/api/status")[0] == 401


def test_filling_the_hidden_field_is_a_bot_and_gets_banned_even_with_the_right_password(web):
    c = Client(web)
    status, _, _ = c.login(PASS, website="http://spam.example")
    assert status == 401
    assert c.login()[0] == 403                                         # die Adresse ist jetzt gesperrt
    kinds = [e.kind for e in web.app.guard.events]
    assert "honeypot" in kinds and "ban" in kinds


@pytest.mark.parametrize("path", ["/wp-login.php", "/admin", "/.env", "/phpmyadmin/"])
def test_decoy_paths_look_real_but_ban_the_visitor(web, path):
    c = Client(web)
    status, _, html = c.req("GET", path)
    assert status == 200 and "Anmeldung" in html                       # glaubhafter Köder
    assert c.req("GET", "/")[0] == 403 and c.login()[0] == 403         # danach ist alles gesperrt
    status, _, html = Client(web).req("POST", path, raw=b"user=a&pass=b", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status in (200, 403)


def test_wrong_passwords_lock_the_address(web):
    c = Client(web)
    assert [c.login("nein")[0] for _ in range(3)] == [401, 401, 401]
    status, _, d = c.login("nein")
    assert status == 429 and "gesperrt" in d["error"]
    assert c.login(PASS)[0] == 429                                     # auch das richtige Passwort wird jetzt abgewiesen


def test_too_many_failures_overall_switch_the_ui_off(web, monkeypatch):
    monkeypatch.setattr(auth.Guard, "GLOBAL_FAIL_LIMIT", 4)
    web.app.guard.fails_before_lock = 1000
    c = Client(web)
    for _ in range(4):
        c.login("nein")
    assert web.app.stop_event.is_set() and "Fehlversuche" in web.app.stop_reason
    assert c.login(PASS)[0] == 503


def test_request_rate_limit_returns_429(tmp_path):
    app = srv.App(passphrase=PASS, service=RescueService(tmp_path / "w"), sleep=lambda s: None, tarpit=(0, 0),
                  guard_kw={"request_limit": 5, "request_window": 60})
    s = srv.WebServer("127.0.0.1", 0, directory=tmp_path / "web", app=app, cert_names=[])
    threading.Thread(target=s.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        c = Client(s)
        codes = [c.req("GET", "/")[0] for _ in range(8)]
        assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
    finally:
        s.httpd.shutdown(); s.httpd.server_close()


def test_body_limits_and_content_type(web):
    c = Client(web); c.login()
    assert c.req("POST", "/api/connect", raw=b"x" * (srv.MAX_BODY + 1), headers={"Content-Type": "application/json", "X-CSRF-Token": c.csrf})[0] == 413
    assert c.req("POST", "/api/connect", raw=b"a=b", headers={"Content-Type": "text/plain", "X-CSRF-Token": c.csrf})[0] == 415
    assert c.req("POST", "/api/connect", raw=b"{kaputt", headers={"Content-Type": "application/json", "X-CSRF-Token": c.csrf})[0] == 400
    assert c.req("POST", "/api/connect", raw=b"[1]", headers={"Content-Type": "application/json", "X-CSRF-Token": c.csrf})[0] == 400


def test_internal_errors_do_not_leak_details(web, monkeypatch):
    c = Client(web); c.login()
    monkeypatch.setattr(web.app.service, "status", lambda: 1 / 0)
    status, _, d = c.req("GET", "/api/status")
    assert status == 500 and d == {"error": "Interner Fehler"}


def test_unknown_methods_and_paths(web):
    c = Client(web); c.login()
    assert c.req("DELETE", "/api/status")[0] == 405
    assert c.req("GET", "/etc/passwd")[0] == 404
    assert c.req("GET", "/api/gibts-nicht")[0] == 404


def test_full_restore_flow_over_http_with_typed_confirmation(web, lab, monkeypatch):    # noqa: F811
    c = Client(web); c.login()
    conn = {"server": "pbs.example", "port": 8007, "datastore": "store1", "auth_id": "u@pbs!t", "token": "geheim",
            "fingerprint": "AA:BB", "backup_id": "", "namespace": ""}
    assert c.req("POST", "/api/connect", {**conn, "token": ""})[0] == 400                   # Pflichtfelder
    assert c.req("POST", "/api/connect", {**conn, "datastore": "../x;rm"})[0] == 400        # Zeichen geprüft
    status, _, d = c.req("POST", "/api/connect", conn)
    assert status == 200 and len(d["snapshots"]) == 2
    newest = max(d["snapshots"], key=lambda s: s["time"])
    status, _, d = c.req("POST", "/api/choose", {"path": newest["path"]})
    assert status == 200 and {x["path"] for x in d["disks"]} == {"/dev/sda", "/dev/sdb"} and d["original_size"] > 0
    assert c.req("POST", "/api/plan", {"disk": "/etc/passwd"})[0] == 400
    status, _, d = c.req("POST", "/api/plan", {"disk": "/dev/sda", "encryption": "none"})
    assert status == 200 and d["summary"] and d["disk"] == "/dev/sda"
    # Ohne richtige Bestätigung geht nichts los
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert c.req("POST", "/api/start", {"confirm": "/dev/sdb"})[0] == 403
    assert not lab
    assert c.req("POST", "/api/start", {"confirm": "/dev/sda"})[0] == 200
    for _ in range(100):
        status, _, d = c.req("GET", "/api/log?since=0")
        if d["state"] != "running":
            break
        time.sleep(0.05)
    assert d["state"] == "ok" and "ok" in d["lines"]
    assert lab and lab[0][0] == "/dev/sda" and lab[0][1] is False                           # wirklich ausgeführt (nicht Trockenlauf)
    assert web.app.service.cfg.token == ""                                                  # Geheimnis nach dem Lauf gelöscht
    assert any("Wiederherstellung fertig" in e.detail for e in web.app.guard.events)


def test_dry_run_needs_no_root_and_no_confirmation(web, lab):    # noqa: F811
    c = Client(web); c.login()
    conn = {"server": "pbs.example", "datastore": "store1", "auth_id": "u@pbs!t", "token": "geheim", "fingerprint": "AA:BB"}
    d = c.req("POST", "/api/connect", conn)[2]
    c.req("POST", "/api/choose", {"path": d["snapshots"][0]["path"]})
    assert c.req("POST", "/api/plan", {"disk": "/dev/sda", "encryption": "none"})[0] == 200
    assert c.req("POST", "/api/start", {"dry_run": True})[0] == 200
    for _ in range(100):
        d = c.req("GET", "/api/log")[2]
        if d["state"] != "running":
            break
        time.sleep(0.05)
    assert d["state"] == "ok" and d["dry_run"] and lab[0][1] is True


def test_vpn_endpoint_validates_input_and_never_echoes_secrets(web, monkeypatch):
    from pbsgui.core import vpn
    c = Client(web); c.login()
    assert c.req("POST", "/api/vpn/up", {"kind": "wireguard", "config": "kein wireguard"})[0] == 400
    assert c.req("POST", "/api/vpn/up", {"kind": "gibtsnicht"})[0] == 400
    calls = []
    monkeypatch.setattr(vpn, "netbird_up", lambda key, url, run=None: calls.append((key, url)) or vpn.Result(True, "ok"))
    status, _, d = c.req("POST", "/api/vpn/up", {"kind": "netbird", "setup_key": "SUPER-GEHEIM", "management_url": "https://nb.example"})
    assert status == 200 and calls == [("SUPER-GEHEIM", "https://nb.example")] and "SUPER-GEHEIM" not in json.dumps(d)


def test_key_upload_is_validated_and_stored_0600(web):
    import os, stat
    c = Client(web); c.login()
    assert c.req("POST", "/api/key", {"content": "kein json"})[0] == 400
    assert c.req("POST", "/api/key", {"content": '{"a": 1}'})[0] == 400
    assert c.req("POST", "/api/key", {"content": json.dumps({"kdf": "none", "fingerprint": "aa:bb"})})[0] == 200
    p = web.app.service.workdir / "key.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and web.app.service.keyfile == str(p)


def test_idle_timeout_stops_the_server_unless_a_restore_runs(tmp_path):
    t = [0.0]
    app = srv.App(passphrase=PASS, service=RescueService(tmp_path / "w"), idle_minutes=1, clock=lambda: t[0])
    assert not app.idle_expired()
    t[0] = 61
    assert app.idle_expired()


def test_cli_web_passes_leading_options_through(monkeypatch):
    from pbsgui.cli import main as cli
    seen = []
    monkeypatch.setattr(srv, "run", lambda argv=None: seen.append(argv) or 0)
    assert cli.main(["web", "--daemon", "--bind", "10.0.0.5", "--timeout", "30"]) == 0
    assert seen == [["--daemon", "--bind", "10.0.0.5", "--timeout", "30"]]


def test_vm_mode_flow_maps_disks_and_needs_all_devices_confirmed(web, fake_env, tmp_path, monkeypatch):
    from pbsgui.core import rescue as rescue_mod, vmimage
    from test_vmimage import CONF, vmsnap
    put_snapshots(fake_env, [vmsnap(100), vmsnap(50, vmid="101")])
    conf = tmp_path / "c.conf"; conf.write_text(CONF)
    monkeypatch.setenv("FAKE_PBS_VMCONF", str(conf))
    monkeypatch.setattr(rescue_mod, "candidate_disks", lambda lsblk_json=None: [
        {"path": "/dev/sda", "size": 64 * 2**30, "model": "A", "tran": "sata", "removable": False},
        {"path": "/dev/sdb", "size": 200 * 2**30, "model": "B", "tran": "sata", "removable": False}])
    ran = []
    monkeypatch.setattr(vmimage, "execute", lambda cfg, plan, on_line, dry_run=False: ran.append((sorted(plan.mapping.items()), dry_run))
                        or on_line("vm fertig") or True)
    c = Client(web); c.login()
    conn = {"server": "pbs.example", "datastore": "store1", "auth_id": "u@pbs!t", "token": "geheim", "fingerprint": "AA:BB",
            "backup_id": "100", "mode": "vm"}
    assert c.req("POST", "/api/connect", {**conn, "mode": "irgendwas"})[0] == 400
    status, _, d = c.req("POST", "/api/connect", conn)
    assert status == 200 and d["mode"] == "vm" and len(d["snapshots"]) == 1
    assert [x["key"] for x in d["snapshots"][0]["disks"]] == ["scsi0", "scsi1"]
    status, _, d = c.req("POST", "/api/choose", {"path": d["snapshots"][0]["path"]})
    assert status == 200 and d["vm"] and d["name"] == "webserver" and d["uefi"] and [x["boot"] for x in d["disks"]] == [True, False]
    assert {t["path"] for t in d["targets"]} == {"/dev/sda", "/dev/sdb"}
    assert c.req("POST", "/api/plan", {"mapping": {"scsi0": "/etc/passwd"}})[0] == 400
    assert c.req("POST", "/api/plan", {"mapping": {"scsi0": "/dev/sda", "scsi1": "/dev/sda"}})[0] == 400
    status, _, d = c.req("POST", "/api/plan", {"mapping": {"scsi0": "/dev/sda", "scsi1": "/dev/sdb"},
                                               "repair": {"network": False}})
    assert status == 200 and d["vm"] and d["disk"] == "/dev/sda /dev/sdb"
    assert any("Netzwerknamen anpassen: nein" in l for l in d["summary"]) and any("scsi1" in l for l in d["summary"])
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert c.req("POST", "/api/start", {"confirm": "/dev/sda"})[0] == 403                   # nur eine der zwei Platten bestätigt
    assert not ran
    assert c.req("POST", "/api/start", {"confirm": "/dev/sdb /dev/sda"})[0] == 200
    for _ in range(100):
        d = c.req("GET", "/api/log")[2]
        if d["state"] != "running":
            break
        time.sleep(0.05)
    assert d["state"] == "ok" and "vm fertig" in d["lines"] and ran == [([("scsi0", "/dev/sda"), ("scsi1", "/dev/sdb")], False)]
