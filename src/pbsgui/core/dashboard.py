"""Überblick über alle Profile: letztes Backup, Dauer, Größe, Wachstum und Speicherverbrauch im Datastore."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..i18n import tr
from .history import RunRecord, list_runs
from .models import Profile, Snapshot
from .paths import Paths
from .pbs_api import ApiError, PbsApi
from .runlock import is_locked
from .versions import profile_snapshots

DAY = 86400
STALE_AFTER = 2 * DAY          # Zeitplan-Profil ohne Backup seit so langer Zeit gilt als überfällig


@dataclass
class DatastoreUsage:
    total: int
    used: int
    avail: int
    dedup: float | None = None       # Datastore-weit, Stand der letzten Müllsammlung (index-data-bytes / disk-bytes)

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


@dataclass
class Overview:
    profile: Profile
    snapshots: int = 0
    last_backup: int | None = None            # Zeit des neuesten Snapshots
    size: int = 0                             # logische Größe des neuesten Snapshots
    growth_last: int | None = None            # Größe gegenüber dem vorherigen Snapshot
    growth_30d: int | None = None             # gegenüber dem Snapshot, der ca. 30 Tage zurückliegt
    logical_total: int = 0                    # Summe aller Snapshots (so viel wäre es ohne Deduplizierung)
    last_run: RunRecord | None = None
    failed_since_success: int = 0
    running: bool = False
    overdue: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        """running | error | warning | ok | none"""
        if self.running:
            return "running"
        if self.last_run is not None and not self.last_run.ok:
            return "error"
        if self.error or self.warnings or self.overdue:
            return "warning"
        return "ok" if self.last_backup or self.last_run is not None else "none"


def _growth(snaps: list[Snapshot], days: int) -> int | None:
    if len(snaps) < 2:
        return None
    target = snaps[0].backup_time - days * DAY
    older = [s for s in snaps[1:] if s.backup_time <= target + DAY]
    ref = min(older, key=lambda s: abs(s.backup_time - target)) if older else None
    return snaps[0].size - ref.size if ref else None


def build_overview(profile: Profile, snapshots: list[Snapshot] | None, paths_list: list[Paths],
                   now: float | None = None, error: str = "") -> Overview:
    """Reine Rechnung aus Snapshots und Verlauf (kein Netzwerk). `snapshots=None` = nicht abrufbar."""
    now = now or time.time()
    ov = Overview(profile, error=error)
    runs = list_runs(paths_list, profile.slug, limit=20)
    ov.last_run = runs[0] if runs else None
    for r in runs:
        if r.ok:
            break
        ov.failed_since_success += 1
    ov.running = any(is_locked(p.state / f"{profile.slug}.lock") for p in paths_list)
    if snapshots:
        snaps = profile_snapshots(profile, snapshots)
        ov.snapshots = len(snaps)
        ov.last_backup = snaps[0].backup_time
        ov.size = snaps[0].size
        ov.logical_total = sum(s.size for s in snaps)
        if len(snaps) > 1:
            ov.growth_last = snaps[0].size - snaps[1].size
        ov.growth_30d = _growth(snaps, 30)
    if profile.schedule and ov.last_backup is not None and now - ov.last_backup > STALE_AFTER:
        ov.overdue = True
        ov.warnings.append(tr("Letztes Backup vor {0} Tagen, obwohl ein Zeitplan eingerichtet ist", int((now - ov.last_backup) // DAY)))
    if ov.failed_since_success:
        ov.warnings.append(tr("{0} Läufe seit dem letzten Erfolg fehlgeschlagen", ov.failed_since_success))
    if profile.encrypt and not profile.keyfile and not profile.key_exported_at:
        ov.warnings.append(tr("Schlüssel noch nicht gesichert"))
    return ov


def datastore_usage(profile: Profile, token: str, timeout: float = 15) -> DatastoreUsage | None:
    """Belegung des Datastores per API; None, wenn der Token das nicht darf oder der Server nicht antwortet."""
    try:
        api = PbsApi(profile.server, profile.port, profile.fingerprint, timeout=timeout)
        api.use_token(profile.auth_id, token)
        st = api.datastore_status(profile.datastore)
    except ApiError:
        return None
    if not st or "total" not in st:
        return None
    gc = st.get("gc-status") or {}
    dedup = None
    try:
        if gc.get("deduplication-factor"):
            dedup = float(gc["deduplication-factor"])
        elif gc.get("disk-bytes") and gc.get("index-data-bytes"):
            dedup = float(gc["index-data-bytes"]) / float(gc["disk-bytes"])
    except (TypeError, ValueError, ZeroDivisionError):
        dedup = None
    return DatastoreUsage(int(st["total"]), int(st.get("used", 0)), int(st.get("avail", 0)), dedup)
