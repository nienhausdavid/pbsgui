"""`pbsgui` – ohne Argumente startet die GUI, mit Unterbefehlen das Kommandozeilen-Tool, `pbsgui tui` die TUI.

Geheimnisse (Token, Root-Passwort) werden nie als Argument angenommen, sondern per Prompt oder über
die Umgebungsvariablen PBSGUI_TOKEN_SECRET / PBSGUI_ROOT_PASSWORD.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from ..core.client import ClientError
from ..core.manager import ProfileManager, can_use_admin
from ..core.models import Profile, slugify
from ..core.modes import FULL, HOME, describe
from ..core.onboarding import (connect_root, connect_token, namespace_message, namespace_state,
                               provision_and_verify, verify_token_profile)
from ..core.paths import Paths
from ..core.pbs_api import ApiError, TokenExistsError, fetch_fingerprint
from ..core.runner import read_latest_status
from ..core.scheduler import SchedulerError, validate_calendar


class CliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def table(rows: list[list[str]], header: list[str]) -> str:
    cols = list(zip(header, *rows)) if rows else [(h,) for h in header]
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(header), line(["-" * w for w in widths]), *(line(r) for r in rows)])


def find_profile(mgr: ProfileManager, name: str) -> Profile:
    for p in mgr.profiles():
        if p.name == name or p.slug == slugify(name):
            return p
    raise CliError(f"Profil nicht gefunden: {name}")


def secret_from(env: str, prompt: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    if not sys.stdin.isatty():
        raise CliError(f"{prompt} fehlt: Umgebungsvariable {env} setzen oder interaktiv ausführen")
    return getpass.getpass(f"{prompt}: ")


def schedule_text(mgr: ProfileManager, p: Profile) -> str:
    try:
        st = mgr.schedule_status(p)
    except Exception:
        return "?"
    return "aktiv" if st["active"] else ("inaktiv" if st["installed"] else "-")


def last_run_text(p: Profile) -> str:
    st = read_latest_status(p)
    if not st:
        return "-"
    return f"{fmt_time(st['time'])} {'OK' if st['ok'] else 'FEHLER'}"


# -- Befehle ------------------------------------------------------------------
def cmd_list(a, mgr: ProfileManager) -> int:
    profiles = mgr.profiles()
    if a.json:
        print(json.dumps([{**p.to_dict(), "schedule_active": schedule_text(mgr, p), "last_run": read_latest_status(p)}
                          for p in profiles], indent=2))
        return 0
    if not profiles:
        print("Keine Profile. Mit `pbsgui add` einen Server hinzufügen.")
        return 0
    rows = [[p.name, f"{p.server}:{p.port}", p.datastore + (f"/{p.namespace}" if p.namespace else ""),
             "System" if p.backup_mode == FULL else "Home", "ja" if p.encrypt else "NEIN", p.scope, p.schedule or "-", schedule_text(mgr, p), last_run_text(p)]
            for p in profiles]
    print(table(rows, ["Name", "Server", "Datastore/Namespace", "Umfang", "Verschl.", "Modus", "Zeitplan", "Status", "Letzter Lauf"]))
    return 0


def cmd_show(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    for k, v in p.to_dict().items():
        print(f"{k:16} {v}")
    print(f"{'repository':16} {p.repository}")
    print(f"{'letzter Lauf':16} {last_run_text(p)}")
    return 0


def _connect(a, fp: str):
    if a.token_id:
        secret = secret_from("PBSGUI_TOKEN_SECRET", "Token-Secret")
        return connect_token(a.server, a.port, fp, a.token_id, secret), secret
    pw = secret_from("PBSGUI_ROOT_PASSWORD", f"Passwort für {a.root_user}")
    return connect_root(a.server, a.port, fp, a.root_user, pw, a.tfa or ""), ""


def _confirm_fingerprint(a) -> str:
    if a.fingerprint:
        return a.fingerprint
    fp = fetch_fingerprint(a.server, a.port)
    print(f"Fingerprint von {a.server}:{a.port}:\n  {fp}")
    if a.yes:
        return fp
    if not sys.stdin.isatty():
        raise CliError("Fingerprint muss bestätigt werden: --fingerprint angeben oder interaktiv ausführen")
    if input("Stimmt er mit dem in PBS angezeigten Wert überein? [j/N] ").strip().lower() not in ("j", "y", "ja", "yes"):
        raise CliError("Abgebrochen: Fingerprint nicht bestätigt")
    return fp


def cmd_probe(a, mgr) -> int:
    """Datastores und Namespaces eines Servers anzeigen (ohne etwas anzulegen)."""
    fp = _confirm_fingerprint(a)
    api, _ = _connect(a, fp)
    stores = api.list_datastores()
    if not stores:
        print("Keine Datastores sichtbar (fehlende Audit-Rechte?).")
    for ds in stores:
        try:
            ns = api.list_namespaces(ds)
        except ApiError as e:
            ns = [f"(nicht lesbar: {e})"]
        print(f"{ds}")
        for n in ns:
            print(f"  └ {n}")
    return 0


def cmd_add(a, mgr: ProfileManager) -> int:
    if a.mode == FULL and not can_use_admin():
        raise CliError("Ein Vollbackup braucht Administrator-Rechte (sudo/pkexec), die hier nicht erkannt wurden. "
                       "Mit --mode home nur das Home-Verzeichnis sichern.")
    if any(p.slug == slugify(a.name) for p in mgr.profiles()):
        raise CliError(f"Profil '{a.name}' existiert bereits")
    fp = _confirm_fingerprint(a)
    api, secret = _connect(a, fp)
    weg_a = not a.token_id
    ns = a.namespace.strip("/")
    stores = api.list_datastores()
    if stores and a.datastore not in stores:
        raise CliError(f"Datastore '{a.datastore}' nicht gefunden. Verfügbar: {', '.join(stores)}")
    try:
        known = api.list_namespaces(a.datastore)
    except ApiError:
        known = None
    state = namespace_state(known, ns)
    print(namespace_message(state, weg_a))
    if state == "new" and not weg_a:
        raise CliError("Namespace existiert nicht und lässt sich mit einem vorhandenen Token nicht anlegen", 2)

    hostname = a.backup_id or os.uname().nodename
    if weg_a:
        try:
            res = provision_and_verify(api, a.server, a.port, fp, a.datastore, ns, a.replace_token, hostname)
        except TokenExistsError as e:
            raise CliError(f"{e}. Mit --replace-token löschen und neu erzeugen (andere Nutzer dieses Tokens "
                           "verlieren den Zugang).", 3)
        auth_id, secret = res.auth_id, res.secret
        print(f"Zugang angelegt: {auth_id}")
    else:
        auth_id = a.token_id
    if a.mode == FULL:
        if a.source:
            raise CliError("--source passt nicht zu --mode full (das ganze System wird gesichert)")
        sources, scope = ["/"], "system"
    else:
        sources, scope = a.source or [str(Path.home())], a.scope
    profile = Profile(name=a.name, server=a.server, port=a.port, datastore=a.datastore, namespace=ns,
                      auth_id=auth_id, fingerprint=fp, backup_id=hostname, sources=sources,
                      excludes=a.exclude or [], encrypt=not a.no_encrypt, scope=scope, backup_mode=a.mode)
    if not weg_a:
        verify_token_profile(profile, secret)
        print("Verbindung mit dem Client geprüft.")
    mgr.save(profile, token=secret)
    if profile.encrypt and mgr.enable_encryption(profile):
        print(f"\nVerschlüsselungs-Schlüssel erzeugt. Fingerprint: {profile.key_fingerprint}")
        print("ACHTUNG: Ohne Kopie des Schlüssels sind die Backups bei Verlust UNWIEDERBRINGLICH unlesbar.")
        if a.export_key:
            mgr.export_key(profile, a.export_key)
            print(f"Schlüssel exportiert nach {a.export_key}")
        else:
            print(f"Jetzt sichern: pbsgui key export {profile.name} <ZIELDATEI>")
    print(f"\nProfil '{profile.name}' angelegt: {describe(profile.backup_mode)} (Quellen: {', '.join(sources)}).")
    if profile.backup_mode == FULL:
        print("Hinweis: Dateibasiertes Vollbackup, kein Systemabbild. Es läuft mit Administrator-Rechten (Modus system).")
    return 0


def cmd_backup(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    return mgr.run_backup(p, on_line=lambda l: print(l, flush=True))


def cmd_snapshots(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    snaps = mgr.snapshots(p)
    if a.json:
        print(json.dumps([{"index": i, "path": s.path, "time": s.backup_time, "size": s.size, "files": s.files}
                          for i, s in enumerate(snaps)], indent=2))
        return 0
    print(table([[str(i), fmt_time(s.backup_time), s.backup_id, fmt_size(s.size), ", ".join(s.files)]
                 for i, s in enumerate(snaps)], ["#", "Zeit", "ID", "Größe", "Dateien"]))
    return 0


def cmd_restore(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    snaps = mgr.snapshots(p)
    if not snaps:
        raise CliError("Keine Snapshots vorhanden")
    if a.snapshot.isdigit():
        if int(a.snapshot) >= len(snaps):
            raise CliError(f"Snapshot #{a.snapshot} existiert nicht (0..{len(snaps) - 1})")
        snap = snaps[int(a.snapshot)]
    else:
        snap = next((s for s in snaps if s.path == a.snapshot), None)
        if not snap:
            raise CliError(f"Snapshot nicht gefunden: {a.snapshot}")
    archives = [f.removesuffix(".didx") for f in snap.files if f.endswith(".pxar.didx") or f.endswith(".pxar")]
    archive = a.archive or (archives[0] if len(archives) == 1 else "")
    if not archive:
        raise CliError("Mehrere Archive, bitte --archive wählen: " + ", ".join(archives or snap.files))
    target = Path(a.target).expanduser()
    if target.exists() and any(target.iterdir()) and not a.force:
        raise CliError(f"Ziel {target} ist nicht leer. Neuen/leeren Ordner wählen oder --force nutzen.")
    mgr.client(p).restore(snap, archive, str(target))
    print(f"Wiederhergestellt: {snap.path} / {archive} -> {target}")
    return 0


def cmd_schedule(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    if a.remove:
        mgr.save(p, schedule="remove")
        print(f"Zeitplan für '{p.name}' entfernt.")
        return 0
    if not a.calendar:
        raise CliError("--calendar angeben (z. B. 'daily' oder '*-*-* 02:00:00') oder --remove")
    scope = a.scope or p.scope
    if scope == "system" and not can_use_admin():
        raise CliError("Modus 'system' braucht Administrator-Rechte (sudo/pkexec)")
    validate_calendar(a.calendar)
    mgr.move_scope(p, scope)
    p.schedule = a.calendar
    if a.require_ac is not None:
        p.require_ac = a.require_ac
    mgr.save(p, schedule="install")
    print(f"Zeitplan aktiv ({scope}): {a.calendar}")
    return 0


def cmd_status(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    st = mgr.schedule_status(p)
    print(f"Zeitplan:     {'aktiv' if st['active'] else ('installiert, inaktiv' if st['installed'] else 'nicht eingerichtet')}"
          f" ({p.scope}, {p.schedule or 'kein Ausdruck'})")
    if st["next"] and st["next"] != "n/a":
        print(f"Nächster Lauf: {st['next']}")
    print(f"Letzter Lauf: {last_run_text(p)}")
    return 0


def cmd_key(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    if a.action == "info":
        stored = bool(mgr.secrets.get(p.slug, "key"))
        print(f"Verschlüsselung: {'an' if p.encrypt else 'AUS'}")
        print(f"Automatischer Schlüssel: {'ja, Fingerprint ' + p.key_fingerprint if stored else 'nein'}")
        if p.keyfile:
            print(f"Eigene Schlüsseldatei: {p.keyfile}")
        return 0
    if not a.dest:
        raise CliError("Zieldatei angeben: pbsgui key export NAME ZIELDATEI")
    if Path(a.dest).exists():
        raise CliError(f"{a.dest} existiert bereits")
    mgr.export_key(p, a.dest)
    print(f"Schlüssel exportiert nach {a.dest} (Rechte 0600). An einem sicheren Ort aufbewahren.")
    return 0


def cmd_remove(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    if not a.yes:
        if not sys.stdin.isatty() or input(f"Profil '{p.name}' samt Zeitplan und Geheimnissen entfernen? [j/N] ").strip().lower() not in ("j", "y"):
            raise CliError("Abgebrochen (zum Bestätigen --yes nutzen)")
    mgr.delete(p)
    print(f"Profil '{p.name}' entfernt. Der Zugang auf dem PBS-Server bleibt bestehen.")
    return 0


def cmd_tui(a, mgr) -> int:
    from ..tui.app import run
    run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pbsgui", description="Proxmox-Backup-Client-Oberfläche. Ohne Befehl: GUI.")
    sub = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_, *names):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        for n in names:
            sp.add_argument(n)
        return sp

    sp = add("list", cmd_list, "Profile anzeigen"); sp.add_argument("--json", action="store_true")
    add("show", cmd_show, "Profil im Detail", "name")
    for nm, fn, h in (("add", cmd_add, "Server hinzufügen (Weg A: root, Weg B: --token-id)"),
                      ("probe", cmd_probe, "Datastores/Namespaces eines Servers anzeigen")):
        sp = sub.add_parser(nm, help=h); sp.set_defaults(fn=fn)
        if nm == "add":
            sp.add_argument("--name", required=True)
        sp.add_argument("--server", required=True)
        sp.add_argument("--port", type=int, default=8007)
        sp.add_argument("--fingerprint", help="bekannter SHA-256-Fingerprint; sonst wird er abgerufen und bestätigt")
        sp.add_argument("--yes", action="store_true", help="abgerufenen Fingerprint ohne Rückfrage akzeptieren")
        sp.add_argument("--token-id", help="Weg B: vorhandener Token (Secret per Prompt/PBSGUI_TOKEN_SECRET)")
        sp.add_argument("--root-user", default="root@pam", help="Weg A (Standard): einmaliger Root-Login")
        sp.add_argument("--tfa", help="2FA-Code für den Root-Login")
        if nm == "add":
            sp.add_argument("--datastore", required=True)
            sp.add_argument("--namespace", default="")
            sp.add_argument("--mode", choices=[HOME, FULL], default=HOME,
                            help="Umfang: home = nur Home-Verzeichnis (Standard), full = ganzes System (braucht Admin)")
            sp.add_argument("--source", action="append", help="Quellordner (mehrfach; Standard: Home; nur mit --mode home)")
            sp.add_argument("--exclude", action="append", help="Ausschlussmuster (mehrfach)")
            sp.add_argument("--backup-id", help="Standard: Hostname")
            sp.add_argument("--no-encrypt", action="store_true", help="clientseitige Verschlüsselung AUS (nicht empfohlen)")
            sp.add_argument("--scope", choices=["user", "system"], default="user")
            sp.add_argument("--replace-token", action="store_true", help="vorhandenen Token löschen und neu erzeugen")
            sp.add_argument("--export-key", metavar="DATEI", help="erzeugten Schlüssel gleich exportieren")
    add("backup", cmd_backup, "Backup jetzt ausführen", "name")
    sp = add("snapshots", cmd_snapshots, "Snapshots auflisten", "name"); sp.add_argument("--json", action="store_true")
    sp = add("restore", cmd_restore, "Snapshot wiederherstellen", "name")
    sp.add_argument("--snapshot", default="0", help="Index aus `snapshots` oder Snapshot-Pfad (Standard: neuester)")
    sp.add_argument("--archive"); sp.add_argument("--target", required=True)
    sp.add_argument("--force", action="store_true", help="auch in nicht leeren Zielordner")
    sp = add("schedule", cmd_schedule, "systemd-Zeitplan setzen/entfernen", "name")
    sp.add_argument("--calendar", help="OnCalendar-Ausdruck, z. B. daily")
    sp.add_argument("--scope", choices=["user", "system"])
    sp.add_argument("--require-ac", action=argparse.BooleanOptionalAction, default=None)
    sp.add_argument("--remove", action="store_true")
    add("status", cmd_status, "Zeitplan- und Laufstatus", "name")
    sp = add("key", cmd_key, "Verschlüsselungs-Schlüssel anzeigen/exportieren", "action")
    sp.add_argument("name"); sp.add_argument("dest", nargs="?")
    sp = add("remove", cmd_remove, "Profil entfernen", "name"); sp.add_argument("--yes", action="store_true")
    add("tui", cmd_tui, "Terminal-Oberfläche starten")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:  # ohne Argumente: GUI
        try:
            from ..gui.main import main as gui_main
        except ImportError:
            print("Die GUI braucht PySide6: pip install 'pbsgui[gui]'. Befehle siehe `pbsgui --help`.", file=sys.stderr)
            return 1
        return gui_main()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 1
    try:
        return args.fn(args, ProfileManager())
    except CliError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return e.code
    except (ApiError, ClientError, SchedulerError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
