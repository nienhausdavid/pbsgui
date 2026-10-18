"""Wrapper um `proxmox-backup-client`. Geheimnisse gehen nur über Umgebungsvariablen."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Callable

from .models import Profile, Snapshot


class ClientError(RuntimeError):
    pass


def client_binary() -> str:
    return os.environ.get("PBSGUI_CLIENT", "proxmox-backup-client")


class PbsClient:
    def __init__(self, profile: Profile, secret: str, key_password: str = "", binary: str | None = None):
        self.profile = profile
        self.secret = secret
        self.key_password = key_password
        self.binary = binary or client_binary()

    # -- intern -------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PBS_REPOSITORY"] = self.profile.repository
        env["PBS_PASSWORD"] = self.secret
        if self.profile.fingerprint:
            env["PBS_FINGERPRINT"] = self.profile.fingerprint
        if self.key_password:
            env["PBS_ENCRYPTION_PASSWORD"] = self.key_password
        return env

    def _ns(self) -> list[str]:
        return ["--ns", self.profile.namespace] if self.profile.namespace else []

    def _run(self, args: list[str], timeout: float | None = 120) -> str:
        try:
            p = subprocess.run([self.binary, *args], env=self._env(), capture_output=True,
                               text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise ClientError(f"Client nicht gefunden: {self.binary}") from None
        except subprocess.TimeoutExpired:
            raise ClientError("Zeitüberschreitung beim Aufruf des Clients") from None
        if p.returncode != 0:
            raise ClientError((p.stderr or p.stdout).strip() or f"Client-Exitcode {p.returncode}")
        return p.stdout

    # -- Operationen --------------------------------------------------------
    def version(self) -> str:
        return self._run(["version"], timeout=15).strip()

    def list_snapshots(self) -> list[Snapshot]:
        out = self._run(["snapshot", "list", "--output-format", "json", *self._ns()])
        try:
            data = json.loads(out or "[]")
        except ValueError as e:
            raise ClientError(f"Ungültige JSON-Ausgabe: {e}") from None
        return sorted((Snapshot.from_json(d) for d in data), key=lambda s: s.backup_time, reverse=True)

    def test_connection(self) -> None:
        self.list_snapshots()

    def backup_args(self) -> list[str]:
        p = self.profile
        if not p.sources:
            raise ClientError("Keine Quellpfade im Profil")
        args = ["backup", *p.archive_specs(), "--backup-id", p.backup_id, *self._ns()]
        for pattern in p.excludes:
            args += ["--exclude", pattern]
        if p.encrypt:
            if not p.keyfile:
                raise ClientError("Verschlüsselung aktiv, aber keine Schlüsseldatei gesetzt")
            args += ["--keyfile", p.keyfile, "--crypt-mode", "encrypt"]
        return args

    def backup(self, on_line: Callable[[str], None] = lambda _l: None) -> int:
        """Startet das Backup und liefert Zeilen live; gibt den Exitcode zurück."""
        try:
            proc = subprocess.Popen([self.binary, *self.backup_args()], env=self._env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except FileNotFoundError:
            raise ClientError(f"Client nicht gefunden: {self.binary}") from None
        assert proc.stdout
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()

    def catalog(self, snapshot: Snapshot, archive: str) -> str:
        return self._run(["catalog", "dump", snapshot.path, "--archive", archive, *self._ns()])

    def restore(self, snapshot: Snapshot, archive: str, target: str) -> None:
        """Stellt in einen (neuen) Zielordner wieder her; überschreibt nie das Original."""
        if not target or target == "/":
            raise ClientError("Ungültiges Wiederherstellungsziel")
        self._run(["restore", snapshot.path, archive, target, *self._ns()], timeout=None)
