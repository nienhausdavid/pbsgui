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
#   -y, --yes     keine Rückfragen von apt (automatisch, wenn stdin kein Terminal ist)
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

# Bei "curl | sh" ist stdin die Pipe: apt könnte nicht antworten und bricht ab.
[ -t 0 ] || yes="-y"

step=0
total=5; [ "$client" = 1 ] || total=4
say() { step=$((step+1)); printf '\n\033[1m==> [%s/%s] %s\033[0m\n' "$step" "$total" "$*"; }
info() { printf '    %s\n' "$*"; }
run() { printf '    \033[2m$ %s\033[0m\n' "$*"; "$@"; }
die() { echo "Fehler: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "bitte als root ausführen (sudo)."
command -v apt-get >/dev/null 2>&1 || die "apt nicht gefunden: nur Debian und Ubuntu werden unterstützt."

if [ "$uninstall" = 1 ]; then
  printf '\n\033[1m==> pbsgui entfernen\033[0m\n'
  # shellcheck disable=SC2086
  run apt-get remove $yes pbsgui pbstui || true
  info "Repository-Eintrag und Schlüssel von pbsgui löschen"
  run rm -fv /etc/apt/sources.list.d/pbsgui.list /usr/share/keyrings/pbsgui.gpg
  echo "Proxmox-Client-Repository und proxmox-backup-client bleiben erhalten (apt remove proxmox-backup-client)."
  exit 0
fi

. /etc/os-release
printf '\033[1mSystem:\033[0m %s, Architektur %s\n' "${PRETTY_NAME:-?}" "$(dpkg --print-architecture 2>/dev/null || uname -m)"
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
info "ca-certificates (HTTPS), curl (Downloads), gnupg (Signaturprüfung)"
run apt-get update
export DEBIAN_FRONTEND=noninteractive
run apt-get install -y ca-certificates curl gnupg

say "pbsgui-Repository einbinden"
info "Quelle: $REPO_URL"
info "Signaturschlüssel  -> /usr/share/keyrings/pbsgui.gpg"
info "Repository-Eintrag -> /etc/apt/sources.list.d/pbsgui.list"
run curl -fsSL "$REPO_URL/pbsgui.gpg" -o /usr/share/keyrings/pbsgui.gpg
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] $REPO_URL stable main" > /etc/apt/sources.list.d/pbsgui.list

if [ "$client" = 1 ]; then
  say "Proxmox-Client-Repository einbinden"
  info "Quelle: $PBS_REPO_URL (Suite trixie, Komponente main)"
  info "Signaturschlüssel  -> /usr/share/keyrings/proxmox-archive-keyring.gpg"
  info "Repository-Eintrag -> /etc/apt/sources.list.d/pbs-client.sources"
  run curl -fsSL "$PBS_KEY_URL" -o /usr/share/keyrings/proxmox-archive-keyring.gpg
  cat > /etc/apt/sources.list.d/pbs-client.sources <<EOF
Types: deb
URIs: $PBS_REPO_URL
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
fi


say "Paketlisten aktualisieren"
run apt-get update

if [ "$gui" = 1 ]; then
  pkgs="pbsgui${version:+=$version}"          # die GUI zieht pbstui (Terminal-Oberfläche, Kommandozeile) mit
else
  pkgs="pbstui${version:+=$version}"          # nur Terminal-Oberfläche und Kommandozeile, kein Qt
fi
[ "$client" = 1 ] && pkgs="$pkgs proxmox-backup-client"
# Zusätzlich zu den Empfehlungen: Werkzeuge für Vollbackup und Dateibrowser (FUSE)
pkgs="$pkgs fdisk gdisk efibootmgr fuse3"
if [ "$gui" = 0 ]; then
  say "Pakete installieren (ohne Qt-GUI)"
  info "Pakete: $pkgs"
  info "pbstui = Terminal-Oberfläche + Kommandozeile, fdisk/gdisk/efibootmgr = Vollbackup, fuse3 = Dateibrowser"
  # shellcheck disable=SC2086
  run apt-get install $yes $pkgs systemd polkitd pkexec 2>/dev/null \
    || { info "polkitd/pkexec nicht verfügbar, installiere ohne"; run apt-get install $yes $pkgs systemd; }
else
  say "Pakete installieren"
  info "Pakete: $pkgs"
  info "pbsgui = Qt-GUI (zieht pbstui mit), fdisk/gdisk/efibootmgr = Vollbackup, fuse3 = Dateibrowser"
  info "Die Qt-Abhängigkeiten sind groß (mehrere hundert MB)."
  # shellcheck disable=SC2086
  run apt-get install $yes $pkgs
fi

printf '\n\033[1m==> Fertig\033[0m\n'
info "Installierte Version:"
pbsgui --version || true
cat <<'MSG'

Nächste Schritte:
  pbsgui            GUI starten (oder im Anwendungsmenü "pbsgui")
  pbstui            Terminal-Oberfläche
  pbsgui add --help Server und Profil per Kommandozeile einrichten

Anleitung: https://github.com/nienhausdavid/pbsgui/blob/main/docs/INSTALLATION.md
Wichtig bei Vollbackups: Schlüssel exportieren (pbsgui key export NAME DATEI) und sicher aufbewahren.
MSG
