"""Backup-Umfang: nur das eigene Home-Verzeichnis oder das ganze System (dateibasiert).

Beim Vollbackup bestimmt der Benutzer, welche eingehängten Laufwerke mitgesichert werden. Erkannt werden
lokale Dateisysteme über /proc/self/mountinfo, dazu USB-Laufwerke (über sysfs) und Bind-Mounts (Duplikate).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

HOME = "home"
FULL = "full"

# Pseudo- und flüchtige Verzeichnisse (liegen meist auf anderen Dateisystemen und würden ohnehin übersprungen;
# die Muster sind eine zusätzliche Absicherung) sowie Swap-Dateien. Bewusst KEINE Container-/Docker-Verzeichnisse
# und nicht /mnt, /media: dort eingehängte Laufwerke wählt der Benutzer selbst aus.
DEFAULT_FULL_EXCLUDES = ["/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/lost+found", "/swapfile",  # noqa: S108  # nosec B108
                         "/swap.img", "/var/cache/apt/archives"]  # Ausschlussliste, keine Temp-Nutzung

# (S108/B108 unten: /tmp usw. sind hier Ausschlussmuster, keine Nutzung von Temp-Verzeichnissen.)
# Der Client geht standardmäßig nicht über Dateisystemgrenzen. Nur diese lokalen Typen kommen als Auswahl in
# Frage; Netzwerk- (nfs, cifs, sshfs …), virtuelle (tmpfs, overlay …) und Pseudo-Dateisysteme nie.
LOCAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "jfs", "reiserfs", "zfs", "exfat", "ntfs3"}
_HIDDEN_PREFIXES = ("/proc", "/sys", "/dev", "/tmp", "/var/tmp", "/snap")  # noqa: S108  # nosec B108
# Mounts, die ohne ausdrückliche Auswahl NICHT mitkommen (Wechselmedien, manuelle Mounts)
_OPT_IN_PREFIXES = ("/media", "/mnt", "/run")
_SUBVOL_FS = {"btrfs", "zfs"}  # Subvolumes haben root != "/" und sind trotzdem keine Bind-Mounts


@dataclass
class MountInfo:
    mountpoint: str
    fstype: str
    source: str
    usb: bool = False
    bind: bool = False  # Duplikat eines bereits eingehängten Verzeichnisses

    @property
    def default_selected(self) -> bool:
        """Standardmäßig mitsichern: interne Laufwerke außerhalb von /media, /mnt, /run, keine Duplikate."""
        return not (self.usb or self.bind or any(
            self.mountpoint == p or self.mountpoint.startswith(p + "/") for p in _OPT_IN_PREFIXES))

    @property
    def kind(self) -> str:
        return "Bind-Mount (Duplikat)" if self.bind else ("USB-Laufwerk" if self.usb else "internes Laufwerk")


def _unescape(path: str) -> str:
    # mountinfo maskiert Leerzeichen & Co. als \040 usw.
    out, i = [], 0
    while i < len(path):
        octal = path[i + 1:i + 4]
        if path[i] == "\\" and len(octal) == 3 and octal.isdigit():
            out.append(chr(int(octal, 8)))
            i += 4
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def is_usb(source: str, sys_class_block: str = "/sys/class/block", _depth: int = 0) -> bool:
    """True, wenn das Blockgerät (auch hinter LUKS/LVM) an einem USB-Bus hängt."""
    if _depth > 5 or not source.startswith("/dev/"):
        return False
    name = os.path.basename(os.path.realpath(source)) if os.path.exists(source) else os.path.basename(source)
    node = os.path.join(sys_class_block, name)
    if re.search(r"/usb\d+/", os.path.realpath(node) + "/"):
        return True
    slaves = os.path.join(node, "slaves")  # dm-crypt/LVM: darunterliegende Geräte prüfen
    try:
        return any(is_usb("/dev/" + s, sys_class_block, _depth + 1) for s in os.listdir(slaves))
    except OSError:
        return False


def detect_mounts(mountinfo: str | None = None, usb_check=None) -> list[MountInfo]:
    """Eingehängte lokale Dateisysteme als Auswahl für `--include-dev` (ohne `/`)."""
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo") as f:
                mountinfo = f.read()
        except OSError:
            return []
    usb_check = usb_check or is_usb
    seen: dict[tuple[str, str], str] = {}   # (major:minor, root) -> erster Einhängepunkt
    devices: dict[str, str] = {}            # major:minor -> erster Einhängepunkt
    out: list[MountInfo] = []
    for line in mountinfo.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        lt, rt = left.split(), right.split()
        if len(lt) < 6 or len(rt) < 2:
            continue
        majmin, root, mountpoint = lt[2], _unescape(lt[3]), _unescape(lt[4])
        fstype, source = rt[0], _unescape(rt[1])
        dup = (majmin, root) in seen or (root != "/" and fstype not in _SUBVOL_FS and majmin in devices)
        seen.setdefault((majmin, root), mountpoint)
        devices.setdefault(majmin, mountpoint)
        if mountpoint == "/" or fstype not in LOCAL_FS:
            continue
        if any(mountpoint == p or mountpoint.startswith(p + "/") for p in _HIDDEN_PREFIXES):
            continue
        if any(m.mountpoint == mountpoint for m in out):
            continue
        out.append(MountInfo(mountpoint, fstype, source, usb=usb_check(source), bind=dup))
    return out


def plan_mounts(selected: list[str] | None, detected: list[MountInfo] | None = None) -> tuple[list[str], list[str]]:
    """(einzubeziehende Mounts, auszuschließende Bind-Mount-Duplikate).

    selected=None: Standardauswahl (interne Laufwerke). Nicht mehr eingehängte oder doppelte Einträge entfallen.
    """
    detected = detect_mounts() if detected is None else detected
    real = [m for m in detected if not m.bind]
    chosen = [m.mountpoint for m in real if (m.default_selected if selected is None else m.mountpoint in selected)]
    return chosen, [m.mountpoint for m in detected if m.bind]


def mount_choices(selected: list[str] | None, detected: list[MountInfo] | None = None) -> list[tuple[MountInfo, bool]]:
    """Erkannte Mounts mit Auswahlzustand für Listen in GUI/TUI (Bind-Duplikate nie angehakt)."""
    detected = detect_mounts() if detected is None else detected
    return [(m, False if m.bind else (m.default_selected if selected is None else m.mountpoint in selected))
            for m in detected]


def describe(mode: str) -> str:
    return "Vollständiges System (Dateiebene)" if mode == FULL else "Nur das eigene Home-Verzeichnis"
