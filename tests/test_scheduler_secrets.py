import os
import shutil
import stat
import subprocess

import pytest

from pbsgui.core.scheduler import SchedulerError, SystemdScheduler, render_service, render_timer, validate_calendar
from pbsgui.core.secrets import FileStore, SystemdCredsStore, best_store, systemd_version


class FakeRun:
    def __init__(self, fail_calendar=False):
        self.calls, self.fail_calendar = [], fail_calendar

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "systemd-analyze" and self.fail_calendar:
            return subprocess.CompletedProcess(cmd, 1, "", "Failed to parse calendar")
        return subprocess.CompletedProcess(cmd, 0, "ActiveState=active\n", "")


def test_file_store_perms(tmp_path):
    s = FileStore(tmp_path / "c")
    s.put("a", "token", "geheim")
    p = s.path("a", "token")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and s.get("a", "token") == "geheim"
    assert s.unit_directive("a", "token").startswith("LoadCredential=token:")
    assert s.unit_directive("a", "keypass") is None
    s.delete("a")
    assert s.get("a", "token") is None


@pytest.mark.skipif(not shutil.which("systemd-creds") or systemd_version() < 256, reason="systemd-creds --user fehlt")
def test_systemd_creds_user_roundtrip(tmp_path):
    s = SystemdCredsStore(tmp_path / "c", user=True)
    s.put("a", "token", "geheim")
    raw = s.path("a", "token").read_bytes()
    assert b"geheim" not in raw  # wirklich verschlüsselt
    assert s.get("a", "token") == "geheim"
    # User-Manager kann LoadCredentialEncrypted nicht (243/CREDENTIALS): Runner entschlüsselt selbst
    assert s.unit_directive("a", "token") is None
    assert SystemdCredsStore(tmp_path / "c", user=False).unit_directive("a", "token").startswith(
        "LoadCredentialEncrypted=token:")


def test_render_units(profile, tmp_path):
    store = FileStore(tmp_path)
    store.put(profile.slug, "token", "x")
    profile.schedule, profile.require_ac = "daily", True
    svc = render_service(profile, store, "user", python="/opt/py")
    assert "ExecStart=/opt/py -m pbsgui.cli.run laptop --scope user" in svc
    assert "ConditionACPower=true" in svc and "LoadCredential=token:" in svc
    assert "NoNewPrivileges=yes" in svc
    assert "OnCalendar=daily" in render_timer(profile)
    assert "LoadCredential=token:" in svc and "=x" not in svc  # nur der Pfad, nie der Wert


def test_install_and_remove(profile, paths, tmp_path):
    profile.schedule = "*-*-* 02:00:00"
    run = FakeRun()
    sched = SystemdScheduler(paths, FileStore(tmp_path / "c"), run)
    sched.install(profile)
    assert (paths.units / "pbsgui-laptop.timer").exists() and (paths.units / "pbsgui-laptop.service").exists()
    assert ["systemctl", "--user", "enable", "--now", "pbsgui-laptop.timer"] in run.calls
    assert sched.status(profile)["active"] is True
    sched.remove(profile)
    assert not (paths.units / "pbsgui-laptop.timer").exists()


def test_invalid_calendar_rejected(profile, paths, tmp_path):
    profile.schedule = "quatsch"
    with pytest.raises(SchedulerError):
        SystemdScheduler(paths, FileStore(tmp_path), FakeRun(fail_calendar=True)).install(profile)
    with pytest.raises(SchedulerError):
        validate_calendar("   ")


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze fehlt")
def test_real_calendar_validation():
    validate_calendar("daily")
    with pytest.raises(SchedulerError):
        validate_calendar("quatsch")


def test_best_store_returns_store(tmp_path):
    assert best_store("user", tmp_path).label
