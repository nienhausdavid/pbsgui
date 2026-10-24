# pbsgui

**English** · [Deutsch](README.de.md)

Graphical, terminal and command-line interface for the Proxmox Backup Client (`proxmox-backup-client`).
Plan and architecture: [docs/PLAN.md](docs/PLAN.md). Status: **prototype**.

The interface is available in **English and German**. It follows the system language (`LANG`); choose explicitly with
`pbsgui language en|de|auto` or the environment variable `PBSGUI_LANG`.

## What works (tested against a fake client)
- Profiles with source folders, exclusions, namespace and an encryption flag
- Backup scope chosen during setup: **home only** (default, no sudo, user mode) or the **entire system** (file level, needs administrator rights, runs in system mode via `pkexec`/systemd). A full backup covers `/` plus the **chosen drives** (`--include-dev`): internal local file systems (ext4, xfs, btrfs, vfat …) are preselected, **USB drives** (also behind LUKS/LVM) and mounts under `/media`, `/mnt`, `/run` only on explicit choice, **network mounts** (NFS, CIFS, sshfs …) and virtual file systems never, **bind-mount duplicates** are not read twice. Excluded are `/proc`, `/sys`, `/dev`, `/run`, `/tmp` and the like; there are no Docker/container default exclusions. It is **not** a system image
- Server setup: way A (once as root, creates user/token/ACLs) or way B (existing token), with fingerprint pinning
- **Change the server afterwards** (server, port, fingerprint, datastore, namespace, token): `pbsgui server NAME …`, GUI “Change server …”, TUI `i`; the new settings are checked with the client before anything is saved
- **Master key** (RSA) as insurance against losing the encryption key: every snapshot then carries a key copy that only the private master key opens; set up and recovery in the GUI, TUI and CLI (also on the rescue ISO: `pbsgui rescue recover-key`)
- Client-side encryption on by default: the key is generated automatically (without a passphrase, because the client only accepts one via a TTY), stored encrypted in the secret store and provided only briefly as a 0600 file in tmpfs; export dialog for safekeeping
- During setup: load datastores, check namespaces live (exists / will be created / cannot be created with way B)
- Start a backup with a live log, list snapshots, restore into a freely chosen target folder
- Schedules as systemd timers, selectable per profile: user or system units; selectable right at creation
- **Read only what changed** (change detection by metadata comparison, saves a lot of read time for large, rarely changing data)
- Optional **database dumps** of running containers (Postgres, MariaDB/MySQL) before the backup, as the archive `dbdumps.pxar`
  (details and credentials: CHANGELOG, `pbsgui dbdump --help`); also “start the first backup right away” at creation
- Secrets encrypted via `systemd-creds` (fallback: file with mode 0600)

## Checked against a real PBS 4.2 (token login)
Provisioning (user, token, nested namespace, ACLs), repeatability, backup, restore
(identical content), isolation (the token does not see foreign namespaces), cleanup.
Findings: new ACLs take 3–8 s to become effective (the app waits for them); deleting a user
leaves its own ACL behind (the app removes it first).

## Not yet / not verified
- Root login by password and the 2FA flow are unchecked (testing used an API token)
- System scope (pkexec helper, system units) is only covered by a unit test with a fake runner
- Prune is missing; so is key rotation (a key is never overwritten)

## Installation
One command (Debian 13 / Ubuntu), details in [docs/INSTALLATION.md](docs/INSTALLATION.md):
```bash
curl -fsSL https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh | sudo sh
```
As a Debian package from source (Debian 13; pure Python, uses `python3-textual` and `python3-pyside6.*` of the distribution):
```bash
scripts/build-deb.sh dist && sudo apt install ./dist/pbstui_*_all.deb ./dist/pbsgui_*_all.deb   # pbstui only: without Qt, for servers
```
The signed APT repository is on GitHub Pages (`https://nienhausdavid.github.io/pbsgui`), see [docs/RELEASING.md](docs/RELEASING.md).
The `proxmox-backup-client` is available from the Proxmox client repository (`pbs-client`).

## Restore single files (file-level restore)
File browser for every snapshot: GUI (Snapshots → “Browse files …”), TUI (select a snapshot, `f`; space marks, `r` to a folder, `o` overwrite, `/` search) and command line:
```bash
pbsgui files NAME [FOLDER] [--search PATTERN]                       # look at / search the content
pbsgui restore NAME --path /Documents --path /note.txt --target ~/back   # restore a selection into a folder
pbsgui restore NAME --path /Documents --overwrite                  # HOME ONLY: replace at the original location (with confirmation)
```
The file tree comes from `catalog dump` (the client decrypts it itself, so this also works for encrypted backups); the data is read through a read-only FUSE `mount`. **Overwriting** is only allowed for paths in your own home directory (otherwise it aborts), only replaces existing files, deletes nothing and saves the old versions to `~/.local/share/pbsgui/overwritten/<time>/` first. Needs FUSE (`fusermount3`).

## Across all snapshots: versions, search, comparison, restore test
```bash
pbsgui versions NAME /Documents/a.txt [--restore 2 --target ~/back]   # all versions of a file
pbsgui find NAME "*.odt"                                              # search all snapshots, also deleted files
pbsgui diff NAME [OLD] [NEW]                                          # new / changed / deleted (default: the last two)
pbsgui preview NAME /Documents/a.txt --snapshot 3                     # look at the content without restoring
pbsgui restore-test NAME --count 5                                    # restore a sample and check it (exit code 1 on error)
```
GUI: file browser with preview and “Versions …”; in the Snapshots tab “Compare …”, “Restore test …” and “Search in all snapshots …”.

## Overview: dashboard, history, tray
`pbsgui dashboard` (also `--json`, `--offline`) shows status, duration, size, growth and the datastore usage per profile; `pbsgui history`
(`--failed`, `--search TEXT`, `--show RUN`) the stored runs with their output. In the GUI these are the “Dashboard” and “History” tabs.
`pbsgui tray` starts the GUI as an icon in the system tray (status, “Back up now”, failure notifications); the tray menu lets you choose
whether it keeps running when the window is closed and whether it starts at login.

## Restore on a new PC (full backup)
Status: the preparation **and** a guided restore (`pbsgui rescue`, rescue ISO) are built in; guide with screenshots: [docs/RESTORE.md](docs/RESTORE.md).
- With every full backup (as root) the app collects disk metadata into `/var/lib/pbsgui/recovery/<profile>/` and thereby includes it in the backup: partition tables (sfdisk/sgdisk), LVM metadata, LUKS headers (only for an encrypted backup), UUIDs, EFI entries, package list, `MANIFEST.json`.
- **Mandatory:** an encrypted full backup (and its schedule) only starts once the key has been exported (`pbsgui key export`), because the key in the secret store is bound to the old machine.
- `pbsgui recovery NAME` shows the checklist, `--sheet FILE` writes the **emergency sheet** (server, fingerprint, namespace, instructions; no secrets) for printing.
- **Rescue ISO** (Debian 13 live, UEFI + BIOS, `scripts/build-iso.sh`; with WireGuard, OpenVPN and NetBird for servers behind a VPN, updates itself at boot): boots into a wizard, rebuilds GPT/LUKS/LVM/file systems with the old UUIDs, restores the backup and reinstalls the bootloader. During the restore you can choose LUKS as in the original, re-encrypt (new passphrase) or no encryption. A **smaller target disk** is possible (“resize” / `--shrink`): partitions and LVs are shrunk to used space plus a reserve, EFI and `/boot` stay, all UUIDs stay. Also available as `pbsgui rescue …` (command line).
- Limits: file-based, not an image; different hardware, Secure Boot and TPM-bound LUKS keys may need manual work; a running system is backed up without a snapshot.

## Usage: GUI, CLI, TUI
All three use the same core (`pbsgui/core`) and can be combined freely.

| Call | What |
|---|---|
| `pbsgui` | Qt GUI (needs `pip install 'pbsgui[gui]'`) |
| `pbstui` (or `pbsgui tui`) | Terminal interface (Textual), also works over SSH without a display |
| `pbsgui list / show / add / server / probe / backup / snapshots / restore / schedule / status / key / remove` | Command line, scriptable (`--json` for `list`, `snapshots`) |

`scripts/install-links.sh` creates `pbsgui` and `pbstui` as links in `~/.local/bin` (on the PATH from the next login).

Examples:
```bash
pbsgui probe --server pbs.example.org --root-user root@pam        # look at datastores/namespaces
pbsgui add --name Laptop --server pbs.example.org --datastore main --namespace laptop   # way A (root, once), home only
pbsgui add --name Server --server pbs.example.org --datastore main --mode full             # entire system (needs admin)
pbsgui mounts                                  # show detected drives (internal/USB/bind)
pbsgui mounts Server --set /boot --set /data   # change the selection of a full-backup profile (--auto, --none)
pbsgui add --name Laptop --server pbs.example.org --datastore main --token-id user@pbs!tok  # way B
pbsgui server Laptop --server pbs2.example.org --fetch-fingerprint --datastore backup   # change the server afterwards
pbsgui key export Laptop ~/laptop-key.json                       # back up the key!
pbsgui key copy-kit Laptop                                       # or: copy key + emergency sheet to the clipboard (password manager)
pbsgui key master-create Laptop --output ~/master-private.pem   # master key: insurance against losing the key (then store the file + passphrase safely)
pbsgui key recover Laptop --master-key ~/master-private.pem --output ~/key.json   # get the key back from a snapshot
pbsgui schedule Laptop --calendar daily --scope user
pbsgui backup Laptop && pbsgui snapshots Laptop && pbsgui restore Laptop --target ~/restore
pbsgui language en                             # choose the interface language (en, de, auto)
```
Secrets are never accepted as an argument: prompt or `PBSGUI_TOKEN_SECRET` / `PBSGUI_ROOT_PASSWORD`.
TUI keys: M master key, a add, b backup, r snapshots, t restore, f files, x compare, w restore test, g search all snapshots, e dashboard, h history, i change server, s schedule, m mode, v encryption, k back up key to a file, y copy key + emergency sheet to the clipboard, o sources, u drives, p DB dumps, n emergency sheet, d remove, q quit. In the file browser: p preview, v versions.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # tests (fake client under tests/)
sudo env PYTHONPATH=src PATH=$PATH:/usr/sbin .venv/bin/python -m pytest tests/test_rescue_integration.py   # layout rebuild with real loop devices (root)
.venv/bin/pbsgui                  # start the GUI
scripts/install-desktop.sh        # install launcher + icon into the application menu (~/.local/share)
```
End to end in QEMU (backup → ISO restore → boot): `tests/vm/` (see the head of `e2e.py`).
Debian/Ubuntu: `sudo apt install python3-venv`, plus the `proxmox-backup-client` from the Proxmox client repo.

Texts: the source strings are German and double as keys; the English translations live in `src/pbsgui/i18n/en_*.py`
(`tr("German text {0}", value)`). A test checks that every `tr()` text has a translation with matching placeholders.

## License
[AGPL-3.0-or-later](LICENSE).
