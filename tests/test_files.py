"""File-Level-Restore: Katalog, Auswahl, Kopieren, Überschreiben nur im Home."""
import os
import stat
from pathlib import Path

import pytest

from pbsgui.core import files
from pbsgui.core.client import PbsClient
from pbsgui.core.files import (Catalog, FilesError, archive_origin, copy_selection, load_catalog, map_to_original,
                               mounted, parse_catalog, require_home_only, restore_to_folder, restore_to_original,
                               unescape)
from pbsgui.core.models import Profile, Snapshot

# Echte Ausgabe von `proxmox-backup-client catalog dump` (Client 4.2.8) mit Sonderzeichen
REAL = r'''d "./odd.pxar.didx"
f "./odd.pxar.didx/back\\slash.txt" 0 2026-10-18T21:18:32Z
d "./odd.pxar.didx/dir \"q\""
f "./odd.pxar.didx/dir \"q\"/x" 2 2026-10-18T21:18:32Z
f "./odd.pxar.didx/emoji-😀.txt" 0 2026-10-18T21:18:32Z
f "./odd.pxar.didx/it's.txt" 0 2026-10-18T21:18:32Z
f "./odd.pxar.didx/mit \"anf.txt" 0 2026-10-18T21:18:32Z
f "./odd.pxar.didx/tab\there.txt" 0 2026-10-18T21:18:32Z
f "./odd.pxar.didx/zeile\nnummer.txt" 0 2026-10-18T21:18:32Z
d "./home-david.pxar.didx"
d "./home-david.pxar.didx/Dokumente"
f "./home-david.pxar.didx/Dokumente/a.txt" 10 2026-10-18T21:18:00Z
l "./home-david.pxar.didx/Dokumente/link"
f "./home-david.pxar.didx/notiz.txt" 5 2026-10-18T21:18:00Z
'''


def test_unescape():
    assert unescape(r"a\\b") == "a\\b" and unescape(r"\"x\"") == '"x"' and unescape(r"t\tn\nr\r") == "t\tn\nr\r"
    assert unescape(r"\u{1f600}") == "😀" and unescape("keine Escapes ö") == "keine Escapes ö" and unescape(r"it\'s") == "it's"


def test_parse_real_catalog_format():
    arch = parse_catalog(REAL)
    assert set(arch) == {"odd.pxar", "home-david.pxar"}                  # Archivnamen ohne .didx
    names = {e.path for e in arch["odd.pxar"]}
    assert {'/back\\slash.txt', '/dir "q"', '/dir "q"/x', "/emoji-😀.txt", "/it's.txt", '/mit "anf.txt',
            "/tab\there.txt", "/zeile\nnummer.txt", "/"} == names
    e = next(e for e in arch["home-david.pxar"] if e.path == "/notiz.txt")
    assert (e.kind, e.size, e.name, e.parent) == ("f", 5, "notiz.txt", "/")
    assert next(e for e in arch["home-david.pxar"] if e.kind == "l").size is None


def test_parse_ignores_noise():
    assert parse_catalog("Warnung: irgendwas\n\n") == {}


def test_catalog_children_walk_search_count():
    cat = Catalog(parse_catalog(REAL)["home-david.pxar"])
    assert [e.name for e in cat.children("/")] == ["Dokumente", "notiz.txt"]          # Ordner zuerst
    assert [e.name for e in cat.children("/Dokumente")] == ["a.txt", "link"]
    assert [e.path for e in cat.walk("/Dokumente")] == ["/Dokumente", "/Dokumente/a.txt", "/Dokumente/link"] or \
        {e.path for e in cat.walk("/Dokumente")} == {"/Dokumente", "/Dokumente/a.txt", "/Dokumente/link"}
    assert cat.count(["/Dokumente", "/notiz.txt", "/Dokumente/a.txt"]) == (3, 15)       # keine Doppelzählung
    assert [e.name for e in cat.search("NOTIZ")] == ["notiz.txt"]
    assert [e.name for e in cat.search("*.txt")] == ["a.txt", "notiz.txt"] or {e.name for e in cat.search("*.txt")} == {"a.txt", "notiz.txt"}
    assert cat.search("gibtsnicht") == []


# -- Kopieren ---------------------------------------------------------------------------------------
@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "mnt"
    (src / "Dokumente/sub").mkdir(parents=True)
    (src / "Dokumente/a.txt").write_text("NEU a")
    (src / "Dokumente/sub/b.txt").write_text("NEU b")
    (src / "notiz.txt").write_text("NEU notiz")
    os.symlink("a.txt", src / "Dokumente/link")
    os.mkfifo(src / "fifo")
    return src


def test_copy_to_folder_keeps_structure_and_skips_special_files(tree, tmp_path):
    dest = tmp_path / "ziel"
    res = copy_selection(tree, ["/Dokumente", "/fifo", "/gibtsnicht"], dest)
    assert (dest / "Dokumente/a.txt").read_text() == "NEU a" and (dest / "Dokumente/sub/b.txt").read_text() == "NEU b"
    assert os.readlink(dest / "Dokumente/link") == "a.txt"
    assert res.copied == 3 and any("Sonderdatei" in s for s in res.skipped) and any("nicht im Archiv" in s for s in res.skipped)


def test_copy_without_overwrite_never_touches_existing_files(tree, tmp_path):
    dest = tmp_path / "ziel"
    (dest / "Dokumente").mkdir(parents=True)
    (dest / "Dokumente/a.txt").write_text("ALT a")
    res = copy_selection(tree, ["/Dokumente"], dest)
    assert (dest / "Dokumente/a.txt").read_text() == "ALT a"
    assert any("a.txt (existiert schon)" in s for s in res.skipped) and (dest / "Dokumente/sub/b.txt").exists()


def test_overwrite_makes_safety_copies_and_keeps_extra_files(tree, tmp_path):
    dest, safety = tmp_path / "home", tmp_path / "safety"
    (dest / "Dokumente").mkdir(parents=True)
    (dest / "Dokumente/a.txt").write_text("ALT a")
    (dest / "Dokumente/nur-lokal.txt").write_text("bleibt")
    res = copy_selection(tree, ["/Dokumente"], dest, overwrite=True, safety_dir=safety)
    assert (dest / "Dokumente/a.txt").read_text() == "NEU a"
    assert (safety / "Dokumente/a.txt").read_text() == "ALT a" and res.safety_copies == 1   # nur was ersetzt wurde
    assert (dest / "Dokumente/nur-lokal.txt").read_text() == "bleibt"                         # nichts wird gelöscht
    assert not (safety / "Dokumente/sub").exists()                                            # Neues braucht keine Kopie


def test_overwrite_replaces_read_only_files_atomically(tree, tmp_path):
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / "notiz.txt").write_text("ALT")
    os.chmod(dest / "notiz.txt", 0o444)
    copy_selection(tree, ["/notiz.txt"], dest, overwrite=True, safety_dir=tmp_path / "s")
    assert (dest / "notiz.txt").read_text() == "NEU notiz"
    assert not list(dest.glob("*.tmp"))                                                       # keine Reste


def test_confine_blocks_symlink_escape(tree, tmp_path):
    home, outside = tmp_path / "home", tmp_path / "draussen"
    home.mkdir()
    outside.mkdir()
    os.symlink(outside, home / "Dokumente")                       # Ordner im Home zeigt nach draußen
    with pytest.raises(FilesError, match="außerhalb"):
        copy_selection(tree, ["/Dokumente"], home, overwrite=True, confine=home, safety_dir=tmp_path / "s")
    assert not list(outside.iterdir())                            # draußen wurde nichts geschrieben


def test_confine_allows_the_confine_root_itself(tree, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    copy_selection(tree, ["/"], home, overwrite=True, confine=home, safety_dir=tmp_path / "s")
    assert (home / "notiz.txt").exists()


# -- Originalort / Home ---------------------------------------------------------------------------------
def prof(**kw):
    return Profile(name="p", server="s", datastore="d", auth_id="a", sources=["/home/david", "/etc"], **kw)


def test_archive_origin_and_mapping(tmp_path):
    p = prof()
    assert archive_origin(p, "home-david.pxar") == "/home/david" and archive_origin(p, "etc.pxar") == "/etc"
    assert archive_origin(p, "recovery.pxar") is None
    home = tmp_path / "h"
    p2 = Profile(name="p", server="s", datastore="d", auth_id="a", sources=[str(home)])
    items = map_to_original(p2, f"{str(home).strip('/').replace('/', '-')}.pxar", ["/Dokumente/a.txt", "/../x"], home)
    assert items[0].dest == home / "Dokumente/a.txt" and items[0].in_home
    assert not items[1].in_home                                                                # ../ führt hinaus


def test_overwrite_only_inside_home():
    items = map_to_original(prof(), "etc.pxar", ["/hosts"], Path("/home/david"))
    with pytest.raises(FilesError, match="nur im Home-Verzeichnis"):
        require_home_only(items)
    with pytest.raises(FilesError, match="unbekannt"):
        map_to_original(prof(), "recovery.pxar", ["/x"], Path("/home/david"))


def test_full_mode_root_archive_allows_only_home_paths(tmp_path):
    home = tmp_path / "home/david"
    home.mkdir(parents=True)
    p = Profile(name="p", server="s", datastore="d", auth_id="a", backup_mode="full", sources=["/"])
    items = map_to_original(p, "root.pxar", [f"{home}/Dokumente/a.txt", "/etc/passwd", f"{home}/../.."], home)
    assert [i.in_home for i in items] == [True, False, False]


# -- Mit Fake-Client (Einhängen als Symlink) ------------------------------------------------------------
@pytest.fixture
def lab(fake_env, tmp_path, monkeypatch):
    tree = tmp_path / "pxar"
    arch = tree / "home.pxar"
    (arch / "Dokumente").mkdir(parents=True)
    (arch / "Dokumente/a.txt").write_text("AUS BACKUP")
    (arch / "notiz.txt").write_text("AUS BACKUP 2")
    monkeypatch.setenv("FAKE_PBS_TREE", str(tree))
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")                       # kein echtes FUSE im Test
    home = tmp_path / "heim"
    (home / "Dokumente").mkdir(parents=True)
    (home / "Dokumente/a.txt").write_text("LOKAL ALT")
    profile = Profile(name="p", server="s", datastore="d", auth_id="a", sources=[str(home)])
    snap = Snapshot("host", "pc", 1000, files=["home.pxar"])
    # Archivname für dieses Profil ist aus dem Pfad abgeleitet; für den Test den Baum entsprechend benennen
    name = profile.archive_specs()[0].split(":")[0]
    (tree / name).symlink_to(arch, target_is_directory=True)
    return PbsClient(profile, "x"), profile, snap, home, name, tmp_path


def test_load_catalog_from_client(lab):
    client, profile, snap, home, name, _ = lab
    cats = load_catalog(client, snap)
    assert "/Dokumente/a.txt" in cats["home.pxar"].by_path and cats["home.pxar"].children("/")[0].name == "Dokumente"


def test_restore_to_folder(lab):
    client, profile, snap, home, name, tmp = lab
    res = restore_to_folder(client, snap, "home.pxar", ["/Dokumente"], tmp / "zurueck")
    assert (tmp / "zurueck/Dokumente/a.txt").read_text() == "AUS BACKUP" and res.copied == 1
    assert (home / "Dokumente/a.txt").read_text() == "LOKAL ALT"            # Original unberührt


def test_restore_to_original_overwrites_with_safety_copy(lab):
    client, profile, snap, home, name, tmp = lab
    res = restore_to_original(client, profile, snap, name, ["/Dokumente"], home=home)
    assert (home / "Dokumente/a.txt").read_text() == "AUS BACKUP"
    assert res.safety_copies == 1 and (res.safety_dir / "Dokumente/a.txt").read_text() == "LOKAL ALT"
    assert str(res.safety_dir).startswith(str(home / ".local/share/pbsgui/overwritten"))


def test_restore_to_original_refuses_outside_home_before_mounting(lab, monkeypatch):
    client, profile, snap, home, name, tmp = lab
    monkeypatch.setattr(files, "mounted", lambda *a, **k: pytest.fail("darf nicht einmal einhängen"))
    other = Profile(name="p", server="s", datastore="d", auth_id="a", sources=["/etc"])
    with pytest.raises(FilesError, match="nur im Home-Verzeichnis"):
        restore_to_original(client, other, snap, "etc.pxar", ["/hosts"], home=home)
    with pytest.raises(FilesError, match="nur im Home-Verzeichnis"):
        restore_to_original(client, profile, snap, name, ["/../../etc/passwd"], home=home)


def test_mount_failure_is_explained(lab, monkeypatch):
    client, profile, snap, home, name, tmp = lab
    monkeypatch.setenv("FAKE_PBS_NO_MOUNT", "1")
    with pytest.raises(FilesError, match="FUSE"):
        with mounted(client, snap, "home.pxar"):
            pass
    with pytest.raises(FilesError, match="kein Datei-Archiv"):
        with mounted(client, snap, "catalog.pcat1"):
            pass
