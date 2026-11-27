"""
Building permits that may belong to a queued project, for the project cards of the connection-queue page:

    python etl/queue_permits.py

GUNB's register of building permit decisions (the same province files as etl/land_permits.py) is read for solar,
battery and wind decisions since 2018, typed and sized from the free text as there (never the investor, the designer or
the text itself).  Each decision is placed at the centre of its precinct, from GUGiK's parcel service ULDK
(uldk.gugik.gov.pl, GetRegionById; looked up once and cached in data/precincts.json).  A project now in a list, placed
at its substation, is then given the decisions of its technology within RADIUS_KM of the substation (15 km for PSE's
transmission stations; 5 km otherwise), decided no earlier than a year before its conditions, and of a similar size when both sizes are
known.  A strong lead states a size between 0.8 and 1.25 times the project's; a permit with no size is a weak lead,
and only within 3 km.  Each permit is one plant, so it goes to the one project it fits best, and each project keeps
its best permit.

A match is a lead, not a proof: the permit's investor isn't known here.
data/queue_permits.json: {project id: [[date, type, MW or null, km, precinct, municipality, "strong" or "weak"]]}.
"""
import collections
import datetime
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from land_permits import PROVINCES, TYPES, kind, megawatts, read_province

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
ULDK = "https://uldk.gugik.gov.pl/"
UA = {"User-Agent": "polish-grid connection queue (github.com/aragn/polish-grid)"}
SINCE = "2018-01-01"
RADIUS_KM = {"pse": 15.0}
DEFAULT_RADIUS_KM = 5.0
SIZE = (0.8, 1.25)          # a strong lead: the permit states a size within these ratios of the project's
WEAK_KM = 3.0               # a permit with no size counts (as a weak lead) only this close to the substation
TECH = {"solar": {"solar"}, "storage": {"battery"}, "wind": {"wind"}, "hybrid": {"solar", "battery", "wind"}}
LIVE = {"study", "conditions", "agreement"}


def precinct_xy(prec):
    """(lat, lon, precinct name, municipality) of a precinct from ULDK, or None."""
    for attempt in range(2):
        try:
            r = requests.get(ULDK, params={"request": "GetRegionById", "id": prec, "result": "geom_extent,region,commune", "srid": "4326"},
                             headers=UA, timeout=20)
            lines = r.text.strip().splitlines()
            if len(lines) >= 2 and lines[0].strip() == "0":
                ext, region, commune = (lines[1].split("|") + ["", "", ""])[:3]
                x1, y1, x2, y2 = (float(v) for v in ext.split(","))
                return [round((y1 + y2) / 2, 5), round((x1 + x2) / 2, 5), region, commune]
            return None
        except (requests.RequestException, ValueError):
            time.sleep(5 * (attempt + 1))
    return None


def km(a, b):
    dy = (a[0] - b[0]) * 110.574
    dx = (a[1] - b[1]) * 111.32 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def main():
    decisions = {}                       # decision key -> {date, kind, mw, new, precincts}
    for code, name in PROVINCES.items():
        t0, n = time.time(), 0
        for row in read_province(name):
            date = row.get("data_wydania_decyzji", "")[:10]
            if date < SINCE:
                continue
            text = row.get("nazwa_zam_budowlanego", "")
            k = kind(text, False)
            if k not in TYPES:
                continue
            unit, precinct = row.get("jednosta_numer_ew", ""), row.get("obreb_numer", "")
            if not unit or not precinct:
                continue
            key = (row.get("numer_urzad", ""), row.get("numer_decyzji_urzedu", ""), date)
            d = decisions.setdefault(key, {"date": date, "kind": k, "mw": None, "precincts": set()})
            d["precincts"].add(f"{unit}.{precinct.zfill(4)}")
            mw = megawatts(text)
            if mw and (d["mw"] is None or mw > d["mw"]):
                d["mw"] = mw
            n += 1
        print(f"Queue permits: {name} read in {time.time() - t0:.0f} s, {n} energy rows", flush=True)
    print(f"Queue permits: {len(decisions)} solar, battery and wind decisions since {SINCE}", flush=True)

    cache_path = os.path.join(DATA, "precincts.json")
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    wanted = sorted({min(d["precincts"]) for d in decisions.values()} - set(cache))
    print(f"Queue permits: {len(wanted)} precincts to look up ({len(cache)} cached)", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for k, (prec, xy) in enumerate(zip(wanted, pool.map(precinct_xy, wanted)), 1):
            cache[prec] = xy
            if k % 250 == 0:      # save as it goes, so a stopped run keeps what it found
                json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
                print(f"Queue permits: {k} of {len(wanted)} precincts looked up in {time.time() - t0:.0f} s", flush=True)
    json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Queue permits: {len(wanted)} precincts looked up in {time.time() - t0:.0f} s ({len(cache)} cached)", flush=True)

    placed = []
    for d in decisions.values():
        xy = cache.get(min(d["precincts"]))
        if xy:
            placed.append((d, xy))
    projects = json.load(open(os.path.join(DATA, "queue.json"), encoding="utf-8"))["projects"]
    projects += json.load(open(os.path.join(DATA, "queue_dso.json"), encoding="utf-8"))["projects"]
    live = [p for p in projects if not p.get("left") and p.get("phase") in LIVE and p.get("lat") and p.get("technology") in TECH]
    # a coarse grid so each project only looks at nearby decisions
    cells = collections.defaultdict(list)
    for d, xy in placed:
        cells[(int(xy[0] * 4), int(xy[1] * 4))].append((d, xy))
    pairs = []
    for p in live:
        here, radius = (p["lat"], p["lon"]), RADIUS_KM.get(p.get("grid") or "pse", DEFAULT_RADIUS_KM)
        since = (datetime.date.fromisoformat(p["conditions"]) - datetime.timedelta(days=365)).isoformat() if p.get("conditions") else SINCE
        types, mw = TECH[p["technology"]], p.get("mw") or 0
        r = int(radius / 25) + 1
        for i in range(int(here[0] * 4) - r, int(here[0] * 4) + r + 1):
            for j in range(int(here[1] * 4) - r, int(here[1] * 4) + r + 1):
                for d, xy in cells.get((i, j), []):
                    if d["kind"] not in types or d["date"] < since:
                        continue
                    dist = km(here, xy)
                    if dist > radius:
                        continue
                    if d["mw"] and mw:
                        if not SIZE[0] <= d["mw"] / mw <= SIZE[1]:
                            continue
                        strength, rank = "strong", abs(math.log(d["mw"] / mw)) + dist / radius
                    elif dist <= WEAK_KM:
                        strength, rank = "weak", 1 + dist / radius
                    else:
                        continue
                    pairs.append((rank, p["id"], id(d), d, xy, dist, strength))
    # Each permit is one plant: it goes to the project it fits best, and each project keeps its best permit.
    pairs.sort(key=lambda x: x[0])
    taken, out = set(), {}
    for rank, pid, did, d, xy, dist, strength in pairs:
        if pid in out or did in taken:
            continue
        taken.add(did)
        out[pid] = [[d["date"], d["kind"], d["mw"], round(dist, 1), xy[2], xy[3], strength]]
    json.dump({"built": datetime.date.today().isoformat(),
               "source": "GUNB register of building permit decisions (wyszukiwarka.gunb.gov.pl), solar, battery and wind since 2018; precincts placed with GUGiK ULDK",
               "radius_km": {"default": DEFAULT_RADIUS_KM, **RADIUS_KM}, "decisions": len(decisions), "projects": out},
              open(os.path.join(DATA, "queue_permits.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    agr = [p for p in live if p["phase"] == "agreement"]
    strong = sum(1 for v in out.values() if v[0][6] == "strong")
    print(f"Queue permits: {len(out)} of {len(live)} placed projects have a lead ({strong} strong); "
          f"{sum(1 for p in agr if p['id'] in out)} of {len(agr)} holding an agreement ({sum(1 for p in agr if p['id'] in out and out[p['id']][0][6] == 'strong')} strong)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
