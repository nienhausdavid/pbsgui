"""`pbsgui rescue …` – Wiederherstellung auf einer leeren Platte (Rettungs-ISO, braucht root)."""
from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

from ..i18n import tr
from ..core.pbs_api import ApiError, fetch_fingerprint
from ..core.rescue import (RescueConfig, RescueError, build_layout_plan, candidate_disks, execute, fetch_recovery,
                           find_key_files, list_full_snapshots, needs_passphrase, pick_snapshot, validate_target)

DEFAULT_CONFIG = "/etc/pbsgui/rescue.json"  # in der personalisierten ISO: Server, Fingerprint, Datastore, Namespace


class RescueCliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def add_arguments(sub) -> None:
    sp = sub.add_parser("rescue", help=tr("Wiederherstellung auf einer leeren Platte (Rettungs-ISO, root)"))
    sp.set_defaults(fn=cmd_rescue)
    rsub = sp.add_subparsers(dest="rescue_cmd")
    for name, help_ in (("disks", tr("mögliche Ziel-Platten anzeigen")),
                        ("keys", tr("USB-Sticks einhängen und nach Schlüsseldateien suchen")), ("snapshots", tr("Vollbackups des Servers auflisten")),
                        ("run", tr("Wiederherstellung durchführen")),
                        ("vm-snapshots", tr("VM-Backups (Proxmox VE, Snapshots vm/<id>) des Servers auflisten")),
                        ("vm-run", tr("VM-Abbild auf eine oder mehrere physische Platten wiederherstellen (V2P)"))):
        r = rsub.add_parser(name, help=help_)
        if name not in ("disks", "keys"):
            r.add_argument("--config", help=tr("JSON mit Verbindungsdaten (Standard: {0}, falls vorhanden)", DEFAULT_CONFIG))
            r.add_argument("--server"); r.add_argument("--port", type=int)
            r.add_argument("--datastore"); r.add_argument("--namespace"); r.add_argument("--auth-id")
            r.add_argument("--fingerprint"); r.add_argument("--backup-id")
            r.add_argument("--keyfile", help=tr("exportierte Schlüsseldatei (bei verschlüsseltem Backup)"))
            r.add_argument("--yes", action="store_true", help=tr("Fingerprint ohne Rückfrage akzeptieren"))
        if name in ("vm-snapshots", "vm-run"):
            r.add_argument("--vmid", help=tr("VM-Nummer (entspricht --backup-id)"))
        if name == "vm-run":
            r.add_argument("--snapshot", help=tr("Snapshot-Pfad, z. B. vm/100/2026-10-20T10:00:00Z; Standard: neuester der VM"))
            r.add_argument("--map", action="append", default=[], metavar="QUELLE=ZIEL",
                           help=tr("Quell-Platte der VM auf Ziel-Platte, z. B. --map scsi0=/dev/sda (mehrfach; ohne Angabe: Boot-Platte auf --disk)"))
            r.add_argument("--disk", help=tr("Kurzform für --map <Boot-Platte>=DISK"))
            r.add_argument("--dry-run", action="store_true"); r.add_argument("--confirm-disk", action="append", default=None,
                                                                              help=tr("jede Ziel-Platte zur Bestätigung angeben (ohne Rückfrage)"))
            for opt, what in (("grow", tr("Partition und Dateisystem vergrößern")), ("initramfs", tr("Initramfs mit allen Treibern neu erzeugen")),
                              ("bootloader", tr("Bootloader neu installieren")), ("network", tr("Netzwerk-Schnittstellennamen anpassen")),
                              ("services", tr("qemu-guest-agent und cloud-init abschalten"))):
                r.add_argument(f"--no-{opt}", dest=f"no_{opt}", action="store_true", help=tr("nicht: {0}", what))
        if name == "run":
            r.add_argument("--disk", required=True, help=tr("Ziel-Platte, z. B. /dev/sda (wird KOMPLETT überschrieben)"))
            r.add_argument("--snapshot", help=tr("Snapshot-Pfad; Standard: neuester"))
            r.add_argument("--dry-run", action="store_true", help=tr("nur anzeigen, was getan würde"))
            r.add_argument("--confirm-disk", help=tr("Ziel-Platte zur Bestätigung noch einmal angeben (ohne Rückfrage)"))
            r.add_argument("--shrink", action="store_true",
                           help=tr("Größe anpassen: erlaubt eine KLEINERE Ziel-Platte (Partitionen/LVs werden verkleinert, die belegten Daten plus 20 %% Reserve müssen passen)"))
            r.add_argument("--encryption", choices=["keep", "new", "none"], default="keep",
                           help=tr("LUKS: keep = wie im Original (Standard), new = neu verschlüsseln mit neuer Passphrase (PBSGUI_NEW_LUKS_PASSPHRASE), none = ohne Verschlüsselung aufbauen"))


def _secret(env: str, prompt: str) -> str:
    v = os.environ.get(env)
    if v:
        return v
    if not sys.stdin.isatty():
        raise RescueCliError(tr("{0} fehlt: Umgebungsvariable {1} setzen oder interaktiv ausführen", prompt, env))
    return getpass.getpass(f"{prompt}: ")


def _new_passphrase() -> str:
    v = os.environ.get("PBSGUI_NEW_LUKS_PASSPHRASE")
    if not v:
        if not sys.stdin.isatty():
            raise RescueCliError(tr("Neue Passphrase fehlt: Umgebungsvariable PBSGUI_NEW_LUKS_PASSPHRASE setzen"))
        v = getpass.getpass(tr("Neue LUKS-Passphrase: "))
        if getpass.getpass(tr("Neue LUKS-Passphrase wiederholen: ")) != v:
            raise RescueCliError(tr("Die Passphrasen stimmen nicht überein"))
    if len(v) < 8:
        raise RescueCliError(tr("Die neue Passphrase ist zu kurz (mindestens 8 Zeichen)"))
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
        raise RescueCliError(tr("Angaben fehlen: ") + ", ".join(m.replace("_", "-") for m in missing) +
                             tr(" (als Option oder in der Konfigurationsdatei)"))
    cfg = RescueConfig.from_json(_tmp_json(base))
    if not cfg.fingerprint:
        fp = fetch_fingerprint(cfg.server, cfg.port)
        print(tr("Fingerprint von {0}:{1}:\n  {2}", cfg.server, cfg.port, fp))
        if not getattr(a, "yes", False):
            if not sys.stdin.isatty() or input(tr("Stimmt er mit dem in PBS angezeigten Wert überein? [j/N] ")).strip().lower() not in ("j", "y"):
                raise RescueCliError(tr("Fingerprint nicht bestätigt (--fingerprint angeben oder --yes)"))
        cfg.fingerprint = fp
    cfg.token = _secret("PBSGUI_TOKEN_SECRET", tr("Token-Secret für {0}", cfg.auth_id))
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
                print(tr("Keine zulässigen Ziel-Platten (eingehängte Platten und das Live-Medium sind ausgeschlossen)."))
            for d in disks:
                print(f"{d['path']:<14} {d['size'] / 2**30:8.1f} GiB  {d['model'] or '-':<24} {d['tran'] or '-':<6}"
                      + (tr("  (Wechseldatenträger)") if d["removable"] else ""))
            return 0
        if a.rescue_cmd == "keys":
            keys = find_key_files(mount=os.geteuid() == 0)
            print("\n".join(keys) if keys else tr("Keine Schlüsseldatei gefunden."))
            return 0 if keys else 1
        if a.rescue_cmd in ("snapshots", "run", "vm-snapshots", "vm-run"):
            return {"snapshots": _snapshots, "run": _run, "vm-snapshots": _vm_snapshots, "vm-run": _vm_run}[a.rescue_cmd](a)
        from ..tui.rescue import run as run_tui  # ohne Unterbefehl: geführter Assistent
        run_tui()
        return 0
    except (RescueCliError, RescueError, ApiError) as e:
        print(tr("Fehler: {0}", e), file=sys.stderr)
        return getattr(e, "code", 1)
    except Exception as e:  # z. B. ClientError mit Hinweis zum Schlüssel
        msg = str(e)
        if "missing key" in msg:
            msg += tr("\nHinweis: Das Backup ist verschlüsselt. Mit --keyfile die exportierte Schlüsseldatei angeben.")
        print(tr("Fehler: {0}", msg), file=sys.stderr)
        return 1


def _snapshots(a) -> int:
    import time
    cfg = build_config(a)
    for i, s in enumerate(list_full_snapshots(cfg)):
        print(f"{i}  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.backup_time))}  {s.backup_id:<20} {s.path}")
    return 0


def _run(a) -> int:
    if os.geteuid() != 0 and not a.dry_run:
        raise RescueCliError(tr("Die Wiederherstellung braucht root (in der Rettungs-ISO ist das der Standard)"))
    cfg = build_config(a)
    cfg.target_disk, cfg.snapshot = a.disk, a.snapshot or ""
    snap = pick_snapshot(cfg)
    print(tr("Snapshot: {0}", snap.path))
    rec = fetch_recovery(cfg, snap, Path(tempfile.mkdtemp(prefix="pbsgui-recovery-")))
    size = validate_target(a.disk, rec)
    cfg.encryption = a.encryption
    cfg.passphrase = (_secret("PBSGUI_LUKS_PASSPHRASE", tr("LUKS-Passphrase des Originalsystems"))
                      if needs_passphrase(rec, a.encryption) else "")
    if a.encryption == "new":
        cfg.new_passphrase = _new_passphrase()
    shrink = a.shrink
    src_size = rec.root_disk().size
    if size < src_size and not shrink:
        print(tr("Die Ziel-Platte ({0:.1f} GiB) ist kleiner als das Original ({1:.1f} GiB).", size / 2**30, src_size / 2**30))
        if sys.stdin.isatty() and input(tr("Partitionen und Volumes verkleinern ('Größe anpassen')? [j/N] ")).strip().lower() in ("j", "y"):
            shrink = True
    plan = build_layout_plan(rec, a.disk, cfg.passphrase or None, target_size=size, encryption=a.encryption,
                             new_passphrase=cfg.new_passphrase or None, shrink=shrink, snapshot_size=snap.size or None)
    print(tr("\nGeplante Wiederherstellung:"))
    for line in plan.summary:
        print("  " + line)
    for w in plan.warnings:
        print("  Warnung: " + w)
    if not a.dry_run:
        typed = a.confirm_disk
        if typed is None:
            if not sys.stdin.isatty():
                raise RescueCliError(tr("Bestätigung fehlt: --confirm-disk ") + a.disk, 2)
            typed = input(tr("\nALLE DATEN AUF {0} GEHEN VERLOREN. Zur Bestätigung den Gerätenamen eintippen: ", a.disk))
        if typed.strip() != a.disk:
            raise RescueCliError(tr("Abgebrochen: Bestätigung stimmt nicht überein, nichts wurde verändert."), 2)
    ok = execute(cfg, snap, rec, plan, lambda l: print(l, flush=True), dry_run=a.dry_run)
    return 0 if ok else 1


def _vm_snapshots(a) -> int:
    import time

    from ..core import vmimage
    a.backup_id = getattr(a, "vmid", None) or getattr(a, "backup_id", None)
    cfg = build_config(a)
    snaps = vmimage.list_vm_snapshots(cfg)
    if not snaps:
        print(tr("Keine VM-Backups (vm/…) mit Abbild gefunden."))
        return 1
    for i, s in enumerate(snaps):
        disks = ", ".join(f"{d.key} {d.image_size / 2**30:.1f} GiB" for d in vmimage.vm_disks(s))
        print(f"{i}  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.backup_time))}  VM {s.backup_id:<8} {disks}   {s.path}")
    return 0


def _vm_run(a) -> int:
    from ..core import vmimage
    if os.geteuid() != 0 and not a.dry_run:
        raise RescueCliError(tr("Die Wiederherstellung braucht root (in der Rettungs-ISO ist das der Standard)"))
    a.backup_id = a.vmid or getattr(a, "backup_id", None)
    cfg = build_config(a)
    snaps = vmimage.list_vm_snapshots(cfg)
    if a.snapshot:
        snap = next((s for s in snaps if s.path == a.snapshot), None)
        if not snap:
            raise RescueCliError(tr("Snapshot nicht gefunden: {0}", a.snapshot))
    elif snaps:
        snap = snaps[0]
    else:
        raise RescueCliError(tr("Kein VM-Backup mit Abbild gefunden (--vmid prüfen)"))
    cfg.snapshot = snap.path
    print(tr("Snapshot: {0}", snap.path))
    vmcfg = vmimage.fetch_config(cfg, snap)
    mapping = vmimage.parse_mapping(a.map)
    if a.disk:
        mapping.setdefault(vmimage.boot_disk_key(vmcfg, vmimage.vm_disks(snap)), a.disk)
    if not mapping:
        raise RescueCliError(tr("Ziel fehlt: --map scsi0=/dev/sda (oder --disk /dev/sda für die Boot-Platte)"))
    if os.environ.get("PBSGUI_LUKS_PASSPHRASE"):
        cfg.passphrase = os.environ["PBSGUI_LUKS_PASSPHRASE"]
    opts = vmimage.RepairOptions(**{k: not getattr(a, f"no_{k}") for k in ("grow", "initramfs", "bootloader", "network", "services")})
    plan = vmimage.build_plan(cfg, snap, vmcfg, mapping, candidate_disks(), opts)
    print(tr("\nVM: {0}   Betriebssystem: {1}   Firmware: {2}", vmcfg.name or snap.backup_id, vmcfg.ostype or '?', 'UEFI' if vmcfg.uefi else 'BIOS'))
    print(tr("Geplante Wiederherstellung:"))
    for line in plan.summary:
        print("  " + line)
    for w in plan.warnings:
        print("  Warnung: " + w)
    if not a.dry_run:
        wanted = sorted(set(mapping.values()))
        typed = a.confirm_disk
        if typed is None:
            if not sys.stdin.isatty():
                raise RescueCliError(tr("Bestätigung fehlt: ") + " ".join(f"--confirm-disk {d}" for d in wanted), 2)
            typed = input(tr("\nALLE DATEN AUF {0} GEHEN VERLOREN. Zur Bestätigung die Gerätenamen eintippen (mit Leerzeichen getrennt): ", ' '.join(wanted))).split()
        if sorted(set(t.strip() for t in typed)) != wanted:
            raise RescueCliError(tr("Abgebrochen: Bestätigung stimmt nicht überein, nichts wurde verändert."), 2)
    return 0 if vmimage.execute(cfg, plan, lambda l: print(l, flush=True), dry_run=a.dry_run) else 1
