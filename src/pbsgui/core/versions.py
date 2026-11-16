"""Über Snapshots hinweg: Dateiversionen, Suche, Vergleich zweier Snapshots, Vorschau und Restore-Test.

Alles baut auf den Dateibäumen (`catalog dump`) auf. Weil ein Snapshot sich nie ändert, werden die Bäume auf der
Platte zwischengespeichert (`CatalogCache`); danach kostet ein Durchlauf über viele Snapshots kaum noch Zeit.
Zwei Fassungen einer Datei gelten als gleich, wenn Größe und Änderungszeit im Katalog übereinstimmen.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import random
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..i18n import tr
from .client import PbsClient
from .files import (Catalog, CatalogEntry, FilesError, _within, archive_origin, copy_selection, iter_catalog,
                    line_hint, mounted, name_test)
from .models import Profile, Snapshot, slugify

Loader = Callable[[Snapshot], "dict[str, Catalog]"]
Progress = Callable[[int, int], None]


# -- Zwischenspeicher ---------------------------------------------------------------------------------
def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "pbsgui" / "catalogs"


class CatalogCache:
    """Speichert die Dateiliste jedes Snapshots (gzip) unter `<dir>/<profil>/<zeit>.txt.gz`; Rechte nur für den Benutzer.

    Bei verschlüsselten Backups stehen so Dateinamen im Klartext auf dem Rechner des Benutzers (wie die Dateien selbst).
    """

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else default_cache_dir()

    def _file(self, profile: Profile, snapshot: Snapshot) -> Path:
        return self.dir / profile.slug / f"{slugify(snapshot.backup_id)}-{int(snapshot.backup_time)}.txt.gz"

    def _text(self, client: PbsClient, profile: Profile, snapshot: Snapshot) -> str:
        f = self._file(profile, snapshot)
        try:
            return gzip.decompress(f.read_bytes()).decode("utf-8")
        except (OSError, EOFError, ValueError):
            pass
        text = client.catalog(snapshot)
        try:
            f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.dir, 0o700)
            os.chmod(f.parent, 0o700)
            tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(gzip.compress(text.encode("utf-8"), 3))
            tmp.replace(f)
        except OSError:
            pass                     # Zwischenspeicher ist nur eine Beschleunigung
        return text

    def loader(self, client: PbsClient, profile: Profile) -> Loader:
        """Lädt die Dateibäume eines Snapshots. `loader.entries(snapshot, hint)` liefert dieselben Einträge einzeln,
        ohne die Bäume aufzubauen: ein Server-Snapshot hat schnell über eine Million Einträge."""
        def entries(snapshot: Snapshot, hint: Callable[[str], bool] | None = None) -> Iterator[tuple[str, CatalogEntry]]:
            f = self._file(profile, snapshot)
            if f.is_file():
                done = 0
                try:
                    with gzip.open(f, "rt", encoding="utf-8", newline="\n") as lines:  # zeilenweise: entpackt mehrere 100 MiB
                        for name, entry in iter_catalog(lines, hint):
                            if name.endswith(".pxar"):
                                done += 1
                                yield name, entry
                    return
                except (OSError, EOFError, ValueError) as e:
                    f.unlink(missing_ok=True)        # beschädigt: beim nächsten Mal neu holen
                    if done:                         # ein Teil ist schon ausgeliefert: nicht doppelt liefern
                        raise FilesError(tr("Zwischengespeicherte Dateiliste beschädigt, bitte wiederholen: {0}", e)) from e
            for name, entry in iter_catalog(io.StringIO(self._text(client, profile, snapshot)), hint):
                if name.endswith(".pxar"):
                    yield name, entry

        def load(snapshot: Snapshot) -> dict[str, Catalog]:
            groups: dict[str, list[CatalogEntry]] = {}
            for name, entry in entries(snapshot):
                groups.setdefault(name, []).append(entry)
            return {name: Catalog(found) for name, found in groups.items()}

        load.entries = entries
        return load

    def prune(self, profile: Profile, snapshots: Iterable[Snapshot]) -> int:
        """Löscht Einträge zu Snapshots, die es nicht mehr gibt. Gibt die Zahl gelöschter Dateien zurück."""
        keep = {self._file(profile, s).name for s in snapshots}
        n = 0
        d = self.dir / profile.slug
        if d.is_dir():
            for f in d.glob("*.txt.gz"):
                if f.name not in keep:
                    f.unlink(missing_ok=True)
                    n += 1
        return n


def profile_snapshots(profile: Profile, snapshots: list[Snapshot]) -> list[Snapshot]:
    """Snapshots dieses Rechners (gleiche Backup-ID), neueste zuerst; gibt es keine, alle."""
    mine = [s for s in snapshots if s.backup_id == profile.backup_id] or list(snapshots)
    return sorted(mine, key=lambda s: s.backup_time, reverse=True)


def _key(entry: CatalogEntry) -> tuple:
    return (entry.kind, entry.size, entry.mtime)


def _scan(snapshots: list[Snapshot], loader: Loader, archive: str | None, on_progress: Progress | None, errors: list[str],
          want: Callable[[CatalogEntry], bool], hint: Callable[[str], bool] | None = None, per_archive: int | None = None
          ) -> Iterator[tuple[Snapshot, list[tuple[str, CatalogEntry]]]]:
    """Geht die Snapshots einzeln durch und liefert je Snapshot nur die passenden Einträge (Archiv, Eintrag).

    Es bleibt nie mehr als ein Snapshot im Speicher: alle Bäume gleichzeitig zu halten kostete bei einem Server mit
    17 Snapshots über 6 GiB."""
    stream = getattr(loader, "entries", None)
    for i, snap in enumerate(snapshots):
        if on_progress:
            on_progress(i, len(snapshots))
        found: list[tuple[str, CatalogEntry]] = []
        counts: dict[str, int] = {}
        try:
            source = stream(snap, hint) if stream else ((n, e) for n, c in loader(snap).items() for e in c.entries)
            for name, entry in source:
                if name == "recovery.pxar" or (archive is not None and name != archive) or not want(entry):
                    continue
                if per_archive is not None and counts.get(name, 0) >= per_archive:
                    continue
                counts[name] = counts.get(name, 0) + 1
                found.append((name, entry))
        except Exception as e:                      # ein defekter Snapshot darf die übrigen nicht verhindern
            errors.append(f"{snap.path}: {e}")
            continue
        yield snap, found
    if on_progress:
        on_progress(len(snapshots), len(snapshots))


# -- Dateiversionen -----------------------------------------------------------------------------------
@dataclass
class FileVersion:
    """Eine Fassung der Datei und alle Snapshots, in denen genau diese Fassung steckt (neueste zuerst)."""
    entry: CatalogEntry
    snapshots: list[Snapshot] = field(default_factory=list)

    @property
    def newest(self) -> Snapshot:
        return self.snapshots[0]

    @property
    def oldest(self) -> Snapshot:
        return self.snapshots[-1]


@dataclass
class VersionsResult:
    archive: str
    path: str
    versions: list[FileVersion]          # neueste Fassung zuerst
    checked: int
    errors: list[str] = field(default_factory=list)


def file_versions(snapshots: list[Snapshot], loader: Loader, path: str, archive: str | None = None,
                  on_progress: Progress | None = None) -> VersionsResult:
    """Alle Fassungen einer Datei aus allen Snapshots. Ohne `archive` wird das Archiv gewählt, in dem der Pfad am häufigsten steht."""
    path = "/" + path.strip("/")
    errors: list[str] = []
    found: dict[str, list[tuple[Snapshot, CatalogEntry]]] = {}
    checked = 0
    for snap, matches in _scan(snapshots, loader, archive, on_progress, errors, lambda e: e.path == path,
                               line_hint(path.rsplit("/", 1)[-1])):
        checked += 1
        for name, e in matches:
            found.setdefault(name, []).append((snap, e))
    archive = archive or (max(found, key=lambda k: len(found[k])) if found else "")
    versions: dict[tuple, FileVersion] = {}
    order: list[tuple] = []
    for snap, e in found.get(archive, []):          # snapshots: neueste zuerst
        k = _key(e)
        if k not in versions:
            versions[k] = FileVersion(e)
            order.append(k)
        versions[k].snapshots.append(snap)
    return VersionsResult(archive, path, [versions[k] for k in order], checked, errors)


# -- Suche über alle Snapshots -----------------------------------------------------------------------------
@dataclass
class SearchHit:
    archive: str
    path: str
    kind: str
    snapshots: list[Snapshot] = field(default_factory=list)     # neueste zuerst
    versions: int = 1                                           # verschiedene Fassungen
    latest: CatalogEntry | None = None
    in_newest: bool = False                                     # gibt es den Pfad noch im neuesten Snapshot?


@dataclass
class SearchResult:
    hits: list[SearchHit]
    checked: int
    truncated: bool = False
    errors: list[str] = field(default_factory=list)


def search_snapshots(snapshots: list[Snapshot], loader: Loader, pattern: str, archive: str | None = None,
                     limit: int = 500, on_progress: Progress | None = None) -> SearchResult:
    """Sucht einen Namen (Teilstring oder `*` `?`) in allen Snapshots; Treffer sind je Pfad zusammengefasst."""
    test = name_test(pattern)
    errors: list[str] = []
    hits: dict[tuple[str, str], SearchHit] = {}
    keys: dict[tuple[str, str], set] = {}
    truncated = False
    checked = 0
    newest = None
    for snap, matches in _scan(snapshots, loader, archive, on_progress, errors,
                               lambda e: e.path != "/" and test(e.name.lower()), line_hint(pattern, fold=True), limit * 4):
        checked += 1
        newest = newest or snap
        for name, e in matches:
            k = (name, e.path)
            h = hits.get(k)
            if h is None:
                if len(hits) >= limit:
                    truncated = True
                    continue
                h = hits[k] = SearchHit(name, e.path, e.kind, latest=e)
                keys[k] = set()
            h.snapshots.append(snap)
            keys[k].add(_key(e))
    for k, h in hits.items():
        h.versions = len(keys[k])
    ordered = sorted(hits.values(), key=lambda h: (-h.snapshots[0].backup_time, h.path.lower()))
    for h in ordered:
        h.in_newest = newest is not None and h.snapshots[0] is newest
    return SearchResult(ordered, checked, truncated, errors)


# -- Vergleich ------------------------------------------------------------------------------------------
@dataclass
class DiffItem:
    change: str                  # "added" | "removed" | "changed"
    path: str
    kind: str                    # Art des Eintrags (d, f, l)
    old: CatalogEntry | None = None
    new: CatalogEntry | None = None
    files: int = 1               # bei Ordnern: Zahl der Dateien darunter
    size: int = 0                # Bytes (bei "changed": neue Größe)

    @property
    def size_delta(self) -> int:
        if self.change == "changed" and self.old is not None and self.new is not None:
            return (self.new.size or 0) - (self.old.size or 0)
        return self.size if self.change == "added" else -self.size


@dataclass
class DiffResult:
    archive: str
    items: list[DiffItem]

    def of(self, change: str) -> list[DiffItem]:
        return [i for i in self.items if i.change == change]

    @property
    def counts(self) -> dict[str, int]:
        c = {"added": 0, "removed": 0, "changed": 0}
        for i in self.items:
            c[i.change] += i.files
        return c


def diff_catalogs(old: Catalog | None, new: Catalog | None, archive: str = "") -> DiffResult:
    """Was ist in `new` neu, geändert oder gelöscht gegenüber `old`? Ganze neue/gelöschte Ordner erscheinen als ein Eintrag."""
    old = old or Catalog([])
    new = new or Catalog([])
    op = {p: e for p, e in old.by_path.items() if p != "/"}
    np_ = {p: e for p, e in new.by_path.items() if p != "/"}
    added = {p for p in np_ if p not in op}
    removed = {p for p in op if p not in np_}
    items: list[DiffItem] = []

    def top(paths: set[str], cat: Catalog, change: str) -> None:
        for p in sorted(paths):
            parent = p.rsplit("/", 1)[0] or "/"
            if parent in paths:
                continue                                 # steckt in einem schon gemeldeten Ordner
            e = cat.by_path[p]
            files, size = cat.count([p]) if e.kind == "d" else (1, e.size or 0)
            items.append(DiffItem(change, p, e.kind, old=e if change == "removed" else None,
                                  new=e if change == "added" else None, files=files, size=size))

    top(added, new, "added")
    top(removed, old, "removed")
    for p in sorted(np_.keys() & op.keys()):
        a, b = op[p], np_[p]
        if a.kind != b.kind or (b.kind != "d" and _key(a) != _key(b)):
            items.append(DiffItem("changed", p, b.kind, old=a, new=b, size=b.size or 0))
    order = {"added": 0, "changed": 1, "removed": 2}
    items.sort(key=lambda i: (order[i.change], i.path.lower()))
    return DiffResult(archive, items)


def diff_snapshots(loader: Loader, old: Snapshot, new: Snapshot, archive: str | None = None) -> list[DiffResult]:
    a, b = loader(old), loader(new)
    names = sorted((set(a) | set(b)) - {"recovery.pxar"})
    if archive:
        names = [n for n in names if n == archive]
    return [diff_catalogs(a.get(n), b.get(n), n) for n in names]


# -- Vorschau --------------------------------------------------------------------------------------------
IMAGE_TYPES = {".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".gif": b"GIF8",
               ".bmp": b"BM", ".webp": b"RIFF"}
MAX_IMAGE = 20 * 1024 * 1024
TEXT_BYTES = 64 * 1024


@dataclass
class Preview:
    kind: str                    # text | image | binary | dir | link | missing | special
    text: str = ""               # Text, Hexdump (binary) oder Linkziel
    data: bytes = b""            # Bilddaten
    size: int = 0
    truncated: bool = False
    note: str = ""


class PreviewSession:
    """Hängt ein Archiv einmal ein und liest daraus Vorschauen; mit `with` oder `close()` wird es wieder ausgehängt."""

    def __init__(self, client: PbsClient, snapshot: Snapshot, archive: str):
        self.client, self.snapshot, self.archive = client, snapshot, archive
        self._cm = None
        self.root: Path | None = None
        self._lock = threading.Lock()          # Oberflächen fragen aus mehreren Threads gleichzeitig

    def __enter__(self) -> "PreviewSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _open(self) -> Path:
        if self.root is None:
            self._cm = mounted(self.client, self.snapshot, self.archive)
            self.root = self._cm.__enter__()
        return self.root

    def open(self) -> None:
        with self._lock:
            self._open()

    def close(self) -> None:
        with self._lock:
            if self._cm is not None:
                try:
                    self._cm.__exit__(None, None, None)
                finally:
                    self._cm, self.root = None, None

    def preview(self, rel: str, max_bytes: int = TEXT_BYTES) -> Preview:
        with self._lock:
            return preview_path(self._open(), rel, max_bytes)


def preview_path(root: Path, rel: str, max_bytes: int = TEXT_BYTES) -> Preview:
    path = root / rel.lstrip("/")
    if not _within(path.parent, root):                            # "..", Ordner-Links aus dem Archiv heraus
        return Preview("missing", note=tr("Pfad außerhalb des Archivs"))
    try:
        st = path.lstat()
    except OSError:
        return Preview("missing", note=tr("Nicht im Archiv"))
    if stat.S_ISLNK(st.st_mode):                                  # nie dem Link folgen: er kann aus dem Archiv herauszeigen
        return Preview("link", text=os.readlink(path), note=tr("Symbolischer Link"))
    if stat.S_ISDIR(st.st_mode):
        return Preview("dir", note=tr("Ordner"))
    if not stat.S_ISREG(st.st_mode):
        return Preview("special", note=tr("Sonderdatei"))
    ext = path.suffix.lower()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        return Preview("missing", note=tr("Nicht lesbar: {0}", e.strerror or e))
    with os.fdopen(fd, "rb") as fh:
        if ext in IMAGE_TYPES:
            if st.st_size > MAX_IMAGE:
                return Preview("binary", size=st.st_size, note=tr("Bild zu groß für die Vorschau ({0} MiB)", st.st_size // 1024 // 1024))
            data = fh.read(MAX_IMAGE + 1)
            if data.startswith(IMAGE_TYPES[ext]):
                return Preview("image", data=data, size=st.st_size)
            fh.seek(0)
        head = fh.read(max_bytes + 4)
    truncated = st.st_size > max_bytes
    sample = head[:max_bytes]
    if b"\0" not in sample:
        for cut in range(4):                                       # abgeschnittenes UTF-8-Zeichen am Ende zulassen
            try:
                text = (sample[:len(sample) - cut] if cut else sample).decode("utf-8")
                return Preview("text", text=text, size=st.st_size, truncated=truncated)
            except UnicodeDecodeError:
                continue
    return Preview("binary", text=hexdump(sample[:256]), size=st.st_size, truncated=st.st_size > 256,
                   note=tr("Binärdatei – Hexdump der ersten 256 Bytes"))


def hexdump(data: bytes) -> str:
    rows = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexa = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{i:08x}  {hexa:<47}  {asc}")
    return "\n".join(rows)


# -- Restore-Test ------------------------------------------------------------------------------------------
MAX_SAMPLE_SIZE = 64 * 1024 * 1024


@dataclass
class SampleResult:
    path: str
    size: int
    status: str            # ok | gelesen | abweichung | groesse | fehler | kein_zugriff
    detail: str = ""
    sha256: str = ""

    @property
    def good(self) -> bool:
        return self.status in ("ok", "gelesen", "kein_zugriff")

    @property
    def checked(self) -> bool:
        """Wurde die Datei wirklich gelesen? „kein_zugriff“ heißt: dieser Benutzer darf sie im Archiv nicht lesen."""
        return self.status != "kein_zugriff"


@dataclass
class RestoreTestResult:
    snapshot: str
    archive: str
    samples: list[SampleResult]
    seconds: float
    time: int = field(default_factory=lambda: int(time.time()))

    @property
    def ok(self) -> bool:
        return any(s.checked for s in self.samples) and all(s.good for s in self.samples)

    @property
    def compared(self) -> int:
        return sum(1 for s in self.samples if s.status == "ok")

    def summary(self) -> str:
        if not self.samples:
            return tr("Keine geeigneten Dateien für den Test gefunden")
        bad = [s for s in self.samples if not s.good]
        checked = [s for s in self.samples if s.checked]
        denied = len(self.samples) - len(checked)
        if not checked:
            return tr("Keine der {0} Dateien war lesbar: sie gehören einem anderen Benutzer. Den Test mit Administrator-Rechten ausführen.", denied)
        text = tr("{0} von {1} Dateien in Ordnung ({2} mit dem Original verglichen)", len(checked) - len(bad), len(checked), self.compared)
        text += tr(", {0} FEHLER", len(bad)) if bad else ""
        return text + (tr("; {0} ohne Zugriff übersprungen (nur mit Administrator-Rechten lesbar)", denied) if denied else "")

    def to_dict(self) -> dict:
        return {"snapshot": self.snapshot, "archive": self.archive, "time": self.time, "seconds": round(self.seconds, 1),
                "ok": self.ok, "summary": self.summary(),
                "samples": [{"path": s.path, "size": s.size, "status": s.status, "detail": s.detail} for s in self.samples]}


def _ram_dir() -> str | None:
    """Entschlüsselte Stichproben möglichst im Arbeitsspeicher ablegen (XDG_RUNTIME_DIR oder /dev/shm), nicht auf der Platte."""
    for d in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):  # noqa: S108  # nosec B108 (RAM-Verzeichnis, kein fester Dateiname)
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def catalog_times(ts: float) -> tuple[str, str]:
    """So kann eine Änderungszeit im Katalog stehen. Der Client schreibt die ORTSZEIT mit einem „Z“ dahinter (gemessen mit
    proxmox-backup-client 3.x/4.x); die echte UTC-Zeit wird ebenfalls angenommen, falls ein Client das einmal richtig macht."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(int(ts))), _iso_utc(ts)


def pick_samples(cat: Catalog, count: int, rng: random.Random | None = None, max_size: int = MAX_SAMPLE_SIZE
                 ) -> list[CatalogEntry]:
    rng = rng or random.Random()  # noqa: S311  # nosec B311 (Stichprobe, keine Kryptographie)
    files = [e for e in cat.entries if e.kind == "f" and e.size is not None and e.size <= max_size]
    nonempty = [e for e in files if e.size]
    pool = nonempty or files
    return rng.sample(pool, min(count, len(pool)))


def restore_test(client: PbsClient, profile: Profile, snapshot: Snapshot, archive: str, cat: Catalog, count: int = 5,
                 rng: random.Random | None = None, on_line: Callable[[str], None] = lambda _l: None,
                 max_size: int = MAX_SAMPLE_SIZE) -> RestoreTestResult:
    """Holt zufällige Dateien aus dem Snapshot in einen Wegwerf-Ordner und prüft sie.

    Geprüft wird: Lesen gelingt (der Client prüft dabei jede Datenhäppchen-Prüfsumme und entschlüsselt), die Größe passt
    zum Katalog und, falls das Original unverändert (Größe und Zeit) am Ursprungsort liegt, der SHA-256 ist gleich.
    """
    t0 = time.time()
    samples = pick_samples(cat, count, rng, max_size)
    origin = archive_origin(profile, archive)
    results: list[SampleResult] = []
    if not samples:
        return RestoreTestResult(snapshot.path, archive, results, time.time() - t0)
    queue, spare, checked = list(samples), None, 0
    with tempfile.TemporaryDirectory(prefix="pbsgui-test-", dir=_ram_dir()) as tmp, mounted(client, snapshot, archive) as mp:
        while queue and checked < len(samples):
            e = queue.pop(0)
            on_line(tr("Prüfe {0}", e.path))
            dest = Path(tmp) / "r"
            try:
                res = copy_selection(mp, [e.path], dest)
            except (OSError, FilesError) as ex:
                results.append(SampleResult(e.path, e.size or 0, "fehler", str(ex)))
                checked += 1
                continue
            if res.denied:
                # Vollbackup, Test ohne Administrator-Rechte: die Datei ist da, aber für diesen Benutzer gesperrt. Das ist kein
                # Fehler des Backups; stattdessen eine andere Stichprobe ziehen (höchstens dreimal so viele Ersatzdateien).
                if len([r for r in results if not r.checked]) < len(samples):
                    results.append(SampleResult(e.path, e.size or 0, "kein_zugriff", tr("nur mit Administrator-Rechten lesbar")))
                if spare is None:
                    spare = [x for x in pick_samples(cat, len(samples) * 4, rng, max_size) if x not in samples]
                if spare:
                    queue.append(spare.pop(0))
                continue
            checked += 1
            got = dest / e.path.lstrip("/")
            if res.skipped or not got.is_file():
                results.append(SampleResult(e.path, e.size or 0, "fehler", "; ".join(res.skipped) or tr("nicht wiederhergestellt")))
                continue
            digest = sha256_file(got)
            actual = got.stat().st_size
            if actual != e.size:
                results.append(SampleResult(e.path, e.size or 0, "groesse", tr("Katalog {0} Bytes, gelesen {1}", e.size, actual), digest))
                continue
            orig = Path(os.path.normpath(Path(origin) / e.path.lstrip("/"))) if origin else None
            status, detail = "gelesen", tr("Original geändert oder nicht lesbar: nur Lesbarkeit und Größe geprüft")
            try:
                if orig is not None and orig.is_file() and not orig.is_symlink():
                    ost = orig.stat()
                    if ost.st_size == e.size and e.mtime in catalog_times(ost.st_mtime):
                        if sha256_file(orig) == digest:
                            status, detail = "ok", tr("identisch mit dem Original")
                        else:
                            status, detail = "abweichung", tr("Inhalt weicht vom unveränderten Original ab")
            except OSError:
                pass
            results.append(SampleResult(e.path, e.size or 0, status, detail, digest))
            shutil.rmtree(dest, ignore_errors=True)
    return RestoreTestResult(snapshot.path, archive, results, time.time() - t0)

