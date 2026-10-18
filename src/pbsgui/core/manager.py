"""Fassade für die GUI: Profile, Geheimnisse und Zeitpläne in beiden Scopes.

Die GUI-Kopie jedes Profils liegt immer im Benutzer-Scope (Quelle der Wahrheit für die Anzeige,
inkl. Token-Kopie zum Durchsuchen der Snapshots). Bei Scope "system" wird zusätzlich per
pkexec eine root-Kopie samt verschlüsselten Credentials und Units angelegt.
"""
from __future__ import annotations

import grp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .client import PbsClient
from .models import Profile, Snapshot
from .paths import Paths
from .scheduler import SchedulerError, SystemdScheduler
from .secrets import SecretStore, best_store
from .store import ProfileStore


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
        return PbsClient(profile, token, self.secrets.get(profile.slug, "keypass") or "")

    def snapshots(self, profile: Profile) -> list[Snapshot]:
        return self.client(profile).list_snapshots()

    # -- Schreiben ----------------------------------------------------------
    def save(self, profile: Profile, token: str | None = None, keypass: str | None = None,
             schedule: str = "keep") -> None:
        """schedule: "install" | "remove" | "keep"."""
        self.profiles_store.save(profile)
        if token:
            self.secrets.put(profile.slug, "token", token)
        if keypass:
            self.secrets.put(profile.slug, "keypass", keypass)
        if profile.scope == "system":
            self._admin({"profile": profile.to_dict(), "schedule": schedule,
                         "secrets": {"token": token or self.secrets.get(profile.slug, "token"),
                                     "keypass": keypass or self.secrets.get(profile.slug, "keypass")}})
        elif schedule != "keep":
            sched = SystemdScheduler(self.paths, self.secrets)
            sched.install(profile) if schedule == "install" else sched.remove(profile)

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

    # -- Privilegierter Pfad ------------------------------------------------
    def _admin(self, request: dict) -> None:
        if os.geteuid() == 0:
            from ..cli.admin import apply
            apply(request)
            return
        root = str(Path(__file__).resolve().parents[2])  # .../src
        r = subprocess.run(["pkexec", "env", f"PYTHONPATH={root}", sys.executable, "-m", "pbsgui.cli.admin"],
                           input=json.dumps(request), capture_output=True, text=True)
        if r.returncode != 0:
            raise SchedulerError(r.stderr.strip() or "Administrator-Aktion abgebrochen oder fehlgeschlagen")
