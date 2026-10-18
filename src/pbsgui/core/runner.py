"""Führt ein Profil aus (vom Timer oder der GUI aufgerufen) und schreibt den Status."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from .client import ClientError, PbsClient
from .models import Profile
from .paths import Paths
from .secrets import SecretStore


def _credential(name: str) -> str | None:
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if d and (Path(d) / name).exists():
        return (Path(d) / name).read_text()
    return None


def write_status(paths: Paths, profile: Profile, ok: bool, code: int, message: str) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    (paths.state / f"{profile.slug}.json").write_text(json.dumps(
        {"time": int(time.time()), "ok": ok, "exit": code, "message": message}))


def read_status(paths: Paths, profile: Profile) -> dict | None:
    p = paths.state / f"{profile.slug}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def run_profile(profile: Profile, paths: Paths, store: SecretStore | None = None,
                on_line: Callable[[str], None] = print, binary: str | None = None) -> int:
    """Gibt den Exitcode zurück (0 = Erfolg). Unter systemd kommen Geheimnisse aus $CREDENTIALS_DIRECTORY."""
    secret = _credential("token") or (store.get(profile.slug, "token") if store else None)
    if not secret:
        on_line("Fehler: Kein API-Token-Secret gefunden")
        write_status(paths, profile, False, 2, "Kein Token-Secret")
        return 2
    keypass = _credential("keypass") or (store.get(profile.slug, "keypass") if store else None) or ""
    client = PbsClient(profile, secret, keypass, binary)
    try:
        on_line(f"Starte Backup '{profile.name}' -> {profile.repository}")
        code = client.backup(on_line)
    except ClientError as e:
        on_line(f"Fehler: {e}")
        write_status(paths, profile, False, 3, str(e))
        return 3
    write_status(paths, profile, code == 0, code, "ok" if code == 0 else f"Client-Exitcode {code}")
    on_line("Backup erfolgreich" if code == 0 else f"Backup fehlgeschlagen (Exitcode {code})")
    return code
