#!/bin/sh
# Installiert Starter und Icon für den aktuellen Benutzer (~/.local/share).
# Aufruf aus dem Projektordner nach `pip install -e .`: scripts/install-desktop.sh [pfad/zu/pbsgui]
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
exe=${1:-$root/.venv/bin/pbsgui}
[ -x "$exe" ] || { echo "Nicht gefunden/ausführbar: $exe" >&2; exit 1; }
apps=${XDG_DATA_HOME:-$HOME/.local/share}/applications
icons=${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps
mkdir -p "$apps" "$icons"
install -m 644 "$root/packaging/pbsgui.svg" "$icons/pbsgui.svg"
cat > "$apps/pbsgui.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=pbsgui
GenericName=Proxmox Backup
Comment=Grafische Oberfläche für den Proxmox Backup Client
Exec="$exe"
Icon=pbsgui
Terminal=false
Categories=Utility;Archiving;
Keywords=backup;proxmox;pbs;
StartupWMClass=pbsgui
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "${icons%/scalable/apps}" || true
echo "Installiert: $apps/pbsgui.desktop"
