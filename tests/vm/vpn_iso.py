#!/usr/bin/env python3
"""Prüft die Rettungs-ISO in QEMU (Netzwerk über QEMU-User-Mode): VPN-Werkzeuge, Update beim Start, APT-Quellen.

Aufruf: python3 tests/vm/vpn_iso.py ISO [--workdir build/vm]
Ohne Server und ohne Zugangsdaten: WireGuard und OpenVPN werden mit Dummy-Konfigurationen (nicht erreichbare Gegenstellen)
aufgebaut, NetBird nur auf Dienst und Programm geprüft.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vmctl import VM  # noqa: E402

WG = """[Interface]
PrivateKey = YEdPyqkSiHBDjCRXOdnVR3mMLnfRTVGW4LuKLWHJ0F4=
Address = 10.99.0.2/32
[Peer]
PublicKey = 2xk3Yb1ExSyC1o1HT4yC3Fl2l0vYYbWGt9nBpbbYTSw=
Endpoint = 192.0.2.1:51820
AllowedIPs = 10.99.0.0/24
PersistentKeepalive = 25
"""
FP = ":".join(["AB"] * 32)      # gültiges Format; es gibt keine Gegenstelle, die geprüft würde
OVPN = f"client\ndev tun\nproto udp\nremote 192.0.2.1 1194\nnobind\nverb 3\npeer-fingerprint {FP}\n"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK ' if ok else 'FEHLER'}] {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iso"); ap.add_argument("--workdir", default="build/vm")
    ap.add_argument("--patch", action="append", default=[], metavar="DATEI", help="Quelldatei (unter src/) vor dem Test in die ISO kopieren, um eine neuere Fassung ohne ISO-Neubau zu prüfen")
    a = ap.parse_args()
    work = Path(a.workdir); work.mkdir(parents=True, exist_ok=True)
    disk = work / "vpn-test.img"
    disk.write_bytes(b""); import os; os.truncate(disk, 2 * 2**30)
    vm = VM("vpn-iso", work, [disk], iso=Path(a.iso), mem=2048)
    results: list[bool] = []
    try:
        vm.start()
        vm.expect(r"root@[\w-]+:[~/]\S*# ", 300)
        vm.send("\n"); vm.run("true", 30)
        vm.run("pkill -f '[p]bsgui rescue' || true; unset HISTFILE", 10)
        import base64 as _b64
        for rel in a.patch:
            data = _b64.b64encode((Path("src") / rel).read_bytes()).decode()
            dest = f"/usr/lib/python3/dist-packages/{rel.removeprefix('src/')}"
            vm.run(f": > /tmp/p.b64", 10)
            for i in range(0, len(data), 1500):
                vm.run(f"printf %s {data[i:i + 1500]} >> /tmp/p.b64", 20)
            vm.run(f"base64 -d /tmp/p.b64 > {dest}; find /usr/lib/python3/dist-packages/pbsgui -name '*.pyc' -delete", 20)
            print("Patch:", rel, flush=True)
        out, _ = vm.run("for t in wg-quick openvpn netbird; do command -v $t >/dev/null && echo $t=ja || echo $t=NEIN; done", 20)
        for t in ("wg-quick", "openvpn", "netbird"):
            results.append(check(f"{t} installiert", f"{t}=ja" in out, out))
        out, _ = vm.run("systemctl is-enabled pbsgui-update.timer; systemctl is-active pbsgui-update.timer", 20)
        results.append(check("pbsgui-update.timer aktiv", "enabled" in out and "active" in out, out))
        # Update beim Start: auf die Statusdatei warten (Netzwerk über QEMU-User-Mode)
        state = ""
        for _ in range(60):
            state, _ = vm.run("cat /run/pbsgui-update.state 2>/dev/null", 15)
            if state.strip():
                break
            time.sleep(5)
        print("Update-Status:", state.strip(), flush=True)
        results.append(check("Update lief (fertig)", re.search(r"^fertig", state, re.M) is not None, state))
        out, _ = vm.run("cat /etc/apt/sources.list.d/*.list 2>/dev/null | grep -E 'netbird|pbsgui|proxmox' | sort -u", 20)
        results.append(check("APT-Quellen für Updates vorhanden", all(k in out for k in ("netbird", "pbsgui", "proxmox")), out))
        out, _ = vm.run("pbsgui update --wait 10 2>&1 | tail -3", 300)
        results.append(check("pbsgui update läuft erneut sauber", "alles aktuell" in out or "aktualisiert" in out, out))
        # WireGuard mit Dummy-Gegenstelle
        import base64
        vm.run(f"mkdir -p /media/usb; echo {base64.b64encode(WG.encode()).decode()} | base64 -d > /media/usb/wg0.conf", 10)
        out, _ = vm.run("pbsgui vpn find wireguard 2>&1; pbsgui vpn up wireguard /media/usb/wg0.conf 2>&1 | tail -4; "
                        "ip -brief addr show wg0; pbsgui vpn status", 60)
        results.append(check("WireGuard-Konfiguration gefunden", "/media/usb/wg0.conf" in out, out))
        results.append(check("WireGuard-Tunnel aufgebaut (wg0 mit 10.99.0.2)", "10.99.0.2" in out and "aktiv: wg0" in out, out))
        out, _ = vm.run("pbsgui vpn down wireguard; ip -brief addr show wg0 2>&1 | head -1", 60)
        results.append(check("WireGuard-Tunnel getrennt", "10.99.0.2" not in out, out))
        # OpenVPN mit Dummy-Gegenstelle: der Dienst startet, Verbindung scheitert (erwartet)
        vm.run(f"echo {base64.b64encode(OVPN.encode()).decode()} | base64 -d > /media/usb/client.ovpn", 10)
        out, _ = vm.run("PBSGUI_VPN_PASSWORD=geheim pbsgui vpn up openvpn /media/usb/client.ovpn --user test 2>&1 | tail -2; "
                        "sleep 3; pbsgui vpn status | grep OpenVPN; ls -l /run/pbsgui-vpn/", 60)
        results.append(check("OpenVPN-Dienst gestartet (läuft weiter, Gegenstelle fehlt)", "OpenVPN    aktiv" in out, out))
        results.append(check("OpenVPN-Passwort nur in 0600-Datei", "-rw-------" in out and "auth.txt" in out, out))
        out, _ = vm.run("pbsgui vpn down openvpn; ls /run/pbsgui-vpn 2>&1 | head -2", 30)
        results.append(check("OpenVPN getrennt, Zugangsdaten gelöscht", "auth.txt" not in out, out))
        # NetBird: Dienst läuft, Status ohne Anmeldung
        out, _ = vm.run("systemctl start netbird; sleep 4; systemctl is-active netbird; netbird status 2>&1 | head -4", 60)
        results.append(check("NetBird-Dienst läuft", "active" in out.split("\n")[0] or "active" in out, out))
        vm.run("poweroff", 10)
    finally:
        vm.kill()
    ok = all(results)
    print(f"\n{sum(results)} von {len(results)} Prüfungen bestanden")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
