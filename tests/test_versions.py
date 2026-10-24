"""Dateiversionen, Suche über alle Snapshots, Vergleich, Vorschau und Restore-Test (mit Fake-Client)."""
import calendar
import json
import os
import random
from pathlib import Path

import pytest

from pbsgui.core import versions
from pbsgui.core.client import PbsClient
from pbsgui.core.files import Catalog, CatalogEntry
from pbsgui.core.models import Profile, Snapshot
from pbsgui.core.versions import (CatalogCache, PreviewSession, diff_catalogs, file_versions, hexdump, pick_samples,
                                  preview_path, restore_test, search_snapshots)

T0 = 1_760_000_000


def E(path, size=1, mtime="2026-10-01T10:00:00Z", kind="f"):
    return CatalogEntry(kind, path, size if kind == "f" else None, mtime if kind == "f" else "")


def cat(*entries):
    return Catalog([CatalogEntry("d", "/"), *entries])


def snap(i):
    return Snapshot("host", "laptop", T0 + i * 86400, size=100 * (i + 1))


def loader_for(mapping):
    """{Snapshot-Index: {Archiv: Catalog}} -> Loader."""
    def load(s):
        return mapping[(s.backup_time - T0) // 86400]
    return load


# -- Versionen ---------------------------------------------------------------------------------------
def test_file_versions_groups_identical_versions():
    snaps = [snap(3), snap(2), snap(1), snap(0)]                     # neueste zuerst
    m = {0: {"h.pxar": cat(E("/a.txt", 5, "t1"))},
         1: {"h.pxar": cat(E("/a.txt", 5, "t1"))},
         2: {"h.pxar": cat(E("/a.txt", 7, "t2"))},
         3: {"h.pxar": cat()}}                                        # im neuesten gelöscht
    res = file_versions(snaps, loader_for(m), "a.txt")
    assert res.archive == "h.pxar" and res.path == "/a.txt" and res.checked == 4
    assert [(v.entry.size, len(v.snapshots)) for v in res.versions] == [(7, 1), (5, 2)]   # neueste Fassung zuerst
    assert res.versions[1].newest.backup_time == T0 + 86400 and res.versions[1].oldest.backup_time == T0


def test_file_versions_collects_errors_and_progress():
    def load(s):
        if s.backup_time == T0 + 86400:
            raise RuntimeError("kaputt")
        return {"h.pxar": cat(E("/a", 1))}
    seen = []
    res = file_versions([snap(2), snap(1), snap(0)], load, "/a", on_progress=lambda d, t: seen.append((d, t)))
    assert len(res.errors) == 1 and "kaputt" in res.errors[0] and res.checked == 2
    assert seen[-1] == (3, 3)


def test_file_versions_unknown_path_and_archive_choice():
    m = {0: {"a.pxar": cat(E("/x", 1)), "b.pxar": cat(E("/x", 2))}}
    assert file_versions([snap(0)], loader_for(m), "/nix").versions == []
    res = file_versions([snap(0)], loader_for(m), "/x", archive="b.pxar")
    assert res.archive == "b.pxar" and res.versions[0].entry.size == 2


# -- Suche ---------------------------------------------------------------------------------------------
def test_search_snapshots_merges_hits_across_snapshots():
    m = {0: {"h.pxar": cat(E("/Doku/alt.txt", 1), E("/Doku/bleibt.txt", 1, "a"))},
         1: {"h.pxar": cat(E("/Doku/bleibt.txt", 1, "a"))},
         2: {"h.pxar": cat(E("/Doku/bleibt.txt", 2, "b"))}}
    res = search_snapshots([snap(2), snap(1), snap(0)], loader_for(m), "*.txt")
    by = {h.path: h for h in res.hits}
    assert set(by) == {"/Doku/alt.txt", "/Doku/bleibt.txt"} and res.checked == 3
    assert [s.backup_time for s in by["/Doku/alt.txt"].snapshots] == [T0] and not by["/Doku/alt.txt"].in_newest
    assert len(by["/Doku/bleibt.txt"].snapshots) == 3 and by["/Doku/bleibt.txt"].versions == 2
    assert by["/Doku/bleibt.txt"].in_newest
    assert res.hits[0].path == "/Doku/bleibt.txt"                      # Treffer im neuesten Snapshot zuerst


def test_search_limit_marks_truncated():
    m = {0: {"h.pxar": cat(*[E(f"/f{i}.txt") for i in range(10)])}}
    res = search_snapshots([snap(0)], loader_for(m), "f", limit=3)
    assert len(res.hits) == 3 and res.truncated


# -- Vergleich -----------------------------------------------------------------------------------------
def test_diff_catalogs_added_removed_changed_collapsed():
    old = cat(E("/d", kind="d"), E("/d/x", 3), E("/alt", 1), E("/same", 2), E("/chg", 4, "t1"), E("/gone", kind="d"),
              E("/gone/y", 10), E("/gone/z", 20))
    new = cat(E("/d", kind="d"), E("/d/x", 3), E("/same", 2), E("/chg", 6, "t2"), E("/neu", 9),
              E("/newdir", kind="d"), E("/newdir/a", 1), E("/newdir/b", 2))
    res = diff_catalogs(old, new, "h.pxar")
    got = {(i.change, i.path): i for i in res.items}
    assert set(got) == {("added", "/neu"), ("added", "/newdir"), ("removed", "/alt"), ("removed", "/gone"), ("changed", "/chg")}
    assert (got["added", "/newdir"].files, got["added", "/newdir"].size) == (2, 3)             # Ordner zusammengefasst
    assert (got["removed", "/gone"].files, got["removed", "/gone"].size) == (2, 30)
    assert got["changed", "/chg"].size_delta == 2 and got["removed", "/alt"].size_delta == -1
    assert res.counts == {"added": 3, "removed": 3, "changed": 1}


def test_diff_with_missing_side_and_identical():
    c = cat(E("/a", 1))
    assert diff_catalogs(c, c).items == []
    assert [i.change for i in diff_catalogs(None, c).items] == ["added"]
    assert [i.change for i in diff_catalogs(c, None).items] == ["removed"]


def test_diff_snapshots_filters_archive():
    m = {0: {"a.pxar": cat(E("/x", 1)), "recovery.pxar": cat(E("/r", 1))}, 1: {"a.pxar": cat(E("/x", 2)), "b.pxar": cat(E("/y"))}}
    res = versions.diff_snapshots(loader_for(m), snap(0), snap(1))
    assert [r.archive for r in res] == ["a.pxar", "b.pxar"]
    assert [r.archive for r in versions.diff_snapshots(loader_for(m), snap(0), snap(1), "b.pxar")] == ["b.pxar"]


# -- Vorschau -------------------------------------------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def test_preview_text_image_binary_link_dir(tmp_path):
    (tmp_path / "t.txt").write_text("Grüße\nzweite Zeile\n")
    (tmp_path / "bild.png").write_bytes(PNG)
    (tmp_path / "fake.png").write_bytes(b"kein Bild\n")
    (tmp_path / "b.bin").write_bytes(bytes(range(256)) + b"\0" * 44)
    (tmp_path / "d").mkdir()
    os.symlink("/etc/passwd", tmp_path / "link")
    os.mkfifo(tmp_path / "fifo")
    p = preview_path(tmp_path, "/t.txt")
    assert (p.kind, p.text) == ("text", "Grüße\nzweite Zeile\n") and not p.truncated
    assert preview_path(tmp_path, "/bild.png").kind == "image" and preview_path(tmp_path, "/bild.png").data == PNG
    assert preview_path(tmp_path, "/fake.png").kind == "text"             # Endung lügt: als Text anzeigen
    b = preview_path(tmp_path, "/b.bin")
    assert b.kind == "binary" and b.text.startswith("00000000  00 01 02") and b.truncated
    assert preview_path(tmp_path, "/d").kind == "dir"
    lk = preview_path(tmp_path, "/link")
    assert lk.kind == "link" and lk.text == "/etc/passwd" and not lk.data                # nie dem Link folgen
    assert preview_path(tmp_path, "/fifo").kind == "special"
    assert preview_path(tmp_path, "/gibtsnicht").kind == "missing"


def test_preview_truncates_and_handles_cut_utf8(tmp_path):
    (tmp_path / "gross.txt").write_text("ä" * 100)                        # 200 Bytes
    p = preview_path(tmp_path, "/gross.txt", max_bytes=51)                # schneidet mitten in ein Zeichen
    assert p.kind == "text" and p.truncated and p.text == "ä" * 25


def test_preview_does_not_leave_root_via_dotdot(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    (tmp_path / "geheim.txt").write_text("geheim")
    link_dir = root / "ld"
    os.symlink(tmp_path, link_dir)                                        # Verzeichnis-Link aus dem Archiv heraus
    assert preview_path(root, "/ld/geheim.txt").kind == "missing"


def test_hexdump_format():
    assert hexdump(b"AB\x00").splitlines()[0].endswith("AB.")


# -- Mit Fake-Client ------------------------------------------------------------------------------------
def make_tree(base: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return base


@pytest.fixture
def world(fake_env, tmp_path, monkeypatch):
    """Drei Snapshots mit je eigenem Dateibaum; Archiv = Name des Quellordners."""
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")      # Testersatz für FUSE: fusermount nötig sonst (fehlt z. B. in der CI)
    origin = tmp_path / "home"
    prof = Profile(name="Laptop", server="pbs.example", datastore="s", auth_id="u@pbs!t", fingerprint="AA", backup_id="laptop",
                   sources=[str(origin)], encrypt=False)
    arch = prof.archive_specs()[0].split(":")[0]
    trees = tmp_path / "trees"
    snaps = []
    contents = [{"a.txt": "eins", "b.txt": "bleibt"}, {"a.txt": "zwei-lang", "b.txt": "bleibt"}, {"a.txt": "zwei-lang", "c.txt": "neu"}]
    for i, files in enumerate(contents):
        s = Snapshot("host", "laptop", T0 + i * 86400, size=100)
        make_tree(trees / s.path.replace("/", "_") / arch, files)
        snaps.append(s)
    fake_env.mkdir(parents=True, exist_ok=True)
    (fake_env / "snapshots.json").write_text(json.dumps(
        [{"backup-type": "host", "backup-id": "laptop", "backup-time": s.backup_time, "size": s.size, "files": [{"filename": arch + ".didx"}]}
         for s in snaps]))
    monkeypatch.setenv("FAKE_PBS_TREES", str(trees))
    monkeypatch.setenv("FAKE_PBS_TREE", str(trees / snaps[-1].path.replace("/", "_")))   # Rückfall
    cache = CatalogCache(tmp_path / "cache")
    client = PbsClient(prof, "secret")
    return prof, arch, snaps, client, cache, origin


def test_catalog_cache_loads_once_and_prunes(world, monkeypatch):
    prof, arch, snaps, client, cache, _ = world
    load = cache.loader(client, prof)
    first = load(snaps[0])
    assert set(first) == {arch} and "a.txt" in {e.name for e in first[arch].children("/")}
    calls = lambda: sum(1 for l in (Path(os.environ["FAKE_PBS_DIR"]) / "calls.jsonl").read_text().splitlines() if '"catalog"' in l)
    n = calls()
    load(snaps[0])
    assert calls() == n                                                   # zweiter Aufruf aus dem Zwischenspeicher
    assert oct((cache.dir).stat().st_mode & 0o777) == "0o700"
    f = next((cache.dir / prof.slug).glob("*.gz"))
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert cache.prune(prof, [snaps[1]]) == 1 and not f.exists()


def test_versions_and_search_and_diff_end_to_end(world):
    prof, arch, snaps, client, cache, _ = world
    load = cache.loader(client, prof)
    newest_first = list(reversed(snaps))
    res = file_versions(newest_first, load, "/a.txt")
    assert [len(v.snapshots) for v in res.versions] == [2, 1] and res.versions[0].entry.size == 9
    hits = search_snapshots(newest_first, load, "txt")
    assert {h.path for h in hits.hits} == {"/a.txt", "/b.txt", "/c.txt"}
    d = versions.diff_snapshots(load, snaps[1], snaps[2])[0]
    assert {(i.change, i.path) for i in d.items} == {("removed", "/b.txt"), ("added", "/c.txt")}


def test_restore_test_compares_with_unchanged_original(world):
    prof, arch, snaps, client, cache, origin = world
    make_tree(origin, {"a.txt": "zwei-lang", "c.txt": "neu"})
    ts = calendar.timegm((2026, 10, 18, 12, 0, 0))                        # wie die feste Zeit des Fake-Katalogs
    for f in origin.iterdir():
        os.utime(f, (ts, ts))
    cat_ = cache.loader(client, prof)(snaps[2])[arch]
    res = restore_test(client, prof, snaps[2], arch, cat_, count=5, rng=random.Random(1))
    assert res.ok and len(res.samples) == 2 and res.compared == 2
    assert {s.path: s.status for s in res.samples} == {"/a.txt": "ok", "/c.txt": "ok"}
    (origin / "a.txt").write_text("zwei-LANG")                            # gleiche Größe, Original verändert (Zeit neu setzen)
    os.utime(origin / "a.txt", (ts, ts))
    res = restore_test(client, prof, snaps[2], arch, cat_, count=5, rng=random.Random(1))
    assert not res.ok and {s.path: s.status for s in res.samples}["/a.txt"] == "abweichung"
    assert "FEHLER" in res.summary()
    assert res.to_dict()["ok"] is False


def test_restore_test_without_comparable_original_is_read_only_check(world):
    prof, arch, snaps, client, cache, origin = world                      # Original fehlt
    cat_ = cache.loader(client, prof)(snaps[0])[arch]
    res = restore_test(client, prof, snaps[0], arch, cat_, count=1, rng=random.Random(0))
    assert res.ok and res.compared == 0 and res.samples[0].status == "gelesen"


def test_restore_test_detects_size_mismatch(world, monkeypatch):
    prof, arch, snaps, client, cache, origin = world
    cat_ = cache.loader(client, prof)(snaps[0])[arch]
    bad = Catalog([CatalogEntry(e.kind, e.path, 999 if e.kind == "f" else e.size, e.mtime) for e in cat_.entries])
    res = restore_test(client, prof, snaps[0], arch, bad, count=2, rng=random.Random(0))
    assert not res.ok and all(s.status == "groesse" for s in res.samples)


def test_restore_test_mount_failure_reports_error(world, monkeypatch):
    prof, arch, snaps, client, cache, _ = world
    cat_ = cache.loader(client, prof)(snaps[0])[arch]
    monkeypatch.setenv("FAKE_PBS_NO_MOUNT", "1")
    with pytest.raises(Exception, match="Einhängen"):
        restore_test(client, prof, snaps[0], arch, cat_, count=1)


def test_pick_samples_prefers_nonempty_and_respects_limits():
    c = cat(E("/leer", 0), E("/a", 5), E("/riesig", 10 ** 12), E("/d", kind="d"))
    assert [e.path for e in pick_samples(c, 5, random.Random(0))] == ["/a"]
    assert pick_samples(cat(E("/leer", 0)), 3)[0].path == "/leer"
    assert pick_samples(cat(), 3) == []


def test_preview_session_mounts_once_and_unmounts(world):
    prof, arch, snaps, client, cache, _ = world
    with PreviewSession(client, snaps[0], arch) as ps:
        assert ps.preview("/a.txt").text == "eins"
        assert ps.preview("/b.txt").text == "bleibt"
        root = ps.root
    assert ps.root is None and not root.exists()


def test_preview_rejects_dotdot_and_outside_symlink(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    (tmp_path / "geheim.txt").write_text("geheim")
    os.symlink("/etc/passwd", tmp_path / "aussen-link")
    assert preview_path(root, "/../geheim.txt").kind == "missing"
    assert preview_path(root, "/../aussen-link").kind == "missing"         # nicht einmal das Linkziel verraten
