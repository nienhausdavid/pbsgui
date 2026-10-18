"""Rescue-Assistent (Textual) mit Fake-Client; Plan-Ausführung wird abgefangen."""
import shutil

import pytest
from textual.widgets import Button, DataTable, Input

from pbsgui.core import rescue as rescue_mod
from pbsgui.core.rescue import RescueConfig
from pbsgui.tui import rescue as rtui
from test_rescue import rec_dir, snap, put_snapshots  # noqa: F401


async def wait_for(pilot, cond, timeout=6.0):
    waited = 0.0
    while waited < timeout:
        try:
            if cond():
                return True
        except Exception:  # Screen ist aktiv, aber seine Widgets sind noch nicht aufgebaut
            pass
        await pilot.pause(0.05)
        waited += 0.05
    return False


@pytest.fixture
def lab(fake_env, rec_dir, tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    shutil.copytree(rec_dir, tree / "recovery.pxar")
    monkeypatch.setenv("FAKE_PBS_TREE", str(tree))
    monkeypatch.setattr(rescue_mod, "candidate_disks", lambda lsblk_json=None: [
        {"path": "/dev/sda", "size": 600_000_000_000, "model": "Leer", "tran": "sata", "removable": False},
        {"path": "/dev/sdb", "size": 8_000_000_000, "model": "USB", "tran": "usb", "removable": True}])
    put_snapshots(fake_env, [snap(100), snap(300)])
    executed = []
    monkeypatch.setattr(rescue_mod, "execute", lambda cfg, snap, rec, plan, on_line, dry_run=False:
                        executed.append((cfg.target_disk, dry_run, snap.backup_time, cfg.passphrase)) or on_line("ok") or True)
    return executed


def fill(screen, **vals):
    for wid, v in vals.items():
        screen.query_one(f"#{wid}", Input).value = v


async def to_plan(pilot, app):
    await pilot.pause()
    s = app.screen
    fill(s, server="pbs.example", datastore="store1", namespace="ns1", **{"backup-id": "pc", "auth-id": "u@pbs!t"},
         token="geheim", fingerprint="AA:BB")
    s.query_one("#next", Button).press()
    assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "SnapshotScreen")
    assert await wait_for(pilot, lambda: app.screen.query_one("#snaps", DataTable).row_count == 2)
    app.screen.query_one("#next", Button).press()                          # neuester Snapshot (Zeile 0)
    assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "DiskScreen")
    assert await wait_for(pilot, lambda: app.screen.query_one("#disks", DataTable).row_count == 2)
    fill(app.screen, **{"pass": "luks-pw"})
    app.screen.query_one("#next", Button).press()                          # erste Platte /dev/sda
    assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "PlanScreen")


async def test_full_wizard_flow_requires_typed_confirmation(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 60)) as pilot:
        await to_plan(pilot, app)
        assert app.cfg.target_disk == "/dev/sda" and app.snap.backup_time == 300
        assert any("überschrieben" in l for l in app.plan.summary)
        s = app.screen
        s.query_one("#go", Button).press()                                 # ohne Bestätigung
        await pilot.pause()
        assert app.screen.__class__.__name__ == "PlanScreen" and lab == []   # nichts ausgeführt
        fill(s, confirm="/dev/sdb")                                        # falscher Gerätename
        s.query_one("#go", Button).press()
        await pilot.pause()
        assert lab == []
        fill(s, confirm="/dev/sda")
        s.query_one("#go", Button).press()
        assert await wait_for(pilot, lambda: lab != [])
        assert lab == [("/dev/sda", False, 300, "luks-pw")]
        assert await wait_for(pilot, lambda: str(app.screen.query_one("#run-title").render()) == "Fertig")
        assert app.screen.query_one("#reboot", Button).disabled is False   # nach Erfolg darf neu gestartet werden


async def test_dry_run_needs_no_confirmation(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 60)) as pilot:
        await to_plan(pilot, app)
        app.screen.query_one("#dry", Button).press()
        assert await wait_for(pilot, lambda: lab != [])
        assert lab[0][1] is True
        assert await wait_for(pilot, lambda: str(app.screen.query_one("#run-title").render()) == "Fertig")
        assert "nichts wurde verändert" in str(app.screen.query_one("#status").render())


async def test_missing_connection_data_is_reported(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        app.screen.query_one("#next", Button).press()
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConnectScreen"
        assert "Server fehlt" in str(app.screen.query_one("#status").render())


async def test_prefilled_from_config_and_missing_keyfile_blocks(lab):
    cfg = RescueConfig(server="pbs.example", datastore="store1", auth_id="u@pbs!t", fingerprint="AA", namespace="ns1",
                       backup_id="pc")
    app = rtui.RescueApp(cfg)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        s = app.screen
        assert s.query_one("#server", Input).value == "pbs.example" and s.query_one("#namespace", Input).value == "ns1"
        fill(s, token="x", keyfile="/gibt/es/nicht.json")
        s.query_one("#next", Button).press()
        await pilot.pause()
        assert "Schlüsseldatei nicht gefunden" in str(s.query_one("#status").render())


async def test_no_full_backup_gives_clear_message(lab, fake_env):
    put_snapshots(fake_env, [snap(100, files=("root.pxar.didx",))])
    app = rtui.RescueApp(RescueConfig(server="pbs.example", datastore="store1", auth_id="u@pbs!t", fingerprint="AA"))
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        fill(app.screen, token="x")
        app.screen.query_one("#next", Button).press()
        assert await wait_for(pilot, lambda: "Kein Vollbackup" in str(app.screen.query_one("#status").render()))
