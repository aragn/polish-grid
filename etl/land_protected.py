"""
Protected areas on each land plot of the Land tab, from the European Environment Agency:

    python etl/land_protected.py          # plots not yet checked
    python etl/land_protected.py --all    # every plot again (after a new EEA release)

Sources (EEA ArcGIS services, both cover all of Poland and answer from GitHub's runners and laptops alike, unlike
GDOS's own service, which refuses the runners):
  - Natura 2000 (Habitats and Birds Directive sites), the end-2024 release: 1,006 Polish sites.
  - Nationally designated areas (NatDA, formerly CDDA): national parks (PL01), nature reserves (PL02), landscape
    parks (PL03) and protected landscape areas (PL04).

For every plot in data/land/<station>.json it adds, in hectares: `n2` inside Natura 2000, `np` inside a national park
or nature reserve, `lp` inside a landscape park, `ol` inside a protected landscape area; `n2d`, the distance in metres
to the nearest Natura 2000 site (0 inside, None beyond N2K_SEARCH_M); and `pz`, the ids of the areas it touches.
data/land/protected.json maps those ids to the type and name, so that plots stay small.

The polygons are downloaded once per run, generalised to SIMPLIFY_M (well within a plot's own outline accuracy).
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time

import requests
import shapely
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAND = os.path.join(ROOT, "data", "land")
BASE = "https://bio.discomap.eea.europa.eu/arcgis/rest/services/ProtectedSites"
N2K = f"{BASE}/Natura2000Sites/MapServer/2/query"
NATDA = f"{BASE}/CDDA_Dyna_WM/MapServer/4/query"
NATDA_TYPES = {"PL01": "national park", "PL02": "nature reserve", "PL03": "landscape park",
               "PL04": "protected landscape area"}
N2K_TYPES = {"A": "Natura 2000 bird area, SPA", "B": "Natura 2000 habitat area, SAC",
             "C": "Natura 2000 bird and habitat area"}
SIMPLIFY_M = 10
DRAW_SIMPLIFY_M = 25            # the outlines drawn on the map (data/land/protected_shapes.json)
DRAW_AROUND_M = 4000            # only within this distance of a shortlisted substation
N2K_SEARCH_M = 5000
PAGE = 100
UA = {"User-Agent": "polish-grid land screening (github.com/aragn/polish-grid)"}
TO_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
TO_4326 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)


def fix_text(s):
    """EEA's NatDA names come as UTF-8 read as cp1252 ("Å‚" for "ł"); undo that where it applies."""
    if not s or not any(c in s for c in "ÃÄÅâ"):
        return s
    raw = bytearray()
    for c in s:
        o = ord(c)
        if 0xDC80 <= o <= 0xDCFF:
            raw.append(o - 0xDC00)
            continue
        try:
            raw += c.encode("cp1252")
        except UnicodeEncodeError:
            if o < 256:
                raw.append(o)
            else:
                return s
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return s


def query(url, where, fields):
    """All features (GeoJSON, EPSG:2180) for where, page by page."""
    out, offset = [], 0
    while True:
        params = {"where": where, "outFields": fields, "outSR": 2180, "maxAllowableOffset": SIMPLIFY_M,
                  "geometryPrecision": 0, "resultOffset": offset, "resultRecordCount": PAGE,
                  "orderByFields": "OBJECTID", "f": "geojson"}
        for attempt in range(5):
            try:
                response = requests.get(url, params=params, headers=UA, timeout=300)
                response.raise_for_status()
                features = response.json()["features"]
                break
            except (requests.RequestException, ValueError, KeyError):
                if attempt == 4:
                    raise
                time.sleep(15 * (attempt + 1))
        out += features
        if len(features) < PAGE:
            return out
        offset += PAGE


def load_areas():
    """[(id, kind, name, geometry in EPSG:2180)] for Natura 2000 and the national designations."""
    areas = []
    for f in query(N2K, "MS='PL'", "SITECODE,SITENAME,SITETYPE"):
        p = f["properties"]
        if f.get("geometry"):
            areas.append((p["SITECODE"], "n2k", fix_text(p["SITENAME"]), N2K_TYPES.get(p["SITETYPE"], "Natura 2000"),
                          shapely.from_geojson(json.dumps(f["geometry"]))))
    codes = ",".join(f"'{c}'" for c in NATDA_TYPES)
    for f in query(NATDA, f"countryCode='PL' AND designationTypeCode IN ({codes})", "siteName,designationTypeCode,nationalId,id"):
        p = f["properties"]
        if f.get("geometry"):
            areas.append((p.get("nationalId") or f"NATDA.{p['id']}", p["designationTypeCode"], fix_text(p["siteName"]),
                          NATDA_TYPES[p["designationTypeCode"]], shapely.from_geojson(json.dumps(f["geometry"]))))
    areas = [(i, k, n, t, shapely.make_valid(g)) for i, k, n, t, g in areas]
    return areas


def plot_shape(plot):
    polygons = []
    for poly in plot["geom"]:
        rings = []
        for ring in poly:
            xs, ys = TO_2180.transform([c[0] for c in ring], [c[1] for c in ring])
            rings.append(list(zip(xs, ys)))
        if len(rings[0]) >= 3:
            polygons.append(shapely.Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3]))
    if not polygons:
        return None
    return shapely.make_valid(shapely.MultiPolygon(polygons) if len(polygons) > 1 else polygons[0])


# Files in data/land/ that aren't one substation's plots.
NOT_STATIONS = ("index.json", "protected.json", "protected_shapes.json", "zoning.json")


def station_files():
    return sorted(f for f in glob.glob(os.path.join(LAND, "*.json")) if os.path.basename(f) not in NOT_STATIONS)


GROUPS = {"n2": ("n2k",), "np": ("PL01", "PL02"), "lp": ("PL03",), "ol": ("PL04",)}


def values_for(shape, areas, tree, n2k_tree, n2k_index):
    area = shape.area
    hits = [areas[i] for i in tree.query(shape, predicate="intersects")]
    values = {}
    for key, kinds in GROUPS.items():
        parts = [a[4] for a in hits if a[1] in kinds]
        if not parts:
            values[key] = 0.0
            continue
        inside = shapely.intersection(shapely.union_all(parts), shape).area
        values[key] = round(min(inside, area) / 10000, 2)
    values["pz"] = sorted({a[0] for a in hits})
    if values["n2"] > 0:
        values["n2d"] = 0
    else:
        index, distance = n2k_tree.query_nearest(shape, max_distance=N2K_SEARCH_M, return_distance=True)
        values["n2d"] = int(round(float(distance[0]))) if len(index) else None
    return values


def main(redo_all=False):
    files = station_files()
    todo = {}
    for path in files:
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            if redo_all or "n2" not in plot:
                todo[plot["id"]] = plot
    print(f"Land protected areas: {len(todo)} plots to check", flush=True)
    if not todo:
        return False
    t0 = time.time()
    areas = load_areas()
    print(f"Land protected areas: {len(areas)} areas downloaded in {time.time() - t0:.0f} s", flush=True)
    tree = shapely.STRtree([a[4] for a in areas])
    n2k = [a for a in areas if a[1] == "n2k"]
    n2k_tree = shapely.STRtree([a[4] for a in n2k])
    results = {}
    for pid, plot in todo.items():
        shape = plot_shape(plot)
        if shape is not None and not shape.is_empty:
            results[pid] = values_for(shape, areas, tree, n2k_tree, n2k)
    print(f"Land protected areas: {len(results)} plots checked in {time.time() - t0:.0f} s", flush=True)

    used = set()
    changed = 0
    index_path = os.path.join(LAND, "index.json")
    index = json.load(open(index_path, encoding="utf-8"))
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if plot["id"] in results:
                plot.update(results[plot["id"]])
                touched = True
            used.update(plot.get("pz", []))
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
        sid = os.path.basename(path)[:-5]
        if sid in index["stations"]:
            index["stations"][sid]["n2k"] = sum(1 for p in data["plots"] if p.get("n2", 0) > 0)
    drawn = write_shapes(areas, index)
    # The lookup keeps the areas already listed (plots not rechecked may still point at them) plus the new ones.
    lookup_path = os.path.join(LAND, "protected.json")
    lookup = json.load(open(lookup_path, encoding="utf-8"))["areas"] if os.path.exists(lookup_path) else {}
    for aid, kind, name, label, _geom in areas:
        lookup[aid] = [kind, name, label]
    lookup = {k: v for k, v in sorted(lookup.items()) if k in used or k in drawn}
    json.dump({"source": "EEA: Natura 2000 (end-2024 release) and Nationally designated areas (NatDA)",
               "fetched": datetime.date.today().isoformat(), "areas": lookup},
              open(lookup_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    index["protected_source"] = "EEA Natura 2000 and NatDA"
    json.dump(index, open(index_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Land protected areas: {changed} station files updated, {len(lookup)} areas in the lookup")
    return changed > 0


def write_shapes(areas, index):
    """The protected areas around the shortlisted substations, simplified, as [id, [ring, ...]] per polygon with rings
    of [lon, lat], for the map.  Returns the ids drawn."""
    xs, ys = TO_2180.transform([s["lon"] for s in index["stations"].values()], [s["lat"] for s in index["stations"].values()])
    around = shapely.union_all(shapely.buffer(shapely.points(list(zip(xs, ys))), DRAW_AROUND_M, quad_segs=8))
    shapes = []
    for aid, _kind, _name, _label, geom in areas:
        if not geom.intersects(around):
            continue
        part = shapely.simplify(shapely.intersection(geom, around), DRAW_SIMPLIFY_M)
        for poly in getattr(part, "geoms", [part]):
            if poly.geom_type != "Polygon" or poly.area < 10000:
                continue
            rings = []
            for ring in [poly.exterior, *poly.interiors]:
                lon, lat = TO_4326.transform(*ring.xy)
                rings.append([[round(a, 5), round(b, 5)] for a, b in zip(lon, lat)])
            shapes.append([aid, rings])
    json.dump({"simplified_m": DRAW_SIMPLIFY_M, "around_m": DRAW_AROUND_M, "shapes": shapes},
              open(os.path.join(LAND, "protected_shapes.json"), "w", encoding="utf-8"), separators=(",", ":"))
    return {s[0] for s in shapes}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    main(args.all)
