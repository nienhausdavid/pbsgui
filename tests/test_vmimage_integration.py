"""V2P mit ECHTEN Loop-Geräten (braucht root): Abbild schreiben, Platte größer, Partition und Dateisystem wachsen,
Netzwerknamen und Gast-Dienste werden angepasst. Initramfs und Bootloader (chroot) prüft der QEMU-Test tests/vm/vm_image_e2e.py.

  sudo env PYTHONPATH=src PATH=$PATH:/usr/sbin .venv/bin/python -m pytest tests/test_vmimage_integration.py -s
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pbsgui.core import vmimage as vm
from pbsgui.core.models import Snapshot
from pbsgui.core.rescue import RescueConfig

TOOLS = ["losetup", "sfdisk", "sgdisk", "mkfs.ext4", "mkfs.vfat", "resize2fs", "e2fsck", "partprobe", "udevadm", "wipefs", "lsblk", "blkid"]
pytestmark = pytest.mark.skipif(os.geteuid() != 0 or any(shutil.which(t) is None for t in TOOLS),
                                reason="braucht root und Plattenwerkzeuge (util-linux, gdisk, e2fsprogs, dosfstools)")


def sh(*argv, input=None):
    r = subprocess.run(list(argv), input=input, capture_output=True, text=True)
    assert r.returncode == 0, f"{' '.join(argv)}: {r.stderr or r.stdout}"
    return r.stdout.strip()


def attach(img: Path) -> str:
    dev = sh("losetup", "-f", "--show", "-P", str(img))
    assert dev.startswith("/dev/loop")             # Sicherheitsnetz: nie auf echten Platten arbeiten
    sh("udevadm", "settle")
    return dev


def test_write_grow_and_repair_guest_files(tmp_path, monkeypatch):
    # 1. "VM-Platte" bauen: GPT, ESP, ext4-Root mit den Dateien eines Debian-Gastes
    src = tmp_path / "vm.img"; src.write_bytes(b""); os.truncate(src, 300 * 2**20)
    sh("sfdisk", "-q", str(src), input="label: gpt\n,32M,U\n,,L\n")
    dev = attach(src)
    try:
        sh("mkfs.vfat", "-n", "ESP", f"{dev}p1"); sh("mkfs.ext4", "-q", "-L", "root", f"{dev}p2")
        root_uuid = sh("blkid", "-s", "UUID", "-o", "value", f"{dev}p2"); esp_uuid = sh("blkid", "-s", "UUID", "-o", "value", f"{dev}p1")
        m = Path(tempfile.mkdtemp(prefix="pbsgui-t-"))
        sh("mount", f"{dev}p2", str(m))
        for rel, text in {"etc/os-release": 'ID=debian\nPRETTY_NAME="Debian"\n',
                          "etc/fstab": f"UUID={root_uuid} / ext4 defaults 0 1\nUUID={esp_uuid} /boot/efi vfat umask=0077 0 1\n",
                          "etc/network/interfaces": "auto ens3\niface ens3 inet dhcp\n",
                          "etc/cloud/cloud.cfg": "x\n", "lib/systemd/system/qemu-guest-agent.service": "[Unit]\n",
                          "etc/default/grub": 'GRUB_CMDLINE_LINUX="console=ttyS0"\n', "daten/wichtig.txt": "nicht verlieren\n"}.items():
            p = m / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        (m / "boot/efi").mkdir(parents=True); sh("umount", str(m))
    finally:
        sh("losetup", "-d", dev)
    # 2. Fake-PBS: Abbild bereitstellen, größere Ziel-Platte anlegen
    imgs = tmp_path / "pbs"; imgs.mkdir(); shutil.copy(src, imgs / "drive-scsi0.img")
    monkeypatch.setenv("FAKE_PBS_IMAGES", str(imgs)); monkeypatch.setenv("FAKE_PBS_DIR", str(tmp_path / "state"))
    target = tmp_path / "disk.img"; target.write_bytes(b""); os.truncate(target, 800 * 2**20)
    tdev = attach(target)
    try:
        cfg = RescueConfig(server="s", datastore="d", auth_id="u@pbs!t", token="x", client=str(Path(__file__).parent / "fake_pbs_client.py"))
        snap = Snapshot.from_json({"backup-type": "vm", "backup-id": "100", "backup-time": 1, "files": [
            {"filename": "drive-scsi0.img.fidx", "size": 300 * 2**20}, {"filename": "qemu-server.conf.blob", "size": 5}]})
        conf = vm.parse_conf("boot: order=scsi0\nostype: l26\nbios: ovmf\n")
        opts = vm.RepairOptions(grow=True, initramfs=False, bootloader=False, network=True, services=True)
        monkeypatch.setattr(vm, "physical_interfaces", lambda sysfs="/sys/class/net": ["enp3s0"])
        plan = vm.build_plan(cfg, snap, conf, {"scsi0": tdev}, [{"path": tdev, "size": 800 * 2**20, "model": "", "tran": "", "removable": False}],
                             opts, mount_root=str(tmp_path / "target"))
        lines: list[str] = []
        assert vm.execute(cfg, plan, lines.append) is True, "\n".join(lines)
        text = "\n".join(lines)
        assert "auf den Rest der Platte vergrößert" in text and "Dateisystem auf die volle Größe" in text and "ens3 -> enp3s0" in text
        # 3. Ergebnis prüfen: Partition und Dateisystem sind gewachsen, Daten und UUIDs unverändert
        sh("udevadm", "settle")
        sizes = sh("lsblk", "-bnr", "-o", "NAME,SIZE", tdev).splitlines()
        part2 = int(next(l.split()[1] for l in sizes if l.endswith("p2") or l.split()[0].endswith("p2")))
        assert part2 > 700 * 2**20
        assert sh("blkid", "-s", "UUID", "-o", "value", f"{tdev}p2") == root_uuid
        m = Path(tempfile.mkdtemp(prefix="pbsgui-t-"))
        sh("mount", f"{tdev}p2", str(m))
        try:
            fs_kib = int(sh("df", "--output=size", "-k", str(m)).splitlines()[1])
            assert fs_kib * 1024 > 650 * 2**20
            assert (m / "daten/wichtig.txt").read_text() == "nicht verlieren\n"
            assert "iface enp3s0 inet dhcp" in (m / "etc/network/interfaces").read_text()
            assert (m / "etc/cloud/cloud-init.disabled").exists() and (m / "etc/network/interfaces.pbsgui-orig").exists()
        finally:
            sh("umount", str(m))
        assert not (tmp_path / "target").is_dir() or not os.path.ismount(tmp_path / "target")        # aufgeräumt
    finally:
        sh("losetup", "-d", tdev)
