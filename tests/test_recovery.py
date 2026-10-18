"""Wiederherstellung auf neuem PC: Metadaten-Sammlung, Notfallblatt, Pflicht-Schlüsselexport."""
import json
import os
import stat
import subprocess

import pytest

from pbsgui.core import manager as manager_mod
from pbsgui.core import runner as runner_mod
from pbsgui.core.manager import ProfileManager
from pbsgui.core.modes import FULL
from pbsgui.core.recovery import (RecoveryError, collect_recovery, ensure_recoverable, needs_key_export,
                                  render_sheet)
from pbsgui.core.runner import run_profile
from pbsgui.core.scheduler import SchedulerError
from pbsgui.core.secrets import FileStore

LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "kname": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "children": [
        {"name": "nvme0n1p1", "kname": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part", "fstype": "vfat"},
        {"name": "nvme0n1p3", "kname": "nvme0n1p3", "path": "/dev/nvme0n1p3", "type": "part",
         "fstype": "crypto_LUKS", "children": [{"name": "crypt", "kname": "dm-0", "type": "crypt",
                                                "fstype": "LVM2_member"}]}]}]}


class FakeRun:
    """Simuliert die Systembefehle; schreibt Artefakt-Dateien wie sgdisk/cryptsetup/vgcfgbackup."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        name = argv[0]
        if name in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "Permission denied")
        if name == "lsblk":
            return subprocess.CompletedProcess(argv, 0, json.dumps(LSBLK), "")
        if name == "sgdisk":
            open(argv[1].split("=", 1)[1], "wb").write(b"GPT")
        if name == "cryptsetup":
            open(argv[argv.index("--header-backup-file") + 1], "wb").write(b"LUKS-HEADER")
        if name == "vgcfgbackup":
            open(argv[2].replace("%s", "vg0"), "w").write("vg0 {}")
        return subprocess.CompletedProcess(argv, 0, f"out-{name}\n", "")


def have_all(_name):
    return "/usr/sbin/x"


def test_collect_writes_metadata_and_manifest(profile, tmp_path):
    profile.backup_mode, profile.encrypt, profile.key_fingerprint = FULL, True, "aa:bb"
    run = FakeRun()
    got = collect_recovery(tmp_path / "rec", profile, run=run, which=have_all)
    d = tmp_path / "rec"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for f in ("lsblk.json", "blkid.txt", "efibootmgr.txt", "partition-table-nvme0n1.sfdisk", "gpt-nvme0n1.bin",
              "luks-header-nvme0n1p3.img", "lvm-vg0.vg", "lvm-pvs.json", "dpkg-selections.txt", "MANIFEST.json",
              "NOTFALLBLATT.txt"):
        assert (d / f).exists(), f
    assert stat.S_IMODE((d / "luks-header-nvme0n1p3.img").stat().st_mode) == 0o600
    m = json.loads((d / "MANIFEST.json").read_text())
    assert m["profil"]["key_fingerprint"] == "aa:bb" and m["firmware"] in ("uefi", "bios")
    assert got.warnings == [] and "LUKS-HEADER" in (d / "luks-header-nvme0n1p3.img").read_text()


def test_luks_header_only_in_encrypted_backups(profile, tmp_path):
    profile.backup_mode, profile.encrypt = FULL, False
    got = collect_recovery(tmp_path / "rec", profile, run=FakeRun(), which=have_all)
    assert not list((tmp_path / "rec").glob("luks-header-*"))
    assert any("LUKS-Header NICHT gesichert" in w for w in got.warnings)  # sonst läge er unverschlüsselt auf dem Server


def test_collect_is_best_effort(profile, tmp_path):
    profile.backup_mode, profile.encrypt = FULL, True
    got = collect_recovery(tmp_path / "rec", profile, run=FakeRun(fail={"sfdisk", "efibootmgr"}),
                           which=lambda n: None if n == "mdadm" else "/x")
    assert (tmp_path / "rec/MANIFEST.json").exists()  # läuft trotz Fehlern durch
    joined = " ".join(got.warnings)
    assert "sfdisk" in joined and "efibootmgr" in joined and "mdadm nicht installiert" in joined


def test_sheet_contains_access_data_but_no_secrets(profile):
    profile.backup_mode, profile.encrypt, profile.key_fingerprint = FULL, True, "9c:1a:b8"
    profile.namespace = "ns1"
    sheet = render_sheet(profile)
    for needed in (profile.server, profile.fingerprint, profile.datastore, "ns1", profile.auth_id, "9c:1a:b8",
                   "root.pxar", "--keyfile", "snapshot list --ns ns1"):
        assert needed in sheet, needed
    assert "Token-Secret: NICHT hier" in sheet
    profile.encrypt = False
    assert "--keyfile" not in render_sheet(profile)


def test_needs_key_export_rules(profile):
    assert not needs_key_export(profile)                       # Home-Modus
    profile.backup_mode = FULL
    assert not needs_key_export(profile)                       # ohne Verschlüsselung kein Schlüssel
    profile.encrypt = True
    assert needs_key_export(profile)                           # Vollbackup + Auto-Key, nicht exportiert
    profile.keyfile = "/eigener/key.json"
    assert not needs_key_export(profile)                       # extern verwalteter Schlüssel
    profile.keyfile, profile.key_exported_at = "", "2026-10-18 15:00:00"
    assert not needs_key_export(profile)
    profile.key_exported_at = ""
    with pytest.raises(RecoveryError, match="pbsgui key export"):
        ensure_recoverable(profile)
    assert isinstance(RecoveryError("x"), SchedulerError)


def test_runner_refuses_full_backup_without_key_export(fake_env, profile, paths):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    profile.backup_mode, profile.encrypt = FULL, True
    lines = []
    assert run_profile(profile, paths, store, lines.append) == 4
    assert "noch nicht exportiert" in lines[0]
    assert not (fake_env / "calls.jsonl").exists()  # der Client wurde gar nicht gestartet


def test_manager_blocks_schedule_and_manual_run_until_exported(fake_env, profile, paths, monkeypatch):
    m = ProfileManager(paths, FileStore(paths.creds))
    pushed = []
    monkeypatch.setattr(ProfileManager, "_admin", lambda self, req: pushed.append(req))
    profile.backup_mode, profile.scope, profile.encrypt, profile.schedule = FULL, "system", True, "daily"
    assert m.enable_encryption(profile) is True
    with pytest.raises(RecoveryError):
        m.save(profile, schedule="install")
    lines = []
    assert m.run_backup(profile, lines.append) == 4 and "Fehler" in lines[0]
    m.export_key(profile, str(paths.config / "key-export.json"))
    assert profile.key_exported_at
    assert m.profiles_store.load(profile.name).key_exported_at == profile.key_exported_at  # gespeichert
    assert pushed[-1]["profile"]["key_exported_at"]                                        # und in die System-Kopie
    m.save(profile, schedule="install")  # jetzt erlaubt (Fake-Admin)


def test_full_backup_collects_recovery_data_as_root(fake_env, profile, paths, monkeypatch):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    profile.backup_mode, profile.encrypt = FULL, False
    seen = {}

    def fake_collect(dest, prof, **kw):
        seen["dest"] = dest
        from pbsgui.core.recovery import Collected
        return Collected(files=["a", "b"], warnings=["x fehlt"])

    monkeypatch.setattr(runner_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner_mod, "collect_recovery", fake_collect)
    lines = []
    assert run_profile(profile, paths, store, lines.append) == 0
    assert seen["dest"] == paths.state / "recovery" / profile.slug  # unter /var/lib/pbsgui -> im Backup enthalten
    assert any("2 Dateien, 1 Warnungen" in l for l in lines) and any("x fehlt" in l for l in lines)


def test_collect_failure_never_blocks_backup(fake_env, profile, paths, monkeypatch):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    profile.backup_mode, profile.encrypt = FULL, False
    monkeypatch.setattr(runner_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner_mod, "collect_recovery", lambda *a, **k: (_ for _ in ()).throw(OSError("voll")))
    lines = []
    assert run_profile(profile, paths, store, lines.append) == 0
    assert any("nicht gesammelt" in l for l in lines)


def test_home_backup_unaffected(fake_env, profile, paths):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    assert run_profile(profile, paths, store, lambda _l: None) == 0


def test_missing_recovery_tools_names_the_package(monkeypatch):
    from pbsgui.core import recovery as rec
    monkeypatch.setattr(rec.Path, "exists", lambda self: False)   # auch /usr/sbin enthält nichts
    miss = rec.missing_recovery_tools(which=lambda t: None if t == "sfdisk" else "/usr/bin/" + t)
    assert miss == {"sfdisk": "fdisk"}
    assert rec.missing_recovery_tools(which=lambda t: "/x") == {}


def test_collect_warns_loudly_when_sfdisk_is_missing(profile, tmp_path):
    profile.backup_mode, profile.encrypt = FULL, False
    got = collect_recovery(tmp_path / "rec", profile, run=FakeRun(), which=lambda n: None if n == "sfdisk" else "/x")
    assert any("sfdisk nicht installiert (Paket fdisk)" in w and "OHNE sie" in w for w in got.warnings)


def test_confirm_key_export_checks_fingerprint_and_content(fake_env, profile, paths, monkeypatch, tmp_path):
    import json as _json
    m = ProfileManager(paths, FileStore(paths.creds))
    monkeypatch.setattr(ProfileManager, "_admin", lambda self, req: None)
    profile.backup_mode, profile.scope, profile.encrypt = FULL, "system", True
    assert m.enable_encryption(profile)
    real = _json.loads(m.secrets.get(profile.slug, "key"))
    good = tmp_path / "gut.json"
    good.write_text(_json.dumps(real))
    assert needs_key_export(profile)
    m.confirm_key_export(profile, str(good))
    assert profile.key_exported_at and not needs_key_export(profile)
    assert m.profiles_store.load(profile.name).key_exported_at == profile.key_exported_at
    profile.key_exported_at = ""
    for bad, msg in ((_json.dumps({"fingerprint": "ff:ff", "data": "x"}), "Falscher Schlüssel"),
                     (_json.dumps({"foo": 1}), "keine Schlüsseldatei"),
                     (_json.dumps({**real, "data": "anderer-inhalt"}), "beschädigt"), ("{kaputt", "nicht lesbar")):
        f = tmp_path / "bad.json"
        f.write_text(bad)
        with pytest.raises(SchedulerError, match=msg):
            m.confirm_key_export(profile, str(f))
        assert not profile.key_exported_at
