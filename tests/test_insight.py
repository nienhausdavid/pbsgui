"""Kommandozeile: versions, find, diff, preview, restore-test, dashboard, history (Fake-Client + Testserver)."""
import json
import time

import pytest

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core.models import Profile
from pbsgui.core.pbs_api import fetch_fingerprint
from test_cli import add_b, env, run  # noqa: F401
from test_rescue import put_snapshots

NOW = int(time.time())


def tree(base, arch, files):
    for rel, text in files.items():
        f = base / arch / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


@pytest.fixture
def ins(env, monkeypatch, tmp_path, capsys, server):
    """Profil 'Test' (Quelle = Fake-Home) mit drei Snapshots, die sich unterscheiden."""
    home = tmp_path / "heim"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")
    code = run(capsys, "add", "--name", "Test", "--server", "127.0.0.1", "--port", str(server), "--fingerprint",
               fetch_fingerprint("127.0.0.1", server), "--datastore", "store1", "--namespace", "a", "--token-id", "u@pbs!t",
               "--backup-id", "host", "--no-encrypt", "--source", str(home))[0]
    assert code == 0
    arch = Profile(name="t", server="s", datastore="d", auth_id="a", sources=[str(home)]).archive_specs()[0].split(":")[0]
    times = [NOW - 3 * 86400, NOW - 2 * 86400, NOW - 86400]
    contents = [{"a.txt": "eins", "alt.txt": "weg"}, {"a.txt": "zwei-lang", "alt.txt": "weg"}, {"a.txt": "zwei-lang", "neu.txt": "da"}]
    trees = tmp_path / "trees"
    items = []
    for t, files in zip(times, contents):
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        tree(trees / f"host_host_{iso}", arch, files)
        items.append({"backup-type": "host", "backup-id": "host", "backup-time": t, "size": 1000 * (len(items) + 1),
                      "files": [{"filename": arch + ".didx"}]})
    monkeypatch.setenv("FAKE_PBS_TREES", str(trees))
    put_snapshots(env / "fake", items)
    capsys.readouterr()
    return home, arch, items


def test_versions_lists_distinct_versions_and_restores_one(ins, capsys, tmp_path):
    code, out, err = run(capsys, "versions", "Test", "/a.txt")
    assert code == 0, err
    assert "2 Fassung(en), 3 Snapshots geprüft" in out
    code, out, err = run(capsys, "versions", "Test", "a.txt", "--restore", "2", "--target", str(tmp_path / "zurueck"))
    assert code == 0, err
    assert (tmp_path / "zurueck/a.txt").read_text() == "eins"                      # Fassung 2 = die älteste
    assert run(capsys, "versions", "Test", "/a.txt", "--restore", "9", "--target", str(tmp_path / "x"))[0] == 1
    assert run(capsys, "versions", "Test", "/a.txt", "--restore", "1")[0] == 1      # ohne --target
    code, out, _ = run(capsys, "versions", "Test", "/a.txt", "--json")
    data = json.loads(out)
    assert [len(v["snapshots"]) for v in data["versions"]] == [2, 1] and data["checked"] == 3
    code, _, err = run(capsys, "versions", "Test", "/gibtsnicht")
    assert code == 1 and "keinem der 3 Snapshots" in err


def test_find_searches_all_snapshots_including_deleted(ins, capsys):
    code, out, err = run(capsys, "find", "Test", "alt")
    assert code == 0, err
    assert "/alt.txt" in out and "NEIN (gelöscht)" in out and "1 Treffer in 3 Snapshots" in out
    code, out, _ = run(capsys, "find", "Test", "*.txt", "--json")
    hits = {h["path"]: h for h in json.loads(out)["hits"]}
    assert set(hits) == {"/a.txt", "/alt.txt", "/neu.txt"} and hits["/a.txt"]["in_newest"] and not hits["/alt.txt"]["in_newest"]


def test_diff_defaults_to_last_two_and_orders_arguments(ins, capsys):
    code, out, err = run(capsys, "diff", "Test")
    assert code == 0, err
    assert "1 neu, 0 geändert, 1 gelöscht" in out and "+ /neu.txt" in out and "- /alt.txt" in out
    code, out, _ = run(capsys, "diff", "Test", "0", "2")                          # vertauscht: wird richtig herum verglichen
    assert "1 neu, 1 geändert, 1 gelöscht" in out
    assert "~ /a.txt  (4 B → 9 B)" in out
    code, out, _ = run(capsys, "diff", "Test", "2", "0", "--json")
    d = json.loads(out)["archives"][0]
    assert {i["change"] for i in d["items"]} == {"added", "removed", "changed"}


def test_preview_prints_text_and_rejects_missing(ins, capsys):
    code, out, err = run(capsys, "preview", "Test", "/a.txt", "--snapshot", "2")
    assert code == 0, err
    assert out.strip() == "eins"
    code, out, _ = run(capsys, "preview", "Test", "/a.txt")
    assert out.strip() == "zwei-lang"
    assert run(capsys, "preview", "Test", "/gibtsnicht")[0] == 1


def test_restore_test_exit_code(ins, capsys):
    home, arch, _ = ins
    code, out, err = run(capsys, "restore-test", "Test", "--count", "2")
    assert code == 0, err
    assert "2 von 2 Dateien in Ordnung" in out
    code, out, _ = run(capsys, "restore-test", "Test", "--json")
    assert json.loads(out)[0]["ok"] is True
    code, out, _ = run(capsys, "dashboard", "Test", "--offline")
    assert "letzter Restore-Test" in out and "in Ordnung" in out


def test_history_records_runs_and_filters(ins, capsys, monkeypatch):
    assert run(capsys, "backup", "Test")[0] == 0
    monkeypatch.setenv("FAKE_PBS_FAIL", "1")
    assert run(capsys, "backup", "Test")[0] != 0
    code, out, _ = run(capsys, "history")
    assert code == 0 and out.count("FEHLER") == 1 and out.count(" OK ") == 1
    code, out, _ = run(capsys, "history", "Test", "--failed")
    assert out.count("FEHLER") == 1 and " OK " not in out
    rid = out.split("\n")[2].split()[0]
    code, out, _ = run(capsys, "history", "--show", rid)
    assert "upload failed: connection reset" in out and "Exitcode" in out
    code, out, _ = run(capsys, "history", "--search", "connection reset")
    assert out.count("FEHLER") == 1
    code, out, _ = run(capsys, "history", "--search", "gibtsnicht")
    assert "Keine passenden Läufe" in out
    code, out, _ = run(capsys, "history", "--json")
    data = json.loads(out)
    assert len(data) == 2 and data[0]["ok"] is False and data[1]["ok"] is True
    assert run(capsys, "history", "--show", "0-0")[0] == 1


def test_dashboard_table_usage_and_state(ins, capsys, monkeypatch):
    assert run(capsys, "backup", "Test")[0] == 0
    code, out, err = run(capsys, "dashboard")
    assert code == 0, err
    row = next(l for l in out.splitlines() if l.startswith("Test"))
    assert row.split()[1] == "OK" and row.split()[-1] == "4"                      # 3 vorbereitete + 1 neues Backup
    assert "1.0 TiB von 4.0 TiB belegt (25 %)" in out and "Deduplizierung 3.0×" in out
    monkeypatch.setenv("FAKE_PBS_FAIL", "1")
    run(capsys, "backup", "Test")
    code, out, _ = run(capsys, "dashboard", "Test", "--json")
    d = json.loads(out)[0]
    assert d["state"] == "error" and d["snapshots"] == 4 and d["growth_last"] is not None and not d["running"]
    assert d["datastore"]["dedup"] == 3.0
    assert any("fehlgeschlagen" in w for w in d["warnings"])
    code, out, _ = run(capsys, "dashboard", "--offline")
    assert "Datastore" not in out and "FEHLER" in out
