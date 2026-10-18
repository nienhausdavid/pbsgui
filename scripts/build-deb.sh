#!/bin/sh
# Baut pbsgui_<version>_all.deb (reines Python, nutzt die Python-Pakete der Distribution).
# Aufruf: scripts/build-deb.sh [AUSGABEVERZEICHNIS]   (Standard: dist/)
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-$root/dist}
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)
[ -n "$version" ] || { echo "Version nicht gefunden" >&2; exit 1; }
pkg=pbsgui
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
d=$stage/${pkg}_${version}_all

install -d "$d/DEBIAN" "$d/usr/lib/python3/dist-packages" "$d/usr/bin" "$d/usr/share/applications" \
  "$d/usr/share/icons/hicolor/scalable/apps" "$d/usr/share/doc/$pkg"
cp -r "$root/src/pbsgui" "$d/usr/lib/python3/dist-packages/pbsgui"
find "$d" -name __pycache__ -type d -prune -exec rm -rf {} +

mk() { # mk NAME MODUL FUNKTION
  printf '#!/usr/bin/python3\nimport sys\nfrom %s import %s as main\nsys.exit(main() or 0)\n' "$2" "$3" > "$d/usr/bin/$1"
  chmod 755 "$d/usr/bin/$1"
}
mk pbsgui pbsgui.cli.main main
mk pbstui pbsgui.tui.app run
mk pbsgui-run pbsgui.cli.run main

install -m 644 "$root/packaging/pbsgui.svg" "$d/usr/share/icons/hicolor/scalable/apps/pbsgui.svg"
cat > "$d/usr/share/applications/pbsgui.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=pbsgui
GenericName=Proxmox Backup
Comment=Grafische Oberfläche für den Proxmox Backup Client
Exec=pbsgui
Icon=pbsgui
Terminal=false
Categories=Utility;Archiving;
Keywords=backup;proxmox;pbs;
StartupWMClass=pbsgui
DESKTOP

cat > "$d/usr/share/doc/$pkg/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: pbsgui
Source: https://github.com/nienhausdavid/pbsgui

Files: *
Copyright: 2026 David Nienhaus
License: UNSPECIFIED
 Lizenz noch nicht festgelegt. Bis dahin sind alle Rechte vorbehalten.
COPY
printf 'pbsgui (%s) unstable; urgency=low\n\n  * Release %s.\n\n -- David Nienhaus <noreply@users.noreply.github.com>  %s\n' \
  "$version" "$version" "$(date -R)" | gzip -9n > "$d/usr/share/doc/$pkg/changelog.gz"

cat > "$d/DEBIAN/control" <<CTRL
Package: $pkg
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-textual (>= 0.80), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets
Recommends: proxmox-backup-client, systemd
Maintainer: David Nienhaus <noreply@users.noreply.github.com>
Homepage: https://github.com/nienhausdavid/pbsgui
Description: GUI, CLI und TUI für den Proxmox Backup Client
 Verwaltet Backups mit proxmox-backup-client: Server einrichten (als root oder
 mit vorhandenem API-Token), clientseitige Verschlüsselung mit automatisch
 erzeugtem Schlüssel, Backup, Restore und systemd-Zeitpläne (Benutzer oder System).
 Enthält die Programme pbsgui (GUI und Kommandozeile), pbstui und pbsgui-run.
CTRL
cat > "$d/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v py3compile >/dev/null 2>&1; then py3compile -p pbsgui || true; fi
exit 0
POST
cat > "$d/DEBIAN/prerm" <<'PRE'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then py3clean -p pbsgui || true; fi
exit 0
PRE
chmod 755 "$d/DEBIAN/postinst" "$d/DEBIAN/prerm"
(cd "$d" && find usr -type f -exec md5sum {} + > DEBIAN/md5sums)

chmod -R u=rwX,go=rX "$d"
mkdir -p "$out"
dpkg-deb --root-owner-group --build "$d" "$out/${pkg}_${version}_all.deb" >/dev/null
echo "$out/${pkg}_${version}_all.deb"
