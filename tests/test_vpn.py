"""VPN-Werkzeuge und Update der Rettungs-ISO (ohne echte Tunnel und ohne Netzwerk)."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from pbsgui.core import update as upd
from pbsgui.core import vpn


class FakeRun:
    def __init__(self, results=None):
        self.calls, self.envs, self.results = [], [], results or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv)); self.envs.append(kw.get("env"))
        for key, (rc, out) in self.results.items():
            if key in " ".join(argv):
                return subprocess.CompletedProcess(argv, rc, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


WG = "[Interface]\nPrivateKey = abc\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = def\nEndpoint = 1.2.3.4:51820\nAllowedIPs = 10.0.0.0/24\n"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.org 1194\n"


def test_find_configs_recognizes_wireguard_and_openvpn(tmp_path):
    (tmp_path / "usb").mkdir()
    (tmp_path / "usb/wg0.conf").write_text(WG)
    (tmp_path / "usb/client.ovpn").write_text(OVPN)
    (tmp_path / "usb/other.conf").write_text("nothing = here\n")
    (tmp_path / "usb/ovpn-in-conf.conf").write_text(OVPN)
    assert vpn.find_configs("wireguard", [str(tmp_path)]) == [str(tmp_path / "usb/wg0.conf")]
    assert vpn.find_configs("openvpn", [str(tmp_path)]) == [str(tmp_path / "usb/client.ovpn"), str(tmp_path / "usb/ovpn-in-conf.conf")]
    assert vpn.find_configs("netbird", [str(tmp_path)]) == []


@pytest.mark.parametrize("name,ok", [("wg0.conf", True), ("mein-vpn.conf", True), ("ein_sehr_langer_name_1234.conf", False),
                                     ("a b.conf", False), ("../x.conf", True)])
def test_interface_name_rules(name, ok):
    if ok:
        assert vpn.interface_name(name)
    else:
        with pytest.raises(vpn.VpnError):
            vpn.interface_name(name)


def test_wireguard_up_copies_config_with_0600_and_uses_name(tmp_path, monkeypatch):
    src = tmp_path / "wg0.conf"; src.write_text(WG)
    etc = tmp_path / "etc-wireguard"
    monkeypatch.setattr(vpn, "Path", lambda p="": etc if str(p) == "/etc/wireguard" else Path(p))
    run = FakeRun()
    r = vpn.wireguard_up(str(src), run)
    assert r.ok and run.calls == [["wg-quick", "up", "wg0"]]
    dest = etc / "wg0.conf"
    assert dest.read_text() == WG and stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_wireguard_down_all(monkeypatch):
    run = FakeRun({"wg show": (0, "wg0 office")})
    assert vpn.wireguard_down(None, run).ok
    assert ["wg-quick", "down", "wg0"] in run.calls and ["wg-quick", "down", "office"] in run.calls


def test_openvpn_password_goes_to_0600_file_not_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(vpn, "RUN_DIR", str(tmp_path / "run"))
    cfg = tmp_path / "client.ovpn"; cfg.write_text(OVPN)
    class Starting(FakeRun):
        def __call__(self, argv, **kw):
            (tmp_path / "run" / "openvpn.pid").write_text(str(os.getpid()))      # wie der echte Dienst
            return super().__call__(argv, **kw)
    run = Starting()
    r = vpn.openvpn_up(str(cfg), "alice", "s3cret!", run, settle=1, sleep=lambda s: None)
    assert r.ok, r.text
    argv = run.calls[0]
    assert "s3cret!" not in " ".join(argv) and "--auth-user-pass" in argv and "--daemon" in argv
    auth = Path(argv[argv.index("--auth-user-pass") + 1])
    assert auth.read_text() == "alice\ns3cret!\n" and stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert vpn.openvpn_down(FakeRun()).ok
    assert not auth.exists()          # Zugangsdaten werden beim Trennen gelöscht, auch wenn kein Tunnel lief


def test_netbird_setup_key_goes_via_environment_not_argv():
    run = FakeRun()
    r = vpn.netbird_up("SETUP-KEY-123", "https://netbird.example.org", run)
    assert r.ok
    up = next(c for c in run.calls if c[:2] == ["netbird", "up"])
    assert "SETUP-KEY-123" not in " ".join(up) and "--management-url" in up
    assert run.envs[run.calls.index(up)]["NB_SETUP_KEY"] == "SETUP-KEY-123"
    with pytest.raises(vpn.VpnError):
        vpn.netbird_up("k", "javascript:alert(1)", FakeRun())


def test_missing_tool_is_reported_not_crashing():
    def boom(*a, **k):
        raise FileNotFoundError
    assert vpn._run(["wg-quick", "up", "x"], boom).text.endswith("nicht installiert")


# -- Update -------------------------------------------------------------------------------------------------
def test_update_offline_does_nothing(monkeypatch):
    run = FakeRun({"curl": (6, "")})
    res = upd.update(run=run, wait=0, sleep=lambda s: None)
    assert res.state == "offline" and not any(c[0] == "apt-get" for c in run.calls)


def test_update_upgrades_only_installed_packages_and_flags_restart(monkeypatch):
    versions = {"pbstui": "0.8.3", "netbird": "0.50.0"}

    class R(FakeRun):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            if argv[0] == "dpkg-query":
                out = "".join(f"{p} {v} ii \n" for p, v in versions.items())
                return subprocess.CompletedProcess(argv, 0, out, "")
            if argv[:2] == ["apt-get", "install"]:
                versions["pbstui"] = "0.8.4"
            return subprocess.CompletedProcess(argv, 0, "", "")
    run = R()
    monkeypatch.setattr(upd.shutil, "which", lambda n: "/usr/bin/" + n)
    res = upd.update(run=run, wait=0, sleep=lambda s: None)
    install = next(c for c in run.calls if c[:2] == ["apt-get", "install"])
    assert "--only-upgrade" in install and "pbstui" in install and "netbird" in install and "openvpn" not in install
    assert res.state == "fertig" and res.restart_needed and "pbstui 0.8.3 -> 0.8.4" in res.text


def test_update_writes_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "STATE_FILE", str(tmp_path / "state"))
    run = FakeRun({"curl": (6, "")})
    upd.update(run=run, wait=0, write_state=True, sleep=lambda s: None)
    assert (tmp_path / "state").read_text().startswith("offline")


def test_cli_vpn_and_update_are_registered():
    from pbsgui.cli.main import build_parser
    ap = build_parser()
    assert ap.parse_args(["vpn", "status"]).action == "status"
    a = ap.parse_args(["vpn", "up", "wireguard", "/media/usb/wg0.conf"])
    assert a.kind == "wireguard" and a.file == "/media/usb/wg0.conf"
    assert ap.parse_args(["update", "--wait", "5"]).wait == 5


def test_openvpn_failure_shows_log_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(vpn, "RUN_DIR", str(tmp_path / "run"))
    cfg = tmp_path / "client.ovpn"; cfg.write_text(OVPN)

    class Fails(FakeRun):
        def __call__(self, argv, **kw):
            (tmp_path / "run" / "openvpn.log").write_text("Options error: You must define CA file (--ca)\n")
            return subprocess.CompletedProcess(argv, 1, "", "")
    r = vpn.openvpn_up(str(cfg), run=Fails(), settle=0, sleep=lambda s: None)
    assert not r.ok and "CA file" in r.text


def test_openvpn_that_dies_right_away_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(vpn, "RUN_DIR", str(tmp_path / "run"))
    cfg = tmp_path / "client.ovpn"; cfg.write_text(OVPN)
    r = vpn.openvpn_up(str(cfg), run=FakeRun(), settle=1, sleep=lambda s: None)      # startet "erfolgreich", legt aber keine PID an
    assert not r.ok and "sofort beendet" in r.text
