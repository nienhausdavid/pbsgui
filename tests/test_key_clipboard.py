"""Schlüssel und Notfall-Set in die Zwischenablage: Text, Export-Vermerk, Kommandozeile, GUI, TUI."""
import json
import os

import pytest

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core import clipboard
from pbsgui.core.clipboard import ClipboardError, copy_text
from pbsgui.core.recovery import needs_key_export
from test_cli import add_b, env, run  # noqa: F401


class Rec:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return type("R", (), {"returncode": self.rc})()


def test_copy_text_uses_first_available_tool_and_stdin():
    rec = Rec()
    tool = copy_text("geheim", run=rec, which=lambda n: "/usr/bin/" + n if n == "xclip" else None)
    assert tool == "xclip" and rec.calls[0][0] == ["xclip", "-selection", "clipboard"]
    assert rec.calls[0][1]["input"] == "geheim" and "geheim" not in rec.calls[0][0]          # nie als Argument


def test_copy_text_without_tool_or_failing_tool_raises():
    with pytest.raises(ClipboardError):
        copy_text("x", run=Rec(), which=lambda n: None)
    with pytest.raises(ClipboardError):
        copy_text("x", run=Rec(rc=1), which=lambda n: "/x")


@pytest.fixture(autouse=True)
def _no_pkexec(monkeypatch):
    """System-Profile würden beim Speichern pkexec (Passwortabfrage) starten: im Test ersatzlos überspringen."""
    from pbsgui.core.manager import ProfileManager
    monkeypatch.setattr(ProfileManager, "_admin", lambda self, request: None)


def make_full(env, capsys, server):
    from pbsgui.core.manager import ProfileManager
    from pbsgui.core.paths import Paths
    assert add_b(capsys, server)[0] == 0
    mgr = ProfileManager(Paths.for_scope("user"))
    p = mgr.profiles()[0]
    p.backup_mode, p.scope, p.sources = "full", "system", []
    mgr.profiles_store.save(p)
    return mgr, mgr.profiles()[0]


def test_kit_contains_sheet_and_key_and_marks_export(env, capsys, server):
    mgr, p = make_full(env, capsys, server)
    assert needs_key_export(p)
    kit = mgr.emergency_kit(p)
    key = json.loads(mgr.key_text(p))
    assert "NOTFALLBLATT" in kit or "EMERGENCY" in kit
    assert key["fingerprint"] in kit and '"data"' in kit and "ACHTUNG" in kit
    mgr.mark_key_exported(p)
    assert not needs_key_export(mgr.profiles()[0])


def test_cli_key_copy_marks_export_and_never_logs_the_key(env, capsys, server, monkeypatch):
    mgr, p = make_full(env, capsys, server)
    seen = []
    monkeypatch.setattr(clipboard, "copy_text", lambda text, **kw: seen.append(text) or "wl-copy")
    code, out, err = run(capsys, "key", "copy-kit", "Test")
    assert code == 0, err
    assert "wl-copy" in out and "data" not in out                                  # der Schlüssel steht nicht in der Ausgabe
    assert seen and json.loads(mgr.key_text(p))["data"] in seen[0]
    assert not needs_key_export(mgr.profiles()[0])
    seen.clear()
    p2 = mgr.profiles()[0]
    p2.key_exported_at = ""
    mgr.profiles_store.save(p2)
    assert run(capsys, "key", "copy-sheet", "Test")[0] == 0
    assert needs_key_export(mgr.profiles()[0]) and "data" not in seen[0]           # Notfallblatt allein sichert den Schlüssel nicht


def test_cli_key_copy_without_tool_fails_clearly(env, capsys, server, monkeypatch):
    make_full(env, capsys, server)
    monkeypatch.setattr(clipboard.shutil, "which", lambda n: None)
    code, _, err = run(capsys, "key", "copy", "Test")
    assert code == 1 and "Zwischenablage" in err


async def test_tui_buttons_copy_and_unlock_backup(env, capsys, server, monkeypatch):
    from textual.widgets import Button
    from pbsgui.core.manager import ProfileManager
    from pbsgui.core.paths import Paths
    from pbsgui.tui.app import PbsTui
    mgr, p = make_full(env, capsys, server)
    copied = []
    monkeypatch.setattr(clipboard, "copy_text", lambda text, **kw: copied.append(text) or "wl-copy")
    monkeypatch.setattr(PbsTui, "action_backup", PbsTui.action_backup)
    async with PbsTui(mgr).run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        app = pilot.app
        assert {b.id for b in app.query(Button)} >= {"btn-copy-key", "btn-copy-kit", "btn-copy-sheet", "btn-key-file"}
        app.query_one("#btn-copy-sheet", Button).press()
        await pilot.pause()
        assert copied and "data" not in copied[-1] and needs_key_export(mgr.profiles()[0])      # Notfallblatt allein: weiter gesperrt
        app.query_one("#btn-copy-kit", Button).press()
        await pilot.pause()
        assert json.loads(mgr.key_text(p))["data"] in copied[-1] and not needs_key_export(mgr.profiles()[0])


async def test_tui_backup_offers_copy_when_blocked(env, capsys, server, monkeypatch):
    from pbsgui.tui.app import PbsTui
    mgr, p = make_full(env, capsys, server)
    monkeypatch.setattr(clipboard, "copy_text", lambda text, **kw: "wl-copy")
    async with PbsTui(mgr).run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ == "ConfirmScreen"          # statt zu scheitern: Kopieren anbieten
        await pilot.press("escape")
        await pilot.pause()
        assert needs_key_export(mgr.profiles()[0])
