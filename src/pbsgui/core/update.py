"""Programme der Rettungs-ISO beim Start aktualisieren (pbstui, proxmox-backup-client, VPN-Werkzeuge).

Die ISO ist ein Abbild vom Release-Tag; neue Fehlerbehebungen kommen über die APT-Quellen (pbsgui-Repository, Proxmox,
NetBird, Debian). Aktualisiert werden nur die genannten Pakete und nur, wenn sie installiert sind. Das Ergebnis steht in
einer Statusdatei, auf die der Start der Oberfläche kurz wartet.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PACKAGES = ("pbstui", "proxmox-backup-client", "netbird", "wireguard-tools", "openvpn")
STATE_FILE = "/run/pbsgui-update.state"     # "fertig", "offline" oder "fehler" (+ Text)
DEFAULT_HOST = "deb.debian.org"

Run = Callable[..., subprocess.CompletedProcess]


@dataclass
class UpdateResult:
    state: str                    # fertig | offline | fehler
    upgraded: list[str] = field(default_factory=list)
    text: str = ""
    restart_needed: bool = False  # pbstui wurde ersetzt: der laufende Assistent nutzt noch den alten Code


def installed_packages(run: Run = subprocess.run) -> dict[str, str]:
    r = run(["dpkg-query", "-W", "-f=${Package} ${Version} ${db:Status-Abbrev}\\n", *PACKAGES], capture_output=True, text=True)
    out: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith("ii"):
            out[parts[0]] = parts[1]
    return out


def online(host: str = DEFAULT_HOST, wait: float = 25, run: Run = subprocess.run, sleep: Callable = time.sleep) -> bool:
    """Wartet bis zu `wait` Sekunden auf eine Verbindung (Namensauflösung und HTTP zum Debian-Spiegel)."""
    end = time.monotonic() + wait
    while True:
        r = run(["curl", "-fsI", "--max-time", "5", f"http://{host}/debian/"], capture_output=True)
        if r.returncode == 0:
            return True
        if time.monotonic() >= end:
            return False
        sleep(2)


def update(run: Run = subprocess.run, wait: float = 25, on_line: Callable[[str], None] = lambda _l: None,
           write_state: bool = False, sleep: Callable = time.sleep) -> UpdateResult:
    def finish(res: UpdateResult) -> UpdateResult:
        if write_state:
            Path(STATE_FILE).write_text(f"{res.state} {res.text}".strip() + "\n")
        return res

    if not shutil.which("apt-get") and run is subprocess.run:
        return finish(UpdateResult("fehler", text="apt-get nicht gefunden"))
    on_line("Prüfe die Netzwerkverbindung …")
    if not online(wait=wait, run=run, sleep=sleep):
        return finish(UpdateResult("offline", text="kein Netzwerk: Updates übersprungen"))
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C"}
    on_line("Aktualisiere Paketlisten …")
    r = run(["apt-get", "update", "-qq"], capture_output=True, text=True, env=env, timeout=180)
    if r.returncode != 0:
        return finish(UpdateResult("fehler", text="apt-get update fehlgeschlagen: " + (r.stderr or r.stdout).strip()[-300:]))
    before = installed_packages(run)
    if not before:
        return finish(UpdateResult("fertig", text="keine der Programme installiert"))
    on_line("Aktualisiere: " + ", ".join(sorted(before)))
    r = run(["apt-get", "install", "-y", "-qq", "--only-upgrade", "-o", "Dpkg::Options::=--force-confold", *sorted(before)],
            capture_output=True, text=True, env=env, timeout=900)
    if r.returncode != 0:
        return finish(UpdateResult("fehler", text="Aktualisierung fehlgeschlagen: " + (r.stderr or r.stdout).strip()[-300:]))
    after = installed_packages(run)
    upgraded = sorted(f"{p} {before[p]} -> {after[p]}" for p in before if p in after and after[p] != before[p])
    text = ("aktualisiert: " + ", ".join(upgraded)) if upgraded else "alles aktuell"
    return finish(UpdateResult("fertig", upgraded, text, restart_needed=any(u.startswith("pbstui ") for u in upgraded)))
