"""`pbsgui-run NAME [--scope user|system]` — wird von den systemd-Timern aufgerufen."""
from __future__ import annotations

import argparse
import sys

from ..core.paths import Paths
from ..core.runner import run_profile
from ..core.secrets import best_store
from ..core.store import ProfileStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pbsgui-run")
    ap.add_argument("profile")
    ap.add_argument("--scope", choices=["user", "system"], default="user")
    args = ap.parse_args(argv)
    paths = Paths.for_scope(args.scope)
    try:
        profile = ProfileStore(paths.profiles).load(args.profile)
    except (OSError, ValueError):
        print(f"Profil nicht gefunden: {args.profile}", file=sys.stderr)
        return 1
    return run_profile(profile, paths, best_store(args.scope, paths.creds))


if __name__ == "__main__":
    sys.exit(main())
