# pbsgui – Plan

**English** · [Deutsch](PLAN.de.md)

GUI for `proxmox-backup-client`. Not a client of its own: a thin, testable wrapper around
the official client, the PBS API and systemd. (This is the original design document; the README and the CHANGELOG describe the current state.)

Status: prototype (see README). Profiles are stored as JSON, not TOML. First target platform: Linux (Debian 13, Ubuntu). Windows/macOS later.

## Stack

- Python 3.11+ with PySide6 (Qt 6) for the GUI
- `proxmox-backup-client` as an external program (subprocess, `--output-format json`)
- PBS HTTP API (port 8007) for provisioning and permission checks
- systemd for schedules

## Layers

```
pbsgui/
  core/      logic without UI (testable)
    client.py      wrapper around proxmox-backup-client
    pbs_api.py     login, create user/token/ACL, check permissions
    models.py      Profile, Snapshot, Job
    store.py       profiles (~/.config/pbsgui/ or /etc/pbsgui/)
    secrets/       SecretStore interface + backends (see below)
    scheduler/     Scheduler interface + systemd backend
    runner.py      runs a profile (hooks, logs, exit code)
  cli/       pbsgui-run <profile>   (called by the timers)
  gui/       PySide6
  tests/
```

Secrets and scheduler are interfaces from the start, so that Keychain/launchd
(macOS) and DPAPI/Task Scheduler (Windows) can be added later as further backends.

## Setting up a server

### Way A: once as root (provisioning)

1. Fetch the TLS fingerprint of the server and show it for confirmation (TOFU), then pin it.
2. Log in as `root@pam` through `POST /access/ticket` (including TOTP if active).
3. Create through the API:
   - a user per host (e.g. `pbsgui-<hostname>@pbs`)
   - an API token for this user
   - optionally a namespace per host in the datastore
   - ACLs with minimal permissions, default: role `DatastoreBackup` on
     `/datastore/<name>[/<ns>]`. User **and** token each need an ACL
     (effective permissions = intersection). Prune permissions only optional.
     Check the roles against the PBS documentation before implementing.
4. The token secret is delivered only once and stored encrypted right away.
   The root password is never stored.

### Way B: existing API credentials

Enter repository, token ID and secret. `GET /access/permissions` checks whether the
permissions suffice for the backup and names missing permissions concretely.

Additionally: remove the entry again (delete token/user on the server).

## Encryption

- Transport: TLS with a pinned fingerprint.
- Backup content: client-side key (`key create`). Additionally a master key
  (`key create-master-key`) for emergency restores. The GUI enforces backing up both keys.
- Secrets (token, passwords, key passphrase) are never stored in plain text on disk.

### SecretStore backends (Linux)

| Backend | When | Note |
|---|---|---|
| `systemd-creds` (system) | mode "system" | `LoadCredentialEncrypted=`, bound to host/TPM |
| `systemd-creds --user` | mode "user", systemd >= 256 | Debian 13 (257), Ubuntu from 25.04. The runner decrypts itself; `LoadCredentialEncrypted=` fails in the user manager (tested, exit 243) |
| File 0600 | fallback in mode "user" | e.g. Ubuntu 24.04 (systemd 255) |
| libsecret/keyring | GUI interaction only | not for timers: no session without a login |

Ubuntu 24.04 LTS has systemd 255, which has no user-scope `systemd-creds`.
The app detects the version and chooses the backend, with a clear note about the
weaker level.

## Schedules (systemd)

The mode is **selectable per profile**. The app suggests a default, the user
decides:

- **System** (`/etc/systemd/system/`): installed once through `pkexec`. Backs up the
  whole system (also other users' files). Only selectable if admin rights are present;
  otherwise the option is disabled and explains why.
- **User** (`~/.config/systemd/user/`): needs no sudo, backs up only the user's data
  (home). For timers to run without a login: `loginctl enable-linger`.
  Also selectable with sudo, e.g. if only home should be backed up.

Default suggestion: system if admin rights are detected, otherwise user.
A profile can later be moved with "change mode" (units and credentials
are recreated in the new scope, the old ones removed).

Per profile `.service` + `.timer`:

- The service calls `pbsgui-run <profile>`, loads credentials, `After=network-online.target`,
  hardening (`NoNewPrivileges`, `ProtectSystem` where possible).
- Optional conditions: network, power (`ConditionACPower`).
- Schedule through `OnCalendar=`, `Persistent=true` for missed runs.
- Logs in the journal and in the GUI; status "last run" and "next run".

## Milestones

1. Core + CLI: client wrapper, profiles, `pbsgui-run`, tests with a fake client.
2. GUI skeleton: set up a server (way A/B), test the connection, list snapshots.
3. Backup: paths, excludes, live log.
4. Restore: catalog browser, default = new target folder.
5. Schedules: systemd generator (system/user), status in the GUI.
6. Encryption (key/master key) and prune with a dry run.
7. Packaging: pipx, later .deb/Flatpak. Then Windows/macOS backends.

## Open points

- Tests run against a fake client (a script that simulates `proxmox-backup-client` and the
  PBS API). No test PBS needed; a test against a real server can follow later
  as an optional integration test.
- Verify the exact roles/permissions for prune and restore against the PBS documentation.
- License: AGPL-3.0-or-later (see `LICENSE`).
- Note: BackupPilot (AGPL) was only read for orientation, no code is taken over.

## Restore on a new PC (rescue ISO)

Implemented: full backups contain `recovery.pxar` (disk metadata); `pbsgui rescue` (CLI + Textual wizard)
rebuilds the layout with the old UUIDs (`core/rescue.py`), restores `root.pxar` and renews initramfs and
bootloader in the chroot. The ISO is built with `live-build` (`iso/`, `scripts/build-iso.sh`). Tests: plan logic as
unit tests, layout rebuild with real loop devices (`tests/test_rescue_integration.py`, root), end to end in
QEMU (`tests/vm/e2e.py`).
