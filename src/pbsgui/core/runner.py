"""Führt ein Profil aus (vom Timer oder der GUI aufgerufen) und schreibt den Status."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from .client import ClientError, PbsClient
from .dbdump import parse_credentials, run_dumps
from .models import Profile
from .paths import Paths
from .recovery import RecoveryError, collect_recovery, ensure_recoverable
from .modes import FULL
from .secrets import SecretStore


def _credential(name: str) -> str | None:
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if d and (Path(d) / name).exists():
        return (Path(d) / name).read_text()
    return None


def _credential_path(name: str) -> str:
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    return str(Path(d) / name) if d and (Path(d) / name).exists() else ""


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
                on_line: Callable[[str], None] = print, binary: str | None = None,
                progress: bool | None = None) -> int:
    """Gibt den Exitcode zurück (0 = Erfolg). Unter systemd kommen Geheimnisse aus $CREDENTIALS_DIRECTORY."""
    try:
        ensure_recoverable(profile)
    except RecoveryError as e:  # auch für Zeitplan-Läufe: kein Vollbackup ohne exportierten Schlüssel
        on_line(f"Fehler: {e}")
        write_status(paths, profile, False, 4, "Schlüssel nicht exportiert")
        return 4
    if profile.backup_mode == FULL and os.geteuid() == 0:
        try:  # Datenträger-Metadaten landen unter /var/lib/pbsgui und damit im Backup selbst
            got = collect_recovery(paths.state / "recovery" / profile.slug, profile)
            on_line(f"Wiederherstellungsdaten gesammelt ({len(got.files)} Dateien, {len(got.warnings)} Warnungen)")
            for w in got.warnings:
                on_line(f"  Warnung: {w}")
        except Exception as e:  # nie das Backup selbst verhindern
            on_line(f"Warnung: Wiederherstellungsdaten nicht gesammelt: {e}")
    dump_dir = paths.state / "dumps" / profile.slug if profile.db_dump else None
    if dump_dir:
        try:  # Dumps sind ein Zusatz: Fehler werden gemeldet, verhindern aber nie das Backup
            on_line("Datenbank-Dumps …")
            creds = parse_credentials(_credential("dbdump") or (store.get(profile.slug, "dbdump") if store else None))
            run_dumps(dump_dir, creds, on_line)
        except Exception as e:
            on_line(f"Warnung: Datenbank-Dumps nicht ausgeführt: {e}")
    secret = _credential("token") or (store.get(profile.slug, "token") if store else None)
    if not secret:
        on_line("Fehler: Kein API-Token-Secret gefunden")
        write_status(paths, profile, False, 2, "Kein Token-Secret")
        return 2
    keypass = _credential("keypass") or (store.get(profile.slug, "keypass") if store else None) or ""
    key_path = _credential_path("key")  # System-Scope: systemd stellt den Key als Datei bereit
    key_content = "" if key_path else ((store.get(profile.slug, "key") if store else None) or "")
    client = PbsClient(profile, secret, keypass, binary, key_content=key_content, key_path=key_path)
    client.dump_dir = dump_dir
    try:
        on_line(f"Starte Backup '{profile.name}' -> {profile.repository}")
        if progress is None:
            progress = bool(os.environ.get("PBSGUI_PROGRESS"))    # nur für interaktive Aufrufer (nicht im Journal)
        from .progress import encode
        code = client.backup(on_line, (lambda p: on_line(encode(p))) if progress else None)
    except ClientError as e:
        on_line(f"Fehler: {e}")
        write_status(paths, profile, False, 3, str(e))
        return 3
    write_status(paths, profile, code == 0, code, "ok" if code == 0 else f"Client-Exitcode {code}")
    on_line("Backup erfolgreich" if code == 0 else f"Backup fehlgeschlagen (Exitcode {code})")
    return code


def read_latest_status(profile: Profile) -> dict | None:
    """Neuester Laufstatus aus Benutzer- und System-Scope (manuelle Läufe und Timer)."""
    found = [st for sc in ("user", "system") if (st := read_status(Paths.for_scope(sc), profile))]
    return max(found, key=lambda st: st["time"]) if found else None
