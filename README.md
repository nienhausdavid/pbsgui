# pbsgui

Grafische Oberfläche für den Proxmox Backup Client (`proxmox-backup-client`).
Plan und Architektur: [docs/PLAN.md](docs/PLAN.md). Status: **Prototyp**.

## Was funktioniert (getestet gegen einen Fake-Client)
- Profile mit Quellordnern, Ausschlüssen, Namespace, Verschlüsselungs-Flag
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

## Entwickeln
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # Tests (Fake-Client unter tests/)
.venv/bin/pbsgui                  # GUI starten
scripts/install-desktop.sh        # Starter + Icon im Anwendungsmenü installieren (~/.local/share)
```
Debian/Ubuntu: `sudo apt install python3-venv`, dazu den `proxmox-backup-client` aus dem Proxmox-Client-Repo.
