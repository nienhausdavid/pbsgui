"""Anmeldung und Abwehr für die Web-Oberfläche der Rettungs-ISO.

Die Oberfläche kann auf einem Server im Internet laufen (VPS). Deshalb gilt:
- zufällige Passphrase (Wörter und Zahlen) bei jeder Aktivierung, nur auf der Konsole sichtbar;
- Rate Limiting pro Adresse, Sperren mit wachsender Dauer, Abschaltung bei zu vielen Fehlversuchen insgesamt;
- Honeypot: ein verstecktes Formularfeld und Köder-Adressen, die nur Bots und Scanner aufrufen; wer sie berührt,
  wird gesperrt und bekommt nur noch verzögerte, wertlose Antworten;
- Sitzungen mit Leerlauf- und Gesamtzeitlimit, nur eine Sitzung gleichzeitig, CSRF-Token.
Alles hier ist ohne Netzwerk testbar: die Uhr ist austauschbar.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

WORDS_FILE = Path(__file__).with_name("words.txt")
Clock = Callable[[], float]


# -- Passphrase ------------------------------------------------------------------------------------------
def load_words() -> list[str]:
    words = [w.strip() for w in WORDS_FILE.read_text().splitlines() if w.strip() and not w.startswith("#")]
    if len(words) < 7000:
        raise RuntimeError("Wortliste unvollständig")
    return words


_RNG = secrets.SystemRandom()      # aus dem Betriebssystem, nicht vorhersagbar


def generate_passphrase(words: int = 4, rng=_RNG, wordlist: list[str] | None = None) -> str:
    """Zufällige Passphrase: jedes Wort bekommt direkt dahinter eine zweistellige Zahl, z. B. `abacus47-grind83-lemon12-quest66`.

    Mit 7776 Wörtern sind das 12,9 Bit je Wort und 6,6 Bit je zweistelliger Zahl: 4 Wörter mit Zahlen ergeben etwa 78 Bit.
    """
    wl = wordlist or load_words()
    return "-".join(f"{rng.choice(wl)}{rng.randrange(10, 100)}" for _ in range(words))


def entropy_bits(words: int = 4, wordlist_size: int = 7776) -> float:
    import math
    return words * (math.log2(wordlist_size) + math.log2(90))


def normalize_passphrase(s: str) -> str:
    """Toleranz beim Abtippen: Groß-/Kleinschreibung, Leerzeichen statt Bindestrichen, Rand-Leerzeichen."""
    return "-".join(s.strip().lower().replace("_", "-").split())


def check_passphrase(given: str, expected: str) -> bool:
    return hmac.compare_digest(normalize_passphrase(given).encode(), normalize_passphrase(expected).encode())


# -- Adressen --------------------------------------------------------------------------------------------
def client_key(addr: str) -> str:
    """Schlüssel für Sperren: IPv4 genau, IPv6 als /64 (ein Angreifer wechselt sonst einfach die Adresse im Block)."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return addr
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


# -- Rate Limiting -----------------------------------------------------------------------------------------
class SlidingWindow:
    """Höchstens `limit` Ereignisse je `window` Sekunden und Schlüssel."""

    def __init__(self, limit: int, window: float, clock: Clock = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:      # Speicher begrenzen: leere Einträge verwerfen
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                    self._hits.pop(k, None)
            return True


@dataclass
class Event:
    time: float
    kind: str          # login-ok | login-fail | lock | honeypot | ban | rate | shutdown | info
    who: str
    detail: str = ""

    def line(self) -> str:
        return f"{time.strftime('%H:%M:%S', time.localtime(self.time))}  {self.kind:<10} {self.who:<22} {self.detail}".rstrip()


@dataclass
class _Lock:
    until: float = 0.0
    strikes: int = 0


class Guard:
    """Anmeldeversuche, Sperren, Honeypot-Treffer und Gesamt-Abschaltung."""

    # Bei dieser Zahl Fehlversuchen insgesamt schaltet sich die Oberfläche ab (neues Aktivieren auf der Konsole nötig).
    GLOBAL_FAIL_LIMIT = 40

    def __init__(self, passphrase: str, clock: Clock = time.monotonic, wall: Clock = time.time,
                 fails_before_lock: int = 5, lock_seconds: float = 900, honeypot_ban_seconds: float = 6 * 3600,
                 request_limit: int = 60, request_window: float = 10, on_shutdown: Callable[[str], None] | None = None):
        self._pass = passphrase
        self.clock, self.wall = clock, wall
        self.fails_before_lock, self.lock_seconds, self.honeypot_ban_seconds = fails_before_lock, lock_seconds, honeypot_ban_seconds
        self.requests = SlidingWindow(request_limit, request_window, clock)
        self._fails: dict[str, deque[float]] = {}
        self._locks: dict[str, _Lock] = {}
        self._bans: dict[str, float] = {}
        self.global_fails = 0
        self.events: deque[Event] = deque(maxlen=300)
        self.on_shutdown = on_shutdown
        self.shut_down = False
        self._mu = threading.Lock()

    # -- Protokoll
    def log(self, kind: str, who: str, detail: str = "") -> None:
        self.events.append(Event(self.wall(), kind, who, detail))

    # -- Sperrstatus
    def blocked_for(self, key: str) -> float:
        """Restsekunden, die diese Adresse noch gesperrt ist (0 = frei)."""
        now = self.clock()
        with self._mu:
            until = max(self._bans.get(key, 0.0), self._locks.get(key, _Lock()).until)
        return max(0.0, until - now)

    def block_kind(self, key: str) -> str:
        """'ban' (Köder berührt: still ablehnen), 'lock' (zu viele falsche Passwörter) oder '' (frei)."""
        now = self.clock()
        with self._mu:
            if self._bans.get(key, 0.0) > now:
                return "ban"
            return "lock" if self._locks.get(key, _Lock()).until > now else ""

    def allow_request(self, key: str) -> bool:
        if self.requests.allow(key):
            return True
        self.log("rate", key, "zu viele Anfragen")
        return False

    # -- Honeypot
    def honeypot(self, key: str, what: str) -> float:
        """Ein Köder wurde berührt: Adresse lange sperren (wächst bei Wiederholung). Gibt die Sperrdauer zurück."""
        with self._mu:
            lk = self._locks.setdefault(key, _Lock())
            lk.strikes += 1
            dur = self.honeypot_ban_seconds * min(8, 2 ** (lk.strikes - 1))
            self._bans[key] = self.clock() + dur
        self.log("honeypot", key, what)
        self.log("ban", key, f"{dur / 3600:.0f} h gesperrt")
        return dur

    # -- Anmeldung
    def check_login(self, key: str, given: str) -> tuple[bool, str]:
        """(ok, grund). Grund bei Misserfolg: 'locked', 'wrong' oder 'down'."""
        if self.shut_down:
            return False, "down"
        if self.blocked_for(key) > 0:
            return False, "locked"
        ok = check_passphrase(given, self._pass)
        now = self.clock()
        if ok:
            with self._mu:
                self._fails.pop(key, None)
            self.log("login-ok", key)
            return True, ""
        with self._mu:
            q = self._fails.setdefault(key, deque())
            while q and q[0] <= now - 600:
                q.popleft()
            q.append(now)
            self.global_fails += 1
            gf = self.global_fails
            lock_now = len(q) >= self.fails_before_lock
            if lock_now:
                lk = self._locks.setdefault(key, _Lock())
                lk.strikes += 1
                lk.until = now + self.lock_seconds * min(16, 2 ** (lk.strikes - 1))
                q.clear()
        self.log("login-fail", key, f"{gf} Fehlversuche insgesamt")
        if lock_now:
            self.log("lock", key, "gesperrt")
        if gf >= self.GLOBAL_FAIL_LIMIT and not self.shut_down:
            self.shut_down = True
            self.log("shutdown", "-", "zu viele Fehlversuche: Web-Oberfläche abgeschaltet")
            if self.on_shutdown:
                self.on_shutdown("zu viele Fehlversuche")
        return False, "wrong"


# -- Sitzungen ---------------------------------------------------------------------------------------------
@dataclass
class Session:
    token: str
    csrf: str
    key: str                  # Adresse, an die die Sitzung gebunden ist
    created: float
    last: float


class Sessions:
    """Genau eine Sitzung: eine neue Anmeldung beendet die alte. Gebunden an die Adresse, mit Leerlauf- und Gesamtzeit."""

    def __init__(self, idle: float = 900, total: float = 7200, clock: Clock = time.monotonic):
        self.idle, self.total, self.clock = idle, total, clock
        self._s: Session | None = None
        self._mu = threading.Lock()

    def create(self, key: str) -> Session:
        now = self.clock()
        with self._mu:
            self._s = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(24), key, now, now)
            return self._s

    def get(self, token: str, key: str) -> Session | None:
        now = self.clock()
        with self._mu:
            s = self._s
            if not s or not token or not hmac.compare_digest(s.token, token) or s.key != key:
                return None
            if now - s.last > self.idle or now - s.created > self.total:
                self._s = None
                return None
            s.last = now
            return s

    def end(self) -> None:
        with self._mu:
            self._s = None

    def active(self) -> bool:
        with self._mu:
            s = self._s
            now = self.clock()
            return bool(s and now - s.last <= self.idle and now - s.created <= self.total)

    def last_activity(self) -> float | None:
        with self._mu:
            return self._s.last if self._s else None


# -- Köder -------------------------------------------------------------------------------------------------
# Adressen, die ein Mensch mit der Konsolen-Adresse nie aufruft, Scanner und Bots aber ständig.
HONEYPOT_PATHS = (
    "/admin", "/administrator", "/login", "/login.php", "/wp-login.php", "/wp-admin", "/xmlrpc.php", "/phpmyadmin",
    "/pma", "/.env", "/.git/config", "/.aws/credentials", "/config.json", "/server-status", "/actuator", "/console",
    "/manager/html", "/cgi-bin/", "/index.php", "/owa/", "/remote/login", "/solr/", "/api/v1/auth", "/api/login.php",
    "/vendor/phpunit", "/boaform/admin/formLogin", "/ssh", "/shell", "/terminal",
)


def is_honeypot_path(path: str) -> bool:
    p = path.split("?", 1)[0].lower()
    return any(p == h.rstrip("/") or p.startswith(h if h.endswith("/") else h + "/") or p == h for h in HONEYPOT_PATHS)


HONEYPOT_FIELD = "website"      # im Formular unsichtbar; Menschen lassen es leer, Bots füllen es aus


@dataclass
class Trap:
    """Zählt für die Konsole mit, was die Köder gefangen haben."""
    hits: int = 0
    keys: set[str] = field(default_factory=set)
