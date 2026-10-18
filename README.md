# pbsgui

Grafische Oberfläche für den Proxmox Backup Client (`proxmox-backup-client`).
Plan und Architektur: [docs/PLAN.md](docs/PLAN.md). Status: **Prototyp**.

## Was funktioniert (getestet gegen einen Fake-Client)
- Profile mit Quellordnern, Ausschlüssen, Namespace, Verschlüsselungs-Flag
- Backup-Umfang beim Einrichten wählbar: **nur Home** (Standard, kein sudo, Benutzer-Modus) oder **ganzes System** (Dateiebene, braucht Administrator-Rechte, läuft im System-Modus über `pkexec`/systemd). Vollbackup sichert `/` plus die **gewählten Laufwerke** (`--include-dev`): interne lokale Dateisysteme (ext4, xfs, btrfs, vfat …) sind vorgewählt, **USB-Laufwerke** (auch hinter LUKS/LVM) und Mounts unter `/media`, `/mnt`, `/run` nur auf ausdrückliche Wahl, **Netzwerk-Mounts** (NFS, CIFS, sshfs …) und virtuelle Dateisysteme nie, **Bind-Mount-Duplikate** werden nicht doppelt gelesen. Ausgeschlossen sind `/proc`, `/sys`, `/dev`, `/run`, `/tmp` u. Ä.; keine Docker-/Container-Standardausschlüsse. Es ist **kein** Systemabbild
- Server einrichten: Weg A (einmalig als root, legt Benutzer/Token/ACLs an) oder Weg B (vorhandener Token), mit Fingerprint-Pinning
- Clientseitige Verschlüsselung standardmäßig an: Key wird automatisch erzeugt (ohne Passphrase, da der Client sie nur per TTY akzeptiert), verschlüsselt im Geheimnis-Speicher abgelegt und nur kurz als 0600-Datei im tmpfs bereitgestellt; Export-Dialog zur Sicherung
- Beim Einrichten: Datastores laden, Namespaces live prüfen (vorhanden / wird angelegt / mit Weg B nicht anlegbar)
- Backup starten mit Live-Log, Snapshots listen, Restore in einen frei gewählten Zielordner
- Zeitpläne als systemd-Timer, wählbar pro Profil: Benutzer- oder System-Units
- Geheimnisse verschlüsselt über `systemd-creds` (Fallback: Datei 0600)

## Gegen echten PBS 4.2 geprüft (Token-Anmeldung)
Provisionierung (Benutzer, Token, verschachtelter Namespace, ACLs), Wiederholbarkeit, Backup, Restore
(Inhalt identisch), Isolation (Token sieht fremde Namespaces nicht), Aufräumen.
Erkenntnisse: neue ACLs greifen erst nach 3–8 s (die App wartet darauf); beim Löschen eines Benutzers
bleibt dessen eigene ACL bestehen (die App entfernt sie vorher).

## Noch nicht / nicht verifiziert
- Root-Login per Passwort und 2FA-Ablauf ungeprüft (getestet wurde mit API-Token)
- System-Scope (pkexec-Helfer, System-Units) ist nur per Unit-Test mit Fake-Runner geprüft
- Master-Key-Erzeugung (RSA), Prune, Katalog-Browser fehlen; Key-Rotation ebenso (ein Key wird nie überschrieben)
- Keine Lizenz festgelegt

## Installation
Als Debian-Paket (Debian 13; reines Python, nutzt `python3-textual` und `python3-pyside6.*` der Distribution):
```bash
scripts/build-deb.sh dist && sudo apt install ./dist/pbsgui_*_all.deb
```
Ein signiertes APT-Repository über GitHub Pages ist vorbereitet, siehe [docs/RELEASING.md](docs/RELEASING.md).
Den `proxmox-backup-client` gibt es im Proxmox-Client-Repository (`pbs-client`).

## Wiederherstellung auf einem neuen PC (Vollbackup)
Stand: Vorbereitung **und** geführte Wiederherstellung (`pbsgui rescue`, Rettungs-ISO) sind eingebaut; Anleitung mit Screenshots: [docs/WIEDERHERSTELLUNG.md](docs/WIEDERHERSTELLUNG.md).
- Bei jedem Vollbackup (als root) sammelt die App Datenträger-Metadaten nach `/var/lib/pbsgui/recovery/<profil>/` und sichert sie damit im Backup mit: Partitionstabellen (sfdisk/sgdisk), LVM-Metadaten, LUKS-Header (nur bei verschlüsseltem Backup), UUIDs, EFI-Einträge, Paketliste, `MANIFEST.json`.
- **Pflicht:** Ein verschlüsseltes Vollbackup (und sein Zeitplan) startet erst, wenn der Schlüssel exportiert wurde (`pbsgui key export`), denn der Schlüssel im Geheimnis-Speicher ist an den alten Rechner gebunden.
- `pbsgui recovery NAME` zeigt die Checkliste, `--sheet DATEI` schreibt das **Notfallblatt** (Server, Fingerprint, Namespace, Anleitung; ohne Geheimnisse) zum Ausdrucken.
- **Rettungs-ISO** (Debian 13 Live, UEFI + BIOS, `scripts/build-iso.sh`): bootet in einen Assistenten, baut GPT/LUKS/LVM/Dateisysteme mit den alten UUIDs nach, spielt das Backup zurück und installiert den Bootloader neu. Beim Wiederherstellen wählbar: LUKS wie im Original, neu verschlüsseln (neue Passphrase) oder ohne Verschlüsselung. Auch als `pbsgui rescue …` (Kommandozeile).
- Grenzen: dateibasiert, kein Abbild; andere Hardware, Secure Boot und TPM-gebundene LUKS-Schlüssel können Handarbeit nötig machen; ein laufendes System wird ohne Snapshot gesichert.

## Bedienung: GUI, CLI, TUI
Alle drei nutzen denselben Core (`pbsgui/core`), beliebig kombinierbar.

| Aufruf | Was |
|---|---|
| `pbsgui` | Qt-GUI (braucht `pip install 'pbsgui[gui]'`) |
| `pbstui` (oder `pbsgui tui`) | Terminal-Oberfläche (Textual), läuft auch über SSH ohne Display |
| `pbsgui list / show / add / probe / backup / snapshots / restore / schedule / status / key / remove` | Kommandozeile, skriptbar (`--json` bei `list`, `snapshots`) |

`scripts/install-links.sh` legt `pbsgui` und `pbstui` als Links nach `~/.local/bin` (ab dem nächsten Login im PATH).

Beispiele:
```bash
pbsgui probe --server pbs.example.org --root-user root@pam        # Datastores/Namespaces ansehen
pbsgui add --name Laptop --server pbs.example.org --datastore main --namespace laptop   # Weg A (root, einmalig), nur Home
pbsgui add --name Server --server pbs.example.org --datastore main --mode full             # ganzes System (braucht Admin)
pbsgui mounts                                  # erkannte Laufwerke (intern/USB/Bind) anzeigen
pbsgui mounts Server --set /boot --set /data   # Auswahl eines Vollbackup-Profils ändern (--auto, --none)
pbsgui add --name Laptop --server pbs.example.org --datastore main --token-id user@pbs!tok  # Weg B
pbsgui key export Laptop ~/laptop-key.json                       # Schlüssel sichern!
pbsgui schedule Laptop --calendar daily --scope user
pbsgui backup Laptop && pbsgui snapshots Laptop && pbsgui restore Laptop --target ~/restore
```
Geheimnisse werden nie als Argument akzeptiert: Prompt oder `PBSGUI_TOKEN_SECRET` / `PBSGUI_ROOT_PASSWORD`.
TUI-Tasten: a Hinzufügen, b Backup, r Snapshots, t Restore, s Zeitplan, m Modus, v Verschlüsselung, k Key sichern, o Quellen, u Laufwerke, n Notfallblatt, d Entfernen, q Beenden.

## Entwickeln
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # Tests (Fake-Client unter tests/)
sudo env PYTHONPATH=src PATH=$PATH:/usr/sbin .venv/bin/python -m pytest tests/test_rescue_integration.py   # Layout-Nachbau mit echten Loop-Geräten (root)
.venv/bin/pbsgui                  # GUI starten
scripts/install-desktop.sh        # Starter + Icon im Anwendungsmenü installieren (~/.local/share)
```
Ende-zu-Ende in QEMU (Backup → ISO-Wiederherstellung → Boot): `tests/vm/` (siehe Kopf von `e2e.py`).
Debian/Ubuntu: `sudo apt install python3-venv`, dazu den `proxmox-backup-client` aus dem Proxmox-Client-Repo.
