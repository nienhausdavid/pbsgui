"""Fassade für die GUI: Profile, Geheimnisse und Zeitpläne in beiden Scopes.

Die GUI-Kopie jedes Profils liegt immer im Benutzer-Scope (Quelle der Wahrheit für die Anzeige,
inkl. Token-Kopie zum Durchsuchen der Snapshots). Bei Scope "system" wird zusätzlich per
pkexec eine root-Kopie samt verschlüsselten Credentials und Units angelegt.
"""
from __future__ import annotations

import grp
import json
import tempfile
import time
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..i18n import tr
from .client import PbsClient, client_binary
from .masterkey import _ram_dir, check_public_pem, create_master_key, pem_fingerprint, recover_key
from .dashboard import DatastoreUsage, Overview, build_overview, datastore_usage
from .history import RunRecord, find_run, list_runs, load_restore_test, save_restore_test
from .dbdump import Database, check_credentials, create_dump_user, dump_credentials, parse_credentials
from .files import Catalog, CopyResult, load_catalog, restore_to_folder, restore_to_original
from .keys import export_key, generate_key
from .models import Profile, Snapshot
from .modes import FULL
from .recovery import RecoveryError, ensure_recoverable, render_kit
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
            raise SchedulerError(tr("Kein Token-Secret gespeichert"))
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
            raise SchedulerError(tr("Archiv {0} nicht im Snapshot", archive))
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
            raise SchedulerError(tr("Zielordner fehlt"))
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
            raise SchedulerError(tr("Schlüsseldatei nicht lesbar: {0}", e)) from None
        fp = data.get("fingerprint") if isinstance(data, dict) else None
        if not fp or "data" not in data:
            raise SchedulerError(tr("Das ist keine Schlüsseldatei (Felder 'data'/'fingerprint' fehlen)"))
        if fp != profile.key_fingerprint:
            raise SchedulerError(tr("Falscher Schlüssel: Fingerprint der Datei {0}… passt nicht zum Profil {1}…", fp[:23], profile.key_fingerprint[:23]))
        stored = self.secrets.get(profile.slug, "key")
        if stored and _json.loads(stored).get("data") != data.get("data"):
            raise SchedulerError(tr("Der Fingerprint stimmt, der Schlüsselinhalt aber nicht: Datei ist beschädigt"))
        profile.key_exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save(profile)

    # -- Master-Schlüssel ----------------------------------------------------------
    def master_dir(self) -> Path:
        return self.paths.config

    def _apply_master(self, profile: Profile, public_pem: str, fingerprint: str) -> Path:
        d = self.master_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"master-public-{fingerprint.replace(':', '')[:16]}.pem"
        path.write_text(public_pem)
        os.chmod(path, 0o644)
        profile.master_pubkey, profile.master_fingerprint = str(path), fingerprint
        return path

    def setup_master_key(self, profile: Profile, passphrase: str, all_profiles: bool = True):
        """Erzeugt ein Master-Schlüsselpaar und trägt den öffentlichen Teil ein (bei Wunsch in allen verschlüsselten Profilen).

        Gibt das MasterKey-Ergebnis zurück: der PRIVATE Teil steht nur dort, pbsgui speichert ihn nicht. Der Aufrufer muss ihn dem Benutzer
        zum Sichern anbieten und danach `confirm_master_key_backup` aufrufen.
        """
        key = create_master_key(passphrase, client_binary())
        targets = [profile] + ([p for p in self.profiles() if p.slug != profile.slug and p.encrypt and not p.keyfile and not p.master_pubkey]
                               if all_profiles else [])
        for p in targets:
            self._apply_master(p, key.public_pem, key.fingerprint)
            p.master_key_ack_at = ""
            self.save(p)
        return key

    def use_master_pubkey(self, profile: Profile, public_pem: str) -> str:
        """Einen vorhandenen öffentlichen Master-Schlüssel (z. B. von einem anderen Gerät) verwenden. Gibt den Fingerprint zurück."""
        pem = check_public_pem(public_pem)
        fp = pem_fingerprint(pem)
        self._apply_master(profile, pem, fp)
        profile.master_key_ack_at = time.strftime("%Y-%m-%d %H:%M:%S")    # der private Teil gehört schon zu einem gesicherten Schlüsselpaar
        self.save(profile)
        return fp

    def confirm_master_key_backup(self, profile: Profile) -> None:
        profile.master_key_ack_at = time.strftime("%Y-%m-%d %H:%M:%S")
        for p in self.profiles():                        # gleicher öffentlicher Schlüssel = gleicher privater Teil
            if p.master_fingerprint and p.master_fingerprint == profile.master_fingerprint:
                p.master_key_ack_at = profile.master_key_ack_at
                self.save(p)

    def recover_key(self, profile: Profile, snapshot: Snapshot, master_private_pem: str, passphrase: str) -> str:
        """Verschlüsselungs-Schlüssel (key.json als Text) mit dem privaten Master-Schlüssel aus einem Snapshot zurückholen."""
        with tempfile.TemporaryDirectory(prefix="pbsgui-blob-", dir=_ram_dir()) as tmp:
            blob = self.client(profile).restore_encrypted_key(snapshot, str(Path(tmp) / "rsa-encrypted.key"))
        return recover_key(client_binary(), blob, master_private_pem, passphrase)

    def install_recovered_key(self, profile: Profile, key_json: str) -> None:
        """Zurückgeholten Schlüssel im Profil einsetzen. Nur erlaubt, wenn er zum Profil passt oder noch keiner da ist."""
        import json as _json
        data = _json.loads(key_json)
        fp = data.get("fingerprint", "")
        if profile.key_fingerprint and fp and fp != profile.key_fingerprint and self.secrets.exists(profile.slug, "key"):
            raise SchedulerError(tr("Der zurückgeholte Schlüssel ({0}…) passt nicht zum vorhandenen Schlüssel des Profils ({1}…)", fp[:23], profile.key_fingerprint[:23]))
        profile.key_fingerprint = fp or profile.key_fingerprint
        profile.encrypt = True
        self.save(profile, key=key_json)
        self.mark_key_exported(profile)

    def key_text(self, profile: Profile) -> str:
        """Schlüssel (JSON) zum Kopieren, etwa in die Zwischenablage."""
        content = self.secrets.get(profile.slug, "key")
        if not content:
            raise SchedulerError(tr("Kein automatisch erzeugter Schlüssel vorhanden"))
        return content

    def emergency_kit(self, profile: Profile) -> str:
        """Notfallblatt und Schlüssel in einem Text (enthält das Geheimnis: nur im Passwortmanager ablegen)."""
        return render_kit(profile, self.key_text(profile))

    def mark_key_exported(self, profile: Profile) -> None:
        """Vermerkt, dass der Schlüssel gesichert wurde (z. B. per Zwischenablage in den Passwortmanager)."""
        profile.key_exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save(profile)

    def export_key(self, profile: Profile, dest: str) -> None:
        content = self.secrets.get(profile.slug, "key")
        if not content:
            raise SchedulerError(tr("Kein automatisch erzeugter Schlüssel vorhanden"))
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
            raise SchedulerError(tr("Ein Vollbackup braucht den System-Modus (Zugriff auf alle Dateien)."))
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
            on_line(tr("Fehler: {0}", e))
            return 4
        if profile.backup_mode != FULL:
            return run_profile(profile, Paths.for_scope("user"), self.secrets, on_line=on_line, progress=True, mode=mode)
        if os.geteuid() == 0:
            paths = Paths.for_scope("system")
            return run_profile(profile, paths, best_store("system", paths.creds), on_line=on_line, progress=True, mode=mode)
        try:
            require_trusted_code()
        except PrivilegeError as e:
            on_line(tr("Fehler: {0}", e))
            return 4
        root = str(Path(__file__).resolve().parents[2])  # .../src
        cmd = ["pkexec", "env", f"PYTHONPATH={root}", "PBSGUI_PROGRESS=1", sys.executable, "-m", "pbsgui.cli.run", profile.slug,
               "--scope", "system", *(["--mode", mode] if mode else [])]
        on_line(tr("Vollbackup braucht Administrator-Rechte: Passwortabfrage …"))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, bufsize=1)
        if proc.stdout is None:
            raise RuntimeError(tr("Kein Ausgabestrom vom Backup-Prozess"))
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()

    def move_scope(self, profile: Profile, new_scope: str) -> None:
        """Wechselt User/System: entfernt den Zeitplan im alten Scope, setzt den neuen Scope (speichert nicht)."""
        if new_scope == profile.scope:
            return
        if profile.backup_mode == FULL:
            raise SchedulerError(tr("Ein Vollbackup braucht den System-Modus und kann nicht gewechselt werden."))
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
            raise SchedulerError(r.stderr.strip() or tr("Administrator-Aktion abgebrochen oder fehlgeschlagen"))
