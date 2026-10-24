#!/bin/sh
# Richtet pbsgui auf Debian 13 / Ubuntu komplett ein (Meldungen deutsch oder englisch je nach LANG):
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

# Sprache der Meldungen: Deutsch, wenn die Systemsprache mit "de" beginnt, sonst Englisch
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in de*) DE=1 ;; *) DE=0 ;; esac
t() { if [ "$DE" = 1 ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }

REPO_URL=${PBSGUI_REPO_URL:-https://nienhausdavid.github.io/pbsgui}
PBS_KEY_URL=https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg
PBS_REPO_URL=http://download.proxmox.com/debian/pbs-client

gui=1; client=1; version=""; uninstall=0; yes=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-gui) gui=0 ;;
    --no-client) client=0 ;;
    --version) shift; version=${1:?"$(t "--version braucht einen Wert" "--version needs a value")"} ;;
    --uninstall) uninstall=1 ;;
    -y|--yes) yes="-y" ;;
    -h|--help) sed -n '2,/^set -eu/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
    *) echo "$(t "Unbekannte Option" "Unknown option"): $1 ($(t "siehe --help" "see --help"))" >&2; exit 2 ;;
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
die() { echo "$(t Fehler Error): $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "$(t "bitte als root ausführen (sudo)." "please run as root (sudo).")"
command -v apt-get >/dev/null 2>&1 || die "$(t "apt nicht gefunden: nur Debian und Ubuntu werden unterstützt." "apt not found: only Debian and Ubuntu are supported.")"

if [ "$uninstall" = 1 ]; then
  printf '\n\033[1m==> %s\033[0m\n' "$(t "pbsgui entfernen" "Removing pbsgui")"
  # shellcheck disable=SC2086
  run apt-get remove $yes pbsgui pbstui || true
  info "$(t "Repository-Eintrag und Schlüssel von pbsgui löschen" "Deleting the repository entry and key of pbsgui")"
  run rm -fv /etc/apt/sources.list.d/pbsgui.list /usr/share/keyrings/pbsgui.gpg
  echo "$(t "Proxmox-Client-Repository und proxmox-backup-client bleiben erhalten (apt remove proxmox-backup-client)." "The Proxmox client repository and proxmox-backup-client are kept (apt remove proxmox-backup-client).")"
  exit 0
fi

. /etc/os-release
printf '\033[1mSystem:\033[0m %s, %s %s\n' "${PRETTY_NAME:-?}" "$(t Architektur architecture)" "$(dpkg --print-architecture 2>/dev/null || uname -m)"
case "${ID:-}" in
  debian)
    [ "${VERSION_ID:-0}" -ge 13 ] 2>/dev/null || die "$(t "Debian 13 oder neuer wird benötigt" "Debian 13 or newer is required") (${PRETTY_NAME:-?})." ;;
  ubuntu)
    case "${VERSION_ID:-}" in
      24.04) echo "$(t "Hinweis: Ubuntu 24.04 hat systemd 255. Benutzer-Zeitpläne speichern Geheimnisse dort als Datei (0600) statt mit systemd-creds." "Note: Ubuntu 24.04 has systemd 255. User schedules store secrets there as a file (0600) instead of with systemd-creds.")" ;;
    esac
    if [ "$client" = 1 ] && [ "${VERSION_ID%%.*}" -lt 25 ] 2>/dev/null; then
      echo "$(t "Hinweis: Der Proxmox-Client ist für Debian 13 gebaut und braucht eine neue glibc. Auf Ubuntu < 25.04 kann die" "Note: The Proxmox client is built for Debian 13 and needs a new glibc. On Ubuntu < 25.04 the")" >&2
      echo "$(t "         Installation fehlschlagen. Dann mit --no-client wiederholen und den Client anderweitig bereitstellen." "         installation may fail. Then repeat with --no-client and provide the client otherwise.")" >&2
    fi ;;
  *) die "$(t "nicht unterstützte Distribution" "unsupported distribution"): ${PRETTY_NAME:-${ID:-?}} ($(t "Debian 13 oder Ubuntu" "Debian 13 or Ubuntu"))." ;;
esac

say "$(t "Voraussetzungen installieren" "Installing prerequisites")"
info "ca-certificates ($(t HTTPS HTTPS)), curl ($(t Downloads downloads)), gnupg ($(t Signaturprüfung "signature check"))"
run apt-get update
export DEBIAN_FRONTEND=noninteractive
run apt-get install -y ca-certificates curl gnupg

say "$(t "pbsgui-Repository einbinden" "Adding the pbsgui repository")"
info "$(t Quelle Source): $REPO_URL"
info "$(t "Signaturschlüssel " "Signing key        ") -> /usr/share/keyrings/pbsgui.gpg"
info "$(t "Repository-Eintrag" "Repository entry  ") -> /etc/apt/sources.list.d/pbsgui.list"
run curl -fsSL "$REPO_URL/pbsgui.gpg" -o /usr/share/keyrings/pbsgui.gpg
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] $REPO_URL stable main" > /etc/apt/sources.list.d/pbsgui.list

if [ "$client" = 1 ]; then
  say "$(t "Proxmox-Client-Repository einbinden" "Adding the Proxmox client repository")"
  info "$(t Quelle Source): $PBS_REPO_URL ($(t "Suite trixie, Komponente main" "suite trixie, component main"))"
  info "$(t "Signaturschlüssel " "Signing key        ") -> /usr/share/keyrings/proxmox-archive-keyring.gpg"
  info "$(t "Repository-Eintrag" "Repository entry  ") -> /etc/apt/sources.list.d/pbs-client.sources"
  run curl -fsSL "$PBS_KEY_URL" -o /usr/share/keyrings/proxmox-archive-keyring.gpg
  cat > /etc/apt/sources.list.d/pbs-client.sources <<EOF
Types: deb
URIs: $PBS_REPO_URL
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
fi


say "$(t "Paketlisten aktualisieren" "Updating the package lists")"
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
  say "$(t "Pakete installieren (ohne Qt-GUI)" "Installing packages (without the Qt GUI)")"
  info "$(t Pakete Packages): $pkgs"
  info "$(t "pbstui = Terminal-Oberfläche + Kommandozeile, fdisk/gdisk/efibootmgr = Vollbackup, fuse3 = Dateibrowser" "pbstui = terminal interface + command line, fdisk/gdisk/efibootmgr = full backup, fuse3 = file browser")"
  # shellcheck disable=SC2086
  run apt-get install $yes $pkgs systemd polkitd pkexec 2>/dev/null \
    || { info "$(t "polkitd/pkexec nicht verfügbar, installiere ohne" "polkitd/pkexec not available, installing without")"; run apt-get install $yes $pkgs systemd; }
else
  say "$(t "Pakete installieren" "Installing packages")"
  info "$(t Pakete Packages): $pkgs"
  info "$(t "pbsgui = Qt-GUI (zieht pbstui mit), fdisk/gdisk/efibootmgr = Vollbackup, fuse3 = Dateibrowser" "pbsgui = Qt GUI (pulls in pbstui), fdisk/gdisk/efibootmgr = full backup, fuse3 = file browser")"
  info "$(t "Die Qt-Abhängigkeiten sind groß (mehrere hundert MB)." "The Qt dependencies are large (several hundred MB).")"
  # shellcheck disable=SC2086
  run apt-get install $yes $pkgs
fi

printf '\n\033[1m==> %s\033[0m\n' "$(t Fertig Done)"
info "$(t "Installierte Version:" "Installed version:")"
pbsgui --version || true
if [ "$DE" = 1 ]; then
cat <<'MSG'

Nächste Schritte:
  pbsgui            GUI starten (oder im Anwendungsmenü "pbsgui")
  pbstui            Terminal-Oberfläche
  pbsgui add --help Server und Profil per Kommandozeile einrichten

Anleitung: https://github.com/nienhausdavid/pbsgui/blob/main/docs/INSTALLATION.de.md
Wichtig bei Vollbackups: Schlüssel exportieren (pbsgui key export NAME DATEI) und sicher aufbewahren.
MSG
else
cat <<'MSG'

Next steps:
  pbsgui            start the GUI (or "pbsgui" in the application menu)
  pbstui            terminal interface
  pbsgui add --help set up a server and profile on the command line

Guide: https://github.com/nienhausdavid/pbsgui/blob/main/docs/INSTALLATION.md
Important for full backups: export the key (pbsgui key export NAME FILE) and keep it safe.
MSG
fi
