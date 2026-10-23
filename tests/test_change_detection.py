"""Änderungserkennung (`--change-detection-mode metadata`): geteilte Archive, Archivnamen, Fortschritt, Bedienung."""
import json

import pytest
from textual.widgets import Button, Checkbox, Static

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.cli.main import main
from pbsgui.core import manager as manager_mod
from pbsgui.core.client import PbsClient
from pbsgui.core.models import LEGACY, METADATA, Profile, Snapshot, logical_archive
from pbsgui.core.progress import Progress, describe, estimate_total
from pbsgui.core.secrets import FileStore
from pbsgui.tui.app import PbsTui
from test_cli import add_b, env, run  # noqa: F401
from test_tui import connect, text, wait_for  # noqa: F401


# -- Archivnamen --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("filename,expected", [
    ("root.pxar.didx", "root.pxar"), ("root.mpxar.didx", "root.pxar"), ("root.ppxar.didx", "root.pxar"),
    ("root.pxar", "root.pxar"), ("home-david.mpxar.didx", "home-david.pxar"), ("a.b.ppxar.didx", "a.b.pxar"),
    ("catalog.pcat1.didx", None), ("index.json.blob", None), ("drive-scsi0.img.fidx", None), ("pxar", None)])
def test_logical_archive(filename, expected):
    assert logical_archive(filename) == expected


def test_snapshot_archives_merge_split_parts_and_keep_order():
    s = Snapshot("host", "pc", 1, files=["recovery.ppxar.didx", "recovery.mpxar.didx", "root.mpxar.didx",
                                          "root.ppxar.didx", "catalog.pcat1.didx", "index.json.blob"])
    assert s.archives == ["recovery.pxar", "root.pxar"] and s.split is True
    legacy = Snapshot("host", "pc", 1, files=["root.pxar.didx", "catalog.pcat1.didx"])
    assert legacy.archives == ["root.pxar"] and legacy.split is False


def test_old_profiles_stay_legacy_and_new_field_roundtrips(profile):
    data = profile.to_dict()
    data.pop("change_detection")
    assert Profile.from_dict(data).change_detection == LEGACY             # Profile von früher ändern ihr Verhalten nicht
    profile.change_detection = METADATA
    assert Profile.from_dict(profile.to_dict()).change_detection == METADATA


# -- Client-Aufruf ---------------------------------------------------------------------------------------------------
def test_flag_only_for_metadata_mode(profile):
    assert "--change-detection-mode" not in PbsClient(profile, "x").backup_args()
    profile.change_detection = METADATA
    args = PbsClient(profile, "x").backup_args()
    assert args[args.index("--change-detection-mode") + 1] == "metadata"


def test_backup_with_metadata_mode_yields_split_snapshot_that_pbsgui_understands(fake_env, profile):
    profile.change_detection = METADATA
    c = PbsClient(profile, "x")
    assert c.backup() == 0
    snap = c.list_snapshots()[0]
    assert snap.files == ["home-david.mpxar.didx", "home-david.ppxar.didx", "etc.mpxar.didx", "etc.ppxar.didx"] \
        and snap.archives == ["home-david.pxar", "etc.pxar"] and snap.split is True


# -- Fortschritt: Lesemenge unbekannt, wenn nur Geändertes gelesen wird -----------------------------------------------------
def snaps(*split_flags):
    return [Snapshot("host", "laptop", 100 + i, size=1000 + i,
                     files=["root.mpxar.didx", "root.ppxar.didx"] if sp else ["root.pxar.didx"])
            for i, sp in enumerate(split_flags)]


def test_estimate_total_metadata_after_split_backup_is_unknown(profile):
    profile.change_detection = METADATA
    total, basis = estimate_total(profile, snaps(False, True))
    assert total is None and "nur geänderte Dateien" in basis


def test_estimate_total_first_metadata_run_still_reads_everything(profile):
    profile.change_detection = METADATA
    assert estimate_total(profile, snaps(False, False)) == (1001, "letztes Backup")   # Vorgänger war klassisch


def test_estimate_total_legacy_profile_ignores_split_history(profile):
    profile.change_detection = LEGACY
    assert estimate_total(profile, snaps(True)) == (1000, "letztes Backup")


def test_describe_shows_reason_instead_of_first_backup_hint():
    p = Progress(done=5_000_000, total=None, rate=1e6, elapsed=5, basis="nur geänderte Dateien (Menge vorher unbekannt)")
    assert "nur geänderte Dateien" in describe(p) and "erstes Backup" not in describe(p)
    assert "erstes Backup" in describe(Progress(done=1, total=None, rate=1.0, elapsed=1))


# -- Rettungs-ISO findet auch geteilte Snapshots ---------------------------------------------------------------------------
def test_rescue_recognises_split_full_backups(monkeypatch):
    from types import SimpleNamespace
    from pbsgui.core import rescue
    split = Snapshot("host", "pc", 2, files=["root.mpxar.didx", "root.ppxar.didx", "recovery.mpxar.didx",
                                              "recovery.ppxar.didx"])
    old = Snapshot("host", "pc", 1, files=["root.pxar.didx", "recovery.pxar.didx"])
    partial = Snapshot("host", "pc", 3, files=["root.mpxar.didx", "root.ppxar.didx"])    # ohne Layout-Daten
    monkeypatch.setattr(rescue, "_client", lambda cfg: SimpleNamespace(list_snapshots=lambda: [old, split, partial]))
    assert [s.backup_time for s in rescue.list_full_snapshots(SimpleNamespace(backup_id=""))] == [1, 2]


# -- Bedienung -------------------------------------------------------------------------------------------------------
def test_cli_add_defaults_to_metadata_and_can_opt_out(env, capsys, server):
    assert add_b(capsys, server)[0] == 0
    assert json.loads(run(capsys, "list", "--json")[1])[0]["change_detection"] == "metadata"
    assert add_b(capsys, server, "--change-detection", "legacy", name="Alt")[0] == 0
    prof = {p["name"]: p for p in json.loads(run(capsys, "list", "--json")[1])}
    assert prof["Alt"]["change_detection"] == "legacy"


def test_cli_detection_command(env, capsys, server):
    add_b(capsys, server)
    assert "metadata" in run(capsys, "detection", "Test")[1]
    code, out, _ = run(capsys, "detection", "Test", "legacy")
    assert code == 0 and "wieder jede Datei" in out
    assert json.loads(run(capsys, "list", "--json")[1])[0]["change_detection"] == "legacy"
    code, out, _ = run(capsys, "detection", "Test", "metadata")
    assert "liest einmal alles" in out and "3.3" in out
    again = run(capsys, "detection", "Test", "metadata")[1]                       # unverändert: nur Anzeige, keine Hinweise
    assert "metadata" in again and "liest einmal alles" not in again
    with pytest.raises(SystemExit) as ex:                                           # argparse lehnt ab
        run(capsys, "detection", "Test", "quatsch")
    assert ex.value.code == 2


def test_cli_backup_restore_roundtrip_with_split_snapshot(env, capsys, server):
    add_b(capsys, server)                                                          # Standard: metadata
    assert run(capsys, "backup", "Test")[0] == 0
    snaps = json.loads(run(capsys, "snapshots", "Test", "--json")[1])
    assert snaps[0]["files"] == ["etc.mpxar.didx", "etc.ppxar.didx"]
    target = env / "out"
    code, out, err = run(capsys, "restore", "Test", "--target", str(target))      # Archivname wird erkannt
    assert code == 0, err
    assert (target / "restored.txt").exists()


def test_cli_files_lists_split_snapshot(env, capsys, server, tmp_path, monkeypatch):
    add_b(capsys, server)
    run(capsys, "backup", "Test")
    arch = tmp_path / "pxar" / "etc.pxar"
    (arch / "d").mkdir(parents=True)
    (arch / "d/a.txt").write_text("x")
    monkeypatch.setenv("FAKE_PBS_TREE", str(tmp_path / "pxar"))
    monkeypatch.setenv("FAKE_PBS_SPLIT", "1")                                       # Katalog zeigt etc.mpxar.didx
    code, out, err = run(capsys, "files", "Test", "/d")
    assert code == 0, err
    assert "a.txt" in out


@pytest.fixture
def mgr(fake_env, tmp_path, monkeypatch, profile):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-s"))
    monkeypatch.setattr(manager_mod, "best_store", lambda scope, d: FileStore(d))
    m = manager_mod.ProfileManager()
    profile.sources = ["/etc"]
    m.save(profile, token="geheim")
    return m


async def test_tui_dialog_defaults_to_only_changed_and_creates_metadata_profile(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 90)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        box = s.query_one("#only-changed", Checkbox)
        assert box.value is True
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        assert next(p for p in mgr.profiles() if p.name == "NeuerServer").change_detection == METADATA


async def test_tui_dialog_can_opt_out(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 90)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        s.query_one("#only-changed", Checkbox).value = False
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        assert next(p for p in mgr.profiles() if p.name == "NeuerServer").change_detection == LEGACY
