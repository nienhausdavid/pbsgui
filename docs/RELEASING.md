# Release and APT repository

**English** · [Deutsch](RELEASING.de.md)

## One-time setup

1. Create a **signing key** for the APT repository (locally, separate from your everyday key):
   ```bash
   gpg --quick-generate-key "pbsgui APT <you@example.org>" ed25519 sign 3y
   gpg --armor --export-secret-keys <KEY-ID> | gh secret set APT_GPG_PRIVATE_KEY
   # only if the key has a passphrase:
   gh secret set APT_GPG_PASSPHRASE
   ```
   Also back up the private key safely (password manager). Without it no new versions can be signed;
   users would then have to import a new public key.
2. Enable **GitHub Pages**: repository → Settings → Pages → Source: *GitHub Actions*.
   On the free plan the repository must be **public** for this.
3. License: AGPL-3.0-or-later (`LICENSE`, `pyproject.toml`, `usr/share/doc/pbsgui/copyright` in `scripts/build-deb.sh`).

## Versions

Version numbers follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`. The only source is
`version` in `pyproject.toml`; it produces `pbsgui --version`, the two `.deb` files (`pbstui_<version>_all.deb`, `pbsgui_<version>_all.deb`) and the ISO
(`pbsgui-rescue-<version>-amd64.iso`). Changes are entered continuously under `[Unreleased]` in `CHANGELOG.md` (English, main file) **and** `CHANGELOG.de.md` (German);
`scripts/release.sh` updates both, the GitHub release notes come from `CHANGELOG.md`.

## Publish a new version

```bash
# 1. Enter the changes under "## [Unreleased]" in CHANGELOG.md (and CHANGELOG.de.md) and commit
# 2. Prepare the release (raise the version, close the changelogs, tests, commit and tag; nothing is published yet):
scripts/release.sh 0.4.0
# 3. Check (git log, git show), then publish:
git push origin main && git push origin v0.4.0      # or right away: scripts/release.sh 0.4.0 --push
```
The tag starts the workflow `Release`: it checks that the tag and `pyproject.toml` match, builds the `.deb` files and the
ISO, creates the GitHub release with the notes from the CHANGELOG and updates the APT repository (all earlier
release `.deb` files stay included). A tag that has been published is never moved: errors in a release
are fixed with the next PATCH release. The ISO is only rebuilt when something changed in `iso/` or `scripts/build-iso.sh` since the last release that has an ISO (the ISO updates the programs itself at boot); a release without an ISO asset keeps pointing to the ISO of the earlier release. With `workflow_dispatch` (input `tag`) an ISO can be attached to an existing release afterwards.

## Installation for users

```bash
curl -fsSL https://nienhausdavid.github.io/pbsgui/pbsgui.gpg | sudo tee /usr/share/keyrings/pbsgui.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] https://nienhausdavid.github.io/pbsgui stable main" | sudo tee /etc/apt/sources.list.d/pbsgui.list
sudo apt update && sudo apt install pbsgui
```
`pbstui` depends on `wl-clipboard` (clipboard buttons) and recommends `proxmox-backup-client` from the Proxmox client repository (see README).

## Build and test locally

```bash
scripts/build-deb.sh dist                      # dist/pbstui_<version>_all.deb and dist/pbsgui_<version>_all.deb
sudo apt install ./dist/pbstui_*_all.deb ./dist/pbsgui_*_all.deb   # or only pbstui without a desktop
GPG_KEY_ID=<id> scripts/build-apt-repo.sh dist site stable   # APT repository in site/
```
