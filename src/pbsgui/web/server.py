"""HTTPS-Server der Web-Oberfläche für die Wiederherstellung (nur Standardbibliothek).

Start: `pbsgui web` (Vordergrund) oder `pbsgui web --daemon`. Beim Start entstehen ein zufälliges Passwort und ein
selbstsigniertes Zertifikat; beides wird nur auf der Konsole angezeigt (Passwort) beziehungsweise als Fingerabdruck
zum Vergleichen im Browser. Siehe auth.py für die Abwehr (Rate Limiting, Sperren, Honeypot).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import auth
from .service import RescueService, ServiceError

RUN_DIR = Path("/run/pbsgui-web")
DEFAULT_PORT = 8443
MAX_BODY = 128 * 1024
MAX_CONNECTIONS = 48
MAX_TARPIT = 16
INDEX = Path(__file__).with_name("index.html")


# -- Zertifikat -----------------------------------------------------------------------------------------------
def local_addresses() -> list[str]:
    """Adressen dieses Rechners (ohne Loopback und Link-Local), für die Anzeige der Adresse und das Zertifikat."""
    out: list[str] = []
    try:
        r = subprocess.run(["ip", "-j", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=10)
        for itf in json.loads(r.stdout or "[]"):
            for a in itf.get("addr_info", []):
                if a.get("local"):
                    out.append(a["local"])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return out


def make_certificate(directory: Path, names: list[str]) -> tuple[Path, Path, str]:
    """Selbstsigniertes ECDSA-Zertifikat (2 Tage) mit den Adressen als Namen; gibt (Zertifikat, Schlüssel, SHA-256-Fingerabdruck) zurück."""
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "cert.pem", directory / "key.pem"
    sans = ",".join([f"IP:{n}" if _is_ip(n) else f"DNS:{n}" for n in dict.fromkeys(["localhost", "127.0.0.1", *names])])
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=pbsgui-rescue",
                        "-addext", f"subjectAltName={sans}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Zertifikat konnte nicht erzeugt werden (openssl): " + (r.stderr or r.stdout).strip()[-300:])
    os.chmod(key, 0o600)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    h = hashlib.sha256(der).hexdigest().upper()
    return cert, key, ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def _is_ip(s: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


# -- Anwendung -------------------------------------------------------------------------------------------------
class App:
    def __init__(self, passphrase: str | None = None, service: RescueService | None = None, idle_minutes: float = 60,
                 clock=time.monotonic, sleep=time.sleep, tarpit=(0.8, 1.6), guard_kw: dict | None = None):
        self.passphrase = passphrase or auth.generate_passphrase()
        self.service = service or RescueService()
        self.clock, self.sleep, self.tarpit = clock, sleep, tarpit
        self.stop_reason = ""
        self.guard = auth.Guard(self.passphrase, clock=clock, on_shutdown=self.request_stop, **(guard_kw or {}))
        self.sessions = auth.Sessions(clock=clock)
        self.idle_seconds = idle_minutes * 60
        self.last_request = clock()
        self.stop_event = threading.Event()
        self.trap_sem = threading.BoundedSemaphore(MAX_TARPIT)
        self.service.on_finished = self._finished

    def request_stop(self, reason: str) -> None:
        self.stop_reason = reason
        self.stop_event.set()

    def _finished(self, ok: bool, dry: bool) -> None:
        self.guard.log("info", "-", ("Trockenlauf" if dry else "Wiederherstellung") + (" fertig" if ok else " fehlgeschlagen"))

    def idle_expired(self) -> bool:
        return self.clock() - self.last_request > self.idle_seconds

    def delay(self) -> None:
        """Verzögerte Antwort (Teergrube): bremst Raten und bindet höchstens MAX_TARPIT Threads."""
        if self.trap_sem.acquire(blocking=False):
            try:
                self.sleep(random.uniform(*self.tarpit))  # noqa: S311  # nosec B311 - nur Zufall für die Verzögerung, kein Geheimnis
            finally:
                self.trap_sem.release()


FAKE_LOGIN = (b"<!doctype html><meta charset=utf-8><title>Anmeldung</title><form method=post><h3>Anmeldung</h3>"
              b"<input name=user placeholder=Benutzer><input name=pass type=password placeholder=Passwort>"
              b"<button>Anmelden</button></form>")
FAKE_FAIL = (b"<!doctype html><meta charset=utf-8><title>Anmeldung</title><p>Benutzername oder Passwort falsch.</p>"
             b"<a href=''>Zur&uuml;ck</a>")


def make_handler(app: App):
    guard, sessions, service = app.guard, app.sessions, app.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "pbsgui"
        sys_version = ""

        def version_string(self) -> str:      # keine Versionsangaben von Python oder der Bibliothek verraten
            return "pbsgui"
        timeout = 10                          # Slowloris: lahme Verbindungen werden nach 10 s getrennt

        def log_message(self, fmt, *args):    # nichts an stderr (kein Geheimnis in Pfaden); wichtige Ereignisse loggt der Guard
            pass

        # -- Hilfen
        def _ip(self) -> str:
            return auth.client_key(self.client_address[0])

        def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: dict, headers: dict | None = None) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json", headers)

        def _body(self) -> dict:
            if self.headers.get("Transfer-Encoding"):
                raise ServiceError("Chunked wird nicht unterstützt", 411)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ServiceError("Content-Length ungültig") from None
            if n < 0 or n > MAX_BODY:
                raise ServiceError("Anfrage zu groß", 413)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            if "json" not in (self.headers.get("Content-Type") or ""):
                raise ServiceError("Nur JSON", 415)
            try:
                d = json.loads(raw)
            except ValueError:
                raise ServiceError("Ungültiges JSON") from None
            if not isinstance(d, dict):
                raise ServiceError("JSON-Objekt erwartet")
            return d

        def _cookie(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "pbsgui_session":
                    return v
            return ""

        # -- Verteilung
        def do_GET(self):
            self._handle()

        do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

        def _handle(self) -> None:
            ip = self._ip()
            app.last_request = app.clock()
            try:
                self._route(ip)
            except ServiceError as e:
                self._json(e.code, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
                pass
            except Exception:                  # kein Stacktrace an den Client
                guard.log("info", ip, f"interner Fehler bei {self.command} {urlsplit(self.path).path}")
                try:
                    self._json(500, {"error": "Interner Fehler"})
                except OSError:
                    pass

        def _route(self, ip: str) -> None:
            path = urlsplit(self.path).path
            rest = guard.blocked_for(ip)
            if rest > 0:                                      # gesperrt: kurz bremsen, dann ablehnen
                app.delay()
                if guard.block_kind(ip) == "lock" and path.startswith("/api/"):     # wer sich vertippt hat, erfährt den Grund
                    return self._json(429, {"error": f"Zu viele Fehlversuche: diese Adresse ist noch etwa {max(1, round(rest / 60))} "
                                                     "Minuten gesperrt"}, {"Retry-After": str(int(rest))})
                return self._send(403, b"Forbidden", "text/plain")
            if not guard.allow_request(ip):
                return self._send(429, b"Too Many Requests", "text/plain", {"Retry-After": "10"})
            if auth.is_honeypot_path(path):                    # Köder: sperren, dann glaubhaft antworten
                guard.honeypot(ip, f"{self.command} {path[:80]}")
                app.delay()
                if self.command == "POST":
                    return self._send(200, FAKE_FAIL, "text/html")
                return self._send(200, FAKE_LOGIN, "text/html")
            if path == "/" and self.command in ("GET", "HEAD"):
                nonce = secrets.token_urlsafe(16)
                html = INDEX.read_text().replace("__NONCE__", nonce).replace("__HP__", auth.HONEYPOT_FIELD).encode()
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
                       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                return self._send(200, html, "text/html", {"Content-Security-Policy": csp})
            if not path.startswith("/api/"):
                return self._send(404, b"Not Found", "text/plain")
            if path == "/api/login":
                return self._login(ip)
            sess = sessions.get(self._cookie(), ip)
            if not sess:
                return self._json(401, {"error": "Nicht angemeldet oder Sitzung abgelaufen"})
            if self.command == "POST":
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token") or "", sess.csrf):
                    return self._json(403, {"error": "CSRF-Prüfung fehlgeschlagen"})
            elif self.command not in ("GET", "HEAD"):
                return self._json(405, {"error": "Methode nicht erlaubt"})
            self._api(path, sess)

        def _login(self, ip: str) -> None:
            if self.command != "POST":
                return self._json(405, {"error": "Methode nicht erlaubt"})
            d = self._body()
            if str(d.get(auth.HONEYPOT_FIELD, "")).strip():     # unsichtbares Feld ausgefüllt: das war kein Mensch
                guard.honeypot(ip, "Honeypot-Feld ausgefüllt")
                app.delay()
                return self._json(401, {"error": "Anmeldung fehlgeschlagen"})
            pw = d.get("password")
            if not isinstance(pw, str) or len(pw) > 200:
                app.delay()
                return self._json(401, {"error": "Anmeldung fehlgeschlagen"})
            ok, why = guard.check_login(ip, pw)
            if ok:
                s = sessions.create(ip)
                cookie = f"pbsgui_session={s.token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=7200"
                return self._json(200, {"csrf": s.csrf}, {"Set-Cookie": cookie})
            app.delay()                                          # Fehlversuche kosten Zeit
            if why == "locked":
                return self._json(429, {"error": "Zu viele Fehlversuche: diese Adresse ist vorübergehend gesperrt"})
            if why == "down":
                return self._json(503, {"error": "Die Web-Oberfläche wurde abgeschaltet (zu viele Fehlversuche)"})
            return self._json(401, {"error": "Anmeldung fehlgeschlagen"})

        def _api(self, path: str, sess) -> None:
            get_routes = {"/api/status": lambda: service.status(), "/api/disks": lambda: service.disks(),
                          "/api/log": lambda: service.log(self._query_int("since"))}
            post_routes = {"/api/fingerprint": service.fingerprint, "/api/connect": service.connect,
                           "/api/choose": service.choose, "/api/plan": service.make_plan, "/api/start": service.start,
                           "/api/key": service.set_key, "/api/vpn/up": service.vpn_up, "/api/vpn/down": service.vpn_down,
                           "/api/power": service.power}
            if self.command in ("GET", "HEAD") and path in get_routes:
                return self._json(200, get_routes[path]())
            if self.command == "POST":
                if path == "/api/logout":
                    sessions.end()
                    return self._json(200, {"ok": True}, {"Set-Cookie": "pbsgui_session=; Max-Age=0; Path=/; Secure; HttpOnly"})
                if path == "/api/stop":
                    self._json(200, {"ok": True})
                    app.request_stop("auf Wunsch beendet")
                    return
                if path in post_routes:
                    return self._json(200, post_routes[path](self._body()))
            self._json(404, {"error": "Unbekannte Adresse"})

        def _query_int(self, name: str) -> int:
            for part in urlsplit(self.path).query.split("&"):
                k, _, v = part.partition("=")
                if k == name and v.isdigit():
                    return int(v)
            return 0

    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = True

    def __init__(self, addr, handler, ctx: ssl.SSLContext, limit: int = MAX_CONNECTIONS):
        self.ctx = ctx
        self._sem = threading.BoundedSemaphore(limit)
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):      # zu viele gleichzeitige Verbindungen: sofort schließen
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            try:
                request = self.ctx.wrap_socket(request, server_side=True)    # TLS-Handshake im eigenen Thread (blockiert nicht den Server)
            except (ssl.SSLError, OSError):
                self.shutdown_request(request)
                return
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()


def tls_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


class WebServer:
    def __init__(self, bind: str = "0.0.0.0", port: int = DEFAULT_PORT, idle_minutes: float = 60, directory: Path | None = None,
                 app: App | None = None, cert_names: list[str] | None = None):
        self.dir = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="pbsgui-web-"))
        self.app = app or App(idle_minutes=idle_minutes, service=RescueService(self.dir / "work"))
        names = cert_names if cert_names is not None else ([] if bind in ("0.0.0.0", "::") else [bind]) + local_addresses()
        cert, key, self.fingerprint = make_certificate(self.dir / "tls", names + [socket.gethostname()])
        self.httpd = BoundedServer((bind, port), make_handler(self.app), tls_context(cert, key))
        self.port = self.httpd.server_address[1]
        self.bind = bind
        self.started = time.time()
        self.addresses = names

    def urls(self) -> list[str]:
        hosts = self.addresses or ([self.bind] if self.bind not in ("0.0.0.0", "::") else ["127.0.0.1"])
        return [f"https://{('[' + h + ']') if ':' in h else h}:{self.port}/" for h in dict.fromkeys(hosts)]

    def info(self) -> dict:
        return {"urls": self.urls(), "fingerprint": self.fingerprint, "password": self.app.passphrase, "pid": os.getpid(),
                "started": self.started, "idle_minutes": self.app.idle_seconds / 60}

    def serve(self) -> str:
        """Läuft bis zum Abschalten (Leerlauf, zu viele Fehlversuche, Signal oder /api/stop); gibt den Grund zurück."""
        t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        t.start()
        try:
            while not self.app.stop_event.wait(5):
                if self.app.idle_expired() and not self.app.service.run.state == "running":
                    self.app.request_stop("Leerlauf: Zeit abgelaufen")
        finally:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.app.service.cleanup()
            shutil.rmtree(self.dir, ignore_errors=True)
        return self.app.stop_reason or "beendet"



# -- Kommandozeile --------------------------------------------------------------------------------------------
def write_info(info: dict) -> None:
    RUN_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    p = RUN_DIR / "info.json"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)


def read_info() -> dict | None:
    try:
        info = json.loads((RUN_DIR / "info.json").read_text())
        os.kill(int(info["pid"]), 0)
        return info
    except (OSError, ValueError, KeyError):
        return None


def describe(info: dict) -> str:
    return ("Web-Oberfläche der Wiederherstellung\n"
            "  Adresse:      " + "\n                ".join(info["urls"]) + "\n"
            f"  Passwort:     {info['password']}\n"
            f"  Fingerabdruck (SHA-256, im Browser vergleichen):\n    {info['fingerprint']}\n"
            f"  Schaltet sich nach {info['idle_minutes']:.0f} Minuten ohne Anfragen ab.")


def start_daemon(bind: str = "0.0.0.0", port: int = DEFAULT_PORT, timeout: float = 60, wait: float = 30) -> dict | None:
    """Startet den Server im Hintergrund und gibt die Zugangsdaten zurück (None = nicht angelaufen)."""
    info = read_info()
    if info:
        return info
    RUN_DIR.mkdir(mode=0o700, exist_ok=True)
    log = open(RUN_DIR / "web.log", "a")
    subprocess.Popen([sys.executable, "-m", "pbsgui.web.server", "--bind", bind, "--port", str(port), "--timeout", str(timeout)],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.4)
        info = read_info()
        if info:
            return info
    return None


def stop_daemon() -> bool:
    info = read_info()
    if not info:
        return False
    os.kill(int(info["pid"]), signal.SIGTERM)
    return True


def events_tail(n: int = 12) -> list[str]:
    try:
        return (RUN_DIR / "events.log").read_text().splitlines()[-n:]
    except OSError:
        return []


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pbsgui web", description="Web-Oberfläche für die Wiederherstellung (Rettungs-ISO)")
    ap.add_argument("--bind", default="0.0.0.0", help="Adresse, an der gelauscht wird; Standard: alle Adressen")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--timeout", type=float, default=60, help="Minuten ohne Anfrage, dann schaltet sie sich ab (Standard 60)")
    ap.add_argument("--daemon", action="store_true", help="im Hintergrund starten und die Zugangsdaten anzeigen")
    ap.add_argument("--stop", action="store_true"); ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.status or a.stop:
        info = read_info()
        if not info:
            print("Die Web-Oberfläche läuft nicht.")
            return 1
        if a.stop:
            os.kill(int(info["pid"]), signal.SIGTERM)
            print("Beendet.")
            return 0
        print(describe(info))
        return 0
    if read_info():
        print(describe(read_info()))      # type: ignore[arg-type]
        print("\n(Sie läuft bereits; `pbsgui web --stop` beendet sie.)")
        return 0
    if not shutil.which("openssl"):
        print("Fehler: openssl fehlt (für das Zertifikat).", file=sys.stderr)
        return 1
    if a.daemon:
        info = start_daemon(a.bind, a.port, a.timeout)
        if info:
            print(describe(info))
            return 0
        print("Fehler: Die Web-Oberfläche ist nicht angelaufen (siehe /run/pbsgui-web/web.log).", file=sys.stderr)
        return 1
    try:
        srv = WebServer(a.bind, a.port, a.timeout)
    except OSError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    events_file = RUN_DIR / "events.log"
    RUN_DIR.mkdir(mode=0o700, exist_ok=True)
    srv.app.guard.log = _tee(srv.app.guard.log, events_file)          # type: ignore[method-assign]
    write_info(srv.info())
    signal.signal(signal.SIGTERM, lambda *_: srv.app.request_stop("beendet (Signal)"))
    print(describe(srv.info()), flush=True)
    print("\nEreignisse (Anmeldungen, Sperren, Köder):", flush=True)
    try:
        reason = srv.serve()
    except KeyboardInterrupt:
        reason = "beendet (Strg+C)"
    (RUN_DIR / "info.json").unlink(missing_ok=True)
    print(f"Web-Oberfläche beendet: {reason}")
    return 0


def _tee(orig, events_file: Path):
    def log(kind, who, detail=""):
        orig(kind, who, detail)
        line = f"{time.strftime('%H:%M:%S')}  {kind:<10} {who:<22} {detail}".rstrip()
        print(line, flush=True)
        try:
            with open(events_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return log


if __name__ == "__main__":
    sys.exit(run())
