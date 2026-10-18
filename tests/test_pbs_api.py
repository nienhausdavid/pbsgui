"""PBS-API-Client gegen einen lokalen TLS-Testserver (selbstsigniertes Zertifikat)."""
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from pbsgui.core.pbs_api import ApiError, PbsApi, fetch_fingerprint

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl fehlt")
LOG = []


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
