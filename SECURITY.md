# Sicherheit

## Meldung von Schwachstellen
Bitte keine öffentlichen Issues für Sicherheitslücken. Schreib an den Maintainer über eine private Nachricht auf GitHub
(Security → „Report a vulnerability“ im Repository).

## Was pbsgui schützt und wie
- **Geheimnisse:** API-Token, Schlüssel und Passphrasen liegen verschlüsselt im Geheimnis-Speicher (`systemd-creds`,
  Rechte 0600; Fallback Datei 0600) und werden nie als Programmargument übergeben. Der Klartext-Schlüssel liegt nur
  kurz in einem 0700-Verzeichnis im RAM (`XDG_RUNTIME_DIR`, `/run` oder `/dev/shm`).
- **Verbindung:** TLS mit gepinntem Fingerabdruck; vor dem Fingerabdruck-Vergleich wird nichts gesendet.
- **Root-Code:** Vollbackups und Systemzeitpläne starten Python als root. pbsgui tut das nur, wenn Code und Interpreter
  root gehören und für andere nicht beschreibbar sind (Debian-Paket); in einem Entwicklungs-Checkout wird das
  verweigert (Ausnahme nur mit `PBSGUI_ALLOW_USER_CODE_AS_ROOT=1`).
- **Root-Helfer:** `pbsgui.cli.admin` nimmt nur bekannte Geheimnis-Arten und Aktionen an; Namen werden zu Dateinamen
  gesäubert, Texte in Units von Steuerzeichen und `%` befreit.
- **Programme** werden immer mit Argumentlisten gestartet, nie über eine Shell mit Benutzereingaben.
- **Wiederherstellung:** „Überschreiben“ ist auf das Home-Verzeichnis begrenzt (Symlinks werden aufgelöst), alte
  Fassungen werden vorher gesichert.

- **Web-Oberfläche der Rettungs-ISO** (nur auf Wunsch aktiv): zufällige Passphrase (etwa 78 Bit), HTTPS, Rate Limiting, Sperren,
  Honeypot (verstecktes Feld, Köder-Adressen), Abschaltung nach 40 Fehlversuchen, eine Sitzung mit Zeitlimits, CSRF-Token,
  Content-Security-Policy mit Nonce, keine Datei- oder Verzeichnisfreigabe, kein Stacktrace an Clients, Größenlimits. Sie lauscht
  standardmäßig an allen Adressen (nötig, solange noch kein VPN steht); bei Bedarf mit `--bind` auf eine Adresse beschränken.

## Prüfungen
Die CI führt `ruff` (Fehler- und Sicherheitsregeln), `bandit` und `pip-audit` aus; `tests/test_security.py` sichert die
oben genannten Eigenschaften ab.

## Bekannte Grenzen
- Die Rettungs-ISO startet eine root-Konsole (Wiederherstellung braucht root). Sie ist für den Einsatz an einem Rechner
  gedacht, auf den man physischen Zugriff hat; die ISO ist nicht für den Dauerbetrieb gedacht.
- Ein kompromittierter Benutzer mit sudo-Rechten kann naturgemäß alles.
- Der Schlüssel im Geheimnis-Speicher ist an den Rechner gebunden; der Export ist Pflicht und liegt in der Verantwortung
  des Benutzers.
