"""Lokaler TLS-Testserver (selbstsigniertes Zertifikat), der die benötigten PBS-API-Endpunkte simuliert."""
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

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
            asked = parse_qs(urlparse(self.path).query).get("path", ["/"])[0]
            return self._send(200, {asked: {"Datastore.Backup": 1}})
        if api_path == "/admin/datastore":
            return self._send(200, [{"store": "store1"}, {"store": "store2"}])
        if api_path.endswith("/namespace") and method == "GET":
            return self._send(200, [{"ns": ""}, {"ns": "a"}, {"ns": "a/b"}])
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


