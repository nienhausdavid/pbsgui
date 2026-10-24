# Installation

**English** · [Deutsch](INSTALLATION.de.md)

pbsgui runs on **Debian 13** and **Ubuntu** (25.04 or newer recommended; on 24.04 see the notes below).
You need a Proxmox Backup Server (PBS) and either its `root` access (once, for the setup) or an
existing API token.

## Quick start: one command

```bash
curl -fsSL https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh | sudo sh
```

The script [`scripts/install.sh`](../scripts/install.sh) does everything in one go:

1. adds the signed **pbsgui APT repository**,
2. adds the **Proxmox client repository** (`proxmox-backup-client`),
3. installs `pbsgui` with the GUI (Qt), terminal interface, command line and the tools for full backups
   (`fdisk`, `gdisk`, `efibootmgr`, `fuse3` for the file browser).

You can look at the script first (recommended) and run it locally:

```bash
curl -fsSLO https://raw.githubusercontent.com/nienhausdavid/pbsgui/main/scripts/install.sh
less install.sh
sudo sh install.sh
```

Options (after `sh install.sh`, with `sh -s -- …` when piping):

| Option | Effect |
|---|---|
| `--no-gui` | only the package `pbstui` (terminal interface and command line, without Qt; servers without a desktop) |
| `--no-client` | do not install `proxmox-backup-client` (already present or provided otherwise) |
| `--version 0.4.0` | install a specific version |
| `--uninstall` | remove pbsgui and the pbsgui repository (profiles under `~/.config/pbsgui` stay) |
| `-y` | apt without questions |

## Manually

```bash
# 1. pbsgui repository
curl -fsSL https://nienhausdavid.github.io/pbsgui/pbsgui.gpg | sudo tee /usr/share/keyrings/pbsgui.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/pbsgui.gpg] https://nienhausdavid.github.io/pbsgui stable main" | sudo tee /etc/apt/sources.list.d/pbsgui.list

# 2. Proxmox client repository
sudo curl -fsSL https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg -o /usr/share/keyrings/proxmox-archive-keyring.gpg
printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pbs-client\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' | sudo tee /etc/apt/sources.list.d/pbs-client.sources

# 3. Install
sudo apt update
sudo apt install pbsgui proxmox-backup-client fdisk gdisk efibootmgr fuse3   # with GUI (pulls in pbstui)
# terminal interface and command line only, without Qt (servers):
# sudo apt install pbstui proxmox-backup-client fdisk gdisk efibootmgr fuse3
```

Alternatively download the `.deb` from the [GitHub releases](https://github.com/nienhausdavid/pbsgui/releases) and install it with
`sudo apt install ./pbstui_<version>_all.deb ./pbsgui_<version>_all.deb` (without a desktop `pbstui` is enough).

## From source (development)

```bash
sudo apt install python3-venv
git clone https://github.com/nienhausdavid/pbsgui && cd pbsgui
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pbsgui                  # GUI
scripts/install-desktop.sh        # launcher in the application menu
```

## Updating and removing

```bash
sudo apt update && sudo apt upgrade     # pulls new pbsgui versions from the repository
sudo sh install.sh --uninstall          # or: sudo apt remove pbsgui
```

## First setup

1. **Start:** `pbsgui` (GUI) or `pbstui` (terminal).
2. **Add a server:**
   - *Way A:* once, with the `root` access of the PBS. pbsgui creates the user, API token, namespace and minimal
     permissions. The root password is not stored.
   - *Way B:* enter an existing API token.
   The TLS fingerprint of the server is shown for confirmation and pinned afterwards. You can change the server, datastore, namespace
   or token later (GUI “Change server …”, TUI `i`, `pbsgui server NAME …`).
3. **Choose the scope:** *home only* (no sudo needed) or *entire system* (administrator rights).
4. **Encryption:** is on by default, the key is created automatically. **Export it** and keep it
   separate from the computer (`pbsgui key export NAME FILE`): without it the backups are lost if the computer fails.
   An encrypted full backup only starts after this export.
5. **Set a schedule** (GUI: Schedule; CLI: `pbsgui schedule NAME --calendar daily --scope user|system`).
6. **Print the emergency sheet** and keep the rescue ISO ready: [RESTORE.md](RESTORE.md).
7. **Language:** the interface follows the system language; `pbsgui language en|de|auto` sets it explicitly.

Example completely on the command line:

```bash
pbsgui add --name Laptop --server pbs.example.org --datastore main --namespace laptop
pbsgui key export Laptop ~/laptop-key.json
pbsgui schedule Laptop --calendar daily --scope user
pbsgui backup Laptop
```

## Notes on Ubuntu and servers

- **Ubuntu 24.04:** systemd 255, so pbsgui stores secrets for user schedules as a file (0600) instead of with
  `systemd-creds --user`. The Proxmox client from the Debian 13 repository needs a newer glibc than 24.04 provides;
  use `--no-client` there and provide the client yourself.
- **Without a desktop:** `--no-gui` installs only the package `pbstui` (no Qt) and brings `pbstui`, `pbsgui` (command line) and `pbsgui-run`; `pbstui` also works over SSH.
- **Two packages:** `pbstui` contains the core, command line, terminal interface and schedules; `pbsgui` contains the Qt interface and the launcher and depends on `pbstui`, so it includes it.
- **Schedules without a login (user mode):** `loginctl enable-linger $USER`.

## Rescue ISO

To restore a full backup on a new PC, download `pbsgui-rescue-<version>-amd64.iso` from the
[releases](https://github.com/nienhausdavid/pbsgui/releases) and write it to a USB stick; the guide with
screenshots is in [RESTORE.md](RESTORE.md).

## License

[AGPL-3.0-or-later](../LICENSE).
