"""Verzeichnisse je Scope (Benutzer oder System)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    scope: str
    config: Path
    units: Path
    creds: Path
    state: Path

    @property
    def profiles(self) -> Path:
        return self.config / "profiles"

    @property
    def systemctl(self) -> list[str]:
        return ["systemctl", "--user"] if self.scope == "user" else ["systemctl"]

    @classmethod
    def for_scope(cls, scope: str) -> "Paths":
        if scope == "system":
            return cls("system", Path("/etc/pbsgui"), Path("/etc/systemd/system"),
                       Path("/etc/pbsgui/creds"), Path("/var/lib/pbsgui"))
        if scope != "user":
            raise ValueError(f"unbekannter Scope: {scope}")
        home = Path.home()
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
        return cls("user", cfg / "pbsgui", cfg / "systemd/user", cfg / "pbsgui/creds", state / "pbsgui")
