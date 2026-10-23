# Changelog

Alle nennenswerten Änderungen stehen hier. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionen nach [Semantic Versioning](https://semver.org/lang/de/): `MAJOR.MINOR.PATCH`
(MAJOR = inkompatible Änderung, MINOR = neue Funktion, PATCH = Fehlerbehebung).
Version 0.x: die Schnittstellen (Profilformat, Befehle) können sich noch ändern.

## [Unreleased]

## [0.4.0] - 2026-10-23

### Hinzugefügt
- **Lizenz:** AGPL-3.0-or-later (`LICENSE`, Paket-Metadaten, Debian-`copyright`).
- **Installation mit einem Befehl:** `scripts/install.sh` richtet APT-Repositories und alle Pakete ein (`--no-gui`, `--no-client`,
  `--version`, `--uninstall`); neue Anleitung `docs/INSTALLATION.md`.
- Signiertes APT-Repository auf GitHub Pages (`https://nienhausdavid.github.io/pbsgui`).
- **Fortschritt beim Backup:** Balken mit Prozent, Menge, Geschwindigkeit und Restzeit in GUI (Backup-Tab), TUI
  (Log-Tab) und Kommandozeile (`pbsgui backup`). Der Client gibt beim Backup selbst keinen Fortschritt aus; die App misst
  den Lesezähler des Clients und vergleicht ihn mit der Größe des letzten Backups (beim ersten Vollbackup mit dem
  belegten Platz, beim ersten Home-Backup unbestimmt). Gegen den echten Server geprüft: 480 von 480 MB gelesen.

### Behoben
- Release-Workflow: Die ISO wird in einem Debian-13-Container gebaut (der `live-build` der Ubuntu-Runner war zu alt);
  mit `workflow_dispatch` und `tag=vX.Y.Z` lässt sich eine ISO nachträglich an ein vorhandenes Release hängen.
- `.sha256`-Datei der ISO enthält den Dateinamen ohne Pfad.
- `pbsgui-run` und `pbsgui backup` geben Ausgaben sofort aus (vorher gepuffert, wenn sie in eine Pipe liefen).

## [0.3.0] - 2026-10-18

Erste veröffentlichte Version. (Die Zwischenstände 0.1.0 und 0.2.0 waren nur interne Entwicklungsstände.)

### Hinzugefügt
- **Oberflächen:** Qt-GUI (`pbsgui`), Terminal-Oberfläche (`pbstui` / `pbsgui tui`) und Kommandozeile
  (`pbsgui list|show|add|probe|mounts|backup|snapshots|files|restore|schedule|status|key|recovery|rescue|remove`).
- **Einrichten:** Weg A (einmalig als root: Benutzer, Token, Namespace und ACLs werden angelegt) oder Weg B
  (vorhandener API-Token); Fingerprint-Pinning, Datastore- und Namespace-Prüfung.
- **Backup:** Home oder ganzes System (Dateiebene), wählbare Laufwerke (intern/USB/Bind-Duplikate erkannt),
  clientseitige Verschlüsselung standardmäßig an mit automatisch erzeugtem Schlüssel.
- **Zeitpläne:** systemd-Timer als Benutzer- oder System-Units; Geheimnisse über `systemd-creds`.
- **Wiederherstellung auf neuem PC:** Datenträger-Metadaten und Notfallblatt, Pflicht-Schlüsselexport,
  Rettungs-ISO (Debian 13, UEFI/BIOS) mit geführtem Assistenten (`pbsgui rescue`); LUKS wie im Original,
  neu verschlüsselt oder ohne Verschlüsselung.
- **Einzelne Dateien wiederherstellen:** Dateibrowser (GUI/TUI/CLI) mit Suche; Überschreiben am Originalort nur im
  Home-Verzeichnis, mit Sicherung der alten Dateien.
- **Paketierung:** `.deb`-Paket, signiertes APT-Repository-Skript, GitHub-Workflows (CI, Release, ISO).
- `pbsgui --version` und Versionsanzeige in GUI und TUI.

### Bekannte Einschränkungen
- Nicht auf echter Hardware, mit BIOS-Boot der ISO, Secure Boot oder WLAN getestet (Tests liefen in QEMU/UEFI).
- System-Scope-Zeitpläne und Root-Passwort-Login mit 2FA sind nur eingeschränkt praktisch getestet.
- Die Rettungs-ISO bringt kein FUSE mit; der Dateibrowser dort ist nicht vorgesehen.

[Unreleased]: https://github.com/nienhausdavid/pbsgui/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.4.0
[0.3.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.3.0
