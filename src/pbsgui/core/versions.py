"""Über Snapshots hinweg: Dateiversionen, Suche, Vergleich zweier Snapshots, Vorschau und Restore-Test.

Alles baut auf den Dateibäumen (`catalog dump`) auf. Weil ein Snapshot sich nie ändert, werden die Bäume auf der
Platte zwischengespeichert (`CatalogCache`); danach kostet ein Durchlauf über viele Snapshots kaum noch Zeit.
Zwei Fassungen einer Datei gelten als gleich, wenn Größe und Änderungszeit im Katalog übereinstimmen.
"""
from __future__ import annotations

import gzip
import hashlib
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
from typing import Callable, Iterable

from .client import PbsClient
from .files import (Catalog, CatalogEntry, FilesError, _within, archive_origin, copy_selection, mounted,
                    parse_catalog)
from .models import Profile, Snapshot

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
        return self.dir / profile.slug / f"{snapshot.backup_id}-{snapshot.backup_time}.txt.gz"

    def loader(self, client: PbsClient, profile: Profile) -> Loader:
        def load(snapshot: Snapshot) -> dict[str, Catalog]:
            f = self._file(profile, snapshot)
            text = None
            try:
                text = gzip.decompress(f.read_bytes()).decode("utf-8")
            except (OSError, EOFError, ValueError):
                pass
            if text is None:
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
            return {k: Catalog(v) for k, v in parse_catalog(text).items() if k.endswith(".pxar")}
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


def _gather(snapshots: list[Snapshot], loader: Loader, archive: str | None, on_progress: Progress | None
            ) -> tuple[list[tuple[Snapshot, dict[str, Catalog]]], list[str]]:
    out, errors = [], []
    for i, snap in enumerate(snapshots):
        if on_progress:
            on_progress(i, len(snapshots))
        try:
            cats = loader(snap)
        except Exception as e:                      # ein defekter Snapshot darf die übrigen nicht verhindern
            errors.append(f"{snap.path}: {e}")
            continue
        cats = {k: v for k, v in cats.items() if k != "recovery.pxar" and (archive is None or k == archive)}
        out.append((snap, cats))
    if on_progress:
        on_progress(len(snapshots), len(snapshots))
    return out, errors


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


def find_archive(cats_by_snapshot: list[tuple[Snapshot, dict[str, Catalog]]], path: str) -> str | None:
    found: dict[str, int] = {}
    for _snap, cats in cats_by_snapshot:
        for name, cat in cats.items():
            if path in cat.by_path:
                found[name] = found.get(name, 0) + 1
    if not found:
        return None
    return max(found, key=lambda k: found[k])


def file_versions(snapshots: list[Snapshot], loader: Loader, path: str, archive: str | None = None,
                  on_progress: Progress | None = None) -> VersionsResult:
    """Alle Fassungen einer Datei aus allen Snapshots. Ohne `archive` wird das Archiv gewählt, in dem der Pfad am häufigsten steht."""
    path = "/" + path.strip("/")
    loaded, errors = _gather(snapshots, loader, archive, on_progress)
    archive = archive or find_archive(loaded, path) or ""
    versions: dict[tuple, FileVersion] = {}
    order: list[tuple] = []
    for snap, cats in loaded:                       # snapshots: neueste zuerst
        e = cats.get(archive).by_path.get(path) if archive in cats else None
        if e is None:
            continue
        k = _key(e)
        if k not in versions:
            versions[k] = FileVersion(e)
            order.append(k)
        versions[k].snapshots.append(snap)
    return VersionsResult(archive, path, [versions[k] for k in order], len(loaded), errors)


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
    loaded, errors = _gather(snapshots, loader, archive, on_progress)
    hits: dict[tuple[str, str], SearchHit] = {}
    keys: dict[tuple[str, str], set] = {}
    truncated = False
    for snap, cats in loaded:
        for name, cat in cats.items():
            for e in cat.search(pattern, limit=limit * 4):
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
    newest = loaded[0][0] if loaded else None
    ordered = sorted(hits.values(), key=lambda h: (-h.snapshots[0].backup_time, h.path.lower()))
    for h in ordered:
        h.in_newest = newest is not None and h.snapshots[0] is newest
    return SearchResult(ordered, len(loaded), truncated, errors)


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
        return Preview("missing", note="Pfad außerhalb des Archivs")
    try:
        st = path.lstat()
    except OSError:
        return Preview("missing", note="Nicht im Archiv")
    if stat.S_ISLNK(st.st_mode):                                  # nie dem Link folgen: er kann aus dem Archiv herauszeigen
        return Preview("link", text=os.readlink(path), note="Symbolischer Link")
    if stat.S_ISDIR(st.st_mode):
        return Preview("dir", note="Ordner")
    if not stat.S_ISREG(st.st_mode):
        return Preview("special", note="Sonderdatei")
    ext = path.suffix.lower()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        return Preview("missing", note=f"Nicht lesbar: {e.strerror or e}")
    with os.fdopen(fd, "rb") as fh:
        if ext in IMAGE_TYPES:
            if st.st_size > MAX_IMAGE:
                return Preview("binary", size=st.st_size, note=f"Bild zu groß für die Vorschau ({st.st_size // 1024 // 1024} MiB)")
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
                   note="Binärdatei – Hexdump der ersten 256 Bytes")


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
    status: str            # ok | gelesen | abweichung | groesse | fehler
    detail: str = ""
    sha256: str = ""

    @property
    def good(self) -> bool:
        return self.status in ("ok", "gelesen")


@dataclass
class RestoreTestResult:
    snapshot: str
    archive: str
    samples: list[SampleResult]
    seconds: float
    time: int = field(default_factory=lambda: int(time.time()))

    @property
    def ok(self) -> bool:
        return bool(self.samples) and all(s.good for s in self.samples)

    @property
    def compared(self) -> int:
        return sum(1 for s in self.samples if s.status == "ok")

    def summary(self) -> str:
        if not self.samples:
            return "Keine geeigneten Dateien für den Test gefunden"
        bad = [s for s in self.samples if not s.good]
        text = f"{len(self.samples) - len(bad)} von {len(self.samples)} Dateien in Ordnung ({self.compared} mit dem Original verglichen)"
        return text + (f", {len(bad)} FEHLER" if bad else "")

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
    with tempfile.TemporaryDirectory(prefix="pbsgui-test-", dir=_ram_dir()) as tmp, mounted(client, snapshot, archive) as mp:
        for e in samples:
            on_line(f"Prüfe {e.path}")
            dest = Path(tmp) / "r"
            try:
                res = copy_selection(mp, [e.path], dest)
            except (OSError, FilesError) as ex:
                results.append(SampleResult(e.path, e.size or 0, "fehler", str(ex)))
                continue
            got = dest / e.path.lstrip("/")
            if res.skipped or not got.is_file():
                results.append(SampleResult(e.path, e.size or 0, "fehler", "; ".join(res.skipped) or "nicht wiederhergestellt"))
                continue
            digest = sha256_file(got)
            actual = got.stat().st_size
            if actual != e.size:
                results.append(SampleResult(e.path, e.size or 0, "groesse", f"Katalog {e.size} Bytes, gelesen {actual}", digest))
                continue
            orig = Path(os.path.normpath(Path(origin) / e.path.lstrip("/"))) if origin else None
            status, detail = "gelesen", "Original geändert oder nicht lesbar: nur Lesbarkeit und Größe geprüft"
            try:
                if orig is not None and orig.is_file() and not orig.is_symlink():
                    ost = orig.stat()
                    if ost.st_size == e.size and _iso_utc(ost.st_mtime) == e.mtime:
                        if sha256_file(orig) == digest:
                            status, detail = "ok", "identisch mit dem Original"
                        else:
                            status, detail = "abweichung", "Inhalt weicht vom unveränderten Original ab"
            except OSError:
                pass
            results.append(SampleResult(e.path, e.size or 0, status, detail, digest))
            shutil.rmtree(dest, ignore_errors=True)
    return RestoreTestResult(snapshot.path, archive, results, time.time() - t0)

