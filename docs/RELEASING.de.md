# Release und APT-Repository

[English](RELEASING.md) · **Deutsch**

## Einmalige Einrichtung

1. **Signaturschlüssel** für das APT-Repository erzeugen (lokal, getrennt vom Alltags-Schlüssel):
   ```bash
   gpg --quick-generate-key "pbsgui APT <deine@mail>" ed25519 sign 3y
   gpg --armor --export-secret-keys <KEY-ID> | gh secret set APT_GPG_PRIVATE_KEY
   # nur falls der Schlüssel eine Passphrase hat:
   gh secret set APT_GPG_PASSPHRASE
   ```
   Den privaten Schlüssel zusätzlich sicher sichern (Passwortmanager). Ohne ihn lassen sich keine
   neuen Versionen signieren; Nutzer müssten dann einen neuen öffentlichen Schlüssel importieren.
2. **GitHub Pages** aktivieren: Repository → Settings → Pages → Source: *GitHub Actions*.
   Auf dem kostenlosen Plan muss das Repository dafür **öffentlich** sein.
3. Lizenz: AGPL-3.0-or-later (`LICENSE`, `pyproject.toml`, `usr/share/doc/pbsgui/copyright` in `scripts/build-deb.sh`).

## Versionen

Versionsnummern nach [Semantic Versioning](https://semver.org/lang/de/): `MAJOR.MINOR.PATCH`. Einzige Quelle ist
`version` in `pyproject.toml`; daraus entstehen `pbsgui --version`, die beiden `.deb` (`pbstui_<version>_all.deb`, `pbsgui_<version>_all.deb`) und die ISO
(`pbsgui-rescue-<version>-amd64.iso`). Änderungen werden laufend in `CHANGELOG.md` (englisch, Hauptdatei) **und** `CHANGELOG.de.md` (deutsch) unter `[Unreleased]` eingetragen; `scripts/release.sh` schreibt beide fort, die GitHub-Release-Notizen kommen aus `CHANGELOG.md`.

## Neue Version veröffentlichen

```bash
# 1. Änderungen unter "## [Unreleased]" in CHANGELOG.md eintragen und committen
# 2. Release vorbereiten (Version erhöhen, CHANGELOG abschließen, Tests, Commit und Tag; noch nichts veröffentlicht):
scripts/release.sh 0.4.0
# 3. Prüfen (git log, git show), dann veröffentlichen:
git push origin main && git push origin v0.4.0      # oder gleich: scripts/release.sh 0.4.0 --push
```
Der Tag startet den Workflow `Release`: Er prüft, dass Tag und `pyproject.toml` übereinstimmen, baut das `.deb` und die
ISO, legt das GitHub-Release mit den Notizen aus dem CHANGELOG an und aktualisiert das APT-Repository (alle früheren
Release-`.deb` bleiben enthalten). Ein einmal veröffentlichter Tag wird nicht verschoben: Fehler in einem Release
behebt man mit dem nächsten PATCH-Release.

## Installation für Nutzer

```bash
curl -fsSL https://nienhausdavid.github.io/pbsgui/pbsgui.gpg | sudo tee /usr/share/keyrings/pbsgui.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] https://nienhausdavid.github.io/pbsgui stable main" | sudo tee /etc/apt/sources.list.d/pbsgui.list
sudo apt update && sudo apt install pbsgui
```
`pbsgui` empfiehlt `proxmox-backup-client` aus dem Proxmox-Client-Repository (siehe README).

## Lokal bauen und testen

```bash
scripts/build-deb.sh dist                      # dist/pbstui_<version>_all.deb und dist/pbsgui_<version>_all.deb
sudo apt install ./dist/pbstui_*_all.deb ./dist/pbsgui_*_all.deb   # oder nur pbstui ohne Desktop
GPG_KEY_ID=<id> scripts/build-apt-repo.sh dist site stable   # APT-Repository in site/
```
