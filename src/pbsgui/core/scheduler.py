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


def unit_base(profile: Profile) -> str:
    return f"pbsgui-{profile.slug}"


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def render_service(profile: Profile, store: SecretStore, scope: str, python: str | None = None) -> str:
    lines = ["[Unit]", f"Description=pbsgui Backup {profile.name}",
             "After=network-online.target", "Wants=network-online.target"]
    if profile.require_ac:
        lines.append("ConditionACPower=true")
    lines += ["", "[Service]", "Type=oneshot"]
    for kind in ("token", "keypass"):
        d = store.unit_directive(profile.slug, kind)
        if d:
            lines.append(d)
    py = python or sys.executable
    lines += [f"ExecStart={_q(py)} -m pbsgui.cli.run {profile.slug} --scope {scope}",
              "NoNewPrivileges=yes", ""]
    return "\n".join(lines)


def render_timer(profile: Profile) -> str:
    return "\n".join(["[Unit]", f"Description=pbsgui Zeitplan {profile.name}", "",
                      "[Timer]", f"OnCalendar={profile.schedule}", "Persistent=true", "",
                      "[Install]", "WantedBy=timers.target", ""])


Runner = Callable[..., subprocess.CompletedProcess]


def validate_calendar(expr: str, run: Runner = subprocess.run) -> str:
    """Prüft einen OnCalendar-Ausdruck mit systemd-analyze; liefert die Normalform."""
    if not expr.strip():
        raise SchedulerError("Zeitplan ist leer")
    r = run(["systemd-analyze", "calendar", expr], capture_output=True, text=True)
    if r.returncode != 0:
        raise SchedulerError((r.stderr or r.stdout).strip() or "Ungültiger Zeitplan")
    return r.stdout


class SystemdScheduler:
    def __init__(self, paths: Paths, store: SecretStore, run: Runner = subprocess.run):
        self.paths, self.store, self._run = paths, store, run

    def _ctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = self._run([*self.paths.systemctl, *args], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SchedulerError((r.stderr or r.stdout).strip() or f"systemctl {args[0]} fehlgeschlagen")
        return r

    def install(self, profile: Profile) -> None:
        validate_calendar(profile.schedule, self._run)
        self.paths.units.mkdir(parents=True, exist_ok=True)
        base = unit_base(profile)
        (self.paths.units / f"{base}.service").write_text(render_service(profile, self.store, self.paths.scope))
        (self.paths.units / f"{base}.timer").write_text(render_timer(profile))
        self._ctl("daemon-reload")
        self._ctl("enable", "--now", f"{base}.timer")

    def remove(self, profile: Profile) -> None:
        base = unit_base(profile)
        self._ctl("disable", "--now", f"{base}.timer", check=False)
        for ext in ("timer", "service"):
            (self.paths.units / f"{base}.{ext}").unlink(missing_ok=True)
        self._ctl("daemon-reload", check=False)

    def status(self, profile: Profile) -> dict:
        base = unit_base(profile)
        r = self._ctl("show", f"{base}.timer", "-p", "ActiveState,NextElapseUSecRealtime,LastTriggerUSec",
                      check=False)
        info = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        return {"installed": (self.paths.units / f"{base}.timer").exists(),
                "active": info.get("ActiveState") == "active",
                "next": info.get("NextElapseUSecRealtime", ""), "last": info.get("LastTriggerUSec", "")}


def linger_enabled() -> bool:
    r = subprocess.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
                       capture_output=True, text=True)
    return "Linger=yes" in r.stdout


def enable_linger() -> None:
    r = subprocess.run(["loginctl", "enable-linger", getpass.getuser()], capture_output=True, text=True)
    if r.returncode != 0:
        raise SchedulerError(r.stderr.strip() or "enable-linger fehlgeschlagen")
