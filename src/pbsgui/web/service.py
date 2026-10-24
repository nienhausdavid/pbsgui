"""Ablauf der Wiederherstellung für die Web-Oberfläche (ohne HTTP): Verbindung, Snapshot, Platte, Plan, Ausführung, VPN.

Nutzt dieselbe Logik wie der Assistent im Terminal (pbsgui.core.rescue). Geheimnisse (Token, Passphrasen) bleiben im
Speicher dieses Objekts und werden nach der Wiederherstellung gelöscht.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..core import rescue, vpn
from ..core.pbs_api import ApiError, fetch_fingerprint
from ..core.rescue import RescueConfig, RescueError

MAX_PASTE = 64 * 1024
_HOST = re.compile(r"^[A-Za-z0-9.:\-\[\]]{1,253}$")
_NAME = re.compile(r"^[A-Za-z0-9_./\-]{0,128}$")
_AUTH = re.compile(r"^[A-Za-z0-9_.@!\-]{1,128}$")
_FP = re.compile(r"^[0-9A-Fa-f:]{0,95}$")


class ServiceError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _s(d: dict, key: str, maxlen: int = 256) -> str:
    v = d.get(key, "")
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > maxlen:
        raise ServiceError(f"Ungültiger Wert für {key}")
    return v.strip()


@dataclass
class RunState:
    state: str = "idle"          # idle | running | ok | failed
    lines: list[str] = None      # type: ignore[assignment]
    dry_run: bool = False

    def __post_init__(self):
        self.lines = self.lines or []


class RescueService:
    def __init__(self, workdir: Path | str | None = None, *, run=subprocess.run, popen=subprocess.Popen):
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="pbsgui-web-"))
        self.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.workdir, 0o700)
        self.run_cmd, self.popen = run, popen
        self.cfg: RescueConfig | None = None
        self.snaps: list = []
        self.snap = None
        self.rec = None
        self.plan = None
        self.target = ""
        self.run = RunState()
        self._lock = threading.Lock()
        self.keyfile = ""
        self.on_finished = None      # Rückruf nach der Wiederherstellung (Web-Oberfläche kann sich dann abschalten)

    # -- Allgemein ----------------------------------------------------------------------------------------
    def status(self) -> dict:
        from .. import __version__
        return {"version": __version__, "root": os.geteuid() == 0, "uefi": Path("/sys/firmware/efi").exists(),
                "tools": vpn.tools_available(), "vpn": vpn.status(self.run_cmd), "connected": bool(self.cfg and self.snaps),
                "has_key": bool(self.keyfile), "run": self.run.state, "snapshot": getattr(self.snap, "path", "")}

    def fingerprint(self, d: dict) -> dict:
        host = self._host(d)
        port = self._port(d)
        try:
            return {"fingerprint": fetch_fingerprint(host, port)}
        except (ApiError, OSError) as e:
            raise ServiceError(f"Fingerprint nicht abrufbar: {e}") from None

    @staticmethod
    def _host(d: dict) -> str:
        h = _s(d, "server")
        if not _HOST.match(h):
            raise ServiceError("Server: Name oder Adresse ungültig")
        return h

    @staticmethod
    def _port(d: dict) -> int:
        try:
            p = int(d.get("port") or 8007)
        except (TypeError, ValueError):
            raise ServiceError("Port ungültig") from None
        if not 1 <= p <= 65535:
            raise ServiceError("Port ungültig")
        return p

    # -- VPN ----------------------------------------------------------------------------------------------
    def vpn_up(self, d: dict) -> dict:
        kind = _s(d, "kind")
        if kind == "wireguard":
            text = _s(d, "config", MAX_PASTE)
            if "[Interface]" not in text:
                raise ServiceError("Das ist keine WireGuard-Konfiguration (Abschnitt [Interface] fehlt)")
            name = _s(d, "name", 15) or "wg0"
            path = self.workdir / f"{name}.conf"
            self._write_secret(path, text + "\n")
            try:
                r = vpn.wireguard_up(str(path), self.run_cmd)
            except vpn.VpnError as e:
                raise ServiceError(str(e)) from None
            finally:
                path.unlink(missing_ok=True)       # vpn.wireguard_up hat eine Kopie unter /etc/wireguard angelegt
        elif kind == "openvpn":
            text = _s(d, "config", MAX_PASTE)
            if not re.search(r"^\s*(remote|client)\b", text, re.M):
                raise ServiceError("Das ist keine OpenVPN-Konfiguration (remote/client fehlt)")
            path = self.workdir / "client.ovpn"
            self._write_secret(path, text + "\n")
            try:
                r = vpn.openvpn_up(str(path), _s(d, "user"), d.get("password") or "", self.run_cmd)
            except vpn.VpnError as e:
                raise ServiceError(str(e)) from None
        elif kind == "netbird":
            try:
                r = vpn.netbird_up(_s(d, "setup_key"), _s(d, "management_url"), self.run_cmd)
            except vpn.VpnError as e:
                raise ServiceError(str(e)) from None
        else:
            raise ServiceError("Unbekannter VPN-Typ")
        return {"ok": r.ok, "text": r.text[-600:], "vpn": vpn.status(self.run_cmd)}

    def vpn_down(self, d: dict) -> dict:
        kind = _s(d, "kind")
        fn = {"wireguard": vpn.wireguard_down, "openvpn": vpn.openvpn_down, "netbird": vpn.netbird_down}.get(kind)
        if not fn:
            raise ServiceError("Unbekannter VPN-Typ")
        r = fn(run=self.run_cmd) if kind != "wireguard" else vpn.wireguard_down(None, self.run_cmd)
        return {"ok": r.ok, "text": r.text[-600:], "vpn": vpn.status(self.run_cmd)}

    @staticmethod
    def _write_secret(path: Path, text: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    # -- Schlüssel ----------------------------------------------------------------------------------------
    def set_key(self, d: dict) -> dict:
        text = _s(d, "content", MAX_PASTE)
        try:
            k = json.loads(text)
        except ValueError:
            raise ServiceError("Das ist keine Schlüsseldatei (kein gültiges JSON)") from None
        if not isinstance(k, dict) or "kdf" not in k or "fingerprint" not in k:
            raise ServiceError("Das sieht nicht nach einem PBS-Schlüssel aus (kdf/fingerprint fehlen)")
        path = self.workdir / "key.json"
        self._write_secret(path, text)
        self.keyfile = str(path)
        return {"ok": True, "fingerprint": k.get("fingerprint", "")}

    # -- Verbindung und Snapshots -----------------------------------------------------------------------------
    def connect(self, d: dict) -> dict:
        fp = _s(d, "fingerprint", 100)
        if not _FP.match(fp):
            raise ServiceError("Fingerprint ungültig")
        auth_id = _s(d, "auth_id")
        if not _AUTH.match(auth_id):
            raise ServiceError("Token-ID ungültig")
        ds, ns, bid = _s(d, "datastore"), _s(d, "namespace"), _s(d, "backup_id")
        for label, v in (("Datastore", ds), ("Namespace", ns), ("Backup-ID", bid)):
            if not _NAME.match(v):
                raise ServiceError(f"{label} enthält unzulässige Zeichen")
        if not ds:
            raise ServiceError("Datastore fehlt")
        token = d.get("token") or ""
        if not isinstance(token, str) or not token or len(token) > 512:
            raise ServiceError("Token-Secret fehlt")
        cfg = RescueConfig(server=self._host(d), port=self._port(d), datastore=ds, auth_id=auth_id, token=token,
                           fingerprint=fp, namespace=ns.strip("/"), backup_id=bid, keyfile=self.keyfile)
        if not cfg.fingerprint:
            raise ServiceError("Fingerprint fehlt: erst abrufen und mit dem Wert in PBS vergleichen")
        try:
            snaps = rescue.list_full_snapshots(cfg)
        except (RescueError, ApiError) as e:
            raise ServiceError(str(e)) from None
        except Exception as e:      # ClientError u. ä.
            msg = str(e).replace(token, "***")
            if "missing key" in msg:
                msg += " (das Backup ist verschlüsselt: erst die Schlüsseldatei angeben)"
            raise ServiceError(msg[-400:]) from None
        self.cfg, self.snaps, self.snap, self.rec, self.plan = cfg, snaps, None, None, None
        return {"snapshots": [{"path": s.path, "time": s.backup_time, "backup_id": s.backup_id, "size": s.size or 0}
                              for s in snaps]}

    def choose(self, d: dict) -> dict:
        if not self.cfg:
            raise ServiceError("Zuerst mit dem Server verbinden", 409)
        path = _s(d, "path", 300)
        snap = next((s for s in self.snaps if s.path == path), None)
        if not snap:
            raise ServiceError("Snapshot nicht gefunden")
        self.cfg.snapshot = snap.path
        try:
            rec = rescue.fetch_recovery(self.cfg, snap, Path(tempfile.mkdtemp(prefix="recovery-", dir=self.workdir)))
        except Exception as e:
            raise ServiceError(str(e).replace(self.cfg.token, "***")[-400:]) from None
        self.snap, self.rec, self.plan = snap, rec, None
        enc = rescue.original_encrypted(rec)
        return {"original_size": rec.root_disk().size, "encrypted": enc, "needs_passphrase": rescue.needs_passphrase(rec),
                "disks": rescue.candidate_disks()}

    def disks(self) -> dict:
        return {"disks": rescue.candidate_disks()}

    # -- Plan ------------------------------------------------------------------------------------------------
    def make_plan(self, d: dict) -> dict:
        if not (self.cfg and self.snap and self.rec):
            raise ServiceError("Zuerst einen Snapshot wählen", 409)
        disk = _s(d, "disk", 64)
        if not re.fullmatch(r"/dev/[A-Za-z0-9/_\-]+", disk):
            raise ServiceError("Ziel-Platte ungültig")
        enc = _s(d, "encryption") or "keep"
        if enc not in ("keep", "new", "none"):
            raise ServiceError("Verschlüsselung ungültig")
        passphrase = d.get("passphrase") or ""
        new_pass = d.get("new_passphrase") or ""
        if not isinstance(passphrase, str) or not isinstance(new_pass, str) or len(passphrase) > 512 or len(new_pass) > 512:
            raise ServiceError("Passphrase ungültig")
        if enc == "new" and len(new_pass) < 8:
            raise ServiceError("Die neue Passphrase braucht mindestens 8 Zeichen")
        if rescue.needs_passphrase(self.rec, enc) and not passphrase:
            raise ServiceError("Die LUKS-Passphrase des Originalsystems fehlt")
        try:
            size = rescue.validate_target(disk, self.rec)
            shrink = bool(d.get("shrink"))
            plan = rescue.build_layout_plan(self.rec, disk, passphrase or None, target_size=size, encryption=enc,
                                            new_passphrase=new_pass or None, shrink=shrink,
                                            snapshot_size=self.snap.size or None)
        except RescueError as e:
            raise ServiceError(str(e)) from None
        self.cfg.target_disk, self.cfg.encryption = disk, enc
        self.cfg.passphrase = passphrase if rescue.needs_passphrase(self.rec, enc) else ""
        self.cfg.new_passphrase = new_pass if enc == "new" else ""
        self.plan, self.target = plan, disk
        return {"disk": disk, "summary": plan.summary, "warnings": plan.warnings, "size": size,
                "original_size": self.rec.root_disk().size}

    # -- Ausführung ---------------------------------------------------------------------------------------------
    def start(self, d: dict) -> dict:
        dry = bool(d.get("dry_run"))
        with self._lock:
            if self.run.state == "running":
                raise ServiceError("Es läuft bereits eine Wiederherstellung", 409)
            if not (self.cfg and self.snap and self.rec and self.plan):
                raise ServiceError("Zuerst einen Plan erstellen", 409)
            if not dry:
                if os.geteuid() != 0:
                    raise ServiceError("Die Wiederherstellung braucht root (nur der Trockenlauf geht ohne)", 403)
                if _s(d, "confirm", 64) != self.target:
                    raise ServiceError(f"Bestätigung falsch: zum Starten den Gerätenamen {self.target} eintippen", 403)
            self.run = RunState("running", [], dry)
            cfg, snap, rec, plan = self.cfg, self.snap, self.rec, self.plan
        threading.Thread(target=self._execute, args=(cfg, snap, rec, plan, dry), daemon=True).start()
        return {"started": True}

    def _execute(self, cfg, snap, rec, plan, dry) -> None:
        def log(line: str) -> None:
            self.run.lines.append(line.rstrip("\n"))
        ok = False
        try:
            ok = rescue.execute(cfg, snap, rec, plan, log, dry_run=dry)
        except Exception as e:
            log(f"FEHLER: {str(e).replace(cfg.token, '***')}")
        finally:
            self.run.state = "ok" if ok else "failed"
            cfg.token = cfg.passphrase = cfg.new_passphrase = ""        # Geheimnisse nicht länger als nötig behalten
            if self.on_finished:
                self.on_finished(ok, dry)

    def log(self, since: int = 0) -> dict:
        lines = self.run.lines
        since = max(0, min(int(since or 0), len(lines)))
        return {"state": self.run.state, "dry_run": self.run.dry_run, "lines": lines[since:since + 500],
                "next": min(len(lines), since + 500)}

    def power(self, d: dict) -> dict:
        action = _s(d, "action")
        if action not in ("reboot", "poweroff"):
            raise ServiceError("Unbekannte Aktion")
        if self.run.state == "running":
            raise ServiceError("Die Wiederherstellung läuft noch", 409)

        def later():
            time.sleep(2)
            self.run_cmd(["systemctl", action], capture_output=True)
        threading.Thread(target=later, daemon=True).start()
        return {"ok": True}

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)
