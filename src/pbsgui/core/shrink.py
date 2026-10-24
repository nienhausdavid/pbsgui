"""Wiederherstellung auf einer KLEINEREN Platte: Größen der Partitionen und Logical Volumes neu berechnen.

Regeln (deterministisch, alles landet im Plan und wird im Assistenten als alt -> neu angezeigt):
- EFI-System-Partition, /boot, BIOS-Boot und unbekannte Partitionen behalten ihre Größe.
- Swap (Partition oder LV) wird im Verhältnis Ziel/Original verkleinert, mindestens 1 GiB (nie größer als vorher).
- Alle übrigen Partitionen bzw. LVs teilen sich den Rest im Verhältnis ihrer ursprünglichen Größen, aber nie unter
  "belegter Platz + Reserve" (20 %, mindestens 2 GiB) und nie über ihrer alten Größe. Wer dadurch auf sein Minimum
  fällt, behält es, die anderen teilen den verbleibenden Platz (Wasserstandsverfahren).
- UUIDs, Namen und Reihenfolge bleiben; nur Start/Größe (Partitionstabelle) und die Extents der LVs (LVM-Metadaten)
  ändern sich. Dateisysteme werden ohnehin neu angelegt und füllen ihr Gerät.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from ..i18n import tr

MIB = 2 ** 20
GIB = 2 ** 30
RESERVE_FRACTION = 0.20
RESERVE_MIN = 2 * GIB
SWAP_MIN = GIB
LUKS_OVERHEAD = 16 * MIB     # LUKS2-Header (grobe Obergrenze)
LVM_SLACK = 4 * MIB          # LVM-Metadaten und Rundung
ALIGN_SECTORS = 2048         # 1 MiB bei 512-Byte-Sektoren
GPT_BACKUP_SECTORS = 33      # Sicherungs-Header + Tabelle am Plattenende


def _err(msg: str):
    from .rescue import RescueError
    return RescueError(msg)


def fmt(n: int) -> str:
    return f"{n / GIB:.1f} GiB" if n >= GIB else f"{n / MIB:.0f} MiB"


def min_with_reserve(used: int) -> int:
    """Belegter Platz plus Reserve (20 %, mindestens 2 GiB)."""
    return used + max(int(used * RESERVE_FRACTION), RESERVE_MIN)


def swap_size(orig: int, target_size: int, src_size: int, unit: int) -> int:
    """Swap im Verhältnis Ziel/Original, mindestens 1 GiB, nie größer als im Original."""
    scaled = int(orig * target_size / src_size) // unit * unit
    return min(orig, max(SWAP_MIN, scaled))


@dataclass
class Item:
    key: object
    orig: int
    minimum: int


class TooSmall(Exception):
    def __init__(self, needed: int):
        super().__init__(needed)
        self.needed = needed


def distribute(items: list[Item], available: int, unit: int) -> dict:
    """Verteilt `available` Bytes proportional zu den Originalgrößen; Minima werden eingehalten (Wasserstand)."""
    if available >= sum(i.orig for i in items):
        return {i.key: i.orig for i in items}
    mins = {i.key: min(i.orig, math.ceil(i.minimum / unit) * unit) for i in items}
    if sum(mins.values()) > available:
        raise TooSmall(sum(mins.values()))
    fixed: dict = {}
    free = list(items)
    while True:
        pool = available - sum(fixed.values())
        factor = pool / sum(i.orig for i in free) if free else 0
        low = [i for i in free if i.orig * factor < mins[i.key]]
        if not low or not free:
            break
        for i in low:
            fixed[i.key] = mins[i.key]
            free.remove(i)
    out = dict(fixed)
    for i in free:
        out[i.key] = min(i.orig, int(i.orig * factor) // unit * unit)
    return {i.key: out[i.key] for i in items}


# -- LVM-Textformat ---------------------------------------------------------------------
_TOKEN = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|(-?\d+)|([A-Za-z_][\w.\-]*)|([\[\]{}=,]))')


def _tokens(text: str) -> list:
    pos, out = 0, []
    while pos < len(text):
        if not text[pos:].strip():
            break
        m = _TOKEN.match(text, pos)
        if not m:
            raise _err(tr("LVM-Metadaten nicht lesbar"))
        pos = m.end()
        comment, string, num, ident, punct = m.groups()
        if comment:
            continue
        if string is not None:
            out.append(("s", re.sub(r'\\(.)', r'\1', string[1:-1])))
        elif num is not None:
            out.append(("i", int(num)))
        elif ident is not None:
            out.append(("n", ident))
        else:
            out.append(("p", punct))
    return out


def parse_lvm(text: str) -> dict:
    toks, pos = _tokens(text), [0]

    def value():
        kind, v = toks[pos[0]]
        pos[0] += 1
        if kind == "p" and v == "[":
            lst = []
            while not (toks[pos[0]] == ("p", "]")):
                if toks[pos[0]] == ("p", ","):
                    pos[0] += 1
                    continue
                lst.append(value())
            pos[0] += 1
            return lst
        return v

    def block(top: bool) -> dict:
        d: dict = {}
        while pos[0] < len(toks):
            kind, v = toks[pos[0]]
            if kind == "p" and v == "}":
                pos[0] += 1
                return d
            pos[0] += 1
            if toks[pos[0]] == ("p", "="):
                pos[0] += 1
                d[v] = value()
            elif toks[pos[0]] == ("p", "{"):
                pos[0] += 1
                d[v] = block(False)
            else:
                raise _err(tr("LVM-Metadaten nicht lesbar"))
        return d
    return block(True)


def dump_lvm(d: dict, indent: int = 0) -> str:
    def val(v):
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return str(v)
    pad, out = "\t" * indent, []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f"{pad}{k} {{\n{dump_lvm(v, indent + 1)}{pad}}}\n")
        else:
            out.append(f"{pad}{k} = {val(v)}\n")
    return "".join(out)


@dataclass
class VgGeometry:
    vg: str
    extent: int            # Bytes je Extent
    pe_start: int          # Bytes
    pe_count: int
    dev_size: int          # Bytes
    lvs: dict[str, int]    # LV-Name -> Bytes (Reihenfolge wie in der Datei)
    data: dict             # geparste Datei


def read_vg(text: str, vg: str) -> VgGeometry:
    data = parse_lvm(text)
    if vg not in data or not isinstance(data[vg], dict):
        raise _err(tr("Volume Group {0} fehlt in den LVM-Metadaten", vg))
    v = data[vg]
    pvs = v.get("physical_volumes") or {}
    if len(pvs) != 1:
        raise _err(tr("Volume Group {0} erstreckt sich über mehrere Platten: 'Größe anpassen' unterstützt nur Volume Groups mit einem einzigen Physical Volume", vg))
    pv = next(iter(pvs.values()))
    ext = int(v["extent_size"]) * 512
    lvs: dict[str, int] = {}
    for name, lv in (v.get("logical_volumes") or {}).items():
        seg = lv.get("segment1") if isinstance(lv, dict) else None
        if int(lv.get("segment_count", 0)) != 1 or not seg or seg.get("type") != "striped" \
                or int(seg.get("stripe_count", 1)) != 1:
            raise _err(tr("Logical Volume {0}/{1} ist nicht linear (Thin-Pool, Snapshot, RAID …): 'Größe anpassen' unterstützt nur einfache LVs", vg, name))
        lvs[name] = int(seg["extent_count"]) * ext
    return VgGeometry(vg, ext, int(pv["pe_start"]) * 512, int(pv["pe_count"]), int(pv["dev_size"]) * 512, lvs, data)


def rewrite_vg(geo: VgGeometry, new_dev_size: int, new_lvs: dict[str, int]) -> str:
    """LVM-Metadaten für ein kleineres Physical Volume: LVs linear und lückenlos hintereinander."""
    v = geo.data[geo.vg]
    pv = next(iter(v["physical_volumes"].values()))
    pv_name = next(iter(v["physical_volumes"]))
    pe_count = (new_dev_size - geo.pe_start) // geo.extent
    pv["dev_size"] = new_dev_size // 512
    pv["pe_count"] = pe_count
    pos = 0
    for name, lv in v["logical_volumes"].items():
        n = new_lvs.get(name, geo.lvs[name]) // geo.extent
        seg = lv["segment1"]
        seg["start_extent"] = 0
        seg["extent_count"] = n
        seg["stripes"] = [pv_name, pos]
        pos += n
    if pos > pe_count:
        raise _err(tr("Interner Fehler: LVs ({0} Extents) passen nicht in das Physical Volume ({1})", pos, pe_count))
    return dump_lvm(geo.data)


# -- Plan -------------------------------------------------------------------------------
@dataclass
class Resize:
    kind: str       # "partition" | "lv"
    name: str       # /dev/vda3 (Quell-Name) bzw. vg/lv
    desc: str       # z. B. "ext4 /"
    old: int
    new: int
    note: str = ""  # "fest", "Minimum", "Swap" …

    def line(self) -> str:
        arrow = tr("unverändert") if self.old == self.new else f"{fmt(self.old)} -> {fmt(self.new)}"
        note = f"  [{self.note}]" if self.note else ""
        return f"{self.name:<16} {self.desc:<18} {arrow}{note}"


@dataclass
class ShrinkResult:
    dump: str                                   # neue sfdisk-Beschreibung (noch mit Quell-Gerätenamen)
    vg_texts: dict[str, str] = field(default_factory=dict)   # VG -> neue Metadaten (nur geänderte)
    resizes: list[Resize] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    min_disk: int = 0


_PART_RE = re.compile(r"^(/dev/\S+)\s*:\s*(.*)$")


def _parse_dump(dump: str):
    head, parts = [], []
    for line in dump.splitlines():
        m = _PART_RE.match(line)
        if m:
            f = m.group(2)
            start, size = re.search(r"start=\s*(\d+)", f), re.search(r"size=\s*(\d+)", f)
            if not start or not size:
                raise _err(tr("Partitionszeile nicht lesbar: {0}", line))
            tm = re.search(r"type=([0-9A-Fa-f-]+)", f)
            parts.append({"path": m.group(1), "fields": f, "start": int(start.group(1)), "size": int(size.group(1)),
                          "type": (tm.group(1).lower() if tm else "")})
        elif not parts:
            head.append(line)
    return head, parts


def _lv_name(vg: str, mapper: str) -> str:
    """Mapper-Name vg--x-lv--y -> lv-y (Bindestriche werden verdoppelt)."""
    esc = vg.replace("-", "--") + "-"
    rest = mapper[len(esc):] if mapper.startswith(esc) else mapper
    return rest.replace("--", "-")


def compute_shrink(rec, src, dump: str, target_size: int, encryption: str = "keep",
                   snapshot_size: int | None = None) -> ShrinkResult:
    from .rescue import _is_data_partition, _part_number
    res = ShrinkResult(dump)
    head, parts = _parse_dump(dump)
    ss = 512
    for line in head:
        m = re.match(r"sector-size:\s*(\d+)", line)
        if m:
            ss = int(m.group(1))
    if any(p["type"] in ("5", "f", "85") for p in parts):
        raise _err(tr("Erweiterte MBR-Partitionen werden bei 'Größe anpassen' nicht unterstützt"))
    unit = ALIGN_SECTORS * ss
    nodes = {_part_number(n.path): n for n in src.children if n.type == "part"}
    for p in parts:
        p["num"] = _part_number(p["path"])
        p["node"] = nodes.get(p["num"])
        p["bytes"] = p["size"] * ss
    parts_sorted = sorted(parts, key=lambda p: p["start"])
    if not parts_sorted:
        raise _err(tr("Die Partitionstabelle enthält keine Partitionen"))

    # belegter Platz je Dateisystem
    def mp_of(n):
        for mp in n.mountpoints:
            real = rec.real_mp(mp)
            if real.startswith("/"):
                return real
        return ""

    def fs_nodes(n):
        return [x for x in n.walk() if x.fstype and x.fstype not in ("crypto_LUKS", "LVM2_member")]

    swap = lambda n: n.fstype == "swap"

    def fixed_partition(p) -> bool:
        n = p["node"]
        if n is None or p["type"] in ("21686148-6449-6e6f-744e-656564454649", "21686148-6449-6E6F-744E-656564454649"):
            return True
        if not n.fstype and not n.children:
            return True
        mps = {rec.real_mp(m) for m in n.mountpoints}
        return n.fstype == "vfat" or bool(mps & {"/boot", "/boot/efi"}) or (n.fstype == "ext4" and mp_of(n) == "/boot")

    flex_fs: list = []     # Dateisysteme, deren Größe sich ändern darf (ohne Swap)
    for p in parts_sorted:
        if not fixed_partition(p) and not swap(p["node"]):
            flex_fs += [x for x in fs_nodes(p["node"]) if not swap(x)]
    used: dict[str, int | None] = {}
    for x in flex_fs:
        mp = mp_of(x)
        used[x.path] = rec.usage.get(mp) if mp else None
    unknown = [x for x in flex_fs if used[x.path] is None]
    if unknown:
        if snapshot_size:
            rest = max(0, snapshot_size - sum(v for v in used.values() if v))
            tot = sum(x.size for x in unknown) or 1
            for x in unknown:
                used[x.path] = int(rest * x.size / tot)
            res.warnings.append(tr("Belegter Platz je Dateisystem unbekannt (Backup ohne fsusage.json): aus der Snapshot-Größe ({0}) geschätzt", fmt(snapshot_size)))
        else:
            for x in unknown:
                used[x.path] = 0
            res.warnings.append(tr("Belegter Platz unbekannt: nur das Minimum von 2 GiB je Dateisystem wird berücksichtigt"))

    # Größen der Partitionsinhalte
    lvm_info: dict[int, dict] = {}   # Partitionsnummer -> {geo, lvs: dict(neu), lv_min}

    def luks_inside(n) -> bool:
        return encryption != "none" and (any(x.fstype == "crypto_LUKS" for x in n.walk()) or
                                         (encryption == "new" and _is_data_partition(n, rec)))

    items: list[Item] = []
    fixed_total, plan_rows = 0, {}
    for p in parts_sorted:
        n = p["node"]
        if fixed_partition(p):
            fixed_total += math.ceil(p["size"] / ALIGN_SECTORS) * ALIGN_SECTORS * ss
            plan_rows[p["num"]] = ("fest", p["bytes"])
        elif swap(n):
            new = swap_size(p["bytes"], target_size, src.size, unit)
            fixed_total += math.ceil(new / unit) * unit
            plan_rows[p["num"]] = ("Swap", new)
        else:
            overhead = LUKS_OVERHEAD if luks_inside(n) else 0
            lvm = next((x for x in n.walk() if x.fstype == "LVM2_member"), None)
            if lvm is not None:
                vg, _ = rec.pvs.get(lvm.path, ("", ""))
                if vg not in rec.vg_files:
                    raise _err(tr("LVM-Metadaten für {0} fehlen in den Wiederherstellungsdaten", lvm.path))
                geo = read_vg(rec.vg_files[vg].read_text(), vg)
                lv_nodes = {_lv_name(vg, x.name): x for x in lvm.walk() if x.type == "lvm"}
                lv_items, fixed_lv = [], {}
                for name, size in geo.lvs.items():
                    x = lv_nodes.get(name)
                    fs = [y for y in x.walk() if y.fstype] if x else []
                    if x is None or not fs:
                        fixed_lv[name] = (size, "fest")
                    elif swap(fs[0]):
                        new = math.ceil(swap_size(size, target_size, src.size, geo.extent) / geo.extent) * geo.extent
                        fixed_lv[name] = (new, "Swap")
                    else:
                        lv_items.append(Item(name, size, min_with_reserve(used.get(fs[0].path) or 0)))
                lv_min = sum(math.ceil(i.minimum / geo.extent) * geo.extent for i in lv_items)
                min_lv_total = lv_min + sum(s for s, _ in fixed_lv.values())
                lvm_info[p["num"]] = {"geo": geo, "items": lv_items, "fixed": fixed_lv, "nodes": lv_nodes,
                                      "overhead": overhead}
                minimum = min_lv_total + geo.pe_start + LVM_SLACK + overhead
            else:
                fs = fs_nodes(n)
                minimum = min_with_reserve(used.get(fs[0].path) or 0 if fs else 0) + overhead + LVM_SLACK
            items.append(Item(p["num"], p["bytes"], min(minimum, p["bytes"])))
    first_start = parts_sorted[0]["start"]
    end = (target_size // ss - GPT_BACKUP_SECTORS) // ALIGN_SECTORS * ALIGN_SECTORS
    available = end * ss - first_start * ss - fixed_total
    try:
        new_sizes = distribute(items, max(available, 0), unit) if items else {}
        if available < 0:
            raise TooSmall(sum(i.minimum for i in items))
    except TooSmall as e:
        needed = first_start * ss + fixed_total + e.needed + GPT_BACKUP_SECTORS * ss + unit
        res.min_disk = needed
        raise _err(tr("Ziel-Platte zu klein, auch mit 'Größe anpassen': {0} vorhanden, mindestens {1} nötig (belegter Platz plus 20 % Reserve, mindestens 2 GiB je Dateisystem; EFI, /boot und Swap-Minimum eingerechnet)", fmt(target_size), fmt(math.ceil(needed / GIB) * GIB))) from None
    for num, size in new_sizes.items():
        plan_rows[num] = ("" if size == next(p["bytes"] for p in parts if p["num"] == num) else "angepasst", size)

    # neue Positionen (lückenlos hintereinander, 1-MiB-ausgerichtet)
    pos, new_geo = first_start, {}
    for p in parts_sorted:
        start = math.ceil(pos / ALIGN_SECTORS) * ALIGN_SECTORS if p is not parts_sorted[0] else p["start"]
        size = plan_rows[p["num"]][1] // ss
        new_geo[p["num"]] = (start, size)
        pos = start + size
    lines = [ln for ln in head if not ln.startswith("last-lba:")] + [""]
    for p in parts:
        start, size = new_geo[p["num"]]
        f = re.sub(r"start=\s*\d+", f"start={start}", p["fields"], count=1)
        f = re.sub(r"size=\s*\d+", f"size={size}", f, count=1)
        lines.append(f"{p['path']} : {f}")
    res.dump = "\n".join(lines) + "\n"

    # LVs je Volume Group
    lv_rows: dict[int, list[Resize]] = {}
    for num, info in lvm_info.items():
        geo, new_part = info["geo"], plan_rows[num][1]
        p = next(q for q in parts if q["num"] == num)
        row_old = p["bytes"]
        dev_size = new_part - info["overhead"]
        pe_count = (dev_size - geo.pe_start) // geo.extent
        fixed_sum = sum(s for s, _ in info["fixed"].values())
        pool = pe_count * geo.extent - fixed_sum
        try:
            new_lvs = dict(distribute(info["items"], pool, geo.extent)) if info["items"] else {}
        except TooSmall as e:
            raise _err(tr("Volume Group {0} ist zu klein ({1} für die LVs, mindestens {2})", geo.vg, fmt(pool), fmt(e.needed))) from None
        new_lvs.update({k: v[0] for k, v in info["fixed"].items()})
        rows = []
        for name, old in geo.lvs.items():
            x = info["nodes"].get(name)
            fs = [y for y in x.walk() if y.fstype][0] if x and any(y.fstype for y in x.walk()) else None
            note = info["fixed"][name][1] if name in info["fixed"] else ("" if new_lvs[name] == old else "angepasst")
            desc = f"{fs.fstype} {mp_of(fs)}".strip() if fs else ""
            rows.append(Resize("lv", f"{geo.vg}/{name}", desc, old, new_lvs[name], note))
        lv_rows[num] = rows
        if new_part < row_old:
            res.vg_texts[geo.vg] = rewrite_vg(geo, dev_size, new_lvs)
    for p in parts_sorted:
        n = p["node"]
        note, size = plan_rows[p["num"]]
        fs = [x for x in (n.walk() if n else []) if x.fstype and x.fstype not in ("crypto_LUKS",)]
        desc = "LVM" if any(x.fstype == "LVM2_member" for x in fs) else (
            f"{fs[0].fstype} {mp_of(fs[0])}".strip() if fs else "")
        if n is not None and n.fstype == "crypto_LUKS":
            desc = "LUKS+" + desc
        res.resizes.append(Resize("partition", p["path"], desc, p["bytes"], size, note))
        res.resizes += lv_rows.get(p["num"], [])
    return res
