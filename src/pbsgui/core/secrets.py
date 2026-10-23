"""Speicher für Geheimnisse (API-Token, Key-Passphrase).

Backends:
- SystemdCredsStore: `systemd-creds` (verschlüsselt, an Host/TPM gebunden)
- FileStore: Klartextdatei mit Rechten 0600 (Fallback, ehrlich als schwächer markiert)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

CRED_NAMES = ("token", "keypass", "key", "dbdump")  # feste Namen, auf die LoadCredential* in den Units zeigen


class SecretError(RuntimeError):
    pass


def systemd_version() -> int:
    try:
        out = subprocess.run(["systemctl", "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"systemd (\d+)", out)
        return int(m.group(1)) if m else 0
    except (OSError, subprocess.TimeoutExpired):
        return 0


class SecretStore:
    encrypted = False
    label = ""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def put(self, profile_slug: str, kind: str, value: str) -> None: raise NotImplementedError
    def get(self, profile_slug: str, kind: str) -> str | None: raise NotImplementedError
    def delete(self, profile_slug: str) -> None:
        for kind in CRED_NAMES:
            self.path(profile_slug, kind).unlink(missing_ok=True)

    def path(self, profile_slug: str, kind: str) -> Path:
        return self.dir / f"{profile_slug}.{kind}{self.suffix}"

    suffix = ""

    def unit_directive(self, profile_slug: str, kind: str) -> str | None:
        """Zeile für die systemd-Unit oder None, falls das Geheimnis fehlt."""
        raise NotImplementedError


class FileStore(SecretStore):
    label = "Datei (0600, nicht verschlüsselt)"
    suffix = ".secret"

    def put(self, profile_slug, kind, value):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        p = self.path(profile_slug, kind)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)

    def get(self, profile_slug, kind):
        p = self.path(profile_slug, kind)
        return p.read_text() if p.exists() else None

    def unit_directive(self, profile_slug, kind):
        p = self.path(profile_slug, kind)
        return f"LoadCredential={kind}:{p}" if p.exists() else None


class SystemdCredsStore(SecretStore):
    encrypted = True
    suffix = ".cred"

    def __init__(self, directory: Path, user: bool):
        super().__init__(directory)
        self.user = user
        self.label = "systemd-creds (verschlüsselt)" + (" [Benutzer]" if user else " [System]")

    def _cmd(self, *args: str) -> list[str]:
        return ["systemd-creds", *(["--user"] if self.user else []), *args]

    def put(self, profile_slug, kind, value):
        # --with-key=host: bewusst ohne TPM2. Der User-Manager darf /dev/tpmrm0 meist nicht öffnen
        # (Exit 243/CREDENTIALS beim Start der Unit); TPM-Bindung wäre außerdem hardwareabhängig.
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        p = self.path(profile_slug, kind)
        r = subprocess.run(self._cmd("encrypt", "--with-key=host", f"--name={kind}", "-", str(p)), input=value.encode(),
                           capture_output=True)
        if r.returncode != 0:
            raise SecretError(r.stderr.decode().strip() or "systemd-creds encrypt fehlgeschlagen")
        os.chmod(p, 0o600)

    def get(self, profile_slug, kind):
        p = self.path(profile_slug, kind)
        if not p.exists():
            return None
        r = subprocess.run(self._cmd("decrypt", f"--name={kind}", str(p), "-"), capture_output=True)
        if r.returncode != 0:
            raise SecretError(r.stderr.decode().strip() or "systemd-creds decrypt fehlgeschlagen")
        return r.stdout.decode()

    def unit_directive(self, profile_slug, kind):
        # Der User-Manager (systemd --user) kann Benutzer-Credentials per LoadCredentialEncrypted= nicht
        # entschlüsseln (Exit 243/CREDENTIALS, getestet auf Debian 13 / systemd 257). Im User-Scope
        # entschlüsselt deshalb der Runner selbst per `systemd-creds --user decrypt`.
        if self.user:
            return None
        p = self.path(profile_slug, kind)
        return f"LoadCredentialEncrypted={kind}:{p}" if p.exists() else None


def best_store(scope: str, directory: Path) -> SecretStore:
    """Wählt das stärkste verfügbare Backend.

    System: systemd-creds ab systemd 250. Benutzer: `--user` ab systemd 256.
    """
    have = shutil.which("systemd-creds") is not None
    ver = systemd_version() if have else 0
    if have and ((scope == "system" and ver >= 250) or (scope == "user" and ver >= 256)):
        return SystemdCredsStore(directory, user=(scope == "user"))
    return FileStore(directory)
