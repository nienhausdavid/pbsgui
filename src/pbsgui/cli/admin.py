"""Privilegierter Helfer für System-Scope. Wird per `pkexec` gestartet; liest JSON von stdin.

Anfrage: {"profile": {...}, "secrets": {"token": "...", "keypass": "..."},
          "schedule": "install"|"remove"|"keep", "delete": false}
"""
from __future__ import annotations

import json
import os
import sys

from ..i18n import tr
from ..core.models import Profile
from ..core.paths import Paths
from ..core.scheduler import SystemdScheduler
from ..core.secrets import CRED_NAMES, best_store
from ..core.store import ProfileStore


def apply(request: dict, paths: Paths | None = None, store=None, run=None) -> None:
    paths = paths or Paths.for_scope("system")
    store = store or best_store("system", paths.creds)
    profile = Profile.from_dict(request["profile"])
    secrets = request.get("secrets") or {}
    bad = [k for k in secrets if k not in CRED_NAMES]
    if bad:   # sonst könnte ein Name wie "../../x" als root beliebige Dateien anlegen
        raise ValueError(tr("Unbekannte Geheimnis-Art: {0!r}", bad[0]))
    if request.get("schedule", "keep") not in ("install", "remove", "keep"):
        raise ValueError(tr("Unbekannte Zeitplan-Aktion"))
    sched = SystemdScheduler(paths, store, **({"run": run} if run else {}))
    if request.get("delete"):
        sched.remove(profile)
        store.delete(profile.slug)
        ProfileStore(paths.profiles).delete(profile.name)
        return
    ProfileStore(paths.profiles).save(profile)
    for kind, value in secrets.items():
        if value:
            store.put(profile.slug, kind, value)
    action = request.get("schedule", "keep")
    if action == "install":
        sched.install(profile)
    elif action == "remove":
        sched.remove(profile)


def main() -> int:
    if os.geteuid() != 0:
        print(tr("Muss als root laufen (pkexec)."), file=sys.stderr)
        return 1
    try:
        apply(json.load(sys.stdin))
    except Exception as e:  # Fehlermeldung an den Aufrufer weiterreichen
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
