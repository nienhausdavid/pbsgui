"""TUI: Dashboard, Verlauf, Vergleich, Restore-Test, Suche in allen Snapshots, Fassungen und Vorschau."""
import json
import time

import pytest
from textual.widgets import Checkbox, DataTable, Input, RichLog, Static, TabbedContent, Tree

from fake_pbs_api import pytestmark  # noqa: F401
from pbsgui.core import manager as manager_mod
from pbsgui.core.dashboard import DatastoreUsage
from pbsgui.core.models import Snapshot
from pbsgui.tui.app import PbsTui
from test_tui import mgr, text, wait_for  # noqa: F401

T0 = int(time.time()) - 5 * 86400


def log_text(widget: RichLog) -> str:
    return "\n".join(l.text for l in widget.lines)


@pytest.fixture
def snaps(mgr, fake_env, tmp_path, monkeypatch):
    """Drei Snapshots mit unterschiedlichem Inhalt für das Profil 'Laptop'."""
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(manager_mod, "datastore_usage", lambda p, t: DatastoreUsage(4 * 1024 ** 4, 1024 ** 4, 3 * 1024 ** 4, 3.0))
    p = mgr.profiles()[0]
    p.sources = [str(tmp_path / "heim")]
    mgr.profiles_store.save(p)
    arch = p.archive_specs()[0].split(":")[0]
    trees, items = tmp_path / "trees", []
    contents = [{"a.txt": "eins", "alt.txt": "weg"}, {"a.txt": "zwei-lang", "alt.txt": "weg"}, {"a.txt": "zwei-lang", "neu.txt": "da"}]
    for i, files in enumerate(contents):
        s = Snapshot("host", "laptop", T0 + i * 86400, size=1000 * (i + 1), files=[arch + ".didx"])
        for rel, content in files.items():
            f = trees / s.path.replace("/", "_") / arch / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        items.append({"backup-type": "host", "backup-id": "laptop", "backup-time": s.backup_time, "size": s.size,
                      "files": [{"filename": arch + ".didx"}]})
    fake_env.mkdir(parents=True, exist_ok=True)
    (fake_env / "snapshots.json").write_text(json.dumps(items))
    monkeypatch.setenv("FAKE_PBS_TREES", str(trees))
    return arch


async def load_snaps(pilot, n=3):
    await pilot.press("r")
    table = pilot.app.query_one("#snaps", DataTable)
    assert await wait_for(pilot, lambda: table.row_count == n)


async def test_dashboard_shows_profile_usage_and_warnings(mgr, snaps):
    async with PbsTui(mgr).run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        dash = pilot.app.query_one("#dashboard", Static)
        assert await wait_for(pilot, lambda: "Datastore" in text(dash))
        t = text(dash)
        assert "Laptop" in t and "3 " in t and "1.0 TiB von 4.0 TiB belegt (25 %)" in t and "Deduplizierung 3.0×" in t
        assert "noch kein Restore-Test" in t
        assert pilot.app.query_one(TabbedContent).active == "tab-dashboard"


async def test_history_lists_runs_and_filters(mgr, snaps, monkeypatch):
    async with PbsTui(mgr).run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        assert await wait_for(pilot, lambda: not pilot.app._busy)
        monkeypatch.setenv("FAKE_PBS_FAIL", "1")
        await pilot.press("b")
        assert await wait_for(pilot, lambda: not pilot.app._busy and len(pilot.app._q("#log", RichLog).lines) >= 3)
        await pilot.press("h")
        table = pilot.app.query_one("#hist-table", DataTable)
        assert await wait_for(pilot, lambda: table.row_count == 2)
        assert [table.get_row_at(i)[3] for i in range(2)] == ["FEHLER", "OK"]
        assert "connection reset" in log_text(pilot.app.query_one("#hist-log", RichLog))          # Ausgabe des neuesten Laufs
        pilot.app.query_one("#hist-failed", Checkbox).value = True
        assert await wait_for(pilot, lambda: table.row_count == 1)
        pilot.app.query_one("#hist-failed", Checkbox).value = False
        pilot.app.query_one("#hist-text", Input).value = "Backup erfolgreich"
        pilot.app.query_one("#hist-text", Input).focus()
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: table.row_count == 1 and table.get_row_at(0)[3] == "OK")


async def test_diff_of_selected_snapshot_with_previous(mgr, snaps):
    async with PbsTui(mgr).run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await load_snaps(pilot)
        await pilot.press("x")                                                      # neuester gegen den vorherigen
        scr = pilot.app.screen
        log = scr.query_one("#text", RichLog)
        assert await wait_for(pilot, lambda: "+ /neu.txt" in log_text(log))
        t = log_text(log)
        assert "- /alt.txt" in t and "1 neu, 0 geändert, 1 gelöscht" in t
        await pilot.press("escape")
        pilot.app.query_one("#snaps", DataTable).move_cursor(row=2)                  # ältester: kein älterer zum Vergleichen
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "DiffScreen"


async def test_restore_test_screen_reports_ok(mgr, snaps):
    async with PbsTui(mgr).run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await load_snaps(pilot)
        await pilot.press("w")
        log = pilot.app.screen.query_one("#text", RichLog)
        assert await wait_for(pilot, lambda: "in Ordnung" in log_text(log), timeout=10)
        assert "a.txt" in log_text(log) and "FEHLER" not in log_text(log)
        assert mgr.last_restore_test(mgr.profiles()[0])["ok"] is True


async def test_find_in_all_snapshots_then_versions(mgr, snaps, tmp_path):
    async with PbsTui(mgr).run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        pilot.app.screen.query_one("#prompt-input", Input).value = "a.txt"
        await pilot.press("enter")
        await pilot.pause()
        hits = pilot.app.screen.query_one("#hits", DataTable)
        assert await wait_for(pilot, lambda: hits.row_count == 1)
        assert hits.get_row_at(0)[0] == "/a.txt" and hits.get_row_at(0)[5] == "ja"
        await pilot.press("enter")                                                  # Fassungen der Datei
        await pilot.pause()
        vers = pilot.app.screen.query_one("#versions", DataTable)
        assert await wait_for(pilot, lambda: vers.row_count == 2)
        assert [vers.get_row_at(i)[3] for i in range(2)] == ["2", "1"]
        vers.move_cursor(row=1)                                                     # die älteste Fassung ("eins")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        prev = pilot.app.screen.query_one("#text", RichLog)
        assert await wait_for(pilot, lambda: log_text(prev).strip() == "eins")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        pilot.app.screen.query_one("#prompt-input", Input).value = str(tmp_path / "raus")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: (tmp_path / "raus/a.txt").exists())
        assert (tmp_path / "raus/a.txt").read_text() == "eins"


async def test_files_screen_preview_and_versions(mgr, snaps):
    from pbsgui.tui.files import FilesScreen
    p = mgr.profiles()[0]
    async with PbsTui(mgr).run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        snap = mgr.snapshots(p)[0]
        pilot.app.push_screen(FilesScreen(mgr, p, snap))
        assert await wait_for(pilot, lambda: len(pilot.app.screen.query_one("#files-tree", Tree).root.children) == 1)
        scr = pilot.app.screen
        tree = scr.query_one("#files-tree", Tree)
        arch_node = tree.root.children[0]
        arch_node.expand()
        assert await wait_for(pilot, lambda: len(arch_node.children) == 2)
        tree.move_cursor(arch_node.children[0])                                    # a.txt
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert await wait_for(pilot, lambda: log_text(pilot.app.screen.query_one("#text", RichLog)).strip() == "zwei-lang")
        await pilot.press("escape")
        await pilot.pause()
        sessions = list(scr._sessions.values())
        assert sessions and sessions[0].root is not None
        await pilot.press("v")
        await pilot.pause()
        vers = pilot.app.screen.query_one("#versions", DataTable)
        assert await wait_for(pilot, lambda: vers.row_count == 2)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")                                               # Dateibrowser schließen: Einhängung beenden
        await pilot.pause()
        assert sessions[0].root is None


async def test_actions_need_snapshot(mgr, snaps):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        for key in ("x", "w"):
            await pilot.press(key)
            await pilot.pause()
            assert pilot.app.screen.__class__.__name__ not in ("DiffScreen", "RestoreTestScreen")


async def test_server_screen_changes_datastore(mgr, snaps):
    from textual.widgets import Input
    async with PbsTui(mgr).run_test(size=(150, 60)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        scr = pilot.app.screen
        scr.query_one("#srv-datastore", Input).value = "store9"
        scr._save()
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ != "ServerScreen")
        assert mgr.profiles()[0].datastore == "store9"
