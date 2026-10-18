from pbsgui.cli.admin import apply
from pbsgui.core.manager import ProfileManager
from pbsgui.core.paths import Paths
from pbsgui.core.secrets import FileStore
from pbsgui.core.store import ProfileStore
from test_scheduler_secrets import FakeRun


def test_manager_save_user_scope_no_schedule(profile, paths):
    m = ProfileManager(paths, FileStore(paths.creds))
    m.save(profile, token="tok", keypass="kp")
    assert [p.name for p in m.profiles()] == ["Laptop"]
    assert m.secrets.get("laptop", "token") == "tok"
    assert not (paths.units).exists()


def test_admin_apply_writes_system_copy(profile, tmp_path):
    sp = Paths("system", tmp_path / "etc/pbsgui", tmp_path / "units", tmp_path / "etc/pbsgui/creds", tmp_path / "var")
    profile.scope, profile.schedule = "system", "daily"
    store = FileStore(sp.creds)
    run = FakeRun()
    apply({"profile": profile.to_dict(), "secrets": {"token": "tok", "keypass": None}, "schedule": "install"},
          paths=sp, store=store, run=run)
    assert ProfileStore(sp.profiles).load("Laptop").scope == "system"
    assert store.get("laptop", "token") == "tok"
    assert ["systemctl", "enable", "--now", "pbsgui-laptop.timer"] in run.calls
    apply({"profile": profile.to_dict(), "delete": True}, paths=sp, store=store, run=run)
    assert store.get("laptop", "token") is None and not (sp.units / "pbsgui-laptop.timer").exists()
