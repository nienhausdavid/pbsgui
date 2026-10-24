"""Master-Schlüssel: erzeugen, einbinden, Schlüssel aus dem Snapshot zurückholen (Fake-Client über ein Pseudo-Terminal)."""
import json
import os
import stat

import pytest

from fake_pbs_api import pytestmark, server  # noqa: F401
from pbsgui.core import masterkey
from pbsgui.core.masterkey import MasterKeyError, check_public_pem, pem_fingerprint, run_with_prompts
from pbsgui.core.models import Snapshot
from test_cli import add_b, env, run  # noqa: F401

FAKE_KEY = json.dumps({"kdf": None, "data": "ZmFrZQ==", "fingerprint": "aa:bb:cc:dd"})


@pytest.fixture(autouse=True)
def _no_pkexec(monkeypatch):
    from pbsgui.core.manager import ProfileManager
    monkeypatch.setattr(ProfileManager, "_admin", lambda self, request: None)


def mgr_for(env):
    from pbsgui.core.manager import ProfileManager
    from pbsgui.core.paths import Paths
    return ProfileManager(Paths.for_scope("user"))


def test_run_with_prompts_answers_in_order_and_hides_secrets(tmp_path):
    script = tmp_path / "ask.py"
    script.write_text("import sys\nfor n in ('Erstes Password: ', 'Zweites Password: '):\n"
                      "    sys.stdout.write(n); sys.stdout.flush(); v = sys.stdin.readline().strip()\n"
                      "    print('\\ngot', v)\nsys.exit(3)\n")
    code, out = run_with_prompts(["python3", str(script)], ["erstes-geheimnis", "zweites-geheimnis"])
    assert code == 3 and "got ***" in out and "geheimnis" not in out


def test_run_with_prompts_times_out(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(30)\n")
    with pytest.raises(MasterKeyError):
        run_with_prompts(["python3", str(script)], [], timeout=1)


def test_create_master_key_with_fake_client(fake_env):
    k = masterkey.create_master_key("passphrase-123", os.environ["PBSGUI_CLIENT"])
    assert k.public_pem.startswith("-----BEGIN PUBLIC KEY-----") and "ENCRYPTED PRIVATE KEY" in k.private_pem
    assert k.fingerprint == pem_fingerprint(k.public_pem) and k.fingerprint.count(":") == 31
    with pytest.raises(MasterKeyError, match="zu kurz"):
        masterkey.create_master_key("kurz", os.environ["PBSGUI_CLIENT"])


def test_public_pem_checks():
    with pytest.raises(MasterKeyError):
        check_public_pem("kein pem")
    with pytest.raises(MasterKeyError):
        check_public_pem("-----BEGIN PUBLIC KEY-----\n!!!nicht base64!!!\n-----END PUBLIC KEY-----")


def test_setup_applies_public_key_to_all_encrypted_profiles_and_confirms(env, capsys, server):
    assert add_b(capsys, server)[0] == 0
    assert add_b(capsys, server, name="Zweit", ns="a")[0] == 0
    mgr = mgr_for(env)
    p = next(x for x in mgr.profiles() if x.name == "Test")
    key = mgr.setup_master_key(p, "passphrase-123")
    profs = {x.name: x for x in mgr.profiles()}
    assert profs["Test"].master_fingerprint == profs["Zweit"].master_fingerprint == key.fingerprint
    assert os.path.exists(profs["Test"].master_pubkey) and profs["Test"].master_pubkey == profs["Zweit"].master_pubkey
    assert oct(os.stat(profs["Test"].master_pubkey).st_mode & 0o777) == "0o644"           # öffentlich lesbar (auch für root-Läufe)
    assert not profs["Test"].master_key_ack_at                                              # erst nach dem Sichern bestätigt
    mgr.confirm_master_key_backup(profs["Test"])
    assert all(x.master_key_ack_at for x in mgr.profiles())                                 # gleicher öffentlicher Schlüssel: gilt für alle
    assert "--master-pubkey-file" in mgr.client(mgr.profiles()[0]).backup_args(keyfile="/x")


def test_backup_uses_master_key_and_warns_without(env, capsys, server):
    assert add_b(capsys, server)[0] == 0
    code, out, _ = run(capsys, "dashboard", "Test", "--offline")
    assert "Kein Master-Schlüssel eingerichtet" in out
    code, out, _ = run(capsys, "key", "info", "Test")
    assert "Master-Schlüssel: nein" in out
    code, out, err = run(capsys, "recovery", "Test")
    assert code == 0


def test_cli_master_create_to_file_never_prints_the_private_key(env, capsys, server, monkeypatch, tmp_path):
    assert add_b(capsys, server)[0] == 0
    monkeypatch.setenv("PBSGUI_MASTER_PASSPHRASE", "passphrase-123")
    assert run(capsys, "key", "master-create", "Test")[0] == 1                               # ohne Ziel: Fehler
    dest = tmp_path / "master-private.pem"
    code, out, err = run(capsys, "key", "master-create", "Test", "--output", str(dest))
    assert code == 0, err
    assert "ENCRYPTED PRIVATE KEY" not in out and stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert "ENCRYPTED PRIVATE KEY" in dest.read_text()
    mgr = mgr_for(env)
    p = mgr.profiles()[0]
    assert p.master_pubkey and p.master_key_ack_at
    assert run(capsys, "key", "master-create", "Test", "--output", str(tmp_path / "x.pem"))[0] == 1       # schon eingerichtet
    code, out, _ = run(capsys, "key", "info", "Test")
    assert "Master-Schlüssel: ja" in out and p.master_fingerprint in out
    code, out, _ = run(capsys, "dashboard", "Test", "--offline")
    assert "Kein Master-Schlüssel" not in out


def test_cli_master_use_public_key_from_another_device(env, capsys, server, tmp_path):
    assert add_b(capsys, server)[0] == 0
    pem = tmp_path / "pub.pem"
    pem.write_text("-----BEGIN PUBLIC KEY-----\nQUJDREVGRw==\n-----END PUBLIC KEY-----\n")
    code, out, err = run(capsys, "key", "master-use", "Test", str(pem))
    assert code == 0 and "Fingerprint" in out, err
    assert mgr_for(env).profiles()[0].master_pubkey
    bad = tmp_path / "bad.pem"
    bad.write_text("nix")
    assert run(capsys, "key", "master-use", "Test", str(bad))[0] == 1


def test_recover_key_from_snapshot_with_private_master_key(env, capsys, server, monkeypatch, tmp_path):
    assert add_b(capsys, server)[0] == 0
    mgr = mgr_for(env)
    key = mgr.setup_master_key(mgr.profiles()[0], "passphrase-123")
    priv = tmp_path / "priv.pem"
    priv.write_text(key.private_pem)
    snap = {"backup-type": "host", "backup-id": "host", "backup-time": 1000, "size": 1,
            "files": [{"filename": "etc.pxar.didx"}, {"filename": "rsa-encrypted.key.blob"}]}
    plain = {"backup-type": "host", "backup-id": "host", "backup-time": 900, "size": 1, "files": [{"filename": "etc.pxar.didx"}]}
    (env / "fake").mkdir(parents=True, exist_ok=True)
    (env / "fake" / "snapshots.json").write_text(json.dumps([snap, plain]))
    monkeypatch.setenv("FAKE_PBS_RSA", FAKE_KEY)
    monkeypatch.setenv("FAKE_PBS_RSA_BLOBNAME", "1")
    monkeypatch.setenv("PBSGUI_MASTER_PASSPHRASE", "passphrase-123")
    out_file = tmp_path / "key-back.json"
    code, out, err = run(capsys, "key", "recover", "Test", "--master-key", str(priv), "--output", str(out_file))
    assert code == 0, err
    assert json.loads(out_file.read_text())["fingerprint"] == "aa:bb:cc:dd" and "ZmFrZQ" not in out
    monkeypatch.setenv("PBSGUI_MASTER_PASSPHRASE", "falsche-passphrase")
    code, _, err = run(capsys, "key", "recover", "Test", "--master-key", str(priv), "--output", str(tmp_path / "zwei.json"))
    assert code == 1 and "Passphrase" in err
    monkeypatch.setenv("PBSGUI_MASTER_PASSPHRASE", "passphrase-123")
    code, _, err = run(capsys, "key", "recover", "Test", "--master-key", str(priv), "--snapshot", "1", "--output", str(tmp_path / "drei.json"))
    assert code == 1 and "Schlüsselkopie" in err                                             # Snapshot ohne Kopie: klare Meldung


def test_install_recovered_key_replaces_missing_key_but_refuses_mismatch(env, capsys, server):
    assert add_b(capsys, server)[0] == 0
    mgr = mgr_for(env)
    p = mgr.profiles()[0]
    mgr.secrets.delete(p.slug)                                    # Gerät "verloren": Schlüssel weg
    mgr.install_recovered_key(p, FAKE_KEY)
    assert mgr.secrets.get(p.slug, "key") == FAKE_KEY and mgr.profiles()[0].key_fingerprint == "aa:bb:cc:dd"
    other = json.dumps({"kdf": None, "data": "eA==", "fingerprint": "11:22:33"})
    with pytest.raises(Exception, match="passt nicht"):
        mgr.install_recovered_key(mgr.profiles()[0], other)


def test_rescue_recover_key_without_profile(env, capsys, server, monkeypatch, tmp_path):
    """In der Rettungs-ISO gibt es kein Profil: Verbindungsdaten per Option, Schlüssel nur mit dem privaten Master-Schlüssel."""
    from pbsgui.cli.main import main
    from pbsgui.core.pbs_api import fetch_fingerprint
    assert add_b(capsys, server)[0] == 0
    mgr = mgr_for(env)
    key = mgr.setup_master_key(mgr.profiles()[0], "passphrase-123")
    priv = tmp_path / "priv.pem"
    priv.write_text(key.private_pem)
    snap = {"backup-type": "host", "backup-id": "host", "backup-time": 1000, "size": 1,
            "files": [{"filename": "root.pxar.didx"}, {"filename": "rsa-encrypted.key.blob"}]}
    (env / "fake").mkdir(parents=True, exist_ok=True)
    (env / "fake" / "snapshots.json").write_text(json.dumps([snap]))
    monkeypatch.setenv("FAKE_PBS_RSA", FAKE_KEY)
    monkeypatch.setenv("FAKE_PBS_RSA_BLOBNAME", "1")
    monkeypatch.setenv("PBSGUI_MASTER_PASSPHRASE", "passphrase-123")
    monkeypatch.setenv("PBSGUI_TOKEN_SECRET", "t")
    out = tmp_path / "key.json"
    code = main(["rescue", "recover-key", "--server", "127.0.0.1", "--port", str(server), "--datastore", "store1", "--auth-id", "u@pbs!t",
                 "--fingerprint", fetch_fingerprint("127.0.0.1", server), "--master-key", str(priv), "--output", str(out)])
    assert code == 0 and json.loads(out.read_text())["fingerprint"] == "aa:bb:cc:dd" and stat.S_IMODE(out.stat().st_mode) == 0o600
    assert main(["rescue", "recover-key", "--server", "127.0.0.1", "--port", str(server), "--datastore", "store1", "--auth-id", "u@pbs!t",
                 "--fingerprint", "AA", "--master-key", str(priv), "--output", str(out)]) == 1                     # Ziel existiert schon
