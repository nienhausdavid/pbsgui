#!/bin/sh
# Baut zwei Debian-Pakete (reines Python, nutzt die Python-Pakete der Distribution):
#   pbstui_<version>_all.deb  Kern, Kommandozeile (pbsgui), Terminal-Oberfläche (pbstui), Zeitpläne (pbsgui-run); ohne Qt
#   pbsgui_<version>_all.deb  grafische Oberfläche (Qt) und Starter; hängt von pbstui ab, enthält es also mit
# Aufruf: scripts/build-deb.sh [AUSGABEVERZEICHNIS]   (Standard: dist/)
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-$root/dist}
version=${PBSGUI_DEB_VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)}
[ -n "$version" ] || { echo "Version nicht gefunden" >&2; exit 1; }
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$out"
site=usr/lib/python3/dist-packages

# Gemeinsame Dateien eines Pakets: Copyright, Changelog, Prüfsummen, postinst/prerm, Bau
finish() { # finish PAKET
  pkg=$1; d=$stage/${pkg}_${version}_all
  install -d "$d/usr/share/doc/$pkg" "$d/DEBIAN"
  cat > "$d/usr/share/doc/$pkg/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: pbsgui
Source: https://github.com/nienhausdavid/pbsgui

Files: *
Copyright: 2026 David Nienhaus
License: AGPL-3+
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU Affero General Public License as published by the Free
 Software Foundation, either version 3 of the License, or (at your option) any
 later version. The full text is in /usr/share/common-licenses/AGPL-3 .
COPY
  printf '%s (%s) unstable; urgency=low\n\n  * Release %s.\n\n -- David Nienhaus <noreply@users.noreply.github.com>  %s\n' \
    "$pkg" "$version" "$version" "$(date -R)" | gzip -9n > "$d/usr/share/doc/$pkg/changelog.gz"
  cat > "$d/DEBIAN/postinst" <<POST
#!/bin/sh
set -e
if [ "\$1" = configure ] && command -v py3compile >/dev/null 2>&1; then py3compile -p $pkg || true; fi
exit 0
POST
  cat > "$d/DEBIAN/prerm" <<PRE
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then py3clean -p $pkg || true; fi
exit 0
PRE
  chmod 755 "$d/DEBIAN/postinst" "$d/DEBIAN/prerm"
  (cd "$d" && find usr -type f -exec md5sum {} + > DEBIAN/md5sums)
  chmod -R u=rwX,go=rX "$d"
  dpkg-deb --root-owner-group --build "$d" "$out/${pkg}_${version}_all.deb" >/dev/null
  echo "$out/${pkg}_${version}_all.deb"
}

mk() { # mk PAKETVERZEICHNIS NAME MODUL FUNKTION
  printf '#!/usr/bin/python3\nimport sys\nfrom %s import %s as main\nsys.exit(main() or 0)\n' "$3" "$4" > "$1/usr/bin/$2"
  chmod 755 "$1/usr/bin/$2"
}

# --- pbstui: alles außer der Qt-Oberfläche ---------------------------------------------------------------
pkg=pbstui; d=$stage/${pkg}_${version}_all
install -d "$d/$site" "$d/usr/bin"
cp -r "$root/src/pbsgui" "$d/$site/pbsgui"
rm -rf "$d/$site/pbsgui/gui"
find "$d" -name __pycache__ -type d -prune -exec rm -rf {} +
printf '__version__ = "%s"\n' "$version" > "$d/$site/pbsgui/_version.py"   # ohne pip-Metadaten
mk "$d" pbsgui pbsgui.cli.main main
mk "$d" pbstui pbsgui.tui.app run
mk "$d" pbsgui-run pbsgui.cli.run main
d=$stage/${pkg}_${version}_all
install -d "$d/DEBIAN"
cat > "$d/DEBIAN/control" <<CTRL
Package: pbstui
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-textual (>= 0.80), wl-clipboard
Recommends: proxmox-backup-client, systemd, fdisk, gdisk, efibootmgr
Replaces: pbsgui (<< $version)
Breaks: pbsgui (<< $version)
Maintainer: David Nienhaus <noreply@users.noreply.github.com>
Homepage: https://github.com/nienhausdavid/pbsgui
Description: Terminal-Oberfläche und Kommandozeile für den Proxmox Backup Client
 Verwaltet Backups mit proxmox-backup-client ohne Desktop: Server einrichten (als
 root oder mit vorhandenem API-Token), clientseitige Verschlüsselung, Backup,
 Restore, systemd-Zeitpläne (Benutzer oder System) und die geführte
 Wiederherstellung (pbsgui rescue). Enthält pbstui, pbsgui (Kommandozeile) und
 pbsgui-run. Die grafische Oberfläche liefert das Paket pbsgui.
CTRL
finish pbstui

# --- pbsgui: Qt-Oberfläche, enthält pbstui über die Abhängigkeit -------------------------------------------
pkg=pbsgui; d=$stage/${pkg}_${version}_all
install -d "$d/$site/pbsgui/gui" "$d/usr/share/applications" "$d/usr/share/icons/hicolor/scalable/apps" "$d/DEBIAN"
cp "$root"/src/pbsgui/gui/*.py "$d/$site/pbsgui/gui/"
find "$d" -name __pycache__ -type d -prune -exec rm -rf {} +
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
cat > "$d/DEBIAN/control" <<CTRL
Package: pbsgui
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: pbstui (= $version), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets
Maintainer: David Nienhaus <noreply@users.noreply.github.com>
Homepage: https://github.com/nienhausdavid/pbsgui
Description: Grafische Oberfläche für den Proxmox Backup Client
 Qt-Oberfläche zum Einrichten von Servern, für Backups, Zeitpläne, Snapshots und
 den Dateibrowser. Das Paket zieht pbstui mit (Kommandozeile, Terminal-Oberfläche,
 Zeitpläne), die Programme pbsgui, pbstui und pbsgui-run sind danach alle da.
CTRL
finish pbsgui
