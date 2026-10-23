"""Erzeugt und verwaltet systemd-Units (service + timer) pro Profil."""
from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .models import Profile
from .paths import Paths
from .secrets import SecretStore


class SchedulerError(RuntimeError):
    pass


def unit_base(profile: Profile, data: bool = False) -> str:
    """Name der systemd-Units; `data=True` ist der wöchentliche Lauf im Modus data (alles neu lesen)."""
    return f"pbsgui-{profile.slug}" + ("-data" if data else "")


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def render_service(profile: Profile, store: SecretStore, scope: str, python: str | None = None,
                   data: bool = False) -> str:
    lines = ["[Unit]", f"Description=pbsgui Backup {profile.name}" + (" (alles neu lesen)" if data else ""),
             "After=network-online.target", "Wants=network-online.target"]
    if profile.require_ac:
        lines.append("ConditionACPower=true")
    lines += ["", "[Service]", "Type=oneshot"]
    for kind in ("token", "keypass", "key", "dbdump"):
        d = store.unit_directive(profile.slug, kind)
        if d:
            lines.append(d)
    py = python or sys.executable
    lines += [f"ExecStart={_q(py)} -m pbsgui.cli.run {profile.slug} --scope {scope}" + (" --mode data" if data else ""),
              "NoNewPrivileges=yes", ""]
    return "\n".join(lines)


def render_timer(profile: Profile, data: bool = False) -> str:
    return "\n".join(["[Unit]", f"Description=pbsgui Zeitplan {profile.name}" + (" (alles neu lesen)" if data else ""), "",
                      "[Timer]", f"OnCalendar={profile.data_schedule if data else profile.schedule}",
                      "Persistent=true", "",
                      "[Install]", "WantedBy=timers.target", ""])


# Auswahl beim Anlegen eines Jobs: (Anzeigetext, OnCalendar-Ausdruck). "" = kein Zeitplan.
SCHEDULE_PRESETS: list[tuple[str, str]] = [
    ("Kein Zeitplan (nur manuell starten)", ""),
    ("Stündlich", "hourly"),
    ("Alle 4 Stunden (00, 04, 08 … Uhr)", "00/4:00"),
    ("Alle 6 Stunden (00, 06, 12, 18 Uhr)", "00/6:00"),
    ("Alle 8 Stunden (00, 08, 16 Uhr)", "00/8:00"),
    ("Alle 12 Stunden (00, 12 Uhr)", "00/12:00"),
    ("Täglich um 02:00 Uhr", "*-*-* 02:00:00"),
    ("Wöchentlich, sonntags 03:00 Uhr", "Sun *-*-* 03:00:00"),
]
CUSTOM_SCHEDULE = "__custom__"

Runner = Callable[..., subprocess.CompletedProcess]


def _default_run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(*args, **kwargs)


def validate_calendar(expr: str, run: Runner | None = None) -> str:
    """Prüft einen OnCalendar-Ausdruck mit systemd-analyze; liefert die Normalform."""
    if not expr.strip():
        raise SchedulerError("Zeitplan ist leer")
    r = (run or _default_run)(["systemd-analyze", "calendar", expr], capture_output=True, text=True)
    if r.returncode != 0:
        raise SchedulerError((r.stderr or r.stdout).strip() or "Ungültiger Zeitplan")
    return r.stdout


def next_elapse(expr: str, run: Runner | None = None) -> str | None:
    """Nächster Ausführungszeitpunkt eines (gültigen) OnCalendar-Ausdrucks, sonst None."""
    try:
        out = validate_calendar(expr, run)
    except (SchedulerError, OSError):
        return None
    for line in out.splitlines():
        if "Next elapse:" in line:
            return line.split("Next elapse:", 1)[1].strip()
    return None


class SystemdScheduler:
    def __init__(self, paths: Paths, store: SecretStore, run: Runner | None = None):
        self.paths, self.store, self._run = paths, store, run or _default_run

    def _ctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = self._run([*self.paths.systemctl, *args], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SchedulerError((r.stderr or r.stdout).strip() or f"systemctl {args[0]} fehlgeschlagen")
        return r

    def _write_units(self, profile: Profile, data: bool) -> str:
        base = unit_base(profile, data)
        (self.paths.units / f"{base}.service").write_text(
            render_service(profile, self.store, self.paths.scope, data=data))
        (self.paths.units / f"{base}.timer").write_text(render_timer(profile, data))
        return base

    def install(self, profile: Profile) -> None:
        """Richtet den Zeitplan ein. Mit weekly_data (und METADATA) kommt ein zweiter Timer für den Modus data dazu."""
        validate_calendar(profile.schedule, self._run)
        data = profile.weekly_data_active
        if data:
            validate_calendar(profile.data_schedule, self._run)
        self.paths.units.mkdir(parents=True, exist_ok=True)
        base = self._write_units(profile, False)
        data_base = self._write_units(profile, True) if data else None
        if not data:
            self._remove_units(profile, True)
        self._ctl("daemon-reload")
        self._ctl("enable", "--now", f"{base}.timer")
        if data_base:
            self._ctl("enable", "--now", f"{data_base}.timer")

    def _remove_units(self, profile: Profile, data: bool) -> None:
        base = unit_base(profile, data)
        self._ctl("disable", "--now", f"{base}.timer", check=False)
        for ext in ("timer", "service"):
            (self.paths.units / f"{base}.{ext}").unlink(missing_ok=True)

    def remove(self, profile: Profile) -> None:
        for data in (False, True):
            self._remove_units(profile, data)
        self._ctl("daemon-reload", check=False)

    def _timer_status(self, base: str) -> dict:
        r = self._ctl("show", f"{base}.timer", "-p", "ActiveState,NextElapseUSecRealtime,LastTriggerUSec", check=False)
        info = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        return {"installed": (self.paths.units / f"{base}.timer").exists(),
                "active": info.get("ActiveState") == "active",
                "next": info.get("NextElapseUSecRealtime", ""), "last": info.get("LastTriggerUSec", "")}

    def status(self, profile: Profile) -> dict:
        st = self._timer_status(unit_base(profile))
        st["data"] = self._timer_status(unit_base(profile, True))   # wöchentlicher Lauf im Modus data
        return st


def linger_enabled() -> bool:
    r = subprocess.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
                       capture_output=True, text=True)
    return "Linger=yes" in r.stdout


def enable_linger() -> None:
    r = subprocess.run(["loginctl", "enable-linger", getpass.getuser()], capture_output=True, text=True)
    if r.returncode != 0:
        raise SchedulerError(r.stderr.strip() or "enable-linger fehlgeschlagen")
