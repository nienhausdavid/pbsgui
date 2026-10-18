"""Backup-Umfang: Home oder ganzes System."""
import subprocess

import pytest

from pbsgui.core import manager as manager_mod
from pbsgui.core.client import PbsClient
from pbsgui.core.manager import ProfileManager
from pbsgui.core.modes import DEFAULT_FULL_EXCLUDES, FULL, HOME, local_mounts
from pbsgui.core.scheduler import SchedulerError
from pbsgui.core.secrets import FileStore

MOUNTS = r"""sysfs /sys sysfs rw 0 0
/dev/mapper/vg-root / ext4 rw 0 0
/dev/nvme0n1p2 /boot ext4 rw 0 0
/dev/nvme0n1p1 /boot/efi vfat rw 0 0
/dev/sdb1 /home xfs rw 0 0
/dev/sdc1 /media/david/USB\040STICK vfat rw 0 0
/dev/sdd1 /mnt/daten ext4 rw 0 0
tmpfs /tmp tmpfs rw 0 0
server:/export /nfs nfs4 rw 0 0
/dev/sde1 /mit\040leerzeichen ext4 rw 0 0
/dev/loop0 /snap/core/1 squashfs ro 0 0
/dev/sdb1 /home xfs rw 0 0
"""


def test_local_mounts_filters_pseudo_removable_and_network():
    assert local_mounts(MOUNTS) == ["/boot", "/boot/efi", "/home", "/mit leerzeichen"]


def test_local_mounts_reads_proc_without_error():
    assert isinstance(local_mounts(), list)


def test_full_mode_args(profile):
    profile.backup_mode, profile.sources, profile.excludes = FULL, ["/ignored"], ["/var/lib/docker"]
    args = PbsClient(profile, "x").backup_args(mounts=["/boot", "/home"])
    assert "root.pxar:/" in args and not any("ignored" in a for a in args)
    pairs = [(args[i], args[i + 1]) for i in range(len(args) - 1)]
    assert ("--include-dev", "/boot") in pairs and ("--include-dev", "/home") in pairs
    assert ("--skip-lost-and-found", "true") in pairs
    excl = [v for k, v in pairs if k == "--exclude"]
    assert excl == [*DEFAULT_FULL_EXCLUDES, "/var/lib/docker"]  # Standard + eigene Ausschlüsse


def test_home_mode_args_have_no_full_options(profile):
    args = PbsClient(profile, "x").backup_args(mounts=["/boot"])
    assert "--include-dev" not in args and "--skip-lost-and-found" not in args
    assert [v for k, v in zip(args, args[1:]) if k == "--exclude"] == ["*.tmp"]


def test_default_mode_is_home_and_effective_sources(profile):
    assert profile.backup_mode == HOME and profile.effective_sources == ["/home/david", "/etc"]
    profile.backup_mode = FULL
    assert profile.effective_sources == ["/"]


def test_full_requires_system_scope(profile, paths):
    m = ProfileManager(paths, FileStore(paths.creds))
    profile.backup_mode = FULL
    with pytest.raises(SchedulerError, match="System-Modus"):
        m.save(profile, token="t")
    profile.backup_mode, profile.scope = HOME, "user"
    m.save(profile, token="t")
    profile.backup_mode = FULL
    with pytest.raises(SchedulerError, match="nicht gewechselt"):
        m.move_scope(profile, "system")


def test_run_backup_home_runs_unprivileged(fake_env, profile, paths, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(paths.state.parent))
    m = ProfileManager(paths, FileStore(paths.creds))
    m.save(profile, token="geheim")
    lines = []
    assert m.run_backup(profile, lines.append) == 0
    assert lines[-1] == "Backup erfolgreich"


def test_run_backup_full_uses_pkexec(profile, paths, monkeypatch):
    m = ProfileManager(paths, FileStore(paths.creds))
    profile.backup_mode, profile.scope = FULL, "system"
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.stdout = iter(["Starte Backup 'Laptop'\n", "Backup erfolgreich\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(manager_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(manager_mod.subprocess, "Popen", FakePopen)
    lines = []
    assert m.run_backup(profile, lines.append) == 0
    cmd = seen["cmd"]
    assert cmd[0] == "pkexec" and cmd[-4:] == ["pbsgui.cli.run", "laptop", "--scope", "system"]
    assert any("Administrator" in l for l in lines) and lines[-1] == "Backup erfolgreich"


def test_run_backup_full_as_root_runs_with_system_paths(profile, paths, monkeypatch):
    m = ProfileManager(paths, FileStore(paths.creds))
    profile.backup_mode, profile.scope = FULL, "system"
    got = {}
    monkeypatch.setattr(manager_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(manager_mod, "run_profile", lambda p, paths, store, on_line: got.update(scope=paths.scope) or 0)
    monkeypatch.setattr(manager_mod, "best_store", lambda scope, d: FileStore(d))
    assert m.run_backup(profile) == 0 and got["scope"] == "system"


@pytest.mark.parametrize("fstype", ["nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "9p", "virtiofs", "ceph",
                                    "glusterfs", "afs", "autofs", "fuse.rclone", "vboxsf", "overlay", "tmpfs"])
def test_network_and_virtual_filesystems_are_never_included(fstype):
    """Allowlist: nur bekannte lokale Dateisysteme bekommen --include-dev, alles Netzwerk-/Virtuelle nie."""
    assert local_mounts(f"srv:/share /daten {fstype} rw 0 0\n/dev/sda1 /boot ext4 rw 0 0\n") == ["/boot"]
