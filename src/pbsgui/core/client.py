"""Wrapper um `proxmox-backup-client`. Geheimnisse gehen nur über Umgebungsvariablen."""
from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from typing import Callable, Iterator

from pathlib import Path

from .models import Profile, Snapshot
from .paths import Paths
from .modes import DEFAULT_FULL_EXCLUDES, FULL, plan_mounts


class ClientError(RuntimeError):
    pass


def client_binary() -> str:
    return os.environ.get("PBSGUI_CLIENT", "proxmox-backup-client")


class PbsClient:
    def __init__(self, profile: Profile, secret: str, key_password: str = "", binary: str | None = None,
                 key_content: str = "", key_path: str = ""):
        self.profile = profile
        self.secret = secret
        self.key_password = key_password
        self.binary = binary or client_binary()
        self.key_content = key_content  # Key-JSON (aus dem Geheimnis-Speicher)
        self.key_path = key_path        # oder Pfad zu einer vorhandenen Datei (systemd-Credential)

    @contextmanager
    def _keyfile(self) -> Iterator[str]:
        """Liefert den Pfad zur Schlüsseldatei (leer = kein Key); temporäre Dateien werden danach gelöscht."""
        from .keys import materialized_key
        if self.key_path:
            yield self.key_path
        elif self.key_content:
            with materialized_key(self.key_content) as p:
                yield p
        else:
            yield self.profile.keyfile

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

    def backup_args(self, keyfile: str = "", mounts: list[str] | None = None,
                    binds: list[str] | None = None, recovery_dir: str | None = None) -> list[str]:
        """mounts/binds überschreiben die Erkennung (für Tests); sonst wird die aktuelle Mount-Tabelle gelesen."""
        p = self.profile
        keyfile = keyfile or p.keyfile
        if not p.effective_sources:
            raise ClientError("Keine Quellpfade im Profil")
        specs = p.archive_specs()
        if p.backup_mode == FULL:
            # Eigenes kleines Archiv mit den Datenträger-Metadaten: die Rettungs-ISO holt damit das Layout in
            # Sekunden, ohne das ganze System entpacken zu müssen (der Client kann kein Teil-Restore).
            rd = Path(recovery_dir) if recovery_dir else Paths.for_scope("system").state / "recovery" / p.slug
            if rd.is_dir():
                specs.append(f"recovery.pxar:{rd}")
        args = ["backup", *specs, "--backup-id", p.backup_id, *self._ns()]
        excludes = list(p.excludes)
        if p.backup_mode == FULL:
            if mounts is None:
                mounts, binds = plan_mounts(p.full_mounts)  # gewählte Laufwerke, Bind-Duplikate
            excludes = [*DEFAULT_FULL_EXCLUDES, *(binds or []), *excludes]  # Duplikate nicht doppelt lesen
            for mount in mounts:
                args += ["--include-dev", mount]
            args += ["--skip-lost-and-found", "true"]
        for pattern in excludes:
            args += ["--exclude", pattern]
        if p.encrypt:
            if not keyfile:
                raise ClientError("Verschlüsselung aktiv, aber kein Schlüssel vorhanden")
            args += ["--keyfile", keyfile, "--crypt-mode", "encrypt"]
        if p.master_pubkey:
            args += ["--master-pubkey-file", p.master_pubkey]
        return args

    def backup(self, on_line: Callable[[str], None] = lambda _l: None) -> int:
        """Startet das Backup und liefert Zeilen live; gibt den Exitcode zurück."""
        with self._keyfile() as keyfile:
            try:
                proc = subprocess.Popen([self.binary, *self.backup_args(keyfile)], env=self._env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, bufsize=1)
            except FileNotFoundError:
                raise ClientError(f"Client nicht gefunden: {self.binary}") from None
            assert proc.stdout
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
            return proc.wait()

    def catalog(self, snapshot: Snapshot, archive: str) -> str:
        with self._keyfile() as kf:
            return self._run(["catalog", "dump", snapshot.path, "--archive", archive, *self._ns(),
                              *(["--keyfile", kf] if kf else [])])

    def restore(self, snapshot: Snapshot, archive: str, target: str) -> None:
        """Stellt in einen (neuen) Zielordner wieder her; überschreibt nie das Original."""
        if not target or target == "/":
            raise ClientError("Ungültiges Wiederherstellungsziel")
        with self._keyfile() as kf:
            self._run(["restore", snapshot.path, archive, target, *self._ns(),
                       *(["--keyfile", kf] if kf else [])], timeout=None)
