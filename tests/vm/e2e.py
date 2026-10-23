#!/usr/bin/env python3
"""Ende-zu-Ende-Test in QEMU: Vollbackup eines Testsystems -> Wiederherstellung mit der Rettungs-ISO auf einer
leeren Platte -> Boot der wiederhergestellten Platte.

Phasen (einzeln oder alle):  provision  backup  restore  verify  cleanup
Voraussetzung: Quell-Image (tests/vm/build_source_image.sh) und ISO (scripts/build-iso.sh).
Kleinere Ziel-Platte: `restore_cli --shrink` (Zielgröße TARGET_SIZE in Bytes, z. B. 5 GiB bei 6-GiB-Quelle), danach
`verify --shrink` (prüft zusätzlich, dass Platte und Root-LV kleiner sind und alle UUIDs gleich blieben).
Umgebung: PBS_TEST_SECRET = Secret des Admin-Tokens root@pam!testpbsgui (nur lesend/Provisionierung im Test-Namespace).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).parent)]
from vmctl import VM  # noqa: E402
from pbsgui.core.pbs_api import ApiError, PbsApi  # noqa: E402

HOST = os.environ.get("PBS_HOST", "10.0.0.2")
FP = os.environ.get("PBS_FP", "1B:11:3C:22:BC:36:EC:C6:9A:04:A9:E7:E3:49:4F:CE:83:7C:4B:F4:BF:F0:6E:11:F6:99:C5:79:56:0B:05:6A")
DS, NS, USERHOST = "hddraid", "pbsgui-test/vm", "vmtest"
PASS = "vmtest-passphrase"
NEWPASS = "ganz-neue-passphrase-9"
WORK = Path(os.environ.get("VM_WORK", ROOT / "build/vm"))
IMAGES = ROOT / "docs/images"
STATE = WORK / "state.json"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def admin() -> PbsApi:
    api = PbsApi(HOST, 8007, FP)
    api.use_token("root@pam!testpbsgui", os.environ["PBS_TEST_SECRET"])
    return api


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(**kw) -> None:
    s = state()
    s.update(kw)
    STATE.write_text(json.dumps(s))
    os.chmod(STATE, 0o600)


def boot_and_login(vm: VM, luks: bool, timeout=300, passphrase: str = PASS) -> None:
    vm.start()
    if luks:
        vm.expect(r"Please unlock disk|Enter passphrase", timeout)
        for attempt in range(3):           # die Abfrage verwirft Eingaben, die zu früh kommen: notfalls wiederholen
            time.sleep(2)
            vm.send(passphrase + "\n")
            try:
                vm.expect(r"root@[\w-]+:[~/]\S*# ", 40)
                break
            except TimeoutError:
                if attempt == 2:
                    raise
                log("Passphrase nicht angenommen, neuer Versuch")
    else:
        vm.expect(r"root@[\w-]+:[~/]\S*# ", timeout)
    vm.send("\n")
    vm.run("true", 30)


def wait_network(vm: VM) -> None:
    for _ in range(60):
        out, _ = vm.run(f"curl -sk -o /dev/null -w '%{{http_code}}' --max-time 4 https://{HOST}:8007/", 20)
        if "200" in out:
            return
        time.sleep(2)
    raise RuntimeError("VM erreicht den PBS-Server nicht")


# -- Phasen ---------------------------------------------------------------------------------
def provision(_a) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    api = admin()
    try:
        api.deprovision(f"pbsgui-{USERHOST}@pbs", DS, NS)
    except ApiError:
        pass
    res = api.provision(DS, USERHOST, namespace=NS)
    t = PbsApi(HOST, 8007, FP)
    t.use_token(res.auth_id, res.secret)
    assert t.wait_for_backup_permission(DS, NS), "Rechte nicht wirksam"
    save(auth_id=res.auth_id, secret=res.secret)
    log(f"Test-Zugang angelegt: {res.auth_id} im Namespace {NS}")


def backup(a) -> None:
    s = state()
    img = Path(a.source_image)
    work_img = WORK / "src-run.img"
    shutil.copyfile(img, work_img)
    vm = VM("src", WORK, [work_img], mem=1024)
    try:
        boot_and_login(vm, luks=True)
        log("Quell-VM läuft")
        wait_network(vm)
        # Das minimale Testsystem hat kein sfdisk (Paket fdisk): wie vom Notfallblatt-Check empfohlen nachinstallieren
        out, rc = vm.run("apt-get install -y -qq fdisk >/dev/null 2>&1; command -v sfdisk", 300)
        assert rc == 0, "fdisk konnte nicht installiert werden"
        out, rc = vm.run(
            f"PBSGUI_TOKEN_SECRET='{s['secret']}' pbsgui add --name vm --server {HOST} --datastore {DS} "
            f"--namespace {NS} --token-id '{s['auth_id']}' --fingerprint '{FP}' --mode full --backup-id quell-pc "
            "--export-key /root/key.json", 180)
        print(out)
        assert rc == 0, "pbsgui add fehlgeschlagen"
        out, rc = vm.run("pbsgui recovery vm", 30)
        print(out)
        log("Vollbackup startet …")
        out, rc = vm.run("pbsgui backup vm", 3600)
        print(out[-2500:])
        assert rc == 0, "Backup fehlgeschlagen"
        out, _ = vm.run("base64 -w0 /root/key.json; echo", 30)
        import re
        key_b64 = re.findall(r"[A-Za-z0-9+/=]{40,}", out.split("base64 -w0", 1)[-1])[-1]
        save(key_b64=key_b64)
        out, rc = vm.run("pbsgui snapshots vm --json | head -c 600", 60)
        print(out)
        vm.run("sync", 30)
        vm.send("poweroff\n")
        try:
            vm.proc.wait(timeout=60)
        except Exception:
            pass  # Daten sind synchronisiert; der Rest wird im finally beendet
        log("Backup abgeschlossen")
    finally:
        vm.kill()
        work_img.unlink(missing_ok=True)


def restore(a) -> None:
    s = state()
    target = WORK / "restored.img"
    shutil.copyfile(Path(a.source_image).with_suffix(".empty"), target) if False else None
    target.write_bytes(b"")
    os.truncate(target, int(os.environ.get("TARGET_SIZE", 6 * 2**30)) + 100 * 2**20)
    vm = VM("rescue", WORK, [target], iso=Path(a.iso), mem=1280)
    shots = IMAGES if a.screens else WORK / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        vm.start()
        vm.expect(r"root@[\w-]+:[~/]\S*# ", 300)
        vm.send("\n")
        vm.run("true", 30)
        log("Rettungs-ISO gebootet")
        if a.screens:
            time.sleep(3)
            vm.screenshot(shots / "01-start.png")
        wait_network(vm)
        key_b64 = s["key_b64"]
        vm.run("mkdir -p /media/usb /etc/pbsgui", 10)
        vm.run(f"echo {key_b64} | base64 -d > /media/usb/key.json", 10)
        cfg = {"server": HOST, "datastore": DS, "namespace": NS, "auth_id": s["auth_id"], "fingerprint": FP,
               "backup_id": "quell-pc"}
        vm.run(f"echo '{json.dumps(cfg)}' > /etc/pbsgui/rescue.json", 10)
        out, rc = vm.run("pbsgui rescue disks", 30)
        print(out)
        # Trockenlauf per CLI (zeigt den Plan)
        out, rc = vm.run(f"PBSGUI_TOKEN_SECRET='{s['secret']}' PBSGUI_LUKS_PASSPHRASE='{PASS}' pbsgui rescue run "
                         "--disk /dev/vda --keyfile /media/usb/key.json --dry-run", 180)
        print(out[-3500:])
        assert rc == 0, "Trockenlauf fehlgeschlagen"
        # Assistent auf der ersten Konsole neu starten (jetzt mit vorausgefüllter rescue.json)
        vm.run("pkill -f '[p]bsgui rescue' || true", 10)   # den beim Boot gestarteten Assistenten beenden
        time.sleep(2)
        vm.keys("clear\npbsgui rescue\n")
        time.sleep(6)
        if a.screens:
            vm.screenshot(shots / "02-verbindung.png")
        # Der Fokus liegt automatisch auf dem ersten leeren Feld: dem Token-Secret
        vm.keys(s["secret"])
        for _ in range(3):                 # token -> fingerprint -> Fingerprint-Knopf -> Schlüsseldatei
            vm.key("tab")
        vm.keys("/media/usb/key.json")
        time.sleep(1)
        if a.screens:
            vm.screenshot(shots / "03-zugangsdaten.png")
        vm.key("tab"); vm.key("tab")       # USB-Suche -> Weiter
        vm.key("ret")
        time.sleep(8)
        if a.screens:
            vm.screenshot(shots / "04-snapshot.png")
        vm.key("tab"); vm.key("ret")       # Snapshot gewählt -> Weiter
        time.sleep(8)
        vm.key("tab")                      # -> Passphrase-Feld
        vm.keys(PASS)
        time.sleep(1)
        if a.screens:
            vm.screenshot(shots / "05-platte.png")
        vm.key("tab"); vm.key("ret")       # Weiter
        time.sleep(3)
        vm.keys("/dev/vda")
        time.sleep(1)
        if a.screens:
            vm.screenshot(shots / "06-bestaetigen.png")
        vm.key("tab"); vm.key("ret")       # Wiederherstellen
        log("Wiederherstellung per Assistent gestartet")
        t0 = time.time()
        shot_done = False
        while time.time() - t0 < 3600:
            time.sleep(12)
            if a.screens and time.time() - t0 > 20 and not shot_done:
                vm.screenshot(shots / "07-laeuft.png")
                shot_done = True
            out, _ = vm.run("tail -c 300 /proc/loadavg >/dev/null; pgrep -f 'proxmox-backup-client restore' >/dev/null && echo RUNNING || echo IDLE", 20)
            if "IDLE" in out and time.time() - t0 > 60:
                time.sleep(15)
                break
        time.sleep(5)
        if a.screens:
            vm.screenshot(shots / "08-fertig.png")
        out, _ = vm.run("lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS /dev/vda; ls /mnt/target 2>/dev/null | head", 30)
        print(out)
        vm.run("sync", 60)
        vm.send("poweroff\n")
        try:
            vm.proc.wait(timeout=60)
        except Exception:
            pass  # Daten sind synchronisiert; der Rest wird im finally beendet
        log("Wiederherstellung beendet")
    finally:
        vm.kill()


def restore_cli(a) -> None:
    """Wiederherstellung per Kommandozeile in der ISO mit gewählter Verschlüsselung (a.encryption: keep|none|new)."""
    s = state()
    target = WORK / "restored.img"
    target.write_bytes(b"")
    os.truncate(target, int(os.environ.get("TARGET_SIZE", 6 * 2**30)) + 100 * 2**20)
    vm = VM("rescue-cli", WORK, [target], iso=Path(a.iso), mem=1280)
    shots = IMAGES if a.screens else WORK / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        vm.start()
        vm.expect(r"root@[\w-]+:[~/]\S*# ", 300)
        vm.send("\n")
        vm.run("true", 30)
        wait_network(vm)
        vm.run("mkdir -p /media/usb /etc/pbsgui", 10)
        vm.run(f"echo {s['key_b64']} | base64 -d > /media/usb/key.json", 10)
        cfg = {"server": HOST, "datastore": DS, "namespace": NS, "auth_id": s["auth_id"], "fingerprint": FP,
               "backup_id": "quell-pc"}
        vm.run(f"echo '{json.dumps(cfg)}' > /etc/pbsgui/rescue.json", 10)
        if a.screens:                      # Assistent bis zur Platten-Auswahl führen und die Verschlüsselungs-Optionen zeigen
            vm.run("pkill -f '[p]bsgui rescue' || true", 10)
            time.sleep(2)
            vm.keys("clear\npbsgui rescue\n")
            time.sleep(6)
            vm.keys(s["secret"])
            for _ in range(3):
                vm.key("tab")
            vm.keys("/media/usb/key.json")
            vm.key("tab"); vm.key("tab"); vm.key("ret")
            time.sleep(8)
            vm.key("tab"); vm.key("ret")
            time.sleep(8)
            vm.key("tab")                  # Fokus von der Platten-Tabelle zu den Optionen
            vm.key("down")                 # "Neu mit LUKS verschlüsseln" markieren
            time.sleep(1)
            vm.screenshot(shots / "09-verschluesselung.png")
            vm.key("ret")
            time.sleep(1)
            vm.screenshot(shots / "10-neue-passphrase.png")
            vm.run("pkill -f '[p]bsgui rescue' || true", 10)
        env = f"PBSGUI_TOKEN_SECRET='{s['secret']}' PBSGUI_LUKS_PASSPHRASE='{PASS}' PBSGUI_NEW_LUKS_PASSPHRASE='{NEWPASS}'"
        log(f"Wiederherstellung per CLI, Verschlüsselung: {a.encryption}")
        out, rc = vm.run(f"{env} pbsgui rescue run --disk /dev/vda --keyfile /media/usb/key.json "
                         f"--encryption {a.encryption} --confirm-disk /dev/vda" + (" --shrink" if a.shrink else ""), 1800)
        print(out[-2500:])
        assert rc == 0, "Wiederherstellung fehlgeschlagen"
        vm.run("sync", 60)
        vm.send("poweroff\n")
        try:
            vm.proc.wait(timeout=240)
        except Exception:
            log("WARNUNG: Rettungs-VM fuhr nicht innerhalb von 240 s herunter")
    finally:
        vm.kill()


def verify(a) -> None:
    mode = getattr(a, "encryption", "keep")
    img = WORK / "restored.img"
    vm = VM("verify", WORK, [img], mem=1024)
    try:
        # keep: alte Passphrase; new: neue Passphrase; none: keine LUKS-Abfrage
        boot_and_login(vm, luks=mode != "none", passphrase=NEWPASS if mode == "new" else PASS)
        log(f"Wiederhergestelltes System ist gebootet (Verschlüsselung: {mode})")
        out, rc = vm.run("hostname; cat /root/marker.txt; cd / && sha256sum -c /root/checksums.txt; "
                         "readlink /home/vmuser/link.txt; stat -c '%a %u %g' /home/vmuser/data.bin; echo CRYPTTAB; "
                         "cat /etc/crypttab; echo LSBLK; "
                         "lsblk -J -b -o NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,UUID,PARTUUID,LABEL,MOUNTPOINTS /dev/vda", 120)
        print(out[-3500:])
        ok = all(x in out for x in ("quell-pc", "root/marker.txt: OK", "home/vmuser/data.bin: OK", "notiz.txt", "640 1000 1000"))
        orig = json.loads(Path(a.source_image).with_suffix(".lsblk.json").read_text())
        flat = lambda nodes: [x for n in nodes for x in [n, *flat(n.get("children", []))]]
        # Dateisystem-/PV-/LV-UUIDs (ohne LUKS-Container) und alle Partitions-GUIDs getrennt vergleichen: bei "none"
        # wandert die PARTUUID der LUKS-Partition auf den LVM-Knoten, die Menge der GUIDs bleibt gleich
        ids = lambda nodes, skip_luks: (
            sorted((n.get("fstype") or "", n.get("uuid") or "") for n in flat(nodes)
                   if n.get("uuid") and not (skip_luks and n.get("fstype") == "crypto_LUKS")),
            sorted(n["partuuid"] for n in flat(nodes) if n.get("partuuid")))
        text = out.rsplit("LSBLK", 1)[1]   # letzte Fundstelle: die erste steht im Echo des Befehls
        new = json.loads(text[text.index("{"):text.rindex("}") + 1])
        luks_new = [n for n in flat(new["blockdevices"]) if n.get("fstype") == "crypto_LUKS"]
        luks_old = [n for n in flat(orig["blockdevices"]) if n.get("fstype") == "crypto_LUKS"]
        crypttab = out.rsplit("CRYPTTAB", 1)[1].split("LSBLK", 1)[0]
        if mode == "keep":
            same = ids(orig["blockdevices"], False) == ids(new["blockdevices"], False)
            enc_ok = len(luks_new) == 1 and luks_new[0]["uuid"] == luks_old[0]["uuid"]
        elif mode == "none":
            same = ids(orig["blockdevices"], True) == ids(new["blockdevices"], True)
            enc_ok = not luks_new and "ohne Verschl" in crypttab and "wiederhergestellt" in crypttab
        else:
            same = ids(orig["blockdevices"], True) == ids(new["blockdevices"], True)
            enc_ok = (len(luks_new) == 1 and luks_new[0]["uuid"] != luks_old[0]["uuid"]
                      and f"UUID={luks_new[0]['uuid']}" in crypttab)
        print(f"Kennungen identisch (ohne LUKS-Container bei none/new): {same} | Verschlüsselung wie gewählt: {enc_ok}")
        assert ok and same and enc_ok, "Verifikation fehlgeschlagen"
        if a.shrink:      # kleinere Platte: Platte und Root-LV müssen kleiner sein als im Original
            size = lambda t: next(n["size"] for n in flat(t["blockdevices"]) if n.get("type") == "disk")
            root = lambda t: next(n["size"] for n in flat(t["blockdevices"]) if "/" in (n.get("mountpoints") or []))
            assert size(new) < size(orig) and root(new) < root(orig), \
                f"Größen nicht kleiner: Platte {size(new)} / {size(orig)}, Root {root(new)} / {root(orig)}"
            log(f"Größe angepasst: Platte {size(orig) >> 20} -> {size(new) >> 20} MiB, Root {root(orig) >> 20} -> {root(new) >> 20} MiB")
        log(f"VERIFIKATION ERFOLGREICH ({mode}): System bootet, Dateien und Kennungen stimmen, Verschlüsselung wie gewählt")
        vm.send("poweroff\n")
        try:
            vm.proc.wait(timeout=60)
        except Exception:
            pass
    finally:
        vm.kill()


def cleanup(_a) -> None:
    api = admin()
    for label, fn in (("Namespace", lambda: api._request("DELETE", f"/admin/datastore/{DS}/namespace",
                                                         {"ns": NS.split("/")[0], "delete-groups": 1})),
                      ("Benutzer+ACLs", lambda: api.deprovision(f"pbsgui-{USERHOST}@pbs", DS, NS))):
        try:
            fn()
            log(f"gelöscht: {label}")
        except ApiError as e:
            log(f"{label}: {e}")
    for name in ("src-run.img", "restored.img", "blank.img"):   # nie das Quell-Image source.img
        (WORK / name).unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["provision", "backup", "restore", "restore_cli", "verify", "cleanup", "all"])
    ap.add_argument("--source-image", default=str(WORK / "source.img"))
    ap.add_argument("--iso", default=str(next(iter(sorted((ROOT / "dist").glob("pbsgui-rescue-*.iso"))), "")))
    ap.add_argument("--screens", action="store_true", help="Screenshots nach docs/images/ schreiben")
    ap.add_argument("--shrink", action="store_true", help="restore_cli: kleinere Ziel-Platte mit Größe anpassen; verify: Größen prüfen")
    ap.add_argument("--encryption", choices=["keep", "none", "new"], default="keep")
    a = ap.parse_args()
    phases = ["provision", "backup", "restore", "verify"] if a.phase == "all" else [a.phase]
    try:
        for p in phases:
            log(f"=== Phase {p} ===")
            globals()[p](a)
    finally:
        if a.phase == "all":
            cleanup(a)


if __name__ == "__main__":
    main()
