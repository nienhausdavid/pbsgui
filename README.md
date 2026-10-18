# pbsgui

Grafische Oberfläche für den Proxmox Backup Client (`proxmox-backup-client`).
Plan und Architektur: [docs/PLAN.md](docs/PLAN.md). Status: **Prototyp**.

## Was funktioniert (getestet gegen einen Fake-Client)
- Profile mit Quellordnern, Ausschlüssen, Namespace, Verschlüsselungs-Flag
- Server einrichten: Weg A (einmalig als root, legt Benutzer/Token/ACLs an) oder Weg B (vorhandener Token), mit Fingerprint-Pinning
- Backup starten mit Live-Log, Snapshots listen, Restore in einen frei gewählten Zielordner
- Zeitpläne als systemd-Timer, wählbar pro Profil: Benutzer- oder System-Units
- Geheimnisse verschlüsselt über `systemd-creds` (Fallback: Datei 0600)

## Noch nicht / nicht verifiziert
- Nicht gegen einen echten PBS-Server getestet (PBS-API-Endpunkte nach Doku, 2FA-Ablauf ungeprüft)
- System-Scope (pkexec-Helfer, System-Units) ist nur per Unit-Test mit Fake-Runner geprüft
- Schlüssel erzeugen, Master-Key, Prune, Katalog-Browser fehlen
- Keine Lizenz festgelegt

## Entwickeln
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # Tests (Fake-Client unter tests/)
.venv/bin/pbsgui                  # GUI starten
```
Debian/Ubuntu: `sudo apt install python3-venv`, dazu den `proxmox-backup-client` aus dem Proxmox-Client-Repo.
