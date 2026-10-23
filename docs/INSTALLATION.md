# Installation

pbsgui läuft auf **Debian 13** und **Ubuntu** (25.04 oder neuer empfohlen; auf 24.04 gelten die Hinweise unten).
Du brauchst einen Proxmox Backup Server (PBS) und entweder dessen `root`-Zugang (einmalig, zum Einrichten) oder einen
vorhandenen API-Token.

## Schnellstart: ein Befehl

```bash
curl -fsSL https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh | sudo sh
```

Das Skript [`scripts/install.sh`](../scripts/install.sh) macht alles in einem Rutsch:

1. bindet das signierte **pbsgui-APT-Repository** ein,
2. bindet das **Proxmox-Client-Repository** ein (`proxmox-backup-client`),
3. installiert `pbsgui` samt GUI (Qt), Terminal-Oberfläche, Kommandozeile und den Werkzeugen für Vollbackups
   (`fdisk`, `gdisk`, `efibootmgr`, `fuse3` für den Dateibrowser).

Du kannst das Skript vorher ansehen (empfohlen) und lokal ausführen:

```bash
curl -fsSLO https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh
less install.sh
sudo sh install.sh
```

Optionen (nach `sh install.sh`, beim Pipe-Aufruf mit `sh -s -- …`):

| Option | Wirkung |
|---|---|
| `--no-gui` | ohne Qt-GUI, nur `pbstui` und Kommandozeile (Server ohne Desktop) |
| `--no-client` | `proxmox-backup-client` nicht installieren (schon vorhanden oder anderweitig bereitgestellt) |
| `--version 0.4.0` | bestimmte Version installieren |
| `--uninstall` | pbsgui und das pbsgui-Repository entfernen (Profile unter `~/.config/pbsgui` bleiben) |
| `-y` | apt ohne Rückfragen |

## Manuell

```bash
# 1. pbsgui-Repository
curl -fsSL https://nienhausdavid.github.io/pbsgui/pbsgui.gpg | sudo tee /usr/share/keyrings/pbsgui.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] https://nienhausdavid.github.io/pbsgui stable main" | sudo tee /etc/apt/sources.list.d/pbsgui.list

# 2. Proxmox-Client-Repository
sudo curl -fsSL https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg -o /usr/share/keyrings/proxmox-archive-keyring.gpg
printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pbs-client\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' | sudo tee /etc/apt/sources.list.d/pbs-client.sources

# 3. Installieren
sudo apt update
sudo apt install pbsgui proxmox-backup-client fdisk gdisk efibootmgr fuse3
```

Alternativ das `.deb` aus den [GitHub-Releases](https://github.com/nienhausdavid/pbsgui/releases) laden und mit
`sudo apt install ./pbsgui_<version>_all.deb` installieren.

## Aus dem Quellcode (Entwicklung)

```bash
sudo apt install python3-venv
git clone https://github.com/nienhausdavid/pbsgui && cd pbsgui
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pbsgui                  # GUI
scripts/install-desktop.sh        # Starter im Anwendungsmenü
```

## Aktualisieren und entfernen

```bash
sudo apt update && sudo apt upgrade     # zieht neue pbsgui-Versionen aus dem Repository
sudo sh install.sh --uninstall          # oder: sudo apt remove pbsgui
```

## Erste Einrichtung

1. **Starten:** `pbsgui` (GUI) oder `pbstui` (Terminal).
2. **Server hinzufügen:**
   - *Weg A:* einmalig mit dem `root`-Zugang des PBS. pbsgui legt Benutzer, API-Token, Namespace und minimale
     Rechte an. Das Root-Passwort wird nicht gespeichert.
   - *Weg B:* vorhandenen API-Token eintragen.
   Der TLS-Fingerprint des Servers wird zur Bestätigung angezeigt und danach gepinnt.
3. **Umfang wählen:** *nur Home* (kein sudo nötig) oder *ganzes System* (Administrator-Rechte).
4. **Verschlüsselung:** ist standardmäßig an, der Schlüssel entsteht automatisch. **Exportiere ihn** und bewahre ihn
   getrennt vom Rechner auf (`pbsgui key export NAME DATEI`): ohne ihn sind Backups verloren, wenn der Rechner ausfällt.
   Ein verschlüsseltes Vollbackup startet erst nach diesem Export.
5. **Zeitplan setzen** (GUI: Zeitplan; CLI: `pbsgui schedule NAME --calendar daily --scope user|system`).
6. **Notfallblatt** drucken und die Rettungs-ISO bereithalten: [WIEDERHERSTELLUNG.md](WIEDERHERSTELLUNG.md).

Beispiel komplett auf der Kommandozeile:

```bash
pbsgui add --name Laptop --server pbs.example.org --datastore main --namespace laptop
pbsgui key export Laptop ~/laptop-key.json
pbsgui schedule Laptop --calendar daily --scope user
pbsgui backup Laptop
```

## Hinweise zu Ubuntu und Servern

- **Ubuntu 24.04:** systemd 255, daher speichert pbsgui Geheimnisse für Benutzer-Zeitpläne als Datei (0600) statt mit
  `systemd-creds --user`. Der Proxmox-Client aus dem Debian-13-Repository braucht eine neuere glibc als 24.04 liefert;
  dort `--no-client` verwenden und den Client selbst bereitstellen.
- **Ohne Desktop:** `--no-gui` installieren und `pbstui` verwenden (läuft auch über SSH).
- **Zeitpläne ohne Anmeldung (Benutzer-Modus):** `loginctl enable-linger $USER`.

## Rettungs-ISO

Zum Wiederherstellen eines Vollbackups auf einem neuen PC lädst du `pbsgui-rescue-<version>-amd64.iso` aus den
[Releases](https://github.com/nienhausdavid/pbsgui/releases) und schreibst sie auf einen USB-Stick; die Anleitung mit
Screenshots steht in [WIEDERHERSTELLUNG.md](WIEDERHERSTELLUNG.md).

## Lizenz

[AGPL-3.0-or-later](../LICENSE).
