"""VPN-Verbindungen für die Rettungs-ISO: WireGuard, OpenVPN und NetBird.

Der Backup-Server steht oft hinter einem VPN. Hier wird nur der Tunnel auf- und abgebaut; Konfigurationen kommen von
einem USB-Stick (oder werden als Pfad angegeben), Geheimnisse (OpenVPN-Passwort, NetBird-Setup-Key) gehen nie als
Programmargument, sondern über eine 0600-Datei im RAM beziehungsweise die Umgebung des Prozesses.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from ..i18n import tr

KINDS = ("wireguard", "openvpn", "netbird")
LABELS = {"wireguard": "WireGuard", "openvpn": "OpenVPN", "netbird": "NetBird"}
EXTENSIONS = {"wireguard": (".conf",), "openvpn": (".ovpn", ".conf")}
RUN_DIR = "/run/pbsgui-vpn"      # RAM: PID, Log und Zugangsdaten verschwinden beim Herunterfahren
_IFACE = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")     # Namensregel von wg-quick / Linux-Schnittstellen

Run = Callable[..., subprocess.CompletedProcess]


class VpnError(RuntimeError):
    pass


@dataclass
class Result:
    ok: bool
    text: str
    missing: bool = False        # Programm nicht installiert (sprachunabhängig prüfbar)


def _run(argv: list[str], run: Run = subprocess.run, env: dict | None = None, timeout: int = 90) -> Result:
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return Result(False, tr("{0} ist nicht installiert", argv[0]), missing=True)
    except subprocess.TimeoutExpired:
        return Result(False, tr("{0}: Zeitüberschreitung", argv[0]))
    return Result(r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip())


def _ram_dir() -> Path:
    d = Path(RUN_DIR)
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


# -- Konfigurationen finden -------------------------------------------------------------------------
def _looks_like(kind: str, path: Path) -> bool:
    try:
        if path.stat().st_size > 65536:
            return False
        head = path.read_text(errors="replace")
    except OSError:
        return False
    if kind == "wireguard":
        return "[Interface]" in head and "PrivateKey" in head
    return bool(re.search(r"^\s*(remote|client|dev|proto)\b", head, re.M))      # OpenVPN-Direktiven


def find_configs(kind: str, roots: list[str] | None = None, mount: bool = False, run: Run = subprocess.run) -> list[str]:
    """Sucht passende Konfigurationsdateien auf eingehängten Medien (bei mount=True werden Wechselmedien zuerst eingehängt)."""
    if kind not in EXTENSIONS:
        return []
    if mount:
        from .rescue import mount_removable
        mount_removable(run)
    found: list[str] = []
    for root in roots or ["/media", "/mnt", "/run/media", "/etc/wireguard", "/etc/openvpn"]:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirs, files in os.walk(base):
            if len(Path(dirpath).relative_to(base).parts) >= 4:
                dirs.clear()
                continue
            for f in files:
                p = Path(dirpath) / f
                if f.lower().endswith(EXTENSIONS[kind]) and _looks_like(kind, p):
                    found.append(str(p))
    return sorted(found)


def interface_name(path: str) -> str:
    """Schnittstellenname aus dem Dateinamen (wg-quick verlangt bis 15 Zeichen aus [A-Za-z0-9_=+.-])."""
    name = Path(path).stem
    if not _IFACE.match(name):
        raise VpnError(tr("Der Dateiname '{0}' taugt nicht als Schnittstellenname (1 bis 15 Zeichen: Buchstaben, Ziffern, _ = + . -). Bitte die Datei umbenennen, z. B. wg0.conf.", Path(path).name))
    return name


# -- WireGuard ------------------------------------------------------------------------------------------
def wireguard_up(path: str, run: Run = subprocess.run) -> Result:
    p = Path(path)
    if not p.is_file():
        raise VpnError(tr("Datei nicht gefunden: {0}", path))
    name = interface_name(path)
    dest = Path("/etc/wireguard") / f"{name}.conf"      # Kopie: der Tunnel überlebt das Abziehen des Sticks
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    if p.resolve() != dest.resolve():
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(p.read_text())
    return _run(["wg-quick", "up", name], run)


def wireguard_down(name: str | None = None, run: Run = subprocess.run) -> Result:
    names = [name] if name else wireguard_interfaces(run)
    if not names:
        return Result(True, tr("Kein WireGuard-Tunnel aktiv"))
    out = [_run(["wg-quick", "down", n], run) for n in names]
    return Result(all(o.ok for o in out), "\n".join(o.text for o in out if o.text))


def wireguard_interfaces(run: Run = subprocess.run) -> list[str]:
    r = _run(["wg", "show", "interfaces"], run, timeout=15)
    return r.text.split() if r.ok else []


# -- OpenVPN --------------------------------------------------------------------------------------------
def _log_tail(n: int = 6) -> str:
    try:
        lines = (Path(RUN_DIR) / "openvpn.log").read_text(errors="replace").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def openvpn_up(path: str, username: str = "", password: str = "", run: Run = subprocess.run,
               settle: float = 3.0, sleep: Callable[[float], None] = time.sleep) -> Result:
    """Startet OpenVPN als Dienst. Meldet Fehler (Konfiguration, Zugangsdaten) mit dem Ende des Logs statt still zu scheitern."""
    if not Path(path).is_file():
        raise VpnError(tr("Datei nicht gefunden: {0}", path))
    d = _ram_dir()
    (d / "openvpn.pid").unlink(missing_ok=True)
    argv = ["openvpn", "--config", str(Path(path).resolve()), "--daemon", "pbsgui-openvpn",
            "--writepid", str(d / "openvpn.pid"), "--log", str(d / "openvpn.log"), "--verb", "3",
            "--cd", str(Path(path).resolve().parent)]          # relative Pfade (ca, cert, key) in der .ovpn
    if username or password:
        auth = d / "auth.txt"
        fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{username}\n{password}\n")
        argv += ["--auth-user-pass", str(auth)]
    r = _run(argv, run)
    if not r.ok:
        return Result(False, (r.text + "\n" + _log_tail()).strip() or tr("OpenVPN konnte nicht gestartet werden"))
    waited = 0.0
    while waited < settle and not openvpn_running():       # der Dienst legt die PID-Datei kurz nach dem Start an
        sleep(0.5); waited += 0.5
    if not openvpn_running():
        return Result(False, tr("OpenVPN hat sich sofort beendet:\n") + (_log_tail() or tr("(kein Log)")))
    return Result(True, tr("OpenVPN läuft. Ob der Tunnel steht, zeigt `pbsgui vpn status` (Log: %s).") % (d / "openvpn.log"))


def openvpn_down(run: Run = subprocess.run) -> Result:
    pidfile = Path(RUN_DIR) / "openvpn.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        shutil.rmtree(RUN_DIR, ignore_errors=True)     # auch ohne laufenden Tunnel: Zugangsdaten nicht liegen lassen
        return Result(True, tr("Kein OpenVPN-Tunnel aktiv"))
    r = _run(["kill", str(pid)], run, timeout=15)
    shutil.rmtree(RUN_DIR, ignore_errors=True)     # auch die Zugangsdaten entfernen
    return Result(True, tr("OpenVPN beendet") if r.ok else r.text)


def openvpn_running() -> bool:
    try:
        pid = int((Path(RUN_DIR) / "openvpn.pid").read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


# -- NetBird --------------------------------------------------------------------------------------------
def netbird_up(setup_key: str = "", management_url: str = "", run: Run = subprocess.run) -> Result:
    """Startet den NetBird-Dienst und meldet das Gerät an. Der Setup-Key geht per Umgebung, nicht als Argument."""
    start = _run(["systemctl", "start", "netbird"], run, timeout=30)
    if not start.ok and start.missing:
        return start
    argv = ["netbird", "up"]
    if management_url:
        if not re.match(r"^https?://[^\s]+$", management_url):
            raise VpnError(tr("Management-URL muss mit https:// beginnen"))
        argv += ["--management-url", management_url]
    env = dict(os.environ)
    if setup_key:
        env["NB_SETUP_KEY"] = setup_key
    return _run(argv, run, env=env, timeout=120)


def netbird_down(run: Run = subprocess.run) -> Result:
    return _run(["netbird", "down"], run, timeout=30)


# -- Gesamtübersicht ---------------------------------------------------------------------------------------
def status(run: Run = subprocess.run) -> dict[str, str]:
    out: dict[str, str] = {}
    wg = wireguard_interfaces(run)
    out["wireguard"] = tr("aktiv: {0}", ", ".join(wg)) if wg else tr("aus")
    out["openvpn"] = tr("aktiv") if openvpn_running() else tr("aus")
    nb = _run(["netbird", "status"], run, timeout=20)
    if not nb.ok:
        out["netbird"] = tr("nicht verbunden") if not nb.missing else tr("nicht installiert")
    else:
        first = next((l for l in nb.text.splitlines() if "Management" in l or "Status" in l), "")
        out["netbird"] = tr("verbunden") if "Connected" in nb.text else (first.strip() or tr("aus"))
    return out


def tools_available() -> dict[str, bool]:
    return {"wireguard": bool(shutil.which("wg-quick")), "openvpn": bool(shutil.which("openvpn")),
            "netbird": bool(shutil.which("netbird"))}
