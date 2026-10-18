"""TUI-Tests mit Textual-Pilot (Fake-Client und lokaler TLS-Testserver)."""
import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, RichLog, Static, TabbedContent

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core import manager as manager_mod
from pbsgui.core.manager import ProfileManager
from pbsgui.core.pbs_api import fetch_fingerprint
from pbsgui.core.secrets import FileStore
from pbsgui.tui.app import PbsTui


async def wait_for(pilot, cond, timeout=6.0):
    waited = 0.0
    while waited < timeout:
        if cond():
            return True
        await pilot.pause(0.05)
        waited += 0.05
    return False


@pytest.fixture
def mgr(fake_env, tmp_path, monkeypatch, profile):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-s"))
    monkeypatch.setattr(manager_mod, "best_store", lambda scope, d: FileStore(d))
    m = ProfileManager()
    profile.sources = ["/etc"]
    m.save(profile, token="geheim")
    return m


def text(widget: Static) -> str:
    return str(widget.render())


async def test_overview_shows_profile_and_warns_when_unencrypted(mgr):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        ov = text(pilot.app.query_one("#overview", Static))
        assert "Laptop" in ov and "pbs.example:8007" in ov and "NEIN" in ov  # Fixture ist unverschlüsselt


async def test_backup_snapshots_restore(mgr, tmp_path):
    async with PbsTui(mgr).run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        log = pilot.app.query_one("#log", RichLog)
        assert await wait_for(pilot, lambda: not pilot.app._busy and len(log.lines) >= 3)
        assert any("Backup erfolgreich" in str(l) for l in log.lines)
        await pilot.press("r")
        table = pilot.app.query_one("#snaps", DataTable)
        assert await wait_for(pilot, lambda: table.row_count == 1)
        await pilot.press("t")
        await pilot.pause()
        target = tmp_path / "restored"
        pilot.app.screen.query_one("#prompt-input", Input).value = str(target)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: (target / "restored.txt").exists())


async def test_restore_without_snapshots_is_refused(mgr):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "PromptScreen"


async def test_enable_encryption_creates_key_and_export(mgr, tmp_path):
    async with PbsTui(mgr).run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("v")  # Verschlüsselung einschalten -> Key wird automatisch erzeugt
        await pilot.pause()
        assert mgr.secrets.get("laptop", "key")
        assert "ja" in text(pilot.app.query_one("#overview", Static))
        await pilot.press("k")
        await pilot.pause()
        dest = tmp_path / "export.json"
        pilot.app.screen.query_one("#prompt-input", Input).value = str(dest)
        await pilot.press("enter")
        await pilot.pause()
        assert dest.read_text() == mgr.secrets.get("laptop", "key")
        assert oct(dest.stat().st_mode & 0o777) == "0o600"


async def test_disable_encryption_needs_confirmation(mgr):
    p = mgr.profiles()[0]
    p.encrypt = True
    mgr.enable_encryption(p)
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        pilot.app.screen.query_one("#no", Button).press()
        await pilot.pause()
        assert mgr.profiles()[0].encrypt is True
        await pilot.press("v")
        await pilot.pause()
        pilot.app.screen.query_one("#yes", Button).press()
        await pilot.pause()
        assert mgr.profiles()[0].encrypt is False


async def test_edit_sources(mgr):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        pilot.app.screen.query_one("#prompt-input", Input).value = "/home/x, /var/lib"
        await pilot.press("enter")
        await pilot.pause()
        assert mgr.profiles()[0].sources == ["/home/x", "/var/lib"]


async def test_delete_profile(mgr):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        pilot.app.screen.query_one("#yes", Button).press()
        await pilot.pause()
        assert mgr.profiles() == []
        assert "Keine Profile" in text(pilot.app.query_one("#overview", Static))


async def test_add_server_weg_b_with_namespace_check(mgr, server):
    fp = fetch_fingerprint("127.0.0.1", server)
    async with PbsTui(mgr).run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        s = pilot.app.screen
        s.query_one("#name", Input).value = "NeuerServer"
        s.query_one("#server", Input).value = "127.0.0.1"
        s.query_one("#port", Input).value = str(server)
        s.query_one("#fp-btn", Button).press()
        assert await wait_for(pilot, lambda: text(s.query_one("#fp", Static)).strip() == fp)
        s.query_one("#fp-ok", Checkbox).value = True
        s.query_one("#mode").query("RadioButton").last().value = True  # Weg B
        await pilot.pause()
        s.query_one("#token-id", Input).value = "u@pbs!t"
        s.query_one("#token-secret", Input).value = "geheim"
        s.query_one("#check", Button).press()
        assert await wait_for(pilot, lambda: "store1" in text(s.query_one("#stores", Static)))
        assert s.query_one("#datastore", Input).value == "store1"
        assert await wait_for(pilot, lambda: s._namespaces is not None)
        s.query_one("#namespace", Input).value = "gibtsnicht"
        await pilot.pause()
        assert "nicht anlegbar" in text(s.query_one("#ns-info", Static))
        s.query_one("#setup", Button).press()
        await pilot.pause()
        assert "nicht anlegen" in text(s.query_one("#status", Static))
        s.query_one("#namespace", Input).value = "a/b"
        await pilot.pause()
        assert "existiert." in text(s.query_one("#ns-info", Static))
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        prof = next(p for p in mgr.profiles() if p.name == "NeuerServer")
        assert prof.namespace == "a/b" and prof.encrypt and mgr.secrets.get(prof.slug, "key")  # Key automatisch


async def test_add_server_requires_fingerprint_confirmation(mgr):
    async with PbsTui(mgr).run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        s = pilot.app.screen
        s.query_one("#server", Input).value = "127.0.0.1"
        s.query_one("#check", Button).press()
        await pilot.pause()
        assert "Fingerprint" in text(s.query_one("#status", Static))


async def test_brackets_in_data_do_not_break_rendering(mgr):
    """Regression: Textual 2.x wertete [..] in Daten (Namen, Pfade) als Markup aus und stürzte ab."""
    p = mgr.profiles()[0]
    p.name, p.sources = "A [b, -] [/x]", ["/pfad/[test]"]
    mgr.save(p)
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        names = [t for t in (text(pilot.app.query_one("#overview", Static)),)]
        assert "A [b, -] [/x]" in names[0] and "/pfad/[test]" in names[0]


async def test_overview_shows_scope_and_full_locks_sources(mgr):
    p = mgr.profiles()[0]
    p.backup_mode, p.scope, p.sources = "full", "user", []
    mgr.profiles_store.save(p)  # System-Kopie per pkexec wird hier bewusst umgangen
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        ov = text(pilot.app.query_one("#overview", Static))
        assert "Vollständiges System" in ov and "Quellen:       /" in ov
        await pilot.press("o")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "PromptScreen"  # Quellen sind beim Vollbackup fest


async def test_add_screen_full_option_disabled_without_admin(mgr, monkeypatch):
    monkeypatch.setattr("pbsgui.tui.screens.can_use_admin", lambda: False)
    async with PbsTui(mgr).run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert pilot.app.screen.query_one("#scope-full").disabled is True
