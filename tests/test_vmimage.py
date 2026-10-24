"""VM-Abbild-Modus (V2P): Konfiguration, Zuordnung, Plan, Reparaturen. Ohne echte Platten (die Ausführung wird mit Dateien geprüft)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from pbsgui.core import vmimage as vm
from pbsgui.core.models import Snapshot
from pbsgui.core.rescue import RescueConfig, RescueError
from test_rescue import put_snapshots

CONF = """boot: order=scsi0;ide2;net0
cores: 2
bios: ovmf
machine: q35
name: webserver
ostype: l26
scsi0: local-lvm:vm-100-disk-0,size=32G
scsi1: local-lvm:vm-100-disk-2,size=100G
efidisk0: local-lvm:vm-100-disk-1,size=4M
tpmstate0: local-lvm:vm-100-disk-3,size=4M

[vorher]
boot: order=ide2
scsi0: local-lvm:alt,size=1G
"""


def vmsnap(t=100, vmid="100", files=("drive-scsi0.img.fidx", "drive-scsi1.img.fidx", "drive-efidisk0.img.fidx",
                                     "qemu-server.conf.blob", "index.json.blob"), sizes=None):
    sizes = sizes or {"drive-scsi0.img.fidx": 32 * 2**30, "drive-scsi1.img.fidx": 100 * 2**30, "drive-efidisk0.img.fidx": 4 * 2**20}
    return {"backup-type": "vm", "backup-id": vmid, "backup-time": t, "size": sum(sizes.values()),
            "files": [{"filename": f, "size": sizes.get(f, 100)} for f in files]}


def disk(path, gib):
    return {"path": path, "size": gib * 2**30, "model": "X", "tran": "sata", "removable": False}


def test_parse_conf_reads_current_state_not_old_snapshots():
    c = vm.parse_conf(CONF)
    assert c.name == "webserver" and c.ostype == "l26" and c.uefi and c.machine == "q35"
    assert c.boot_order == ["scsi0", "ide2", "net0"]
    assert c.keys == ["scsi0", "scsi1", "efidisk0", "tpmstate0"]         # [vorher] wird ignoriert


def test_vm_disks_skip_efi_and_tpm_and_know_sizes():
    s = Snapshot.from_json(vmsnap())
    disks = vm.vm_disks(s)
    assert [(d.key, d.archive, d.image_size) for d in disks] == [("scsi0", "drive-scsi0.img", 32 * 2**30),
                                                                ("scsi1", "drive-scsi1.img", 100 * 2**30)]
    assert vm.skipped_disks(s) == ["efidisk0"]
    assert vm.boot_disk_key(vm.parse_conf(CONF), disks) == "scsi0"
    assert vm.boot_disk_key(vm.parse_conf("boot: order=virtio5\n"), disks) == "scsi0"       # Boot-Platte ohne Abbild: erste Platte


def test_list_vm_snapshots_filters_type_and_id(fake_env):
    rcfg = RescueConfig(server="pbs.example", datastore="d", auth_id="u@pbs!t", token="x", backup_id="100",
                        client=str(Path(__file__).parent / "fake_pbs_client.py"))
    put_snapshots(fake_env, [vmsnap(100), vmsnap(300), vmsnap(200, vmid="101"),
                             {"backup-type": "host", "backup-id": "100", "backup-time": 400, "files": [{"filename": "root.pxar.didx"}]},
                             vmsnap(250, files=("qemu-server.conf.blob",))])            # ohne Abbild
    assert [s.backup_time for s in vm.list_vm_snapshots(rcfg)] == [300, 100]
    rcfg.backup_id = ""
    assert len(vm.list_vm_snapshots(rcfg)) == 3


def test_fetch_config_via_client(fake_env, tmp_path, monkeypatch):
    conf = tmp_path / "c.conf"; conf.write_text(CONF)
    monkeypatch.setenv("FAKE_PBS_VMCONF", str(conf))
    rcfg = RescueConfig(server="s", datastore="d", auth_id="u@pbs!t", token="x",
                        client=str(Path(__file__).parent / "fake_pbs_client.py"))
    got = vm.fetch_config(rcfg, Snapshot.from_json(vmsnap()))
    assert got.name == "webserver" and got.boot_order[0] == "scsi0"
    monkeypatch.delenv("FAKE_PBS_VMCONF")
    with pytest.raises(RescueError, match="qemu-server.conf"):
        vm.fetch_config(rcfg, Snapshot.from_json(vmsnap()))


def test_validate_mapping_errors():
    disks = vm.vm_disks(Snapshot.from_json(vmsnap()))
    targets = [disk("/dev/sda", 64), disk("/dev/sdb", 200), disk("/dev/sdc", 10)]
    assert vm.validate_mapping(disks, {"scsi0": "/dev/sda", "scsi1": "/dev/sdb"}, targets) == {"scsi0": 64 * 2**30, "scsi1": 200 * 2**30}
    with pytest.raises(RescueError, match="zu klein"):
        vm.validate_mapping(disks, {"scsi0": "/dev/sdc"}, targets)
    with pytest.raises(RescueError, match="nur einmal"):
        vm.validate_mapping(disks, {"scsi0": "/dev/sda", "scsi1": "/dev/sda"}, targets)
    with pytest.raises(RescueError, match="zulässige"):
        vm.validate_mapping(disks, {"scsi0": "/dev/sdz"}, targets)
    with pytest.raises(RescueError, match="Unbekannte"):
        vm.validate_mapping(disks, {"efidisk0": "/dev/sda"}, targets)
    with pytest.raises(RescueError, match="Keine Ziel-Platte"):
        vm.validate_mapping(disks, {}, targets)


def plan_for(rcfg, mapping, conf=CONF, targets=None, opts=None):
    return vm.build_plan(rcfg, Snapshot.from_json(vmsnap()), vm.parse_conf(conf), mapping,
                         targets or [disk("/dev/sda", 64), disk("/dev/sdb", 200)], opts, uefi_live=True)


@pytest.fixture
def rcfg(fake_env):
    return RescueConfig(server="pbs.example", datastore="d", auth_id="u@pbs!t", token="geheim", namespace="ns",
                        client=str(Path(__file__).parent / "fake_pbs_client.py"))


def test_plan_lists_disks_repairs_and_growth(rcfg):
    p = plan_for(rcfg, {"scsi0": "/dev/sda", "scsi1": "/dev/sdb"})
    text = "\n".join(p.summary)
    assert "scsi0 (32.0 GiB) -> /dev/sda (64.0 GiB), danach vergrößert auf 64.0 GiB  [Boot-Platte]" in text
    assert "scsi1 (100.0 GiB) -> /dev/sdb (200.0 GiB)" in text and "efidisk0: nicht geschrieben" in text
    assert "Reparatur: Netzwerknamen anpassen: ja" in text
    titles = [s.title for s in p.steps]
    assert titles[0].startswith("Signaturen auf /dev/sda") and any("Abbild drive-scsi1.img" in t for t in titles)
    assert titles[-1].startswith("Initramfs und Bootloader") and p.cleanup


def test_plan_warns_when_boot_disk_not_mapped_and_for_unknown_os(rcfg):
    p = plan_for(rcfg, {"scsi1": "/dev/sdb"}, conf="boot: order=scsi0\nostype: other\n")
    assert any("Boot-Platte scsi0" in w for w in p.warnings) and any("unbekannt" in w for w in p.warnings)


@pytest.mark.parametrize("ostype", ["win11", "solaris", "w2k8"][:2])
def test_plan_refuses_windows_and_solaris(rcfg, ostype):
    with pytest.raises(RescueError, match="nicht unterstützt"):
        plan_for(rcfg, {"scsi0": "/dev/sda"}, conf=f"ostype: {ostype}\n")


def test_repair_options_can_be_switched_off(rcfg):
    off = vm.RepairOptions.from_dict({"grow": False, "network": False, "services": False, "initramfs": False, "bootloader": False})
    p = plan_for(rcfg, {"scsi0": "/dev/sda"}, opts=off)
    assert [s.title for s in p.steps][-1].startswith("Partitionstabellen")      # nur schreiben, nichts einhängen/reparieren
    assert "Reparatur: Bootloader neu: nein" in "\n".join(p.summary)


def test_dry_run_prints_every_step_and_changes_nothing(rcfg, capsys):
    p = plan_for(rcfg, {"scsi0": "/dev/sda"})
    lines = []
    assert vm.execute(rcfg, p, lines.append, dry_run=True) is True
    text = "\n".join(lines)
    assert "==> Signaturen auf /dev/sda löschen" in text and "wipefs -a /dev/sda" in text and "==> Gastsystem finden" in text


def test_execute_writes_image_to_target_file(rcfg, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"] + ":/usr/sbin:/sbin")        # wipefs liegt in sbin
    (tmp_path / "img").mkdir()
    (tmp_path / "img/drive-scsi0.img").write_bytes(b"ABCD" * 1024)
    monkeypatch.setenv("FAKE_PBS_IMAGES", str(tmp_path / "img"))
    target = tmp_path / "disk.img"
    target.write_bytes(b"\0" * 8192)
    s = Snapshot.from_json(vmsnap(files=("drive-scsi0.img.fidx", "qemu-server.conf.blob"), sizes={"drive-scsi0.img.fidx": 4096}))
    off = vm.RepairOptions(grow=False, initramfs=False, bootloader=False, network=False, services=False)
    p = vm.build_plan(rcfg, s, vm.parse_conf("boot: order=scsi0\nostype: l26\n"), {"scsi0": str(target)},
                      [{"path": str(target), "size": 8192, "model": "", "tran": "", "removable": False}], off)
    lines = []
    assert vm.execute(rcfg, p, lines.append) is True, lines
    assert target.read_bytes()[:4096] == b"ABCD" * 1024 and target.stat().st_size == 8192
    assert "geheim" not in "\n".join(lines)


def test_execute_reports_missing_archive_and_still_cleans_up(rcfg, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"] + ":/usr/sbin:/sbin")
    target = tmp_path / "disk.img"; target.write_bytes(b"\0" * 8192)
    s = Snapshot.from_json(vmsnap(files=("drive-scsi0.img.fidx",), sizes={"drive-scsi0.img.fidx": 4096}))
    off = vm.RepairOptions(False, False, False, False, False)
    p = vm.build_plan(rcfg, s, vm.parse_conf("ostype: l26\n"), {"scsi0": str(target)},
                      [{"path": str(target), "size": 8192, "model": "", "tran": "", "removable": False}], off)
    done = []
    p.cleanup = [vm.Step("Aufräumen", [], call=lambda on_line: done.append(1))]
    lines = []
    assert vm.execute(rcfg, p, lines.append) is False
    assert done == [1] and any("FEHLER" in l for l in lines)


# -- Netzwerk ------------------------------------------------------------------------------------------------------
def guest(tmp_path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_network_rewrites_single_missing_interface(tmp_path):
    root = guest(tmp_path, {"etc/network/interfaces": "auto lo\niface lo inet loopback\nauto ens3\niface ens3 inet static\n  address 10.0.0.5/24\n",
                            "etc/netplan/01.yaml": "network:\n  version: 2\n  ethernets:\n    ens3:\n      dhcp4: true\n",
                            "etc/udev/rules.d/70-persistent-net.rules": "SUBSYSTEM==\"net\", ATTR{address}==\"aa:bb\", NAME=\"ens3\"\n"})
    log = []
    changed = vm.rewrite_network(root, ["enp3s0"], log.append)
    assert sorted(changed) == ["etc/netplan/01.yaml", "etc/network/interfaces"]
    assert "iface enp3s0 inet static" in (root / "etc/network/interfaces").read_text() and "auto lo" in (root / "etc/network/interfaces").read_text()
    assert "    enp3s0:" in (root / "etc/netplan/01.yaml").read_text()
    assert (root / "etc/network/interfaces.pbsgui-orig").read_text().count("ens3") == 2         # Sicherung
    assert (root / "etc/udev/rules.d/70-persistent-net.rules.pbsgui-disabled").exists()
    assert not (root / "etc/udev/rules.d/70-persistent-net.rules").exists()


def test_network_ambiguous_or_fine_changes_nothing(tmp_path):
    root = guest(tmp_path, {"etc/network/interfaces": "auto ens3\niface ens3 inet dhcp\n"})
    log = []
    assert vm.rewrite_network(root, ["enp1s0", "enp2s0"], log.append) == []                       # zwei Karten: nicht eindeutig
    assert "ens3" in (root / "etc/network/interfaces").read_text() and any("Nicht eindeutig" in l for l in log)
    assert vm.rewrite_network(root, ["ens3"], log.append) == []                                   # Name passt schon
    assert vm.rewrite_network(root, [], log.append) == []                                         # keine echte Netzwerkkarte


def test_network_keeps_bridges_and_loopback_alone(tmp_path):
    root = guest(tmp_path, {"etc/network/interfaces": "auto lo\niface lo inet loopback\nauto vmbr0\niface vmbr0 inet dhcp\n"})
    assert vm.rewrite_network(root, ["enp3s0"]) == []


def test_physical_interfaces_only_real_hardware(tmp_path):
    for name, dev in (("lo", False), ("enp3s0", True), ("wlp2s0", True), ("docker0", False), ("veth12", False), ("virbr0", False), ("eno1", True)):
        d = tmp_path / name
        d.mkdir()
        if dev:
            (d / "device").mkdir()
    (tmp_path / "wlp2s0" / "wireless").mkdir()
    assert vm.physical_interfaces(str(tmp_path)) == ["eno1", "enp3s0"]


# -- Gast-Dienste -----------------------------------------------------------------------------------------------------
def test_disarm_services_disables_agent_and_cloud_init(tmp_path):
    root = guest(tmp_path, {"lib/systemd/system/qemu-guest-agent.service": "[Unit]\n", "etc/cloud/cloud.cfg": "x\n",
                            "etc/default/grub": 'GRUB_CMDLINE_LINUX="console=ttyS0,115200 quiet"\n'})
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    log = vm.disarm_guest_services(root, fake_run)
    assert calls == [["systemctl", f"--root={root}", "disable", "qemu-guest-agent.service"]]
    assert (root / "etc/cloud/cloud-init.disabled").exists() and any("console=ttyS0" in l for l in log)
    assert vm.disarm_guest_services(tmp_path / "leer", fake_run) == ["keine Gast-Dienste gefunden"]


def test_parse_mapping_and_fstab(tmp_path):
    assert vm.parse_mapping(["scsi0=/dev/sda", "virtio1=/dev/nvme0n1"]) == {"scsi0": "/dev/sda", "virtio1": "/dev/nvme0n1"}
    for bad in ("scsi0", "scsi0=", "=/dev/sda", "scsi0=sda"):
        with pytest.raises(RescueError):
            vm.parse_mapping([bad])
    f = tmp_path / "fstab"
    f.write_text("# comment\nUUID=abc / ext4 defaults 0 1\nUUID=def /boot/efi vfat umask=0077 0 1\n\n/dev/sdb1 /data ext4 defaults 0 2\n")
    assert vm._fstab_mounts(f) == [("UUID=abc", "/"), ("UUID=def", "/boot/efi"), ("/dev/sdb1", "/data")]


def test_chain_orders_luks_then_pv_then_lv():
    tree = [{"path": "/dev/sda", "type": "disk", "children": [
        {"path": "/dev/sda3", "type": "part", "fstype": "crypto_LUKS", "children": [
            {"path": "/dev/mapper/pbsgui-luks0", "type": "crypt", "fstype": "LVM2_member", "children": [
                {"path": "/dev/mapper/vg-root", "type": "lvm", "fstype": "ext4"}]}]}]}]
    ctx = vm._Ctx(cfg=None, snap=None, vmcfg=None, mapping={}, boot="", opts=vm.RepairOptions(), run=subprocess.run,
                  mount_root="/mnt/target", uefi_live=True, sizes={}, images={})
    ctx._lsblk = lambda: tree          # type: ignore[method-assign]
    assert ctx._chain("/dev/mapper/vg-root") == [("luks", "/dev/mapper/pbsgui-luks0"), ("pv", "/dev/mapper/pbsgui-luks0"),
                                                 ("lv", "/dev/mapper/vg-root")]


# -- Kommandozeile ----------------------------------------------------------------------------------------------------
def cli(argv, monkeypatch, fake_env, tmp_path, capsys, env=None):
    from pbsgui.cli.main import main
    monkeypatch.setenv("PBSGUI_TOKEN_SECRET", "geheim")
    monkeypatch.setenv("FAKE_PBS_DIR", str(fake_env))
    conf = tmp_path / "c.conf"; conf.write_text(CONF)
    monkeypatch.setenv("FAKE_PBS_VMCONF", str(conf))
    from pbsgui.core import rescue
    monkeypatch.setattr("pbsgui.cli.rescue_cli.candidate_disks", lambda: [disk("/dev/sda", 64), disk("/dev/sdb", 200)])
    base = ["rescue", argv[0], "--server", "pbs.example", "--datastore", "d", "--auth-id", "u@pbs!t", "--fingerprint", "AA:BB", *argv[1:]]
    rc = main(base)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_vm_snapshots_and_dry_run(fake_env, tmp_path, monkeypatch, capsys):
    put_snapshots(fake_env, [vmsnap(100), vmsnap(300), vmsnap(200, vmid="101")])
    rc, out, _ = cli(["vm-snapshots", "--vmid", "100"], monkeypatch, fake_env, tmp_path, capsys)
    assert rc == 0 and "VM 100" in out and "scsi0 32.0 GiB, scsi1 100.0 GiB" in out and "VM 101" not in out
    rc, out, err = cli(["vm-run", "--vmid", "100", "--map", "scsi0=/dev/sda", "--map", "scsi1=/dev/sdb", "--dry-run"],
                       monkeypatch, fake_env, tmp_path, capsys)
    assert rc == 0, err
    assert "VM: webserver" in out and "Firmware: UEFI" in out and "scsi0 (32.0 GiB) -> /dev/sda" in out
    assert "wipefs -a /dev/sdb" in out and "geheim" not in out + err


def test_cli_vm_run_needs_confirmation_and_mapping(fake_env, tmp_path, monkeypatch, capsys):
    put_snapshots(fake_env, [vmsnap(100)])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    rc, out, err = cli(["vm-run", "--vmid", "100", "--disk", "/dev/sda"], monkeypatch, fake_env, tmp_path, capsys)
    assert rc == 2 and "--confirm-disk /dev/sda" in err                       # nicht interaktiv, keine Bestätigung
    rc, out, err = cli(["vm-run", "--vmid", "100", "--disk", "/dev/sda", "--confirm-disk", "/dev/sdx"], monkeypatch, fake_env, tmp_path, capsys)
    assert rc == 2 and "Bestätigung stimmt nicht" in err
    rc, out, err = cli(["vm-run", "--vmid", "100", "--dry-run"], monkeypatch, fake_env, tmp_path, capsys)
    assert rc == 1 and "Ziel fehlt" in err


def test_align_crypttab_renames_the_luks_mapper_like_the_guest(tmp_path):
    root = guest(tmp_path / "gast", {"etc/crypttab": "# c\nvmcrypt UUID=abc-123 none luks\nswap /dev/vda4 /dev/urandom swap\n"})
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        out = "abc-123\n" if argv[0] == "blkid" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")
    ctx = vm._Ctx(cfg=None, snap=None, vmcfg=None, mapping={}, boot="", opts=vm.RepairOptions(), run=fake_run,
                  mount_root=str(root), uefi_live=True, sizes={}, images={})
    ctx.opened, ctx.luks = ["pbsgui-luks0"], [("pbsgui-luks0", "/dev/vda3")]
    log = []
    ctx.align_crypttab(log.append)
    assert ["dmsetup", "rename", "pbsgui-luks0", "vmcrypt"] in calls
    assert ctx.opened == ["vmcrypt"] and ctx.luks == [("vmcrypt", "/dev/vda3")] and "vmcrypt" in log[0]
    calls.clear()
    ctx.align_crypttab(log.append)                                                        # schon richtig benannt: nichts tun
    assert not any(c[0] == "dmsetup" for c in calls)


def test_network_wildcard_patterns_are_never_rewritten(tmp_path):
    root = guest(tmp_path, {"etc/systemd/network/20-wired.network": "[Match]\nName=en*\n\n[Network]\nDHCP=yes\n"})
    assert vm.rewrite_network(root, ["enp3s0"]) == []
    assert "Name=en*" in (root / "etc/systemd/network/20-wired.network").read_text()


def _grow_ctx(blkid_out: str, calls: list):
    def fake_run(argv, **kw):
        calls.append(list(argv))
        out = blkid_out if argv[0] == "blkid" else ('{"partitiontable": {"label": "gpt", "partitions": [{"node": "/dev/vdb1", "start": 2048}]}}'
                                                      if argv[:2] == ["sfdisk", "-J"] else "")
        return subprocess.CompletedProcess(argv, 0, out, "")
    ctx = vm._Ctx(cfg=None, snap=None, vmcfg=None, mapping={"scsi1": "/dev/vdb"}, boot="scsi0", opts=vm.RepairOptions(), run=fake_run,
                  mount_root="/mnt/x", uefi_live=True, sizes={"scsi1": 200 * 2**20}, images={"scsi1": vm.VmDisk("scsi1", "drive-scsi1.img", 96 * 2**20)})
    return ctx


def test_disk_without_partition_table_is_never_touched_by_sgdisk():
    """Regression (QEMU-Test): `sgdisk -e` auf eine Platte mit Dateisystem direkt darauf schreibt eine GPT über den ext4-Superblock."""
    calls: list = []
    ctx = _grow_ctx("TYPE=ext4\nUUID=abc\n", calls)
    log: list = []
    ctx.grow_partitions(log.append)
    names = [c[0] for c in calls]
    assert "sgdisk" not in names and "sfdisk" not in names
    assert ["resize2fs", "/dev/vdb"] in calls and any("direkt auf der Platte" in l for l in log)


def test_unknown_content_without_partition_table_is_left_alone():
    calls: list = []
    ctx = _grow_ctx("", calls)
    log: list = []
    ctx.grow_partitions(log.append)
    assert [c[0] for c in calls] == ["blkid"] and any("nicht vergrößert" in l for l in log)


def test_gpt_disk_is_grown_with_sgdisk_and_sfdisk():
    calls: list = []
    ctx = _grow_ctx("PTTYPE=gpt\n", calls)
    ctx.grow_partitions(lambda l: None)
    assert ["sgdisk", "-e", "/dev/vdb"] in calls and ["sfdisk", "-N", "1", "--no-reread", "-q", "/dev/vdb"] in calls
