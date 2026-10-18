"""`pbsgui rescue …` – Wiederherstellung auf einer leeren Platte (Rettungs-ISO, braucht root)."""
from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

from ..core.pbs_api import ApiError, fetch_fingerprint
from ..core.rescue import (RescueConfig, RescueError, build_layout_plan, candidate_disks, execute, fetch_recovery,
                           find_key_files, list_full_snapshots, needs_passphrase, pick_snapshot, validate_target)

DEFAULT_CONFIG = "/etc/pbsgui/rescue.json"  # in der personalisierten ISO: Server, Fingerprint, Datastore, Namespace


class RescueCliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def add_arguments(sub) -> None:
    sp = sub.add_parser("rescue", help="Wiederherstellung auf einer leeren Platte (Rettungs-ISO, root)")
    sp.set_defaults(fn=cmd_rescue)
    rsub = sp.add_subparsers(dest="rescue_cmd")
    for name, help_ in (("disks", "mögliche Ziel-Platten anzeigen"),
                        ("keys", "USB-Sticks einhängen und nach Schlüsseldateien suchen"), ("snapshots", "Vollbackups des Servers auflisten"),
                        ("run", "Wiederherstellung durchführen")):
        r = rsub.add_parser(name, help=help_)
        if name not in ("disks", "keys"):
            r.add_argument("--config", help=f"JSON mit Verbindungsdaten (Standard: {DEFAULT_CONFIG}, falls vorhanden)")
            r.add_argument("--server"); r.add_argument("--port", type=int)
            r.add_argument("--datastore"); r.add_argument("--namespace"); r.add_argument("--auth-id")
            r.add_argument("--fingerprint"); r.add_argument("--backup-id")
            r.add_argument("--keyfile", help="exportierte Schlüsseldatei (bei verschlüsseltem Backup)")
            r.add_argument("--yes", action="store_true", help="Fingerprint ohne Rückfrage akzeptieren")
        if name == "run":
            r.add_argument("--disk", required=True, help="Ziel-Platte, z. B. /dev/sda (wird KOMPLETT überschrieben)")
            r.add_argument("--snapshot", help="Snapshot-Pfad; Standard: neuester")
            r.add_argument("--dry-run", action="store_true", help="nur anzeigen, was getan würde")
            r.add_argument("--confirm-disk", help="Ziel-Platte zur Bestätigung noch einmal angeben (ohne Rückfrage)")
            r.add_argument("--encryption", choices=["keep", "new", "none"], default="keep",
                           help="LUKS: keep = wie im Original (Standard), new = neu verschlüsseln mit neuer Passphrase "
                                "(PBSGUI_NEW_LUKS_PASSPHRASE), none = ohne Verschlüsselung aufbauen")


def _secret(env: str, prompt: str) -> str:
    v = os.environ.get(env)
    if v:
        return v
    if not sys.stdin.isatty():
        raise RescueCliError(f"{prompt} fehlt: Umgebungsvariable {env} setzen oder interaktiv ausführen")
    return getpass.getpass(f"{prompt}: ")


def _new_passphrase() -> str:
    v = os.environ.get("PBSGUI_NEW_LUKS_PASSPHRASE")
    if not v:
        if not sys.stdin.isatty():
            raise RescueCliError("Neue Passphrase fehlt: Umgebungsvariable PBSGUI_NEW_LUKS_PASSPHRASE setzen")
        v = getpass.getpass("Neue LUKS-Passphrase: ")
        if getpass.getpass("Neue LUKS-Passphrase wiederholen: ") != v:
            raise RescueCliError("Die Passphrasen stimmen nicht überein")
    if len(v) < 8:
        raise RescueCliError("Die neue Passphrase ist zu kurz (mindestens 8 Zeichen)")
    return v


def build_config(a) -> RescueConfig:
    path = a.config or (DEFAULT_CONFIG if Path(DEFAULT_CONFIG).exists() else None)
    base: dict = json.loads(Path(path).read_text()) if path else {}
    for k in ("server", "port", "datastore", "namespace", "auth_id", "fingerprint", "backup_id", "keyfile"):
        v = getattr(a, k, None)
        if v not in (None, ""):
            base[k] = v
    missing = [k for k in ("server", "datastore", "auth_id") if not base.get(k)]
    if missing:
        raise RescueCliError("Angaben fehlen: " + ", ".join(m.replace("_", "-") for m in missing) +
                             " (als Option oder in der Konfigurationsdatei)")
    cfg = RescueConfig.from_json(_tmp_json(base))
    if not cfg.fingerprint:
        fp = fetch_fingerprint(cfg.server, cfg.port)
        print(f"Fingerprint von {cfg.server}:{cfg.port}:\n  {fp}")
        if not getattr(a, "yes", False):
            if not sys.stdin.isatty() or input("Stimmt er mit dem in PBS angezeigten Wert überein? [j/N] ").strip().lower() not in ("j", "y"):
                raise RescueCliError("Fingerprint nicht bestätigt (--fingerprint angeben oder --yes)")
        cfg.fingerprint = fp
    cfg.token = _secret("PBSGUI_TOKEN_SECRET", f"Token-Secret für {cfg.auth_id}")
    return cfg


def _tmp_json(d: dict) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(d, f)
    f.close()
    return f.name


def cmd_rescue(a, mgr=None) -> int:
    try:
        if a.rescue_cmd == "disks":
            disks = candidate_disks()
            if not disks:
                print("Keine zulässigen Ziel-Platten (eingehängte Platten und das Live-Medium sind ausgeschlossen).")
            for d in disks:
                print(f"{d['path']:<14} {d['size'] / 2**30:8.1f} GiB  {d['model'] or '-':<24} {d['tran'] or '-':<6}"
                      + ("  (Wechseldatenträger)" if d["removable"] else ""))
            return 0
        if a.rescue_cmd == "keys":
            keys = find_key_files(mount=os.geteuid() == 0)
            print("\n".join(keys) if keys else "Keine Schlüsseldatei gefunden.")
            return 0 if keys else 1
        if a.rescue_cmd in ("snapshots", "run"):
            return _snapshots(a) if a.rescue_cmd == "snapshots" else _run(a)
        from ..tui.rescue import run as run_tui  # ohne Unterbefehl: geführter Assistent
        run_tui()
        return 0
    except (RescueCliError, RescueError, ApiError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return getattr(e, "code", 1)
    except Exception as e:  # z. B. ClientError mit Hinweis zum Schlüssel
        msg = str(e)
        if "missing key" in msg:
            msg += "\nHinweis: Das Backup ist verschlüsselt. Mit --keyfile die exportierte Schlüsseldatei angeben."
        print(f"Fehler: {msg}", file=sys.stderr)
        return 1


def _snapshots(a) -> int:
    import time
    cfg = build_config(a)
    for i, s in enumerate(list_full_snapshots(cfg)):
        print(f"{i}  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.backup_time))}  {s.backup_id:<20} {s.path}")
    return 0


def _run(a) -> int:
    if os.geteuid() != 0 and not a.dry_run:
        raise RescueCliError("Die Wiederherstellung braucht root (in der Rettungs-ISO ist das der Standard)")
    cfg = build_config(a)
    cfg.target_disk, cfg.snapshot = a.disk, a.snapshot or ""
    snap = pick_snapshot(cfg)
    print(f"Snapshot: {snap.path}")
    rec = fetch_recovery(cfg, snap, Path(tempfile.mkdtemp(prefix="pbsgui-recovery-")))
    size = validate_target(a.disk, rec)
    cfg.encryption = a.encryption
    cfg.passphrase = (_secret("PBSGUI_LUKS_PASSPHRASE", "LUKS-Passphrase des Originalsystems")
                      if needs_passphrase(rec, a.encryption) else "")
    if a.encryption == "new":
        cfg.new_passphrase = _new_passphrase()
    plan = build_layout_plan(rec, a.disk, cfg.passphrase or None, target_size=size, encryption=a.encryption,
                             new_passphrase=cfg.new_passphrase or None)
    print("\nGeplante Wiederherstellung:")
    for line in plan.summary:
        print("  " + line)
    for w in plan.warnings:
        print("  Warnung: " + w)
    if not a.dry_run:
        typed = a.confirm_disk
        if typed is None:
            if not sys.stdin.isatty():
                raise RescueCliError("Bestätigung fehlt: --confirm-disk " + a.disk, 2)
            typed = input(f"\nALLE DATEN AUF {a.disk} GEHEN VERLOREN. Zur Bestätigung den Gerätenamen eintippen: ")
        if typed.strip() != a.disk:
            raise RescueCliError("Abgebrochen: Bestätigung stimmt nicht überein, nichts wurde verändert.", 2)
    ok = execute(cfg, snap, rec, plan, lambda l: print(l, flush=True), dry_run=a.dry_run)
    return 0 if ok else 1
