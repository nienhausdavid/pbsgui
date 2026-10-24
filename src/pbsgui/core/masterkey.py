"""RSA-Master-Schlüssel: erzeugen, öffentlichen Teil einbinden, den Schlüssel aus einem Snapshot zurückholen.

Mit `--master-pubkey-file` legt der Client in jedem Snapshot eine mit dem öffentlichen Master-Schlüssel verschlüsselte Kopie des
Verschlüsselungs-Schlüssels ab (`rsa-encrypted.key.blob`). Mit dem privaten Master-Schlüssel (und seiner Passphrase) lässt sich der
Schlüssel daraus zurückholen, auch wenn das Gerät samt `key.json` verloren ist. Den privaten Teil speichert pbsgui nie.

Der Client fragt die Passphrase nur auf einem Terminal ab; sie wird deshalb über ein Pseudo-Terminal übergeben (nie als Argument).
"""
from __future__ import annotations

import base64
import hashlib
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..i18n import tr

PUBLIC_HEADER = "-----BEGIN PUBLIC KEY-----"
ENCRYPTED_NAMES = ("rsa-encrypted.key", "rsa-encrypted.key.blob")     # Name im Snapshot (mit und ohne .blob)


class MasterKeyError(RuntimeError):
    pass


@dataclass
class MasterKey:
    public_pem: str
    private_pem: str
    fingerprint: str


def pem_fingerprint(public_pem: str) -> str:
    """SHA-256 über den DER-Teil eines öffentlichen PEM-Schlüssels, als `ab:cd:…`."""
    lines = [l.strip() for l in public_pem.strip().splitlines() if l.strip() and not l.startswith("-----")]
    try:
        der = base64.b64decode("".join(lines), validate=True)
    except ValueError:
        raise MasterKeyError(tr("Das ist kein gültiger öffentlicher Schlüssel (PEM)")) from None
    h = hashlib.sha256(der).hexdigest()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def check_public_pem(text: str) -> str:
    text = text.strip() + "\n"
    if not text.startswith(PUBLIC_HEADER) or "-----END PUBLIC KEY-----" not in text:
        raise MasterKeyError(tr("Das ist kein öffentlicher Master-Schlüssel (erwartet: -----BEGIN PUBLIC KEY-----)"))
    pem_fingerprint(text)
    return text


def _ram_dir() -> str | None:
    for d in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):  # noqa: S108  # nosec B108
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return None


def run_with_prompts(argv: list[str], answers: list[str], cwd: str | None = None, env: dict | None = None,
                     timeout: float = 120) -> tuple[int, str]:
    """Startet ein Programm auf einem Pseudo-Terminal und beantwortet jede Passwort-Abfrage der Reihe nach.

    Gibt (Exitcode, Ausgabe; eingegebene Antworten sind durch *** ersetzt) zurück. Kein fork im eigenen Prozess (sicher mit Threads).
    """
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, start_new_session=True, close_fds=True)
    except OSError as e:
        os.close(master)
        os.close(slave)
        raise MasterKeyError(tr("Client nicht gefunden: {0}", e)) from None
    os.close(slave)
    out, given, tail, end = b"", 0, b"", time.time() + timeout
    try:
        while True:
            if time.time() > end:
                proc.kill()
                raise MasterKeyError(tr("Zeitüberschreitung beim Aufruf des Clients"))
            r, _, _ = select.select([master], [], [], 0.5)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 4096)
            except OSError:                       # Gegenstelle geschlossen: Programm ist fertig
                break
            if not data:
                break
            out += data
            tail = (tail + data)[-200:]
            if (b"assword: " in tail or b"assphrase: " in tail) and given < len(answers):
                os.write(master, answers[given].encode() + b"\n")
                given += 1
                tail = b""
    finally:
        os.close(master)
    code = proc.wait()
    text = out.decode("utf-8", "replace").replace("\r", "")
    for a in answers:
        if len(a) >= 4:
            text = text.replace(a, "***")
    return code, text


def _create_unprotected(work: str) -> tuple[Path, Path]:
    """Schlüsselpaar ohne Passphrase: Der Client verlangt beim Erzeugen eine Passphrase, deshalb erzeugt openssl das Paar im selben Format."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise MasterKeyError(tr("Für einen Master-Schlüssel ohne Passphrase wird openssl gebraucht (Paket openssl)"))
    priv, pub = Path(work) / "master-private.pem", Path(work) / "master-public.pem"
    for argv in ([openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:4096", "-out", str(priv)],
                 [openssl, "pkey", "-in", str(priv), "-pubout", "-out", str(pub)]):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)     # noqa: S603  # nosec B603
        if r.returncode != 0:
            raise MasterKeyError(tr("Master-Schlüssel konnte nicht erzeugt werden: {0}", r.stderr.strip()[-300:]))
    os.chmod(priv, 0o600)
    return pub, priv


def create_master_key(passphrase: str, binary: str) -> MasterKey:
    """Erzeugt ein 4096-Bit-RSA-Schlüsselpaar; der private Teil ist mit `passphrase` geschützt (`key create-master-key`).

    Eine leere Passphrase erzeugt einen ungeschützten privaten Schlüssel (dann ist die Datei selbst das Geheimnis)."""
    if passphrase and len(passphrase) < 8:
        raise MasterKeyError(tr("Die Passphrase des Master-Schlüssels ist zu kurz (mindestens 8 Zeichen)"))
    work = tempfile.mkdtemp(prefix="pbsgui-master-", dir=_ram_dir())
    try:
        if passphrase:
            code, out = run_with_prompts([binary, "key", "create-master-key"], [passphrase, passphrase], cwd=work)
            pub, priv = Path(work) / "master-public.pem", Path(work) / "master-private.pem"
            if code != 0 or not pub.is_file() or not priv.is_file():
                raise MasterKeyError(tr("Master-Schlüssel konnte nicht erzeugt werden: {0}", out.strip()[-300:]))
        else:
            pub, priv = _create_unprotected(work)
        public_pem = check_public_pem(pub.read_text())
        return MasterKey(public_pem, priv.read_text(), pem_fingerprint(public_pem))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def recover_key(binary: str, encrypted_key: bytes, master_private_pem: str, passphrase: str) -> str:
    """Holt den Verschlüsselungs-Schlüssel (key.json als Text) aus der RSA-verschlüsselten Kopie zurück (`key import-with-master-key`)."""
    protected = "ENCRYPTED" in master_private_pem.splitlines()[0] if master_private_pem.strip() else False
    if protected and not passphrase:
        raise MasterKeyError(tr("Dieser private Master-Schlüssel ist durch eine Passphrase geschützt"))
    work = tempfile.mkdtemp(prefix="pbsgui-recover-", dir=_ram_dir())
    try:
        (Path(work) / "master.pem").write_text(master_private_pem)
        os.chmod(Path(work) / "master.pem", 0o600)
        (Path(work) / "rsa-encrypted.key").write_bytes(encrypted_key)
        out_file = Path(work) / "key.json"
        code, out = run_with_prompts([binary, "key", "import-with-master-key", str(out_file), "--master-keyfile", str(Path(work) / "master.pem"),
                                      "--encrypted-keyfile", str(Path(work) / "rsa-encrypted.key"), "--kdf", "none"],
                                     [passphrase if protected else "unused"], cwd=work)      # ungeschützter Schlüssel: Antwort wird ignoriert
        if code != 0 or not out_file.is_file():
            raise MasterKeyError(tr("Schlüssel konnte nicht zurückgeholt werden (falsche Passphrase oder falscher Master-Schlüssel?): {0}", out.strip()[-300:]))
        return out_file.read_text()
    finally:
        shutil.rmtree(work, ignore_errors=True)
