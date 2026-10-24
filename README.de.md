# pbsgui

[English](README.md) · **Deutsch**

Grafische Oberfläche für den Proxmox Backup Client (`proxmox-backup-client`).
Plan und Architektur: [docs/PLAN.de.md](docs/PLAN.de.md). Status: **Prototyp**.

Die Oberfläche gibt es auf **Deutsch und Englisch**. Sie folgt der Systemsprache (`LANG`); ausdrücklich wählen mit
`pbsgui language de|en|auto` oder der Umgebungsvariable `PBSGUI_LANG`.

## Was funktioniert (getestet gegen einen Fake-Client)
- Profile mit Quellordnern, Ausschlüssen, Namespace, Verschlüsselungs-Flag
- Backup-Umfang beim Einrichten wählbar: **nur Home** (Standard, kein sudo, Benutzer-Modus) oder **ganzes System** (Dateiebene, braucht Administrator-Rechte, läuft im System-Modus über `pkexec`/systemd). Vollbackup sichert `/` plus die **gewählten Laufwerke** (`--include-dev`): interne lokale Dateisysteme (ext4, xfs, btrfs, vfat …) sind vorgewählt, **USB-Laufwerke** (auch hinter LUKS/LVM) und Mounts unter `/media`, `/mnt`, `/run` nur auf ausdrückliche Wahl, **Netzwerk-Mounts** (NFS, CIFS, sshfs …) und virtuelle Dateisysteme nie, **Bind-Mount-Duplikate** werden nicht doppelt gelesen. Ausgeschlossen sind `/proc`, `/sys`, `/dev`, `/run`, `/tmp` u. Ä.; keine Docker-/Container-Standardausschlüsse. Es ist **kein** Systemabbild
- Server einrichten: Weg A (einmalig als root, legt Benutzer/Token/ACLs an) oder Weg B (vorhandener Token), mit Fingerprint-Pinning
- **Server nachträglich ändern** (Server, Port, Fingerprint, Datastore, Namespace, Token): `pbsgui server NAME …`, GUI „Server ändern …“, TUI `i`; die neuen Angaben werden vor dem Speichern mit dem Client geprüft
- Clientseitige Verschlüsselung standardmäßig an: Key wird automatisch erzeugt (ohne Passphrase, da der Client sie nur per TTY akzeptiert), verschlüsselt im Geheimnis-Speicher abgelegt und nur kurz als 0600-Datei im tmpfs bereitgestellt; Export-Dialog zur Sicherung
- Beim Einrichten: Datastores laden, Namespaces live prüfen (vorhanden / wird angelegt / mit Weg B nicht anlegbar)
- Backup starten mit Live-Log, Snapshots listen, Restore in einen frei gewählten Zielordner
- Zeitpläne als systemd-Timer, wählbar pro Profil: Benutzer- oder System-Units; beim Anlegen direkt auswählbar
- **Nur Geändertes lesen** (Änderungserkennung per Metadaten-Vergleich, spart bei großen, selten geänderten Datenmengen viel Lesezeit)
- Optional **Datenbank-Dumps** laufender Container (Postgres, MariaDB/MySQL) vor dem Backup, als Archiv `dbdumps.pxar`
  (Details und Zugangsdaten: CHANGELOG, `pbsgui dbdump --help`); beim Anlegen auch „erstes Backup direkt starten“
- Geheimnisse verschlüsselt über `systemd-creds` (Fallback: Datei 0600)

## Gegen echten PBS 4.2 geprüft (Token-Anmeldung)
Provisionierung (Benutzer, Token, verschachtelter Namespace, ACLs), Wiederholbarkeit, Backup, Restore
(Inhalt identisch), Isolation (Token sieht fremde Namespaces nicht), Aufräumen.
Erkenntnisse: neue ACLs greifen erst nach 3–8 s (die App wartet darauf); beim Löschen eines Benutzers
bleibt dessen eigene ACL bestehen (die App entfernt sie vorher).

## Noch nicht / nicht verifiziert
- Root-Login per Passwort und 2FA-Ablauf ungeprüft (getestet wurde mit API-Token)
- System-Scope (pkexec-Helfer, System-Units) ist nur per Unit-Test mit Fake-Runner geprüft
- Master-Key-Erzeugung (RSA) und Prune fehlen; Key-Rotation ebenso (ein Key wird nie überschrieben)

## Installation
Ein Befehl (Debian 13 / Ubuntu), Details in [docs/INSTALLATION.md](docs/INSTALLATION.md):
```bash
curl -fsSL https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh | sudo sh
```
Als Debian-Paket aus dem Quellcode (Debian 13; reines Python, nutzt `python3-textual` und `python3-pyside6.*` der Distribution):
```bash
scripts/build-deb.sh dist && sudo apt install ./dist/pbstui_*_all.deb ./dist/pbsgui_*_all.deb   # nur pbstui: ohne Qt, für Server
```
Das signierte APT-Repository liegt auf GitHub Pages (`https://nienhausdavid.github.io/pbsgui`), siehe [docs/RELEASING.de.md](docs/RELEASING.de.md).
Den `proxmox-backup-client` gibt es im Proxmox-Client-Repository (`pbs-client`).

## Einzelne Dateien wiederherstellen (File-Level-Restore)
Dateibrowser für jeden Snapshot: GUI (Snapshots → „Dateien durchsuchen …“), TUI (Snapshot wählen, `f`; Leertaste markiert, `r` in Ordner, `o` überschreiben, `/` suchen) und Kommandozeile:
```bash
pbsgui files NAME [ORDNER] [--search MUSTER]                       # Inhalt ansehen / suchen
pbsgui restore NAME --path /Dokumente --path /notiz.txt --target ~/zurueck   # Auswahl in einen Ordner
pbsgui restore NAME --path /Dokumente --overwrite                 # NUR Home: am Originalort ersetzen (mit Rückfrage)
```
Der Dateibaum kommt aus `catalog dump` (der Client entschlüsselt ihn selbst, geht also auch bei verschlüsselten Backups), die Daten per schreibgeschütztem FUSE-`mount`. **Überschreiben** ist nur für Pfade im eigenen Home-Verzeichnis erlaubt (sonst Abbruch), ersetzt nur vorhandene Dateien, löscht nichts und sichert die alten Fassungen vorher nach `~/.local/share/pbsgui/overwritten/<Zeit>/`. Braucht FUSE (`fusermount3`).

## Über alle Snapshots: Fassungen, Suche, Vergleich, Restore-Test
```bash
pbsgui versions NAME /Dokumente/a.txt [--restore 2 --target ~/zurueck]   # alle Fassungen einer Datei
pbsgui find NAME "*.odt"                                                 # in allen Snapshots suchen, auch Gelöschtes
pbsgui diff NAME [ALT] [NEU]                                             # neu / geändert / gelöscht (Standard: die letzten zwei)
pbsgui preview NAME /Dokumente/a.txt --snapshot 3                        # Inhalt ansehen, ohne wiederherzustellen
pbsgui restore-test NAME --count 5                                       # Stichprobe zurückholen und prüfen (Exitcode 1 bei Fehler)
```
GUI: Dateibrowser mit Vorschau und „Fassungen …“; im Snapshots-Tab „Vergleichen …“, „Restore-Test …“ und „Suche in allen Snapshots …“.

## Überblick: Dashboard, Verlauf, Tray
`pbsgui dashboard` (auch `--json`, `--offline`) zeigt je Profil Status, Dauer, Größe, Zuwachs und die Belegung des Datastores; `pbsgui history`
(`--failed`, `--search TEXT`, `--show LAUF`) die gespeicherten Läufe mit Ausgabe. In der GUI sind das die Tabs „Dashboard“ und „Verlauf“.
`pbsgui tray` startet die GUI als Symbol im Systembereich (Status, „Jetzt sichern“, Fehlermeldungen); im Tray-Menü lässt sich einstellen,
dass sie beim Schließen weiterläuft und beim Anmelden startet.

## Wiederherstellung auf einem neuen PC (Vollbackup)
Stand: Vorbereitung **und** geführte Wiederherstellung (`pbsgui rescue`, Rettungs-ISO) sind eingebaut; Anleitung mit Screenshots: [docs/RESTORE.de.md](docs/RESTORE.de.md).
- Bei jedem Vollbackup (als root) sammelt die App Datenträger-Metadaten nach `/var/lib/pbsgui/recovery/<profil>/` und sichert sie damit im Backup mit: Partitionstabellen (sfdisk/sgdisk), LVM-Metadaten, LUKS-Header (nur bei verschlüsseltem Backup), UUIDs, EFI-Einträge, Paketliste, `MANIFEST.json`.
- **Pflicht:** Ein verschlüsseltes Vollbackup (und sein Zeitplan) startet erst, wenn der Schlüssel exportiert wurde (`pbsgui key export`), denn der Schlüssel im Geheimnis-Speicher ist an den alten Rechner gebunden.
- `pbsgui recovery NAME` zeigt die Checkliste, `--sheet DATEI` schreibt das **Notfallblatt** (Server, Fingerprint, Namespace, Anleitung; ohne Geheimnisse) zum Ausdrucken.
- **Rettungs-ISO** (Debian 13 Live, UEFI + BIOS, `scripts/build-iso.sh`; mit WireGuard, OpenVPN und NetBird für Server hinter einem VPN, aktualisiert sich beim Start): bootet in einen Assistenten, baut GPT/LUKS/LVM/Dateisysteme mit den alten UUIDs nach, spielt das Backup zurück und installiert den Bootloader neu. Beim Wiederherstellen wählbar: LUKS wie im Original, neu verschlüsseln (neue Passphrase) oder ohne Verschlüsselung. Eine **kleinere Ziel-Platte** ist möglich („Größe anpassen“ / `--shrink`): Partitionen und LVs werden auf belegten Platz + Reserve verkleinert, EFI und `/boot` bleiben, alle UUIDs bleiben. Auch als `pbsgui rescue …` (Kommandozeile).
- Grenzen: dateibasiert, kein Abbild; andere Hardware, Secure Boot und TPM-gebundene LUKS-Schlüssel können Handarbeit nötig machen; ein laufendes System wird ohne Snapshot gesichert.

## Bedienung: GUI, CLI, TUI
Alle drei nutzen denselben Core (`pbsgui/core`), beliebig kombinierbar.

| Aufruf | Was |
|---|---|
| `pbsgui` | Qt-GUI (braucht `pip install 'pbsgui[gui]'`) |
| `pbstui` (oder `pbsgui tui`) | Terminal-Oberfläche (Textual), läuft auch über SSH ohne Display |
| `pbsgui list / show / add / server / probe / backup / snapshots / restore / schedule / status / key / remove` | Kommandozeile, skriptbar (`--json` bei `list`, `snapshots`) |

`scripts/install-links.sh` legt `pbsgui` und `pbstui` als Links nach `~/.local/bin` (ab dem nächsten Login im PATH).

Beispiele:
```bash
pbsgui probe --server pbs.example.org --root-user root@pam        # Datastores/Namespaces ansehen
pbsgui add --name Laptop --server pbs.example.org --datastore main --namespace laptop   # Weg A (root, einmalig), nur Home
pbsgui add --name Server --server pbs.example.org --datastore main --mode full             # ganzes System (braucht Admin)
pbsgui mounts                                  # erkannte Laufwerke (intern/USB/Bind) anzeigen
pbsgui mounts Server --set /boot --set /data   # Auswahl eines Vollbackup-Profils ändern (--auto, --none)
pbsgui add --name Laptop --server pbs.example.org --datastore main --token-id user@pbs!tok  # Weg B
pbsgui server Laptop --server pbs2.example.org --fetch-fingerprint --datastore backup   # Server nachträglich ändern
pbsgui key export Laptop ~/laptop-key.json                       # Schlüssel sichern!
pbsgui key copy-kit Laptop                                       # oder: Schlüssel + Notfallblatt in die Zwischenablage (Passwortmanager)
pbsgui schedule Laptop --calendar daily --scope user
pbsgui backup Laptop && pbsgui snapshots Laptop && pbsgui restore Laptop --target ~/restore
pbsgui language de                             # Sprache der Oberfläche wählen (de, en, auto)
```
Geheimnisse werden nie als Argument akzeptiert: Prompt oder `PBSGUI_TOKEN_SECRET` / `PBSGUI_ROOT_PASSWORD`.
TUI-Tasten: a Hinzufügen, b Backup, r Snapshots, t Restore, f Dateien, x Vergleich, w Restore-Test, g Suche in allen Snapshots, e Dashboard, h Verlauf, i Server ändern, s Zeitplan, m Modus, v Verschlüsselung, k Key in Datei sichern, y Key + Notfallblatt in die Zwischenablage kopieren, o Quellen, u Laufwerke, p DB-Dumps, n Notfallblatt, d Entfernen, q Beenden. Im Dateibrowser: p Vorschau, v Fassungen.

## Entwickeln
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # Tests (Fake-Client unter tests/)
sudo env PYTHONPATH=src PATH=$PATH:/usr/sbin .venv/bin/python -m pytest tests/test_rescue_integration.py   # Layout-Nachbau mit echten Loop-Geräten (root)
.venv/bin/pbsgui                  # GUI starten
scripts/install-desktop.sh        # Starter + Icon im Anwendungsmenü installieren (~/.local/share)
```
Texte: Die Quelltexte sind deutsch und zugleich die Schlüssel; die englischen Übersetzungen stehen in `src/pbsgui/i18n/en_*.py`
(`tr("Deutscher Text {0}", wert)`). Ein Test prüft, dass jeder `tr()`-Text eine Übersetzung mit passenden Platzhaltern hat.

Ende-zu-Ende in QEMU (Backup → ISO-Wiederherstellung → Boot): `tests/vm/` (siehe Kopf von `e2e.py`).
Debian/Ubuntu: `sudo apt install python3-venv`, dazu den `proxmox-backup-client` aus dem Proxmox-Client-Repo.

## Lizenz
[AGPL-3.0-or-later](LICENSE).
