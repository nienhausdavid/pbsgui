"""Wrapper um `proxmox-backup-client`. Geheimnisse gehen nur über Umgebungsvariablen."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

from pathlib import Path

from ..i18n import tr
from .models import DATA, METADATA, Profile, Snapshot
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
        self.dump_dir: Path | None = None  # gesetzt vom Runner, wenn das Profil Datenbank-Dumps anlegt
        self.mode_override: str | None = None  # z. B. DATA: einmalig alles neu lesen, unabhängig vom Profil

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

    def _run(self, args: list[str], timeout: float | None = 120, merge_stderr: bool = False) -> str:
        try:
            p = subprocess.run([self.binary, *args], env=self._env(), capture_output=True,
                               text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise ClientError(tr("Client nicht gefunden: {0}", self.binary)) from None
        except subprocess.TimeoutExpired:
            raise ClientError(tr("Zeitüberschreitung beim Aufruf des Clients")) from None
        if p.returncode != 0:
            raise ClientError((p.stderr or p.stdout).strip() or tr("Client-Exitcode {0}", p.returncode))
        return (p.stdout + p.stderr) if merge_stderr else p.stdout

    # -- Operationen --------------------------------------------------------
    def version(self) -> str:
        return self._run(["version"], timeout=15).strip()

    def list_snapshots(self) -> list[Snapshot]:
        out = self._run(["snapshot", "list", "--output-format", "json", *self._ns()])
        try:
            data = json.loads(out or "[]")
        except ValueError as e:
            raise ClientError(tr("Ungültige JSON-Ausgabe: {0}", e)) from None
        return sorted((Snapshot.from_json(d) for d in data), key=lambda s: s.backup_time, reverse=True)

    def test_connection(self) -> None:
        self.list_snapshots()

    def backup_args(self, keyfile: str = "", mounts: list[str] | None = None,
                    binds: list[str] | None = None, recovery_dir: str | None = None) -> list[str]:
        """mounts/binds überschreiben die Erkennung (für Tests); sonst wird die aktuelle Mount-Tabelle gelesen."""
        p = self.profile
        keyfile = keyfile or p.keyfile
        if not p.effective_sources:
            raise ClientError(tr("Keine Quellpfade im Profil"))
        specs = p.archive_specs()
        if p.backup_mode == FULL:
            # Eigenes kleines Archiv mit den Datenträger-Metadaten: die Rettungs-ISO holt damit das Layout in
            # Sekunden, ohne das ganze System entpacken zu müssen (der Client kann kein Teil-Restore).
            rd = Path(recovery_dir) if recovery_dir else Paths.for_scope("system").state / "recovery" / p.slug
            if rd.is_dir():
                specs.append(f"recovery.pxar:{rd}")
        if p.db_dump and self.dump_dir and self.dump_dir.is_dir() and any(self.dump_dir.iterdir()):
            specs.append(f"dbdumps.pxar:{self.dump_dir}")  # Dumps als eigenes Archiv, auch außerhalb der Quellen
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
        mode = self.mode_override or p.change_detection
        if mode in (METADATA, DATA):  # METADATA: nur Geändertes lesen; DATA: alles lesen, aber geteilte Archive
            args += ["--change-detection-mode", mode]
        if p.encrypt:
            if not keyfile:
                raise ClientError(tr("Verschlüsselung aktiv, aber kein Schlüssel vorhanden"))
            args += ["--keyfile", keyfile, "--crypt-mode", "encrypt"]
        if p.master_pubkey:
            args += ["--master-pubkey-file", p.master_pubkey]
        return args

    def backup(self, on_line: Callable[[str], None] = lambda _l: None,
               on_progress: Callable | None = None) -> int:
        """Startet das Backup und liefert Zeilen live; gibt den Exitcode zurück.

        on_progress: wird etwa einmal pro Sekunde mit einem progress.Progress aufgerufen (der Client selbst gibt
        beim Backup keinen Fortschritt aus).
        """
        with self._keyfile() as keyfile:
            try:
                proc = subprocess.Popen([self.binary, *self.backup_args(keyfile)], env=self._env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, bufsize=1)
            except FileNotFoundError:
                raise ClientError(tr("Client nicht gefunden: {0}", self.binary)) from None
            if proc.stdout is None:
                raise ClientError(tr("Kein Ausgabestrom vom Client"))
            sampler = None
            if on_progress is not None:
                from . import progress as prog
                try:
                    total, basis = prog.estimate_total(self.profile, self.list_snapshots(), plan_mounts(self.profile.full_mounts)[0]
                                                       if self.profile.backup_mode == FULL else None, self.mode_override)
                except ClientError:
                    total, basis = None, ""
                sampler = prog.Sampler(proc.pid, total, basis, on_progress)
                sampler.start()
            try:
                for line in proc.stdout:
                    on_line(line.rstrip("\n"))
                return proc.wait()
            finally:
                if sampler:
                    sampler.stop()

    def catalog(self, snapshot: Snapshot) -> str:
        """Dateiliste aller Archive des Snapshots (`catalog dump`; bei verschlüsselten Backups mit Schlüssel)."""
        with self._keyfile() as kf:
            # Gemessen mit Client 4.2.8: `catalog dump` schreibt die Dateiliste auf STDERR (stdout bleibt leer)
            return self._run(["catalog", "dump", snapshot.path, *self._ns(), *(["--keyfile", kf] if kf else [])],
                             timeout=600, merge_stderr=True)

    def mount(self, snapshot: Snapshot, archive: str, target: str) -> None:
        """Hängt ein Archiv per FUSE ein. Der Client läuft danach als Hintergrundprozess weiter (Aushängen: fusermount3 -u)."""
        with self._keyfile() as kf:
            args = [self.binary, "mount", snapshot.path, archive, target, *self._ns(), *(["--keyfile", kf] if kf else [])]
            try:
                p = subprocess.run(args, env=self._env(), capture_output=True, text=True, timeout=60,
                                   stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise ClientError(tr("Client nicht gefunden: {0}", self.binary)) from None
            except subprocess.TimeoutExpired:
                raise ClientError(tr("Zeitüberschreitung beim Einhängen")) from None
            if p.returncode != 0:
                raise ClientError((p.stderr or p.stdout).strip() or tr("Client-Exitcode {0}", p.returncode))

    def restore_text(self, snapshot: Snapshot, archive: str) -> str:
        """Liest eine kleine Datei (z. B. qemu-server.conf) eines Snapshots und gibt den Inhalt zurück."""
        with self._keyfile() as kf:
            return self._run(["restore", snapshot.path, archive, "-", *self._ns(), *(["--keyfile", kf] if kf else [])])

    def restore_image(self, snapshot: Snapshot, archive: str, target: str, on_line=None) -> None:
        """Schreibt ein Abbild-Archiv (.img) auf ein Blockgerät oder in eine Datei.

        Der Client legt sein Ziel selbst mit `create_new` an und scheitert deshalb bei einem vorhandenen Gerät ("File exists");
        darum liest er auf die Standardausgabe und pbsgui schreibt in das Ziel (und synchronisiert am Ende).
        Die Meldungen des Clients und alle 256 MiB der Stand gehen zeilenweise an on_line.
        """
        if not target or target == "/" or target == "-":
            raise ClientError(tr("Ungültiges Wiederherstellungsziel"))
        say = on_line or (lambda _l: None)
        with self._keyfile() as kf:
            args = ["restore", snapshot.path, archive, "-", *self._ns(), *(["--keyfile", kf] if kf else [])]
            try:
                proc = subprocess.Popen([self.binary, *args], env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise ClientError(tr("Client nicht gefunden: {0}", self.binary)) from None
            if proc.stdout is None or proc.stderr is None:
                raise ClientError(tr("Kein Ausgabestrom vom Client"))
            tail: list[str] = []

            def read_err() -> None:
                for raw in proc.stderr:           # type: ignore[union-attr]
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        tail.append(line)
                        del tail[:-8]
                        say(line)
            t = threading.Thread(target=read_err, daemon=True)
            t.start()
            written, mark = 0, 0
            try:
                with open(target, "r+b" if os.path.exists(target) else "wb", buffering=0) as out:
                    while True:
                        chunk = proc.stdout.read(4 * 2**20)
                        if not chunk:
                            break
                        out.write(chunk)
                        written += len(chunk)
                        if written - mark >= 256 * 2**20:
                            mark = written
                            say(tr("{0} MiB geschrieben", written >> 20))
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as e:
                proc.kill()
                raise ClientError(tr("Schreiben auf {0} fehlgeschlagen: {1}", target, e)) from None
            finally:
                code = proc.wait()
                t.join(timeout=5)
            if code != 0:
                raise ClientError("\n".join(tail).strip() or tr("restore fehlgeschlagen (Exitcode {0})", code))
            say(tr("{0} MiB auf {1} geschrieben", written >> 20, target))

    def restore(self, snapshot: Snapshot, archive: str, target: str) -> None:
        """Stellt in einen (neuen) Zielordner wieder her; überschreibt nie das Original."""
        if not target or target == "/":
            raise ClientError(tr("Ungültiges Wiederherstellungsziel"))
        with self._keyfile() as kf:
            self._run(["restore", snapshot.path, archive, target, *self._ns(),
                       *(["--keyfile", kf] if kf else [])], timeout=None)
