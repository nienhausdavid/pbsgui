"""Datenbank-Dumps der laufenden Container vor dem Backup.

Warum: Der Client liest die Dateien einer laufenden Datenbank nacheinander über Minuten. Das Ergebnis ist nur
absturzkonsistent (wie nach einem Stromausfall) und lässt sich meist, aber nicht garantiert wiederherstellen.
Ein Dump ist ein sauberer, jederzeit prüfbarer Stand und wird als eigenes Archiv `dbdumps.pxar` mitgesichert.

Erkennung (nicht nach Namen): Pro Docker-kompatiblem Socket (System-Daemon, rootless-Daemons aller Benutzer,
Podman-Docker-Socket) werden die laufenden Container untersucht. Eine Datenbank liegt vor, wenn ein Datenverzeichnis
eingehängt ist: `/var/lib/postgresql…` (Postgres) bzw. `/var/lib/mysql` (MariaDB/MySQL). Gestoppte Container werden
nicht gedumpt; ihre Dateien sind ohnehin konsistent und stecken im normalen Backup.

Zugangsdaten:
- Postgres: keine. Der Dump läuft im Container über den lokalen Socket (`pg_dumpall`, offizielles Image: trust).
- MariaDB/MySQL: ausdrücklich hinterlegt (Geheimnis `dbdump`, JSON), nie aus .env-Dateien oder der Container-
  Umgebung gelesen. Fehlt der Zugang, wird der Container übersprungen und gemeldet. Das Passwort geht über stdin
  in den Container, nie über die Kommandozeile.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import pwd
import re
import secrets as _secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

POSTGRES = "postgres"
MARIADB = "mariadb"  # auch MySQL
DUMP_USER = "pbsgui_dump"
SOCKET_GLOBS = ("/var/run/docker.sock", "/run/docker.sock", "/run/user/*/docker.sock",
                "/run/podman/podman.sock", "/run/user/*/podman/podman.sock")
DUMP_TIMEOUT = 3600  # Sekunden je Datenbank
STALE_DAYS = 14      # Dump-Dateien von Containern, die nicht mehr erkannt werden, bleiben so lange liegen

# Läuft im Container. $1 = Benutzer; das Passwort kommt als erste Zeile von stdin. MARIADB_HOST/MYSQL_HOST aus der
# Container-Umgebung (z. B. Paperless) würden den Client sonst auf einen nicht auflösbaren Host zeigen lassen.
_MYSQL_PRELUDE = ("unset MARIADB_HOST MYSQL_HOST MARIADB_PWD MYSQL_TCP_PORT; IFS= read -r MYSQL_PWD; "
                  "export MYSQL_PWD; ")
_MYSQL_DUMP = (_MYSQL_PRELUDE + 'D=$(command -v mariadb-dump || command -v mysqldump) || { echo "kein mariadb-dump '
               'im Container" >&2; exit 127; }; exec "$D" --protocol=socket -u"$1" --single-transaction --quick '
               "--routines --events --triggers --all-databases")
_MYSQL_CLIENT = (_MYSQL_PRELUDE + 'C=$(command -v mariadb || command -v mysql) || { echo "kein mariadb-Client im '
                 'Container" >&2; exit 127; }; exec "$C" --protocol=socket -u"$1"')
_POSTGRES_DUMP = 'exec pg_dumpall -U "${POSTGRES_USER:-postgres}"'


class DumpError(RuntimeError):
    pass


def docker_binary() -> str:
    return os.environ.get("PBSGUI_DOCKER", "docker")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "x"


@dataclass(frozen=True)
class Database:
    socket: str
    owner: str       # Benutzer, dem der Daemon gehört (root, dockeruser, …)
    container: str
    engine: str      # POSTGRES | MARIADB

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.container}"

    @property
    def filename(self) -> str:
        return f"{_safe(self.owner)}__{_safe(self.container)}.{self.engine}.sql.gz"


@dataclass
class DumpResult:
    ok: list[Database] = field(default_factory=list)
    skipped: list[tuple[Database, str]] = field(default_factory=list)
    failed: list[tuple[Database, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # Namen entfernter, veralteter Dump-Dateien

    def summary(self) -> str:
        return f"{len(self.ok)} gesichert, {len(self.skipped)} übersprungen, {len(self.failed)} fehlgeschlagen"


# -- Erkennung -------------------------------------------------------------------------------------------------
def discover_sockets(globs: Iterable[str] = SOCKET_GLOBS) -> list[str]:
    """Alle vorhandenen Docker-kompatiblen Sockets (ohne Duplikate wie /var/run -> /run)."""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in globs:
        for path in sorted(glob.glob(pattern)):
            real = os.path.realpath(path)
            try:
                is_sock = stat.S_ISSOCK(os.stat(real).st_mode)
            except OSError:
                continue
            if is_sock and real not in seen:
                seen.add(real)
                found.append(path)
    return found


def _owner(sock: str) -> str:
    try:
        uid = os.stat(os.path.realpath(sock)).st_uid
    except OSError:
        return "unbekannt"
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _docker(sock: str, *args: str) -> list[str]:
    return [docker_binary(), "-H", f"unix://{sock}", *args]


def classify(container: dict) -> str | None:
    """Engine einer (mit `docker inspect` untersuchten) Container-Beschreibung, sonst None."""
    dests = [m.get("Destination", "") for m in (container.get("Mounts") or [])]
    if any(d == "/var/lib/mysql" for d in dests):
        return MARIADB
    if any(d == "/var/lib/postgresql" or d.startswith("/var/lib/postgresql/") for d in dests):
        return POSTGRES
    return None


def find_databases(sockets: Iterable[str] | None = None, on_line: Callable[[str], None] = lambda _l: None
                   ) -> list[Database]:
    found: list[Database] = []
    for sock in (list(sockets) if sockets is not None else discover_sockets()):
        try:
            ps = subprocess.run(_docker(sock, "ps", "-q"), capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise DumpError(f"Docker-Client nicht gefunden: {docker_binary()}") from None
        except subprocess.TimeoutExpired:
            on_line(f"  {sock}: Zeitüberschreitung, übersprungen")
            continue
        if ps.returncode != 0:
            why = (ps.stderr.strip().splitlines() or ["?"])[-1]
            on_line(f"  {sock}: nicht erreichbar ({why})")
            continue
        ids = ps.stdout.split()
        if not ids:
            continue
        ins = subprocess.run(_docker(sock, "inspect", *ids), capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL)
        if ins.returncode != 0:
            on_line(f"  {sock}: inspect fehlgeschlagen")
            continue
        try:
            containers = json.loads(ins.stdout or "[]")
        except ValueError:
            on_line(f"  {sock}: ungültige inspect-Ausgabe")
            continue
        owner = _owner(sock)
        for c in containers:
            engine = classify(c)
            if engine:
                found.append(Database(sock, owner, (c.get("Name") or c.get("Id", "?")[:12]).lstrip("/"), engine))
    return found


# -- Zugangsdaten ----------------------------------------------------------------------------------------------
def parse_credentials(raw: str | None) -> dict[str, dict]:
    """JSON-Geheimnis `dbdump`: {"owner/container": {"user": "...", "password": "..."}}."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("user") and v.get("password")} \
        if isinstance(data, dict) else {}


def dump_credentials(creds: dict[str, dict]) -> str:
    return json.dumps(creds, sort_keys=True)


# -- Ausführen -------------------------------------------------------------------------------------------------
def _stream_to_gzip(cmd: list[str], dest: Path, stdin_data: str | None = None, timeout: float = DUMP_TIMEOUT) -> int:
    """Führt cmd aus und schreibt stdout gzip-komprimiert nach dest (atomar). Gibt die Größe zurück."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".dump-", suffix=".tmp")
    tmp = Path(tmp_name)
    os.close(fd)
    os.chmod(tmp, 0o600)
    err = tempfile.TemporaryFile()
    try:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=err)
        except FileNotFoundError:
            raise DumpError(f"Docker-Client nicht gefunden: {docker_binary()}") from None
        assert proc.stdout
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            if stdin_data is not None:
                assert proc.stdin
                try:
                    proc.stdin.write(stdin_data.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            with gzip.open(tmp, "wb", compresslevel=6) as out:
                shutil.copyfileobj(proc.stdout, out, 1 << 20)
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
        if code != 0:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip().splitlines()
            raise DumpError((msg[-1] if msg else f"Exitcode {code}")[:300])
        size = tmp.stat().st_size
        os.replace(tmp, dest)
        return size
    finally:
        err.close()
        tmp.unlink(missing_ok=True)


def dump_database(db: Database, dest_dir: Path, cred: dict | None = None) -> int:
    dest = dest_dir / db.filename
    if db.engine == POSTGRES:
        return _stream_to_gzip(_docker(db.socket, "exec", db.container, "sh", "-c", _POSTGRES_DUMP), dest)
    if not cred:
        raise DumpError("kein Datenbank-Zugang hinterlegt")
    return _stream_to_gzip(_docker(db.socket, "exec", "-i", db.container, "sh", "-c", _MYSQL_DUMP, "sh",
                                   cred["user"]), dest, stdin_data=cred["password"] + "\n")


def remove_stale(dest_dir: Path, active: Iterable[Database], on_line: Callable[[str], None] = lambda _l: None,
                 days: float = STALE_DAYS, now: float | None = None) -> list[str]:
    """Entfernt Dump-Dateien von Containern, die nicht mehr erkannt werden und älter als `days` Tage sind.

    Konservativ: Dateien erkannter Container bleiben immer (auch wenn der Dump gerade fehlschlägt oder der Zugang
    fehlt), fremde Dateien werden nie angefasst. Ein gestoppter Container zählt als „nicht erkannt“: seine Dateien
    stecken im normalen Backup, und die Historie früherer Dumps liegt in den älteren Snapshots auf dem Server.
    """
    import time
    if not dest_dir.is_dir():
        return []
    keep = {db.filename for db in active}
    limit = (now if now is not None else time.time()) - days * 86400
    removed: list[str] = []
    for f in sorted(dest_dir.glob("*__*.sql.gz")):
        try:
            if f.name in keep or not f.is_file() or f.stat().st_mtime >= limit:
                continue
            f.unlink()
        except OSError:
            continue
        removed.append(f.name)
        on_line(f"  Entfernt (Container nicht mehr erkannt, älter als {days:g} Tage): {f.name}")
    return removed


def run_dumps(dest_dir: Path, creds: dict[str, dict], on_line: Callable[[str], None] = print,
              sockets: Iterable[str] | None = None) -> DumpResult:
    """Dumpt alle erkannten Datenbanken nach dest_dir. Ein Fehler stoppt weder die übrigen noch das Backup."""
    res = DumpResult()
    dbs = find_databases(sockets, on_line)
    res.removed = remove_stale(dest_dir, dbs, on_line)
    if not dbs:
        on_line("Datenbank-Dumps: keine laufenden Datenbank-Container gefunden")
        return res
    for db in dbs:
        label = f"{db.key} ({db.engine})"
        if db.engine == MARIADB and not creds.get(db.key):
            res.skipped.append((db, "kein Zugang hinterlegt"))
            on_line(f"  Übersprungen: {label}: kein Zugang hinterlegt (pbsgui dbdump …)")
            continue
        try:
            size = dump_database(db, dest_dir, creds.get(db.key))
        except DumpError as e:
            res.failed.append((db, str(e)))
            on_line(f"  Warnung: {label}: {e} (letzter Dump bleibt erhalten)")
            continue
        res.ok.append(db)
        on_line(f"  Gesichert: {label} -> {db.filename} ({size / 1048576:.1f} MiB)")
    on_line(f"Datenbank-Dumps: {res.summary()}")
    return res


# -- Zugang einrichten / prüfen ---------------------------------------------------------------------------------
def _mysql_session(db: Database, user: str, password: str, sql: str = "", script: str = _MYSQL_CLIENT,
                   timeout: float = 60) -> str:
    """Startet den mariadb-Client im Container; Passwort als erste stdin-Zeile, danach das SQL."""
    try:
        p = subprocess.run(_docker(db.socket, "exec", "-i", db.container, "sh", "-c", script, "sh", user),
                           input=password + "\n" + sql, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DumpError(f"Docker-Client nicht gefunden: {docker_binary()}") from None
    except subprocess.TimeoutExpired:
        raise DumpError("Zeitüberschreitung") from None
    if p.returncode != 0:
        raise DumpError(((p.stderr or p.stdout).strip().splitlines() or [f"Exitcode {p.returncode}"])[-1][:300])
    return p.stdout


def check_credentials(db: Database, user: str, password: str) -> None:
    """Prüft Anmeldung und Leserechte, ohne etwas zu verändern."""
    _mysql_session(db, user, password, "SELECT 1;\n")


def create_dump_user(db: Database, admin_user: str, admin_password: str, password: str | None = None) -> dict:
    """Legt im Container einen Dump-Benutzer mit reinen Leserechten an. Der Admin-Zugang wird nur hier benutzt."""
    password = password or _secrets.token_urlsafe(24)  # nur [A-Za-z0-9_-]: gefahrlos in SQL-Strings
    acct = f"'{DUMP_USER}'@'localhost'"
    sql = (f"CREATE USER IF NOT EXISTS {acct} IDENTIFIED BY '{password}';\n"
           f"ALTER USER {acct} IDENTIFIED BY '{password}';\n"
           f"GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON *.* TO {acct};\n"
           "FLUSH PRIVILEGES;\n")
    _mysql_session(db, admin_user, admin_password, sql)
    return {"user": DUMP_USER, "password": password}
