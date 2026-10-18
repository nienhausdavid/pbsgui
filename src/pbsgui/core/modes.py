"""Backup-Umfang: nur das eigene Home-Verzeichnis oder das ganze System (dateibasiert)."""
from __future__ import annotations

HOME = "home"
FULL = "full"

# Vollbackup: Pseudo- und flüchtige Verzeichnisse (die liegen meist auf anderen Dateisystemen und würden
# ohnehin übersprungen; die Muster sind eine zusätzliche Absicherung) und Wechselmedien/Swap
DEFAULT_FULL_EXCLUDES = ["/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/mnt", "/media",
                         "/lost+found", "/swapfile", "/swap.img", "/var/cache/apt/archives"]

# Der Client geht standardmäßig nicht über Dateisystemgrenzen. Einzubeziehen sind lokale, dauerhafte
# Dateisysteme (z. B. /boot, /boot/efi, getrenntes /home), nicht Netzlaufwerke, tmpfs oder Wechselmedien.
LOCAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "jfs", "reiserfs", "zfs"}
_SKIP_PREFIXES = ("/media", "/mnt", "/run", "/tmp", "/var/tmp", "/proc", "/sys", "/dev", "/snap")


def _unescape(path: str) -> str:
    # /proc/mounts maskiert Leerzeichen & Co. als \040 usw.
    out, i = [], 0
    while i < len(path):
        if path[i] == "\\" and path[i + 1:i + 4].isdigit() and len(path[i + 1:i + 4]) == 3:
            out.append(chr(int(path[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def local_mounts(proc_mounts: str | None = None) -> list[str]:
    """Einhängepunkte lokaler Dateisysteme unterhalb von `/` (ohne `/` selbst), für `--include-dev`."""
    if proc_mounts is None:
        try:
            with open("/proc/mounts") as f:
                proc_mounts = f.read()
        except OSError:
            return []
    found: list[str] = []
    for line in proc_mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint, fstype = _unescape(parts[1]), parts[2]
        if mountpoint == "/" or fstype not in LOCAL_FS or mountpoint in found:
            continue
        if any(mountpoint == p or mountpoint.startswith(p + "/") for p in _SKIP_PREFIXES):
            continue
        found.append(mountpoint)
    return found


def describe(mode: str) -> str:
    return "Vollständiges System (Dateiebene)" if mode == FULL else "Nur das eigene Home-Verzeichnis"
