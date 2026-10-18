"""Backup-Umfang: Home oder ganzes System."""
import subprocess

import pytest

from pbsgui.core import manager as manager_mod
from pbsgui.core.client import PbsClient
from pbsgui.core.manager import ProfileManager
from pbsgui.core.modes import DEFAULT_FULL_EXCLUDES, FULL, HOME, detect_mounts, is_usb, plan_mounts
from pbsgui.core.scheduler import SchedulerError
from pbsgui.core.secrets import FileStore

def mi(mountpoint, fstype="ext4", source="/dev/sda1", majmin="8:1", root="/", mid=100):
    """Eine Zeile im Format von /proc/self/mountinfo."""
    return f"{mid} 30 {majmin} {root} {mountpoint} rw,relatime shared:1 - {fstype} {source} rw"


MOUNTINFO = "\n".join([
    mi("/", source="/dev/mapper/vg-root", majmin="254:1", mid=30),
    mi("/boot", source="/dev/nvme0n1p2", majmin="259:2", mid=50),
    mi("/boot/efi", "vfat", "/dev/nvme0n1p1", "259:1", mid=53),
    mi("/home", "xfs", "/dev/sdb1", "8:17", mid=60),
    mi("/media/david/USB\\040STICK", "vfat", "/dev/sdc1", "8:33", mid=61),
    mi("/mnt/daten", "ext4", "/dev/sdd1", "8:49", mid=62),
    mi("/tmp", "tmpfs", "tmpfs", "0:42", mid=47),
    mi("/nfs", "nfs4", "srv:/export", "0:90", mid=63),
    mi("/mit\\040leerzeichen", "ext4", "/dev/sde1", "8:65", mid=64),
    mi("/snap/core/1", "squashfs", "/dev/loop0", "7:0", mid=65),
    mi("/var/lib/docker/overlay2/x/merged", "overlay", "overlay", "0:62", mid=66),
    mi("/srv/home-bind", "xfs", "/dev/sdb1", "8:17", mid=67),                      # gleiche Wurzel: Duplikat
    mi("/srv/sub", "xfs", "/dev/sdb1", "8:17", root="/david/projekt", mid=68),   # Unterverzeichnis-Bind
    mi("/srv/subvol", "btrfs", "/dev/sdf1", "0:99", root="/@data", mid=69),       # btrfs-Subvolume: KEIN Bind
])


def by_mp(mounts):
    return {m.mountpoint: m for m in mounts}


def test_detect_mounts_filters_and_classifies():
    ms = by_mp(detect_mounts(MOUNTINFO, usb_check=lambda src: src == "/dev/sdc1"))
    assert set(ms) == {"/boot", "/boot/efi", "/home", "/media/david/USB STICK", "/mnt/daten", "/mit leerzeichen",
                       "/srv/home-bind", "/srv/sub", "/srv/subvol"}
    assert ms["/media/david/USB STICK"].usb and not ms["/boot"].usb
    assert ms["/srv/home-bind"].bind and ms["/srv/sub"].bind
    assert not ms["/srv/subvol"].bind and not ms["/home"].bind  # Subvolume und Original sind keine Duplikate


def test_default_selection_is_internal_only():
    ms = by_mp(detect_mounts(MOUNTINFO, usb_check=lambda src: src == "/dev/sdc1"))
    default = sorted(mp for mp, m in ms.items() if m.default_selected)
    assert default == ["/boot", "/boot/efi", "/home", "/mit leerzeichen", "/srv/subvol"]
    assert not ms["/media/david/USB STICK"].default_selected and not ms["/mnt/daten"].default_selected


def test_plan_mounts_default_explicit_and_binds():
    det = detect_mounts(MOUNTINFO, usb_check=lambda src: src == "/dev/sdc1")
    inc, binds = plan_mounts(None, det)
    assert "/boot" in inc and "/media/david/USB STICK" not in inc and binds == ["/srv/home-bind", "/srv/sub"]
    inc, _ = plan_mounts(["/boot", "/media/david/USB STICK", "/nicht/mehr/da", "/srv/home-bind"], det)
    assert inc == ["/boot", "/media/david/USB STICK"]  # USB nur auf ausdrückliche Wahl; Duplikat/unbekannt entfallen
    assert plan_mounts([], det)[0] == []


def test_detect_mounts_reads_real_proc_without_error():
    assert isinstance(detect_mounts(), list)


@pytest.mark.parametrize("fstype", ["nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "9p", "virtiofs", "ceph",
                                    "glusterfs", "afs", "autofs", "fuse.rclone", "vboxsf", "overlay", "tmpfs"])
def test_network_and_virtual_filesystems_are_never_offered(fstype):
    """Allowlist: nur bekannte lokale Dateisysteme stehen zur Auswahl, Netzwerk-/virtuelle nie."""
    info = mi("/daten", fstype, "srv:/share", "0:77") + "\n" + mi("/boot", "ext4", "/dev/sda1", "8:1")
    assert [m.mountpoint for m in detect_mounts(info, usb_check=lambda s: False)] == ["/boot"]


def test_is_usb_follows_dm_slaves(tmp_path):
    root = tmp_path / "sys"
    usb_dev = root / "devices/pci0/usb2/2-1/host6/block/sdb/sdb1"
    int_dev = root / "devices/pci0/ata1/host0/block/sda/sda1"
    dm = root / "devices/virtual/block/dm-0"
    for d in (usb_dev, int_dev, dm / "slaves"):
        d.mkdir(parents=True)
    cls = root / "class/block"
    cls.mkdir(parents=True)
    (cls / "sdb1").symlink_to(usb_dev)
    (cls / "sda1").symlink_to(int_dev)
    (cls / "dm-0").symlink_to(dm)
    (dm / "slaves" / "sdb1").symlink_to(usb_dev)          # LUKS auf USB-Partition
    assert is_usb("/dev/sdb1", str(cls)) is True
    assert is_usb("/dev/sda1", str(cls)) is False
    assert is_usb("/dev/dm-0", str(cls)) is True
    assert is_usb("tmpfs", str(cls)) is False


def test_full_mode_args(profile):
    profile.backup_mode, profile.sources, profile.excludes = FULL, ["/ignored"], ["/var/lib/docker"]
    args = PbsClient(profile, "x").backup_args(mounts=["/boot", "/home"], binds=["/srv/dup"])
    assert "root.pxar:/" in args and not any("ignored" in a for a in args)
    pairs = [(args[i], args[i + 1]) for i in range(len(args) - 1)]
    assert ("--include-dev", "/boot") in pairs and ("--include-dev", "/home") in pairs
    assert ("--skip-lost-and-found", "true") in pairs
    excl = [v for k, v in pairs if k == "--exclude"]
    assert excl == [*DEFAULT_FULL_EXCLUDES, "/srv/dup", "/var/lib/docker"]  # Standard + Bind-Duplikate + eigene
    assert "/var/lib/docker" in excl and not {"/mnt", "/media"} & set(excl)  # nur die eigenen, keine Docker-Standards


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
