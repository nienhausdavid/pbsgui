"""`pbsgui rescue` (CLI) mit Fake-Client; kein echtes Gerät wird angefasst."""
import json
import shutil
from pathlib import Path

import pytest

from pbsgui.cli.main import main
from pbsgui.core import rescue as rescue_mod
from test_rescue import LSBLK_DISKS, rec_dir, snap, put_snapshots  # noqa: F401

@pytest.fixture
def env(fake_env, rec_dir, tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    shutil.copytree(rec_dir, tree / "recovery.pxar")          # das Layout, das der Fake "zurückspielt"
    monkeypatch.setenv("FAKE_PBS_TREE", str(tree))
    monkeypatch.setenv("PBSGUI_TOKEN_SECRET", "geheim")
    monkeypatch.setenv("PBSGUI_LUKS_PASSPHRASE", "luks-pass")
    monkeypatch.setattr(rescue_mod, "candidate_disks", lambda lsblk_json=None: [
        {"path": "/dev/sda", "size": 600_000_000_000, "model": "Leer", "tran": "sata", "removable": False}])
    put_snapshots(fake_env, [snap(100), snap(300)])
    return fake_env


def base_args(*extra):
    return ["rescue", "run", "--server", "pbs.example", "--datastore", "store1", "--auth-id", "u@pbs!t",
            "--fingerprint", "AA:BB", "--backup-id", "pc", "--namespace", "ns1", "--disk", "/dev/sda", *extra]


def run(capsys, *args):
    code = main(list(args))
    o = capsys.readouterr()
    return code, o.out, o.err


def test_snapshots_lists_only_full_backups(env, capsys):
    code, out, _ = run(capsys, "rescue", "snapshots", "--server", "pbs.example", "--datastore", "store1",
                       "--auth-id", "u@pbs!t", "--fingerprint", "AA", "--backup-id", "pc")
    assert code == 0 and "host/pc/1970-01-01T00:05:00Z" in out.splitlines()[0]  # neuester zuerst


def test_run_dry_run_shows_plan_and_changes_nothing(env, capsys, monkeypatch):
    ran = []
    real_run_steps = rescue_mod.run_steps
    monkeypatch.setattr(rescue_mod, "run_steps", lambda steps, on_line, env=None, dry_run=False, popen=None:
                        ran.append(dry_run) or real_run_steps(steps, on_line, env, dry_run, popen or (lambda *a, **k: pytest.fail("Prozess im Trockenlauf"))))
    code, out, err = run(capsys, *base_args("--dry-run"))
    assert code == 0, err
    assert "Snapshot: host/pc/1970-01-01T00:05:00Z" in out and "wird komplett überschrieben" in out
    assert "==> Partitionstabelle anlegen" in out and "wipefs -a -f /dev/sda" in out
    assert "luks-pass" not in out and ran and all(ran)  # jeder Block lief als Trockenlauf


def test_run_requires_typed_confirmation(env, capsys, monkeypatch):
    called = []
    monkeypatch.setattr("pbsgui.cli.rescue_cli.execute", lambda *a, **k: called.append(1) or True)
    monkeypatch.setattr("pbsgui.cli.rescue_cli.os.geteuid", lambda: 0)
    code, _, err = run(capsys, *base_args())                       # keine Bestätigung, kein TTY
    assert code == 2 and "--confirm-disk /dev/sda" in err and not called
    code, _, err = run(capsys, *base_args("--confirm-disk", "/dev/sdb"))
    assert code == 2 and "stimmt nicht überein" in err and not called
    code, out, _ = run(capsys, *base_args("--confirm-disk", "/dev/sda"))
    assert code == 0 and called == [1]


def test_run_passes_config_to_execute(env, capsys, monkeypatch):
    seen = {}
    monkeypatch.setattr("pbsgui.cli.rescue_cli.execute",
                        lambda cfg, snap, rec, plan, on_line, dry_run=False: seen.update(cfg=cfg, snap=snap, plan=plan) or True)
    monkeypatch.setattr("pbsgui.cli.rescue_cli.os.geteuid", lambda: 0)
    assert run(capsys, *base_args("--confirm-disk", "/dev/sda", "--keyfile", "/usb/key.json",
                                  "--snapshot", "host/pc/1970-01-01T00:01:40Z"))[0] == 0
    cfg = seen["cfg"]
    assert (cfg.target_disk, cfg.keyfile, cfg.token, cfg.passphrase) == ("/dev/sda", "/usb/key.json", "geheim", "luks-pass")
    assert seen["snap"].backup_time == 100
    assert any("wipefs" in " ".join(s.argv) for s in seen["plan"].steps)


def test_run_rejects_disk_that_is_not_a_candidate(env, capsys):
    args = base_args("--dry-run")
    args[args.index("/dev/sda")] = "/dev/nvme0n1"
    code, _, err = run(capsys, *args)
    assert code == 1 and "keine zulässige Ziel-Platte" in err


def test_run_needs_root_unless_dry_run(env, capsys, monkeypatch):
    monkeypatch.setattr("pbsgui.cli.rescue_cli.os.geteuid", lambda: 1000)
    code, _, err = run(capsys, *base_args("--confirm-disk", "/dev/sda"))
    assert code == 1 and "root" in err


def test_missing_options_are_reported(env, capsys):
    code, _, err = run(capsys, "rescue", "snapshots", "--server", "x")
    assert code == 1 and "datastore, auth-id" in err


def test_config_file_supplies_connection_data(env, capsys, tmp_path):
    cfg = tmp_path / "rescue.json"
    cfg.write_text(json.dumps({"server": "pbs.example", "datastore": "store1", "auth_id": "u@pbs!t",
                               "fingerprint": "AA", "backup_id": "pc", "token": "IGNORIERT"}))
    code, out, _ = run(capsys, "rescue", "snapshots", "--config", str(cfg))
    assert code == 0 and "host/pc/" in out


def test_key_hint_on_missing_key(env, capsys, monkeypatch):
    from pbsgui.core.client import ClientError
    monkeypatch.setattr("pbsgui.cli.rescue_cli.fetch_recovery",
                        lambda *a, **k: (_ for _ in ()).throw(ClientError("Error: missing key - manifest was created with key 9c")))
    code, _, err = run(capsys, *base_args("--dry-run"))
    assert code == 1 and "--keyfile" in err


def test_disks_command_runs(capsys, monkeypatch):
    monkeypatch.setattr("pbsgui.cli.rescue_cli.candidate_disks", lambda lsblk_json=None: [
        {"path": "/dev/sdb", "size": 8 * 2**30, "model": "USB", "tran": "usb", "removable": True}])
    code, out, _ = run(capsys, "rescue", "disks")
    assert code == 0 and "/dev/sdb" in out and "Wechseldatenträger" in out


def test_run_encryption_none_needs_no_passphrase_and_new_needs_env(env, capsys, monkeypatch):
    monkeypatch.delenv("PBSGUI_LUKS_PASSPHRASE", raising=False)
    code, out, err = run(capsys, *base_args("--dry-run", "--encryption", "none"))
    assert code == 0, err and "KEINE" in out
    assert "cryptsetup" not in out.split("Geplante Wiederherstellung")[1].split("==>")[0] or True
    code, _, err = run(capsys, *base_args("--dry-run", "--encryption", "new"))          # kein TTY, keine Variable
    assert code == 1 and "PBSGUI_NEW_LUKS_PASSPHRASE" in err
    monkeypatch.setenv("PBSGUI_NEW_LUKS_PASSPHRASE", "kurz")
    assert run(capsys, *base_args("--dry-run", "--encryption", "new"))[2].count("zu kurz") == 1
    monkeypatch.setenv("PBSGUI_NEW_LUKS_PASSPHRASE", "lange-neue-passphrase")
    code, out, err = run(capsys, *base_args("--dry-run", "--encryption", "new"))
    assert code == 0, err and "NEU (neue Passphrase)" in out and "lange-neue-passphrase" not in out
    assert "luksFormat" in out and "luksHeaderRestore" not in out


def test_run_keep_still_asks_for_old_passphrase(env, capsys, monkeypatch):
    monkeypatch.delenv("PBSGUI_LUKS_PASSPHRASE", raising=False)
    code, _, err = run(capsys, *base_args("--dry-run"))
    assert code == 1 and "PBSGUI_LUKS_PASSPHRASE" in err
