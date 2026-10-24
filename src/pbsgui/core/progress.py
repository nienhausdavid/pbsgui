"""Fortschritt eines laufenden Backups.

Der Client gibt beim Backup keinen Fortschritt aus (auch nicht mit Terminal; gemessen mit Client 4.2.8). Wir lesen
deshalb `rchar` aus /proc/<pid>/io: die Zahl der Bytes, die der Client aus Dateien liest. Verglichen mit der Größe des
letzten Backups (oder, beim ersten Mal, dem belegten Platz) ergibt das Prozent, Geschwindigkeit und Restzeit.
Beim echten Backup eines 25-GB-Systems lag die Abweichung bei etwa einem Prozentpunkt.

Die Messung muss im selben Prozess-Besitz laufen wie der Client (root-Backup: im root-Prozess). Das Ergebnis wird deshalb
als Textzeile weitergereicht (PREFIX + JSON), damit es auch durch eine pkexec-Pipe zur Oberfläche kommt.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable
from ..i18n import tr

PREFIX = "@@pbsgui-progress "


@dataclass
class Progress:
    done: int                    # Bytes, die der Client bisher gelesen hat
    total: int | None            # erwartete Gesamtmenge (None = unbekannt)
    rate: float                  # Bytes pro Sekunde (geglättet)
    elapsed: float               # Sekunden seit Start
    basis: str = ""              # woher "total" stammt: "letztes Backup" | "Schätzung" | ""

    @property
    def percent(self) -> float | None:
        if not self.total:
            return None
        return min(99.0, 100.0 * self.done / self.total)   # 100 % erst nach dem Ende

    @property
    def eta(self) -> float | None:
        if not self.total or self.rate <= 0:
            return None
        return max(0.0, (self.total - self.done) / self.rate)


def encode(p: Progress) -> str:
    return PREFIX + json.dumps(asdict(p), separators=(",", ":"))


def decode(line: str) -> Progress | None:
    if not line.startswith(PREFIX):
        return None
    try:
        return Progress(**json.loads(line[len(PREFIX):]))
    except (ValueError, TypeError):
        return None


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1000
    return str(n)


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} s"
    if sec < 3600:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {sec % 3600 // 60} min"


def describe(p: Progress) -> str:
    """Einzeilige Anzeige für GUI, TUI und Kommandozeile."""
    rate = f"{fmt_bytes(p.rate)}/s"
    if p.percent is None:
        why = tr(p.basis) if p.basis else tr("Gesamtmenge unbekannt (erstes Backup)")
        return tr("{0} gelesen  ·  {1}  ·  seit {2}  ·  {3}", fmt_bytes(p.done), rate, fmt_duration(p.elapsed), why)
    eta = tr("  ·  noch ca. {0}", fmt_duration(p.eta)) if p.eta is not None and p.rate > 0 else ""
    return (tr("{0:.0f} %  ·  {1} von {2}  ·  {3}{4}", p.percent, fmt_bytes(p.done), fmt_bytes(p.total or 0), rate, eta)
            + (f"  ({tr(p.basis)})" if p.basis else ""))


def read_rchar(pid: int, io_root: str = "/proc") -> int | None:
    try:
        with open(f"{io_root}/{pid}/io") as f:
            for line in f:
                if line.startswith("rchar:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


class Sampler(threading.Thread):
    """Fragt regelmäßig den Lesezähler des Clients ab und meldet den Fortschritt."""

    def __init__(self, pid: int, total: int | None, basis: str, callback: Callable[[Progress], None],
                 interval: float = 1.0, reader: Callable[[int], int | None] = read_rchar, window: float = 10.0):
        super().__init__(daemon=True)
        self.pid, self.total, self.basis, self.cb = pid, total, basis, callback
        self.interval, self.reader, self.window = interval, reader, window
        self._stop_evt = threading.Event()
        self._samples: deque[tuple[float, int]] = deque()
        self._base: int | None = None

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        t0 = time.monotonic()
        # Ausgangswert SOFORT lesen (Bibliotheken/Konfiguration beim Start nicht mitzählen). Würde man erst nach dem
        # ersten Intervall messen, fehlten die Daten der ersten Sekunde (im Test: 218 von 480 MB, also 45 %).
        self._base = self.reader(self.pid)
        while not self._stop_evt.wait(self.interval):
            cur = self.reader(self.pid)
            if cur is None:
                continue
            if self._base is None:
                self._base = cur
            now = time.monotonic()
            done = max(0, cur - self._base)
            self._samples.append((now, done))
            while len(self._samples) > 2 and now - self._samples[0][0] > self.window:
                self._samples.popleft()
            (ta, da), (tb, db) = self._samples[0], self._samples[-1]
            rate = (db - da) / (tb - ta) if tb > ta else 0.0
            self.cb(Progress(done, self.total, rate, now - t0, self.basis))


def estimate_total(profile, snapshots, mounts: list[str] | None = None, mode: str | None = None) -> tuple[int | None, str]:
    """Erwartete Gesamtmenge: Größe des letzten Backups dieses Profils, sonst (Vollbackup) belegter Platz."""
    from .models import METADATA
    from .modes import FULL
    mine = [s for s in snapshots if s.backup_id == profile.backup_id and s.size]
    if mine:
        last = max(mine, key=lambda s: s.backup_time)
        if (mode or profile.change_detection) == METADATA and last.split:
            # Der Client liest nur Geändertes: die Größe des letzten Backups sagt nichts über die Lesemenge
            return None, "nur geänderte Dateien (Menge vorher unbekannt)"
        return last.size, "letztes Backup"
    if profile.backup_mode == FULL:
        used = 0
        for path in ["/", *(mounts or [])]:
            try:
                st = os.statvfs(path)
                used += (st.f_blocks - st.f_bfree) * st.f_frsize
            except OSError:
                pass
        if used:
            return used, "Schätzung aus belegtem Platz"
    return None, ""
