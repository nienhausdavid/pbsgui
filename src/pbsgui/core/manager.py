"""Fassade für die GUI: Profile, Geheimnisse und Zeitpläne in beiden Scopes.

Die GUI-Kopie jedes Profils liegt immer im Benutzer-Scope (Quelle der Wahrheit für die Anzeige,
inkl. Token-Kopie zum Durchsuchen der Snapshots). Bei Scope "system" wird zusätzlich per
pkexec eine root-Kopie samt verschlüsselten Credentials und Units angelegt.
"""
from __future__ import annotations

import grp
import json
import time
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .client import PbsClient
from .dashboard import DatastoreUsage, Overview, build_overview, datastore_usage
from .history import RunRecord, find_run, list_runs, load_restore_test, save_restore_test
from .dbdump import Database, check_credentials, create_dump_user, dump_credentials, parse_credentials
from .files import Catalog, CopyResult, load_catalog, restore_to_folder, restore_to_original
from .keys import export_key, generate_key
from .models import Profile, Snapshot
from .modes import FULL
from .recovery import RecoveryError, ensure_recoverable
from .runner import run_profile
from .paths import Paths
from .privilege import PrivilegeError, require_trusted_code
from .scheduler import SchedulerError, SystemdScheduler
from .secrets import SecretStore, best_store
from .store import ProfileStore
from .versions import (CatalogCache, DiffResult, PreviewSession, RestoreTestResult, SearchResult, VersionsResult,
                       diff_snapshots, file_versions, profile_snapshots, restore_test, search_snapshots)


def can_use_admin() -> bool:
    """Grobe Erkennung, ob System-Scope möglich ist (root oder pkexec + Admin-Gruppe)."""
    if os.geteuid() == 0:
        return True
    if not shutil.which("pkexec"):
        return False
    me = {g.gr_gid for g in grp.getgrall() if os.environ.get("USER", "") in g.gr_mem}
    me.add(os.getgid())
    names = {grp.getgrgid(g).gr_name for g in me}
    return bool(names & {"sudo", "wheel", "admin"})


class ProfileManager:
    def __init__(self, user_paths: Paths | None = None, user_secrets: SecretStore | None = None):
        self.paths = user_paths or Paths.for_scope("user")
        self.profiles_store = ProfileStore(self.paths.profiles)
        self.secrets = user_secrets or best_store("user", self.paths.creds)

    # -- Lesen --------------------------------------------------------------
    def profiles(self) -> list[Profile]:
        return self.profiles_store.list()

    def client(self, profile: Profile) -> PbsClient:
        token = self.secrets.get(profile.slug, "token")
        if not token:
            raise SchedulerError("Kein Token-Secret gespeichert")
        return PbsClient(profile, token, self.secrets.get(profile.slug, "keypass") or "",
                         key_content=self.secrets.get(profile.slug, "key") or "")

    def catalogs(self, profile: Profile, snapshot: Snapshot) -> dict[str, Catalog]:
        """Dateibäume aller Datei-Archive des Snapshots (Name ohne .didx)."""
        return {k: v for k, v in load_catalog(self.client(profile), snapshot).items() if k.endswith(".pxar")}

    # -- Über Snapshots hinweg ------------------------------------------------
    def cache(self) -> CatalogCache:
        if getattr(self, "_cache", None) is None:
            self._cache = CatalogCache()
        return self._cache

    def _loader(self, profile: Profile):
        return self.cache().loader(self.client(profile), profile)

    def all_snapshots(self, profile: Profile) -> list[Snapshot]:
        """Snapshots dieses Profils (gleiche Backup-ID), neueste zuerst; räumt dabei den Katalog-Zwischenspeicher auf."""
        raw = self.snapshots(profile)
        self.cache().prune(profile, raw)
        return profile_snapshots(profile, raw)

    def file_versions(self, profile: Profile, path: str, archive: str | None = None, on_progress=None) -> VersionsResult:
        return file_versions(self.all_snapshots(profile), self._loader(profile), path, archive, on_progress)

    def search_all(self, profile: Profile, pattern: str, archive: str | None = None, on_progress=None) -> SearchResult:
        return search_snapshots(self.all_snapshots(profile), self._loader(profile), pattern, archive, on_progress=on_progress)

    def diff(self, profile: Profile, old: Snapshot, new: Snapshot, archive: str | None = None) -> list[DiffResult]:
        return diff_snapshots(self._loader(profile), old, new, archive)

    def preview_session(self, profile: Profile, snapshot: Snapshot, archive: str) -> PreviewSession:
        return PreviewSession(self.client(profile), snapshot, archive)

    def run_restore_test(self, profile: Profile, snapshot: Snapshot, archive: str, count: int = 5,
                         on_line=lambda _l: None) -> RestoreTestResult:
        cat = self._loader(profile)(snapshot).get(archive)
        if cat is None:
            raise SchedulerError(f"Archiv {archive} nicht im Snapshot")
        res = restore_test(self.client(profile), profile, snapshot, archive, cat, count, on_line=on_line)
        save_restore_test(self.paths, profile, res.to_dict())
        return res

    # -- Überblick und Verlauf --------------------------------------------------
    def _scopes(self) -> list[Paths]:
        """Verzeichnisse mit Läufen: Benutzer (dieses Manager-Objekts) und System."""
        return [self.paths, Paths.for_scope("system")]

    def history(self, profile: Profile | None = None, **kw) -> list[RunRecord]:
        return list_runs(self._scopes(), profile.slug if profile else None, **kw)

    def history_run(self, run_id: str, profile: Profile | None = None) -> RunRecord | None:
        return find_run(self._scopes(), run_id, profile.slug if profile else None)

    def last_restore_test(self, profile: Profile) -> dict | None:
        return load_restore_test(self._scopes(), profile)

    def overview(self, profile: Profile, online: bool = True) -> Overview:
        """Status eines Profils. online=False: nur lokale Daten (Verlauf, Sperre), keine Netzwerkabfrage."""
        snaps, err = None, ""
        if online:
            try:
                snaps = self.snapshots(profile)
            except Exception as e:
                err = str(e)
        return build_overview(profile, snaps, self._scopes(), error=err)

    def usage(self, profile: Profile) -> DatastoreUsage | None:
        token = self.secrets.get(profile.slug, "token")
        return datastore_usage(profile, token) if token else None

    def restore_files(self, profile: Profile, snapshot: Snapshot, archive: str, rels: list[str],
                      target: str | None = None, overwrite_home: bool = False, on_line=lambda _l: None) -> CopyResult:
        """Gewählte Dateien/Ordner zurückholen: in einen Zielordner oder (nur Home) an den Originalort."""
        client = self.client(profile)
        if overwrite_home:
            return restore_to_original(client, profile, snapshot, archive, rels, on_line=on_line)
        if not target:
            raise SchedulerError("Zielordner fehlt")
        return restore_to_folder(client, snapshot, archive, rels, Path(target), on_line=on_line)

    def has_key(self, profile: Profile) -> bool:
        return bool(profile.keyfile or self.secrets.exists(profile.slug, "key"))

    def enable_encryption(self, profile: Profile) -> bool:
        """Erzeugt automatisch einen Key, falls noch keiner existiert. True = neu erzeugt.

        Ein vorhandener Key wird nie überschrieben: damit wären alte Backups nicht mehr lesbar.
        """
        profile.encrypt = True
        if self.has_key(profile):
            return False
        content, fp = generate_key()
        profile.key_fingerprint = fp
        self.save(profile, key=content)
        return True

    def confirm_key_export(self, profile: Profile, path: str) -> None:
        """Eine bereits gesicherte Schlüsseldatei prüfen (Fingerprint) und den Export als erledigt vermerken."""
        import json as _json
        try:
            data = _json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            raise SchedulerError(f"Schlüsseldatei nicht lesbar: {e}") from None
        fp = data.get("fingerprint") if isinstance(data, dict) else None
        if not fp or "data" not in data:
            raise SchedulerError("Das ist keine Schlüsseldatei (Felder 'data'/'fingerprint' fehlen)")
        if fp != profile.key_fingerprint:
            raise SchedulerError(f"Falscher Schlüssel: Fingerprint der Datei {fp[:23]}… passt nicht zum Profil "
                                 f"{profile.key_fingerprint[:23]}…")
        stored = self.secrets.get(profile.slug, "key")
        if stored and _json.loads(stored).get("data") != data.get("data"):
            raise SchedulerError("Der Fingerprint stimmt, der Schlüsselinhalt aber nicht: Datei ist beschädigt")
        profile.key_exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save(profile)

    def export_key(self, profile: Profile, dest: str) -> None:
        content = self.secrets.get(profile.slug, "key")
        if not content:
            raise SchedulerError("Kein automatisch erzeugter Schlüssel vorhanden")
        export_key(content, dest)
        profile.key_exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save(profile)  # merkt sich den Export (bei Vollbackup auch in der System-Kopie)

    def snapshots(self, profile: Profile) -> list[Snapshot]:
        return self.client(profile).list_snapshots()

    # -- Schreiben ----------------------------------------------------------
    def save(self, profile: Profile, token: str | None = None, keypass: str | None = None,
             schedule: str = "keep", key: str | None = None, dbdump: str | None = None) -> None:
        """schedule: "install" | "remove" | "keep"."""
        if profile.backup_mode == FULL and profile.scope != "system":
            raise SchedulerError("Ein Vollbackup braucht den System-Modus (Zugriff auf alle Dateien).")
        if schedule == "install":
            ensure_recoverable(profile)  # Vollbackup-Zeitplan erst nach Schlüssel-Export
        self.profiles_store.save(profile)
        if token:
            self.secrets.put(profile.slug, "token", token)
        if keypass:
            self.secrets.put(profile.slug, "keypass", keypass)
        if key:
            self.secrets.put(profile.slug, "key", key)
        if dbdump:
            self.secrets.put(profile.slug, "dbdump", dbdump)
        if profile.scope == "system":
            self._admin({"profile": profile.to_dict(), "schedule": schedule,
                         "secrets": {"token": token or self.secrets.get(profile.slug, "token"),
                                     "keypass": keypass or self.secrets.get(profile.slug, "keypass"),
                                     "key": key or self.secrets.get(profile.slug, "key"),
                                     "dbdump": dbdump or self.secrets.get(profile.slug, "dbdump")}})
        elif schedule != "keep":
            sched = SystemdScheduler(self.paths, self.secrets)
            sched.install(profile) if schedule == "install" else sched.remove(profile)

    def apply_detection(self, profile: Profile) -> None:
        """Speichert geänderte Einstellungen zur Änderungserkennung und gleicht den wöchentlichen data-Timer an."""
        installed = bool(profile.schedule) and self.schedule_status(profile)["installed"]
        self.save(profile, schedule="install" if installed else "keep")

    # -- Zeitplan, der auf den Schlüssel-Export wartet ------------------------------------------------
    def schedule_pending(self, profile: Profile) -> bool:
        """Im Profil ist ein Zeitplan vermerkt, aber noch keine Timer-Unit eingerichtet."""
        return bool(profile.schedule) and not self.schedule_status(profile)["installed"]

    def activate_pending_schedule(self, profile: Profile) -> bool:
        """Richtet einen vermerkten Zeitplan ein. False = nichts zu tun oder noch gesperrt (Schlüssel-Export fehlt)."""
        if not self.schedule_pending(profile):
            return False
        try:
            ensure_recoverable(profile)
        except RecoveryError:
            return False
        self.save(profile, schedule="install")
        return True

    # -- Datenbank-Dumps ----------------------------------------------------------------------------------
    def db_credentials(self, profile: Profile) -> dict[str, dict]:
        return parse_credentials(self.secrets.get(profile.slug, "dbdump"))

    def set_db_credential(self, profile: Profile, db: Database, user: str, password: str, verify: bool = True) -> None:
        """Hinterlegt den Zugang eines MariaDB/MySQL-Containers verschlüsselt (nach Prüfung, wenn verify)."""
        if verify:
            check_credentials(db, user, password)
        creds = self.db_credentials(profile)
        creds[db.key] = {"user": user, "password": password}
        self._store_db_credentials(profile, creds)

    def create_db_credential(self, profile: Profile, db: Database, admin_user: str, admin_password: str) -> None:
        """Legt im Container einen Dump-Benutzer mit Leserechten an und hinterlegt ihn. Der Admin-Zugang wird nicht gespeichert."""
        cred = create_dump_user(db, admin_user, admin_password)
        check_credentials(db, cred["user"], cred["password"])
        creds = self.db_credentials(profile)
        creds[db.key] = cred
        self._store_db_credentials(profile, creds)

    def remove_db_credential(self, profile: Profile, key: str) -> bool:
        creds = self.db_credentials(profile)
        if key not in creds:
            return False
        del creds[key]
        self._store_db_credentials(profile, creds)
        return True

    def _store_db_credentials(self, profile: Profile, creds: dict[str, dict]) -> None:
        # Bei Zeitplan: Unit neu schreiben, damit sie das Geheimnis per LoadCredential lädt
        again = "install" if profile.schedule and self.schedule_status(profile)["installed"] else "keep"
        self.save(profile, schedule=again, dbdump=dump_credentials(creds) if creds else "{}")

    def delete(self, profile: Profile) -> None:
        if profile.scope == "system":
            self._admin({"profile": profile.to_dict(), "delete": True})
        else:
            SystemdScheduler(self.paths, self.secrets).remove(profile)
        self.secrets.delete(profile.slug)
        self.profiles_store.delete(profile.name)

    def schedule_status(self, profile: Profile) -> dict:
        paths = Paths.for_scope(profile.scope)
        store = self.secrets if profile.scope == "user" else best_store("system", paths.creds)
        return SystemdScheduler(paths, store).status(profile)

    def run_backup(self, profile: Profile, on_line=print, mode: str | None = None) -> int:
        """Führt das Backup jetzt aus. Vollbackup läuft privilegiert (pkexec), Home-Backup als Benutzer."""
        try:
            ensure_recoverable(profile)
        except RecoveryError as e:
            on_line(f"Fehler: {e}")
            return 4
        if profile.backup_mode != FULL:
            return run_profile(profile, Paths.for_scope("user"), self.secrets, on_line=on_line, progress=True, mode=mode)
        if os.geteuid() == 0:
            paths = Paths.for_scope("system")
            return run_profile(profile, paths, best_store("system", paths.creds), on_line=on_line, progress=True, mode=mode)
        try:
            require_trusted_code()
        except PrivilegeError as e:
            on_line(f"Fehler: {e}")
            return 4
        root = str(Path(__file__).resolve().parents[2])  # .../src
        cmd = ["pkexec", "env", f"PYTHONPATH={root}", "PBSGUI_PROGRESS=1", sys.executable, "-m", "pbsgui.cli.run", profile.slug,
               "--scope", "system", *(["--mode", mode] if mode else [])]
        on_line("Vollbackup braucht Administrator-Rechte: Passwortabfrage …")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, bufsize=1)
        if proc.stdout is None:
            raise RuntimeError("Kein Ausgabestrom vom Backup-Prozess")
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()

    def move_scope(self, profile: Profile, new_scope: str) -> None:
        """Wechselt User/System: entfernt den Zeitplan im alten Scope, setzt den neuen Scope (speichert nicht)."""
        if new_scope == profile.scope:
            return
        if profile.backup_mode == FULL:
            raise SchedulerError("Ein Vollbackup braucht den System-Modus und kann nicht gewechselt werden.")
        old = Profile.from_dict({**profile.to_dict(), "scope": profile.scope})
        self.save(old, schedule="remove")
        profile.scope = new_scope

    # -- Privilegierter Pfad ------------------------------------------------
    def _admin(self, request: dict) -> None:
        if os.geteuid() == 0:
            from ..cli.admin import apply
            apply(request)
            return
        try:
            require_trusted_code()
        except PrivilegeError as e:
            raise SchedulerError(str(e)) from None
        root = str(Path(__file__).resolve().parents[2])  # .../src
        r = subprocess.run(["pkexec", "env", f"PYTHONPATH={root}", sys.executable, "-m", "pbsgui.cli.admin"],
                           input=json.dumps(request), capture_output=True, text=True)
        if r.returncode != 0:
            raise SchedulerError(r.stderr.strip() or "Administrator-Aktion abgebrochen oder fehlgeschlagen")
