"""`pbsgui versions | find | diff | preview | restore-test | dashboard | history` – Blick über alle Snapshots und Läufe.

Die Darstellung (Texte, Tabellen) liegt hier, damit auch die Terminal-Oberfläche sie nutzen kann.
"""
from __future__ import annotations

import json
import sys
import time

from ..i18n import tr
from ..core.dashboard import DatastoreUsage, Overview
from ..core.files import FilesError
from ..core.history import RunRecord, fmt_duration
from ..core.manager import ProfileManager
from ..core.models import Profile, Snapshot, logical_archive
from ..core.versions import DiffResult, RestoreTestResult
from .main import CliError, _pick_snapshot, find_profile, fmt_size, fmt_time, table

STATE_TEXT = {"ok": "OK", "error": tr("FEHLER"), "running": tr("läuft"), "warning": tr("Warnung"), "none": "-"}


def signed(n: int | None) -> str:
    if n is None:
        return "-"
    return ("+" if n > 0 else "-" if n < 0 else "±") + fmt_size(abs(n))


def ago(ts: int | None, now: float | None = None) -> str:
    if ts is None:
        return "-"
    d = (now or time.time()) - ts
    if d < 90:
        return tr("gerade eben")
    if d < 5400:
        return tr("vor {0} min", int(d // 60))
    if d < 129600:
        return tr("vor {0} h", int(d // 3600))
    return tr("vor {0} Tagen", int(d // 86400))


# -- Darstellung ---------------------------------------------------------------------------------------
def overview_row(ov: Overview) -> list[str]:
    run = ov.last_run
    return [ov.profile.name, STATE_TEXT[ov.state], ago(ov.last_backup) if ov.last_backup else "-",
            fmt_duration(run.duration) if run else "-", fmt_size(ov.size) if ov.last_backup else "-",
            signed(ov.growth_last), signed(ov.growth_30d), str(ov.snapshots)]


OVERVIEW_HEADER = [tr("Profil"), "Status", tr("Letztes Backup"), tr("Dauer"), tr("Größe"), tr("Zuwachs"), tr("30 Tage"), "Snapshots"]


def usage_text(profile: Profile, u: DatastoreUsage | None) -> str:
    where = f"{profile.server}/{profile.datastore}"
    if u is None:
        return tr("Datastore {0}: Belegung nicht abrufbar (der Token braucht Leserechte auf dem Datastore)", where)
    text = tr("Datastore {0}: {1} von {2} belegt ({3:.0f} %), {4} frei", where, fmt_size(u.used), fmt_size(u.total), u.percent, fmt_size(u.avail))
    if u.dedup:
        text += tr(", Deduplizierung {0:.1f}×", u.dedup)
    return text


def overview_details(ov: Overview) -> list[str]:
    out = []
    if ov.last_run is not None:
        r = ov.last_run
        out.append(tr("{0}: letzter Lauf {1}, {2}, ", ov.profile.name, fmt_time(r.start), fmt_duration(r.duration))
                   + ("erfolgreich" if r.ok else tr("FEHLGESCHLAGEN ({0})", r.message)))
    if ov.logical_total and ov.snapshots > 1:
        out.append(tr("{0}: {1} Snapshots, zusammen {2} logisch (ohne Deduplizierung; der tatsächliche Platz ist deutlich kleiner)", ov.profile.name, ov.snapshots, fmt_size(ov.logical_total)))
    if ov.error:
        out.append(tr("{0}: Server nicht erreichbar: {1}", ov.profile.name, ov.error))
    out += [tr("{0}: Warnung: {1}", ov.profile.name, w) for w in ov.warnings]
    return out


def run_row(r: RunRecord, names: dict[str, str] | None = None) -> list[str]:
    return [r.id, fmt_time(r.start), (names or {}).get(r.profile, r.profile), "OK" if r.ok else tr("FEHLER"),
            fmt_duration(r.duration) + (" (data)" if r.mode == "data" else ""), r.message]


RUN_HEADER = [tr("Lauf"), "Start", tr("Profil"), tr("Ergebnis"), tr("Dauer"), tr("Meldung")]


def diff_lines(res: DiffResult, limit: int = 200) -> list[str]:
    out = []
    marks = {"added": "+", "removed": "-", "changed": "~"}
    for change in ("added", "changed", "removed"):
        items = res.of(change)
        for it in items[:limit]:
            extra = ""
            if it.kind == "d":
                extra = tr("  ({0} Dateien, {1})", it.files, fmt_size(it.size))
            elif change == "changed":
                extra = f"  ({fmt_size(it.old.size or 0)} → {fmt_size(it.new.size or 0)})"      # type: ignore[union-attr]
            elif it.size:
                extra = f"  ({fmt_size(it.size)})"
            out.append(f"{marks[change]} {it.path}{'/' if it.kind == 'd' else ''}{extra}")
        if len(items) > limit:
            out.append(tr("  … {0} weitere {1} Einträge", len(items) - limit, ('neue', 'geänderte', 'gelöschte')[('added', 'changed', 'removed').index(change)]))
    return out


def restore_test_lines(res: RestoreTestResult) -> list[str]:
    icons = {"ok": "OK       ", "gelesen": "gelesen  ", "abweichung": "ABWEICHUNG", "groesse": "GRÖSSE   ", "fehler": "FEHLER   "}
    lines = [f"{icons.get(s.status, s.status)} {s.path} ({fmt_size(s.size)}) – {s.detail}" for s in res.samples]
    return [*lines, res.summary()]


# -- Hilfen --------------------------------------------------------------------------------------------
def _progress(label: str):
    if not sys.stderr.isatty():
        return None

    def show(done: int, total: int) -> None:
        sys.stderr.write(f"\r{label} {done}/{total}" + ("\n" if done >= total else ""))
        sys.stderr.flush()
    return show


def _archives_of(snap: Snapshot, wanted: str | None) -> list[str]:
    names = [a for a in snap.archives if a != "recovery.pxar"]
    if wanted:
        name = logical_archive(wanted) or wanted
        if name not in names:
            raise CliError(tr("Archiv nicht gefunden: {0}. Vorhanden: {1}", wanted, ', '.join(names)))
        return [name]
    if not names:
        raise CliError(tr("Dieser Snapshot enthält keine Datei-Archive"))
    return names


# -- Befehle -------------------------------------------------------------------------------------------
def cmd_versions(a, mgr: ProfileManager) -> int:
    p = find_profile(mgr, a.name)
    res = mgr.file_versions(p, a.path, a.archive, _progress(tr("Snapshots prüfen")))
    if a.json:
        print(json.dumps({"archive": res.archive, "path": res.path, "checked": res.checked, "versions": [
            {"number": i + 1, "size": v.entry.size, "mtime": v.entry.mtime, "snapshots": [s.path for s in v.snapshots]}
            for i, v in enumerate(res.versions)]}, indent=2))
        return 0
    for err in res.errors:
        print(tr("Warnung: {0}", err), file=sys.stderr)
    if not res.versions:
        raise CliError(tr("{0} kommt in keinem der {1} Snapshots vor (suchen mit: pbsgui find {2} NAME)", res.path, res.checked, p.name))
    print(tr("{0} in {1}: {2} Fassung(en), {3} Snapshots geprüft", res.path, res.archive, len(res.versions), res.checked))
    rows = [[str(i + 1), v.entry.mtime or "-", fmt_size(v.entry.size or 0), str(len(v.snapshots)), v.newest.path, v.oldest.path]
            for i, v in enumerate(res.versions)]
    print(table(rows, ["Nr.", tr("Geändert"), tr("Größe"), "Snapshots", tr("Neuester"), tr("Ältester")]))
    if a.restore:
        if not 1 <= a.restore <= len(res.versions):
            raise CliError(tr("Fassung {0} gibt es nicht (1..{1})", a.restore, len(res.versions)))
        if not a.target:
            raise CliError(tr("--restore braucht --target ZIELORDNER"))
        v = res.versions[a.restore - 1]
        try:
            r = mgr.restore_files(p, v.newest, res.archive, [res.path], target=a.target, on_line=lambda l: print(l, flush=True))
        except FilesError as e:
            raise CliError(str(e))
        print(tr("Fassung {0} aus {1} wiederhergestellt: {2} Datei(en) nach {3}", a.restore, v.newest.path, r.copied, a.target))
        for s_ in r.skipped:
            print(tr("  übersprungen: {0}", s_))
    else:
        print(tr("\nHolen: pbsgui versions {0} {1} --restore NR --target ZIELORDNER", p.name, res.path))
    return 0


def cmd_find(a, mgr: ProfileManager) -> int:
    p = find_profile(mgr, a.name)
    res = mgr.search_all(p, a.pattern, a.archive, _progress(tr("Snapshots durchsuchen")))
    if a.json:
        print(json.dumps({"checked": res.checked, "truncated": res.truncated, "hits": [
            {"archive": h.archive, "path": h.path, "kind": h.kind, "versions": h.versions, "in_newest": h.in_newest,
             "snapshots": [s.path for s in h.snapshots]} for h in res.hits]}, indent=2))
        return 0
    for err in res.errors:
        print(tr("Warnung: {0}", err), file=sys.stderr)
    rows = [[h.path + ("/" if h.kind == "d" else ""), h.archive, str(len(h.snapshots)), str(h.versions),
             fmt_time(h.snapshots[0].backup_time), tr("ja") if h.in_newest else tr("NEIN (gelöscht)")] for h in res.hits]
    print(tr("{0} Treffer in {1} Snapshots", len(res.hits), res.checked) + (tr(" (Anzeige begrenzt)") if res.truncated else ""))
    if rows:
        print(table(rows, [tr("Pfad"), tr("Archiv"), "Snapshots", tr("Fassungen"), tr("Zuletzt gesehen"), tr("Im neuesten")]))
        print(tr("\nAlle Fassungen einer Datei: pbsgui versions {0} PFAD", p.name))
    return 0


def cmd_diff(a, mgr: ProfileManager) -> int:
    p = find_profile(mgr, a.name)
    old, new = _pick_snapshot(mgr, p, a.old), _pick_snapshot(mgr, p, a.new)
    if old.backup_time > new.backup_time:
        old, new = new, old                                  # immer älter -> neuer vergleichen
    results = mgr.diff(p, old, new, a.archive)
    if a.json:
        print(json.dumps({"old": old.path, "new": new.path, "archives": [
            {"archive": r.archive, "counts": r.counts, "items": [
                {"change": i.change, "path": i.path, "kind": i.kind, "files": i.files, "size": i.size} for i in r.items]}
            for r in results]}, indent=2))
        return 0
    print(tr("Vergleich: {0}  →  {1}", old.path, new.path))
    for r in results:
        c = r.counts
        print(tr("\n{0}: {1} neu, {2} geändert, {3} gelöscht", r.archive, c['added'], c['changed'], c['removed']))
        for line in diff_lines(r, a.limit):
            print(line)
    return 0


def cmd_preview(a, mgr: ProfileManager) -> int:
    p = find_profile(mgr, a.name)
    snap = _pick_snapshot(mgr, p, a.snapshot)
    archive = _archives_of(snap, a.archive)[0] if (a.archive or len(_archives_of(snap, None)) == 1) else None
    if archive is None:
        raise CliError(tr("Mehrere Archive, bitte --archive wählen: ") + ", ".join(_archives_of(snap, None)))
    with mgr.preview_session(p, snap, archive) as ps:
        pv = ps.preview("/" + a.path.strip("/"), a.bytes)
    if pv.kind in ("text", "binary"):
        print(pv.text.rstrip("\n"))
        if pv.truncated:
            print(tr("\n… gekürzt, Datei hat {0}", fmt_size(pv.size)), file=sys.stderr)
    elif pv.kind == "image":
        print(tr("Bild, {0} (Vorschau in GUI; speichern mit `pbsgui restore`)", fmt_size(pv.size)))
    elif pv.kind == "link":
        print(tr("Symbolischer Link auf {0}", pv.text))
    else:
        raise CliError(pv.note or pv.kind)
    return 0


def cmd_restore_test(a, mgr: ProfileManager) -> int:
    p = find_profile(mgr, a.name)
    snap = _pick_snapshot(mgr, p, a.snapshot)
    ok, out = True, []
    for archive in _archives_of(snap, a.archive):
        if not a.json:
            print(tr("Restore-Test: {0} / {1}, {2} zufällige Dateien …", snap.path, archive, a.count), flush=True)
        res = mgr.run_restore_test(p, snap, archive, a.count)
        ok &= res.ok
        out.append(res)
        if not a.json:
            for line in restore_test_lines(res):
                print(line)
    if a.json:
        print(json.dumps([r.to_dict() for r in out], indent=2))
    return 0 if ok else 1


def cmd_dashboard(a, mgr: ProfileManager) -> int:
    profiles = [find_profile(mgr, a.name)] if a.name else mgr.profiles()
    if not profiles:
        print(tr("Keine Profile. Mit `pbsgui add` einen Server hinzufügen."))
        return 0
    ovs = [mgr.overview(p, online=not a.offline) for p in profiles]
    usages: dict[tuple, DatastoreUsage | None] = {}
    if not a.offline:
        for p in profiles:
            key = (p.server, p.port, p.datastore)
            if key not in usages:
                usages[key] = mgr.usage(p)
    if a.json:
        print(json.dumps([{"profile": o.profile.name, "state": o.state, "last_backup": o.last_backup, "size": o.size,
                           "growth_last": o.growth_last, "growth_30d": o.growth_30d, "snapshots": o.snapshots,
                           "logical_total": o.logical_total, "running": o.running, "warnings": o.warnings,
                           "last_run": o.last_run.to_dict() | {"log": []} if o.last_run else None,
                           "datastore": (lambda u: u and {"total": u.total, "used": u.used, "avail": u.avail, "dedup": u.dedup})(
                               usages.get((o.profile.server, o.profile.port, o.profile.datastore)))} for o in ovs], indent=2))
        return 0
    print(table([overview_row(o) for o in ovs], OVERVIEW_HEADER))
    shown = set()
    print()
    for o in ovs:
        for line in overview_details(o):
            print(line)
        key = (o.profile.server, o.profile.port, o.profile.datastore)
        if key in usages and key not in shown:
            shown.add(key)
            print(usage_text(o.profile, usages[key]))
        t = mgr.last_restore_test(o.profile)
        if t:
            print(tr("{0}: letzter Restore-Test {1}: {2}", o.profile.name, fmt_time(t['time']), t['summary']))
        else:
            print(tr("{0}: noch kein Restore-Test (pbsgui restore-test {1})", o.profile.name, o.profile.name))
    return 0


def cmd_history(a, mgr: ProfileManager) -> int:
    p = find_profile(mgr, a.name) if a.name else None
    names = {x.slug: x.name for x in mgr.profiles()}
    if a.show:
        r = mgr.history_run(a.show, p)
        if r is None:
            raise CliError(tr("Lauf nicht gefunden: {0}", a.show))
        print(tr("Lauf {0}: {1}, {2}, {3}, {4} ({5}), Exitcode {6}, Scope {7}\n", r.id, names.get(r.profile, r.profile), fmt_time(r.start), fmt_duration(r.duration), 'OK' if r.ok else tr('FEHLER'), r.message, r.code, r.scope))
        print("\n".join(r.log))
        return 0
    since = time.time() - a.days * 86400 if a.days else None
    runs = mgr.history(p, failed_only=a.failed, text=a.search or "", since=since, limit=a.limit)
    if a.json:
        print(json.dumps([r.to_dict() | {"log": r.log[-5:]} for r in runs], indent=2))
        return 0
    if not runs:
        print(tr("Keine passenden Läufe."))
        return 0
    print(table([run_row(r, names) for r in runs], RUN_HEADER))
    print(tr("\nAusgabe eines Laufs: pbsgui history --show LAUF"))
    return 0


def add_arguments(sub) -> None:
    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("versions", cmd_versions, tr("Alle Fassungen einer Datei aus allen Snapshots zeigen (und eine holen)"))
    sp.add_argument("name"); sp.add_argument("path", help=tr("Pfad innerhalb des Archivs, z. B. /Dokumente/a.txt"))
    sp.add_argument("--archive"); sp.add_argument("--json", action="store_true")
    sp.add_argument("--restore", type=int, metavar="NR", help=tr("Fassung NR (aus der Liste) wiederherstellen; braucht --target"))
    sp.add_argument("--target", help=tr("Zielordner für --restore (nichts wird überschrieben)"))
    sp = add("find", cmd_find, tr("Dateien in ALLEN Snapshots suchen (auch längst gelöschte)"))
    sp.add_argument("name"); sp.add_argument("pattern", help=tr("Namensteil oder Muster mit * und ?"))
    sp.add_argument("--archive"); sp.add_argument("--json", action="store_true")
    sp = add("diff", cmd_diff, tr("Zwei Snapshots vergleichen: neu, geändert, gelöscht"))
    sp.add_argument("name"); sp.add_argument("old", nargs="?", default="1", help=tr("Index oder Pfad (Standard: 1, der vorherige)"))
    sp.add_argument("new", nargs="?", default="0", help=tr("Index oder Pfad (Standard: 0, der neueste)"))
    sp.add_argument("--archive"); sp.add_argument("--limit", type=int, default=200, help=tr("Zeilen je Art (Standard 200)"))
    sp.add_argument("--json", action="store_true")
    sp = add("preview", cmd_preview, tr("Inhalt einer Datei im Snapshot ansehen (Text, Hexdump), ohne wiederherzustellen"))
    sp.add_argument("name"); sp.add_argument("path")
    sp.add_argument("--snapshot", default="0"); sp.add_argument("--archive")
    sp.add_argument("--bytes", type=int, default=65536, help=tr("höchstens so viele Bytes lesen (Standard 65536)"))
    sp = add("restore-test", cmd_restore_test, tr("Zufällige Dateien wiederherstellen und prüfen (Exitcode 1 bei Fehler)"))
    sp.add_argument("name"); sp.add_argument("--snapshot", default="0"); sp.add_argument("--archive")
    sp.add_argument("--count", type=int, default=5, help=tr("Zahl der Dateien je Archiv (Standard 5)"))
    sp.add_argument("--json", action="store_true")
    sp = add("dashboard", cmd_dashboard, tr("Überblick: letztes Backup, Dauer, Größe, Zuwachs, Speicher im Datastore"))
    sp.add_argument("name", nargs="?"); sp.add_argument("--json", action="store_true")
    sp.add_argument("--offline", action="store_true", help=tr("keine Serverabfrage, nur lokaler Verlauf"))
    sp = add("history", cmd_history, tr("Verlauf der Backup-Läufe mit Ausgabe, filterbar"))
    sp.add_argument("name", nargs="?"); sp.add_argument("--failed", action="store_true", help=tr("nur fehlgeschlagene Läufe"))
    sp.add_argument("--search", metavar="TEXT", help=tr("Text in Meldung oder Ausgabe"))
    sp.add_argument("--days", type=int, help=tr("nur die letzten N Tage")); sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--show", metavar="LAUF", help=tr("Ausgabe eines Laufs anzeigen")); sp.add_argument("--json", action="store_true")
