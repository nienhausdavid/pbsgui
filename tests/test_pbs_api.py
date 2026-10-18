"""PBS-API-Client gegen einen lokalen TLS-Testserver (selbstsigniertes Zertifikat)."""
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from pbsgui.core.pbs_api import ApiError, PbsApi, TokenExistsError, fetch_fingerprint

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl fehlt")
LOG = []
EXISTS: set[str] = set()  # exakte API-Pfade (ohne /api2/json), für die POST „already exists“ liefert


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, data=None, message=None):
        body = json.dumps({"data": data} if message is None else {"message": message}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        LOG.append((method, self.path, form, dict(self.headers)))
        api_path = self.path.removeprefix("/api2/json")
        if method == "DELETE":
            EXISTS.discard(api_path)  # gelöscht: danach lässt sich der Eintrag wieder anlegen
        if method == "POST" and api_path in EXISTS:
            return self._send(400, message="parameter verification failed - already exists")
        if self.path.endswith("/access/ticket"):
            if form.get("password") != "rootpw":
                return self._send(401, message="authentication failure")
            return self._send(200, {"ticket": "PBS:root@pam:T", "CSRFPreventionToken": "csrf1"})
        if "/token/" in self.path:
            return self._send(200, {"tokenid": "pbsgui-host@pbs!backup", "value": "SECRET"})
        if self.path.startswith("/api2/json/access/permissions"):
            return self._send(200, {"/datastore/s": {"Datastore.Backup": 1}})
        self._send(200, None)

    do_GET = lambda self: self._handle("GET")
    do_POST = lambda self: self._handle("POST")
    do_PUT = lambda self: self._handle("PUT")
    do_DELETE = lambda self: self._handle("DELETE")


@pytest.fixture
def server(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    LOG.clear()
    EXISTS.clear()
    yield srv.server_address[1]
    srv.shutdown()


def test_fingerprint_and_pinning(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    assert len(fp.split(":")) == 32
    api = PbsApi("127.0.0.1", server, fp)
    api.login("root@pam", "rootpw")
    with pytest.raises(ApiError, match="Fingerprint"):
        PbsApi("127.0.0.1", server, "00:11").login("root@pam", "rootpw")


def test_wrong_password(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    with pytest.raises(ApiError, match="authentication failure"):
        PbsApi("127.0.0.1", server, fp).login("root@pam", "falsch")


def test_provision_sets_user_and_token_acls(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    api = PbsApi("127.0.0.1", server, fp)
    api.login("root@pam", "rootpw")
    res = api.provision("s", "host", namespace="ns1")
    assert res.auth_id == "pbsgui-host@pbs!backup" and res.secret == "SECRET"
    acls = [f for m, p, f, h in LOG if m == "PUT" and p.endswith("/access/acl")]
    assert {a["auth-id"] for a in acls} == {"pbsgui-host@pbs", "pbsgui-host@pbs!backup"}
    assert all(a["role"] == "DatastoreBackup" and a["path"] == "/datastore/s/ns1" for a in acls)
    posts = [(p, h) for m, p, f, h in LOG if m != "GET" and "ticket" not in p]
    assert all(h.get("CSRFPreventionToken") == "csrf1" for _, h in posts)  # CSRF bei Schreibzugriffen


def test_token_permission_check(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    api = PbsApi("127.0.0.1", server, fp)
    api.use_token("pbsgui-host@pbs!backup", "SECRET")
    assert api.check_backup_permission("s") is True
    assert LOG[-1][3]["Authorization"] == "PBSAPIToken=pbsgui-host@pbs!backup:SECRET"


def _login(server):
    api = PbsApi("127.0.0.1", server, fetch_fingerprint("127.0.0.1", server))
    api.login("root@pam", "rootpw")
    return api


def test_nested_namespace_created_level_by_level(server):
    _login(server).provision("s", "host", namespace="srv/laptop")
    ns = [f for m, p, f, h in LOG if m == "POST" and p.endswith("/namespace")]
    assert ns == [{"name": "srv"}, {"name": "laptop", "parent": "srv"}]
    acl = [f for m, p, f, h in LOG if m == "PUT"]
    assert all(a["path"] == "/datastore/s/srv/laptop" for a in acl)


def test_existing_user_and_namespace_are_reused(server):
    EXISTS.update({"/access/users", "/admin/datastore/s/namespace"})
    res = _login(server).provision("s", "host", namespace="srv")
    assert res.secret == "SECRET"
    assert any(m == "PUT" for m, *_ in LOG)  # ACLs wurden trotzdem gesetzt


def test_existing_token_requires_confirmation_then_replaced(server):
    token_path = "/access/users/pbsgui-host%40pbs/token/backup"
    EXISTS.add(token_path)
    api = _login(server)
    with pytest.raises(TokenExistsError, match="existiert bereits"):
        api.provision("s", "host")
    assert not any(m == "DELETE" for m, *_ in LOG)  # ohne Zustimmung wird nichts gelöscht
    assert api.provision("s", "host", replace_token=True).secret == "SECRET"
    assert [p for m, p, *_ in LOG if m == "DELETE"] == ["/api2/json" + token_path]


def test_wait_for_permission_polls_until_effective(server):
    api = PbsApi("127.0.0.1", server, fetch_fingerprint("127.0.0.1", server))
    api.use_token("a@pbs!t", "x")
    answers = iter([False, False, True])
    api.check_backup_permission = lambda ds, ns="": next(answers)
    sleeps = []
    assert api.wait_for_backup_permission("s", sleep=sleeps.append) is True
    assert sleeps == [2, 2]


def test_wait_for_permission_times_out(server):
    api = PbsApi("127.0.0.1", server, fetch_fingerprint("127.0.0.1", server))
    api.check_backup_permission = lambda ds, ns="": False
    assert api.wait_for_backup_permission("s", timeout=4, interval=2, sleep=lambda _s: None) is False


def test_deprovision_removes_acls_before_user(server):
    _login(server).deprovision("pbsgui-host@pbs", "s", "ns1")
    calls = [(m, p.removeprefix("/api2/json"), f.get("auth-id"), f.get("delete")) for m, p, f, h in LOG[1:]]
    assert calls[0][0] == "PUT" and calls[0][2] == "pbsgui-host@pbs!backup" and calls[0][3] == "1"
    assert calls[1][0] == "PUT" and calls[1][2] == "pbsgui-host@pbs"
    assert calls[2] == ("DELETE", "/access/users/pbsgui-host%40pbs", None, None)
