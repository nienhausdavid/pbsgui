"""Integrationstest mit ECHTEN Loop-Geräten (braucht root): baut ein GPT/LUKS/LVM/ext4-System nach.

Läuft nur als root und nur auf Loop-Geräten (Sicherheitsprüfung unten). Aufruf:
  sudo env PYTHONPATH=src PATH=$PATH:/usr/sbin .venv/bin/python -m pytest tests/test_rescue_integration.py -s
"""
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from pbsgui.core.models import Profile
from pbsgui.core.recovery import collect_recovery
from pbsgui.core.rescue import RecoveryData, build_layout_plan, restore_step, run_steps

TOOLS = ["losetup", "sfdisk", "cryptsetup", "pvcreate", "vgcreate", "lvcreate", "mkfs.ext4", "mkfs.vfat", "mkswap",
         "partprobe", "udevadm", "wipefs"]
pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or any(shutil.which(t) is None for t in TOOLS),
    reason="braucht root und die Speicher-Werkzeuge (cryptsetup, lvm2, e2fsprogs, dosfstools, util-linux)")

PASS = "testpass-123"
SRC_MP, DST_MP = "/mnt/pbsgui-src", "/mnt/pbsgui-dst"
SIZE = 1200 * 2 ** 20


def sh(*argv, input=None, check=True):
    r = subprocess.run(list(argv), input=input, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} -> {r.returncode}: {r.stderr or r.stdout}")
    return r.stdout.strip()


def settle():
    sh("udevadm", "settle")


def loop_attach(img: Path) -> str:
    dev = sh("losetup", "-f", "--show", "-P", str(img))
    assert dev.startswith("/dev/loop"), dev  # Sicherheitsnetz: nie auf echten Platten arbeiten
    settle()
    return dev


@pytest.fixture
def lab():
    base = Path(tempfile.mkdtemp(prefix="pbsgui-it-", dir="/var/tmp"))
    state = {"loops": [], "vg": "tstvg" + uuid.uuid4().hex[:6], "map": "tstcrypt" + uuid.uuid4().hex[:6]}
    yield base, state
    # Aufräumen, egal wie der Test endete
    for mp in (DST_MP, SRC_MP):
        sh("umount", "-R", "-l", mp, check=False)
    sh("vgchange", "-an", state["vg"], check=False)
    for m in (state["map"],):
        sh("cryptsetup", "close", m, check=False)
    for dev in state["loops"]:
        sh("losetup", "-d", dev, check=False)
    shutil.rmtree(base, ignore_errors=True)
    for mp in (DST_MP, SRC_MP):
        shutil.rmtree(mp, ignore_errors=True)


def build_source(base: Path, st: dict) -> str:
    img = base / "src.img"
    img.write_bytes(b"")
    os.truncate(img, SIZE)
    dev = loop_attach(img)
    st["loops"].append(dev)
    layout = ("label: gpt\nunit: sectors\n\n"
              "start=2048, size=131072, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n"       # 64 MiB ESP
              "start=133120, size=262144, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4\n"      # 128 MiB /boot
              "type=0FC63DAF-8483-4772-8E79-3D69D8477DE4\n")                                # Rest: LUKS
    sh("sfdisk", dev, input=layout)
    sh("partprobe", dev)
    settle()
    sh("mkfs.vfat", "-F", "32", "-n", "ESP", f"{dev}p1")
    sh("mkfs.ext4", "-F", "-q", "-L", "boot", f"{dev}p2")
    sh("cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--pbkdf", "pbkdf2", "--pbkdf-force-iterations",
       "1000", "--key-file=-", f"{dev}p3", input=PASS)
    sh("cryptsetup", "open", "--key-file=-", f"{dev}p3", st["map"], input=PASS)
    sh("pvcreate", "-y", f"/dev/mapper/{st['map']}")
    sh("vgcreate", st["vg"], f"/dev/mapper/{st['map']}")
    sh("lvcreate", "-y", "-L", "500M", "-n", "root", st["vg"])
    sh("lvcreate", "-y", "-L", "64M", "-n", "swap", st["vg"])
    settle()
    sh("mkfs.ext4", "-F", "-q", "-L", "root", f"/dev/{st['vg']}/root")
    sh("mkswap", f"/dev/{st['vg']}/swap")
    return dev


def populate_and_mount(st: dict, dev: str, tree: Path):
    os.makedirs(SRC_MP, exist_ok=True)
    sh("mount", f"/dev/{st['vg']}/root", SRC_MP)
    os.makedirs(f"{SRC_MP}/boot", exist_ok=True)
    sh("mount", f"{dev}p2", f"{SRC_MP}/boot")
    os.makedirs(f"{SRC_MP}/boot/efi", exist_ok=True)
    sh("mount", f"{dev}p1", f"{SRC_MP}/boot/efi")
    Path(f"{SRC_MP}/etc").mkdir()
    Path(f"{SRC_MP}/etc/hostname").write_text("quell-pc\n")
    Path(f"{SRC_MP}/boot/vmlinuz-test").write_bytes(os.urandom(300_000))
    Path(f"{SRC_MP}/boot/efi/marker.efi").write_text("efi")
    Path(f"{SRC_MP}/home").mkdir()
    Path(f"{SRC_MP}/home/datei.bin").write_bytes(os.urandom(5_000_000))
    os.chmod(f"{SRC_MP}/home/datei.bin", 0o640)
    os.chown(f"{SRC_MP}/home/datei.bin", 1000, 1000)
    os.symlink("datei.bin", f"{SRC_MP}/home/link")
    os.link(f"{SRC_MP}/home/datei.bin", f"{SRC_MP}/home/hardlink")
    # Dateibaum für den Fake-Client: root.pxar enthält alles, auch /boot und /boot/efi
    (tree / "root.pxar").mkdir(parents=True)
    sh("cp", "-a", f"{SRC_MP}/.", str(tree / "root.pxar"))


def test_rebuild_layout_with_original_uuids_and_restore_files(lab, monkeypatch):
    base, st = lab
    tree, rec_dir = base / "tree", base / "rec"
    dev = build_source(base, st)
    populate_and_mount(st, dev, tree)
    orig = json.loads(sh("lsblk", "-J", "-b", "-o", "NAME,PATH,UUID,PARTUUID,FSTYPE", dev))

    # Wiederherstellungsdaten wie im echten Backup sammeln, aber NUR für unsere Test-Geräte
    real_run = subprocess.run

    def guarded(argv, **kw):
        argv = list(argv)
        if argv[:2] == ["cryptsetup", "luksHeaderBackup"] and not argv[2].startswith(dev):
            return subprocess.CompletedProcess(argv, 1, "", "übersprungen (nicht Testgerät)")
        if argv[0] == "vgcfgbackup":
            argv.append(st["vg"])
        return real_run(argv, **kw)

    profile = Profile(name="it", server="x", datastore="d", auth_id="a", backup_mode="full", encrypt=True)
    got = collect_recovery(rec_dir, profile, run=guarded)
    assert any(f.startswith("luks-header-") for f in got.files) and f"lvm-{st['vg']}.vg" in got.files
    # lsblk nennt ein Loop-Gerät "loop" (nicht "disk"): die Partitionstabelle legen wir für den Test selbst ab
    (rec_dir / f"partition-table-{Path(dev).name}.sfdisk").write_text(sh("sfdisk", "--dump", dev) + "\n")
    # Schutz: kein fremder LUKS-Header (z. B. vom Laptop) im Testordner
    assert all(f"luks-header-{Path(dev).name}p3.img" == f.name for f in rec_dir.glob("luks-header-*"))

    # Quelle abbauen
    sh("umount", "-R", SRC_MP)
    sh("vgchange", "-an", st["vg"])
    sh("cryptsetup", "close", st["map"])

    # Neue, leere Platte (etwas größer als das Original) und Plan ausführen
    dst_img = base / "dst.img"
    dst_img.write_bytes(b"")
    os.truncate(dst_img, SIZE + 100 * 2 ** 20)
    ddev = loop_attach(dst_img)
    st["loops"].append(ddev)
    rec = RecoveryData.from_dir(rec_dir, mount_prefix=SRC_MP, source_disk=Path(dev).name)
    assert rec.root_disk().path == dev
    plan = build_layout_plan(rec, ddev, PASS, target_size=SIZE + 100 * 2 ** 20, mount_root=DST_MP)
    lines = []
    failed = run_steps(plan.steps, lines.append)
    assert failed is None, "\n".join(lines[-15:])

    # Dateien zurückspielen mit dem Fake-Client (kopiert den Baum) und Ergebnis prüfen
    monkeypatch.setenv("FAKE_PBS_TREE", str(tree))
    monkeypatch.setenv("FAKE_PBS_DIR", str(base / "fakestate"))
    fake = str(Path(__file__).parent / "fake_pbs_client.py")
    assert run_steps([restore_step(fake, "host/it/2026-01-01T00:00:00Z", "root.pxar", DST_MP)], lines.append) is None

    # 1) Identische Kennungen: Partitionstabelle, LUKS, PV/VG, Dateisysteme
    new = json.loads(sh("lsblk", "-J", "-b", "-o", "NAME,PATH,UUID,PARTUUID,FSTYPE", ddev))
    flat = lambda t: {n["path"].split("/")[-1].replace(Path(t["blockdevices"][0]["path"]).name, "DISK"): n
                      for n in walk(t["blockdevices"])}
    def walk(nodes):
        for n in nodes:
            yield n
            yield from walk(n.get("children", []))
    o_nodes, n_nodes = flat(orig), flat(new)
    for key in ("DISKp1", "DISKp2", "DISKp3"):
        assert o_nodes[key]["partuuid"] == n_nodes[key]["partuuid"], f"PARTUUID {key}"
        assert o_nodes[key]["uuid"] == n_nodes[key]["uuid"], f"UUID {key}"          # inkl. LUKS-UUID
    for lv in ("root", "swap"):
        o = next(n for k, n in o_nodes.items() if k.endswith(f"{st['vg']}-{lv}"))
        n = next(n for k, n in n_nodes.items() if k.endswith(f"{st['vg']}-{lv}"))
        assert o["uuid"] == n["uuid"], f"LV {lv}"
    orig_vg = next(v for r in json.loads((rec_dir / "lvm-vgs.json").read_text())["report"] for v in r["vg"]
                   if v["vg_name"] == st["vg"])
    assert sh("vgs", "--noheadings", "-o", "vg_uuid", st["vg"]) == orig_vg["vg_uuid"]   # VG-UUID identisch
    orig_pv = next(v for r in json.loads((rec_dir / "lvm-pvs.json").read_text())["report"] for v in r["pv"]
                   if v["vg_name"] == st["vg"])
    assert sh("pvs", "--noheadings", "-o", "pv_uuid", f"/dev/mapper/{st['map']}") == orig_pv["pv_uuid"]
    # 2) Dateien identisch (Inhalt, Rechte, Besitzer, Links)
    assert Path(f"{DST_MP}/etc/hostname").read_text() == "quell-pc\n"
    assert Path(f"{DST_MP}/home/datei.bin").read_bytes() == Path(tree / "root.pxar/home/datei.bin").read_bytes()
    stt = os.stat(f"{DST_MP}/home/datei.bin")
    assert (stt.st_mode & 0o777, stt.st_uid, stt.st_gid) == (0o640, 1000, 1000)
    assert os.readlink(f"{DST_MP}/home/link") == "datei.bin"
    assert os.stat(f"{DST_MP}/home/hardlink").st_ino == stt.st_ino
    assert Path(f"{DST_MP}/boot/vmlinuz-test").exists() and Path(f"{DST_MP}/boot/efi/marker.efi").exists()
    # /boot und /boot/efi sind wirklich eigene Dateisysteme auf den Ziel-Partitionen
    assert sh("findmnt", "-n", "-o", "SOURCE", f"{DST_MP}/boot") == f"{ddev}p2"
    assert sh("findmnt", "-n", "-o", "SOURCE", f"{DST_MP}/boot/efi") == f"{ddev}p1"

    # 3) Aufräumen über den Plan funktioniert und lässt nichts offen
    assert run_steps(plan.cleanup, lines.append) is None
    assert subprocess.run(["findmnt", DST_MP], capture_output=True).returncode != 0
    assert not Path(f"/dev/mapper/{st['map']}").exists()
