#!/usr/bin/env python3
"""Ende-zu-Ende in QEMU/UEFI für den VM-Abbild-Modus (V2P): Proxmox-VM-Backup -> Rettungs-ISO -> größere Platten -> Boot.

Phasen:  backup  restore  verify  cleanup  (oder all)
- backup:  erzeugt aus dem Test-Image (tests/vm/build_source_image.sh) und einer kleinen Daten-Platte einen VM-Snapshot
           vm/9001 im PBS (so, wie ihn Proxmox VE ablegt: drive-scsi0.img, drive-scsi1.img, qemu-server.conf).
- restore: ISO bootet mit zwei LEEREN, größeren Platten und stellt beide per `pbsgui rescue vm-run` wieder her.
- verify:  bootet die wiederhergestellte Boot-Platte (LUKS entsperren), prüft Dateien, Datenplatte, vergrößerte Größen.
- cleanup: löscht NUR den Test-Snapshot vm/9001 (nur dieser Token, nur dieser Namespace).
Umgebung: PBSGUI_TOKEN_SECRET = Secret des Backup-Tokens (nur Rolle DatastoreBackup, Namespace DavidsLaptop).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e import FP, HOST, PASS, WORK, boot_and_login, log, wait_network  # noqa: E402
from vmctl import VM  # noqa: E402

DS, NS, AUTH, VMID = "hddraid", "DavidsLaptop", "pbsgui-LinuxLaptop@pbs!backup", "9001"
STATE = WORK / "vm-image-state.json"
CONF = f"boot: order=scsi0\ncores: 2\nmemory: 2048\nbios: ovmf\nmachine: q35\nname: testvm\nostype: l26\n" \
       f"scsi0: local-lvm:vm-{VMID}-disk-0,size=6G\nscsi1: local-lvm:vm-{VMID}-disk-2,size=96M\nefidisk0: local-lvm:vm-{VMID}-disk-1,size=4M\n"


def client_env() -> dict[str, str]:
    return {**os.environ, "PBS_REPOSITORY": f"{AUTH}@{HOST}:8007:{DS}", "PBS_PASSWORD": os.environ["PBSGUI_TOKEN_SECRET"],
            "PBS_FINGERPRINT": FP}


def pbs(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["proxmox-backup-client", *args], env=client_env(), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"proxmox-backup-client {args[0]}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def test_snapshots() -> list[dict]:
    out = pbs("snapshot", "list", "--ns", NS, "--output-format", "json").stdout
    return [s for s in json.loads(out) if s["backup-type"] == "vm" and s["backup-id"] == VMID]


def backup(a) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    data = WORK / "data.img"
    tree = WORK / "data-tree"; tree.mkdir(exist_ok=True)
    (tree / "daten.txt").write_text("Daten der zweiten Platte\n")
    data.unlink(missing_ok=True)
    subprocess.run(["mkfs.ext4", "-q", "-L", "daten", "-d", str(tree), str(data), "96M"], check=True,
                   env={**os.environ, "PATH": os.environ["PATH"] + ":/usr/sbin:/sbin"})
    (WORK / "qemu-server.conf").write_text(CONF)
    old = [s["backup-time"] for s in test_snapshots()]
    if old:
        log(f"Hinweis: es gibt schon {len(old)} Test-Snapshot(s) vm/{VMID}; der neueste (dieser Lauf) wird verwendet")
    log("Erzeuge VM-Backup vm/9001 (Boot-Platte und Datenplatte) im Namespace " + NS)
    r = pbs("backup", f"drive-scsi0.img:{a.source_image}", f"drive-scsi1.img:{data}", f"qemu-server.conf:{WORK / 'qemu-server.conf'}",
            "--backup-type", "vm", "--backup-id", VMID, "--ns", NS, "--crypt-mode", "none")
    print(r.stdout[-600:])
    snap = max(test_snapshots(), key=lambda s: s["backup-time"])
    STATE.write_text(json.dumps({"time": snap["backup-time"]}))
    log(f"Snapshot vm/{VMID}/{snap['backup-time']} liegt auf dem Server")


def new_disk(name: str, size: int) -> Path:
    p = WORK / name
    p.write_bytes(b"")
    os.truncate(p, size)
    return p


def restore(a) -> None:
    secret = os.environ["PBSGUI_TOKEN_SECRET"]
    boot_disk, data_disk = new_disk("vmimg-boot.img", 8 * 2**30), new_disk("vmimg-data.img", 160 * 2**20)
    vm = VM("vmimg-restore", WORK, [boot_disk, data_disk], iso=Path(a.iso), mem=2048)
    try:
        vm.start()
        vm.expect(r"root@[\w-]+:[~/]\S*# ", 300)
        vm.send("\n"); vm.run("true", 30)
        vm.run("pkill -f '[p]bsgui rescue' || true; unset HISTFILE", 10)
        for rel in a.patch:          # neuere Quelldateien ohne ISO-Neubau einspielen (nur zum Entwickeln)
            data = base64.b64encode((Path("src") / rel).read_bytes()).decode()
            vm.run(": > /tmp/p.b64", 10)
            for i in range(0, len(data), 1500):
                vm.run(f"printf %s {data[i:i + 1500]} >> /tmp/p.b64", 20)
            vm.run(f"base64 -d /tmp/p.b64 > /usr/lib/python3/dist-packages/{rel}; find /usr/lib/python3/dist-packages/pbsgui -name '*.pyc' -delete", 20)
            log(f"Datei eingespielt: {rel}")
        wait_network(vm)
        vm.run("stty -echo", 10)                                   # Secret nicht im Terminal-Echo (Log)
        vm.send(f"export PBSGUI_TOKEN_SECRET='{secret}'\n")
        vm.run("stty echo", 10)
        vm.run(f"export PBSGUI_LUKS_PASSPHRASE='{PASS}'", 10)
        out, _ = vm.run("pbsgui rescue vm-snapshots --server %s --datastore %s --namespace %s --auth-id '%s' --fingerprint %s --vmid %s 2>&1 | tail -3"
                        % (HOST, DS, NS, AUTH, FP, VMID), 120)
        print(out[-600:])
        assert f"VM {VMID}" in out and "scsi0" in out and "scsi1" in out, "VM-Snapshot nicht sichtbar"
        log("Wiederherstellung per `pbsgui rescue vm-run` (zwei Platten, alle Reparaturen)")
        out, rc = vm.run("pbsgui rescue vm-run --server %s --datastore %s --namespace %s --auth-id '%s' --fingerprint %s --vmid %s "
                         "--map scsi0=/dev/vda --map scsi1=/dev/vdb --confirm-disk /dev/vda --confirm-disk /dev/vdb 2>&1 | tail -120"
                         % (HOST, DS, NS, AUTH, FP, VMID), 5400)
        print(out[-7000:])
        assert "Wiederherstellung abgeschlossen" in out, "Wiederherstellung fehlgeschlagen"
        assert secret not in out, "Secret im Protokoll!"
        vm.run("sync", 60)
        vm.send("poweroff\n")
        try:
            vm.proc.wait(timeout=240)
        except Exception:
            pass
    finally:
        vm.kill()
        vm.buf = ""


def verify(a) -> None:
    vm = VM("vmimg-verify", WORK, [WORK / "vmimg-boot.img", WORK / "vmimg-data.img"], mem=2048)
    try:
        boot_and_login(vm, luks=True, passphrase=PASS)
        log("Wiederhergestelltes VM-System ist gebootet")
        out, _ = vm.run("hostname; cat /root/marker.txt; cd / && sha256sum -c /root/checksums.txt; readlink /home/vmuser/link.txt; "
                        "mkdir -p /mnt/d && mount /dev/vdb /mnt/d && cat /mnt/d/daten.txt; umount /mnt/d; echo ROOTSIZE; "
                        "lsblk -bnr -o NAME,SIZE,MOUNTPOINTS /dev/vda | grep ' /$'; echo VDA; lsblk -bndo SIZE /dev/vda; "
                        "echo SERVICES; systemctl is-enabled qemu-guest-agent 2>&1; ls /etc/cloud 2>&1 | head -2; echo NET; "
                        "cat /etc/network/interfaces 2>/dev/null | head -8; ip -brief addr", 120)
        print(out[-3000:])
        ok = all(x in out for x in ("quell-pc", "root/marker.txt: OK", "home/vmuser/data.bin: OK", "Daten der zweiten Platte"))
        orig = json.loads(Path(a.source_image).with_suffix(".lsblk.json").read_text())
        flat = lambda nodes: [x for n in nodes for x in [n, *flat(n.get("children", []))]]
        old_root = next(n["size"] for n in flat(orig["blockdevices"]) if "/" in (n.get("mountpoints") or []))
        section = out.rsplit("ROOTSIZE", 1)[1].split("VDA", 1)[0]       # letzte Fundstelle: die erste steht im Echo des Befehls
        line = next((l for l in section.splitlines() if l.rstrip().endswith(" /")), "")
        new_root = int(line.split()[1]) if len(line.split()) > 1 else 0
        log(f"Root-Dateisystem: {old_root >> 20} MiB -> {new_root >> 20} MiB")
        assert ok, "Dateien oder Datenplatte stimmen nicht"
        assert new_root > old_root + 800 * 2**20, f"Root nicht vergrößert ({old_root} -> {new_root})"
        log("VERIFIKATION ERFOLGREICH: VM-Abbild auf größere Platten wiederhergestellt, System bootet, Dateien stimmen, Root vergrößert")
        vm.send("poweroff\n")
        try:
            vm.proc.wait(timeout=60)
        except Exception:
            pass
    finally:
        vm.kill()


def cleanup(_a) -> None:
    snaps = test_snapshots()
    for s in snaps:
        from datetime import datetime, timezone
        path = f"vm/{VMID}/" + datetime.fromtimestamp(s["backup-time"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = pbs("snapshot", "forget", path, "--ns", NS, check=False)
        if r.returncode == 0:
            log(f"Test-Snapshot gelöscht: {path}")
        else:      # die Backup-Rolle darf nicht löschen (kein Datastore.Prune): nicht umgehen, sondern melden
            log(f"NICHT gelöscht (fehlende Berechtigung, bitte im PBS unter {NS} von Hand löschen): {path}")
    if not snaps:
        log("Kein Test-Snapshot vorhanden")
    STATE.unlink(missing_ok=True)
    for n in ("vmimg-boot.img", "vmimg-data.img", "data.img"):
        (WORK / n).unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["backup", "restore", "verify", "cleanup", "all"])
    ap.add_argument("--iso", required=False, default="")
    ap.add_argument("--source-image", default=str(WORK / "source.img"))
    ap.add_argument("--patch", action="append", default=[], metavar="DATEI", help="Datei unter src/ vor dem Test in die ISO kopieren")
    a = ap.parse_args()
    phases = ["backup", "restore", "verify"] if a.phase == "all" else [a.phase]
    try:
        for p in phases:
            log(f"=== Phase {p} ===")
            globals()[p](a)
    finally:
        if a.phase == "all":
            cleanup(a)


if __name__ == "__main__":
    main()
