"""Rescue-Plan: Layout aus den Wiederherstellungsdaten nachbauen (ohne echte Geräte)."""
import json
import subprocess

import pytest

from pbsgui.core.rescue import (Node, Plan, RecoveryData, RescueError, Step, build_layout_plan, finish_steps,
                                partition_path, restore_step, retarget_sfdisk, run_steps)

GIB = 2 ** 30
DISK_SIZE = 512_110_190_592

SFDISK = """label: gpt
label-id: 2AC01B6E-E4F8-4153-AEA7-48BEDE2CE30B
device: /dev/nvme0n1
unit: sectors
first-lba: 34
last-lba: 1000215182
sector-size: 512

/dev/nvme0n1p1 : start=        2048, size=     1998848, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=19AAE77B-0F5C-48ED-A87E-F8B487521EBD
/dev/nvme0n1p2 : start=     2000896, size=     2000896, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, uuid=409F5AA4-9E5D-4646-B61B-42567C9333D8
/dev/nvme0n1p3 : start=     4001792, size=   996212736, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, uuid=04492FD8-7696-47D5-AB26-38A34FFC6E35
"""

LSBLK = {"blockdevices": [{
    "name": "nvme0n1", "kname": "nvme0n1", "path": "/dev/nvme0n1", "size": DISK_SIZE, "type": "disk", "children": [
        {"name": "nvme0n1p1", "kname": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part", "fstype": "vfat",
         "uuid": "19AA-E77B", "label": None, "mountpoints": ["/boot/efi"]},
        {"name": "nvme0n1p2", "kname": "nvme0n1p2", "path": "/dev/nvme0n1p2", "type": "part", "fstype": "ext4",
         "uuid": "11111111-2222-3333-4444-555555555555", "label": "boot", "mountpoints": ["/boot"]},
        {"name": "nvme0n1p3", "kname": "nvme0n1p3", "path": "/dev/nvme0n1p3", "type": "part",
         "fstype": "crypto_LUKS", "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "mountpoints": [None], "children": [
            {"name": "nvme0n1p3_crypt", "kname": "dm-0", "path": "/dev/mapper/nvme0n1p3_crypt", "type": "crypt",
             "fstype": "LVM2_member", "uuid": "pv-uuid-0001", "mountpoints": [None], "children": [
                {"name": "vg--root", "kname": "dm-1", "path": "/dev/mapper/vg--root", "type": "lvm",
                 "fstype": "ext4", "uuid": "99999999-8888-7777-6666-555555555555", "mountpoints": ["/"]},
                {"name": "vg--swap", "kname": "dm-2", "path": "/dev/mapper/vg--swap", "type": "lvm",
                 "fstype": "swap", "uuid": "77777777-6666-5555-4444-333333333333", "mountpoints": ["[SWAP]"]}]}]}]}]}

PVS = {"report": [{"pv": [{"pv_name": "/dev/mapper/nvme0n1p3_crypt", "vg_name": "vg", "pv_uuid": "PV-UUID-1234"}]}]}


@pytest.fixture
def rec_dir(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "lsblk.json").write_text(json.dumps(LSBLK))
    (d / "partition-table-nvme0n1.sfdisk").write_text(SFDISK)
    (d / "luks-header-nvme0n1p3.img").write_bytes(b"HDR")
    (d / "lvm-vg.vg").write_text("vg {}")
    (d / "lvm-pvs.json").write_text(json.dumps(PVS))
    (d / "efibootmgr.txt").write_text("BootCurrent: 0001\nBoot0001* debian\tHD(1,GPT)\n")
    (d / "MANIFEST.json").write_text(json.dumps({"firmware": "uefi"}))
    return d


def titles(plan):
    return [s.title for s in plan.steps]


def idx(plan, fragment):
    return next(i for i, s in enumerate(plan.steps) if fragment in s.title)


def test_partition_path_naming():
    assert partition_path("/dev/vda", 2) == "/dev/vda2"
    assert partition_path("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"
    assert partition_path("/dev/mmcblk0", 1) == "/dev/mmcblk0p1"
    assert partition_path("/dev/loop3", 1) == "/dev/loop3p1"


def test_retarget_sfdisk_keeps_guids_and_sizes():
    out = retarget_sfdisk(SFDISK, "/dev/nvme0n1", "/dev/vda")
    assert "device: /dev/vda\n" in out and "/dev/vda1 : start=        2048" in out and "/dev/vda3 :" in out
    assert "nvme0n1" not in out
    assert "label-id: 2AC01B6E-E4F8-4153-AEA7-48BEDE2CE30B" in out
    assert "uuid=04492FD8-7696-47D5-AB26-38A34FFC6E35" in out and "size=   996212736" in out


def test_recovery_data_parsing(rec_dir):
    rec = RecoveryData.from_dir(rec_dir)
    assert rec.root_disk().kname == "nvme0n1" and rec.efi_label == "debian"
    assert rec.pvs["/dev/mapper/nvme0n1p3_crypt"] == ("vg", "PV-UUID-1234")
    assert set(rec.luks_headers) == {"nvme0n1p3"} and set(rec.vg_files) == {"vg"}


def test_missing_recovery_data_gives_clear_error(tmp_path):
    with pytest.raises(RescueError, match="keine Wiederherstellungsdaten"):
        RecoveryData.from_dir(tmp_path)


def test_full_layout_plan_order_and_uuids(rec_dir):
    plan = build_layout_plan(RecoveryData.from_dir(rec_dir), "/dev/vda", passphrase="geheim", target_size=DISK_SIZE)
    order = ["leeren", "Partitionstabelle", "LUKS-Header", "öffnen", "LVM-Volume", "Volume Group vg wiederherstellen",
             "aktivieren", "Dateisystem ext4 anlegen auf /dev/mapper/vg--root", "/ einhängen", "/boot einhängen",
             "/boot/efi einhängen"]
    pos = [idx(plan, f) for f in order]
    assert pos == sorted(pos), titles(plan)  # richtige Reihenfolge
    by_title = {s.title: s for s in plan.steps}
    assert by_title["Partitionstabelle anlegen"].stdin.startswith("label: gpt\nlabel-id: 2AC01B6E")
    assert ["pvcreate", "-ff", "-y", "--uuid", "PV-UUID-1234", "--restorefile"] == next(
        s.argv[:6] for s in plan.steps if s.argv[0] == "pvcreate")
    mk = {s.argv[0]: s.argv for s in plan.steps if s.argv[0].startswith("mk")}
    assert mk["mkfs.ext4"][-3:] == ["-U", "99999999-8888-7777-6666-555555555555", "/dev/mapper/vg--root"] or \
        "99999999-8888-7777-6666-555555555555" in mk["mkfs.ext4"]
    assert mk["mkfs.vfat"][mk["mkfs.vfat"].index("-i") + 1] == "19AAE77B"  # FAT-Seriennummer aus der UUID
    assert "77777777-6666-5555-4444-333333333333" in mk["mkswap"]
    assert mk["mkfs.vfat"][-1] == "/dev/vda1"  # Partitionen der Ziel-Platte


def test_passphrase_only_in_stdin_never_in_argv_or_titles(rec_dir):
    plan = build_layout_plan(RecoveryData.from_dir(rec_dir), "/dev/vda", passphrase="supergeheim")
    assert not any("supergeheim" in " ".join(s.argv) or "supergeheim" in s.title for s in plan.steps)
    assert [s.stdin for s in plan.steps if s.stdin == "supergeheim"] == ["supergeheim"]
    assert "--key-file=-" in next(s.argv for s in plan.steps if s.argv[:2] == ["cryptsetup", "open"])


def test_mount_order_root_first_and_target_paths(rec_dir):
    plan = build_layout_plan(RecoveryData.from_dir(rec_dir), "/dev/vda", "x", mount_root="/mnt/ziel")
    mounts = [s.argv[4:] for s in plan.steps if s.title.endswith("einhängen") or "einhängen (" in s.title]
    assert [m[0] for m in mounts] == ["/mnt/ziel", "/mnt/ziel/boot", "/mnt/ziel/boot/efi"]
    assert mounts[0][1] == "/dev/mapper/vg--root" and mounts[2][1] == "/dev/vda1"


def test_cleanup_reverse_order(rec_dir):
    plan = build_layout_plan(RecoveryData.from_dir(rec_dir), "/dev/vda", "x")
    c = [s.argv[0:2] for s in plan.cleanup]
    cmds = [" ".join(s.argv) for s in plan.cleanup]
    assert "umount -R" in cmds[0] and "vgchange -an" in cmds[1] and "cryptsetup close" in cmds[2]  # Aushängen, VG, LUKS
    assert all("for i in 1 2 3 4 5" in c for c in cmds[1:])  # kurze Wiederholungen, falls das Gerät noch kurz belegt ist


def test_errors(rec_dir, tmp_path):
    rec = RecoveryData.from_dir(rec_dir)
    with pytest.raises(RescueError, match="zu klein"):
        build_layout_plan(rec, "/dev/vda", "x", target_size=DISK_SIZE - 1)
    with pytest.raises(RescueError, match="Passphrase"):
        build_layout_plan(rec, "/dev/vda", None)
    (rec_dir / "luks-header-nvme0n1p3.img").unlink()
    with pytest.raises(RescueError, match="LUKS-Header .* fehlt"):
        build_layout_plan(RecoveryData.from_dir(rec_dir), "/dev/vda", "x")


def test_plain_layout_without_luks_and_lvm():
    lsblk = {"blockdevices": [{"name": "sda", "kname": "sda", "path": "/dev/sda", "size": 10 * GIB, "type": "disk",
                               "children": [{"name": "sda1", "kname": "sda1", "path": "/dev/sda1", "type": "part",
                                             "fstype": "xfs", "uuid": "u-1", "label": "ROOT",
                                             "mountpoints": ["/"]}]}]}
    rec = RecoveryData([Node.from_json(n) for n in lsblk["blockdevices"]], {}, {"sda": "label: dos\ndevice: /dev/sda\n\n/dev/sda1 : start=2048, size=100, type=83\n"},
                        {}, {}, {})
    plan = build_layout_plan(rec, "/dev/vdb", None)
    assert any(s.argv[0] == "mkfs.xfs" and "uuid=u-1" in s.argv and s.argv[-1] == "/dev/vdb1" for s in plan.steps)
    assert not any(s.argv[0] in ("cryptsetup", "pvcreate") for s in plan.steps)


def test_unsupported_filesystem_is_warned_not_fatal(rec_dir):
    lsblk = json.loads((rec_dir / "lsblk.json").read_text())
    lsblk["blockdevices"][0]["children"][1]["fstype"] = "ntfs"
    (rec_dir / "lsblk.json").write_text(json.dumps(lsblk))
    plan = build_layout_plan(RecoveryData.from_dir(rec_dir), "/dev/vda", "x")
    assert any("ntfs" in w for w in plan.warnings)


def test_restore_and_finish_steps(rec_dir):
    st = restore_step("proxmox-backup-client", "host/pc/2026-10-18T12:00:00Z", "root.pxar", "/mnt/target",
                      namespace="ns1", keyfile="/media/key.json")
    assert st.argv[:4] == ["proxmox-backup-client", "restore", "host/pc/2026-10-18T12:00:00Z", "root.pxar"]
    assert "--allow-existing-dirs" in st.argv and st.argv[st.argv.index("--ns") + 1] == "ns1"
    assert st.argv[st.argv.index("--keyfile") + 1] == "/media/key.json"
    rec = RecoveryData.from_dir(rec_dir)
    uefi = [" ".join(s.argv) for s in finish_steps(rec, "/mnt/target", "/dev/vda", uefi=True)]
    assert any("update-initramfs -u -k all" in c for c in uefi)
    assert any("--bootloader-id=debian" in c for c in uefi) and any("--removable" in c for c in uefi)
    assert uefi[-1].endswith("update-grub")
    assert all(" env LC_ALL=C " in c for c in uefi if c.startswith("chroot"))             # keine Locale-Warnungen
    assert uefi[0].startswith("sh -c") and "mkdir -p proc sys dev run tmp var/tmp" in uefi[0]  # vom Backup ausgeschlossen
    assert "chmod 1777 tmp var/tmp" in uefi[0]
    bios = [" ".join(s.argv) for s in finish_steps(rec, "/mnt/target", "/dev/vda", uefi=False)]
    assert any(c.endswith("grub-install --recheck /dev/vda") for c in bios) and not any("efi" in c for c in bios)


class FakePopen:
    def __init__(self, fail_on=None):
        self.calls, self.stdin_seen, self.fail_on = [], {}, fail_on

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        me = self
        code = 7 if (self.fail_on and argv[0] == self.fail_on) else 0

        class P:
            stdout = iter([f"out {argv[0]}\n"])
            stdin = type("S", (), {"write": lambda s, t: me.stdin_seen.__setitem__(argv[0], t), "close": lambda s: None})()
            def wait(self): return code
        return P()


def test_run_steps_executes_stops_on_failure_and_hides_secret():
    steps = [Step("eins", ["a"]), Step("geheim", ["b"], stdin="TOPSECRET"), Step("kaputt", ["c"]), Step("nie", ["d"])]
    fp, lines = FakePopen(fail_on="c"), []
    failed = run_steps(steps, lines.append, popen=fp)
    assert failed.title == "kaputt" and [c[0] for c in fp.calls] == ["a", "b", "c"]
    assert fp.stdin_seen["b"] == "TOPSECRET" and not any("TOPSECRET" in l for l in lines)
    assert any("Exitcode 7" in l for l in lines)


def test_run_steps_dry_run_executes_nothing():
    fp, lines = FakePopen(), []
    assert run_steps([Step("x", ["wipefs", "-a", "/dev/vda"])], lines.append, dry_run=True, popen=fp) is None
    assert fp.calls == [] and lines == ["==> x", "    wipefs -a /dev/vda"]


def test_ok_codes_allow_cleanup_tolerance():
    fp = FakePopen(fail_on="c")
    assert run_steps([Step("c", ["c"], ok_codes=(0, 7))], lambda _l: None, popen=fp) is None


# -- Gesamtablauf ---------------------------------------------------------------------
from pbsgui.core.rescue import RescueConfig, candidate_disks, execute, list_full_snapshots, pick_snapshot, validate_target  # noqa: E402

LSBLK_DISKS = json.dumps({"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 500, "type": "disk", "model": "SSD", "tran": "nvme", "rm": False,
     "mountpoints": [None], "children": [{"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part",
                                          "mountpoints": ["/boot/efi"]}]},
    {"name": "sda", "path": "/dev/sda", "size": 1000, "type": "disk", "model": "Leer ", "tran": "sata", "rm": False,
     "mountpoints": [None]},
    {"name": "sdb", "path": "/dev/sdb", "size": 8, "type": "disk", "model": "USB", "tran": "usb", "rm": True,
     "mountpoints": [None]},
    {"name": "loop0", "path": "/dev/loop0", "size": 1, "type": "loop", "mountpoints": ["/snap/x"]},
    {"name": "sr0", "path": "/dev/sr0", "size": 1, "type": "rom", "mountpoints": [None]}]})


def test_candidate_disks_excludes_mounted_loop_and_rom():
    paths = [d["path"] for d in candidate_disks(LSBLK_DISKS)]
    assert paths == ["/dev/sda", "/dev/sdb"]  # nvme0n1 ist eingehängt (läuft gerade), loop/rom nie
    assert {d["path"]: d["removable"] for d in candidate_disks(LSBLK_DISKS)}["/dev/sdb"] is True


def test_validate_target(rec_dir):
    rec = RecoveryData.from_dir(rec_dir)
    disks = candidate_disks(LSBLK_DISKS)
    assert validate_target("/dev/sda", rec, disks) == 1000
    with pytest.raises(RescueError, match="keine zulässige Ziel-Platte"):
        validate_target("/dev/nvme0n1", rec, disks)  # läuft gerade: darf nie überschrieben werden


@pytest.fixture
def rescue_cfg(fake_env, tmp_path):
    return RescueConfig(server="pbs.example", datastore="store1", auth_id="u@pbs!t", token="geheim", namespace="ns1",
                        backup_id="pc", client=str(__import__("pathlib").Path(__file__).parent / "fake_pbs_client.py"),
                        target_disk="/dev/vda", uefi=True)  # fake_env setzt zusätzlich PBSGUI_CLIENT


def put_snapshots(fake_env, items):
    fake_env.mkdir(parents=True, exist_ok=True)
    (fake_env / "snapshots.json").write_text(json.dumps(items))


def snap(t, bid="pc", files=("root.pxar.didx", "recovery.pxar.didx", "catalog.pcat1.didx")):
    return {"backup-type": "host", "backup-id": bid, "backup-time": t, "size": 1, "files": [{"filename": f} for f in files]}


def test_list_and_pick_snapshot(fake_env, rescue_cfg):
    put_snapshots(fake_env, [snap(100), snap(300), snap(200, files=("root.pxar.didx",)),   # ohne recovery.pxar
                             snap(400, bid="anderer-pc")])
    assert [s.backup_time for s in list_full_snapshots(rescue_cfg)] == [300, 100]
    assert pick_snapshot(rescue_cfg).backup_time == 300
    rescue_cfg.snapshot = "host/pc/1970-01-01T00:01:40Z"
    assert pick_snapshot(rescue_cfg).backup_time == 100
    rescue_cfg.snapshot = "host/pc/1999-01-01T00:00:00Z"
    with pytest.raises(RescueError, match="nicht gefunden"):
        pick_snapshot(rescue_cfg)


def test_pick_snapshot_without_recovery_data_is_a_clear_error(fake_env, rescue_cfg):
    put_snapshots(fake_env, [snap(100, files=("root.pxar.didx",))])
    with pytest.raises(RescueError, match="Vollbackup mit Wiederherstellungsdaten"):
        pick_snapshot(rescue_cfg)


def test_execute_runs_everything_in_order_and_always_cleans_up(rec_dir, rescue_cfg):
    rec = RecoveryData.from_dir(rec_dir)
    plan = build_layout_plan(rec, "/dev/vda", "pw")
    fp, lines = FakePopen(), []
    class S:  # minimaler Snapshot
        path = "host/pc/2026-10-18T12:00:00Z"
    assert execute(rescue_cfg, S(), rec, plan, lines.append, popen=fp) is True
    names = [c[0] for c in fp.calls]
    umount_at = next(i for i, c in enumerate(fp.calls) if "umount -R" in " ".join(c))
    assert names.index("wipefs") < names.index(rescue_cfg.client) < names.index("chroot") < umount_at
    assert any("update-grub" in " ".join(c) for c in fp.calls)
    assert lines[-1].startswith("Wiederherstellung abgeschlossen")


def test_execute_failure_stops_but_still_cleans_up(rec_dir, rescue_cfg):
    rec = RecoveryData.from_dir(rec_dir)
    plan = build_layout_plan(rec, "/dev/vda", "pw")
    fp, lines = FakePopen(fail_on="pvcreate"), []
    class S:
        path = "host/pc/x"
    assert execute(rescue_cfg, S(), rec, plan, lines.append, popen=fp) is False
    names = [c[0] for c in fp.calls]
    assert rescue_cfg.client not in names and "chroot" not in names     # nichts wurde zurückgespielt
    joined = [" ".join(c) for c in fp.calls]
    assert any("umount -R" in j for j in joined) and any("vgchange -an" in j for j in joined)  # aufgeräumt
    assert not any("Wiederherstellung abgeschlossen" in l for l in lines)


def test_execute_passes_pbs_env_only_to_restore_step(rec_dir, rescue_cfg):
    rec = RecoveryData.from_dir(rec_dir)
    plan = build_layout_plan(rec, "/dev/vda", "pw")
    seen = {}
    class P(FakePopen):
        def __call__(self, argv, **kw):
            seen[argv[0]] = kw["env"]
            return super().__call__(argv, **kw)
    execute(rescue_cfg, type("S", (), {"path": "host/pc/x"})(), rec, plan, lambda _l: None, popen=P())
    assert seen[rescue_cfg.client]["PBS_PASSWORD"] == "geheim" and "PBS_FINGERPRINT" not in seen[rescue_cfg.client]
    assert seen[rescue_cfg.client]["PBS_REPOSITORY"] == "u@pbs!t@pbs.example:8007:store1"


def test_dry_run_changes_nothing(rec_dir, rescue_cfg):
    rec = RecoveryData.from_dir(rec_dir)
    plan = build_layout_plan(rec, "/dev/vda", "pw")
    fp, lines = FakePopen(), []
    assert execute(rescue_cfg, type("S", (), {"path": "host/pc/x"})(), rec, plan, lines.append, dry_run=True, popen=fp)
    assert fp.calls == [] and any("wipefs" in l for l in lines)


def test_config_from_json_ignores_secrets(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"server": "s", "datastore": "d", "auth_id": "a", "namespace": "n", "token": "SOLL-IGNORIERT",
                             "passphrase": "AUCH-NICHT", "unbekannt": 1}))
    cfg = RescueConfig.from_json(f)
    assert (cfg.server, cfg.namespace, cfg.token, cfg.passphrase) == ("s", "n", "", "")


from pbsgui.core.rescue import find_key_files, mount_removable  # noqa: E402


def test_find_key_files_recognizes_only_pbs_keys(tmp_path):
    usb = tmp_path / "usb" / "backup"
    usb.mkdir(parents=True)
    (usb / "laptop-key.json").write_text(json.dumps({"kdf": None, "data": "x", "fingerprint": "aa:bb"}))
    (usb / "anderes.json").write_text(json.dumps({"hallo": 1}))
    (usb / "kaputt.json").write_text("{nicht json")
    (usb / "gross.json").write_text(json.dumps({"kdf": None, "fingerprint": "x", "pad": "y" * 9000}))
    deep = usb / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "zu-tief.json").write_text(json.dumps({"kdf": None, "fingerprint": "x"}))
    assert find_key_files([str(tmp_path / "usb")]) == [str(usb / "laptop-key.json")]
    assert find_key_files([str(tmp_path / "gibt-es-nicht")]) == []


def test_mount_removable_mounts_only_unmounted_partitions_readonly(tmp_path):
    lsblk = json.dumps({"blockdevices": [
        {"name": "sda", "path": "/dev/sda", "type": "disk", "children": [
            {"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "ext4", "mountpoints": ["/"]},
            {"name": "sda2", "path": "/dev/sda2", "type": "part", "fstype": "crypto_LUKS", "mountpoints": [None]}]},
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "children": [
            {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "vfat", "mountpoints": [None]}]}]})
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, lsblk if argv[0] == "lsblk" else "", "")

    got = mount_removable(run, base=str(tmp_path / "pbs"))
    assert got == [str(tmp_path / "pbs/sdb1")]
    assert [c for c in calls if c[0] == "mount"] == [["mount", "-o", "ro", "/dev/sdb1", str(tmp_path / "pbs/sdb1")]]


def test_efi_label_comes_from_boot_path_not_firmware_entries(rec_dir):
    (rec_dir / "efibootmgr.txt").write_text(
        "BootCurrent: 0001\nBoot0000* UiApp\tFvVol(7cb8bdc9)/FvFile(462caa21)\n"
        "Boot0001* Ubuntu\tHD(1,GPT,x)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
        "Boot0002* UEFI QEMU\tPciRoot(0x0)/Sata(0,0,0)\n")
    assert RecoveryData.from_dir(rec_dir).efi_label == "ubuntu"
    (rec_dir / "efibootmgr.txt").write_text("Boot0000* UiApp\tFvVol(7cb8bdc9)\nBoot0001* Windows\tHD(1)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n")
    assert RecoveryData.from_dir(rec_dir).efi_label == "debian"   # kein passender Eintrag: Standard


def test_dry_run_message_does_not_claim_restore(rec_dir, rescue_cfg):
    rec = RecoveryData.from_dir(rec_dir)
    lines = []
    execute(rescue_cfg, type("S", (), {"path": "host/pc/x"})(), rec, build_layout_plan(rec, "/dev/vda", "pw"),
            lines.append, dry_run=True, popen=FakePopen())
    assert lines[-1] == "Trockenlauf beendet: es wurde nichts verändert."
