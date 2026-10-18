"""Automatische Erzeugung des Verschlüsselungs-Keys und sicheres Bereitstellen als Datei.

`proxmox-backup-client key create --kdf scrypt` verlangt ein TTY für die Passphrase (Umgebungsvariable
wird nicht ausgewertet; getestet mit Client 4.2.8). Deshalb wird der Key ohne KDF erzeugt und danach
verschlüsselt in den Geheimnis-Speicher (systemd-creds) gelegt, nie dauerhaft als Klartextdatei.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .client import ClientError, client_binary


def _tmpdir() -> str:
    # Bevorzugt tmpfs (XDG_RUNTIME_DIR), damit der Klartext nie auf die Platte kommt
    d = tempfile.mkdtemp(prefix="pbsgui-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
    os.chmod(d, 0o700)
    return d


@contextmanager
def materialized_key(content: str) -> Iterator[str]:
    """Schreibt den Key kurzzeitig in eine 0600-Datei und löscht sie danach wieder."""
    d = _tmpdir()
    path = os.path.join(d, "key.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        yield path
    finally:
        shutil.rmtree(d, ignore_errors=True)


def generate_key(binary: str | None = None) -> tuple[str, str]:
    """Erzeugt einen neuen Key; liefert (Key-JSON, Fingerprint)."""
    d = _tmpdir()
    path = os.path.join(d, "key.json")
    try:
        try:
            r = subprocess.run([binary or client_binary(), "key", "create", path, "--kdf", "none"],
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise ClientError(f"Client nicht gefunden: {binary or client_binary()}") from None
        if r.returncode != 0:
            raise ClientError((r.stderr or r.stdout).strip() or "key create fehlgeschlagen")
        content = Path(path).read_text()
        try:
            fp = json.loads(content).get("fingerprint", "")
        except ValueError:
            raise ClientError("Ungültige Key-Datei vom Client") from None
        return content, fp
    finally:
        shutil.rmtree(d, ignore_errors=True)


def export_key(content: str, dest: str) -> None:
    """Schreibt eine Kopie des Keys (0600) an einen vom Benutzer gewählten Ort."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
