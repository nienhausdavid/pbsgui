"""Wiederherstellung eines Vollbackups auf einer leeren Platte (läuft in der Rettungs-ISO als root).

Ablauf: Layout-Daten aus `recovery.pxar` lesen -> Partitionstabelle, LUKS, LVM und Dateisysteme mit den
ALTEN UUIDs neu anlegen (damit fstab/crypttab/GRUB gültig bleiben) -> `root.pxar` einspielen -> chroot:
Initramfs und Bootloader neu erzeugen.

Alle Befehle werden zuerst als Plan (Liste von Step) aufgebaut: prüfbar, als Vorschau anzeigbar und testbar.
Geheimnisse (LUKS-Passphrase) gehen nur über stdin und stehen nie in Titeln oder Logs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from ..i18n import tr


class RescueError(RuntimeError):
    pass


# -- Eingabedaten ----------------------------------------------------------------
@dataclass
class Node:
    """Ein Knoten aus `lsblk -J` (Platte, Partition, LUKS, LVM …)."""
    name: str
    kname: str
    path: str
    type: str
    size: int = 0
    fstype: str = ""
    uuid: str = ""
    label: str = ""
    mountpoints: list[str] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict) -> "Node":
        mps = [m for m in (d.get("mountpoints") or []) if m]
        return cls(name=d.get("name") or "", kname=d.get("kname") or d.get("name") or "",
                   path=d.get("path") or "/dev/" + (d.get("name") or ""), type=d.get("type") or "",
                   size=int(d.get("size") or 0), fstype=d.get("fstype") or "", uuid=d.get("uuid") or "",
                   label=d.get("label") or "", mountpoints=mps,
                   children=[cls.from_json(c) for c in d.get("children") or []])

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class RecoveryData:
    disks: list[Node]
    manifest: dict
    sfdisk: dict[str, str]            # kname der Platte -> sfdisk-Dump
    luks_headers: dict[str, Path]     # kname der LUKS-Partition -> Header-Backup
    vg_files: dict[str, Path]         # VG-Name -> vgcfgbackup-Datei
    pvs: dict[str, tuple[str, str]]   # PV-Pfad (alt) -> (VG-Name, PV-UUID)
    efi_label: str = "debian"
    mount_prefix: str = ""
    source_disk: str = ""   # kname der Quell-Platte erzwingen (Tests); sonst die Platte mit `/`
    usage: dict[str, int] = field(default_factory=dict)   # Einhängepunkt -> belegte Bytes (fsusage.json)

    @classmethod
    def from_dir(cls, d: Path, mount_prefix: str = "", source_disk: str = "") -> "RecoveryData":
        d = Path(d)
        try:
            lsblk = json.loads((d / "lsblk.json").read_text())
        except (OSError, ValueError):
            raise RescueError(tr("lsblk.json fehlt oder ist ungültig: das Backup enthält keine Wiederherstellungsdaten (wurde es als Vollbackup mit pbsgui ≥ 0.2 erstellt?)")) from None
        disks = [Node.from_json(n) for n in lsblk.get("blockdevices", [])
                 if n.get("type") == "disk" or (source_disk and (n.get("kname") or n.get("name")) == source_disk)]
        manifest = json.loads((d / "MANIFEST.json").read_text()) if (d / "MANIFEST.json").exists() else {}
        sfdisk = {p.name[len("partition-table-"):-len(".sfdisk")]: p.read_text()
                  for p in d.glob("partition-table-*.sfdisk")}
        luks = {p.name[len("luks-header-"):-len(".img")]: p for p in d.glob("luks-header-*.img")}
        vgs = {p.name[len("lvm-"):-len(".vg")]: p for p in d.glob("lvm-*.vg")}
        pvs: dict[str, tuple[str, str]] = {}
        try:
            for rep in json.loads((d / "lvm-pvs.json").read_text()).get("report", []):
                for pv in rep.get("pv", []):
                    pvs[pv["pv_name"]] = (pv.get("vg_name", ""), pv.get("pv_uuid", ""))
        except (OSError, ValueError):
            pass
        label = "debian"
        try:  # Verzeichnis unter \EFI\, in dem GRUB/shim lag (nicht Firmware-Einträge wie "UiApp")
            m = re.search(r"File\(\\EFI\\(?!BOOT\\)([^\\)]+)\\(?:grub|shim)", (d / "efibootmgr.txt").read_text(),
                          re.I)
            label = m.group(1) if m else label
        except OSError:
            pass
        usage: dict[str, int] = {}
        try:
            raw = json.loads((d / "fsusage.json").read_text())
            pre = mount_prefix.rstrip("/")
            for mp, v in raw.items():     # mit Test-Präfix zählen nur die Einhängepunkte unter dem Präfix
                if not pre:
                    usage[mp] = int(v["used"])
                elif mp == pre or mp.startswith(pre + "/"):
                    usage[mp[len(pre):] or "/"] = int(v["used"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return cls(disks, manifest, sfdisk, luks, vgs, pvs, label, mount_prefix.rstrip("/"), source_disk, usage)

    def real_mp(self, mp: str) -> str:
        """Einhängepunkt ohne Test-Präfix (nur für Integrationstests gesetzt)."""
        if self.mount_prefix and (mp == self.mount_prefix or mp.startswith(self.mount_prefix + "/")):
            return mp[len(self.mount_prefix):] or "/"
        return mp

    def root_disk(self) -> Node:
        if self.source_disk:
            for disk in self.disks:
                if disk.kname == self.source_disk:
                    return disk
            raise RescueError(tr("Quell-Platte {0} nicht in den Wiederherstellungsdaten", self.source_disk))
        for disk in self.disks:
            if any(self.real_mp(mp) == "/" for n in disk.walk() for mp in n.mountpoints):
                return disk
        raise RescueError(tr("Keine Platte mit dem Root-Dateisystem (/) in den Wiederherstellungsdaten gefunden"))


# -- Plan ----------------------------------------------------------------------------
@dataclass
class Step:
    title: str
    argv: list[str]
    stdin: str | None = None          # z. B. Passphrase: wird nie geloggt
    destructive: bool = False
    ok_codes: tuple[int, ...] = (0,)
    env: dict[str, str] = field(default_factory=dict)
    call: Callable[[Callable[[str], None]], None] | None = None   # interne Funktion statt externem Befehl


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    cleanup: list[Step] = field(default_factory=list)   # laufen immer, auch nach Fehlern (in Reihenfolge)
    warnings: list[str] = field(default_factory=list)
    mount_root: str = "/mnt/target"
    summary: list[str] = field(default_factory=list)
    crypttab: dict[str, str | None] = field(default_factory=dict)   # Mapper-Name -> LUKS-UUID (None = Eintrag entfernen)
    needs_cryptsetup: bool = False   # neue LUKS-Container in einem Zielsystem, das evtl. kein cryptsetup-initramfs hat
    resizes: list = field(default_factory=list)   # Größenänderungen (shrink.Resize) bei kleinerer Ziel-Platte


def partition_path(disk: str, n: int) -> str:
    return f"{disk}p{n}" if disk[-1].isdigit() else f"{disk}{n}"


def _part_number(path: str) -> int:
    m = re.search(r"(\d+)$", path)
    if not m:
        raise RescueError(tr("Partitionsnummer nicht erkennbar: {0}", path))
    return int(m.group(1))


def retarget_sfdisk(dump: str, source_disk: str, target_disk: str) -> str:
    """Passt einen `sfdisk --dump` an die Ziel-Platte an (Gerätenamen); GUIDs und Größen bleiben."""
    out = []
    for line in dump.splitlines():
        if line.startswith("device:"):
            line = f"device: {target_disk}"
        else:
            m = re.match(r"^(/dev/\S+)\s*:(.*)$", line)
            if m:
                line = f"{partition_path(target_disk, _part_number(m.group(1)))} :{m.group(2)}"
        out.append(line)
    return "\n".join(out) + "\n"


def _mkfs(node: Node, dev: str) -> Step | None:
    fs, uuid, label = node.fstype, node.uuid, node.label
    lab = lambda flag, mx: [flag, label[:mx]] if label else []
    if fs in ("ext2", "ext3", "ext4"):
        argv = [f"mkfs.{fs}", "-F", "-q", *(["-U", uuid] if uuid else []), *lab("-L", 16), dev]
    elif fs == "xfs":
        argv = ["mkfs.xfs", "-f", *(["-m", f"uuid={uuid}"] if uuid else []), *lab("-L", 12), dev]
    elif fs == "vfat":
        argv = ["mkfs.vfat", "-F", "32", *(["-i", uuid.replace("-", "")] if uuid else []), *lab("-n", 11), dev]
    elif fs == "btrfs":
        argv = ["mkfs.btrfs", "-f", *(["-U", uuid] if uuid else []), *lab("-L", 255), dev]
    elif fs == "swap":
        argv = ["mkswap", *(["-U", uuid] if uuid else []), *lab("-L", 15), dev]
    else:
        return None
    return Step(tr("Dateisystem {0} anlegen auf {1}", fs, dev) + (tr(" (UUID {0})", uuid) if uuid else ""), argv, destructive=True)


def _luks_format(dev: str, name: str, new_passphrase: str, plan: "Plan") -> str:
    """Neuer LUKS2-Container mit frischer UUID; liefert die UUID (für crypttab)."""
    import uuid as _uuid
    luks_uuid = str(_uuid.uuid4())
    plan.steps.append(Step(tr("Neuen LUKS-Container auf {0} anlegen (UUID {1})", dev, luks_uuid),
                           ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--uuid", luks_uuid,
                            "--key-file=-", dev], stdin=new_passphrase, destructive=True))
    plan.steps.append(Step(tr("LUKS {0} öffnen als {1}", dev, name),
                           ["cryptsetup", "open", "--type", "luks", "--key-file=-", dev, name], stdin=new_passphrase))
    plan.cleanup.append(Step(tr("LUKS {0} schließen", name),
                             ["sh", "-c", 'for i in 1 2 3 4 5; do cryptsetup close "$1" && exit 0; sleep 2; done; exit 5',
                              "luks-close", name], ok_codes=(0, 4, 5)))
    return luks_uuid


ENCRYPTION_MODES = ("keep", "new", "none")   # wie im Original / neu verschlüsseln / ohne Verschlüsselung


def _is_data_partition(part: "Node", rec: "RecoveryData") -> bool:
    """Partitionen, die bei neuer Verschlüsselung in LUKS gehören: nicht ESP, nicht /boot, nicht Swap-Rohpartition."""
    mps = {rec.real_mp(m) for m in part.mountpoints}
    return not (part.fstype in ("vfat", "swap", "") or mps & {"/boot", "/boot/efi"})


def build_layout_plan(rec: RecoveryData, target_disk: str, passphrase: str | None = None,
                      target_size: int | None = None, mount_root: str = "/mnt/target",
                      encryption: str = "keep", new_passphrase: str | None = None, shrink: bool = False,
                      snapshot_size: int | None = None, workdir: Path | None = None) -> Plan:
    """Schritte: Platte leeren, Partitionstabelle, LUKS/LVM/Dateisysteme mit alten UUIDs, Einhängen.

    encryption: "keep" = wie im Original (LUKS-Header und alte Passphrase), "new" = neue LUKS-Container mit
    neuer Passphrase (auch wenn das Original unverschlüsselt war), "none" = ohne Verschlüsselung.
    shrink: Ziel-Platte darf kleiner sein als das Original; Partitionen/LVs werden verkleinert (siehe core/shrink.py).
    snapshot_size: Snapshot-Größe als Schätzung des belegten Platzes, falls das Backup keine fsusage.json enthält.
    """
    if encryption not in ENCRYPTION_MODES:
        raise RescueError(tr("Unbekannte Verschlüsselungs-Option: {0}", encryption))
    if encryption == "new" and not new_passphrase:
        raise RescueError(tr("Für die neue Verschlüsselung wird eine neue Passphrase benötigt"))
    src = rec.root_disk()
    smaller = target_size is not None and target_size < src.size
    if smaller and not shrink:
        raise RescueError(tr("Ziel-Platte zu klein: {0:.1f} GiB, die Original-Platte hatte {1:.1f} GiB. Mit 'Größe anpassen' (--shrink) werden Partitionen und Volumes verkleinert, sofern die belegten Daten passen.", target_size / 2**30, src.size / 2**30))
    dump = rec.sfdisk.get(src.kname)
    if not dump:
        raise RescueError(tr("Partitionstabelle der Platte {0} fehlt in den Wiederherstellungsdaten", src.kname))
    plan = Plan(mount_root=mount_root)
    vg_files = dict(rec.vg_files)
    if smaller:
        from . import shrink as _shrink
        sr = _shrink.compute_shrink(rec, src, dump, target_size, encryption, snapshot_size)
        dump = sr.dump
        plan.resizes, plan.warnings = sr.resizes, list(sr.warnings)
        if sr.vg_texts:
            import tempfile
            wd = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="pbsgui-shrink-"))
            wd.mkdir(parents=True, exist_ok=True)
            for vg, text in sr.vg_texts.items():
                vg_files[vg] = wd / f"lvm-{vg}.vg"
                vg_files[vg].write_text(text)
    plan.summary.append(tr("Ziel-Platte {0} wird komplett überschrieben (Original: {1}, {2:.1f} GiB)", target_disk, src.path, src.size / 2**30))
    plan.steps += [
        Step(tr("Ziel-Platte {0} leeren", target_disk), ["wipefs", "-a", "-f", target_disk], destructive=True),
        Step(tr("Partitionstabelle anlegen"), ["sfdisk", "--no-reread", target_disk], destructive=True,
             stdin=retarget_sfdisk(dump, src.path, target_disk)),
        Step(tr("Partitionen erkennen"), ["partprobe", target_disk]),
        Step(tr("Auf Geräte warten"), ["udevadm", "settle"]),
    ]
    fs_jobs: list[tuple[Node, str]] = []     # (Knoten, neues Gerät)
    vg_pvs: dict[str, list[str]] = {}

    def visit(node: Node, dev: str) -> None:
        if node.fstype == "crypto_LUKS":
            child = next((c for c in node.children if c.type == "crypt"), None)
            if not child:
                raise RescueError(tr("Entschlüsselter Knoten zu {0} fehlt in den Wiederherstellungsdaten", node.path))
            if encryption == "none":      # Inhalt direkt auf die Partition, alter crypttab-Eintrag entfällt
                plan.crypttab[child.name] = None
                visit(child, dev)
                return
            if encryption == "new":       # frischer Container mit neuer Passphrase (neue UUID in crypttab)
                plan.crypttab[child.name] = _luks_format(dev, child.name, new_passphrase or "", plan)
                visit(child, f"/dev/mapper/{child.name}")
                return
            hdr = rec.luks_headers.get(node.kname)
            if not hdr:
                raise RescueError(tr("LUKS-Header von {0} fehlt im Backup (das Backup war unverschlüsselt). Verschlüsselte Datenträger lassen sich nur mit dem gesicherten Header nachbauen (oder mit der Option 'neu verschlüsseln' bzw. 'ohne Verschlüsselung').", node.path))
            if not passphrase:
                raise RescueError(tr("Für den LUKS-Datenträger wird die Passphrase benötigt"))
            plan.steps.append(Step(tr("LUKS-Header auf {0} wiederherstellen", dev),
                                   ["cryptsetup", "luksHeaderRestore", "--batch-mode", dev,
                                    "--header-backup-file", str(hdr)], destructive=True))
            plan.steps.append(Step(tr("LUKS {0} öffnen als {1}", dev, child.name),
                                   ["cryptsetup", "open", "--type", "luks", "--key-file=-", dev, child.name],
                                   stdin=passphrase))
            plan.cleanup.append(Step(tr("LUKS {0} schließen", child.name),
                                     ["sh", "-c", 'for i in 1 2 3 4 5; do cryptsetup close "$1" && exit 0; sleep 2; done; exit 5',
                                      "luks-close", child.name], ok_codes=(0, 4, 5)))
            visit(child, f"/dev/mapper/{child.name}")
            return
        if node.fstype == "LVM2_member":
            vg, uuid = rec.pvs.get(node.path, ("", ""))
            if not vg or not uuid or vg not in vg_files:
                raise RescueError(tr("LVM-Metadaten für {0} fehlen in den Wiederherstellungsdaten", node.path))
            plan.steps.append(Step(tr("LVM-Volume {0} anlegen (PV-UUID {1})", dev, uuid),
                                   ["pvcreate", "-ff", "-y", "--uuid", uuid, "--restorefile", str(vg_files[vg]),
                                    dev], destructive=True))
            vg_pvs.setdefault(vg, []).append(dev)
            for c in node.children:  # Logical Volumes: entstehen durch vgcfgrestore + vgchange
                visit(c, f"/dev/mapper/{c.name}")
            return
        if node.fstype:
            fs_jobs.append((node, dev))
        for c in node.children:
            visit(c, dev)

    for part in src.children:
        if part.type != "part":
            continue
        pdev = partition_path(target_disk, _part_number(part.path))
        if encryption == "new" and part.fstype != "crypto_LUKS" and _is_data_partition(part, rec):
            name = f"{part.kname}_crypt"
            plan.crypttab[name] = _luks_format(pdev, name, new_passphrase or "", plan)
            plan.needs_cryptsetup = True
            plan.summary.append(tr("{0} wird neu mit LUKS verschlüsselt ({1})", pdev, name))
            visit(part, f"/dev/mapper/{name}")
        else:
            visit(part, pdev)

    for vg in vg_pvs:
        plan.steps.append(Step(tr("Volume Group {0} wiederherstellen", vg),
                               ["vgcfgrestore", "-f", str(vg_files[vg]), "--force", vg], destructive=True))
        plan.steps.append(Step(tr("Volume Group {0} aktivieren", vg), ["vgchange", "-ay", vg]))
        plan.cleanup.append(Step(tr("Volume Group {0} deaktivieren", vg),
                                 ["sh", "-c", 'for i in 1 2 3 4 5; do vgchange -an "$1" && exit 0; sleep 2; done; exit 5',
                                  "vg-deactivate", vg], ok_codes=(0, 5)))
    if vg_pvs:
        plan.steps.append(Step(tr("Auf Geräte warten"), ["udevadm", "settle"]))

    mounts: list[tuple[str, str, Node]] = []
    for node, dev in fs_jobs:
        step = _mkfs(node, dev)
        if step is None:
            plan.warnings.append(tr("Dateisystem {0} auf {1} wird nicht unterstützt: übersprungen", node.fstype, node.path))
            continue
        plan.steps.append(step)
        for mp in node.mountpoints:
            real = rec.real_mp(mp)
            if real.startswith("/") and not real.startswith("[") and real != "[SWAP]":
                mounts.append((real, dev, node))
                break
    if not any(mp == "/" for mp, _, _ in mounts):
        raise RescueError(tr("Das Root-Dateisystem (/) konnte nicht zugeordnet werden"))
    if encryption == "new" and not any(mp == "/boot" for mp, _, _ in mounts):
        raise RescueError(tr("Neue Verschlüsselung braucht eine getrennte /boot-Partition (GRUB kann das verschlüsselte Root-Dateisystem sonst nicht lesen). Bitte 'wie im Original' oder 'ohne' wählen."))
    plan.summary.insert(1, {"keep": tr("Verschlüsselung: wie im Original"), "new": tr("Verschlüsselung: NEU (neue Passphrase)"),
                         "none": tr("Verschlüsselung: KEINE (Platte wird unverschlüsselt aufgebaut)")}[encryption])
    for mp, dev, node in sorted(mounts, key=lambda m: m[0].count("/") if m[0] != "/" else -1):
        target = mount_root + ("" if mp == "/" else mp)
        plan.steps.append(Step(tr("{0} einhängen ({1})", mp, dev), ["sh", "-c", 'mkdir -p "$1" && mount "$2" "$1"', "mount",
                                                           target, dev]))
        plan.summary.append(f"{mp:<12} {node.fstype:<6} {dev}")
    if plan.resizes:
        plan.summary.append("")
        plan.summary.append(tr("Größe angepasst: Ziel-Platte {0:.1f} GiB statt {1:.1f} GiB (alt -> neu)", target_size / 2**30, src.size / 2**30))
        plan.summary += ["  " + r.line() for r in plan.resizes]
    # Aufräumen in umgekehrter Reihenfolge des Aufbaus: erst aushängen, dann VG deaktivieren, dann LUKS schließen
    # erst normal, dann lazy aushängen; danach die Geräte freigeben lassen (sonst bleibt der LV/LUKS "in Benutzung")
    plan.cleanup = [Step(tr("Zielsystem aushängen"), ["sh", "-c", 'sync; umount -R "$1" 2>/dev/null; '
                                                  'for m in $(findmnt -rn -o TARGET -R "$1" 2>/dev/null | sort -r); '
                                                  'do umount -l "$m" 2>/dev/null; done; udevadm settle; sleep 1',
                                                  "umount", mount_root], ok_codes=(0, 1, 32)),
                    *reversed(plan.cleanup)]
    return plan


_MOUNTABLE_FS = {"vfat", "exfat", "ntfs", "ntfs3", "ext2", "ext3", "ext4", "xfs", "btrfs"}


def mount_removable(run: Callable = subprocess.run, base: str = "/media/pbsgui") -> list[str]:
    """Hängt alle noch nicht eingehängten Partitionen (z. B. USB-Sticks) schreibgeschützt ein; liefert die Ziele."""
    r = run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,MOUNTPOINTS"], capture_output=True, text=True)
    mounted = []
    try:
        tree = json.loads(r.stdout or "{}").get("blockdevices", [])
    except ValueError:
        return mounted
    for disk in tree:
        for n in Node.from_json(disk).walk():
            if n.type != "part" or n.fstype not in _MOUNTABLE_FS or n.mountpoints:
                continue
            target = f"{base}/{n.name}"
            os.makedirs(target, exist_ok=True)
            if run(["mount", "-o", "ro", n.path, target], capture_output=True, text=True).returncode == 0:
                mounted.append(target)
    return mounted


def find_key_files(roots: list[str] | None = None, mount: bool = False, run: Callable = subprocess.run) -> list[str]:
    """Sucht exportierte pbsgui-/PBS-Schlüsseldateien (JSON mit "kdf" und "fingerprint") auf eingehängten Medien."""
    if mount:
        mount_removable(run)
    found: list[str] = []
    for root in roots or ["/media", "/mnt", "/run/media"]:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirs, files in os.walk(base):
            if Path(dirpath).relative_to(base).parts.__len__() >= 4:
                dirs.clear()
                continue
            for f in files:
                if not f.lower().endswith(".json"):
                    continue
                p = Path(dirpath) / f
                try:
                    if p.stat().st_size > 8192:
                        continue
                    d = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and "kdf" in d and "fingerprint" in d:
                    found.append(str(p))
    return sorted(found)


def original_encrypted(rec: RecoveryData) -> bool:
    """War die Quell-Platte mit LUKS verschlüsselt?"""
    return any(n.fstype == "crypto_LUKS" for n in rec.root_disk().walk())


def needs_passphrase(rec: RecoveryData, encryption: str = "keep") -> bool:
    """Wird die alte LUKS-Passphrase gebraucht? Nur beim Nachbau 'wie im Original' eines verschlüsselten Systems."""
    return encryption == "keep" and original_encrypted(rec)


def restore_step(client: str, snapshot: str, archive: str, mount_root: str, namespace: str = "", keyfile: str = "",
                 env: dict[str, str] | None = None) -> Step:
    argv = [client, "restore", snapshot, archive, mount_root, "--allow-existing-dirs", "true",
            "--overwrite-files", "true", "--overwrite-symlinks", "true", "--overwrite-hardlinks", "true"]
    if namespace:
        argv += ["--ns", namespace]
    if keyfile:
        argv += ["--keyfile", keyfile]
    return Step(tr("Dateien zurückspielen ({0})", archive), argv, env=env or {})


def edit_crypttab(mount_root: str, edits: dict[str, str | None], on_line: Callable[[str], None] = print) -> None:
    """Passt /etc/crypttab des Zielsystems an: Name -> neue LUKS-UUID (None = Eintrag entfernen/auskommentieren)."""
    path = Path(mount_root) / "etc" / "crypttab"
    lines = path.read_text().splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        parts = line.split()
        if parts and not line.lstrip().startswith("#") and parts[0] in edits:
            name = parts[0]
            seen.add(name)
            if edits[name] is None:
                out.append(tr("# pbsgui: ohne Verschlüsselung wiederhergestellt, Eintrag deaktiviert: ") + line)
                on_line(tr("crypttab: Eintrag {0} deaktiviert", name))
            else:
                if len(parts) > 1:
                    parts[1] = f"UUID={edits[name]}"
                out.append(" ".join(parts))
                on_line(tr("crypttab: Eintrag {0} auf neue UUID {1} gesetzt", name, edits[name]))
        else:
            out.append(line)
    for name, luks_uuid in edits.items():
        if luks_uuid and name not in seen:
            out.append(f"{name} UUID={luks_uuid} none luks,discard")
            on_line(tr("crypttab: Eintrag {0} ergänzt", name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")


_ENSURE_CRYPTSETUP = (
    'r="$1"; chroot "$r" dpkg -s cryptsetup-initramfs >/dev/null 2>&1 && exit 0; '
    '[ -e "$r/etc/resolv.conf" ] && mv "$r/etc/resolv.conf" "$r/etc/resolv.conf.pbsgui"; '
    'cp -L /etc/resolv.conf "$r/etc/resolv.conf"; '
    'chroot "$r" env LC_ALL=C DEBIAN_FRONTEND=noninteractive sh -c "apt-get update && apt-get install -y cryptsetup-initramfs"; '
    'rc=$?; rm -f "$r/etc/resolv.conf"; [ -e "$r/etc/resolv.conf.pbsgui" ] && mv "$r/etc/resolv.conf.pbsgui" "$r/etc/resolv.conf"; '
    'exit $rc')


def finish_steps(rec: RecoveryData, mount_root: str, target_disk: str, uefi: bool,
                 plan: "Plan | None" = None) -> list[Step]:
    """Im chroot: Initramfs und Bootloader neu erzeugen (Hardware kann sich geändert haben)."""
    # LC_ALL=C: die Live-Umgebung hat de_DE gesetzt, das Zielsystem nicht zwingend (sonst Locale-Warnungen)
    chroot = lambda *a: ["chroot", mount_root, "env", "LC_ALL=C", *a]
    steps = [Step(tr("Leere Systemverzeichnisse anlegen (vom Backup ausgeschlossen: /tmp, /var/tmp, /proc …)"),
                  ["sh", "-c", 'cd "$1" && mkdir -p proc sys dev run tmp var/tmp mnt media && chmod 1777 tmp var/tmp',
                   "mkdirs", mount_root])]
    steps += [Step(tr("/{0} einbinden", d), ["sh", "-c", 'mkdir -p "$2" && mount --rbind "$1" "$2" && mount --make-rslave "$2"',
                                      "bind", f"/{d}", f"{mount_root}/{d}"]) for d in ("dev", "proc", "sys", "run")]
    if plan and plan.needs_cryptsetup:
        steps.append(Step(tr("Verschlüsselungs-Pakete im Zielsystem sicherstellen (cryptsetup-initramfs)"),
                          ["sh", "-c", _ENSURE_CRYPTSETUP, "ensure-crypt", mount_root]))
    if plan and plan.crypttab:
        edits = dict(plan.crypttab)
        steps.append(Step(tr("crypttab im Zielsystem anpassen"), [],
                          call=lambda on_line: edit_crypttab(mount_root, edits, on_line)))
    steps.append(Step(tr("Initramfs neu erzeugen"), chroot("update-initramfs", "-u", "-k", "all")))
    if uefi:
        steps += [
            Step(tr("GRUB (UEFI) installieren"), chroot("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi",
                                                    f"--bootloader-id={rec.efi_label}", "--recheck")),
            Step(tr("GRUB als Fallback-Eintrag installieren (für neue Hardware)"),
                 chroot("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi", "--removable", "--recheck")),
        ]
    else:
        steps.append(Step(tr("GRUB (BIOS) installieren"), chroot("grub-install", "--recheck", target_disk)))
    steps.append(Step(tr("GRUB-Konfiguration erzeugen"), chroot("update-grub")))
    return steps


# -- Ausführung ------------------------------------------------------------------------
def run_steps(steps: list[Step], on_line: Callable[[str], None] = print, env: dict[str, str] | None = None,
              dry_run: bool = False, popen=subprocess.Popen) -> Step | None:
    """Führt Schritte nacheinander aus; liefert den fehlgeschlagenen Schritt oder None."""
    for st in steps:
        on_line(f"==> {st.title}")
        if dry_run:
            on_line("    " + (" ".join(st.argv) if st.argv else tr("(interne Funktion)")))
            continue
        if st.call:
            try:
                st.call(lambda l: on_line("    " + l))
            except Exception as e:  # Fehler wie bei einem fehlgeschlagenen Befehl behandeln
                on_line(tr("    FEHLER: {0}", e))
                return st
            continue
        full_env = {**os.environ, **(env or {}), **st.env}
        try:
            p = popen(st.argv, env=full_env, stdin=subprocess.PIPE if st.stdin is not None else subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            on_line(tr("    FEHLER: {0}", e))
            return st
        if st.stdin is not None and p.stdin:
            p.stdin.write(st.stdin)
            p.stdin.close()
        if p.stdout is None:
            raise RescueError(tr("Kein Ausgabestrom vom Befehl"))
        for line in p.stdout:
            on_line("    " + line.rstrip("\n"))
        code = p.wait()
        if code not in st.ok_codes:
            on_line(tr("    FEHLER: Exitcode {0}", code))
            return st
    return None


# -- Gesamtablauf ------------------------------------------------------------------------
@dataclass
class RescueConfig:
    server: str
    datastore: str
    auth_id: str
    token: str = ""            # Secret: nur im Speicher / Umgebung des Clients
    port: int = 8007
    fingerprint: str = ""
    namespace: str = ""
    backup_id: str = ""        # leer = alle Vollbackups des Namespace zur Auswahl
    snapshot: str = ""         # Snapshot-Pfad; leer = neuester
    keyfile: str = ""          # Pfad zur exportierten Schlüsseldatei (verschlüsseltes Backup)
    passphrase: str = ""       # LUKS-Passphrase des Originalsystems
    encryption: str = "keep"   # "keep" (wie Original) | "new" (neu verschlüsseln) | "none" (ohne Verschlüsselung)
    new_passphrase: str = ""   # Passphrase für neue LUKS-Container (nur bei encryption="new")
    target_disk: str = ""
    client: str = ""           # leer = proxmox-backup-client (oder $PBSGUI_CLIENT)
    uefi: bool | None = None   # None = nach Boot-Modus der Rettungs-ISO

    def profile(self):
        from .models import Profile
        return Profile(name="rescue", server=self.server, port=self.port, datastore=self.datastore,
                       auth_id=self.auth_id, fingerprint=self.fingerprint, namespace=self.namespace,
                       backup_id=self.backup_id or "rescue")

    def pbs_env(self) -> dict[str, str]:
        p = self.profile()
        env = {"PBS_REPOSITORY": p.repository, "PBS_PASSWORD": self.token}
        if self.fingerprint:
            env["PBS_FINGERPRINT"] = self.fingerprint
        return env

    @classmethod
    def from_json(cls, path: str | Path) -> "RescueConfig":
        """Nicht geheime Verbindungsdaten (z. B. /etc/pbsgui/rescue.json aus der personalisierten ISO)."""
        d = json.loads(Path(path).read_text())
        known = {f for f in cls.__dataclass_fields__ if f not in ("token", "passphrase", "new_passphrase")}
        return cls(**{k: v for k, v in d.items() if k in known})


def _client(cfg: RescueConfig):
    from .client import PbsClient, client_binary
    return PbsClient(cfg.profile(), cfg.token, binary=cfg.client or client_binary(), key_path=cfg.keyfile)


def list_full_snapshots(cfg: RescueConfig):
    """Snapshots, die ein Vollbackup mit Wiederherstellungsdaten sind (root.pxar + recovery.pxar), neueste zuerst."""
    snaps = _client(cfg).list_snapshots()
    ok = [s for s in snaps if "root.pxar" in s.archives and "recovery.pxar" in s.archives]
    if cfg.backup_id:
        ok = [s for s in ok if s.backup_id == cfg.backup_id]
    return ok


def pick_snapshot(cfg: RescueConfig):
    snaps = list_full_snapshots(cfg)
    if not snaps:
        raise RescueError(tr("Kein Vollbackup mit Wiederherstellungsdaten gefunden (benötigt pbsgui ≥ 0.2 mit --mode full)"))
    if cfg.snapshot:
        for s in snaps:
            if s.path == cfg.snapshot:
                return s
        raise RescueError(tr("Snapshot nicht gefunden: {0}", cfg.snapshot))
    return snaps[0]


def fetch_recovery(cfg: RescueConfig, snapshot, dest: Path) -> RecoveryData:
    """Holt nur das kleine recovery.pxar (Sekunden) und liest das Layout daraus."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _client(cfg).restore(snapshot, "recovery.pxar", str(dest))
    return RecoveryData.from_dir(dest)


def candidate_disks(lsblk_json: str | None = None) -> list[dict]:
    """Platten, auf die wiederhergestellt werden darf: keine eingehängten, keine Loop-/CD-Geräte, nicht das Live-Medium."""
    if lsblk_json is None:
        r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,MODEL,TRAN,RM,MOUNTPOINTS,PKNAME"],
                           capture_output=True, text=True)
        lsblk_json = r.stdout
    live = ""
    try:  # Gerät, von dem die ISO gebootet wurde
        live = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "/run/live/medium"], capture_output=True,
                              text=True).stdout.strip()
    except OSError:
        pass
    out = []
    for d in json.loads(lsblk_json or "{}").get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        node = Node.from_json(d)
        mounted = [mp for n in node.walk() for mp in n.mountpoints]
        if mounted or (live and any(n.path == live for n in node.walk())):
            continue
        out.append({"path": node.path, "size": node.size, "model": (d.get("model") or "").strip(),
                    "tran": d.get("tran") or "", "removable": bool(d.get("rm"))})
    return out


def validate_target(disk: str, rec: RecoveryData, disks: list[dict] | None = None) -> int:
    """Prüft die Ziel-Platte und liefert ihre Größe in Bytes."""
    cands = candidate_disks() if disks is None else disks
    match = next((d for d in cands if d["path"] == disk), None)
    if not match:
        raise RescueError(tr("{0} ist keine zulässige Ziel-Platte (eingehängt, Live-Medium oder nicht vorhanden)", disk))
    return match["size"]


def execute(cfg: RescueConfig, snapshot, rec: RecoveryData, plan: Plan, on_line: Callable[[str], None] = print,
            dry_run: bool = False, popen=subprocess.Popen) -> bool:
    """Führt Layout, Dateien und Bootloader aus; räumt immer auf. True = Erfolg."""
    uefi = cfg.uefi if cfg.uefi is not None else Path("/sys/firmware/efi").exists()
    env = cfg.pbs_env()
    ok = False
    try:
        if run_steps(plan.steps, on_line, env, dry_run, popen):
            return False
        from .client import client_binary
        step = restore_step(cfg.client or client_binary(), snapshot.path, "root.pxar", plan.mount_root, cfg.namespace, cfg.keyfile, env)
        if run_steps([step], on_line, env, dry_run, popen):
            return False
        if run_steps(finish_steps(rec, plan.mount_root, cfg.target_disk, uefi, plan), on_line, env, dry_run, popen):
            return False
        ok = True
        return True
    finally:
        on_line(tr("==> Aufräumen"))
        run_steps(plan.cleanup, on_line, env, dry_run, popen)
        if ok:
            on_line(tr("Trockenlauf beendet: es wurde nichts verändert.") if dry_run else
                    tr("Wiederherstellung abgeschlossen. System neu starten und das Rettungsmedium entfernen."))
