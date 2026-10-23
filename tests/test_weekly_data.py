"""Wöchentlicher Prüflauf im Modus data: Timer, Client-Aufruf, Sperre gegen gleichzeitige Läufe, Bedienung."""
import json
import threading
import time

import pytest
from textual.widgets import Button, Checkbox, Input, Static

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core import manager as manager_mod
from pbsgui.core import runlock
from pbsgui.core import scheduler as scheduler_mod
from pbsgui.core.client import PbsClient
from pbsgui.core.models import DATA, DEFAULT_DATA_SCHEDULE, LEGACY, METADATA
from pbsgui.core.runlock import LockTimeout, run_lock
from pbsgui.core.runner import run_profile
from pbsgui.core.scheduler import SystemdScheduler, render_service, render_timer
from pbsgui.core.secrets import FileStore
from pbsgui.tui.app import PbsTui
from test_cli import add_b, env, run  # noqa: F401
from test_scheduler_secrets import FakeRun
from test_tui import connect, text, wait_for  # noqa: F401


class SelectiveFail(FakeRun):
    """systemd-analyze lehnt nur den Ausdruck „quatsch“ ab; alles andere ist gültig."""
    def __call__(self, cmd, **kw):
        import subprocess
        if cmd[0] == "systemd-analyze" and cmd[-1] == "quatsch":
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "Failed to parse calendar")
        return super().__call__(cmd, **kw)


def enabled(fake, name):
    return ["systemctl", "--user", "enable", "--now", name] in fake.calls


# -- Modell ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("weekly,mode,schedule,expected", [
    (True, METADATA, "daily", True), (False, METADATA, "daily", False), (True, LEGACY, "daily", False),
    (True, METADATA, "", False)])
def test_weekly_data_active_needs_metadata_and_a_schedule(profile, weekly, mode, schedule, expected):
    profile.weekly_data, profile.change_detection, profile.schedule = weekly, mode, schedule
    assert profile.weekly_data_active is expected


def test_old_profiles_have_no_weekly_job(profile):
    data = profile.to_dict()
    data.pop("weekly_data"); data.pop("data_schedule")
    from pbsgui.core.models import Profile
    p = Profile.from_dict(data)
    assert p.weekly_data is False and p.data_schedule == DEFAULT_DATA_SCHEDULE


# -- Client ------------------------------------------------------------------------------------------------------------
def mode_of(args):
    return args[args.index("--change-detection-mode") + 1] if "--change-detection-mode" in args else None


def test_mode_override_data(profile):
    c = PbsClient(profile, "x")
    assert mode_of(c.backup_args()) is None                                    # klassisch
    c.mode_override = DATA
    assert mode_of(c.backup_args()) == "data"                                  # einmalig alles lesen, auch klassisches Profil
    profile.change_detection = METADATA
    c.mode_override = None
    assert mode_of(c.backup_args()) == "metadata"
    c.mode_override = DATA
    assert mode_of(c.backup_args()) == "data"


# -- systemd-Units ---------------------------------------------------------------------------------------------------------
def test_render_units_for_the_data_run(profile, tmp_path):
    store = FileStore(tmp_path)
    store.put(profile.slug, "token", "x")
    profile.schedule, profile.data_schedule = "00/8:00", "Sun *-*-* 03:00:00"
    svc = render_service(profile, store, "system", python="/opt/py", data=True)
    assert "ExecStart=/opt/py -m pbsgui.cli.run laptop --scope system --mode data" in svc
    assert "alles neu lesen" in svc and "LoadCredential=token:" in svc
    assert "--mode" not in render_service(profile, store, "system", python="/opt/py")
    assert "OnCalendar=Sun *-*-* 03:00:00" in render_timer(profile, data=True)
    assert "OnCalendar=00/8:00" in render_timer(profile)


def weekly_profile(profile):
    profile.schedule, profile.change_detection, profile.weekly_data = "00/8:00", METADATA, True
    return profile


def test_install_creates_both_timers_and_remove_removes_both(profile, paths, tmp_path):
    weekly_profile(profile)
    run_ = FakeRun()
    sched = SystemdScheduler(paths, FileStore(tmp_path / "c"), run_)
    sched.install(profile)
    for name in ("pbsgui-laptop", "pbsgui-laptop-data"):
        assert (paths.units / f"{name}.timer").exists() and (paths.units / f"{name}.service").exists()
        assert ["systemctl", "--user", "enable", "--now", f"{name}.timer"] in run_.calls
    assert "--mode data" in (paths.units / "pbsgui-laptop-data.service").read_text()
    assert "--mode" not in (paths.units / "pbsgui-laptop.service").read_text()
    assert sched.status(profile)["data"]["installed"] is True
    sched.remove(profile)
    assert not list(paths.units.glob("pbsgui-laptop*"))


@pytest.mark.parametrize("change", [{"weekly_data": False}, {"change_detection": LEGACY}])
def test_install_drops_the_weekly_timer_when_it_is_switched_off(profile, paths, tmp_path, change):
    weekly_profile(profile)
    sched = SystemdScheduler(paths, FileStore(tmp_path / "c"), FakeRun())
    sched.install(profile)
    assert (paths.units / "pbsgui-laptop-data.timer").exists()
    for k, v in change.items():
        setattr(profile, k, v)
    sched.install(profile)
    assert (paths.units / "pbsgui-laptop.timer").exists() and not (paths.units / "pbsgui-laptop-data.timer").exists()
    assert not (paths.units / "pbsgui-laptop-data.service").exists()


def test_invalid_weekly_expression_is_rejected_before_writing_units(profile, paths, tmp_path):
    weekly_profile(profile)
    profile.data_schedule = "quatsch"

    from pbsgui.core.scheduler import SchedulerError
    with pytest.raises(SchedulerError):
        SystemdScheduler(paths, FileStore(tmp_path / "c"), SelectiveFail()).install(profile)
    assert not list(paths.units.glob("pbsgui-laptop*")) if paths.units.exists() else True


# -- Sperre pro Profil ---------------------------------------------------------------------------------------------------
def test_run_lock_serialises_and_reports_waiting(tmp_path):
    lock, lines, order = tmp_path / "l.lock", [], []

    def second():
        with run_lock(lock, lines.append, wait=5, poll=0.05):
            order.append("zweiter")

    with run_lock(lock):
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.3)
        order.append("erster fertig")
    t.join(5)
    assert order == ["erster fertig", "zweiter"]
    assert any("warte" in l for l in lines) and any("Weiter" in l for l in lines)


def test_run_lock_times_out_with_clear_error(tmp_path):
    lock = tmp_path / "l.lock"
    with run_lock(lock):
        with pytest.raises(LockTimeout, match="nicht gestartet"):
            with run_lock(lock, wait=0.2, poll=0.05):
                pass


def test_run_lock_is_released_after_an_error(tmp_path):
    lock = tmp_path / "l.lock"
    with pytest.raises(ValueError):
        with run_lock(lock):
            raise ValueError("kaputt")
    with run_lock(lock, wait=0.2):
        pass


def test_run_profile_passes_data_mode_and_waits_for_a_running_backup(fake_env, profile, paths, monkeypatch):
    lines = []
    release = threading.Event()
    holder_in = threading.Event()

    def holder():
        with run_lock(paths.state / f"{profile.slug}.lock"):
            holder_in.set()
            release.wait(5)
    threading.Thread(target=holder, daemon=True).start()
    assert holder_in.wait(5)
    result = {}
    t = threading.Thread(target=lambda: result.update(
        code=run_profile(profile, paths, FileStore(paths.creds), on_line=lines.append, mode="data")))
    FileStore(paths.creds).put(profile.slug, "token", "t")
    t.start()
    time.sleep(0.4)
    assert "code" not in result                                                # wartet
    release.set()
    t.join(10)
    assert result["code"] == 0 and any("warte" in l for l in lines) and any("alles neu lesen" in l for l in lines)
    call = json.loads((fake_env / "calls.jsonl").read_text().splitlines()[-1])
    assert mode_of(call["argv"]) == "data"


def test_run_profile_gives_up_when_the_previous_run_never_ends(fake_env, profile, paths, monkeypatch):
    monkeypatch.setattr(runlock, "WAIT_SECONDS", 0.3)
    FileStore(paths.creds).put(profile.slug, "token", "t")
    lines = []
    with run_lock(paths.state / f"{profile.slug}.lock"):
        code = run_profile(profile, paths, FileStore(paths.creds), on_line=lines.append)
    assert code == 5 and any("nicht gestartet" in l for l in lines)
    from pbsgui.core.runner import read_status
    assert read_status(paths, profile)["ok"] is False


# -- Kommandozeile ---------------------------------------------------------------------------------------------------------
def test_cli_add_with_schedule_creates_both_timers_by_default(env, capsys, server, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    code, out, err = add_b(capsys, server, "--schedule", "00/8:00")
    assert code == 0, err
    assert "Wöchentlicher Prüflauf" in out and "Sun *-*-* 03:00:00" in out
    assert enabled(fake, "pbsgui-test.timer") and enabled(fake, "pbsgui-test-data.timer")
    p = json.loads(run(capsys, "list", "--json")[1])[0]
    assert p["weekly_data"] is True and p["data_schedule"] == "Sun *-*-* 03:00:00"


def test_cli_add_can_opt_out_or_use_legacy(env, capsys, server, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    add_b(capsys, server, "--schedule", "daily", "--no-weekly-data", name="A")
    add_b(capsys, server, "--schedule", "daily", "--change-detection", "legacy", name="B")
    assert enabled(fake, "pbsgui-a.timer") and enabled(fake, "pbsgui-b.timer")
    assert not enabled(fake, "pbsgui-a-data.timer") and not enabled(fake, "pbsgui-b-data.timer")
    profs = {p["name"]: p for p in json.loads(run(capsys, "list", "--json")[1])}
    assert profs["A"]["weekly_data"] is False and profs["B"]["weekly_data"] is False   # legacy: kein Prüflauf


def test_cli_add_custom_weekly_time_and_validation(env, capsys, server, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "_default_run", FakeRun())
    code, out, _ = add_b(capsys, server, "--schedule", "daily", "--data-schedule", "Sat *-*-* 05:00:00")
    assert code == 0 and "Sat *-*-* 05:00:00" in out
    monkeypatch.setattr(scheduler_mod, "_default_run", SelectiveFail())
    code, _, err = add_b(capsys, server, "--schedule", "daily", "--data-schedule", "quatsch", name="X")
    assert code == 1 and "Wöchentlicher Lauf" in err
    assert "X" not in run(capsys, "list")[1]


def test_cli_weekly_without_schedule_is_noted_not_installed(env, capsys, server, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    code, out, _ = add_b(capsys, server)
    assert code == 0 and "sobald es einen Zeitplan gibt" in out
    assert not enabled(fake, "pbsgui-test-data.timer")
    code, out, _ = run(capsys, "schedule", "Test", "--calendar", "daily")           # Zeitplan später: Prüflauf kommt dazu
    assert ["systemctl", "--user", "enable", "--now", "pbsgui-test-data.timer"] in fake.calls


def test_cli_detection_manages_weekly_run(env, capsys, server, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    add_b(capsys, server, "--schedule", "daily")
    assert "an, Sun *-*-* 03:00:00" in run(capsys, "detection", "Test")[1]
    code, out, _ = run(capsys, "detection", "Test", "--weekly-data", "off")
    assert code == 0 and "aus" in out
    assert not (env / "xdg-c/systemd/user/pbsgui-test-data.timer").exists()          # Timer wurde entfernt
    code, out, _ = run(capsys, "detection", "Test", "--weekly-data", "on", "--data-schedule", "Fri *-*-* 02:00:00")
    assert "an, Fri *-*-* 02:00:00" in out
    assert "OnCalendar=Fri *-*-* 02:00:00" in (env / "xdg-c/systemd/user/pbsgui-test-data.timer").read_text()
    run(capsys, "detection", "Test", "legacy")                                          # klassisch: kein Prüflauf mehr
    assert not (env / "xdg-c/systemd/user/pbsgui-test-data.timer").exists()
    assert "Wöchentlich" not in run(capsys, "detection", "Test")[1]


def test_cli_status_shows_weekly_run(env, capsys, server, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "_default_run", FakeRun())
    add_b(capsys, server, "--schedule", "daily")
    out = run(capsys, "status", "Test")[1]
    assert "Wöchentlich alles neu lesen (data): aktiv" in out


def test_cli_backup_data_flag_reads_everything_once(env, capsys, server):
    add_b(capsys, server)
    assert run(capsys, "backup", "Test")[0] == 0
    assert run(capsys, "backup", "Test", "--data")[0] == 0
    calls = [json.loads(l) for l in (env / "fake/calls.jsonl").read_text().splitlines() if '"backup"' in l]
    assert [mode_of(c["argv"]) for c in calls] == ["metadata", "data"]
    assert run(capsys, "backup", "Test")[0] == 0                                        # danach wieder normal
    calls = [json.loads(l) for l in (env / "fake/calls.jsonl").read_text().splitlines() if '"backup"' in l]
    assert mode_of(calls[-1]["argv"]) == "metadata"


# -- TUI --------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def mgr(fake_env, tmp_path, monkeypatch, profile):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-s"))
    monkeypatch.setattr(manager_mod, "best_store", lambda scope, d: FileStore(d))
    m = manager_mod.ProfileManager()
    profile.sources = ["/etc"]
    m.save(profile, token="geheim")
    return m


async def test_tui_dialog_weekly_is_on_by_default_and_follows_only_changed(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 100)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        weekly = s.query_one("#weekly-data", Checkbox)
        assert weekly.value is True and weekly.disabled is False
        s.query_one("#only-changed", Checkbox).value = False
        await pilot.pause()
        assert weekly.disabled is True
        s.query_one("#only-changed", Checkbox).value = True
        await pilot.pause()
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        assert next(p for p in mgr.profiles() if p.name == "NeuerServer").weekly_data is True


async def test_tui_dialog_without_only_changed_never_creates_weekly_job(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 100)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        s.query_one("#only-changed", Checkbox).value = False
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        prof = next(p for p in mgr.profiles() if p.name == "NeuerServer")
        assert prof.change_detection == LEGACY and prof.weekly_data is False


async def test_tui_key_c_opens_detection_dialog_and_saves(mgr, monkeypatch):
    fake = SelectiveFail()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    p = mgr.profiles()[0]
    p.schedule = "daily"
    mgr.save(p, schedule="install")                                                  # Hauptzeitplan ist eingerichtet
    async with PbsTui(mgr).run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        assert "alles jedes Mal" in text(pilot.app.query_one("#overview", Static))
        await pilot.press("c")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ == "DetectScreen"
        pilot.app.screen.query_one("#cancel", Button).press()                          # Abbrechen ändert nichts
        await pilot.pause()
        assert mgr.profiles()[0].change_detection == LEGACY
        await pilot.press("c")
        await pilot.pause()
        scr = pilot.app.screen
        assert scr.query_one("#det-weekly", Checkbox).disabled is True                  # erst „nur Geändertes“ einschalten
        scr.query_one("#det-changed", Checkbox).value = True
        await pilot.pause()
        assert scr.query_one("#det-weekly", Checkbox).disabled is False
        scr.query_one("#det-weekly", Checkbox).value = True
        scr.query_one("#det-schedule", Input).value = "quatsch"
        scr.query_one("#ok", Button).press()
        await pilot.pause()
        assert pilot.app.screen is scr                                                  # ungültig: bleibt offen
        scr.query_one("#det-schedule", Input).value = "Fri *-*-* 02:00:00"
        scr.query_one("#ok", Button).press()
        await pilot.pause()
        saved = mgr.profiles()[0]
        assert (saved.change_detection, saved.weekly_data, saved.data_schedule) == (METADATA, True, "Fri *-*-* 02:00:00")
        ov = text(pilot.app.query_one("#overview", Static))
        assert "nur Geändertes" in ov and "wöchentlich alles neu lesen: Fri *-*-* 02:00:00" in ov
        assert (mgr.paths.units / "pbsgui-laptop-data.timer").exists()                   # Timer kam mit dem Speichern
