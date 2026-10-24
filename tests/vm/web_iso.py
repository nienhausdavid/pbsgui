#!/usr/bin/env python3
"""Prüft die Web-Oberfläche der Rettungs-ISO in QEMU: Start per `pbsgui web --daemon`, Zugriff vom Host über einen
weitergeleiteten Port (VM_HOSTFWD), Zertifikat-Fingerabdruck, Anmeldung, Honeypot, Sperre und Abschalten.

Aufruf: VM_HOSTFWD=tcp:127.0.0.1:18443-:8443 python3 tests/vm/web_iso.py ISO [--keep]
Mit --keep bleibt die VM nach den Prüfungen laufen (für Browser-Screenshots), bis Enter gedrückt wird.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path

os.environ.setdefault("VM_HOSTFWD", "tcp:127.0.0.1:18443-:8443")
sys.path.insert(0, str(Path(__file__).parent))
from vmctl import VM  # noqa: E402

PORT = 18443


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK ' if ok else 'FEHLER'}] {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)
    return ok


def ctx() -> ssl.SSLContext:
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    return c


def request(method, path, body=None, cookie="", csrf=""):
    c = http.client.HTTPSConnection("127.0.0.1", PORT, context=ctx(), timeout=15)
    h = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        if csrf:
            h["X-CSRF-Token"] = csrf
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body=data, headers=h)
    r = c.getresponse(); text = r.read().decode("utf-8", "replace"); c.close()
    return r.status, r, text


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("iso"); ap.add_argument("--keep", action="store_true")
    ap.add_argument("--patch", action="append", default=[], metavar="DATEI", help="Quelldatei (unter src/) vor dem Test in die ISO kopieren")
    a = ap.parse_args()
    work = Path("build/vm"); work.mkdir(parents=True, exist_ok=True)
    disk = work / "web-test.img"; disk.write_bytes(b""); os.truncate(disk, 2 * 2**30)
    vm = VM("web-iso", work, [disk], iso=Path(a.iso), mem=2048)
    res: list[bool] = []
    try:
        vm.start()
        vm.expect(r"root@[\w-]+:[~/]\S*# ", 300); vm.send("\n"); vm.run("true", 30)
        vm.run("pkill -f '[p]bsgui rescue' || true; unset HISTFILE", 10)
        import base64
        for rel in a.patch:
            data = base64.b64encode((Path("src") / rel).read_bytes()).decode()
            vm.run(": > /tmp/p.b64", 10)
            for i in range(0, len(data), 1500):
                vm.run(f"printf %s {data[i:i + 1500]} >> /tmp/p.b64", 20)
            vm.run(f"base64 -d /tmp/p.b64 > /usr/lib/python3/dist-packages/{rel}; find /usr/lib/python3/dist-packages/pbsgui -name '*.pyc' -delete", 20)
            print("Patch:", rel, flush=True)
        out, _ = vm.run("pbsgui web --daemon 2>&1 | tail -12", 90)
        print(out)
        info_raw, _ = vm.run("cat /run/pbsgui-web/info.json", 15)
        info = json.loads(info_raw[info_raw.index("{"):info_raw.rindex("}") + 1])
        pw, fp = info["password"], info["fingerprint"]
        import re
        res.append(check("Passwort: 4 Wörter, jedes mit Zahl dahinter", re.fullmatch(r"[a-z]+\d\d(-[a-z]+\d\d){3}", pw) is not None, pw))
        # TLS-Fingerabdruck wie im Browser
        with socket.create_connection(("127.0.0.1", PORT), timeout=15) as s, ctx().wrap_socket(s) as t:
            der = t.getpeercert(binary_form=True)
        h = hashlib.sha256(der).hexdigest().upper()
        res.append(check("Fingerabdruck stimmt mit der Konsolen-Anzeige überein", ":".join(h[i:i + 2] for i in range(0, 64, 2)) == fp))
        code, r, html = request("GET", "/")
        res.append(check("Startseite mit CSP und Honeypot-Feld", code == 200 and "Content-Security-Policy" in dict(r.getheaders()) and 'name="website"' in html))
        code, _, _ = request("POST", "/api/login", {"password": "falsch-falsch-falsch"})
        res.append(check("Falsches Passwort wird abgewiesen", code == 401))
        code, r, body = request("POST", "/api/login", {"password": pw})
        cookie = (r.getheader("Set-Cookie") or "").split(";")[0]; csrf = json.loads(body).get("csrf", "")
        res.append(check("Anmeldung mit dem Konsolen-Passwort klappt", code == 200 and cookie.startswith("pbsgui_session=")))
        code, _, body = request("GET", "/api/status", cookie=cookie)
        st = json.loads(body)
        res.append(check("Status zeigt root, VPN-Werkzeuge und Version", code == 200 and st.get("root") and all(st["tools"].values()), body[:200]))
        code, _, body = request("POST", "/api/start", {"dry_run": True}, cookie=cookie, csrf=csrf)
        res.append(check("Start ohne Plan wird abgelehnt", code == 409))
        # Honeypot: ein Köder sperrt die Adresse (hier die Weiterleitung 10.0.2.2 der VM)
        code, _, html = request("GET", "/wp-login.php")
        res.append(check("Köder-Seite wirkt echt", code == 200 and "Anmeldung" in html))
        code, _, _ = request("GET", "/")
        res.append(check("Danach ist die Adresse gesperrt (403)", code == 403))
        out, _ = vm.run("cat /run/pbsgui-web/events.log | tail -6", 15)
        res.append(check("Konsole meldet Anmeldung und Honeypot-Treffer", "login-ok" in out and "honeypot" in out, out))
        out, _ = vm.run("pbsgui web --stop; sleep 2; pbsgui web --status; ls /run/pbsgui-web/ 2>&1 | head", 30)
        res.append(check("Web-Oberfläche lässt sich beenden", "läuft nicht" in out, out))
        if a.keep:
            print("VM läuft weiter; Enter beendet …", flush=True)
            input()
        vm.run("poweroff", 10)
    finally:
        vm.kill()
    print(f"\n{sum(res)} von {len(res)} Prüfungen bestanden")
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
