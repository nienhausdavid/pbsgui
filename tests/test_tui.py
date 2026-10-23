"""TUI-Tests mit Textual-Pilot (Fake-Client und lokaler TLS-Testserver)."""
import shutil
import subprocess

import pytest
from textual.widgets import (Button, Checkbox, DataTable, Input, OptionList, RichLog, Select, SelectionList, Static,
                             TabbedContent)

from fake_docker_env import dock  # noqa: F401
from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core import manager as manager_mod
from pbsgui.core import scheduler as scheduler_mod
from pbsgui.core.manager import ProfileManager
from pbsgui.core.pbs_api import fetch_fingerprint
from pbsgui.core.secrets import FileStore
from pbsgui.tui.app import PbsTui


async def wait_for(pilot, cond, timeout=6.0):
    waited = 0.0
    while waited < timeout:
        try:
            if cond():
                return True
        except Exception:  # Screen aktiv, aber Widgets noch nicht aufgebaut
            pass
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


async def test_overview_and_edit_mounts_in_full_mode(mgr, fake_mounts, monkeypatch):
    pushed = []
    monkeypatch.setattr(manager_mod.ProfileManager, "_admin", lambda self, req: pushed.append(req))  # statt pkexec
    p = mgr.profiles()[0]
    p.backup_mode, p.scope, p.sources = "full", "system", []
    mgr.profiles_store.save(p)
    async with PbsTui(mgr).run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        assert "Laufwerke:     /boot, /data" in text(pilot.app.query_one("#overview", Static))
        await pilot.press("u")
        await pilot.pause()
        sel = pilot.app.screen.query_one("#mounts", SelectionList)
        assert sorted(sel.selected) == ["/boot", "/data"]
        assert len(sel.options) == 3  # /boot, /data, USB; das Bind-Duplikat wird nicht angeboten
        sel.deselect("/boot")
        sel.select("/media/david/STICK")  # USB nur auf ausdrückliche Wahl
        pilot.app.screen.query_one("#ok", Button).press()
        await pilot.pause()
        assert mgr.profiles()[0].full_mounts is not None
        assert sorted(mgr.profiles()[0].full_mounts) == ["/data", "/media/david/STICK"]
        assert pushed and sorted(pushed[-1]["profile"]["full_mounts"]) == ["/data", "/media/david/STICK"]  # System-Kopie


async def test_mounts_action_refused_for_home_profile(mgr):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "MountsScreen"


async def test_add_screen_shows_drive_list_only_for_full(mgr, monkeypatch, fake_mounts):
    monkeypatch.setattr("pbsgui.tui.screens.can_use_admin", lambda: True)
    async with PbsTui(mgr).run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        s = pilot.app.screen
        assert s.query_one("#mounts").display is False
        s.query_one("#scope").query("RadioButton").last().value = True
        await pilot.pause()
        assert s.query_one("#mounts").display is True
        assert sorted(s.query_one("#mounts", SelectionList).selected) == ["/boot", "/data"]


# -- Dateibrowser ---------------------------------------------------------------------------------
async def test_files_screen_browse_mark_and_restore_to_folder(mgr, tmp_path, monkeypatch):
    from pbsgui.core.models import Snapshot
    from pbsgui.tui.files import FilesScreen
    from textual.widgets import Tree
    p = mgr.profiles()[0]
    p.sources = [str(tmp_path / "heim")]
    mgr.profiles_store.save(p)
    name = p.archive_specs()[0].split(":")[0]
    arch = tmp_path / "pxar" / name
    (arch / "Dokumente").mkdir(parents=True)
    (arch / "Dokumente/a.txt").write_text("AUS BACKUP")
    (arch / "notiz.txt").write_text("N")
    monkeypatch.setenv("FAKE_PBS_TREE", str(tmp_path / "pxar"))
    monkeypatch.setenv("PBSGUI_FUSERMOUNT", "true")
    async with PbsTui(mgr).run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        pilot.app.push_screen(FilesScreen(mgr, mgr.profiles()[0], Snapshot("host", "pc", 1000, files=[name])))
        assert await wait_for(pilot, lambda: len(pilot.app.screen.query_one("#files-tree", Tree).root.children) == 1)
        scr = pilot.app.screen
        tree = scr.query_one("#files-tree", Tree)
        arch_node = tree.root.children[0]
        arch_node.expand()
        assert await wait_for(pilot, lambda: len(arch_node.children) == 2)
        assert [str(c.label) for c in arch_node.children] == ["[ ] Dokumente/", "[ ] notiz.txt  (1 B)"]
        tree.move_cursor(arch_node.children[0])
        await pilot.pause()
        await pilot.press("space")                                              # Ordner markieren
        await pilot.pause()
        assert str(arch_node.children[0].label) == "[x] Dokumente/" and scr.selected() == (name, ["/Dokumente"])
        await pilot.press("r")
        await pilot.pause()
        pilot.app.screen.query_one("#prompt-input", Input).value = str(tmp_path / "raus")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: (tmp_path / "raus/Dokumente/a.txt").exists())
        assert (tmp_path / "raus/Dokumente/a.txt").read_text() == "AUS BACKUP"


async def test_files_action_needs_a_loaded_snapshot(mgr):
    async with PbsTui(mgr).run_test() as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ != "FilesScreen"


# -- Server hinzufügen: Dropdowns, Zeitplan, Datenbank-Dumps ----------------------------------------------------------
class CtlFake:
    """systemctl wird nur mitgeschrieben; systemd-analyze läuft echt (Ausdrücke wirklich prüfen)."""
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "systemd-analyze":
            return subprocess.run(cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, "ActiveState=active\n", "")


needs_analyze = pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze fehlt")


def values(select: Select) -> list:
    return [v for _label, v in select._options if isinstance(v, str)]


async def connect(pilot, server, name="NeuerServer", weg_b=True):
    """Dialog öffnen, Fingerprint bestätigen, Verbindung prüfen und warten, bis Datastores/Namespaces da sind."""
    fp = fetch_fingerprint("127.0.0.1", server)
    await pilot.press("a")
    await pilot.pause()
    s = pilot.app.screen
    s.query_one("#name", Input).value = name
    s.query_one("#server", Input).value = "127.0.0.1"
    s.query_one("#port", Input).value = str(server)
    s.query_one("#fp-btn", Button).press()
    assert await wait_for(pilot, lambda: text(s.query_one("#fp", Static)).strip() == fp)
    s.query_one("#fp-ok", Checkbox).value = True
    if weg_b:
        s.query_one("#mode").query("RadioButton").last().value = True
        await pilot.pause()
        s.query_one("#token-id", Input).value = "u@pbs!t"
        s.query_one("#token-secret", Input).value = "geheim"
    else:
        s.query_one("#root-pw", Input).value = "rootpw"
    s.query_one("#check", Button).press()
    assert await wait_for(pilot, lambda: s._namespaces is not None)
    await pilot.pause()
    return s


async def test_datastore_and_namespace_dropdowns(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        ds, ns = s.query_one("#datastore-select", Select), s.query_one("#namespace-select", Select)
        assert ds.display and values(ds) == ["store1", "store2"] and ds.value == "store1"
        assert ns.display and values(ns) == ["", "a", "a/b"]            # Weg B: kein „Neu anlegen“
        ns.value = "a/b"                                                 # Auswahl füllt das Eingabefeld
        await pilot.pause()
        assert s.query_one("#namespace", Input).value == "a/b" and "existiert." in text(s.query_one("#ns-info", Static))
        s.query_one("#namespace", Input).value = "a"                     # Tippen führt die Auswahl nach
        await pilot.pause()
        assert ns.value == "a"
        s.query_one("#namespace", Input).value = "gibtsnicht"           # unbekannt: nichts gewählt
        await pilot.pause()
        assert not isinstance(ns.value, str)
        ns.value = ""                                                    # Wurzel
        await pilot.pause()
        assert s.query_one("#namespace", Input).value == ""
        ds.value = "store2"
        await pilot.pause()
        assert s.query_one("#datastore", Input).value == "store2"


async def test_namespace_dropdown_offers_new_only_with_weg_a(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server, weg_b=False)
        ns = s.query_one("#namespace-select", Select)
        assert values(ns) == ["", "a", "a/b", "__new__"]
        ns.value = "__new__"                                             # leert das Feld zum Eintippen
        await pilot.pause()
        assert s.query_one("#namespace", Input).value == ""
        s.query_one("#namespace", Input).value = "neu/ns"
        await pilot.pause()
        assert ns.value == "__new__" and "neu angelegt" in text(s.query_one("#ns-info", Static))


async def test_dropdown_hidden_until_connected(mgr):
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        s = pilot.app.screen
        assert s.query_one("#datastore-select").display is False and s.query_one("#namespace-select").display is False
        assert s.query_one("#datastore", Input).display  # Handeingabe bleibt als Rückfall


@needs_analyze
async def test_create_with_schedule_preset_installs_timer(mgr, server, monkeypatch):
    fake = CtlFake()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        sched = s.query_one("#schedule-select", Select)
        assert sched.value == ""
        sched.value = "00/8:00"
        await pilot.pause()
        assert "Nächster Lauf" in text(s.query_one("#schedule-info", Static))
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        prof = next(p for p in mgr.profiles() if p.name == "NeuerServer")
        assert prof.schedule == "00/8:00"
        assert await wait_for(pilot, lambda: ["systemctl", "--user", "enable", "--now", "pbsgui-neuerserver.timer"]
                              in fake.calls)
        assert "00/8:00" in text(pilot.app.query_one("#overview", Static))


@needs_analyze
async def test_custom_schedule_is_validated_before_anything_is_created(mgr, server, monkeypatch):
    fake = CtlFake()
    monkeypatch.setattr(scheduler_mod, "_default_run", fake)
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        s.query_one("#schedule-select", Select).value = "__custom__"
        await pilot.pause()
        custom = s.query_one("#schedule-custom", Input)
        assert custom.display
        s.query_one("#setup", Button).press()                            # leer
        await pilot.pause()
        assert "Eigenen Zeitplan" in text(s.query_one("#status", Static))
        custom.value = "quatsch"
        s.query_one("#setup", Button).press()
        await pilot.pause()
        assert "ungültig" in text(s.query_one("#status", Static))
        assert not any(p.name == "NeuerServer" for p in mgr.profiles())  # nichts halb angelegt
        custom.value = "Mon..Fri *-*-* 03:00:00"
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        assert next(p for p in mgr.profiles() if p.name == "NeuerServer").schedule == "Mon..Fri *-*-* 03:00:00"


async def test_create_without_schedule_leaves_none(mgr, server):
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: any(p.name == "NeuerServer" for p in mgr.profiles()))
        prof = next(p for p in mgr.profiles() if p.name == "NeuerServer")
        assert prof.schedule == "" and prof.db_dump is False


async def test_create_with_db_dump_opens_db_screen_and_sets_up_mariadb_user(mgr, server, dock):
    async with PbsTui(mgr).run_test(size=(140, 80)) as pilot:
        await pilot.pause()
        s = await connect(pilot, server)
        s.query_one("#db-dump", Checkbox).value = True
        s.query_one("#setup", Button).press()
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "DbDumpScreen")
        prof = next(p for p in mgr.profiles() if p.name == "NeuerServer")
        assert prof.db_dump is True
        scr = pilot.app.screen
        lst = scr.query_one("#db-list", OptionList)
        assert await wait_for(pilot, lambda: lst.option_count == 3)
        labels = [str(lst.get_option_at_index(i).prompt) for i in range(3)]
        assert any("kein Zugang nötig" in l for l in labels) and any("Zugang FEHLT" in l for l in labels)
        maria = next(i for i, l in enumerate(labels) if "firefly_db" in l)
        lst.highlighted = maria
        await pilot.pause()
        assert scr.query_one("#cred", Button).disabled is False
        scr.query_one("#cred", Button).press()
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "DbCredScreen")
        cred = pilot.app.screen
        cred.query_one("#cred-create", Checkbox).value = True
        cred.query_one("#admin-pw", Input).value = "adminpw"
        cred.query_one("#save", Button).press()
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "DbDumpScreen"
                              and "Zugang: pbsgui_dump" in str(pilot.app.screen.query_one(
                                  "#db-list", OptionList).get_option_at_index(maria).prompt))
        saved = mgr.db_credentials(prof)
        assert [k.split("/", 1)[1] for k in saved] == ["firefly_db"] and saved[next(iter(saved))]["user"] == "pbsgui_dump"
        assert "adminpw" not in (mgr.secrets.get(prof.slug, "dbdump") or "")


async def test_db_screen_wrong_credentials_keep_dialog_open(mgr, dock):
    prof = mgr.profiles()[0]
    async with PbsTui(mgr).run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert await wait_for(pilot, lambda: pilot.app.screen.query_one("#db-list", OptionList).option_count == 3)
        scr = pilot.app.screen
        lst = scr.query_one("#db-list", OptionList)
        lst.highlighted = next(i for i in range(3) if "firefly_db" in str(lst.get_option_at_index(i).prompt))
        await pilot.pause()
        scr.query_one("#cred", Button).press()
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "DbCredScreen")
        cred = pilot.app.screen
        cred.query_one("#cred-user", Input).value = "nobody"
        cred.query_one("#cred-pw", Input).value = "falsch"
        cred.query_one("#save", Button).press()
        assert await wait_for(pilot, lambda: "Fehler" in text(cred.query_one("#cred-status", Static)))
        assert pilot.app.screen is cred and mgr.db_credentials(prof) == {}
        assert cred.query_one("#cred-pw", Input).value == ""            # Passwort nicht im Widget behalten


async def test_db_screen_toggle_saves_flag_and_overview_shows_it(mgr, dock):
    async with PbsTui(mgr).run_test(size=(140, 60)) as pilot:
        await pilot.pause()
        assert "Datenbank-Dumps: aus" in text(pilot.app.query_one("#overview", Static))
        await pilot.press("p")
        assert await wait_for(pilot, lambda: pilot.app.screen.__class__.__name__ == "DbDumpScreen")
        pilot.app.screen.query_one("#dump-on", Checkbox).value = True
        pilot.app.screen.query_one("#ok", Button).press()
        await pilot.pause()
        assert mgr.profiles()[0].db_dump is True
        assert "Datenbank-Dumps: an" in text(pilot.app.query_one("#overview", Static))


async def test_db_screen_passwordless_setup_button(mgr, dock):
    prof = mgr.profiles()[0]
    async with PbsTui(mgr).run_test(size=(140, 70)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        assert await wait_for(pilot, lambda: pilot.app.screen.query_one("#db-list", OptionList).option_count == 3)
        scr = pilot.app.screen
        lst = scr.query_one("#db-list", OptionList)
        maria = next(i for i in range(3) if "firefly_db" in str(lst.get_option_at_index(i).prompt))
        pg = next(i for i in range(3) if "kein Zugang nötig" in str(lst.get_option_at_index(i).prompt))
        lst.highlighted = pg
        await pilot.pause()
        assert scr.query_one("#socket", Button).disabled is True                    # Postgres braucht das nicht
        lst.highlighted = maria
        await pilot.pause()
        assert scr.query_one("#socket", Button).disabled is False
        scr.query_one("#socket", Button).press()
        await pilot.pause()
        assert pilot.app.screen.__class__.__name__ == "ConfirmScreen"
        pilot.app.screen.query_one("#no", Button).press()                           # Nein: nichts wird gestoppt
        await pilot.pause()
        assert dock.data()["events"] == []
        scr.query_one("#socket", Button).press()
        await pilot.pause()
        pilot.app.screen.query_one("#yes", Button).press()
        assert await wait_for(pilot, lambda: "ohne Passwort (Socket)" in str(
            scr.query_one("#db-list", OptionList).get_option_at_index(maria).prompt))
        assert dock.data()["events"][0] == "stop firefly_db" and dock.data()["events"][-1] == "start firefly_db"
        assert mgr.db_credentials(prof) == {}                                       # es wurde nichts gespeichert
        lst.highlighted = maria
        await pilot.pause()
        assert scr.query_one("#socket", Button).disabled is True                    # schon eingerichtet
