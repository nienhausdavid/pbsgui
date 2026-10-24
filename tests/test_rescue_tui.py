"""Rescue-Assistent (Textual) mit Fake-Client; Plan-Ausführung wird abgefangen."""
import shutil

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input

from pbsgui.core import rescue as rescue_mod
from pbsgui.core.rescue import RescueConfig
from pbsgui.tui import rescue as rtui
from test_rescue import snap, put_snapshots  # noqa: F401
from test_rescue_shrink import rec_dir  # noqa: F401  (Wiederherstellungsdaten mit echten LVM-Metadaten)


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


async def test_confirm_field_has_focus_so_typing_works(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 60)) as pilot:
        await to_plan(pilot, app)
        assert await wait_for(pilot, lambda: app.screen.focused is app.screen.query_one("#confirm", Input))
        await pilot.press(*"/dev/sda")                       # wie ein Benutzer: einfach lostippen
        assert app.screen.query_one("#confirm", Input).value == "/dev/sda"


async def test_connect_screen_focuses_first_empty_field(lab):
    cfg = RescueConfig(server="pbs.example", datastore="store1", auth_id="u@pbs!t", fingerprint="AA")
    app = rtui.RescueApp(cfg)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        assert await wait_for(pilot, lambda: app.screen.focused is app.screen.query_one("#token", Input))


from textual.widgets import RadioSet  # noqa: E402


async def to_disks(pilot, app):
    await pilot.pause()
    s = app.screen
    fill(s, server="pbs.example", datastore="store1", namespace="ns1", **{"backup-id": "pc", "auth-id": "u@pbs!t"},
         token="geheim", fingerprint="AA:BB")
    s.query_one("#next", Button).press()
    assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "SnapshotScreen")
    assert await wait_for(pilot, lambda: app.screen.query_one("#snaps", DataTable).row_count == 2)
    app.screen.query_one("#next", Button).press()
    assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "DiskScreen")
    assert await wait_for(pilot, lambda: app.screen.query_one("#disks", DataTable).row_count == 2)


async def test_encryption_choice_shows_matching_fields(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 70)) as pilot:
        await to_disks(pilot, app)
        s = app.screen
        radios = list(s.query_one("#enc", RadioSet).query("RadioButton"))
        assert "Wie im Original (verschlüsselt (LUKS))" in str(radios[0].label)
        assert s.query_one("#pass").display and not s.query_one("#newpass").display          # Standard: wie Original
        radios[2].value = True                                                               # ohne Verschlüsselung
        await pilot.pause()
        assert not s.query_one("#pass").display and not s.query_one("#newpass").display
        radios[1].value = True                                                               # neu verschlüsseln
        await pilot.pause()
        assert not s.query_one("#pass").display and s.query_one("#newpass").display and s.query_one("#newpass2").display


async def test_new_encryption_validates_and_builds_plan(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 70)) as pilot:
        await to_disks(pilot, app)
        s = app.screen
        list(s.query_one("#enc", RadioSet).query("RadioButton"))[1].value = True
        await pilot.pause()
        fill(s, newpass="abcdefgh1", newpass2="anders-anders")
        s.query_one("#next", Button).press()
        await pilot.pause()
        assert "stimmen nicht überein" in str(s.query_one("#status").render()) and app.screen is s
        fill(s, newpass="kurz", newpass2="kurz")
        s.query_one("#next", Button).press()
        await pilot.pause()
        assert "zu kurz" in str(s.query_one("#status").render()) and app.screen is s
        fill(s, newpass="lange-neue-passphrase", newpass2="lange-neue-passphrase")
        s.query_one("#next", Button).press()
        assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "PlanScreen")
        assert app.cfg.encryption == "new" and app.cfg.new_passphrase == "lange-neue-passphrase"
        assert any("luksFormat" in st.argv for st in app.plan.steps)
        assert not any("luksHeaderRestore" in st.argv for st in app.plan.steps)
        assert any("NEU" in l for l in app.plan.summary)


async def test_none_encryption_needs_no_passphrase(lab):
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 70)) as pilot:
        await to_disks(pilot, app)
        s = app.screen
        list(s.query_one("#enc", RadioSet).query("RadioButton"))[2].value = True
        await pilot.pause()
        s.query_one("#next", Button).press()                                                   # keine Passphrase eingetippt
        assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "PlanScreen")
        assert app.cfg.encryption == "none" and not any("cryptsetup" in st.argv for st in app.plan.steps)
        assert any("KEINE" in l for l in app.plan.summary)


async def test_disk_screen_fits_the_linux_console(lab):
    """Auf der Konsole (160x50 bei 1280x800) müssen auch im höchsten Modus alle Knöpfe ohne Scrollen sichtbar sein."""
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(160, 50)) as pilot:
        await to_disks(pilot, app)
        s = app.screen
        list(s.query_one("#enc", RadioSet).query("RadioButton"))[1].value = True      # "neu": die meisten Felder
        await pilot.pause()
        nxt = s.query_one("#next", Button).region
        assert nxt.y + nxt.height <= 50, f"Weiter-Knopf unterhalb des Bildschirms: {nxt}"
        assert s.query_one("#panel").max_scroll_y == 0


async def test_smaller_disk_offers_shrink_option(lab, monkeypatch):
    monkeypatch.setattr(rescue_mod, "candidate_disks", lambda lsblk_json=None: [
        {"path": "/dev/sda", "size": 600_000_000_000, "model": "Gross", "tran": "sata", "removable": False},
        {"path": "/dev/sdb", "size": 128 * 2**30, "model": "Klein", "tran": "sata", "removable": False}])
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 70)) as pilot:
        await to_disks(pilot, app)
        s = app.screen
        box = s.query_one("#shrink", Checkbox)
        assert not box.display                                                   # große Platte: keine Option
        s.query_one("#disks", DataTable).move_cursor(row=1)
        assert await wait_for(pilot, lambda: box.display)                        # kleine Platte: wird angeboten
        fill(s, **{"pass": "luks-pw"})
        s.query_one("#next", Button).press()                                    # ohne Haken: klare Fehlermeldung
        assert await wait_for(pilot, lambda: "Größe anpassen" in str(s.query_one("#status").render()))
        assert app.screen is s
        box.value = True
        s.query_one("#next", Button).press()
        assert await wait_for(pilot, lambda: app.screen.__class__.__name__ == "PlanScreen")
        assert app.plan.resizes and any("->" in l for l in app.plan.summary)


async def test_vpn_and_update_screens_open_with_function_keys_even_from_an_input(monkeypatch):
    from pbsgui.core import update as upd
    monkeypatch.setattr(upd, "update", lambda **kw: upd.UpdateResult("offline", text="kein Netzwerk: Updates übersprungen"))
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        assert await wait_for(pilot, lambda: pilot.app.screen.query_one("#server", Input).has_focus)
        await pilot.press("f3")                       # Fokus steht im Eingabefeld: die Taste darf nicht getippt werden
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "VpnScreen")
        assert await wait_for(pilot, lambda: len(pilot.app.screen.query("#file")) > 0)
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "ConnectScreen")
        assert pilot.app.screen.query_one("#server", Input).value == ""
        await pilot.press("f4")
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "UpdateScreen")
        assert await wait_for(pilot, lambda: "Kein Netzwerk" in str(pilot.app.screen.query_one("#status").render()))


async def test_vpn_screen_connects_wireguard_with_entered_file(monkeypatch, tmp_path):
    from pbsgui.core import vpn
    calls = []
    monkeypatch.setattr(vpn, "wireguard_up", lambda path, *a, **k: calls.append(path) or vpn.Result(True, "wg0 ist oben"))
    app = rtui.RescueApp(RescueConfig(server="", datastore="", auth_id=""))
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        await pilot.press("f3")
        assert await wait_for(pilot, lambda: len(pilot.app.screen.query("#file")) > 0)
        pilot.app.screen.query_one("#file", Input).value = "/media/usb/wg0.conf"
        pilot.app.screen.query_one("#up", Button).press()
        assert await wait_for(pilot, lambda: calls == ["/media/usb/wg0.conf"])
        assert await wait_for(pilot, lambda: "Verbunden" in str(pilot.app.screen.query_one("#status").render()))
