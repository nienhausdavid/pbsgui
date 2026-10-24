"""Verlauf der Backup-Läufe: jeder Lauf (Zeitplan oder manuell) wird mit Ausgabe gespeichert.

Pro Lauf eine kleine JSON-Datei (Metadaten) und eine Textdatei mit der Ausgabe unter `<state>/history/<profil>/`, damit
Listen schnell bleiben. Die Ausgabe wird auf die letzten Zeilen begrenzt und enthält keine Fortschrittszeilen. Ältere Läufe werden gelöscht (je Profil höchstens MAX_RUNS).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .models import Profile
from .paths import Paths
from .progress import decode

MAX_RUNS = 200       # je Profil
MAX_LOG_LINES = 3000


@dataclass
class RunRecord:
    id: str
    profile: str                 # Profil-Slug
    start: int
    end: int
    ok: bool
    code: int
    message: str = ""
    mode: str = ""               # "" = normal, "data" = alles neu gelesen
    scope: str = "user"
    log: list[str] = field(default_factory=list)

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)

    def matches(self, text: str) -> bool:
        t = text.lower()
        return t in self.message.lower() or any(t in line.lower() for line in self.log)

    def to_dict(self) -> dict:
        return {"id": self.id, "profile": self.profile, "start": self.start, "end": self.end, "ok": self.ok,
                "code": self.code, "message": self.message, "mode": self.mode, "scope": self.scope, "log": self.log}

    def meta(self) -> dict:
        return {k: v for k, v in self.to_dict().items() if k != "log"}

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(id=str(d["id"]), profile=str(d["profile"]), start=int(d["start"]), end=int(d["end"]), ok=bool(d["ok"]),
                   code=int(d.get("code", 0)), message=str(d.get("message", "")), mode=str(d.get("mode", "")),
                   scope=str(d.get("scope", "user")), log=[str(x) for x in d.get("log", [])])


class LogCapture:
    """Reicht Zeilen an `inner` weiter und merkt sich die letzten (ohne Fortschrittszeilen)."""

    def __init__(self, inner: Callable[[str], None]):
        self.inner = inner
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        if decode(line) is None:
            self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES * 2:
                del self.lines[:-MAX_LOG_LINES]
        self.inner(line)


def history_dir(paths: Paths, slug: str) -> Path:
    return paths.state / "history" / slug


def record_run(paths: Paths, profile: Profile, start: float, end: float, code: int, message: str,
               lines: list[str], mode: str | None = None) -> RunRecord | None:
    """Speichert einen Lauf. Schlägt das Schreiben fehl, geht nur der Verlaufseintrag verloren (nie das Backup)."""
    rec = RunRecord(id=f"{int(start)}-{int(end)}", profile=profile.slug, start=int(start), end=int(end), ok=code == 0,
                    code=code, message=message, mode=mode or "", scope=paths.scope, log=lines[-MAX_LOG_LINES:])
    d = history_dir(paths, profile.slug)
    try:
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        while (d / f"{rec.id}-{n}.json").exists():
            n += 1
        rec.id = f"{rec.id}-{n}"
        (d / f"{rec.id}.log").write_text("\n".join(rec.log))
        tmp = d / f".{rec.id}.tmp"
        tmp.write_text(json.dumps(rec.meta()))
        tmp.replace(d / f"{rec.id}.json")
        for old in sorted(d.glob("*.json"))[:-MAX_RUNS]:
            old.unlink(missing_ok=True)
            old.with_suffix(".log").unlink(missing_ok=True)
    except OSError:
        return None
    return rec


def _read(path: Path) -> RunRecord | None:
    try:
        return RunRecord.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _log(path: Path) -> list[str]:
    try:
        return path.with_suffix(".log").read_text().split("\n")
    except OSError:
        return []


def all_scope_paths() -> list[Paths]:
    return [Paths.for_scope("user"), Paths.for_scope("system")]


def list_runs(paths_list: list[Paths], slug: str | None = None, *, failed_only: bool = False, text: str = "",
              since: float | None = None, limit: int = 100, with_log: bool = False) -> list[RunRecord]:
    """Läufe aus allen angegebenen Scopes, neueste zuerst. `text` sucht in Meldung und Ausgabe."""
    out: list[RunRecord] = []
    for paths in paths_list:
        base = paths.state / "history"
        try:
            dirs = [base / slug] if slug else [p for p in base.iterdir() if p.is_dir()]
        except OSError:
            continue
        for d in dirs:
            try:
                files = sorted(d.glob("*.json"), reverse=True)
            except OSError:
                continue
            matched = 0
            for f in files:                      # Dateinamen beginnen mit der Startzeit: neueste zuerst
                rec = _read(f)
                if rec is None or (failed_only and rec.ok) or (since is not None and rec.start < since):
                    continue
                if text or with_log or not rec.ok:
                    rec.log = _log(f)
                if text and not rec.matches(text):
                    continue
                if not with_log:
                    rec.log = rec.log[-20:] if not rec.ok else []     # kurzer Auszug genügt für die Liste
                out.append(rec)
                matched += 1
                if matched >= limit:
                    break
    out.sort(key=lambda r: r.start, reverse=True)
    return out[:limit]


def find_run(paths_list: list[Paths], run_id: str, slug: str | None = None) -> RunRecord | None:
    for r in list_runs(paths_list, slug, limit=100000, with_log=True):
        if r.id == run_id or r.id.startswith(run_id):
            return r
    return None


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def save_restore_test(paths: Paths, profile: Profile, result: dict) -> None:
    """Letztes Ergebnis des Restore-Tests je Profil (für das Dashboard)."""
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        (paths.state / f"{profile.slug}.restoretest.json").write_text(json.dumps(result))
    except OSError:
        pass


def load_restore_test(paths_list: list[Paths], profile: Profile) -> dict | None:
    found = []
    for p in paths_list:
        try:
            found.append(json.loads((p.state / f"{profile.slug}.restoretest.json").read_text()))
        except (OSError, ValueError):
            continue
    return max(found, key=lambda d: d.get("time", 0)) if found else None
