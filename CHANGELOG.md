# Changelog

Alle nennenswerten Änderungen stehen hier. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionen nach [Semantic Versioning](https://semver.org/lang/de/): `MAJOR.MINOR.PATCH`
(MAJOR = inkompatible Änderung, MINOR = neue Funktion, PATCH = Fehlerbehebung).
Version 0.x: die Schnittstellen (Profilformat, Befehle) können sich noch ändern.

## [Unreleased]

### Hinzugefügt
- **Schlüssel und Notfall-Set in die Zwischenablage kopieren:** GUI-Knöpfe „Schlüssel kopieren“, „Schlüssel + Notfallblatt kopieren“ und „Notfallblatt kopieren“,
  dieselben Knöpfe in der Übersicht der TUI (und Taste `y`), `pbsgui key copy|copy-kit|copy-sheet NAME` (braucht `wl-copy`, `xclip` oder `xsel`;
  der Text geht über stdin). Das Notfall-Set enthält das Notfallblatt und den geheimen Schlüssel; die GUI leert die Zwischenablage nach 2 Minuten.
  Schlüssel und Set gelten als gesicherter Export und lösen die Sperre des verschlüsselten Vollbackups.
- **Backup startet nach dem Sichern von selbst:** Ist das Vollbackup noch gesperrt, bietet die GUI (und die TUI mit `b`) an, Schlüssel und Notfallblatt
  gleich in die Zwischenablage zu kopieren; danach läuft das Backup ohne weiteren Export-Dialog los.

### Behoben
- Tray: Das Untermenü „Jetzt sichern“ konnte verschwinden (Referenz wurde nicht gehalten).

## [0.9.0] - 2026-10-24

### Hinzugefügt
- **Server nachträglich ändern:** Server, Port, Fingerprint, Datastore, Namespace und Token eines vorhandenen Profils lassen sich ändern
  (`pbsgui server NAME …`, GUI „Server ändern …“ im Backup-Tab, TUI `i`). Profilname, Schlüssel, Quellen, Zeitplan und Verlauf bleiben.
  Die neuen Angaben werden vor dem Speichern mit dem echten Client geprüft (sonst bleibt alles unverändert); eine geänderte Token-ID braucht ihr
  Secret, ein neuer Fingerprint muss ausdrücklich bestätigt werden. Vorhandene Snapshots bleiben auf dem alten Ziel, das nächste Backup lädt am
  neuen alles neu hoch (der Schlüssel passt weiter).
- **Zweisprachig: Deutsch und Englisch.** GUI, Terminal-Oberfläche, Kommandozeile, Rettungs-Assistent und Web-Oberfläche (`index.en.html`)
  folgen der Systemsprache (`LANG`); ausdrücklich mit `pbsgui language de|en|auto` (Datei `~/.config/pbsgui/language`, in der GUI links unten)
  oder `PBSGUI_LANG`. Die Quelltexte sind deutsch und zugleich die Schlüssel (`tr("Text {0}", wert)`), die Übersetzungen stehen in
  `src/pbsgui/i18n/en_*.py`; ein Test prüft, dass jeder Text übersetzt ist und die Platzhalter passen. README, Installation, Wiederherstellung,
  Sicherheit und CHANGELOG gibt es auf Englisch (`README.md`, `docs/INSTALLATION.md`, `docs/RESTORE.md`, `SECURITY.md`, `CHANGELOG.en.md`);
  die deutschen Fassungen heißen `*.de.md` bzw. `docs/WIEDERHERSTELLUNG.md`.

### Hinzugefügt
- **VM-Backups von Proxmox VE auf physische Platten (V2P)** mit der Rettungs-ISO: Snapshots `vm/<id>/…` mit Abbild-Archiven
  (`drive-*.img`) lassen sich auf eine oder mehrere Platten zurückspielen (Kommandozeile `pbsgui rescue vm-snapshots|vm-run`,
  Haken im Terminal-Assistenten, „Art der Wiederherstellung“ in der Web-Oberfläche). Zuordnung Quell- zu Ziel-Platte, Boot-Platte aus der
  VM-Konfiguration, Bestätigung durch Eintippen aller Ziel-Platten. Danach automatische, einzeln abschaltbare Reparaturen für
  Debian/Ubuntu-Gäste: Partition, LUKS, LVM und Dateisystem auf eine größere Platte vergrößern, Initramfs mit allen Treibern
  (`MODULES=most`), GRUB (UEFI mit Fallback oder BIOS), Netzwerk-Schnittstellennamen, `qemu-guest-agent` und `cloud-init`. Geprüft in
  QEMU/UEFI (VM mit LUKS, LVM, zwei Platten, größere Ziel-Platten: bootet, Daten stimmen) und mit echten Loop-Geräten.
  Windows/Solaris-Gäste und kleinere Ziel-Platten werden abgelehnt.

### Hinzugefügt
- **Restore und Suche über alle Snapshots** (GUI, TUI, Kommandozeile):
  - **Dateiversionen:** `pbsgui versions NAME /Pfad/datei` zeigt alle Fassungen einer Datei aus allen Snapshots (gleiche Größe und
    Änderungszeit = dieselbe Fassung), `--restore NR --target ORDNER` holt eine davon. GUI: Dateibrowser → „Fassungen …“ mit Vorschau
    jeder Fassung; TUI: `v` im Dateibrowser.
  - **Suche in allen Snapshots:** `pbsgui find NAME MUSTER` findet auch längst gelöschte Dateien (Spalte „Im neuesten“). GUI:
    Snapshots → „Suche in allen Snapshots …“; TUI: `g`. Die Dateilisten werden pro Snapshot zwischengespeichert
    (`~/.cache/pbsgui/catalogs`, nur für den Benutzer lesbar; bei verschlüsselten Backups stehen dort Dateinamen im Klartext),
    der erste Durchlauf ist langsam, weitere gehen schnell.
  - **Vergleich zweier Snapshots:** `pbsgui diff NAME [ALT] [NEU]` (Standard: vorheriger gegen neuesten), neue/geänderte/gelöschte
    Dateien, ganze Ordner zusammengefasst. GUI: „Vergleichen …“; TUI: `x`.
  - **Vorschau** im Dateibrowser (GUI: Text, Bilder, Hexdump rechts neben dem Baum; TUI: `p`; `pbsgui preview NAME PFAD`), ohne
    wiederherzustellen. Symbolische Links werden nie verfolgt.
  - **Restore-Test:** `pbsgui restore-test NAME` holt zufällige Dateien in einen Wegwerf-Ordner (im Arbeitsspeicher) und prüft
    Lesbarkeit, Größe und, wenn das Original unverändert ist, den SHA-256. Exitcode 1 bei Fehler, damit sich der Test per Zeitplan
    nutzen lässt. GUI: „Restore-Test …“; TUI: `w`. Das letzte Ergebnis erscheint im Dashboard.
- **Überblick und Betrieb:**
  - **Dashboard** (`pbsgui dashboard`, GUI-Tab, TUI `e`): je Profil Status, letztes Backup, Dauer, Größe, Zuwachs gegenüber dem
    vorherigen Snapshot und vor 30 Tagen, Zahl der Snapshots, Warnungen (überfälliges Backup, Fehler, Schlüssel nicht gesichert);
    dazu Belegung des Datastores und Deduplizierungsfaktor (Stand der letzten Müllsammlung), falls der Token sie lesen darf.
    Die Deduplizierung pro Profil lässt sich vom Client aus nicht genau ermitteln, deshalb gilt der Wert für den ganzen Datastore.
  - **Verlauf** (`pbsgui history`, GUI-Tab, TUI `h`): jeder Lauf (Zeitplan oder manuell) wird mit Ausgabe gespeichert
    (`<state>/history`, je Profil die letzten 200), filterbar nach Profil, „nur Fehler“, Zeitraum und Text.
  - **Tray-Icon:** zeigt „läuft“, „Fehler“ oder „in Ordnung“, Menü „Jetzt sichern“ je Profil, meldet neue Fehler auf dem Desktop.
    Wahlweise läuft die Oberfläche beim Schließen im Hintergrund weiter und startet beim Anmelden (`pbsgui tray`, beides im
    Tray-Menü einstellbar). Die Zeitpläne laufen weiter unabhängig davon über systemd.

### Hinzugefügt
- **Web-Oberfläche für die Wiederherstellung** (Rettungs-ISO, für VPS und Server ohne bequeme Konsole): `pbsgui web --daemon`, `F5` oder
  Knopf „Web“ im Assistenten. Zeigt Adresse, ein zufälliges Passwort (4 Wörter, jedes mit einer Zahl dahinter, etwa 78 Bit) und den TLS-Fingerabdruck.
  Im Browser dieselben Schritte wie im Terminal-Assistenten: VPN, Server, Snapshot, Platte und Verschlüsselung, Plan, Bestätigung
  durch Eintippen des Gerätenamens, Live-Log, Neustart. Nur Standardbibliothek (HTTPS, selbstsigniertes Zertifikat).
  Schutz: Rate Limiting, Sperren mit wachsender Dauer, Abschaltung nach zu vielen Fehlversuchen, **Honeypot** (unsichtbares
  Formularfeld und Köder-Adressen wie `/wp-login.php`, `/.env`: Treffer werden gesperrt und auf der Konsole gemeldet), eine Sitzung
  mit Leerlauf- und Gesamtzeit, CSRF-Token, Content-Security-Policy, begrenzte Anfragegröße und Verbindungen. `--bind`, `--port`,
  `--timeout`, `--status`, `--stop`.

### Hinzugefügt
- **Rettungs-ISO mit VPN:** WireGuard, OpenVPN und NetBird sind enthalten (NetBird aus dem NetBird-Repository). Im Assistenten mit `F3` oder dem Knopf „VPN“
  (Konfiguration vom USB-Stick suchen, verbinden, trennen, Status), auf der Kommandozeile mit `pbsgui vpn status|find|up|down`.
  Passwörter und Setup-Keys gehen nie als Programmargument (OpenVPN-Zugangsdaten nur kurz in einer 0600-Datei im RAM, NetBird per
  Umgebung `NB_SETUP_KEY`).
- **Programme aktualisieren sich beim Start der ISO:** `pbsgui-update.timer` startet beim Booten (und wiederholt alle 2 Minuten, bis es
  klappt) `pbsgui update`: Paketlisten holen und `pbstui`, `proxmox-backup-client`, `netbird`, `wireguard-tools`, `openvpn`
  nur aktualisieren, wenn installiert. Quellen: pbsgui-Repository, Proxmox, NetBird, Debian (jetzt auch im fertigen Live-System).
  Der Assistent wartet beim Start bis zu 45 s auf das Ergebnis (eine Taste überspringt); von Hand mit `F4` im Assistenten oder
  `pbsgui update`; nach einem Update von `pbstui` startet sich der Assistent auf Wunsch neu.

## [0.8.3] - 2026-10-24

### Geändert
- **Zwei Debian-Pakete:** `pbstui` (Kern, Kommandozeile `pbsgui`, Terminal-Oberfläche `pbstui`, Zeitpläne `pbsgui-run`; ohne Qt, für
  Server) und `pbsgui` (Qt-Oberfläche und Starter; hängt von `pbstui` gleicher Version ab, enthält es also mit). Bisher war alles
  in einem Paket, die Qt-Teile nur empfohlen. Ein Upgrade von 0.8.2 oder früher klappt ohne Handarbeit (`Replaces`/`Breaks`;
  im Container getestet). Der Installer `scripts/install.sh --no-gui` installiert nur `pbstui`; die Rettungs-ISO enthält nur
  `pbstui`. `pbsgui` ohne Argumente erklärt, wie man die GUI nachinstalliert, wenn nur `pbstui` da ist.

## [0.8.2] - 2026-10-24

### Sicherheit
Prüfung mit `ruff`, `bandit`, `pip-audit` und Durchsicht der sicherheitskritischen Stellen; keine schweren Funde, aber diese Härtungen:
- **Kein Root-Code aus Benutzerordnern:** Vollbackup (`pkexec`) und Systemzeitpläne starten Python nur noch als root, wenn Code
  und Interpreter root gehören und für andere nicht beschreibbar sind (Debian-Paket). In einem Entwicklungs-Checkout oder
  einer virtuellen Umgebung wird das verweigert, mit Hinweis; Ausnahme nur mit `PBSGUI_ALLOW_USER_CODE_AS_ROOT=1`.
- **Root-Helfer** (`pbsgui.cli.admin`) akzeptiert nur noch die bekannten Geheimnis-Arten (`token`, `keypass`, `key`, `dbdump`) und
  Zeitplan-Aktionen; vorher hätte ein Name wie `../../x` eine Datei außerhalb des Geheimnis-Ordners angelegt.
- **systemd-Units:** Profilname und Zeitplan werden von Steuerzeichen (Zeilenumbruch) und `%` befreit, damit sie keine eigenen
  Anweisungen einschleusen; zusätzlich `ProtectKernelModules`, `ProtectControlGroups`, `LockPersonality`, `RestrictSUIDSGID`.
- **Schlüssel im RAM:** Der kurzzeitig entschlüsselte Schlüssel liegt auch als root (wo `XDG_RUNTIME_DIR` fehlt) in `/run` oder
  `/dev/shm`, nicht mehr im plattengestützten `/tmp`. Ein exportierter Schlüssel bekommt immer Rechte 0600, auch wenn die Datei
  schon existierte.
- **PBS-API:** Datastore- und Token-Namen werden im Pfad maskiert.
- `assert` als Laufzeitprüfung durch echte Fehlerprüfungen ersetzt (wirkt auch mit `python -O`); stilles `except: pass`
  in der Rettungs-Oberfläche meldet jetzt den Grund.
- **CI:** neuer Job „Sicherheitsprüfungen“ (`ruff`, `bandit`, `pip-audit`); neue Tests `tests/test_security.py`;
  `SECURITY.md` beschreibt Schutzmaßnahmen und Grenzen.

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

[Unreleased]: https://github.com/nienhausdavid/pbsgui/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.9.0
[0.8.3]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.8.3
[0.8.2]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.8.2
[0.8.1]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.8.1
[0.8.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.8.0
[0.7.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.7.0
[0.6.1]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.6.1
[0.6.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.6.0
[0.5.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.5.0
[0.4.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.4.0
[0.3.0]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.3.0
