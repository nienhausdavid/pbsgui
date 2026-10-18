"""Datenmodelle: Backup-Profil und Snapshot."""
from __future__ import annotations

import re
import socket
from dataclasses import asdict, dataclass, field, fields


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-").lower() or "profile"


def archive_name(path: str) -> str:
    """Archivname (ohne .pxar) aus einem Quellpfad, z. B. /home/david -> home-david."""
    p = path.strip("/")
    return slugify(p.replace("/", "-")) if p else "root"


@dataclass
class Profile:
    name: str
    server: str
    datastore: str
    auth_id: str  # user@realm!tokenname
    fingerprint: str = ""
    port: int = 8007
    namespace: str = ""
    backup_id: str = field(default_factory=socket.gethostname)
    backup_mode: str = "home"  # "home" = nur Home, "full" = ganzes System (braucht System-Modus/root)
    full_mounts: list[str] | None = None  # Vollbackup: gewählte Laufwerke; None = Standardauswahl (intern)
    sources: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    encrypt: bool = True  # clientseitige Verschlüsselung standardmäßig an
    keyfile: str = ""  # optional: eigene, extern verwaltete Schlüsseldatei
    key_fingerprint: str = ""  # Fingerprint des automatisch erzeugten Keys (öffentlich)
    master_pubkey: str = ""  # optional: RSA-Master-Public-Key (--master-pubkey-file)
    schedule: str = ""  # systemd OnCalendar, leer = kein Zeitplan
    scope: str = "user"  # "user" | "system"
    require_ac: bool = False

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def repository(self) -> str:
        return f"{self.auth_id}@{self.server}:{self.port}:{self.datastore}"

    @property
    def effective_sources(self) -> list[str]:
        return ["/"] if self.backup_mode == "full" else self.sources

    def archive_specs(self) -> list[str]:
        """`name.pxar:/pfad`-Angaben für den Client; Namen werden eindeutig gemacht."""
        seen: dict[str, int] = {}
        specs = []
        for src in self.effective_sources:
            base = archive_name(src)
            n = seen.get(base, 0)
            seen[base] = n + 1
            specs.append(f"{base if n == 0 else f'{base}-{n}'}.pxar:{src}")
        return specs

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Snapshot:
    backup_type: str
    backup_id: str
    backup_time: int
    size: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Snapshot-Pfad wie ihn der Client erwartet: host/id/2026-01-01T00:00:00Z."""
        from datetime import datetime, timezone

        ts = datetime.fromtimestamp(self.backup_time, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{self.backup_type}/{self.backup_id}/{ts}"

    @classmethod
    def from_json(cls, d: dict) -> "Snapshot":
        files = [f["filename"] if isinstance(f, dict) else f for f in d.get("files", [])]
        return cls(
            backup_type=d["backup-type"],
            backup_id=d["backup-id"],
            backup_time=int(d["backup-time"]),
            size=int(d.get("size") or 0),
            files=files,
        )
