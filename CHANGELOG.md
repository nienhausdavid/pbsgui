# Changelog

**English** · [Deutsch](CHANGELOG.de.md)

All notable changes are listed here. Format after [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions after [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`
(MAJOR = incompatible change, MINOR = new feature, PATCH = bug fix).
Version 0.x: the interfaces (profile format, commands) may still change.

## [Unreleased]

## [0.9.1] - 2026-10-24

### Added
- **Copy the key and emergency kit to the clipboard:** GUI buttons “Copy key”, “Copy key + emergency sheet” and “Copy emergency sheet”,
  the same buttons in the TUI overview (and key `y`), `pbsgui key copy|copy-kit|copy-sheet NAME` (uses `wl-copy`, otherwise `xclip` or `xsel`; the package `pbstui` now depends on `wl-clipboard`;
  the text goes through stdin). The emergency kit contains the emergency sheet and the secret key; the GUI clears the clipboard after 2 minutes.
  Key and kit count as a safe export and lift the block on the encrypted full backup.
- **The backup starts by itself after the key is backed up:** if the full backup is still blocked, the GUI (and the TUI with `b`) offers to copy the key
  and emergency sheet to the clipboard right away; afterwards the backup starts without a further export dialog.

### Fixed
- Tray: the “Back up now” submenu could disappear (a reference was not kept).

## [0.9.0] - 2026-10-24

### Added
- **Change the server afterwards:** the server, port, fingerprint, datastore, namespace and token of an existing profile can be changed
  (`pbsgui server NAME …`, GUI “Change server …” in the Backup tab, TUI `i`). Profile name, key, sources, schedule and history stay.
  The new settings are checked with the real client before saving (otherwise everything stays unchanged); a changed token ID needs its
  secret, a new fingerprint must be confirmed explicitly. Existing snapshots stay on the old target, the next backup uploads everything again
  to the new one (the key still fits).
- **Bilingual: German and English.** GUI, terminal interface, command line, rescue wizard and web interface (`index.en.html`)
  follow the system language (`LANG`); choose explicitly with `pbsgui language de|en|auto` (file `~/.config/pbsgui/language`, in the GUI at the bottom left)
  or `PBSGUI_LANG`. The source strings are German and double as keys (`tr("Text {0}", value)`), the translations live in
  `src/pbsgui/i18n/en_*.py`; a test checks that every text is translated and the placeholders match. README, installation, restore,
  security and the CHANGELOG exist in English (`README.md`, `docs/INSTALLATION.md`, `docs/RESTORE.md`, `SECURITY.md`, `CHANGELOG.md`);
  the German versions are called `*.de.md`.

### Added
- **VM backups from Proxmox VE onto physical disks (V2P)** with the rescue ISO: snapshots `vm/<id>/…` with image archives
  (`drive-*.img`) can be written to one or more disks (command line `pbsgui rescue vm-snapshots|vm-run`,
  tick box in the terminal wizard, “Kind of restore” in the web interface). Mapping of source to target disks, boot disk from the
  VM configuration, confirmation by typing all target disks. Afterwards automatic repairs that can be switched off individually, for
  Debian/Ubuntu guests: enlarge partition, LUKS, LVM and file system to a larger disk, initramfs with all drivers
  (`MODULES=most`), GRUB (UEFI with fallback, or BIOS), network interface names, `qemu-guest-agent` and `cloud-init`. Checked in
  QEMU/UEFI (VM with LUKS, LVM, two disks, larger target disks: boots, data matches) and with real loop devices.
  Windows/Solaris guests and smaller target disks are rejected.

### Added
- **Restore and search across all snapshots** (GUI, TUI, command line):
  - **File versions:** `pbsgui versions NAME /path/file` shows all versions of a file from all snapshots (same size and
    modification time = same version), `--restore NO --target FOLDER` fetches one of them. GUI: file browser → “Versions …” with a preview
    of every version; TUI: `v` in the file browser.
  - **Search in all snapshots:** `pbsgui find NAME PATTERN` also finds long-deleted files (column “In newest”). GUI:
    Snapshots → “Search in all snapshots …”; TUI: `g`. The file lists are cached per snapshot
    (`~/.cache/pbsgui/catalogs`, readable only by the user; for encrypted backups file names are stored there in plain text),
    the first run is slow, further ones are fast.
  - **Compare two snapshots:** `pbsgui diff NAME [OLD] [NEW]` (default: previous against newest), new/changed/deleted
    files, whole folders summarized. GUI: “Compare …”; TUI: `x`.
  - **Preview** in the file browser (GUI: text, images, hex dump next to the tree; TUI: `p`; `pbsgui preview NAME PATH`), without
    restoring. Symbolic links are never followed.
  - **Restore test:** `pbsgui restore-test NAME` fetches random files into a throwaway folder (in RAM) and checks
    readability, size and, if the original is unchanged, the SHA-256. Exit code 1 on error, so the test can be used from a
    schedule. GUI: “Restore test …”; TUI: `w`. The last result appears in the dashboard.
- **Overview and operation:**
  - **Dashboard** (`pbsgui dashboard`, GUI tab, TUI `e`): per profile status, last backup, duration, size, growth against the
    previous snapshot and 30 days ago, number of snapshots, warnings (overdue backup, errors, key not backed up);
    plus datastore usage and deduplication factor (as of the last garbage collection) if the token may read them.
    Deduplication per profile cannot be determined exactly from the client, so the value applies to the whole datastore.
  - **History** (`pbsgui history`, GUI tab, TUI `h`): every run (scheduled or manual) is stored with its output
    (`<state>/history`, the last 200 per profile), filterable by profile, “errors only”, period and text.
  - **Tray icon:** shows “running”, “error” or “all good”, a “Back up now” menu per profile, reports new errors on the desktop.
    Optionally the interface keeps running in the background when closed and starts at login (`pbsgui tray`, both
    can be set in the tray menu). The schedules keep running independently through systemd.

### Added
- **Web interface for the restore** (rescue ISO, for VPS and servers without a convenient console): `pbsgui web --daemon`, `F5` or
  the “Web” button in the wizard. Shows the address, a random password (4 words, each followed by a number, about 78 bits) and the TLS fingerprint.
  In the browser the same steps as in the terminal wizard: VPN, server, snapshot, disk and encryption, plan, confirmation
  by typing the device name, live log, restart. Standard library only (HTTPS, self-signed certificate).
  Protection: rate limiting, lockouts of growing length, shutdown after too many failed attempts, **honeypot** (invisible
  form field and decoy addresses such as `/wp-login.php`, `/.env`: hits are locked out and reported on the console), a single session
  with idle and total time limits, CSRF token, content security policy, limited request size and connections. `--bind`, `--port`,
  `--timeout`, `--status`, `--stop`.

### Added
- **Rescue ISO with VPN:** WireGuard, OpenVPN and NetBird are included (NetBird from the NetBird repository). In the wizard with `F3` or the “VPN” button
  (find a configuration on a USB stick, connect, disconnect, status), on the command line with `pbsgui vpn status|find|up|down`.
  Passwords and setup keys are never passed as a program argument (OpenVPN credentials only briefly in a 0600 file in RAM, NetBird through the
  environment `NB_SETUP_KEY`).
- **Programs update themselves when the ISO starts:** `pbsgui-update.timer` runs `pbsgui update` at boot (and repeats every 2 minutes until it
  works): fetch package lists and update `pbstui`, `proxmox-backup-client`, `netbird`, `wireguard-tools`, `openvpn`
  only if installed. Sources: pbsgui repository, Proxmox, NetBird, Debian (now also in the finished live system).
  The wizard waits up to 45 s for the result at start (any key skips); by hand with `F4` in the wizard or
  `pbsgui update`; after an update of `pbstui` the wizard restarts on request.

## [0.8.3] - 2026-10-24

### Changed
- **Two Debian packages:** `pbstui` (core, command line `pbsgui`, terminal interface `pbstui`, schedules `pbsgui-run`; without Qt, for
  servers) and `pbsgui` (Qt interface and launcher; depends on `pbstui` of the same version, so it includes it). Until now everything was
  in one package, with the Qt parts only recommended. An upgrade from 0.8.2 or earlier works without manual steps (`Replaces`/`Breaks`;
  tested in a container). The installer `scripts/install.sh --no-gui` installs only `pbstui`; the rescue ISO contains only
  `pbstui`. `pbsgui` without arguments explains how to install the GUI later if only `pbstui` is present.

## [0.8.2] - 2026-10-24

### Security
Review with `ruff`, `bandit`, `pip-audit` and a walk through the security-critical places; no serious findings, but these hardenings:
- **No root code from user folders:** full backup (`pkexec`) and system schedules only start Python as root if the code
  and the interpreter belong to root and are not writable by others (Debian package). In a development checkout or
  a virtual environment this is refused, with a hint; exception only with `PBSGUI_ALLOW_USER_CODE_AS_ROOT=1`.
- **Root helper** (`pbsgui.cli.admin`) now only accepts the known kinds of secrets (`token`, `keypass`, `key`, `dbdump`) and
  schedule actions; before, a name such as `../../x` would have created a file outside the secret folder.
- **systemd units:** profile name and schedule are stripped of control characters (line break) and `%` so that they cannot smuggle in their own
  directives; additionally `ProtectKernelModules`, `ProtectControlGroups`, `LockPersonality`, `RestrictSUIDSGID`.
- **Key in RAM:** the briefly decrypted key lives in `/run` or `/dev/shm` also as root (where `XDG_RUNTIME_DIR` is missing),
  no longer in the disk-backed `/tmp`. An exported key always gets mode 0600, even if the file already existed.
- **PBS API:** datastore and token names are escaped in the path.
- `assert` as a runtime check replaced by real error checks (also effective with `python -O`); silent `except: pass`
  in the rescue interface now reports the reason.
- **CI:** new job “Security checks” (`ruff`, `bandit`, `pip-audit`); new tests `tests/test_security.py`;
  `SECURITY.md` describes protections and limits.

## [0.8.1] - 2026-10-24

### Added
- **Dark mode** in the GUI: switch at the top right with “System” (follows the desktop, also when it changes at runtime), “Light” and
  “Dark”; the choice is remembered.
- **Own title bar with minimize, maximize/restore and close** at the top right (GNOME otherwise shows neither minimize nor
  maximize). Move by dragging the bar, double-click maximizes, resizing at all edges
  and corners. With `PBSGUI_NATIVE_FRAME=1` the system frame stays.

### Changed
- **Higher contrast** in the GUI: text at least 7:1, secondary text at least 4.5:1, borders at least 3:1 (secured by a
  test); ticks, selection and tabs clearer, uniform colors also in dialogs.

## [0.8.0] - 2026-10-24

### Added
- **Restore onto a smaller disk** (“resize”; wizard: tick box when the disk is too small,
  `pbsgui rescue run --shrink`, offered interactively). EFI and `/boot` keep their size, swap is shrunk
  proportionally (at least 1 GiB), the remaining partitions and LVs share the rest proportionally, but never
  below used space + 20 % reserve (at least 2 GiB); otherwise an error message with the required minimum size. UUIDs are
  kept, the LVM metadata is rewritten for the smaller physical volume, the partition table rewritten
  (GPT backup header at the end of the disk). The plan shows old -> new. Works with all three
  encryption modes. Not supported: VGs across several disks, thin pools/snapshots/RAID, extended MBR partitions.
  Checked in QEMU/UEFI with a real full backup (477 GB laptop, 33 GiB used) onto a 64 GB disk: all UUIDs
  the same, the system boots up to the login.
- Full backups store the used space per mount point as `fsusage.json` in the recovery data (basis
  for “resize”; for older backups it is estimated from the snapshot size).

### Changed
- **Faster GUI:** starting took about 4 s because the key secret was decrypted twice through `systemd-creds`,
  only to display “key present?”. Now a file check is enough (`SecretStore.exists`); start and profile switch
  take about 0.1 s.
- **Reworked GUI:** uniform look (Fusion style, stylesheet based on the system palette, light and dark),
  header with server and scope, settings in groups and scrollable, fixed action bar (“Back up now”),
  log in its own movable area. The window can be shrunk to about 700 × 490 (before at least
  780 × 780) and remembers its size and position.

## [0.7.0] - 2026-10-24

### Added
- **Passwordless dump access for MariaDB, entirely without a root password:** `pbsgui dbdump NAME --setup-access CONTAINER` (TUI:
  button “Set up without a password” in `p`, GUI: the same button in the dialog “Databases and access”) creates a user `mysql`
  with the login type `unix_socket` in the container (read-only; only applies to the operating system user of the same name in the
  container, i.e. to everyone allowed to `docker exec`, as with Postgres). No password is needed and none is stored: the container
  is stopped cleanly once for about 15 to 40 seconds, a throwaway container with the same image and the same data runs the SQL file
  with `--init-file`, then the original container runs again (also on errors, otherwise with the hint `docker start …`). Once the access
  is set up it is preferred for the dump; a stored password remains as a fallback. Measured with `mariadb:11`: dump without a password
  works, `DROP`/`INSERT`/`CREATE USER` are rejected, as `root` in the container the user is rejected.

## [0.6.1] - 2026-10-24

### Changed
- **Database dumps are backed up uncompressed** (`*.sql` instead of `*.sql.gz`). PBS compresses the blocks itself and
  recognizes unchanged parts; a gzip stream on the other hand changes completely and was uploaded in full on every run
  (measured: 140 MiB per run for the Immich dump). A new dump replaces the old `.sql.gz` of the container;
  if the new dump fails, the old one is kept.

## [0.6.0] - 2026-10-23

### Added
- **Change detection (`--change-detection-mode metadata`):** the client only reads files whose metadata changed since
  the last backup, instead of reading every file completely every time (measured: 0 MiB read for
  unchanged data). On by default when creating in the GUI, TUI and `pbsgui add` (`--change-detection legacy`
  switches it off); existing profiles stay classic and can be switched: GUI (Backup tab), TUI (`c`),
  `pbsgui detection NAME [metadata|legacy]`. The first backup after switching reads everything once. Needs a
  PBS server from version 3.3. The progress bar then shows “changed files only” instead of a percentage.
- **Weekly check run in mode `data` (on by default):** a second systemd timer (`pbsgui-NAME-data.timer`,
  Sundays 03:00 by default) reads everything again once a week, thereby also checks files with unchanged metadata
  and catches read errors in rarely used files. It only exists with change detection `metadata` and a
  schedule. Adjustable at creation (GUI, TUI, `pbsgui add --weekly-data/--no-weekly-data --data-schedule`) and afterwards
  (GUI Backup tab, TUI `c`, `pbsgui detection NAME --weekly-data on|off --data-schedule …`); `pbsgui backup NAME --data`
  reads everything once; `pbsgui status` shows the timer.
- **Lock per profile:** never more than one backup of the same profile runs at the same time (schedule, weekly run and
  manual start). A second run waits up to 4 hours for the first one instead of aborting with a PBS error.

### Changed
- Snapshots in the new mode contain split archives (`x.mpxar.didx` + `x.ppxar.didx` instead of `x.pxar.didx`). pbsgui
  treats both as the same archive `x.pxar` (file browser, restore, rescue ISO); `restore` and `mount`
  of the client still accept the name `x.pxar`.

## [0.5.0] - 2026-10-23

### Added
- **TUI, add server:** drop-downs for **datastore** and **namespace** (filled after “Check connection”; namespace
  additionally “root” and with way A “Create a new namespace …”). The input fields stay as a fallback and stay in sync with
  the selection. (The GUI already had editable selection lists.)
- **Schedule at creation** (GUI, TUI and `pbsgui add --schedule`): choice from hourly to weekly or a custom
  OnCalendar expression with a preview of the next run. An invalid expression is checked before any access is
  created. For a full backup with an automatic key the schedule is only set up after the key export
  (TUI `k`, GUI “Export the key”, `pbsgui key export|confirm`).
- **Start the first backup right away** (GUI, TUI and `pbsgui add --backup-now`). If a full backup is blocked (key not yet
  exported) it explains what is missing instead of failing; with database dumps the access setup comes first.
- **Database dumps (optional)** in the GUI, TUI and command line: Postgres and MariaDB/MySQL containers are dumped before the
  backup and backed up as the separate archive `dbdumps.pxar`. Detection over all Docker-compatible sockets
  (system daemon and rootless daemons of all users) by the mounted data directories, not by names;
  stopped containers are not dumped. Postgres needs no access. For MariaDB/MySQL an access is
  stored explicitly (encrypted, never read from `.env` or the container environment) or a dump user with
  read-only permissions is created; passwords only go through stdin. A failed dump never prevents the backup, the
  last good dump is kept. Dumps are overwritten per container; pbsgui cleans up files of containers that have not been
  detected for more than 14 days (never detected containers).
  Operation: checkbox at creation, TUI `p`, GUI “Databases and access …”, `pbsgui add --db-dump`,
  `pbsgui dbdump NAME [--on|--off|--set|--create-user|--remove|--run]`.

## [0.4.0] - 2026-10-23

### Added
- **License:** AGPL-3.0-or-later (`LICENSE`, package metadata, Debian `copyright`).
- **Installation with one command:** `scripts/install.sh` sets up APT repositories and all packages (`--no-gui`, `--no-client`,
  `--version`, `--uninstall`); new guide `docs/INSTALLATION.md`.
- Signed APT repository on GitHub Pages (`https://nienhausdavid.github.io/pbsgui`).
- **Progress during the backup:** bar with percentage, amount, speed and remaining time in the GUI (Backup tab), TUI
  (Log tab) and command line (`pbsgui backup`). The client itself prints no progress during the backup; the app measures
  the read counter of the client and compares it with the size of the last backup (for the first full backup with the
  used space, for the first home backup indeterminate). Checked against the real server: 480 of 480 MB read.

### Fixed
- Release workflow: the ISO is built in a Debian 13 container (the `live-build` of the Ubuntu runners was too old);
  with `workflow_dispatch` and `tag=vX.Y.Z` an ISO can be attached to an existing release afterwards.
- The `.sha256` file of the ISO contains the file name without a path.
- `pbsgui-run` and `pbsgui backup` print output immediately (before, buffered when piped).

## [0.3.0] - 2026-10-18

First published version. (The intermediate states 0.1.0 and 0.2.0 were internal development states only.)

### Added
- **Interfaces:** Qt GUI (`pbsgui`), terminal interface (`pbstui` / `pbsgui tui`) and command line
  (`pbsgui list|show|add|probe|mounts|backup|snapshots|files|restore|schedule|status|key|recovery|rescue|remove`).
- **Setup:** way A (once as root: user, token, namespace and ACLs are created) or way B
  (existing API token); fingerprint pinning, datastore and namespace check.
- **Backup:** home or entire system (file level), selectable drives (internal/USB/bind duplicates detected),
  client-side encryption on by default with an automatically generated key.
- **Schedules:** systemd timers as user or system units; secrets through `systemd-creds`.
- **Restore on a new PC:** disk metadata and emergency sheet, mandatory key export,
  rescue ISO (Debian 13, UEFI/BIOS) with a guided wizard (`pbsgui rescue`); LUKS as in the original,
  re-encrypted or without encryption.
- **Restore single files:** file browser (GUI/TUI/CLI) with search; overwriting at the original location only in the
  home directory, with a backup of the old files.
- **Packaging:** `.deb` package, signed APT repository script, GitHub workflows (CI, release, ISO).
- `pbsgui --version` and version display in the GUI and TUI.

### Known limitations
- Not tested on real hardware, with BIOS boot of the ISO, Secure Boot or Wi-Fi (tests ran in QEMU/UEFI).
- System-scope schedules and root password login with 2FA are only tested to a limited extent in practice.
- The rescue ISO brings no FUSE; the file browser is not intended there.

[Unreleased]: https://github.com/nienhausdavid/pbsgui/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/nienhausdavid/pbsgui/releases/tag/v0.9.1
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
