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
from ..core.dbdump import MARIADB, DumpError, find_databases, run_dumps
from ..core.files import FilesError, map_to_original
from ..core.manager import ProfileManager, can_use_admin
from ..core.models import Profile, slugify
from ..core.modes import FULL, HOME, describe, detect_mounts, mount_choices, plan_mounts
from ..core.onboarding import (connect_root, connect_token, namespace_message, namespace_state,
                               provision_and_verify, verify_token_profile)
from ..core.paths import Paths
from ..core.recovery import collect_recovery, missing_recovery_tools, needs_key_export, render_sheet
from ..core.pbs_api import ApiError, TokenExistsError, fetch_fingerprint
from ..core.runner import read_latest_status
from ..core.scheduler import SchedulerError, next_elapse, validate_calendar


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
    if a.schedule:
        try:
            validate_calendar(a.schedule)  # vor dem Anlegen des Zugangs: ein Tippfehler soll nichts halb einrichten
        except SchedulerError as e:
            raise CliError(f"Zeitplan ungültig: {e}") from None
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
    full_mounts = None
    if a.mode == FULL:
        if a.source:
            raise CliError("--source passt nicht zu --mode full (das ganze System wird gesichert)")
        sources, scope = ["/"], "system"
        if a.mount is not None:
            full_mounts = _validate_mounts(a.mount)
    else:
        if a.mount:
            raise CliError("--mount gilt nur mit --mode full")
        sources, scope = a.source or [str(Path.home())], a.scope
    profile = Profile(name=a.name, server=a.server, port=a.port, datastore=a.datastore, namespace=ns,
                      auth_id=auth_id, fingerprint=fp, backup_id=hostname, sources=sources,
                      excludes=a.exclude or [], encrypt=not a.no_encrypt, scope=scope, backup_mode=a.mode,
                      full_mounts=full_mounts, schedule=a.schedule or "", db_dump=a.db_dump)
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
    if profile.schedule:
        if mgr.activate_pending_schedule(profile):
            print(f"Zeitplan aktiv ({profile.scope}): {profile.schedule}")
        else:
            print(f"Zeitplan '{profile.schedule}' ist vermerkt und startet, sobald der Schlüssel exportiert ist: "
                  f"pbsgui key export {profile.name} <ZIELDATEI>")
    if profile.db_dump:
        print(f"Datenbank-Dumps sind an. Erkannte Datenbanken und Zugänge: pbsgui dbdump {profile.name}")
    if profile.backup_mode == FULL:
        print("Hinweis: Dateibasiertes Vollbackup, kein Systemabbild. Es läuft mit Administrator-Rechten (Modus system).")
        print(_mounts_summary(profile))
        for tool, pkg in missing_recovery_tools().items():
            print(f"WARNUNG: {tool} fehlt (Paket {pkg}): ohne Partitionstabelle ist keine Wiederherstellung auf einem "
                  f"neuen PC möglich. Installieren: apt install {pkg}")
    if a.backup_now:
        if needs_key_export(profile):
            print(f"\nDas erste Backup startet noch nicht: erst den Schlüssel sichern (pbsgui key export {profile.name} "
                  f"<ZIELDATEI>), dann pbsgui backup {profile.name}.")
            return 0
        print(f"\nStarte das erste Backup von '{profile.name}' …")
        return cmd_backup(argparse.Namespace(name=profile.name), mgr)
    return 0


def _validate_mounts(paths: list[str]) -> list[str]:
    ok = {m.mountpoint for m in detect_mounts() if not m.bind}
    unknown = [p for p in paths if p not in ok]
    if unknown:
        raise CliError("Kein auswählbares Laufwerk eingehängt unter: " + ", ".join(unknown) +
                       ". Siehe `pbsgui mounts`.")
    return paths


def _mounts_summary(p: Profile) -> str:
    chosen, binds = plan_mounts(p.full_mounts)
    text = "Mitgesicherte Laufwerke: " + (", ".join(chosen) if chosen else "keine (nur das Root-Dateisystem)")
    return text + (f"\nBind-Mount-Duplikate (übersprungen): {', '.join(binds)}" if binds else "")


def cmd_mounts(a, mgr) -> int:
    """Erkannte Laufwerke anzeigen; mit NAME und --set/--auto/--none die Auswahl eines Vollbackup-Profils ändern."""
    p = find_profile(mgr, a.name) if a.name else None
    if a.set is not None or a.auto or a.none:
        if not p or p.backup_mode != FULL:
            raise CliError("Die Auswahl gibt es nur für ein Profil mit Vollbackup (NAME angeben)")
        p.full_mounts = None if a.auto else ([] if a.none else _validate_mounts(a.set))
        mgr.save(p)
        print("Auswahl gespeichert.")
    selected = None if not p else p.full_mounts
    rows = [[m.mountpoint, m.fstype, m.kind, "ja" if m.default_selected else "nein", ("ja" if on else "nein")]
            for m, on in mount_choices(selected)]
    header = ["Einhängepunkt", "Typ", "Art", "Standard", "Gesichert" if p else "Würde gesichert"]
    print(table(rows, header) if rows else "Keine weiteren lokalen Laufwerke erkannt (nur das Root-Dateisystem).")
    if p:
        print("\n" + _mounts_summary(p))
    return 0


def cmd_backup(a, mgr) -> int:
    from ..core.progress import decode, describe
    p = find_profile(mgr, a.name)
    tty = sys.stdout.isatty()
    state = {"line": False, "last_pct": -10}

    def on_line(line: str) -> None:
        prog = decode(line)
        if prog is None:
            if state["line"] and tty:          # laufende Fortschrittszeile vor normalem Text abschließen
                print()
                state["line"] = False
            print(line, flush=True)
            return
        text = describe(prog)
        if tty:                                # eine sich aktualisierende Zeile
            print("\r\033[K" + text, end="", flush=True)
            state["line"] = True
        elif prog.percent is not None and prog.percent - state["last_pct"] >= 10:   # Pipe/Log: alle 10 %
            state["last_pct"] = prog.percent
            print(text, flush=True)

    code = mgr.run_backup(p, on_line=on_line)
    if state["line"] and tty:
        print()
    return code


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


def _pick_snapshot(mgr, p: Profile, spec: str):
    snaps = mgr.snapshots(p)
    if not snaps:
        raise CliError("Keine Snapshots vorhanden")
    if spec.isdigit():
        if int(spec) >= len(snaps):
            raise CliError(f"Snapshot #{spec} existiert nicht (0..{len(snaps) - 1})")
        return snaps[int(spec)]
    snap = next((s for s in snaps if s.path == spec), None)
    if not snap:
        raise CliError(f"Snapshot nicht gefunden: {spec}")
    return snap


def _pick_archive(cats: dict, wanted: str | None) -> str:
    names = sorted(n for n in cats if n != "recovery.pxar")
    if wanted:
        name = wanted.removesuffix(".didx")
        if name not in cats:
            raise CliError(f"Archiv nicht gefunden: {wanted}. Vorhanden: {', '.join(names)}")
        return name
    if len(names) == 1:
        return names[0]
    raise CliError("Mehrere Archive, bitte --archive wählen: " + ", ".join(names))


def cmd_files(a, mgr) -> int:
    """Dateien eines Snapshots auflisten/suchen (File-Level-Restore: Auswahl dann mit `restore --path`)."""
    p = find_profile(mgr, a.name)
    snap = _pick_snapshot(mgr, p, a.snapshot)
    cats = mgr.catalogs(p, snap)
    archive = _pick_archive(cats, a.archive)
    cat = cats[archive]
    print(f"Snapshot {snap.path}, Archiv {archive}")
    if a.search:
        rows = cat.search(a.search)
        print(f"{len(rows)} Treffer" + (" (Anzeige begrenzt)" if len(rows) >= 500 else ""))
    else:
        path = "/" + a.path.strip("/") if a.path else "/"
        if path != "/" and path not in cat.by_path:
            raise CliError(f"Pfad nicht im Archiv: {path}")
        rows = cat.children(path)
    for e in rows:
        shown = e.path if a.search else e.name
        print(f"{e.kind}  {fmt_size(e.size or 0) if e.size is not None else '':>10}  {e.mtime[:10]:10}  {shown}"
              + ("/" if e.kind == "d" else ""))
    return 0


def _restore_selected(a, mgr, p: Profile) -> int:
    snap = _pick_snapshot(mgr, p, a.snapshot)
    cats = mgr.catalogs(p, snap)
    archive = _pick_archive(cats, a.archive)
    cat = cats[archive]
    rels = ["/" + x.strip("/") for x in a.path]
    missing = [r for r in rels if r != "/" and r not in cat.by_path]
    if missing:
        raise CliError("Nicht im Archiv: " + ", ".join(missing) + f" (suchen mit: pbsgui files {p.name} --search NAME)")
    files, size = cat.count(rels)
    print(f"Auswahl: {len(rels)} Pfad(e), {files} Dateien, {fmt_size(size)}")
    if a.overwrite:
        try:
            items = map_to_original(p, archive, rels)
        except FilesError as e:
            raise CliError(str(e))
        outside = [i for i in items if not i.in_home]
        if outside:
            raise CliError("Überschreiben ist nur im Home-Verzeichnis erlaubt. Nicht erlaubt: "
                           + ", ".join(str(i.dest) for i in outside))
        existing = sum(1 for i in items if i.exists)
        print(f"Am Originalort überschreiben: {len(items)} Pfad(e), davon {existing} schon vorhanden. "
              "Überschriebene Dateien werden vorher nach ~/.local/share/pbsgui/overwritten/<Zeit>/ gesichert.")
        if not a.yes:
            if not sys.stdin.isatty() or input("Wirklich überschreiben? [j/N] ").strip().lower() not in ("j", "y"):
                raise CliError("Abgebrochen (zum Bestätigen --yes nutzen)", 2)
    elif not a.target:
        raise CliError("--target ZIELORDNER oder --overwrite angeben")
    try:
        res = mgr.restore_files(p, snap, archive, rels, target=a.target, overwrite_home=a.overwrite,
                                on_line=lambda l: print(l, flush=True))
    except FilesError as e:
        raise CliError(str(e))
    print(f"{res.copied} Dateien wiederhergestellt ({fmt_size(res.bytes)})."
          + (f" {res.safety_copies} überschriebene Dateien gesichert in {res.safety_dir}" if a.overwrite else ""))
    for s_ in res.skipped:
        print(f"  übersprungen: {s_}")
    return 0


def cmd_restore(a, mgr) -> int:
    p = find_profile(mgr, a.name)
    if a.path:
        return _restore_selected(a, mgr, p)
    if a.overwrite:
        raise CliError("--overwrite braucht --path (nur einzelne Dateien/Ordner aus dem Home, nie ein ganzes Archiv)")
    if not a.target:
        raise CliError("--target ZIELORDNER angeben")
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


def _pick_database(dbs: list, wanted: str):
    hits = [d for d in dbs if wanted in (d.key, d.container)]
    if not hits:
        raise CliError(f"Datenbank-Container nicht gefunden: {wanted}. Erkannt: {', '.join(d.key for d in dbs) or '-'}")
    if len(hits) > 1:
        raise CliError(f"'{wanted}' ist nicht eindeutig, bitte als Besitzer/Container angeben: "
                       f"{', '.join(d.key for d in hits)}")
    return hits[0]


def cmd_dbdump(a, mgr) -> int:
    """Datenbank-Dumps eines Profils: ein/aus, erkannte Container, Zugänge, Testlauf."""
    p = find_profile(mgr, a.name)
    if a.on or a.off:
        p.db_dump = bool(a.on)
        mgr.save(p)
        print(f"Datenbank-Dumps für '{p.name}': {'an' if p.db_dump else 'aus'}")
    try:
        dbs = find_databases(on_line=print)
    except DumpError as e:
        raise CliError(str(e)) from None
    creds = mgr.db_credentials(p)
    if a.set or a.create_user or a.remove:
        if a.remove:
            db = _pick_database(dbs, a.remove)
            print("Zugang entfernt." if mgr.remove_db_credential(p, db.key) else "Für diesen Container war kein Zugang hinterlegt.")
        elif a.create_user:
            db = _pick_database(dbs, a.create_user)
            if db.engine != MARIADB:
                raise CliError("Postgres braucht keinen Zugang")
            pw = secret_from("PBSGUI_DB_ADMIN_PASSWORD", f"Admin-Passwort von {db.key} (wird nicht gespeichert)")
            mgr.create_db_credential(p, db, a.admin_user, pw)
            print(f"Dump-Benutzer für {db.key} angelegt (nur Leserechte) und verschlüsselt hinterlegt.")
        else:
            db = _pick_database(dbs, a.set)
            if db.engine != MARIADB:
                raise CliError("Postgres braucht keinen Zugang")
            if not a.user:
                raise CliError("--user angeben (Passwort per Prompt oder PBSGUI_DB_PASSWORD)")
            mgr.set_db_credential(p, db, a.user, secret_from("PBSGUI_DB_PASSWORD", f"Passwort für {a.user}"))
            print(f"Zugang für {db.key} geprüft und verschlüsselt hinterlegt.")
        return 0
    if a.run:
        dest = Path(a.dest) if a.dest else Paths.for_scope(p.scope).state / "dumps" / p.slug
        res = run_dumps(dest, creds, print)
        print(f"Ziel: {dest}")
        return 1 if res.failed else 0
    if not dbs:
        print("Keine laufenden Datenbank-Container gefunden (oder kein Zugriff auf deren Docker-Socket).")
    for d in dbs:
        if d.engine == MARIADB:
            c = creds.get(d.key)
            state = f"Zugang: {c['user']}" if c else "Zugang FEHLT (pbsgui dbdump NAME --set/--create-user …)"
        else:
            state = "kein Zugang nötig"
        print(f"{d.key:40} {d.engine:9} {state}")
    print(f"Dumps vor jedem Backup: {'an' if p.db_dump else 'aus (pbsgui dbdump NAME --on)'}")
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
        if stored:
            print(f"Extern gesichert: {'ja, am ' + p.key_exported_at if p.key_exported_at else 'NEIN (pbsgui key export ...)'}")
        if p.keyfile:
            print(f"Eigene Schlüsseldatei: {p.keyfile}")
        return 0
    if a.action == "confirm":
        if not a.dest:
            raise CliError("Schlüsseldatei angeben: pbsgui key confirm NAME DATEI")
        mgr.confirm_key_export(p, a.dest)
        print(f"Schlüsseldatei {a.dest} stimmt mit dem Profil überein; Export vermerkt ({p.key_exported_at}).")
        _activate_pending(mgr, p)
        return 0
    if not a.dest:
        raise CliError("Zieldatei angeben: pbsgui key export NAME ZIELDATEI")
    if Path(a.dest).exists():
        raise CliError(f"{a.dest} existiert bereits")
    mgr.export_key(p, a.dest)
    print(f"Schlüssel exportiert nach {a.dest} (Rechte 0600). An einem sicheren Ort aufbewahren.")
    _activate_pending(mgr, p)
    return 0


def _activate_pending(mgr: ProfileManager, p: Profile) -> None:
    if mgr.activate_pending_schedule(p):
        print(f"Zeitplan aktiv ({p.scope}): {p.schedule}")


def cmd_recovery(a, mgr) -> int:
    """Vorbereitung für die Wiederherstellung auf einem neuen PC: Checkliste, Notfallblatt, Metadaten."""
    p = find_profile(mgr, a.name)
    if a.sheet:
        if Path(a.sheet).exists():
            raise CliError(f"{a.sheet} existiert bereits")
        Path(a.sheet).write_text(render_sheet(p))
        os.chmod(a.sheet, 0o600)
        print(f"Notfallblatt gespeichert: {a.sheet} (enthält keine Geheimnisse; ausdrucken und sicher aufbewahren)")
    if a.collect:
        got = collect_recovery(Path(a.collect), p)
        print(f"{len(got.files)} Dateien nach {a.collect} geschrieben.")
        for w in got.warnings:
            print(f"  Warnung: {w}")
    if not (a.sheet or a.collect):
        ok = lambda b: "[x]" if b else "[ ]"
        full = p.backup_mode == FULL
        print(f"Wiederherstellung auf einem neuen PC: Profil '{p.name}'")
        print(f"  {ok(full)} Vollbackup (Dateiebene)" + ("" if full else "  <- nur bei --mode full sinnvoll"))
        if p.encrypt:
            exported = bool(p.key_exported_at or p.keyfile)
            print(f"  {ok(exported)} Verschlüsselungs-Schlüssel extern gesichert"
                  + (f" (exportiert am {p.key_exported_at})" if p.key_exported_at else
                     "  <- PFLICHT: pbsgui key export " + p.name + " <ZIELDATEI>"))
        else:
            print("  [-] Verschlüsselung aus: kein Schlüssel nötig")
        miss = missing_recovery_tools()
        print(f"  {ok(not miss)} Werkzeuge für die Datenträger-Metadaten"
              + (": fehlt " + ", ".join(f"{t} (apt install {pkg})" for t, pkg in miss.items()) if miss else ""))
        print("  [ ] Notfallblatt ausdrucken: pbsgui recovery " + p.name + " --sheet <DATEI>")
        print("  Datenträger-Metadaten werden bei jedem Vollbackup automatisch mitgesichert "
              "(/var/lib/pbsgui/recovery/" + p.slug + "/).")
        if needs_key_export(p):
            print("\nVollbackup und Zeitplan sind gesperrt, bis der Schlüssel exportiert ist.")
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
    from .. import __version__
    ap = argparse.ArgumentParser(prog="pbsgui", description="Proxmox-Backup-Client-Oberfläche. Ohne Befehl: GUI.")
    ap.add_argument("--version", action="version", version=f"pbsgui {__version__}")
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
            sp.add_argument("--mount", action="append", metavar="PFAD",
                            help="nur mit --mode full: genau diese Laufwerke mitsichern (mehrfach; "
                                 "Standard: interne Laufwerke). USB-Laufwerke nur so.")
            sp.add_argument("--scope", choices=["user", "system"], default="user")
            sp.add_argument("--replace-token", action="store_true", help="vorhandenen Token löschen und neu erzeugen")
            sp.add_argument("--export-key", metavar="DATEI", help="erzeugten Schlüssel gleich exportieren")
            sp.add_argument("--schedule", metavar="AUSDRUCK",
                            help="gleich einen Zeitplan einrichten (systemd OnCalendar, z. B. daily oder 00/8:00). "
                                 "Bei einem Vollbackup mit automatischem Schlüssel erst nach `key export`.")
            sp.add_argument("--backup-now", action="store_true",
                            help="gleich nach dem Anlegen das erste Backup starten")
            sp.add_argument("--db-dump", action="store_true",
                            help="Datenbanken laufender Container (Postgres, MariaDB/MySQL) vor dem Backup dumpen")
    sp = sub.add_parser("mounts", help="Erkannte Laufwerke anzeigen / Auswahl eines Vollbackup-Profils ändern")
    sp.set_defaults(fn=cmd_mounts)
    sp.add_argument("name", nargs="?")
    sp.add_argument("--set", action="append", metavar="PFAD", help="genau diese Laufwerke sichern (mehrfach)")
    sp.add_argument("--auto", action="store_true", help="Standardauswahl (interne Laufwerke)")
    sp.add_argument("--none", action="store_true", help="keine weiteren Laufwerke, nur das Root-Dateisystem")
    add("backup", cmd_backup, "Backup jetzt ausführen", "name")
    sp = add("snapshots", cmd_snapshots, "Snapshots auflisten", "name"); sp.add_argument("--json", action="store_true")
    sp = add("files", cmd_files, "Dateien eines Snapshots durchsuchen", "name")
    sp.add_argument("path", nargs="?", help="Ordner im Archiv (Standard: Wurzel)")
    sp.add_argument("--snapshot", default="0"); sp.add_argument("--archive")
    sp.add_argument("--search", metavar="MUSTER", help="Namen suchen (Teilstring oder * ? Platzhalter)")
    sp = add("restore", cmd_restore, "Snapshot oder einzelne Dateien wiederherstellen", "name")
    sp.add_argument("--snapshot", default="0", help="Index aus `snapshots` oder Snapshot-Pfad (Standard: neuester)")
    sp.add_argument("--archive"); sp.add_argument("--target")
    sp.add_argument("--path", action="append", help="nur diese Datei/diesen Ordner aus dem Archiv holen (mehrfach)")
    sp.add_argument("--overwrite", action="store_true",
                    help="mit --path: an den Originalort zurückschreiben und vorhandene Dateien ersetzen "
                         "(NUR im Home-Verzeichnis, alte Fassung wird vorher gesichert)")
    sp.add_argument("--yes", action="store_true", help="--overwrite ohne Rückfrage")
    sp.add_argument("--force", action="store_true", help="auch in nicht leeren Zielordner")
    sp = add("schedule", cmd_schedule, "systemd-Zeitplan setzen/entfernen", "name")
    sp.add_argument("--calendar", help="OnCalendar-Ausdruck, z. B. daily")
    sp.add_argument("--scope", choices=["user", "system"])
    sp.add_argument("--require-ac", action=argparse.BooleanOptionalAction, default=None)
    sp.add_argument("--remove", action="store_true")
    sp = add("dbdump", cmd_dbdump, "Datenbank-Dumps: erkannte Container, Zugänge, ein/aus, Testlauf", "name")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="Dumps vor jedem Backup anlegen")
    g.add_argument("--off", action="store_true", help="keine Dumps mehr anlegen")
    sp.add_argument("--set", metavar="CONTAINER", help="Zugang eines MariaDB/MySQL-Containers hinterlegen (mit --user)")
    sp.add_argument("--user", help="Benutzer für --set (Passwort per Prompt oder PBSGUI_DB_PASSWORD)")
    sp.add_argument("--create-user", metavar="CONTAINER",
                    help="Dump-Benutzer (nur Leserechte) anlegen und hinterlegen; Admin-Passwort per Prompt oder "
                         "PBSGUI_DB_ADMIN_PASSWORD, wird nicht gespeichert")
    sp.add_argument("--admin-user", default="root", help="Admin-Benutzer für --create-user (Standard: root)")
    sp.add_argument("--remove", metavar="CONTAINER", help="hinterlegten Zugang entfernen")
    sp.add_argument("--run", action="store_true", help="Dumps jetzt anlegen (Testlauf)")
    sp.add_argument("--dest", metavar="ORDNER", help="Zielordner für --run (Standard: wie beim Backup)")
    add("status", cmd_status, "Zeitplan- und Laufstatus", "name")
    sp = add("key", cmd_key, "Schlüssel anzeigen / exportieren / bereits gesicherte Datei bestätigen (info|export|confirm)", "action")
    sp.add_argument("name"); sp.add_argument("dest", nargs="?")
    sp = add("remove", cmd_remove, "Profil entfernen", "name"); sp.add_argument("--yes", action="store_true")
    sp = add("recovery", cmd_recovery, "Wiederherstellung auf neuem PC: Checkliste, Notfallblatt", "name")
    sp.add_argument("--sheet", metavar="DATEI", help="Notfallblatt (ohne Geheimnisse) speichern")
    sp.add_argument("--collect", metavar="VERZEICHNIS", help="Datenträger-Metadaten jetzt sammeln (braucht root)")
    from .rescue_cli import add_arguments as add_rescue
    add_rescue(sub)
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
    except (ApiError, ClientError, SchedulerError, DumpError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
