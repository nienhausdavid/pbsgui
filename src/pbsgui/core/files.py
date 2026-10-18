"""Dateien eines Snapshots durchsuchen und gezielt wiederherstellen (File-Level-Restore).

Der Client kann kein Teil-Restore: Der Dateibaum kommt aus `catalog dump` (der Client entschlüsselt den Katalog
selbst, daher geht das auch bei verschlüsselten Backups), die Daten werden per `mount` (FUSE, schreibgeschützt)
gelesen und die gewählten Pfade kopiert. Optional kann an den Originalort zurückgeschrieben werden, aber nur
innerhalb des Home-Verzeichnisses und mit Sicherheitskopie der überschriebenen Dateien.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .client import ClientError, PbsClient
from .models import Profile, Snapshot


class FilesError(RuntimeError):
    pass


# -- Katalog ----------------------------------------------------------------------------------
@dataclass
class CatalogEntry:
    kind: str            # "d" Ordner, "f" Datei, "l" Symlink, andere (Sockets, Geräte …)
    path: str            # innerhalb des Archivs, mit führendem "/" (Wurzel = "/")
    size: int | None = None
    mtime: str = ""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1] or "/"

    @property
    def parent(self) -> str:
        p = self.path.rsplit("/", 1)[0]
        return p or "/"


_ESC = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}


def unescape(s: str) -> str:
    """Rust-Debug-Quoting des Clients: \\\\ \\" \\' \\n \\t \\r \\0 und \\u{…}; Unicode steht unmaskiert da."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "u" and s[i + 2:i + 3] == "{":
                end = s.index("}", i)
                out.append(chr(int(s[i + 3:end], 16)))
                i = end + 1
                continue
            out.append(_ESC.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_LINE = re.compile(r'^(?P<kind>\S) "(?P<path>(?:[^"\\]|\\.)*)"(?:\s+(?P<size>\d+)\s+(?P<mtime>\S+))?\s*$')


def parse_catalog(text: str) -> dict[str, list[CatalogEntry]]:
    """`catalog dump`-Ausgabe -> {Archivname: Einträge}. Oberste Ebene des Katalogs sind die Archive (`x.pxar.didx`)."""
    archives: dict[str, list[CatalogEntry]] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        path = unescape(m["path"])
        if not path.startswith("./"):
            continue
        parts = path[2:].split("/", 1)
        archive = parts[0].removesuffix(".didx").removesuffix(".fidx")
        inner = "/" + (parts[1] if len(parts) > 1 else "")
        archives.setdefault(archive, []).append(
            CatalogEntry(m["kind"], inner, int(m["size"]) if m["size"] else None, m["mtime"] or ""))
    return archives


class Catalog:
    """Dateibaum eines Archivs mit schnellem Zugriff auf den Inhalt eines Ordners."""

    def __init__(self, entries: list[CatalogEntry]):
        self.entries = entries
        self.by_path = {e.path: e for e in entries}
        self._children: dict[str, list[CatalogEntry]] = {}
        for e in entries:
            if e.path != "/":
                self._children.setdefault(e.parent, []).append(e)
        for kids in self._children.values():
            kids.sort(key=lambda e: (e.kind != "d", e.name.lower()))

    def children(self, path: str = "/") -> list[CatalogEntry]:
        return self._children.get(path or "/", [])

    def walk(self, path: str) -> Iterator[CatalogEntry]:
        """Eintrag und alles darunter."""
        root = self.by_path.get(path)
        if root:
            yield root
        stack = [path]
        while stack:
            for c in self._children.get(stack.pop(), []):
                yield c
                if c.kind == "d":
                    stack.append(c.path)

    def search(self, pattern: str, limit: int = 500) -> list[CatalogEntry]:
        """Groß-/Kleinschreibung egal; `*` und `?` als Platzhalter, sonst Teilstring des Namens."""
        pat = pattern.lower()
        if any(ch in pat for ch in "*?"):
            rx = re.compile(re.escape(pat).replace(r"\*", ".*").replace(r"\?", "."))
            test = lambda name: rx.fullmatch(name) is not None
        else:
            test = lambda name: pat in name
        out = []
        for e in self.entries:
            if e.path != "/" and test(e.name.lower()):
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def count(self, paths: list[str]) -> tuple[int, int]:
        """(Dateien, Bytes) der Auswahl inklusive Unterordnern."""
        files = size = 0
        seen: set[str] = set()
        for p in paths:
            for e in self.walk(p):
                if e.path not in seen and e.kind != "d":
                    seen.add(e.path)
                    files += 1
                    size += e.size or 0
        return files, size


def load_catalog(client: PbsClient, snapshot: Snapshot) -> dict[str, Catalog]:
    return {name: Catalog(entries) for name, entries in parse_catalog(client.catalog(snapshot)).items()}


# -- Einhängen -----------------------------------------------------------------------------------
def _fusermount() -> list[str]:
    return [os.environ.get("PBSGUI_FUSERMOUNT") or shutil.which("fusermount3") or "fusermount", "-u"]


@contextmanager
def mounted(client: PbsClient, snapshot: Snapshot, archive: str, timeout: float = 30) -> Iterator[Path]:
    """Hängt ein Archiv schreibgeschützt per FUSE ein und hängt es am Ende sicher wieder aus."""
    if not archive.endswith(".pxar"):
        raise FilesError(f"{archive} ist kein Datei-Archiv (.pxar)")
    mp = Path(tempfile.mkdtemp(prefix="pbsgui-mnt-"))
    try:
        try:
            client.mount(snapshot, archive, str(mp))
        except ClientError as e:
            raise FilesError(f"Einhängen fehlgeschlagen: {e} (ist FUSE installiert? `fusermount3` nötig)") from None
        end = time.time() + timeout
        while not (os.path.ismount(mp) or any(mp.iterdir()) or mp.is_symlink()) and time.time() < end:
            time.sleep(0.2)
        yield mp
    finally:
        for flag in ([], ["-z"]):                 # normal, dann lazy
            r = subprocess.run([*_fusermount(), *flag, str(mp)], capture_output=True)
            if r.returncode == 0:
                break
        try:
            mp.unlink() if mp.is_symlink() else mp.rmdir()
        except OSError:
            pass


# -- Kopieren -------------------------------------------------------------------------------------
@dataclass
class CopyResult:
    copied: int = 0
    skipped: list[str] = field(default_factory=list)
    safety_copies: int = 0
    safety_dir: Path | None = None
    bytes: int = 0


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _copy_file(src: Path, dest: Path) -> None:
    """Atomar: erst neben das Ziel schreiben, dann ersetzen (auch bei schreibgeschützten Dateien)."""
    tmp = dest.with_name(dest.name + f".pbsgui-{os.getpid()}.tmp")
    try:
        shutil.copy2(src, tmp, follow_symlinks=False)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def copy_selection(src_root: Path, rels: list[str], dest_root: Path, *, overwrite: bool = False,
                   confine: Path | None = None, safety_dir: Path | None = None,
                   on_line: Callable[[str], None] = lambda _l: None) -> CopyResult:
    """Kopiert die gewählten Pfade (mit Pfadstruktur) von src_root nach dest_root.

    overwrite=False: vorhandene Dateien bleiben unangetastet (übersprungen).
    overwrite=True : vorhandene Dateien werden ersetzt; vorher wandert die alte Fassung nach safety_dir.
    confine: Zielpfade müssen (nach Auflösen von Symlinks) darin liegen, sonst wird abgebrochen.
    """
    res = CopyResult(safety_dir=safety_dir)
    src_root = Path(src_root)

    def dest_for(rel: str) -> Path:
        return dest_root / rel.lstrip("/")

    def guard(dest: Path) -> None:
        # Das Ziel selbst darf der erlaubte Ordner sein; sonst muss der (aufgelöste) Elternordner darin liegen,
        # damit ein Symlink im Ziel nie aus dem Bereich herausführen kann.
        if confine is None:
            return
        if dest.resolve(strict=False) == confine.resolve() or _within(dest.parent, confine):
            return
        raise FilesError(f"Zielpfad liegt außerhalb des erlaubten Bereichs: {dest}")

    def one(src: Path, rel: str) -> None:
        dest = dest_for(rel)
        guard(dest)
        st = src.lstat()
        if stat.S_ISDIR(st.st_mode):
            dest.mkdir(parents=True, exist_ok=True)
            guard(dest / "x")
            for child in sorted(src.iterdir()):
                one(child, f"{rel.rstrip('/')}/{child.name}")
            try:
                shutil.copystat(src, dest, follow_symlinks=False)
            except OSError:
                pass
            return
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            res.skipped.append(f"{rel} (Sonderdatei)")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        guard(dest)
        if os.path.lexists(dest):
            if not overwrite:
                res.skipped.append(f"{rel} (existiert schon)")
                return
            if os.path.isdir(dest) and not os.path.islink(dest):
                res.skipped.append(f"{rel} (am Ziel ist ein Ordner)")
                return
            if safety_dir is not None:                    # alte Fassung sichern, bevor sie ersetzt wird
                keep = safety_dir / rel.lstrip("/")
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, keep, follow_symlinks=False)
                res.safety_copies += 1
        if stat.S_ISLNK(st.st_mode):
            if os.path.lexists(dest):
                dest.unlink()
            os.symlink(os.readlink(src), dest)
        else:
            _copy_file(src, dest)
            res.bytes += st.st_size
        res.copied += 1

    for rel in rels:
        src = src_root / rel.lstrip("/")
        if not os.path.lexists(src):
            res.skipped.append(f"{rel} (nicht im Archiv)")
            continue
        on_line(f"Kopiere {rel}")
        one(src, rel)
    return res


# -- Originalort (nur Home) -------------------------------------------------------------------------
@dataclass
class OriginalPath:
    rel: str
    dest: Path
    in_home: bool
    exists: bool


def archive_origin(profile: Profile, archive: str) -> str | None:
    """Ursprünglicher Quellordner eines Archivs (z. B. home-david.pxar -> /home/david)."""
    for spec in profile.archive_specs():
        name, _, source = spec.partition(":")
        if name == archive:
            return source
    return None


def map_to_original(profile: Profile, archive: str, rels: list[str], home: Path | None = None) -> list[OriginalPath]:
    """Wohin würden die Pfade am Originalort zurückgeschrieben, und liegen sie im Home-Verzeichnis?"""
    home = (home or Path.home()).resolve()
    origin = archive_origin(profile, archive)
    if origin is None:
        raise FilesError(f"Der Originalort von {archive} ist unbekannt: Zurückschreiben nicht möglich")
    out = []
    for rel in rels:
        dest = Path(os.path.normpath(Path(origin) / rel.lstrip("/")))
        out.append(OriginalPath(rel, dest, _within(dest, home), os.path.lexists(dest)))
    return out


def require_home_only(items: list[OriginalPath]) -> None:
    outside = [i.dest for i in items if not i.in_home]
    if outside:
        raise FilesError("Überschreiben am Originalort ist nur im Home-Verzeichnis erlaubt. Außerhalb: "
                         + ", ".join(str(p) for p in outside[:5]) + (" …" if len(outside) > 5 else ""))


def safety_dir_for(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".local/share/pbsgui/overwritten" / time.strftime("%Y%m%d-%H%M%S")


def restore_to_original(client: PbsClient, profile: Profile, snapshot: Snapshot, archive: str, rels: list[str],
                        home: Path | None = None, on_line: Callable[[str], None] = lambda _l: None) -> CopyResult:
    """Schreibt die gewählten Pfade an den Originalort zurück: nur Home, mit Sicherheitskopie der alten Dateien."""
    home = (home or Path.home()).resolve()
    items = map_to_original(profile, archive, rels, home)
    require_home_only(items)
    origin = Path(archive_origin(profile, archive) or "/")
    safety = safety_dir_for(home)
    with mounted(client, snapshot, archive) as mp:
        # relativ zum Originalordner kopieren: dest_root = Quellordner des Archivs, Zielpfade auf Home begrenzt
        return copy_selection(mp, rels, origin, overwrite=True, confine=home, safety_dir=safety, on_line=on_line)


def restore_to_folder(client: PbsClient, snapshot: Snapshot, archive: str, rels: list[str], target: Path,
                      on_line: Callable[[str], None] = lambda _l: None) -> CopyResult:
    """Kopiert die gewählten Pfade in einen Zielordner (nichts wird überschrieben)."""
    target = Path(target).expanduser()
    if target.exists() and not target.is_dir():
        raise FilesError(f"{target} ist kein Ordner")
    target.mkdir(parents=True, exist_ok=True)
    with mounted(client, snapshot, archive) as mp:
        return copy_selection(mp, rels, target, overwrite=False, on_line=on_line)
