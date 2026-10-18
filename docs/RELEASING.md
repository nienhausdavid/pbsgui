# Release und APT-Repository

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
3. Lizenz festlegen (`LICENSE` und `usr/share/doc/pbsgui/copyright` in `scripts/build-deb.sh`).

## Neue Version veröffentlichen

```bash
# Version in pyproject.toml erhöhen, committen, dann:
git tag v0.2.0 && git push origin v0.2.0
```
Der Workflow `Release` baut das `.deb`, legt ein GitHub-Release an und aktualisiert das APT-Repository
(alle früheren Release-`.deb` bleiben enthalten).

## Installation für Nutzer

```bash
curl -fsSL https://nienhausdavid.github.io/pbsgui/pbsgui.gpg | sudo tee /usr/share/keyrings/pbsgui.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] https://nienhausdavid.github.io/pbsgui stable main" | sudo tee /etc/apt/sources.list.d/pbsgui.list
sudo apt update && sudo apt install pbsgui
```
`pbsgui` empfiehlt `proxmox-backup-client` aus dem Proxmox-Client-Repository (siehe README).

## Lokal bauen und testen

```bash
scripts/build-deb.sh dist                      # dist/pbsgui_<version>_all.deb
sudo apt install ./dist/pbsgui_*_all.deb
GPG_KEY_ID=<id> scripts/build-apt-repo.sh dist site stable   # APT-Repository in site/
```
