"""Profile als JSON-Dateien (ohne Geheimnisse)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Profile, slugify


class ProfileStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _path(self, name: str) -> Path:
        return self.dir / f"{slugify(name)}.json"

    def list(self) -> list[Profile]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(Profile.from_dict(json.loads(p.read_text())))
            except (OSError, ValueError, TypeError):
                continue  # defekte Datei überspringen
        return out

    def load(self, name: str) -> Profile:
        return Profile.from_dict(json.loads(self._path(name).read_text()))

    def save(self, profile: Profile) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(profile.to_dict(), f, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self._path(profile.name))

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
