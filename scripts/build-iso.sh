#!/bin/sh
# Baut die bootbare Rettungs-ISO (Debian 13 Live, UEFI + BIOS) mit pbsgui und proxmox-backup-client.
# Aufruf (als root, z. B. per sudo):  scripts/build-iso.sh [AUSGABEVERZEICHNIS] [--profile NAME]
# Das Arbeitsverzeichnis braucht ca. 5 GB und darf NICHT auf einem tmpfs liegen (BUILD_DIR=... setzen).
set -eu
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen (sudo)." >&2; exit 1; }
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-$root/dist}
mkdir -p "$out"; out=$(cd "$out" && pwd)   # absolut: das Skript wechselt später das Verzeichnis
profile=""
[ "${2:-}" = "--profile" ] && profile=${3:-}
work=${BUILD_DIR:-$root/build/iso}
comp=${ISO_COMPRESSION:-zstd}   # zstd = schnell, xz = kleiner (Release)
PATH=$PATH:/usr/sbin:/sbin

mkdir -p "$out" "$work"
rm -rf "$work/config" "$work/auto"
"$root/scripts/build-deb.sh" "$work/deb" >/dev/null
cp -r "$root/iso/config" "$work/config"
mkdir -p "$work/config/packages.chroot"
cp "$work"/deb/pbstui_*_all.deb "$work/config/packages.chroot/"   # nur Terminal-Teil, kein Qt

# Proxmox-Archivschlüssel (Trixie) holen
key="$work/proxmox-archive-keyring.gpg"
[ -s "$key" ] || curl -fsSL https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg -o "$key"
cp "$key" "$work/config/archives/pbs-client.key.chroot"   # live-build legt ihn nach /etc/apt/trusted.gpg.d

# Optional: nicht geheime Verbindungsdaten eines Profils einbauen (Server, Fingerprint, Datastore, Namespace)
if [ -n "$profile" ]; then
  PYTHONPATH="$root/src" python3 - "$profile" "$work/config/includes.chroot/etc/pbsgui/rescue.json" <<'PY'
import json, sys
from pbsgui.core.manager import ProfileManager
name, dest = sys.argv[1], sys.argv[2]
p = next((x for x in ProfileManager().profiles() if x.name == name), None)
if not p:
    sys.exit(f"Profil nicht gefunden: {name}")
json.dump({"server": p.server, "port": p.port, "datastore": p.datastore, "namespace": p.namespace,
           "auth_id": p.auth_id, "fingerprint": p.fingerprint, "backup_id": p.backup_id}, open(dest, "w"), indent=2)
print(f"Verbindungsdaten (ohne Geheimnisse) für '{name}' eingebaut")
PY
fi

cd "$work"
lb clean --purge >/dev/null 2>&1 || true
lb config noauto \
  --distribution trixie --archive-areas "main contrib non-free-firmware" \
  --parent-archive-areas "main contrib non-free-firmware" \
  --binary-images iso-hybrid --bootloaders "grub-efi,syslinux" \
  --debian-installer none --apt-recommends false --memtest none \
  --chroot-squashfs-compression-type "$comp" \
  --iso-application "pbsgui Rettungsmedium" --iso-volume "PBSGUI-RESCUE" --iso-publisher "pbsgui" \
  --bootappend-live "boot=live components quiet noautologin locales=de_DE.UTF-8 keyboard-layouts=de timezone=Europe/Berlin" \
  --security true --updates true
lb build
iso=$(ls -1 "$work"/*.iso 2>/dev/null | head -1)
[ -n "$iso" ] || { echo "Keine ISO erzeugt" >&2; exit 1; }
dest="$out/pbsgui-rescue-$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)-amd64.iso"
mv "$iso" "$dest"
(cd "$out" && sha256sum "$(basename "$dest")" > "$(basename "$dest").sha256")   # relativer Name: prüfbar mit sha256sum -c
echo "$dest"
