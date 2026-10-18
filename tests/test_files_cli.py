"""`pbsgui files` und `pbsgui restore --path/--overwrite` mit Fake-Client."""
import os
from pathlib import Path

import pytest

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core.models import Profile
from test_cli import add_b, env, run  # noqa: F401
from test_rescue import put_snapshots, snap


@pytest.fixture
def fl(env, monkeypatch, tmp_path, capsys, server):
    """Profil 'Test' mit Quelle = (Fake-)Home; Backup-Baum und Snapshot im Fake-Client."""
    home = tmp_path / "heim"
    (home / "Dokumente/sub").mkdir(parents=True)
    (home / "Dokumente/a.txt").write_text("LOKAL ALT")
    monkeypatch.setenv("HOME", str(home))
    fp_args = ("--source", str(home))
    from pbsgui.core.pbs_api import fetch_fingerprint
    code = run(capsys, "add", "--name", "Test", "--server", "127.0.0.1", "--port", str(server), "--fingerprint",
               fetch_fingerprint("127.0.0.1", server), "--datastore", "store1", "--namespace", "a", "--token-id", "u@pbs!t",
               "--backup-id", "host", "--no-encrypt", *fp_args)[0]
    assert code == 0
    name = Profile(name="t", server="s", datastore="d", auth_id="a", sources=[str(home)]).archive_specs()[0].split(":")[0]
    tree = tmp_path / "pxar" / name
    (tree / "Dokumente/sub").mkdir(parents=True)
    (tree / "Dokumente/a.txt").write_text("AUS BACKUP")
    (tree / "Dokumente/sub/b.txt").write_text("B")
    (tree / "notiz mit leerzeichen.txt").write_text("N")
    monkeypatch.setenv("FAKE_PBS_TREE", str(tmp_path / "pxar"))
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")
    put_snapshots(env / "fake", [snap(100, bid="host")])
    capsys.readouterr()
    return home, name


def test_files_lists_directories_and_searches(fl, capsys):
    code, out, _ = run(capsys, "files", "Test")
    assert code == 0 and "Dokumente/" in out and "notiz mit leerzeichen.txt" in out
    assert out.index("Dokumente/") < out.index("notiz mit")                        # Ordner zuerst
    code, out, _ = run(capsys, "files", "Test", "Dokumente")
    assert "a.txt" in out and "sub/" in out
    code, out, _ = run(capsys, "files", "Test", "--search", "b.TXT")
    assert "1 Treffer" in out and "/Dokumente/sub/b.txt" in out
    assert run(capsys, "files", "Test", "gibtsnicht")[0] == 1


def test_restore_selected_to_target_keeps_originals(fl, capsys, tmp_path):
    home, _ = fl
    code, out, err = run(capsys, "restore", "Test", "--path", "/Dokumente", "--path", "/notiz mit leerzeichen.txt",
                         "--target", str(tmp_path / "zurueck"))
    assert code == 0, err
    assert "2 Pfad(e), 3 Dateien" in out
    assert (tmp_path / "zurueck/Dokumente/a.txt").read_text() == "AUS BACKUP"
    assert (home / "Dokumente/a.txt").read_text() == "LOKAL ALT"                  # Original unberührt


def test_overwrite_in_home_needs_confirmation_and_keeps_safety_copy(fl, capsys):
    home, _ = fl
    code, _, err = run(capsys, "restore", "Test", "--path", "/Dokumente", "--overwrite")      # kein TTY, kein --yes
    assert code == 2 and "--yes" in err and (home / "Dokumente/a.txt").read_text() == "LOKAL ALT"
    code, out, err = run(capsys, "restore", "Test", "--path", "/Dokumente", "--overwrite", "--yes")
    assert code == 0, err
    assert (home / "Dokumente/a.txt").read_text() == "AUS BACKUP" and (home / "Dokumente/sub/b.txt").exists()
    safety = next((home / ".local/share/pbsgui/overwritten").iterdir())
    assert (safety / "Dokumente/a.txt").read_text() == "LOKAL ALT"
    assert "1 überschriebene Dateien gesichert" in out


def test_overwrite_refused_outside_home(fl, capsys, tmp_path, monkeypatch):
    home, _ = fl
    monkeypatch.setenv("HOME", str(tmp_path / "anderes-home"))              # die Quelle liegt jetzt NICHT mehr im Home
    (tmp_path / "anderes-home").mkdir()
    code, _, err = run(capsys, "restore", "Test", "--path", "/Dokumente", "--overwrite", "--yes")
    assert code == 1 and "nur im Home-Verzeichnis" in err
    assert (home / "Dokumente/a.txt").read_text() == "LOKAL ALT"


def test_overwrite_without_path_is_rejected(fl, capsys):
    code, _, err = run(capsys, "restore", "Test", "--overwrite", "--yes", "--target", "/tmp/x")
    assert code == 1 and "--path" in err


def test_unknown_path_is_reported_with_hint(fl, capsys, tmp_path):
    code, _, err = run(capsys, "restore", "Test", "--path", "/gibt-es-nicht", "--target", str(tmp_path / "z"))
    assert code == 1 and "Nicht im Archiv" in err and "pbsgui files" in err


def test_selected_restore_needs_target_or_overwrite(fl, capsys):
    code, _, err = run(capsys, "restore", "Test", "--path", "/Dokumente")
    assert code == 1 and "--target" in err
