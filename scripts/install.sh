#!/bin/sh
# Richtet pbsgui auf Debian 13 / Ubuntu komplett ein:
#   1. APT-Repository von pbsgui (signiert) einbinden
#   2. Proxmox-Client-Repository (proxmox-backup-client) einbinden
#   3. pbsgui samt Abhängigkeiten installieren (GUI, TUI, Kommandozeile, systemd-Zeitpläne)
#
# Direkt aus dem Netz:
#   curl -fsSL https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh | sudo sh
# oder mit Optionen:
#   curl -fsSL .../install.sh | sudo sh -s -- --no-gui
#
# Optionen:
#   --no-gui      nur das Paket pbstui (Terminal-Oberfläche und Kommandozeile, ohne Qt; z. B. für Server)
#   --no-client   proxmox-backup-client nicht installieren (z. B. wenn schon vorhanden)
#   --version V   bestimmte pbsgui-Version installieren (z. B. 0.4.0)
#   --uninstall   pbsgui und die beiden Repository-Einträge entfernen (Profile in ~/.config/pbsgui bleiben)
#   -y, --yes     keine Rückfragen von apt
#   -h, --help    diese Hilfe
set -eu

REPO_URL=${PBSGUI_REPO_URL:-https://nienhausdavid.github.io/pbsgui}
PBS_KEY_URL=https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg
PBS_REPO_URL=http://download.proxmox.com/debian/pbs-client

gui=1; client=1; version=""; uninstall=0; yes=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-gui) gui=0 ;;
    --no-client) client=0 ;;
    --version) shift; version=${1:?--version braucht einen Wert} ;;
    --uninstall) uninstall=1 ;;
    -y|--yes) yes="-y" ;;
    -h|--help) sed -n '2,/^set -eu/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unbekannte Option: $1 (siehe --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "Fehler: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "bitte als root ausführen (sudo)."
command -v apt-get >/dev/null 2>&1 || die "apt nicht gefunden: nur Debian und Ubuntu werden unterstützt."

if [ "$uninstall" = 1 ]; then
  say "pbsgui entfernen"
  apt-get remove $yes pbsgui pbstui || true
  rm -f /etc/apt/sources.list.d/pbsgui.list /usr/share/keyrings/pbsgui.gpg
  echo "Proxmox-Client-Repository und proxmox-backup-client bleiben erhalten (apt remove proxmox-backup-client)."
  exit 0
fi

. /etc/os-release
case "${ID:-}" in
  debian)
    [ "${VERSION_ID:-0}" -ge 13 ] 2>/dev/null || die "Debian 13 oder neuer wird benötigt (gefunden: ${PRETTY_NAME:-?})." ;;
  ubuntu)
    case "${VERSION_ID:-}" in
      24.04) echo "Hinweis: Ubuntu 24.04 hat systemd 255. Benutzer-Zeitpläne speichern Geheimnisse dort als Datei (0600) statt mit systemd-creds." ;;
    esac
    if [ "$client" = 1 ] && [ "${VERSION_ID%%.*}" -lt 25 ] 2>/dev/null; then
      echo "Hinweis: Der Proxmox-Client ist für Debian 13 gebaut und braucht eine neue glibc. Auf Ubuntu < 25.04 kann die"
      echo "         Installation fehlschlagen. Dann mit --no-client wiederholen und den Client anderweitig bereitstellen." >&2
    fi ;;
  *) die "nicht unterstützte Distribution: ${PRETTY_NAME:-${ID:-?}} (Debian 13 oder Ubuntu)." ;;
esac

say "Voraussetzungen installieren"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg >/dev/null

say "pbsgui-Repository einbinden ($REPO_URL)"
curl -fsSL "$REPO_URL/pbsgui.gpg" -o /usr/share/keyrings/pbsgui.gpg
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] $REPO_URL stable main" > /etc/apt/sources.list.d/pbsgui.list

if [ "$client" = 1 ]; then
  say "Proxmox-Client-Repository einbinden"
  curl -fsSL "$PBS_KEY_URL" -o /usr/share/keyrings/proxmox-archive-keyring.gpg
  cat > /etc/apt/sources.list.d/pbs-client.sources <<EOF
Types: deb
URIs: $PBS_REPO_URL
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
fi

apt-get update -qq

if [ "$gui" = 1 ]; then
  pkgs="pbsgui${version:+=$version}"          # die GUI zieht pbstui (Terminal-Oberfläche, Kommandozeile) mit
else
  pkgs="pbstui${version:+=$version}"          # nur Terminal-Oberfläche und Kommandozeile, kein Qt
fi
[ "$client" = 1 ] && pkgs="$pkgs proxmox-backup-client"
# Zusätzlich zu den Empfehlungen: Werkzeuge für Vollbackup und Dateibrowser (FUSE)
pkgs="$pkgs fdisk gdisk efibootmgr fuse3"
if [ "$gui" = 0 ]; then
  say "Installiere (ohne Qt-GUI): $pkgs"
  # shellcheck disable=SC2086
  apt-get install $yes $pkgs systemd polkitd pkexec 2>/dev/null \
    || apt-get install $yes $pkgs systemd
else
  say "Installiere: $pkgs"
  # shellcheck disable=SC2086
  apt-get install $yes $pkgs
fi

say "Fertig"
pbsgui --version || true
cat <<'MSG'

Nächste Schritte:
  pbsgui            GUI starten (oder im Anwendungsmenü "pbsgui")
  pbstui            Terminal-Oberfläche
  pbsgui add --help Server und Profil per Kommandozeile einrichten

Anleitung: https://github.com/nienhausdavid/pbsgui/blob/main/docs/INSTALLATION.md
Wichtig bei Vollbackups: Schlüssel exportieren (pbsgui key export NAME DATEI) und sicher aufbewahren.
MSG
