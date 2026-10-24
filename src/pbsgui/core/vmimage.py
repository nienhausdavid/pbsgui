"""VM-Abbilder wiederherstellen (V2P): Proxmox-VE-Backups (Snapshots `vm/<id>/<zeit>`) auf physische Platten zurückspielen.

Ein VM-Backup enthält je Platte ein Abbild-Archiv (`drive-scsi0.img.fidx`) und die VM-Konfiguration (`qemu-server.conf.blob`).
Der Client schreibt ein Abbild direkt auf ein Blockgerät. Danach wird das Gastsystem (Linux, Debian/Ubuntu-Familie) für die
neue Hardware repariert: Platte und Dateisystem vergrößern, Initramfs mit Treibern für echte Hardware, Bootloader, Netzwerknamen,
Gast-Dienste (qemu-guest-agent, cloud-init). Jede Reparatur steht im Plan und ist einzeln abschaltbar.

Ausführung und Planung nutzen die Schritt-Maschinerie von rescue.py (Trockenlauf, Protokoll, Aufräumen).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..i18n import tr
from . import rescue
from .models import Snapshot
from .rescue import RescueConfig, RescueError, Step, run_steps

Run = Callable[..., subprocess.CompletedProcess]
SKIP_DISKS = ("efidisk", "tpmstate")           # keine Daten-Platten: EFI-Variablen und TPM-Zustand der VM
_ARCHIVE = re.compile(r"^drive-(?P<key>[a-z]+\d+)\.img\.fidx$")
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide|efidisk|tpmstate|nvme)\d+$")
LINUX_OSTYPES = ("l26", "l24", "other", "")
MOUNT_ROOT = "/mnt/target"
GROW_SLACK = 16 * 2**20                          # so viel größer muss die Platte sein, damit vergrößert wird


@dataclass
class VmDisk:
    key: str                  # scsi0, virtio1 …
    archive: str              # drive-scsi0.img
    image_size: int           # Größe des Abbilds in Byte


@dataclass
class VmConfig:
    name: str = ""
    ostype: str = ""
    bios: str = "seabios"
    machine: str = ""
    boot_order: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)    # alle Platten-Schlüssel der Konfiguration
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def uefi(self) -> bool:
        return self.bios == "ovmf"


@dataclass
class RepairOptions:
    grow: bool = True          # Partition und Dateisystem füllen, wenn die Ziel-Platte größer ist
    initramfs: bool = True     # Initramfs mit allen Treibern (MODULES=most) neu erzeugen
    bootloader: bool = True    # GRUB (UEFI mit Fallback oder BIOS) neu installieren
    network: bool = True       # Schnittstellennamen in der Netzwerk-Konfiguration anpassen
    services: bool = True      # qemu-guest-agent und cloud-init abschalten

    @classmethod
    def from_dict(cls, d: dict | None) -> "RepairOptions":
        d = d or {}
        return cls(**{k: bool(d.get(k, True)) for k in ("grow", "initramfs", "bootloader", "network", "services")})


@dataclass
class VmPlan:
    steps: list[Step] = field(default_factory=list)
    cleanup: list[Step] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    boot_key: str = ""


# -- Konfiguration und Snapshots ---------------------------------------------------------------------------------
def parse_conf(text: str) -> VmConfig:
    """qemu-server.conf: `schlüssel: wert`; Abschnitte (`[snapshot]`) und Kommentare werden ignoriert, auch ältere Stände."""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("[") or line.startswith("#") or not line.strip():
            if line.startswith("["):
                break                    # ab hier folgen Snapshot-Stände der VM, nicht die aktuelle Konfiguration
            continue
        k, sep, v = line.partition(":")
        if sep:
            raw[k.strip()] = v.strip()
    boot = raw.get("boot", "")
    order: list[str] = []
    m = re.search(r"order=([^,\s]+)", boot)
    if m:
        order = [x for x in m.group(1).split(";") if x]
    elif boot and "=" not in boot:                     # alte Schreibweise: boot: cdn
        order = []
    return VmConfig(name=raw.get("name", ""), ostype=raw.get("ostype", ""), bios=raw.get("bios", "seabios"),
                    machine=raw.get("machine", ""), boot_order=order, keys=[k for k in raw if _DISK_KEY.match(k)], raw=raw)


def vm_disks(snap: Snapshot) -> list[VmDisk]:
    """Daten-Platten eines VM-Snapshots (ohne EFI-Variablen und TPM)."""
    out: list[VmDisk] = []
    for f in snap.files:
        m = _ARCHIVE.match(f)
        if m and not m["key"].startswith(SKIP_DISKS):
            out.append(VmDisk(m["key"], f.removesuffix(".fidx"), snap.file_sizes.get(f, 0)))
    return sorted(out, key=lambda d: d.key)


def skipped_disks(snap: Snapshot) -> list[str]:
    return sorted(m["key"] for f in snap.files if (m := _ARCHIVE.match(f)) and m["key"].startswith(SKIP_DISKS))


def _client(cfg: RescueConfig):
    return rescue._client(cfg)


def list_vm_snapshots(cfg: RescueConfig) -> list[Snapshot]:
    """VM-Snapshots mit mindestens einem Abbild-Archiv, neueste zuerst; `backup_id` (VM-Nummer) filtert."""
    snaps = [s for s in _client(cfg).list_snapshots() if s.backup_type == "vm" and vm_disks(s)]
    if cfg.backup_id:
        snaps = [s for s in snaps if s.backup_id == cfg.backup_id]
    return snaps


def fetch_config(cfg: RescueConfig, snap: Snapshot) -> VmConfig:
    try:
        return parse_conf(_client(cfg).restore_text(snap, "qemu-server.conf"))
    except Exception as e:      # ohne Konfiguration geht es trotzdem (Boot-Platte = erste Platte), aber der Nutzer muss es wissen
        raise RescueError(tr("qemu-server.conf nicht lesbar: {0}", e)) from None


def boot_disk_key(vmcfg: VmConfig, disks: list[VmDisk]) -> str:
    """Boot-Platte: erster Eintrag der Boot-Reihenfolge, der ein Abbild hat; sonst die erste Platte."""
    have = {d.key for d in disks}
    for k in vmcfg.boot_order:
        if k in have:
            return k
    return disks[0].key if disks else ""


# -- Plan --------------------------------------------------------------------------------------------------------
def validate_mapping(disks: list[VmDisk], mapping: dict[str, str], targets: list[dict]) -> dict[str, int]:
    """Prüft Zuordnung Quell-Platte -> Ziel-Platte; liefert die Größe der Ziele. Fehler als RescueError."""
    by_path = {t["path"]: t for t in targets}
    keys = {d.key for d in disks}
    unknown = [k for k in mapping if k not in keys]
    if unknown:
        raise RescueError(tr("Unbekannte Quell-Platte: {0}", ', '.join(unknown)))
    if not mapping:
        raise RescueError(tr("Keine Ziel-Platte gewählt"))
    if len(set(mapping.values())) != len(mapping):
        raise RescueError(tr("Eine Ziel-Platte darf nur einmal vorkommen"))
    sizes: dict[str, int] = {}
    for key, dev in mapping.items():
        t = by_path.get(dev)
        if not t:
            raise RescueError(tr("{0} ist keine zulässige Ziel-Platte (eingehängt, Live-Medium oder nicht vorhanden)", dev))
        d = next(x for x in disks if x.key == key)
        if t["size"] < d.image_size:
            raise RescueError(tr("{0} ist zu klein: {1:.1f} GiB, das Abbild {2} braucht {3:.1f} GiB (kleinere Platten gehen nur mit dem Dateimodus-Backup)", dev, t['size'] / 2**30, key, d.image_size / 2**30))
        sizes[key] = t["size"]
    return sizes


def build_plan(cfg: RescueConfig, snap: Snapshot, vmcfg: VmConfig, mapping: dict[str, str], targets: list[dict],
               options: RepairOptions | None = None, run: Run = subprocess.run, mount_root: str = MOUNT_ROOT,
               uefi_live: bool | None = None) -> VmPlan:
    opts = options or RepairOptions()
    disks = vm_disks(snap)
    if vmcfg.ostype.startswith(("win", "solaris")):
        raise RescueError(tr("Gast-Betriebssystem '{0}' wird nicht unterstützt (nur Linux); das Abbild lässt sich per `proxmox-backup-client restore` von Hand auf eine Platte schreiben, die Reparatur fehlt dann.", vmcfg.ostype))
    sizes = validate_mapping(disks, mapping, targets)
    boot = boot_disk_key(vmcfg, disks)
    plan = VmPlan(mapping=dict(mapping), boot_key=boot)
    if boot not in mapping:
        plan.warnings.append(tr("Die Boot-Platte {0} der VM ist keiner Ziel-Platte zugeordnet: das System wird so nicht booten.", boot))
    if vmcfg.ostype == "other" or not vmcfg.ostype:
        plan.warnings.append(tr("Gast-Betriebssystem unbekannt (ostype fehlt/other): die Reparatur gilt nur für Debian und Ubuntu."))
    for k in skipped_disks(snap):
        plan.summary.append(tr("{0}: nicht geschrieben (EFI-Variablen/TPM der VM; die neue Hardware legt eigene an)", k))
    env = cfg.pbs_env()
    ctx = _Ctx(cfg=cfg, snap=snap, vmcfg=vmcfg, mapping=dict(mapping), boot=boot, opts=opts, run=run, mount_root=mount_root,
               uefi_live=Path("/sys/firmware/efi").exists() if uefi_live is None else uefi_live,
               sizes=sizes, images={d.key: d for d in disks})
    for key, dev in mapping.items():
        d = ctx.images[key]
        extra = ""
        if opts.grow and sizes[key] > d.image_size + GROW_SLACK:
            extra = tr(", danach vergrößert auf {0:.1f} GiB", sizes[key] / 2**30)
        plan.summary.append(tr("{0} ({1:.1f} GiB) -> {2} ({3:.1f} GiB){4}", key, d.image_size / 2**30, dev, sizes[key] / 2**30, extra)
                            + ("  [Boot-Platte]" if key == boot else ""))
        plan.steps.append(Step(tr("Signaturen auf {0} löschen", dev), ["wipefs", "-a", dev], destructive=True))
        plan.steps.append(Step(tr("{0} auf {1} schreiben (Abbild {2})", key, dev, d.archive), [], env=env, destructive=True,
                               call=lambda on_line, k=key, dv=dev: ctx.write_image(k, dv, on_line)))
    plan.steps.append(Step(tr("Partitionstabellen neu einlesen"), ["sh", "-c", "partprobe " + " ".join(mapping.values()) +
                                                            "; udevadm settle; sleep 1"]))
    if opts.grow:
        plan.steps.append(Step(tr("Partitionen vergrößern (falls die Ziel-Platte größer ist)"), [], call=ctx.grow_partitions))
    repairs = opts.grow or opts.network or opts.services or opts.initramfs or opts.bootloader
    if repairs:
        plan.steps.append(Step(tr("Gastsystem finden und einhängen"), [], call=ctx.mount_guest))
    if opts.grow and repairs:
        plan.steps.append(Step(tr("Dateisystem vergrößern"), [], call=ctx.grow_filesystem))
    if opts.network:
        plan.steps.append(Step(tr("Netzwerk-Konfiguration an die neue Hardware anpassen"), [], call=ctx.fix_network))
    if opts.services:
        plan.steps.append(Step(tr("Gast-Dienste abschalten (qemu-guest-agent, cloud-init)"), [], call=ctx.disarm_services))
    if opts.initramfs or opts.bootloader:
        plan.steps.append(Step(tr("Initramfs und Bootloader für die neue Hardware"), [], call=ctx.repair_boot))
    plan.cleanup = [Step(tr("Gastsystem aushängen und Volumes schließen"), [], call=ctx.cleanup)]
    for what, on in ((tr("Vergrößern"), opts.grow), (tr("Initramfs mit allen Treibern"), opts.initramfs), (tr("Bootloader neu"), opts.bootloader),
                     (tr("Netzwerknamen anpassen"), opts.network), (tr("Gast-Dienste abschalten"), opts.services)):
        plan.summary.append(tr("Reparatur: {0}: {1}", what, tr("ja") if on else tr("nein")))
    if any(not d.image_size for d in disks):
        plan.warnings.append(tr("Die Größe eines Abbilds ist unbekannt; die Prüfung der Plattengröße ist unvollständig."))
    return plan


# -- Ausführung ---------------------------------------------------------------------------------------------------
@dataclass
class _Ctx:
    cfg: RescueConfig
    snap: Snapshot
    vmcfg: VmConfig
    mapping: dict[str, str]
    boot: str
    opts: RepairOptions
    run: Run
    mount_root: str
    uefi_live: bool
    sizes: dict[str, int]
    images: dict[str, VmDisk]
    opened: list[str] = field(default_factory=list)       # geöffnete LUKS-Container (Mapper-Namen)
    luks: list[tuple[str, str]] = field(default_factory=list)   # (Mapper-Name, Partition) der geöffneten Container
    vgs: list[str] = field(default_factory=list)
    mounted: list[str] = field(default_factory=list)      # Einhängepunkte in Reihenfolge
    root_dev: str = ""
    root_fstype: str = ""
    esp: bool = False

    # -- kleine Helfer
    def sh(self, argv: list[str], on_line: Callable[[str], None] | None = None, ok=(0,), input_: str | None = None) -> str:
        # ohne Eingabe ausdrücklich leere stdin: ein Werkzeug darf nie auf dem Terminal nach einem Passwort fragen und hängen
        extra = {"input": input_} if input_ is not None else {"stdin": subprocess.DEVNULL}
        r = self.run(argv, capture_output=True, text=True, **extra)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if on_line and out:
            for line in out.splitlines()[-20:]:
                on_line(line)
        if r.returncode not in ok:
            raise RescueError(tr("{0} fehlgeschlagen: {1}", ' '.join(argv[:3]), out[-300:]))
        return out

    def write_image(self, key: str, dev: str, on_line: Callable[[str], None]) -> None:
        _client(self.cfg).restore_image(self.snap, self.images[key].archive, dev, on_line)

    # -- Vergrößern
    def grow_partitions(self, on_line: Callable[[str], None]) -> None:
        for key, dev in self.mapping.items():
            if self.sizes[key] <= self.images[key].image_size + GROW_SLACK:
                continue
            pt = self.sh(["blkid", "-p", "-o", "export", dev], None, ok=(0, 2))
            kind = dict(l.split("=", 1) for l in pt.splitlines() if "=" in l)
            if kind.get("PTTYPE") not in ("gpt", "dos"):
                # Kein Partitionsschema: entweder ein Dateisystem direkt auf der Platte oder etwas Unbekanntes. sgdisk würde hier
                # eine NEUE GPT über den Anfang schreiben und das Dateisystem zerstören: nie anfassen.
                fs = kind.get("TYPE", "")
                if fs.startswith("ext"):
                    on_line(tr("{0}: Dateisystem {1} direkt auf der Platte, vergrößere es", dev, fs))
                    self.sh(["e2fsck", "-fy", dev], on_line, ok=(0, 1))
                    self.sh(["resize2fs", dev], on_line)
                else:
                    on_line(tr("{0}: kein Partitionsschema ({1}), nicht vergrößert", dev, fs or 'unbekannter Inhalt'))
                continue
            on_line(tr("{0}: Platte ist größer als das Abbild, passe die Partitionstabelle an", dev))
            self.sh(["sgdisk", "-e", dev], on_line)
            tab = json.loads(self.sh(["sfdisk", "-J", dev]) or "{}").get("partitiontable", {})
            parts = tab.get("partitions", [])
            if not parts or tab.get("label") not in ("gpt", "dos"):
                on_line(tr("{0}: Partitionstabelle nicht lesbar, nicht vergrößert", dev))
                continue
            last = max(parts, key=lambda p: p["start"])
            if tab["label"] == "dos" and last.get("type") in ("5", "f", "85"):
                on_line(tr("{0}: letzte Partition ist erweitert, nicht vergrößert", dev))
                continue
            n = re.search(r"(\d+)$", last["node"]).group(1)          # type: ignore[union-attr]
            self.sh(["sfdisk", "-N", n, "--no-reread", "-q", dev], on_line, input_=", +\n")
            self.sh(["sh", "-c", f"partprobe {dev}; udevadm settle; sleep 1"])
            on_line(tr("{0}: auf den Rest der Platte vergrößert", last['node']))

    # -- Gastsystem finden
    def _lsblk(self) -> list[dict]:
        out = self.sh(["lsblk", "-J", "-p", "-o", "NAME,PATH,TYPE,FSTYPE,UUID,LABEL,PARTUUID,PKNAME,SIZE", "-b",
                       *self.mapping.values()])
        return json.loads(out or "{}").get("blockdevices", [])

    @staticmethod
    def _walk(nodes: list[dict]):
        for n in nodes:
            yield n
            yield from _Ctx._walk(n.get("children") or [])

    def mount_guest(self, on_line: Callable[[str], None]) -> None:
        # 1. verschlüsselte Container öffnen, LVM aktivieren
        nodes = list(self._walk(self._lsblk()))
        luks = [n for n in nodes if n.get("fstype") == "crypto_LUKS"]
        if luks:
            if not self.cfg.passphrase:
                raise RescueError(tr("Das Abbild enthält verschlüsselte Volumes (LUKS): die Passphrase fehlt"))
            for i, n in enumerate(luks):
                name = f"pbsgui-luks{i}"
                self.sh(["cryptsetup", "open", "--type", "luks", "--key-file=-", n["path"], name], input_=self.cfg.passphrase)
                self.opened.append(name)
                self.luks.append((name, n["path"]))
                on_line(tr("{0}: LUKS geöffnet", n['path']))
        self.sh(["vgscan", "--mknodes"], None, ok=(0, 5))
        out = self.sh(["vgs", "--noheadings", "-o", "vg_name"], None, ok=(0, 5))
        self.vgs = [v.strip() for v in out.splitlines() if v.strip()]
        for vg in self.vgs:
            self.sh(["vgchange", "-ay", vg], None, ok=(0, 5))
        self.sh(["udevadm", "settle"], None, ok=(0, 1))
        # 2. Root-Dateisystem suchen: Dateisystem mit /etc/fstab und /etc/os-release
        nodes = list(self._walk(self._lsblk()))
        cands = [n for n in nodes if n.get("fstype") in ("ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs")]
        mnt = Path(self.mount_root)
        mnt.mkdir(parents=True, exist_ok=True)
        for n in cands:
            probe = Path(tempfile.mkdtemp(prefix="pbsgui-probe-"))
            try:
                self.sh(["mount", "-o", "ro", n["path"], str(probe)])
                is_root = (probe / "etc/fstab").is_file() and (probe / "etc/os-release").is_file()
            except RescueError:
                is_root = False
            finally:
                subprocess.run(["umount", str(probe)], capture_output=True)
                shutil.rmtree(probe, ignore_errors=True)
            if is_root:
                self.root_dev, self.root_fstype = n["path"], n["fstype"]
                break
        if not self.root_dev:
            raise RescueError(tr("Im Abbild wurde kein Linux-Root-Dateisystem gefunden (etc/fstab und etc/os-release fehlen). Verschlüsselte Volumes brauchen die Passphrase; Windows und andere Systeme sind nicht unterstützt."))
        self.sh(["mount", self.root_dev, self.mount_root])
        self.mounted.append(self.mount_root)
        on_line(tr("Root-Dateisystem: {0} ({1})", self.root_dev, self.root_fstype))
        self.align_crypttab(on_line)
        # 3. weitere Einhängepunkte aus der fstab des Gastes (/boot, /boot/efi)
        for spec, point in _fstab_mounts(Path(self.mount_root) / "etc/fstab"):
            if point not in ("/boot", "/boot/efi", "/usr", "/var"):
                continue
            dev = self._resolve(spec)
            if not dev:
                on_line(tr("{0}: Gerät zu '{1}' nicht gefunden, übersprungen", point, spec))
                continue
            target = Path(self.mount_root + point)
            target.mkdir(parents=True, exist_ok=True)
            try:
                self.sh(["mount", dev, str(target)])
            except RescueError as e:
                on_line(tr("{0}: nicht einhängbar ({1})", point, e))
                continue
            self.mounted.append(str(target))
            if point == "/boot/efi":
                self.esp = True
            on_line(f"{point}: {dev}")

    def align_crypttab(self, on_line: Callable[[str], None]) -> None:
        """Benennt geöffnete LUKS-Container so um, wie sie in der crypttab des Gastes heißen.

        Der cryptsetup-Hook von initramfs-tools sucht den Namen des Mapper-Geräts unter dem Root-Dateisystem in /etc/crypttab;
        heißt es anders (hier: pbsgui-luks0), fehlt im neuen Initramfs die Entsperr-Abfrage und das System bootet nicht.
        """
        entries = [(f[0], f[1]) for f in (ln.split() for ln in _read_lines(Path(self.mount_root) / "etc/crypttab")) if len(f) >= 2
                   and not f[0].startswith("#")]
        for i, (name, dev) in enumerate(list(self.luks)):
            uuid = self.sh(["blkid", "-s", "UUID", "-o", "value", dev]).strip()
            want = next((n for n, spec in entries if spec in (f"UUID={uuid}", dev)), "")
            if want and want != name:
                self.sh(["dmsetup", "rename", name, want])
                self.opened[self.opened.index(name)] = want
                self.luks[i] = (want, dev)
                on_line(tr("{0}: Mapper-Name {1} -> {2} (wie in der crypttab des Gastes)", dev, name, want))
        if self.luks and not entries:
            on_line(tr("WARNUNG: der Gast hat Verschlüsselung, aber keine /etc/crypttab: das neue Initramfs fragt evtl. nicht nach der Passphrase"))

    def _resolve(self, spec: str) -> str:
        if spec.startswith("/dev/"):
            return spec if Path(spec).exists() else ""
        if "=" in spec:
            key, _, val = spec.partition("=")
            val = val.strip('"')
            flag = {"UUID": "-U", "LABEL": "-L"}.get(key.upper())
            argv = ["blkid", flag, val] if flag else ["blkid", "-t", f"{key.upper()}={val}", "-o", "device"]
            r = self.run(argv, capture_output=True, text=True)
            return (r.stdout or "").strip().splitlines()[0] if r.returncode == 0 and (r.stdout or "").strip() else ""
        return ""

    # -- Dateisystem vergrößern
    def grow_filesystem(self, on_line: Callable[[str], None]) -> None:
        dev = self.root_dev
        node = next((n for n in self._walk(self._lsblk()) if n.get("path") == dev), None)
        if node is None:
            return
        # Kette von unten: Partition -> (LUKS) -> (LVM) -> Dateisystem
        chain = self._chain(dev)
        for kind, d in chain:
            if kind == "luks":
                name = Path(d).name
                self.sh(["cryptsetup", "resize", "--key-file=-", name], on_line, input_=self.cfg.passphrase)
                on_line(tr("{0}: LUKS auf die neue Größe gebracht", d))
            elif kind == "pv":
                self.sh(["pvresize", d], on_line)
        if any(k == "lv" for k, _ in chain):
            self.sh(["lvextend", "-l", "+100%FREE", dev], on_line, ok=(0, 5))     # 5 = nichts zu tun
        fs = self.root_fstype
        if fs.startswith("ext"):
            self.sh(["resize2fs", dev], on_line)
        elif fs == "xfs":
            self.sh(["xfs_growfs", self.mount_root], on_line)
        elif fs == "btrfs":
            self.sh(["btrfs", "filesystem", "resize", "max", self.mount_root], on_line)
        else:
            on_line(tr("Dateisystem {0} wird nicht vergrößert", fs))
        on_line(tr("Dateisystem auf die volle Größe gebracht"))

    def _chain(self, dev: str) -> list[tuple[str, str]]:
        """Geräte unter dem Root-Dateisystem von innen nach außen: [(lv|luks|pv, gerät) …] soweit relevant."""
        out: list[tuple[str, str]] = []
        nodes = {n["path"]: n for n in self._walk(self._lsblk())}
        cur = nodes.get(dev)
        parents: dict[str, dict] = {}
        for n in nodes.values():
            for c in n.get("children") or []:
                parents[c["path"]] = n
        while cur is not None:
            t = cur.get("type")
            if t == "lvm":
                out.append(("lv", cur["path"]))
            if t == "crypt":
                out.append(("luks", cur["path"]))
            if t in ("part", "crypt") and cur.get("fstype") == "LVM2_member":
                out.append(("pv", cur["path"]))
            cur = parents.get(cur["path"])
        # äußerste (Partition-nahe) zuerst vergrößern: erst LUKS, dann PV; LV zählt separat
        order = {"luks": 0, "pv": 1, "lv": 2}
        return sorted(out, key=lambda x: order[x[0]])

    # -- Reparaturen im Gastsystem
    def _os_family(self) -> str:
        try:
            text = (Path(self.mount_root) / "etc/os-release").read_text()
        except OSError:
            return ""
        ids = " ".join(re.findall(r'^(?:ID|ID_LIKE)=(.*)$', text, re.M)).replace('"', "").lower()
        return "debian" if ("debian" in ids or "ubuntu" in ids) else ids.split()[0] if ids.split() else ""

    def fix_network(self, on_line: Callable[[str], None]) -> None:
        changes = rewrite_network(Path(self.mount_root), physical_interfaces(), on_line)
        if not changes:
            on_line(tr("Netzwerk: nichts anzupassen"))

    def disarm_services(self, on_line: Callable[[str], None]) -> None:
        for line in disarm_guest_services(Path(self.mount_root), self.run):
            on_line(line)

    def repair_boot(self, on_line: Callable[[str], None]) -> None:
        if self._os_family() != "debian":
            on_line(tr("Reparatur für '{0}' fehlt: Initramfs und Bootloader bitte von Hand erneuern (chroot in /mnt/target). Das Abbild selbst ist geschrieben.", self._os_family() or 'unbekanntes System'))
            return
        root = Path(self.mount_root)
        boot_dev = self.mapping.get(self.boot) or next(iter(self.mapping.values()))
        steps: list[Step] = [Step(tr("Leere Systemverzeichnisse anlegen"), ["sh", "-c", 'cd "$1" && mkdir -p proc sys dev run tmp mnt media && chmod 1777 tmp',
                                                                       "mkdirs", self.mount_root])]
        steps += [Step(tr("/{0} einbinden", d), ["sh", "-c", 'mkdir -p "$2" && mount --rbind "$1" "$2" && mount --make-rslave "$2"',
                                          "bind", f"/{d}", f"{self.mount_root}/{d}"]) for d in ("dev", "proc", "sys", "run")]
        chroot = lambda *a: ["chroot", self.mount_root, "env", "LC_ALL=C", *a]
        if self.opts.initramfs:
            conf = root / "etc/initramfs-tools/conf.d"
            conf.mkdir(parents=True, exist_ok=True)
            (conf / "pbsgui-modules").write_text(tr("# von pbsgui beim Wiederherstellen: alle Treiber, damit die neue Hardware bootet\nMODULES=most\n"))
            on_line(tr("initramfs-tools: MODULES=most gesetzt (etc/initramfs-tools/conf.d/pbsgui-modules)"))
            steps.append(Step(tr("Initramfs neu erzeugen"), chroot("update-initramfs", "-u", "-k", "all")))
        if self.opts.bootloader:
            label = _os_id(root) or "debian"
            uefi = self.esp or self.vmcfg.uefi
            if uefi:
                nonvram = [] if self.uefi_live else ["--no-nvram"]
                steps += [Step(tr("GRUB (UEFI) installieren"), chroot("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi",
                                                                  f"--bootloader-id={label}", "--recheck", *nonvram)),
                          Step(tr("GRUB als Fallback-Eintrag installieren (EFI/BOOT/BOOTX64.EFI)"),
                               chroot("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi", "--removable", "--recheck", *nonvram))]
            else:
                steps.append(Step(tr("GRUB (BIOS) installieren"), chroot("grub-install", "--recheck", boot_dev)))
            steps.append(Step(tr("GRUB-Konfiguration erzeugen"), chroot("update-grub")))
        failed = run_steps(steps, on_line, popen=subprocess.Popen)
        for d in ("run", "sys", "proc", "dev"):          # immer wieder lösen, auch nach Fehlern
            subprocess.run(["umount", "-R", "-l", f"{self.mount_root}/{d}"], capture_output=True)
        if failed:
            raise RescueError(tr("Schritt fehlgeschlagen: {0}", failed.title))

    def cleanup(self, on_line: Callable[[str], None]) -> None:
        subprocess.run(["sync"], capture_output=True)
        for d in ("run", "sys", "proc", "dev"):
            subprocess.run(["umount", "-R", "-l", f"{self.mount_root}/{d}"], capture_output=True)
        for m in reversed(self.mounted):
            subprocess.run(["umount", "-l", m], capture_output=True)
        self.mounted.clear()
        for vg in self.vgs:
            subprocess.run(["vgchange", "-an", vg], capture_output=True)
        for name in self.opened:
            for _ in range(3):
                if subprocess.run(["cryptsetup", "close", name], capture_output=True).returncode == 0:
                    break
        self.opened.clear()


def _os_id(root: Path) -> str:
    try:
        m = re.search(r'^ID=(.*)$', (root / "etc/os-release").read_text(), re.M)
    except OSError:
        return ""
    return re.sub(r"[^A-Za-z0-9_-]", "", m.group(1).strip('"')) if m else ""


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def _fstab_mounts(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) >= 2 and not f[0].startswith("#"):
            out.append((f[0], f[1]))
    return out


# -- Netzwerk ------------------------------------------------------------------------------------------------------
def physical_interfaces(sysfs: str = "/sys/class/net") -> list[str]:
    """Schnittstellen mit echter Hardware (haben ein device-Verzeichnis), ohne Loopback, Brücken, VPN und virtuelle Geräte."""
    base = Path(sysfs)
    names = []
    try:
        for p in sorted(base.iterdir()):
            if (p / "device").exists() and not (p / "wireless").exists() and not p.name.startswith(("veth", "docker", "br", "virbr")):
                names.append(p.name)
    except OSError:
        pass
    return names


_NET_FILES = ("etc/network/interfaces", "etc/network/interfaces.d/*", "etc/netplan/*.yaml", "etc/systemd/network/*.network",
              "etc/NetworkManager/system-connections/*.nmconnection")
_NAME_PATTERNS = (re.compile(r"^\s*(?:auto|allow-hotplug|iface)\s+([A-Za-z0-9_.:-]+)", re.M),     # /etc/network/interfaces
                  re.compile(r"^\s{2,}([a-z][a-z0-9]{1,14}):\s*$", re.M),                           # netplan: "  ens3:"
                  re.compile(r"^\s*Name=([A-Za-z0-9_.*-]+)\s*$", re.M),                             # systemd-networkd
                  re.compile(r"^\s*interface-name=([A-Za-z0-9_.-]+)\s*$", re.M))                    # NetworkManager


def _referenced_names(text: str) -> set[str]:
    names: set[str] = set()
    for rx in _NAME_PATTERNS:
        names.update(rx.findall(text))
    names = {n for n in names if not any(ch in n for ch in "*?[")}          # Muster wie `Name=en*` (systemd-networkd) gelten für alle Karten: nie ändern
    return {n for n in names if n not in ("lo", "ethernets", "network", "version", "renderer", "wifis", "bridges", "bonds", "vlans")
            and not n.endswith(":") and not n.startswith(("br", "bond", "wg", "tun", "tap", "docker", "veth", "virbr", "vmbr"))}


def rewrite_network(root: Path, physical: list[str], on_line: Callable[[str], None] = lambda _l: None) -> list[str]:
    """Ersetzt in der Netzwerk-Konfiguration des Gastes Schnittstellennamen, die es auf der neuen Hardware nicht gibt.

    Eindeutig nur, wenn es genau einen verwendeten Namen gibt, der fehlt, und genau eine echte Schnittstelle, die dafür in Frage kommt;
    sonst wird gewarnt und nichts geändert. Geänderte Dateien bekommen eine Sicherung `.pbsgui-orig`. Gibt die Änderungen zurück.
    """
    files = [p for pat in _NET_FILES for p in sorted(root.glob(pat)) if p.is_file()]
    used: set[str] = set()
    for p in files:
        try:
            used |= _referenced_names(p.read_text())
        except OSError:
            continue
    missing = sorted(n for n in used if n not in physical)
    if not missing:
        return []
    free = [n for n in physical if n not in used]
    rules = root / "etc/udev/rules.d/70-persistent-net.rules"
    if rules.is_file():
        rules.rename(rules.with_name(rules.name + ".pbsgui-disabled"))
        on_line(tr("udev: 70-persistent-net.rules deaktiviert (alte MAC-Adressen der VM)"))
    if len(missing) == 1 and len(free) == 1:
        old, new = missing[0], free[0]
        changed: list[str] = []
        for p in files:
            text = p.read_text()
            new_text = re.sub(rf"(?<![A-Za-z0-9_.-]){re.escape(old)}(?![A-Za-z0-9_.-])", new, text)
            if new_text != text:
                shutil.copy2(p, p.with_name(p.name + ".pbsgui-orig"))
                p.write_text(new_text)
                changed.append(str(p.relative_to(root)))
        on_line(tr("Netzwerk: Schnittstelle {0} -> {1} in {2} (Sicherung: *.pbsgui-orig)", old, new, ', '.join(changed) or '-'))
        return changed
    on_line(tr("WARNUNG Netzwerk: die Konfiguration nutzt {0}, hier gibt es {1}. Nicht eindeutig: nichts geändert. Nach dem Start die Namen in /etc/network/interfaces bzw. /etc/netplan anpassen.", ', '.join(missing), ', '.join(physical) or 'keine echte Netzwerkkarte'))
    return []


# -- Gast-Dienste ----------------------------------------------------------------------------------------------------
def disarm_guest_services(root: Path, run: Run = subprocess.run) -> list[str]:
    """qemu-guest-agent und cloud-init abschalten (nichts wird gelöscht; rückgängig mit systemctl enable bzw. Datei löschen)."""
    log: list[str] = []
    for unit in ("qemu-guest-agent.service",):
        if (root / "lib/systemd/system" / unit).exists() or (root / "usr/lib/systemd/system" / unit).exists():
            r = run(["systemctl", f"--root={root}", "disable", unit], capture_output=True, text=True)
            log.append(f"{unit}: " + ("abgeschaltet" if r.returncode == 0 else tr("konnte nicht abgeschaltet werden")))
    if (root / "etc/cloud").is_dir():
        marker = root / "etc/cloud/cloud-init.disabled"
        if not marker.exists():
            marker.write_text(tr("von pbsgui beim Wiederherstellen auf physische Hardware angelegt; zum Aktivieren löschen\n"))
        log.append(tr("cloud-init: abgeschaltet (etc/cloud/cloud-init.disabled)"))
    try:
        grub = (root / "etc/default/grub").read_text()
        if re.search(r"console=ttyS\d", grub):
            log.append(tr("Hinweis: etc/default/grub enthält console=ttyS0 (serielle Konsole der VM); an einem Rechner ohne serielle Konsole entfernen"))
    except OSError:
        pass
    return log or [tr("keine Gast-Dienste gefunden")]


# -- Gesamtablauf ------------------------------------------------------------------------------------------------------
def execute(cfg: RescueConfig, plan: VmPlan, on_line: Callable[[str], None] = print, dry_run: bool = False) -> bool:
    """Führt den Plan aus; die Aufräum-Schritte laufen immer. True = Erfolg."""
    ok = False
    try:
        failed = run_steps(plan.steps, on_line, env=cfg.pbs_env(), dry_run=dry_run)
        ok = failed is None
        if failed:
            on_line(tr("FEHLER in: {0}", failed.title))
    except Exception as e:
        on_line(tr("FEHLER: {0}", e))
    finally:
        run_steps(plan.cleanup, on_line, dry_run=dry_run)
    if ok and not dry_run:
        on_line(tr("Wiederherstellung abgeschlossen. Rettungsmedium entfernen und neu starten."))
    return ok


def parse_mapping(items: list[str]) -> dict[str, str]:
    """`scsi0=/dev/sda` -> {"scsi0": "/dev/sda"}."""
    out: dict[str, str] = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep or not k or not v.startswith("/dev/"):
            raise RescueError(tr("Zuordnung ungültig: '{0}' (erwartet z. B. scsi0=/dev/sda)", it))
        out[k.strip()] = v.strip()
    return out


__all__ = ["VmConfig", "VmDisk", "VmPlan", "RepairOptions", "parse_conf", "vm_disks", "list_vm_snapshots", "fetch_config",
           "boot_disk_key", "validate_mapping", "build_plan", "execute", "parse_mapping", "rewrite_network", "physical_interfaces",
           "disarm_guest_services"]
