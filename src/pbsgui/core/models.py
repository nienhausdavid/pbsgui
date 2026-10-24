"""Datenmodelle: Backup-Profil und Snapshot."""
from __future__ import annotations

import re
import socket
from dataclasses import asdict, dataclass, field, fields


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-").lower() or "profile"


LEGACY = "legacy"      # Client liest jede Datei jedes Mal vollständig und lädt nur Neues hoch
METADATA = "metadata"  # Client liest nur Dateien, deren Metadaten sich seit dem letzten Backup geändert haben
CHANGE_MODES = (METADATA, LEGACY)
DATA = "data"          # wie METADATA, liest aber alles neu und prüft so auch Dateien mit unveränderten Metadaten
DEFAULT_DATA_SCHEDULE = "Sun *-*-* 03:00:00"   # wöchentlich, sonntags 03:00 Uhr

_ARCHIVE_FILE = re.compile(r"^(?P<base>.+?)\.(?:pxar|mpxar|ppxar)(?:\.didx)?$")


def logical_archive(filename: str) -> str | None:
    """Archivname, wie ihn `restore`/`mount` erwarten, aus einem Dateinamen im Snapshot.

    Im Modus `legacy` heißt das Archiv `x.pxar.didx`; im Modus `metadata` teilt der Client es in `x.mpxar.didx`
    (Metadaten) und `x.ppxar.didx` (Inhalt). Beide ergeben `x.pxar`. Andere Dateien (Katalog, Manifest): None.
    """
    m = _ARCHIVE_FILE.match(filename)
    return f"{m['base']}.pxar" if m else None


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
    key_exported_at: str = ""  # Zeitpunkt des Schlüssel-Exports (Pflicht beim Vollbackup)
    master_pubkey: str = ""  # optional: RSA-Master-Public-Key (--master-pubkey-file)
    master_fingerprint: str = ""  # SHA-256 des öffentlichen Master-Schlüssels (zur Wiedererkennung, nicht geheim)
    master_key_ack_at: str = ""  # Zeitpunkt, an dem bestätigt wurde, dass der PRIVATE Master-Schlüssel gesichert ist
    schedule: str = ""  # systemd OnCalendar, leer = kein Zeitplan
    scope: str = "user"  # "user" | "system"
    require_ac: bool = False
    db_dump: bool = False  # laufende Datenbank-Container (Postgres, MariaDB/MySQL) vor dem Backup dumpen
    change_detection: str = LEGACY  # METADATA = nur Geändertes lesen; bestehende Profile bleiben bei LEGACY
    weekly_data: bool = False  # zusätzlich ein Lauf im Modus "data" (alles neu lesen); nur mit METADATA und Zeitplan
    data_schedule: str = DEFAULT_DATA_SCHEDULE

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def weekly_data_active(self) -> bool:
        """Es gibt einen eigenen Zeitplan für den Lauf im Modus data (nur zusammen mit METADATA und einem Zeitplan)."""
        return bool(self.weekly_data and self.change_detection == METADATA and self.schedule and self.data_schedule)

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
    file_sizes: dict[str, int] = field(default_factory=dict)   # Dateiname -> Größe (bei Abbild-Archiven: Größe des Abbilds)

    @property
    def archives(self) -> list[str]:
        """Datei-Archive des Snapshots unter dem Namen, den `restore`/`mount` erwarten (`x.pxar`, ohne Duplikate)."""
        seen: list[str] = []
        for f in self.files:
            a = logical_archive(f)
            if a and a not in seen:
                seen.append(a)
        return seen

    @property
    def split(self) -> bool:
        """Snapshot wurde im Modus `metadata` erstellt (geteilte Archive)."""
        return any(f.endswith(".mpxar.didx") for f in self.files)

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
            file_sizes={f["filename"]: int(f.get("size") or 0) for f in d.get("files", []) if isinstance(f, dict) and "filename" in f},
        )
