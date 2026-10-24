# Changelog

Alle nennenswerten Änderungen stehen hier. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionen nach [Semantic Versioning](https://semver.org/lang/de/): `MAJOR.MINOR.PATCH`
(MAJOR = inkompatible Änderung, MINOR = neue Funktion, PATCH = Fehlerbehebung).
Version 0.x: die Schnittstellen (Profilformat, Befehle) können sich noch ändern.

## [Unreleased]

## [0.8.1] - 2026-10-24

### Hinzugefügt
- **Dark Mode** in der GUI: Umschalter oben rechts mit „System“ (folgt dem Desktop, auch bei Wechsel zur Laufzeit), „Hell“ und
  „Dunkel“; die Wahl wird gemerkt.
- **Eigene Titelleiste mit Minimieren, Maximieren/Wiederherstellen und Schließen** oben rechts (GNOME zeigt Minimieren und
  Maximieren sonst nicht an). Verschieben per Ziehen der Leiste, Doppelklick maximiert, Vergrößern an allen Rändern
  und Ecken. Mit `PBSGUI_NATIVE_FRAME=1` bleibt der Rahmen des Systems.

### Geändert
- **Höherer Kontrast** in der GUI: Text mindestens 7:1, Nebentext mindestens 4,5:1, Rahmen mindestens 3:1 (per Test
  abgesichert); Haken, Auswahl und Tabs deutlicher, einheitliche Farben auch in Dialogen.

## [0.8.0] - 2026-10-24

### Hinzugefügt
- **Wiederherstellung auf einer kleineren Platte** („Größe anpassen“; Assistent: Haken bei zu kleiner Platte,
  `pbsgui rescue run --shrink`, interaktiv wird es angeboten). EFI und `/boot` behalten ihre Größe, Swap wird
  proportional (mind. 1 GiB) verkleinert, die übrigen Partitionen und LVs teilen sich den Rest proportional, aber nie
  unter belegtem Platz + 20 % Reserve (mind. 2 GiB); sonst Fehlermeldung mit der nötigen Mindestgröße. UUIDs bleiben
  erhalten, die LVM-Metadaten werden für das kleinere Physical Volume umgeschrieben, die Partitionstabelle neu
  geschrieben (GPT-Backup-Header am Plattenende). Der Plan zeigt alt -> neu. Funktioniert mit allen drei
  Verschlüsselungs-Modi. Nicht unterstützt: VGs über mehrere Platten, Thin-Pools/Snapshots/RAID, erweiterte MBR-Partitionen.
  Geprüft in QEMU/UEFI mit einem echten Vollbackup (477-GB-Laptop, 33 GiB belegt) auf eine 64-GB-Platte: alle UUIDs
  gleich, das System bootet bis zur Anmeldung.
- Vollbackups legen den belegten Platz je Einhängepunkt als `fsusage.json` in den Wiederherstellungsdaten ab (Grundlage
  für „Größe anpassen“; bei älteren Backups wird er aus der Snapshot-Größe geschätzt).

### Geändert
- **GUI schneller:** Der Start dauerte ca. 4 s, weil das Geheimnis des Schlüssels zweimal per `systemd-creds` entschlüsselt wurde,
  nur um „Schlüssel vorhanden?“ anzuzeigen. Jetzt genügt ein Dateicheck (`SecretStore.exists`); Start und Profilwechsel
  dauern ca. 0,1 s.
- **GUI überarbeitet:** einheitliches Aussehen (Fusion-Stil, Stylesheet auf Basis der Systempalette, hell und dunkel),
  Kopfzeile mit Server und Umfang, Einstellungen in Gruppen und scrollbar, feste Aktionsleiste („Jetzt sichern“),
  Protokoll in eigenem, verschiebbarem Bereich. Das Fenster lässt sich auf ca. 700 × 490 verkleinern (vorher
  mindestens 780 × 780) und merkt sich Größe und Lage.

## [0.7.0] - 2026-10-24

### Hinzugefügt
- **Passwortloser Dump-Zugang für MariaDB, ganz ohne Root-Passwort:** `pbsgui dbdump NAME --setup-access CONTAINER` (TUI:
  Knopf „Ohne Passwort einrichten“ in `p`, GUI: gleicher Knopf im Dialog „Datenbanken und Zugänge“) legt im Container
  einen Benutzer `mysql` mit der Anmeldeart `unix_socket` an (nur Leserechte; gilt nur für den gleichnamigen
  Betriebssystem-Benutzer im Container, also für alle, die `docker exec` dürfen, wie bei Postgres). Dafür wird kein
  Passwort gebraucht und keines gespeichert: Der Container wird einmalig für etwa 15 bis 40 Sekunden sauber gestoppt,
  ein Wegwerf-Container mit demselben Image und denselben Daten führt per `--init-file` die SQL-Datei aus, danach läuft
  der Original-Container wieder (auch bei Fehlern, sonst mit Hinweis `docker start …`). Ist der Zugang eingerichtet, wird
  er beim Dump bevorzugt; ein hinterlegtes Passwort bleibt als Rückfall. Gemessen mit `mariadb:11`: Dump ohne Passwort
  geht, `DROP`/`INSERT`/`CREATE USER` werden abgelehnt, als `root` im Container wird der Benutzer abgelehnt.

## [0.6.1] - 2026-10-24

### Geändert
- **Datenbank-Dumps werden unkomprimiert gesichert** (`*.sql` statt `*.sql.gz`). PBS komprimiert die Blöcke selbst und
  erkennt unveränderte Teile wieder; ein gzip-Strom ändert sich dagegen komplett und wurde bei jedem Lauf voll
  hochgeladen (gemessen: 140 MiB je Lauf für den Immich-Dump). Ein neuer Dump ersetzt den alten `.sql.gz` des Containers;
  schlägt der neue Dump fehl, bleibt der alte erhalten.

## [0.6.0] - 2026-10-23

### Hinzugefügt
- **Änderungserkennung (`--change-detection-mode metadata`):** Der Client liest nur noch Dateien, deren Metadaten sich seit
  dem letzten Backup geändert haben, statt jede Datei jedes Mal komplett zu lesen (gemessen: 0 MiB Lesemenge bei
  unveränderten Daten). Beim Anlegen in GUI, TUI und `pbsgui add` standardmäßig an (`--change-detection legacy`
  schaltet es ab); bestehende Profile bleiben klassisch und lassen sich umstellen: GUI (Backup-Tab), TUI (`c`),
  `pbsgui detection NAME [metadata|legacy]`. Das erste Backup nach dem Umstellen liest einmal alles. Braucht einen
  PBS-Server ab Version 3.3. Der Fortschrittsbalken zeigt dann „nur geänderte Dateien“ statt einer Prozentzahl.
- **Wöchentlicher Prüflauf im Modus `data` (standardmäßig an):** Ein zweiter systemd-Timer (`pbsgui-NAME-data.timer`,
  Standard sonntags 03:00 Uhr) liest einmal pro Woche wieder alles, prüft so auch Dateien mit unveränderten Metadaten
  und fängt Lesefehler in selten benutzten Dateien ab. Er entsteht nur mit Änderungserkennung `metadata` und einem
  Zeitplan. Einstellbar beim Anlegen (GUI, TUI, `pbsgui add --weekly-data/--no-weekly-data --data-schedule`) und danach
  (GUI Backup-Tab, TUI `c`, `pbsgui detection NAME --weekly-data on|off --data-schedule …`); `pbsgui backup NAME --data`
  liest einmalig alles; `pbsgui status` zeigt den Timer.
- **Sperre pro Profil:** Es läuft nie mehr als ein Backup desselben Profils gleichzeitig (Zeitplan, wöchentlicher Lauf und
  manueller Start). Ein zweiter Lauf wartet bis zu 4 Stunden auf den ersten, statt mit einem PBS-Fehler abzubrechen.

### Geändert
- Snapshots im neuen Modus enthalten geteilte Archive (`x.mpxar.didx` + `x.ppxar.didx` statt `x.pxar.didx`). pbsgui
  behandelt beide als dasselbe Archiv `x.pxar` (Dateibrowser, Wiederherstellung, Rettungs-ISO); `restore` und `mount`
  des Clients akzeptieren den Namen `x.pxar` weiterhin.

## [0.5.0] - 2026-10-23

### Hinzugefügt
- **TUI, Server hinzufügen:** Dropdowns für **Datastore** und **Namespace** (gefüllt nach „Verbindung prüfen“; Namespace
  zusätzlich „Wurzel“ und mit Weg A „Neuen Namespace anlegen …“). Die Eingabefelder bleiben als Rückfall und bleiben mit
  der Auswahl synchron. (Die GUI hatte editierbare Auswahllisten bereits.)
- **Zeitplan beim Anlegen** (GUI, TUI und `pbsgui add --schedule`): Auswahl von stündlich bis wöchentlich oder eigener
  OnCalendar-Ausdruck mit Vorschau des nächsten Laufs. Ein ungültiger Ausdruck wird geprüft, bevor ein Zugang angelegt
  wird. Bei einem Vollbackup mit automatischem Schlüssel wird der Zeitplan erst nach dem Schlüssel-Export eingerichtet
  (TUI `k`, GUI „Schlüssel exportieren“, `pbsgui key export|confirm`).
- **Erstes Backup direkt starten** (GUI, TUI und `pbsgui add --backup-now`). Bei gesperrtem Vollbackup (Schlüssel noch
  nicht exportiert) wird erklärt, was fehlt, statt zu scheitern; mit Datenbank-Dumps kommt zuerst die Zugangs-Einrichtung.
- **Datenbank-Dumps (optional)** in GUI, TUI und Kommandozeile: Postgres- und MariaDB/MySQL-Container werden vor dem
  Backup gedumpt und als eigenes Archiv `dbdumps.pxar` mitgesichert. Erkennung über alle Docker-kompatiblen Sockets
  (System-Daemon und rootless-Daemons aller Benutzer) anhand der eingehängten Datenverzeichnisse, nicht nach Namen;
  gestoppte Container werden nicht gedumpt. Postgres braucht keinen Zugang. Für MariaDB/MySQL wird ein Zugang
  ausdrücklich hinterlegt (verschlüsselt, nie aus `.env` oder der Container-Umgebung gelesen) oder ein Dump-Benutzer mit
  reinen Leserechten angelegt; Passwörter gehen nur über stdin. Ein fehlgeschlagener Dump verhindert das Backup nie, der
  letzte gute Dump bleibt erhalten. Dumps werden pro Container überschrieben; Dateien von Containern, die länger als
  14 Tage nicht mehr erkannt werden, räumt pbsgui auf (erkannte Container nie).
  Bedienung: Checkbox beim Anlegen, TUI `p`, GUI „Datenbanken und Zugänge …“, `pbsgui add --db-dump`,
  `pbsgui dbdump NAME [--on|--off|--set|--create-user|--remove|--run]`.

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

[Unreleased]: https://github.com/nienhausdavid/pbsgui/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.8.1
[0.8.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.8.0
[0.7.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.7.0
[0.6.1]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.6.1
[0.6.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.6.0
[0.5.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.5.0
[0.4.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.4.0
[0.3.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.3.0
