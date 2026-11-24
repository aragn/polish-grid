"""
Land plots (działki ewidencyjne) around the substations of the groups with room, for the Land tab.

    python etl/land.py                       # fetch what is missing or older than REFRESH_DAYS, within the budget
    python etl/land.py --budget-minutes 300  # a longer run
    python etl/land.py --only energa-13      # one group's stations

Which substations: those of the distribution groups (TAURON, Energa, PGE) with room for at least MIN_ROOM_MW of
generation or demand in TARGET_YEAR in data/grid_capacity.json, placed at a known substation (the operator's own
coordinates, an OpenStreetMap substation of that name, or the 110 kV substation nearest the town it is named after),
and the BENCHMARK groups whatever their room.

Plots: GUGiK's collective WFS of the land and building registers (EGiB, "Usługa zbiorcza", built from every
county's service) for a square of RADIUS_KM around each substation.  It answers from GitHub's runners but not
from every network.  Kept: plots of at least MIN_AREA_HA (area computed from the outline) within RADIUS_KM, with
their identifier, municipality, precinct, number, area, and where the county publishes them the land-use and soil
classes and the owner group (grupa rejestrowa: the kind of owner, never a name).  Outlines are simplified to
about 1.5 m and rounded to 1e-5 degrees.

Paging: a page holds at most PAGE plots, and the service's numberMatched on a page only counts the plots up to that
page (813 on the first page of a square that holds 11,143), so the total comes from a separate hits request and the
pages are read until it.  A refetch keeps what the later steps found on a plot (constraints, protected areas, zoning,
flood) when the plot is unchanged, so they only work on the new plots.

Writes data/land/<station>.json (one per substation) and data/land/index.json.
"""

import argparse
import concurrent.futures
import datetime
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stations import fold  # noqa: E402
from land_budget import capped  # noqa: E402

ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "land")
WFS = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
TARGET_YEAR = "2030"
MIN_ROOM_MW = 50
RADIUS_KM = 3.0
MIN_AREA_HA = 1.0
REFRESH_DAYS = 90
VERSION = 3              # bump to re-fetch everything (2: outlines simplified correctly; 3: every page, not just the first)
PAGE = 1000
NS = {"gml": "http://www.opengis.net/gml/3.2"}
PLACED = {"substation", "operator", "near"}
# Benchmark groups: fetched whatever their room (even full, or PSE's own substations), to check the screening against
# projects that already got a connection there.  Narew (PSE, 400/110 kV): MEE Turośń Kościelna, 200 MW, agreed 2021.
BENCHMARK = {"pse-narew"}


def station_id(op, station):
    base = station.get("code") or fold(station["name"]).replace(" ", "-")
    return f"{op}-{base}".lower()


def shortlist():
    data = json.load(open(os.path.join(ROOT, "data", "grid_capacity.json"), encoding="utf-8"))
    stations = {}
    for area in data["areas"]:
        bench = area["id"] in BENCHMARK
        if area["op"] == "pse" and not bench:
            continue
        room = max(area["gen"].get(TARGET_YEAR) or 0, area["dem"].get(TARGET_YEAR) or 0)
        if room < MIN_ROOM_MW and not bench:
            continue
        for s in area["stations"]:
            if s.get("lat") is None or (s.get("how") not in PLACED and not (bench and s.get("how") == "station")):
                continue
            sid = station_id(area["op"], s)
            entry = stations.setdefault(sid, {"id": sid, "name": s["name"], "code": s.get("code", ""), "op": area["op"], "lat": s["lat"], "lon": s["lon"],
                                              "how": s["how"], "groups": [], "room": 0})
            entry["groups"].append(area["id"])
            entry["room"] = max(entry["room"], room)
    return stations


def get(params):
    for attempt in range(5):
        try:
            response = requests.get(WFS, params=params, headers=UA, timeout=180)
            response.raise_for_status()
            if b"ExceptionReport" in response.content[:3000]:
                raise requests.HTTPError(response.content[:300].decode("utf-8", "replace"))
            return response.content
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(15 * (attempt + 1))


def project(lat0):
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110574
    return lambda lat, lon: (lon * kx, lat * ky)


def ring_area(points):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def simplify(points, tolerance):
    """Douglas-Peucker on projected points; returns the indices kept.  A closed ring (first point = last) is split
    at the point farthest from its start, since a segment of zero length would keep nothing."""
    if len(points) < 4:
        return list(range(len(points)))
    far = max(range(len(points)), key=lambda i: math.hypot(points[i][0] - points[0][0], points[i][1] - points[0][1]))
    keep = {0, far, len(points) - 1}
    stack = [(0, far), (far, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = points[a], points[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-9
        best, index = 0.0, None
        for i in range(a + 1, b):
            x, y = points[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm
            if d > best:
                best, index = d, i
        if index is not None and best > tolerance:
            keep.add(index)
            stack += [(a, index), (index, b)]
    return sorted(keep)


def parse(content):
    root = ET.fromstring(content)
    for member in root.iter():
        if not member.tag.endswith("}dzialki"):
            continue
        fields = {child.tag.split("}")[1]: (child.text or "").strip() for child in member if child.tag.split("}")[1] != "geom"}
        polygons = []
        for polygon in member.iter("{http://www.opengis.net/gml/3.2}Polygon"):
            rings = []
            for pos in polygon.iter("{http://www.opengis.net/gml/3.2}posList"):
                values = [float(v) for v in pos.text.split()]
                rings.append(list(zip(values[0::2], values[1::2])))   # lat, lon (EPSG:4326 axis order)
            if rings:
                polygons.append(rings)
        if polygons:
            yield fields, polygons


def plots_around(station):
    lat, lon = station["lat"], station["lon"]
    dlat = RADIUS_KM / 110.574
    dlon = RADIUS_KM / (111.320 * math.cos(math.radians(lat)))
    bbox = f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon},urn:ogc:def:crs:EPSG::4326"
    to_xy = project(lat)
    sx, sy = to_xy(lat, lon)
    query = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "ms:dzialki", "SRSNAME": "urn:ogc:def:crs:EPSG::4326", "BBOX": bbox}
    m = re.search(rb'numberMatched="(\d+)"', get({**query, "RESULTTYPE": "hits"})[:3000])
    total = int(m.group(1)) if m else None
    out, seen, start, empty = [], set(), 0, 0
    while True:
        content = get({**query, "COUNT": PAGE, "STARTINDEX": start})
        n = 0
        for fields, polygons in parse(content):
            n += 1
            if fields.get("ID_DZIALKI", "") in seen:
                continue
            seen.add(fields.get("ID_DZIALKI", ""))
            area = 0.0
            geometry, nearest = [], 1e9
            for rings in polygons:
                projected = [[to_xy(a, b) for a, b in ring] for ring in rings]
                area += ring_area(projected[0]) - sum(ring_area(r) for r in projected[1:])
                shape = []
                for ring, xy in zip(rings, projected):
                    kept = simplify(xy, 1.5)
                    shape.append([[round(ring[i][1], 5), round(ring[i][0], 5)] for i in kept])   # [lon, lat]
                    nearest = min(nearest, min(math.hypot(x - sx, y - sy) for x, y in xy))
                geometry.append(shape)
            hectares = area / 10000
            if hectares < MIN_AREA_HA or nearest > RADIUS_KM * 1000:
                continue
            out.append({"id": fields.get("ID_DZIALKI", ""), "gmina": fields.get("NAZWA_GMINY", ""), "obreb": fields.get("NAZWA_OBREBU", ""),
                        "nr": fields.get("NUMER_DZIALKI", ""), "ha": round(hectares, 2), "ha_egib": float(fields["POLE_EWIDENYJNE"]) if re.fullmatch(r"\d+(\.\d+)?", fields.get("POLE_EWIDENYJNE", "")) else None,
                        "klasy": fields.get("KLASOUZYTKI_EGIB", ""), "grupa": fields.get("GRUPA_REJESTROWA", ""),
                        "km": round(nearest / 1000, 2), "geom": geometry})
        start += PAGE
        # Without a total, stop after two empty pages in a row (a page can come back with fewer than PAGE plots).
        empty = empty + 1 if n == 0 else 0
        if (total is not None and start >= total) or (total is None and empty >= 2):
            break
    return out, total if total is not None else len(seen)


FETCHED = {"id", "gmina", "obreb", "nr", "ha", "ha_egib", "klasy", "grupa", "km", "geom"}


def carry_over(path, plots):
    """Copy what the later steps found (constraints, protected areas, zoning, flood, neighbours) from the station's
    previous file onto the plots that haven't changed (same id, area and outline)."""
    if not os.path.exists(path):
        return 0
    old = {p["id"]: p for p in json.load(open(path, encoding="utf-8")).get("plots", [])}
    kept = 0
    for plot in plots:
        before = old.get(plot["id"])
        if before and before.get("ha") == plot["ha"] and before.get("geom") == plot["geom"]:
            plot.update({k: v for k, v in before.items() if k not in FETCHED})
            kept += 1
    return kept


def main(budget_minutes=50, only=None):
    os.makedirs(OUT, exist_ok=True)
    index_path = os.path.join(OUT, "index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {"stations": {}}
    wanted = shortlist()
    today = datetime.date.today()
    todo = []
    for sid, station in wanted.items():
        if only and only not in station["groups"] and only != sid:
            continue
        known = index["stations"].get(sid)
        moved = known and (abs(known["lat"] - station["lat"]) > 1e-4 or abs(known["lon"] - station["lon"]) > 1e-4)
        stale = not known or known.get("v") != VERSION or not known.get("fetched") or (today - datetime.date.fromisoformat(known["fetched"])).days >= REFRESH_DAYS
        if stale or moved:
            todo.append(station)
    todo.sort(key=lambda s: -s["room"])      # the substations of the groups with the most room first
    print(f"Land: {len(wanted)} substations shortlisted, {len(todo)} to fetch", flush=True)
    deadline = time.time() + capped(budget_minutes, 30, "Land") * 60
    done = added = 0

    def work(station):
        if time.time() > deadline:
            return station, None, None, "skipped"
        try:
            plots, matched = plots_around(station)
            return station, plots, matched, None
        except Exception as error:  # noqa: BLE001
            return station, None, None, str(error)[:200]

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        for station, plots, matched, error in pool.map(work, todo):
            sid = station["id"]
            if error == "skipped":
                continue
            if error:
                print(f"Land: {sid} failed: {error}", flush=True)
                continue
            path = os.path.join(OUT, f"{sid}.json")
            before = (index["stations"].get(sid) or {}).get("n", 0)
            kept = carry_over(path, plots)
            json.dump({"station": {k: station[k] for k in ("id", "name", "code", "op", "lat", "lon", "how", "groups")}, "radius_km": RADIUS_KM,
                       "min_ha": MIN_AREA_HA, "fetched": today.isoformat(), "plots_in_square": matched, "plots": plots},
                      open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            index["stations"][sid] = {**{k: station[k] for k in ("name", "code", "op", "lat", "lon", "how", "groups")}, "fetched": today.isoformat(), "v": VERSION, "n": len(plots),
                                      "big": sum(1 for p in plots if p["ha"] >= 5), "max_ha": max((p["ha"] for p in plots), default=0),
                                      "checked": sum(1 for p in plots if "u" in p), "big_usable": sum(1 for p in plots if p.get("u", 0) >= 5),
                                      "n2k": sum(1 for p in plots if p.get("n2", 0) > 0)}
            done += 1
            added += len(plots) - kept
            if done <= 5 or done % 20 == 0:
                print(f"Land: {done}/{len(todo)} ({sid}: {matched} plots in the square, {len(plots)} of 1 ha or more, was {before}; {kept} unchanged)", flush=True)
                json.dump(index, open(index_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    # Stations no longer shortlisted stay on disk but leave the index.
    index["stations"] = {sid: v for sid, v in index["stations"].items() if sid in wanted}
    for sid in index["stations"]:
        index["stations"][sid]["groups"] = wanted[sid]["groups"]
    index.update({"updated": today.isoformat(), "rule": {"year": TARGET_YEAR, "min_room_mw": MIN_ROOM_MW, "radius_km": RADIUS_KM, "min_ha": MIN_AREA_HA},
                  "source": WFS, "shortlisted": len(wanted)})
    json.dump(index, open(index_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    left = len(todo) - done
    print(f"Land: fetched {done}, {left} left for the next run, {len(index['stations'])} substations with plots; {added} new or changed plots for the later steps")
    return done > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget-minutes", type=float, default=50)
    parser.add_argument("--only")
    args = parser.parse_args()
    main(args.budget_minutes, args.only)
