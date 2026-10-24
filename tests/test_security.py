"""Sicherheits-Regressionstests: Eingaben dürfen nie zu Anweisungen, fremden Dateien oder Root-Code werden."""
import os
import stat
from pathlib import Path

import pytest

from pbsgui.cli.admin import apply
from pbsgui.core import keys, privilege
from pbsgui.core.models import Profile
from pbsgui.core.paths import Paths
from pbsgui.core.privilege import PrivilegeError, require_trusted_code
from pbsgui.core.scheduler import SchedulerError, SystemdScheduler, render_service, render_timer, validate_calendar
from pbsgui.core.secrets import FileStore


def test_profile_name_cannot_inject_unit_directives(profile, paths):
    profile.name = "Laptop\nExecStartPre=/bin/evil\n[Service]\nExecStart=/bin/evil"
    unit = render_service(profile, FileStore(paths.creds), "user").splitlines()
    assert not any(l.startswith("ExecStartPre=") for l in unit)
    assert sum(l.startswith("ExecStart=") for l in unit) == 1 and unit.count("[Service]") == 1
    profile.schedule = "daily"
    timer = render_timer(profile).splitlines()
    assert timer.count("[Unit]") == 1 and not any(l.startswith("ExecStart") for l in timer)


def test_percent_specifiers_are_escaped(profile, paths):
    profile.name = "Backup %H %n"
    assert "Description=pbsgui Backup Backup %%H %%n" in render_service(profile, FileStore(paths.creds), "user")


@pytest.mark.parametrize("expr", ["daily\nExecStart=/bin/evil", "daily\x00", "%H"])
def test_calendar_with_control_characters_is_rejected(expr, profile):
    profile.schedule = expr
    with pytest.raises(SchedulerError):
        validate_calendar(expr, run=lambda *a, **k: None)
    with pytest.raises(SchedulerError):
        render_timer(profile)


def test_admin_helper_rejects_unknown_secret_kinds(tmp_path, profile):
    paths = Paths("system", tmp_path / "etc", tmp_path / "units", tmp_path / "etc/creds", tmp_path / "var")
    store = FileStore(paths.creds)
    req = {"profile": profile.to_dict(), "secrets": {"../../evil": "x"}, "schedule": "keep"}
    with pytest.raises(ValueError):
        apply(req, paths=paths, store=store)
    assert not list(tmp_path.rglob("evil*"))
    with pytest.raises(ValueError):
        apply({"profile": profile.to_dict(), "schedule": "rm -rf /"}, paths=paths, store=store)
    apply({"profile": profile.to_dict(), "secrets": {"token": "t"}, "schedule": "keep"}, paths=paths, store=store)
    assert store.path(profile.slug, "token").read_text() == "t"


def test_user_writable_code_is_not_run_as_root(tmp_path, monkeypatch):
    monkeypatch.delenv(privilege.ENV_ALLOW, raising=False)
    code = tmp_path / "src"
    (code / "pbsgui").mkdir(parents=True)
    monkeypatch.setattr(privilege, "package_root", lambda: code)
    with pytest.raises(PrivilegeError) as e:
        require_trusted_code()
    assert "Debian-Paket" in str(e.value) and privilege.ENV_ALLOW in str(e.value)
    monkeypatch.setenv(privilege.ENV_ALLOW, "1")   # bewusste Ausnahme für Entwickler
    require_trusted_code()


@pytest.mark.skipif(os.geteuid() == 0, reason="als root ist alles im Test-Verzeichnis root-eigen")
def test_system_schedule_refuses_user_owned_code(profile, tmp_path, monkeypatch):
    monkeypatch.delenv(privilege.ENV_ALLOW, raising=False)
    paths = Paths("system", tmp_path / "etc", tmp_path / "units", tmp_path / "creds", tmp_path / "var")
    sched = SystemdScheduler(paths, FileStore(paths.creds), run=lambda *a, **k: None)
    with pytest.raises(SchedulerError, match="root"):
        sched.install(profile)
    assert not (tmp_path / "units").exists() or not list((tmp_path / "units").glob("*.service"))


def test_root_trusts_system_python_and_package_paths():
    # /usr/bin/python3 und /usr/lib gehören root und sind nur für root beschreibbar
    assert privilege._unsafe_part(Path("/usr/bin")) is None


def test_exported_key_gets_0600_even_if_file_existed(tmp_path):
    dest = tmp_path / "key.json"
    dest.write_text("alt"); os.chmod(dest, 0o644)
    keys.export_key('{"k": 1}', str(dest))
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600 and dest.read_text() == '{"k": 1}'


def test_key_tempdir_is_in_ram_and_private(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with keys.materialized_key("geheim") as p:
        assert Path(p).parent.parent in (Path("/run"), Path("/dev/shm"), Path(os.environ.get("TMPDIR", "/tmp")))
        assert stat.S_IMODE(Path(p).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(p).parent.stat().st_mode) == 0o700
    assert not Path(p).exists()


def test_api_path_parts_are_quoted():
    from pbsgui.core.pbs_api import _q
    assert _q("a/b?c") == "a%2Fb%3Fc" and "/" not in _q("../x")
