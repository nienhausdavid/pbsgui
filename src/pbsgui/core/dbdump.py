"""Datenbank-Dumps der laufenden Container vor dem Backup.

Warum: Der Client liest die Dateien einer laufenden Datenbank nacheinander über Minuten. Das Ergebnis ist nur
absturzkonsistent (wie nach einem Stromausfall) und lässt sich meist, aber nicht garantiert wiederherstellen.
Ein Dump ist ein sauberer, jederzeit prüfbarer Stand und wird als eigenes Archiv `dbdumps.pxar` mitgesichert.
Die Dumps liegen bewusst UNKOMPRIMIERT als `.sql` im Archiv: PBS komprimiert die Blöcke selbst und erkennt unveränderte
Teile wieder (Deduplizierung). Ein gzip-Strom ändert sich dagegen komplett und würde bei jedem Lauf voll hochgeladen.

Erkennung (nicht nach Namen): Pro Docker-kompatiblem Socket (System-Daemon, rootless-Daemons aller Benutzer,
Podman-Docker-Socket) werden die laufenden Container untersucht. Eine Datenbank liegt vor, wenn ein Datenverzeichnis
eingehängt ist: `/var/lib/postgresql…` (Postgres) bzw. `/var/lib/mysql` (MariaDB/MySQL). Gestoppte Container werden
nicht gedumpt; ihre Dateien sind ohnehin konsistent und stecken im normalen Backup.

Zugangsdaten:
- Postgres: keine. Der Dump läuft im Container über den lokalen Socket (`pg_dumpall`, offizielles Image: trust).
- MariaDB, bevorzugt ohne jedes Passwort: Ein Benutzer `mysql` mit der Anmeldeart `unix_socket` (nur Leserechte) gilt nur
  für den gleichnamigen Betriebssystem-Benutzer im Container, also für alle, die `docker exec -u mysql` ausführen
  dürfen: dieselbe Vertrauensgrenze wie bei Postgres. `setup_socket_access` legt ihn ohne Root-Passwort an: Container
  kurz stoppen, mit demselben Image und denselben Daten einmal mit `--init-file` starten (führt SQL ohne Anmeldung
  aus), wieder stoppen, Container neu starten.
- MariaDB/MySQL mit Passwort (Rückfall): ausdrücklich hinterlegt (Geheimnis `dbdump`, JSON), nie aus .env-Dateien oder
  der Container-Umgebung gelesen. Das Passwort geht über stdin in den Container, nie über die Kommandozeile.
- Ohne beides wird der Container übersprungen und gemeldet.
"""
from __future__ import annotations

import glob
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

# Anmeldung über den Unix-Socket: DB-Benutzer und Betriebssystem-Benutzer im Container heißen beide „mysql“.
SOCKET_USER = "mysql"
_SOCKET_PRELUDE = "unset MARIADB_HOST MYSQL_HOST MARIADB_PWD MYSQL_PWD MYSQL_TCP_PORT; "
_SOCKET_CHECK = (_SOCKET_PRELUDE + 'C=$(command -v mariadb || command -v mysql) || exit 127; exec "$C" --protocol=socket '
                 f'-u{SOCKET_USER} -N -e "select 1"')
_SOCKET_DUMP = (_SOCKET_PRELUDE + 'D=$(command -v mariadb-dump || command -v mysqldump) || { echo "kein mariadb-dump im '
                f'Container" >&2; exit 127; }}; exec "$D" --protocol=socket -u{SOCKET_USER} --single-transaction --quick '
                "--routines --events --triggers --all-databases")
_INIT_SQL = (f"CREATE USER IF NOT EXISTS '{SOCKET_USER}'@'localhost' IDENTIFIED VIA unix_socket;\n"
             f"GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON *.* TO '{SOCKET_USER}'@'localhost';\n")


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
        return f"{_safe(self.owner)}__{_safe(self.container)}.{self.engine}.sql"

    @property
    def legacy_filename(self) -> str:
        """Dateiname der früheren, gzip-komprimierten Dumps (bis 0.6.0)."""
        return self.filename + ".gz"


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
def _stream_to_file(cmd: list[str], dest: Path, stdin_data: str | None = None, timeout: float = DUMP_TIMEOUT) -> int:
    """Führt cmd aus und schreibt stdout unkomprimiert nach dest (atomar). Gibt die Größe zurück."""
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
        if proc.stdout is None:
            raise DumpError("Kein Ausgabestrom vom Docker-Client")
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            if stdin_data is not None:
                if proc.stdin is None:
                    raise DumpError("Kein Eingabestrom zum Docker-Client")
                try:
                    proc.stdin.write(stdin_data.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            with open(tmp, "wb") as out:
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


def dump_database(db: Database, dest_dir: Path, cred: dict | None = None, via_socket: bool = False) -> int:
    size = _dump_database(db, dest_dir, cred, via_socket)
    (dest_dir / db.legacy_filename).unlink(missing_ok=True)   # der neue Dump ersetzt den alten gzip-Dump
    return size


def _dump_database(db: Database, dest_dir: Path, cred: dict | None, via_socket: bool = False) -> int:
    dest = dest_dir / db.filename
    if db.engine == POSTGRES:
        return _stream_to_file(_docker(db.socket, "exec", db.container, "sh", "-c", _POSTGRES_DUMP), dest)
    if via_socket:   # ohne Passwort: als Betriebssystem-Benutzer „mysql“ im Container
        return _stream_to_file(_docker(db.socket, "exec", "-u", SOCKET_USER, db.container, "sh", "-c", _SOCKET_DUMP), dest)
    if not cred:
        raise DumpError("kein Datenbank-Zugang hinterlegt")
    return _stream_to_file(_docker(db.socket, "exec", "-i", db.container, "sh", "-c", _MYSQL_DUMP, "sh",
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
    keep = {name for db in active for name in (db.filename, db.legacy_filename)}
    limit = (now if now is not None else time.time()) - days * 86400
    removed: list[str] = []
    for f in sorted([*dest_dir.glob("*__*.sql"), *dest_dir.glob("*__*.sql.gz")]):
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
        via_socket = db.engine == MARIADB and socket_access_works(db)
        if db.engine == MARIADB and not via_socket and not creds.get(db.key):
            res.skipped.append((db, "kein Zugang"))
            on_line(f"  Übersprungen: {label}: kein Zugang (ohne Passwort einrichten: pbsgui dbdump NAME "
                    f"--setup-access {db.container})")
            continue
        try:
            size = dump_database(db, dest_dir, creds.get(db.key), via_socket)
        except DumpError as e:
            res.failed.append((db, str(e)))
            on_line(f"  Warnung: {label}: {e} (letzter Dump bleibt erhalten)")
            continue
        res.ok.append(db)
        how = " ohne Passwort" if via_socket else (" mit hinterlegtem Zugang" if db.engine == MARIADB else "")
        on_line(f"  Gesichert: {label}{how} -> {db.filename} ({size / 1048576:.1f} MiB)")
    on_line(f"Datenbank-Dumps: {res.summary()}")
    return res


# -- Zugang ohne Passwort (unix_socket) -----------------------------------------------------------------------------
def _docker_run(db: Database, *args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(_docker(db.socket, *args), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise DumpError(f"Docker-Client nicht gefunden: {docker_binary()}") from None
    except subprocess.TimeoutExpired:
        raise DumpError(f"Zeitüberschreitung bei docker {args[0]}") from None
    if check and p.returncode != 0:
        raise DumpError(((p.stderr or p.stdout).strip().splitlines() or [f"Exitcode {p.returncode}"])[-1][:300])
    return p


def socket_access_works(db: Database) -> bool:
    """Funktioniert die Anmeldung als Betriebssystem-Benutzer „mysql“ im Container ohne Passwort?"""
    try:
        p = _docker_run(db, "exec", "-u", SOCKET_USER, db.container, "sh", "-c", _SOCKET_CHECK, check=False, timeout=30)
    except DumpError:
        return False
    return p.returncode == 0 and p.stdout.strip() == "1"


def access_state(db: Database, creds: dict[str, dict]) -> str:
    """'none' (Postgres), 'socket' (ohne Passwort), 'password' (hinterlegt) oder 'missing'."""
    if db.engine != MARIADB:
        return "none"
    if socket_access_works(db):
        return "socket"
    return "password" if creds.get(db.key) else "missing"


def _wait(cond: Callable[[], bool], timeout: float, interval: float = 2.0) -> bool:
    import time
    end = time.monotonic() + timeout
    while True:
        if cond():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(interval)


def setup_socket_access(db: Database, on_line: Callable[[str], None] = lambda _l: None, timeout: float = 180,
                        poll: float = 2.0) -> bool:
    """Richtet den passwortlosen Lesezugang ein, ohne ein Passwort zu kennen. True = eingerichtet, False = war schon da.

    Der Container steht dabei kurz still (typisch 15 bis 40 Sekunden): Er wird sauber gestoppt, mit demselben Image und
    denselben Daten startet ein Wegwerf-Container mit `--init-file` (der Server führt die SQL-Datei ohne Anmeldung aus),
    danach läuft der Original-Container wieder. Der Original-Container wird in jedem Fall wieder gestartet.
    """
    if db.engine != MARIADB:
        raise DumpError("Nur für MariaDB-Container nötig (Postgres braucht keinen Zugang)")
    if socket_access_works(db):
        on_line(f"  {db.container}: passwortloser Zugang ist schon eingerichtet")
        return False
    info = json.loads(_docker_run(db, "inspect", db.container).stdout or "[]")
    if not info:
        raise DumpError(f"Container nicht gefunden: {db.container}")
    info = info[0]
    flavor = _docker_run(db, "exec", db.container, "sh", "-c", "command -v mariadbd", check=False, timeout=30)
    if flavor.returncode != 0 or not flavor.stdout.strip():
        raise DumpError("Nur für MariaDB-Images möglich (kein mariadbd im Container gefunden)")
    mount = next((m for m in info.get("Mounts") or [] if m.get("Destination") == "/var/lib/mysql"), None)
    if not mount:
        raise DumpError("Das Datenverzeichnis /var/lib/mysql ist nicht eingehängt")
    source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
    image = info.get("Image") or (info.get("Config") or {}).get("Image")
    if not source or not image:
        raise DumpError("Image oder Datenverzeichnis des Containers nicht ermittelbar")

    tmp = Path(tempfile.mkdtemp(prefix="pbsgui-init-"))
    name = f"pbsgui-init-{_safe(db.container)}-{os.getpid()}"
    problem: Exception | None = None
    try:
        os.chmod(tmp, 0o755)  # noqa: S103  # nosec B103
        sql = tmp / "init.sql"
        sql.write_text(_INIT_SQL)
        os.chmod(sql, 0o644)
        on_line(f"  Stoppe {db.container} (sauber, bis zu 2 Minuten) …")
        _docker_run(db, "stop", "-t", "120", db.container, timeout=200)
        try:
            on_line("  Einmaliger Start mit --init-file …")
            _docker_run(db, "run", "-d", "--rm", "--name", name, "--user", SOCKET_USER, "--entrypoint", "mariadbd",
                        "--mount", f"type={mount.get('Type', 'bind')},source={source},destination=/var/lib/mysql",
                        "-v", f"{sql}:/pbsgui-init.sql:ro", image, "--skip-networking",
                        "--init-file=/pbsgui-init.sql")
            probe = Database(db.socket, db.owner, name, MARIADB)
            try:
                if not _wait(lambda: socket_access_works(probe), timeout, poll):
                    logs = _docker_run(db, "logs", "--tail", "5", name, check=False, timeout=30)
                    raise DumpError("Der einmalige Start hat den Zugang nicht angelegt: "
                                    + ((logs.stderr or logs.stdout).strip().splitlines() or ["keine Ausgabe"])[-1][:200])
            finally:
                _docker_run(db, "stop", "-t", "120", name, check=False, timeout=200)   # --rm räumt den Container auf
        except Exception as e:
            problem = e
        finally:
            on_line(f"  Starte {db.container} wieder …")
            try:
                _docker_run(db, "start", db.container, timeout=120)
            except DumpError as e:
                problem = DumpError(f"{db.container} konnte nicht wieder gestartet werden, bitte von Hand starten "
                                    f"(docker start {db.container}): {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if problem:
        raise problem if isinstance(problem, DumpError) else DumpError(str(problem))
    if not _wait(lambda: socket_access_works(db), timeout, poll):
        raise DumpError(f"{db.container} läuft wieder, der passwortlose Zugang ist aber nicht verfügbar "
                        "(existiert der Benutzer 'mysql' schon mit anderer Anmeldeart?)")
    on_line(f"  {db.container}: passwortloser Lesezugang eingerichtet")
    return True


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
