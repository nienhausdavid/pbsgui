import json

import pytest

from pbsgui.core.client import ClientError, PbsClient
from pbsgui.core.runner import read_status, run_profile
from pbsgui.core.secrets import FileStore


def calls(fake_env):
    return [json.loads(l) for l in (fake_env / "calls.jsonl").read_text().splitlines()]


def test_backup_and_list(fake_env, profile):
    c = PbsClient(profile, "geheim")
    lines = []
    assert c.backup(lines.append) == 0
    assert any("home-david.pxar" in l for l in lines)
    snaps = c.list_snapshots()
    assert len(snaps) == 1 and snaps[0].backup_id == "laptop"
    call = calls(fake_env)[0]
    assert "--exclude" in call["argv"] and "*.tmp" in call["argv"]
    # Geheimnis nur in der Umgebung, nie in den Argumenten
    assert call["env"]["PBS_PASSWORD"] == "geheim"
    assert all("geheim" not in a for c_ in calls(fake_env) for a in c_["argv"])


def test_namespace_and_encryption_args(fake_env, profile):
    profile.namespace, profile.encrypt, profile.keyfile = "ns1", True, "/k.json"
    args = PbsClient(profile, "x").backup_args()
    assert args[args.index("--ns") + 1] == "ns1"
    assert "--keyfile" in args and "encrypt" in args


def test_encrypt_without_key_fails(profile):
    profile.encrypt = True
    with pytest.raises(ClientError):
        PbsClient(profile, "x").backup_args()


def test_auth_error(fake_env, profile, monkeypatch):
    monkeypatch.setenv("FAKE_PBS_EXPECT_PASSWORD", "richtig")
    with pytest.raises(ClientError, match="authentication failed"):
        PbsClient(profile, "falsch").test_connection()


def test_missing_binary(profile):
    with pytest.raises(ClientError, match="nicht gefunden"):
        PbsClient(profile, "x", binary="/nicht/da").version()


def test_restore_guard(fake_env, profile, tmp_path):
    c = PbsClient(profile, "x")
    c.backup()
    snap = c.list_snapshots()[0]
    with pytest.raises(ClientError):
        c.restore(snap, "etc.pxar", "/")
    c.restore(snap, "etc.pxar", str(tmp_path / "out"))
    assert (tmp_path / "out/restored.txt").exists()


def test_run_profile_success_and_status(fake_env, profile, paths):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "geheim")
    out = []
    assert run_profile(profile, paths, store, out.append) == 0
    st = read_status(paths, profile)
    assert st["ok"] is True and out[-1] == "Backup erfolgreich"


def test_run_profile_uses_systemd_credential(fake_env, profile, paths, tmp_path, monkeypatch):
    cred = tmp_path / "creds"
    cred.mkdir()
    (cred / "token").write_text("aus-credential")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred))
    assert run_profile(profile, paths, None, lambda _l: None) == 0
    assert calls(fake_env)[0]["env"]["PBS_PASSWORD"] == "aus-credential"


def test_run_profile_failure(fake_env, profile, paths, monkeypatch):
    monkeypatch.setenv("FAKE_PBS_FAIL", "1")
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "x")
    assert run_profile(profile, paths, store, lambda _l: None) == 1
    assert read_status(paths, profile)["ok"] is False


def test_run_profile_no_secret(fake_env, profile, paths):
    assert run_profile(profile, paths, FileStore(paths.creds), lambda _l: None) == 2


def test_generate_key_and_encrypted_backup(fake_env, profile, paths):
    from pbsgui.core.keys import generate_key
    content, fp = generate_key()
    assert fp == "aa:bb:cc:dd" and "fingerprint" in content
    profile.encrypt = True
    c = PbsClient(profile, "x", key_content=content)
    assert c.backup() == 0
    call = calls(fake_env)[-1]
    assert call["keyfile"]["exists"] and call["keyfile"]["content"] == content
    assert call["keyfile"]["mode"] == "0o600"
    assert "--crypt-mode" in call["argv"]
    import os
    assert not os.path.exists(call["keyfile"]["path"])  # temporäre Datei wurde gelöscht


def test_restore_passes_keyfile(fake_env, profile, tmp_path):
    profile.encrypt = True
    c = PbsClient(profile, "x", key_content='{"fingerprint":"aa"}')
    c.backup()
    snap = c.list_snapshots()[0]
    c.restore(snap, "etc.pxar", str(tmp_path / "o"))
    assert calls(fake_env)[-1]["keyfile"]["exists"]


def test_run_profile_encrypted_uses_stored_key(fake_env, profile, paths):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    store.put(profile.slug, "key", '{"fingerprint":"aa"}')
    profile.encrypt = True
    assert run_profile(profile, paths, store, lambda _l: None) == 0
    assert calls(fake_env)[-1]["keyfile"]["content"] == '{"fingerprint":"aa"}'


def test_run_profile_encrypted_without_key_fails(fake_env, profile, paths):
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    profile.encrypt = True
    assert run_profile(profile, paths, store, lambda _l: None) == 3


def test_manager_enable_encryption_never_overwrites(fake_env, profile, paths):
    from pbsgui.core.manager import ProfileManager
    m = ProfileManager(paths, FileStore(paths.creds))
    assert m.enable_encryption(profile) is True
    first = m.secrets.get(profile.slug, "key")
    assert profile.key_fingerprint == "aa:bb:cc:dd" and profile.encrypt
    assert m.enable_encryption(profile) is False  # vorhandener Key bleibt erhalten
    assert m.secrets.get(profile.slug, "key") == first
    dest = paths.config / "export.json"
    m.export_key(profile, str(dest))
    assert dest.read_text() == first and oct(dest.stat().st_mode & 0o777) == "0o600"
