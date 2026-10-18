"""Privilegierter Helfer für System-Scope. Wird per `pkexec` gestartet; liest JSON von stdin.

Anfrage: {"profile": {...}, "secrets": {"token": "...", "keypass": "..."},
          "schedule": "install"|"remove"|"keep", "delete": false}
"""
from __future__ import annotations

import json
import os
import sys

from ..core.models import Profile
from ..core.paths import Paths
from ..core.scheduler import SystemdScheduler
from ..core.secrets import best_store
from ..core.store import ProfileStore


def apply(request: dict, paths: Paths | None = None, store=None, run=None) -> None:
    paths = paths or Paths.for_scope("system")
    store = store or best_store("system", paths.creds)
    profile = Profile.from_dict(request["profile"])
    sched = SystemdScheduler(paths, store, **({"run": run} if run else {}))
    if request.get("delete"):
        sched.remove(profile)
        store.delete(profile.slug)
        ProfileStore(paths.profiles).delete(profile.name)
        return
    ProfileStore(paths.profiles).save(profile)
    for kind, value in (request.get("secrets") or {}).items():
        if value:
            store.put(profile.slug, kind, value)
    action = request.get("schedule", "keep")
    if action == "install":
        sched.install(profile)
    elif action == "remove":
        sched.remove(profile)


def main() -> int:
    if os.geteuid() != 0:
        print("Muss als root laufen (pkexec).", file=sys.stderr)
        return 1
    try:
        apply(json.load(sys.stdin))
    except Exception as e:  # Fehlermeldung an den Aufrufer weiterreichen
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
