# pbsgui – Plan

[English](PLAN.md) · **Deutsch**

GUI für `proxmox-backup-client`. Kein eigener Client: dünner, testbarer Wrapper um
den offiziellen Client, die PBS-API und systemd.

Status: Prototyp (siehe README). Profile liegen als JSON, nicht TOML. Erste Ziel-Plattform: Linux (Debian 13, Ubuntu). Windows/macOS später.

## Stack

- Python 3.11+ mit PySide6 (Qt 6) für die GUI
- `proxmox-backup-client` als externes Programm (subprocess, `--output-format json`)
- PBS-HTTP-API (Port 8007) für Provisionierung und Berechtigungsprüfung
- systemd für Zeitpläne

## Schichten

```
pbsgui/
  core/      Logik ohne UI (testbar)
    client.py      Wrapper um proxmox-backup-client
    pbs_api.py     Login, User/Token/ACL anlegen, Rechte prüfen
    models.py      Profile, Snapshot, Job
    store.py       Profile als TOML (~/.config/pbsgui/ bzw. /etc/pbsgui/)
    secrets/       SecretStore-Interface + Backends (siehe unten)
    scheduler/     Scheduler-Interface + systemd-Backend
    runner.py      führt ein Profil aus (Hooks, Logs, Exitcode)
  cli/       pbsgui-run <profil>   (wird von den Timern aufgerufen)
  gui/       PySide6
  tests/
```

Secrets und Scheduler sind von Anfang an Interfaces, damit später Keychain/launchd
(macOS) und DPAPI/Task Scheduler (Windows) als weitere Backends dazukommen.

## Einrichtung eines Servers

### Weg A: einmalig als root (Provisionierung)

1. TLS-Fingerprint des Servers holen und zur Bestätigung anzeigen (TOFU), danach pinnen.
2. Login als `root@pam` über `POST /access/ticket` (inkl. TOTP, falls aktiv).
3. Per API anlegen:
   - Benutzer pro Host (z. B. `pbsgui-<hostname>@pbs`)
   - API-Token für diesen Benutzer
   - optional Namespace pro Host im Datastore
   - ACLs mit minimalen Rechten, Standard: Rolle `DatastoreBackup` auf
     `/datastore/<name>[/<ns>]`. Benutzer **und** Token brauchen je eine ACL
     (effektive Rechte = Schnitt). Prune-Rechte nur optional.
     Rollen vor der Umsetzung gegen die PBS-Doku prüfen.
4. Token-Secret wird nur einmal geliefert und sofort verschlüsselt gespeichert.
   Das Root-Passwort wird nie gespeichert.

### Weg B: vorhandene API-Zugangsdaten

Repository, Token-ID und Secret eingeben. `GET /access/permissions` prüft, ob die
Rechte fürs Backup reichen, und nennt fehlende Rechte konkret.

Zusätzlich: Eintrag wieder entfernen (Token/Benutzer serverseitig löschen).

## Verschlüsselung

- Transport: TLS mit gepinntem Fingerprint.
- Backup-Inhalt: clientseitiger Key (`key create`). Zusätzlich Master-Key
  (`key create-master-key`) für Notfall-Wiederherstellung. Die GUI erzwingt die
  Sicherung beider Schlüssel.
- Geheimnisse (Token, Passwörter, Key-Passphrase) liegen nie im Klartext auf der Platte.

### SecretStore-Backends (Linux)

| Backend | Wann | Anmerkung |
|---|---|---|
| `systemd-creds` (System) | Modus „System“ | `LoadCredentialEncrypted=`, an Host/TPM gebunden |
| `systemd-creds --user` | Modus „Benutzer“, systemd >= 256 | Debian 13 (257), Ubuntu ab 25.04. Der Runner entschlüsselt selbst; `LoadCredentialEncrypted=` scheitert im User-Manager (getestet, Exit 243) |
| Datei 0600 | Fallback in Modus „Benutzer“ | z. B. Ubuntu 24.04 (systemd 255) |
| libsecret/Keyring | nur GUI-Interaktion | nicht für Timer: ohne Login keine Sitzung |

Ubuntu 24.04 LTS hat systemd 255, dort gibt es kein User-Scope-`systemd-creds`.
Die App erkennt die Version und wählt das Backend, mit klarem Hinweis auf die
schwächere Stufe.

## Zeitpläne (systemd)

Der Modus ist **pro Profil wählbar**. Die App schlägt einen Standard vor, der Benutzer
entscheidet:

- **System** (`/etc/systemd/system/`): Installation einmalig per `pkexec`. Sichert das
  ganze System (auch fremde Dateien). Nur auswählbar, wenn Admin-Rechte vorhanden sind;
  sonst ist die Option deaktiviert und erklärt warum.
- **Benutzer** (`~/.config/systemd/user/`): braucht kein sudo, sichert nur Daten des
  Benutzers (Home). Damit Timer ohne Login laufen: `loginctl enable-linger`.
  Auch mit sudo wählbar, z. B. wenn nur Home gesichert werden soll.

Standardvorschlag: System, wenn Admin-Rechte erkannt werden, sonst Benutzer.
Ein Profil kann später per „Modus wechseln“ umgezogen werden (Units und Credentials
werden im neuen Scope neu angelegt, die alten entfernt).

Pro Profil `.service` + `.timer`:

- Service ruft `pbsgui-run <profil>` auf, lädt Credentials, `After=network-online.target`,
  Hardening (`NoNewPrivileges`, `ProtectSystem` wo möglich).
- Optionale Bedingungen: Netz, Strom (`ConditionACPower`).
- Zeitplan über `OnCalendar=`, `Persistent=true` für verpasste Läufe.
- Logs im Journal und in der GUI; Status „letzter Lauf“ und „nächster Lauf“.

## Meilensteine

1. Core + CLI: Client-Wrapper, Profile, `pbsgui-run`, Tests mit Fake-Client.
2. GUI-Grundgerüst: Server einrichten (Weg A/B), Verbindung testen, Snapshots listen.
3. Backup: Pfade, Excludes, Live-Log.
4. Restore: Katalog-Browser, Standard = neuer Zielordner.
5. Zeitpläne: systemd-Generator (System/User), Status in der GUI.
6. Verschlüsselung (Key/Master-Key) und Prune mit Dry-Run.
7. Packaging: pipx, später .deb/Flatpak. Danach Windows/macOS-Backends.

## Offene Punkte

- Tests laufen gegen einen Fake-Client (Skript, das `proxmox-backup-client` und die
  PBS-API simuliert). Kein Test-PBS nötig; ein Test gegen einen echten Server kann später
  als optionaler Integrationstest folgen.
- Exakte Rollen/Rechte für Prune und Restore gegen die PBS-Doku verifizieren.
- Lizenz: AGPL-3.0-or-later (siehe `LICENSE`).
- Hinweis: BackupPilot (AGPL) wurde nur zur Orientierung gelesen, Code wird nicht übernommen.

## Wiederherstellung auf neuem PC (Rettungs-ISO)

Umgesetzt: Vollbackups enthalten `recovery.pxar` (Datenträger-Metadaten); `pbsgui rescue` (CLI + Textual-Assistent)
baut das Layout mit den alten UUIDs nach (`core/rescue.py`), spielt `root.pxar` zurück und erneuert Initramfs und
Bootloader im chroot. Die ISO entsteht mit `live-build` (`iso/`, `scripts/build-iso.sh`). Tests: Plan-Logik als
Unit-Tests, Layout-Nachbau mit echten Loop-Geräten (`tests/test_rescue_integration.py`, root), Ende-zu-Ende in
QEMU (`tests/vm/e2e.py`).
