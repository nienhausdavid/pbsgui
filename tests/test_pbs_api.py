"""PBS-API-Client gegen den lokalen TLS-Testserver (siehe fake_pbs_api.py)."""
import pytest

from fake_pbs_api import EXISTS, Handler, LOG, pytestmark, server  # noqa: F401
from pbsgui.core.pbs_api import ApiError, PbsApi, TokenExistsError, fetch_fingerprint


def test_fingerprint_and_pinning(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    assert len(fp.split(":")) == 32
    api = PbsApi("127.0.0.1", server, fp)
    api.login("root@pam", "rootpw")
    with pytest.raises(ApiError, match="Fingerprint"):
        PbsApi("127.0.0.1", server, "00:11").login("root@pam", "rootpw")


def test_wrong_password(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    with pytest.raises(ApiError, match="authentication failure"):
        PbsApi("127.0.0.1", server, fp).login("root@pam", "falsch")


def test_provision_sets_user_and_token_acls(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    api = PbsApi("127.0.0.1", server, fp)
    api.login("root@pam", "rootpw")
    res = api.provision("s", "host", namespace="ns1")
    assert res.auth_id == "pbsgui-host@pbs!backup" and res.secret == "SECRET"
    acls = [f for m, p, f, h in LOG if m == "PUT" and p.endswith("/access/acl")]
    assert {a["auth-id"] for a in acls} == {"pbsgui-host@pbs", "pbsgui-host@pbs!backup"}
    assert all(a["role"] == "DatastoreBackup" and a["path"] == "/datastore/s/ns1" for a in acls)
    posts = [(p, h) for m, p, f, h in LOG if m != "GET" and "ticket" not in p]
    assert all(h.get("CSRFPreventionToken") == "csrf1" for _, h in posts)  # CSRF bei Schreibzugriffen


def test_token_permission_check(server):
    fp = fetch_fingerprint("127.0.0.1", server)
    api = PbsApi("127.0.0.1", server, fp)
    api.use_token("pbsgui-host@pbs!backup", "SECRET")
    assert api.check_backup_permission("s") is True
    assert LOG[-1][3]["Authorization"] == "PBSAPIToken=pbsgui-host@pbs!backup:SECRET"


def _login(server):
    api = PbsApi("127.0.0.1", server, fetch_fingerprint("127.0.0.1", server))
    api.login("root@pam", "rootpw")
    return api


def test_nested_namespace_created_level_by_level(server):
    _login(server).provision("s", "host", namespace="srv/laptop")
    ns = [f for m, p, f, h in LOG if m == "POST" and p.endswith("/namespace")]
    assert ns == [{"name": "srv"}, {"name": "laptop", "parent": "srv"}]
    acl = [f for m, p, f, h in LOG if m == "PUT"]
    assert all(a["path"] == "/datastore/s/srv/laptop" for a in acl)


def test_existing_user_and_namespace_are_reused(server):
    EXISTS.update({"/access/users", "/admin/datastore/s/namespace"})
    res = _login(server).provision("s", "host", namespace="srv")
    assert res.secret == "SECRET"
    assert any(m == "PUT" for m, *_ in LOG)  # ACLs wurden trotzdem gesetzt


def test_existing_token_requires_confirmation_then_replaced(server):
    token_path = "/access/users/pbsgui-host%40pbs/token/backup"
    EXISTS.add(token_path)
    api = _login(server)
    with pytest.raises(TokenExistsError, match="existiert bereits"):
        api.provision("s", "host")
    assert not any(m == "DELETE" for m, *_ in LOG)  # ohne Zustimmung wird nichts gelöscht
    assert api.provision("s", "host", replace_token=True).secret == "SECRET"
    assert [p for m, p, *_ in LOG if m == "DELETE"] == ["/api2/json" + token_path]


def test_wait_for_permission_polls_until_effective(server):
    api = PbsApi("127.0.0.1", server, fetch_fingerprint("127.0.0.1", server))
    api.use_token("a@pbs!t", "x")
    answers = iter([False, False, True])
    api.check_backup_permission = lambda ds, ns="": next(answers)
    sleeps = []
    assert api.wait_for_backup_permission("s", sleep=sleeps.append) is True
    assert sleeps == [2, 2]


def test_wait_for_permission_times_out(server):
    api = PbsApi("127.0.0.1", server, fetch_fingerprint("127.0.0.1", server))
    api.check_backup_permission = lambda ds, ns="": False
    assert api.wait_for_backup_permission("s", timeout=4, interval=2, sleep=lambda _s: None) is False


def test_deprovision_removes_acls_before_user(server):
    _login(server).deprovision("pbsgui-host@pbs", "s", "ns1")
    calls = [(m, p.removeprefix("/api2/json"), f.get("auth-id"), f.get("delete")) for m, p, f, h in LOG[1:]]
    assert calls[0][0] == "PUT" and calls[0][2] == "pbsgui-host@pbs!backup" and calls[0][3] == "1"
    assert calls[1][0] == "PUT" and calls[1][2] == "pbsgui-host@pbs"
    assert calls[2] == ("DELETE", "/access/users/pbsgui-host%40pbs", None, None)


def test_list_datastores_and_namespaces(server):
    api = _login(server)
    assert api.list_datastores() == ["store1", "store2"]
    assert api.list_namespaces("store1") == ["a", "a/b"]  # Wurzel ("") wird ausgelassen
