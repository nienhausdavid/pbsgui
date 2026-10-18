"""Wiederherstellung auf einem neuen PC: Datenträger-Metadaten sammeln und ein Notfallblatt erzeugen.

Ein Vollbackup sichert Dateien. Damit sich daraus ein ganzes System neu aufbauen lässt, braucht man zusätzlich
Partitionstabelle, LUKS-/LVM-Metadaten, UUIDs und Bootloader-Einträge. Diese Daten werden vor jedem
Vollbackup nach /var/lib/pbsgui/recovery/<profil>/ geschrieben und sind damit im Backup selbst enthalten.
Das Notfallblatt enthält nie Geheimnisse (weder Token-Secret noch Schlüssel).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .modes import FULL
from .models import Profile
from .scheduler import SchedulerError


class RecoveryError(SchedulerError):
    pass


@dataclass
class Collected:
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def needs_key_export(profile: Profile) -> bool:
    """Vollbackup mit automatisch erzeugtem Key: ohne exportierte Kopie wäre das Backup auf einem neuen PC
    nicht lesbar (der Key im Geheimnis-Speicher ist an den alten Host gebunden)."""
    return profile.backup_mode == FULL and profile.encrypt and not profile.keyfile and not profile.key_exported_at


def ensure_recoverable(profile: Profile) -> None:
    if needs_key_export(profile):
        raise RecoveryError(
            "Vollbackup gesperrt: Der Verschlüsselungs-Schlüssel wurde noch nicht exportiert. Ohne Kopie ließe sich "
            "das System auf einem neuen PC nicht wiederherstellen. Jetzt sichern: "
            f"pbsgui key export {profile.name} <ZIELDATEI>")


REQUIRED_TOOLS = {"lsblk": "util-linux", "blkid": "util-linux", "sfdisk": "fdisk"}


def missing_recovery_tools(which: Callable[[str], str | None] = shutil.which) -> dict[str, str]:
    """Werkzeuge, ohne die ein Vollbackup nicht auf einem neuen PC wiederherstellbar ist: Name -> Debian-Paket.

    Gefunden im VM-Test: Ein minimal installiertes Debian hat kein sfdisk (Paket fdisk); dann fehlt im Backup die
    Partitionstabelle. Prüfung auch in /usr/sbin, falls der PATH sie nicht enthält.
    """
    def have(tool: str) -> bool:
        return bool(which(tool) or any(Path(d, tool).exists() for d in ("/usr/sbin", "/sbin", "/usr/bin")))
    return {t: pkg for t, pkg in REQUIRED_TOOLS.items() if not have(t)}


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("pbsgui")
    except Exception:
        return "unbekannt"


def collect_recovery(dest: Path, profile: Profile, run: Callable | None = None,
                     which: Callable[[str], str | None] = shutil.which) -> Collected:
    """Sammelt Datenträger-Metadaten (best effort; viele Befehle brauchen root). Fehlendes wird als Warnung vermerkt."""
    run = run or subprocess.run
    out = Collected()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)

    def note(msg: str) -> None:
        out.warnings.append(msg)

    def sh(argv: list[str], outfile: str | None = None, artifact: str | None = None) -> str | None:
        """Führt einen Befehl aus. outfile: stdout dorthin; artifact: Datei, die der Befehl selbst erzeugt."""
        if not which(argv[0]):
            hint = f" (Paket {REQUIRED_TOOLS[argv[0]]})" if argv[0] in REQUIRED_TOOLS else ""
            note(f"{argv[0]} nicht installiert{hint}" + (": OHNE sie ist keine Wiederherstellung auf einem neuen PC "
                                                        "möglich" if argv[0] in REQUIRED_TOOLS else ""))
            return None
        try:
            r = run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            note(f"{' '.join(argv[:2])}: {e}")
            return None
        if r.returncode != 0:
            note(f"{' '.join(argv[:2])} fehlgeschlagen: {(r.stderr or r.stdout).strip()[:160]}")
            return None
        if outfile:
            (dest / outfile).write_text(r.stdout)
            out.files.append(outfile)
        if artifact and (dest / artifact).exists():
            os.chmod(dest / artifact, 0o600)
            out.files.append(artifact)
        return r.stdout

    lsblk = sh(["lsblk", "-J", "-b", "-o", "NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,UUID,PARTUUID,LABEL,MOUNTPOINTS,PKNAME,"
                "MODEL,TRAN"], "lsblk.json")
    devices: list[dict] = []

    def walk(nodes):
        for n in nodes or []:
            devices.append(n)
            walk(n.get("children"))
    try:
        walk(json.loads(lsblk or "{}").get("blockdevices"))
    except ValueError:
        note("lsblk lieferte kein gültiges JSON")

    for d in (n for n in devices if n.get("type") == "disk"):
        path, name = d.get("path") or f"/dev/{d['name']}", d["kname"]
        sh(["sfdisk", "--dump", path], f"partition-table-{name}.sfdisk")
        sh(["sgdisk", f"--backup={dest / f'gpt-{name}.bin'}", path], artifact=f"gpt-{name}.bin")

    sh(["blkid"], "blkid.txt")
    sh(["findmnt", "-J"], "findmnt.json")
    sh(["efibootmgr", "-v"], "efibootmgr.txt")
    sh(["mdadm", "--detail", "--scan"], "mdadm.conf.scan")
    sh(["dpkg", "--get-selections"], "dpkg-selections.txt")
    sh(["pvs", "--reportformat", "json", "-o", "+pv_uuid,vg_uuid"], "lvm-pvs.json")
    sh(["vgs", "--reportformat", "json", "-o", "+vg_uuid"], "lvm-vgs.json")
    sh(["lvs", "--reportformat", "json", "-o", "+lv_uuid"], "lvm-lvs.json")
    if which("vgcfgbackup"):
        sh(["vgcfgbackup", "-f", str(dest / "lvm-%s.vg")])  # eine Datei pro Volume Group
        out.files += sorted(p.name for p in dest.glob("lvm-*.vg"))
    try:
        shutil.copyfile("/proc/self/mountinfo", dest / "mountinfo.txt")
        out.files.append("mountinfo.txt")
    except OSError as e:
        note(f"mountinfo: {e}")

    luks = [n for n in devices if n.get("fstype") == "crypto_LUKS"]
    if luks and not profile.encrypt:
        # Der Header enthält die Schlüssel-Slots: nur in ein verschlüsseltes Backup legen
        note("LUKS-Header NICHT gesichert: das Backup ist unverschlüsselt")
    elif luks:
        for n in luks:
            sh(["cryptsetup", "luksHeaderBackup", n.get("path") or f"/dev/{n['name']}", "--header-backup-file",
                str(dest / f"luks-header-{n['kname']}.img")], artifact=f"luks-header-{n['kname']}.img")

    manifest = {
        "erstellt": time.strftime("%Y-%m-%d %H:%M:%S"), "hostname": socket.gethostname(),
        "kernel": platform.release(), "architektur": platform.machine(),
        "firmware": "uefi" if Path("/sys/firmware/efi").exists() else "bios",
        "pbsgui": _version(), "dateien": out.files, "warnungen": out.warnings,
        "profil": {"server": profile.server, "port": profile.port, "datastore": profile.datastore,
                   "namespace": profile.namespace, "backup_id": profile.backup_id, "backup_mode": profile.backup_mode,
                   "full_mounts": profile.full_mounts, "encrypt": profile.encrypt,
                   "key_fingerprint": profile.key_fingerprint},
    }
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    (dest / "NOTFALLBLATT.txt").write_text(render_sheet(profile))
    out.files += ["MANIFEST.json", "NOTFALLBLATT.txt"]
    return out


def render_sheet(profile: Profile) -> str:
    """Notfallblatt zum Ausdrucken. Enthält bewusst KEIN Token-Secret und keinen Schlüssel."""
    ns = f" --ns {profile.namespace}" if profile.namespace else ""
    repo = profile.repository
    key = (f"Schlüssel-Fingerprint: {profile.key_fingerprint}\n"
           "Schlüsseldatei:        EXTERN AUFBEWAHREN (Passwortmanager/USB-Stick), ohne sie ist das Backup unlesbar!"
           if profile.encrypt else "Verschlüsselung:       aus (Backup liegt unverschlüsselt auf dem Server)")
    keyopt = " --keyfile /pfad/zum/schluessel.json" if profile.encrypt else ""
    return f"""NOTFALLBLATT – pbsgui Vollbackup '{profile.name}'
Erstellt: {time.strftime('%Y-%m-%d %H:%M')}   (enthält KEINE Geheimnisse)

SERVER
  Adresse:     {profile.server}:{profile.port}
  Fingerprint: {profile.fingerprint}
  Datastore:   {profile.datastore}
  Namespace:   {profile.namespace or '(Wurzel)'}
  Backup-ID:   host/{profile.backup_id}
  Zugang:      {profile.auth_id}
  Token-Secret: NICHT hier. Im Passwortmanager oder in PBS neu erzeugen (Benutzer/Token verwalten).

VERSCHLÜSSELUNG
  {key.replace(chr(10), chr(10) + '  ')}

WIEDERHERSTELLUNG VON HAND (aus einem Live-System mit proxmox-backup-client)
  1. Ziel-Platte vorbereiten: Partitionen, ggf. LUKS/LVM und Dateisysteme anlegen. Die Vorlage dazu liegt im Backup
     unter /var/lib/pbsgui/recovery/{profile.slug}/ (partition-table-*.sfdisk, lvm-*.vg, luks-header-*.img, blkid.txt).
  2. Zielsystem unter /mnt/ziel einhängen (Root, dann /boot, /boot/efi usw.).
  3. Zugang setzen und Snapshots ansehen:
       export PBS_REPOSITORY='{repo}'
       export PBS_FINGERPRINT='{profile.fingerprint}'
       export PBS_PASSWORD='<Token-Secret>'
       proxmox-backup-client snapshot list{ns}
  4. Wiederherstellen (Archiv des Vollbackups heißt root.pxar):
       proxmox-backup-client restore host/{profile.backup_id}/<ZEITSTEMPEL> root.pxar /mnt/ziel{ns}{keyopt}
  5. Bootloader/Initramfs neu erzeugen (chroot): update-initramfs -u -k all; grub-install; update-grub.

HINWEISE
  - Das ist ein dateibasiertes Backup, kein Abbild. Auf anderer Hardware kann das Nachbauen nötig sein
    (Treiber, Netzwerknamen, Secure Boot, TPM-gebundene LUKS-Schlüssel gehen nicht mit).
  - Ein laufendes System wird ohne Snapshot gesichert: Datenbanken können inkonsistent sein.
"""
