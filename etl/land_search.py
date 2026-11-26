"""Compact per-province index of every land plot, for the Land tab's area-wide search (data/land/search/<province>.json).

The station files (data/land/<station>.json) hold each plot's outline and every field the plot card shows; together
they are over 500 MB, too much to load for a whole province.  This keeps only what the page's filters read, one row
per plot (at its nearest substation), so the page can run the same filters over every substation of a province, or of
the whole country, and then open the chosen site on the usual map from its station file.

Row: [station, precinct, nr, km×100, ha×100, usable ha×100 or null, metres to the nearest building or null, average
width (m), mean slope×10 or null, flags, owner groups, general-plan zone or -1, local-plan ha×100, lat×10⁴, lon×10⁴,
distances or null, neighbours, wind speed at 150 m×10 or null, aviation zones (as `av` in the station file) or null]
- station: index into "stations"; precinct: index into "precincts" ([id prefix, municipality, precinct name]);
- flags: 1 national park/reserve, 2 landscape park, 4 Natura 2000, 8 protected landscape area (each on more than a
  tenth of the plot), 16 more than a tenth in the 1% flood area, 32 any part in it, 64 class I–III farmland, 128 the
  register lists soil classes, 256 a solar, battery or wind permit on the plot;
- zone: index into "zones" (ids of data/land/zoning.json); distances: metres for the keys in "distance_keys" (null:
  none within the search distance); neighbours: row indices in the same file.
"""
import datetime
import glob
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
OUT = os.path.join(LAND, "search")
STATION_FILE = re.compile(r"^(tauron|energa|pge|enea|stoen|pse)-.+\.json$")
# The distances the page's filters use (buffers and road access); the plot card reads the rest from the station file.
SEARCH_KEYS = ["home", "housing", "public_road", "major_road", "railway", "hv_line", "cemetery", "airfield", "school", "care"]
GOOD_SOIL = re.compile(r"^(R|Ł|Ps)(I|II|III|IIIa|IIIb)$")
PROVINCES = {"02": "dolnośląskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie", "10": "łódzkie",
             "12": "małopolskie", "14": "mazowieckie", "16": "opolskie", "18": "podkarpackie", "20": "podlaskie",
             "22": "pomorskie", "24": "śląskie", "26": "świętokrzyskie", "28": "warmińsko-mazurskie",
             "30": "wielkopolskie", "32": "zachodniopomorskie"}


def width(p):
    """2 × area / perimeter, as the page's widthOf()."""
    lat0 = math.radians(p["geom"][0][0][0][1])
    kx, ky, per = 111320 * math.cos(lat0), 110574, 0.0
    for poly in p["geom"]:
        for ring in poly:
            for a, b in zip(ring, ring[1:]):
                per += math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * ky)
    return round(2 * p["ha"] * 1e4 / per) if per else 0


def centre(p):
    ring = p["geom"][0][0]
    return sum(x[1] for x in ring) / len(ring), sum(x[0] for x in ring) / len(ring)


def flags(p):
    f, ha = 0, p["ha"]
    for bit, key in ((1, "np"), (2, "lp"), (4, "n2"), (8, "ol")):
        if (p.get(key) or 0) > 0.1 * ha:
            f |= bit
    q1 = p.get("q1") or 0
    if q1 > 0.1 * ha:
        f |= 16
    if q1 > 0:
        f |= 32
    if p.get("klasy"):
        f |= 128
        if any(GOOD_SOIL.match(x.split("/")[-1]) for x in re.split(r"[,;]\s*", p["klasy"])):
            f |= 64
    if any(g[1] in ("solar", "battery", "wind") for g in p.get("gp") or []):
        f |= 256
    return f


def full_id(p, ref):
    return ref if "_" in ref else p["id"][:p["id"].rindex(".") + 1] + ref


def main():
    index = json.load(open(os.path.join(LAND, "index.json"), encoding="utf-8"))
    dk = {k: i for i, k in enumerate(index["distance_keys"])}
    best = {}   # plot id -> (plot, station id)
    for path in sorted(glob.glob(os.path.join(LAND, "*.json"))):
        name = os.path.basename(path)
        if not STATION_FILE.match(name):
            continue
        sid = name[:-5]
        for p in json.load(open(path, encoding="utf-8"))["plots"]:
            if p["id"] not in best or p["km"] < best[p["id"]][0]["km"]:
                best[p["id"]] = (p, sid)
    os.makedirs(OUT, exist_ok=True)
    by_province = {}
    for pid, (p, sid) in best.items():
        by_province.setdefault(pid[:2], []).append((p, sid))
    summary, changed = {}, 0
    for code, items in sorted(by_province.items()):
        items.sort(key=lambda x: x[0]["id"])
        row_of = {p["id"]: i for i, (p, _) in enumerate(items)}
        stations, precincts, zones = {}, {}, {}
        rows = []
        for p, sid in items:
            st = stations.setdefault(sid, len(stations))
            prefix = p["id"][:p["id"].rindex(".")]
            pr = precincts.setdefault(prefix, (len(precincts), p.get("gmina") or "", p.get("obreb") or ""))[0]
            zone = zones.setdefault(p["pg"][0][0], len(zones)) if p.get("pg") else -1
            lat, lon = centre(p)
            d = p.get("d")
            # A plot checked before a kind was added has a shorter list: null for that kind.
            dist = [d[dk[k]] if k in dk and dk[k] < len(d) else None for k in SEARCH_KEYS] if d else None
            nx = sorted({row_of[n] for n in (full_id(p, r) for r in p.get("nx") or []) if n in row_of})
            rows.append([st, pr, p["id"][len(prefix) + 1:], round(p["km"] * 100), round(p["ha"] * 100),
                         None if p.get("u") is None else round(p["u"] * 100), p.get("bm"), width(p),
                         None if p.get("sl") is None else round(p["sl"] * 10), flags(p), p.get("grupa") or "", zone,
                         round((p.get("mp") or 0) * 100), round(lat * 1e4), round(lon * 1e4), dist, nx,
                         round(p["ws"][1] * 10) if p.get("ws") else None, p.get("av")])
        data = {"province": PROVINCES.get(code, code), "distance_keys": SEARCH_KEYS, "stations": list(stations),
                "precincts": [[k, v[1], v[2]] for k, v in sorted(precincts.items(), key=lambda x: x[1][0])],
                "zones": list(zones), "rows": rows}
        path = os.path.join(OUT, f"{code}.json")
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        # Rewritten only when it changes, so a weekly run that touches one province doesn't add 68 MB to the history.
        if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
            open(path, "w", encoding="utf-8").write(text)
            changed += 1
        summary[code] = {"name": PROVINCES.get(code, code), "plots": len(rows), "stations": len(stations),
                         "kb": round(os.path.getsize(path) / 1024)}
    index_path = os.path.join(OUT, "index.json")
    old = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {}
    if changed or old.get("provinces") != summary:
        json.dump({"built": datetime.date.today().isoformat(), "provinces": summary},
                  open(index_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    total = sum(v["plots"] for v in summary.values())
    print(f"Land search: {total} plots in {len(summary)} provinces, {sum(v['kb'] for v in summary.values()) / 1024:.0f} MB; "
          f"{changed} province files changed")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
