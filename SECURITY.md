# Security

**English** · [Deutsch](SECURITY.de.md)

## Reporting vulnerabilities
Please do not open public issues for security vulnerabilities. Write to the maintainer through a private message on GitHub
(Security → “Report a vulnerability” in the repository).

## What pbsgui protects and how
- **Secrets:** API tokens, keys and passphrases are stored encrypted in the secret store (`systemd-creds`,
  mode 0600; fallback: file with mode 0600) and are never passed as a program argument. The plain-text key only exists
  briefly in a 0700 directory in RAM (`XDG_RUNTIME_DIR`, `/run` or `/dev/shm`).
- **Connection:** TLS with a pinned fingerprint; nothing is sent before the fingerprint has been compared.
- **Root code:** full backups and system schedules start Python as root. pbsgui only does so if the code and the
  interpreter belong to root and are not writable by others (Debian package); in a development checkout this is
  refused (exception only with `PBSGUI_ALLOW_USER_CODE_AS_ROOT=1`).
- **Root helper:** `pbsgui.cli.admin` accepts only known kinds of secrets and actions; names are sanitized into file names,
  texts in units are stripped of control characters and `%`.
- **Programs** are always started with argument lists, never through a shell with user input.
- **Restore:** “overwrite” is limited to the home directory (symlinks are resolved), old versions are saved beforehand.
- **Changing the server afterwards:** the new settings are checked with the client before anything is stored; a changed
  token ID needs its secret; a changed fingerprint must be confirmed explicitly.
- **Catalog cache and restore test:** the cached file lists (`~/.cache/pbsgui/catalogs`) are readable only by the user (0600/0700);
  for encrypted backups they contain file names in plain text, like the files themselves on that computer. Restore-test samples are
  decrypted only in RAM (`XDG_RUNTIME_DIR` or `/dev/shm`) and removed right away. The file preview never follows symbolic links
  and refuses paths outside the archive.

- **Web interface of the rescue ISO** (only active on request): random passphrase (about 78 bits), HTTPS, rate limiting, lockouts,
  honeypot (hidden field, decoy addresses), shutdown after 40 failed attempts, a single session with time limits, CSRF token,
  content security policy with a nonce, no file or directory sharing, no stack trace to clients, size limits. By default it
  listens on all addresses (needed as long as there is no VPN yet); restrict it to one address with `--bind` if needed.

## Checks
The CI runs `ruff` (error and security rules), `bandit` and `pip-audit`; `tests/test_security.py` secures the properties
listed above.

## Known limits
- The rescue ISO starts a root console (restoring needs root). It is meant for use on a machine you have physical access
  to; the ISO is not meant for permanent operation.
- A compromised user with sudo rights can of course do anything.
- The key in the secret store is bound to the machine; exporting it is mandatory and the user's responsibility.

## Rescue ISO: web interface and VM images
- The web interface (`pbsgui web`) uses HTTPS with a self-signed certificate (fingerprint on the console), a random passphrase
  (4 words and numbers), rate limiting, lockouts of growing length, a honeypot (hidden form field, decoy addresses), a session
  with a CSRF token and a content security policy; it switches itself off after being idle or after too many failed attempts.
- VM images: destructive steps need the typed confirmation of **all** target disks; the choice is restricted to permitted disks
  (no mounted ones, not the live medium); tools always run with empty input so that nothing asks for passwords on the terminal;
  passphrases only go through stdin.
