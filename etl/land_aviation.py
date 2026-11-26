"""
Aviation limits on each land plot of the Land tab: the building restricted areas (BRA) around the civil aviation
ground equipment in Poland, from the Civil Aviation Authority (ULC):

    python etl/land_aviation.py                      # check every plot against data/land/aviation.json
    python etl/land_aviation.py --update-from DIR    # rebuild data/land/aviation.json from ULC's zips (or folders) in DIR
    python etl/land_aviation.py --update-ols         # refresh the airports' obstacle limitation surfaces from ULC's map

Source: the shapefiles ULC publishes for investors and municipalities on ulc.gov.pl/zegluga-powietrzna/mapy-on-line
("Dane wektorowe ... powierzchni ograniczających zabudowę (BRA)"), one zip per kind of equipment: ILS, COM
(communication), NAV (radio navigation), SUR (radar) and MET (weather radar).  ULC sets no conditions for reusing
them.  They are downloaded by hand when ULC posts new ones (the file names carry the date) and turned into
data/land/aviation.json with --update-from; the staff names in the edit fields are left out.

The rule (Aviation Law art. 86 and the regulation on aviation ground equipment and BRA, Dz.U. 2023 poz. 1181):
- within 600 m of the equipment ("Strefa0"), anything built needs ULC's agreement (code 0);
- in the zone around it ("wew", "zew"), objects more than 15 m above the ground need it (code 1), so every turbine;
- in the outer zone of radio navigation and weather radars ("turbiny"), the height limit applies to wind turbines
  only (code 2): for weather radars it reaches 30 km (Dz.U. 2023 poz. 229).
Above the limit is not banned: it needs ULC's agreement (art. 86(7)).  ULC draws the limit as rings of equal height
above sea level; here a plot gets the height of the ring just inside it (the lower edge of its step), so the limit
is on the safe side.  Military equipment isn't in these files and is still checked case by case.

Airports: the obstacle limitation surfaces (OLS) of the civil airports in ULC's register, read with --update-ols from
the public feature services behind ULC's map "Rejestr Lotnisk Cywilnych oraz OLS" (published for investors and
municipalities, no conditions for reuse): the approach surfaces as polygons, and the height lines of every surface
(above sea level).  The flat inner horizontal surface and the conical one around it are lines of equal height round
the airport, turned into nested rings; the approach surfaces are lines across the approach.  Take-off climb
surfaces have no outline in the service and mostly lie within the approach ones; they are left out.  Heights of 0 are placeholders and are dropped, as are heights
far above the airport's conical surface (typos).  Planned airports and surfaces ("Zezwolenie", "Promesa") are kept and
marked.  Objects above the surfaces need ULC's agreement (Aviation Law art. 87).

Adds `av` per plot: [[equipment, code, limit]] for each piece of equipment whose zone touches the plot (its most
restrictive zone), with equipment an index into aviation.json's "equipment" and limit the height above sea level in
metres at the plot's centre, or null where ULC gives no rings; [] when no zone touches the plot.  And `ap`
(not `ol`, which holds the protected landscape area):
[[airport, limit, surface, planned]] for each airport whose surfaces cover the plot's centre, with airport an index
into "airports", limit the lowest surface there in metres above sea level (on the safe side of the steps), surface 0
the inner horizontal or conical surface and 1 an approach surface; [] when none does.
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys
import tempfile
import zipfile

import numpy as np
import pyogrio.raw as raw
import pyproj
import requests
import shapely
import shapely.geometry
import time

from land_protected import station_files

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "data", "land", "aviation.json")
KINDS = ["ILS", "COM", "NAV", "SUR", "MET"]
CODE = {"strefa0": 0, "wew": 1, "zew": 1, "turbiny": 2}
SIMPLIFY_M = 10


def read(path):
    meta, _, geom, fields = raw.read(path)
    names = list(meta["fields"])
    rows = []
    for i in range(len(geom)):
        row = {n: fields[k][i] for k, n in enumerate(names)}
        row["geometry"] = shapely.from_wkb(geom[i]) if geom[i] is not None else None
        rows.append(row)
    return meta["crs"], rows


def text(v):
    return " ".join(str(v).split()) if v is not None and str(v) not in ("nan", "None") else None


def number(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def to_lonlat(crs):
    t = pyproj.Transformer.from_crs(pyproj.CRS.from_user_input(crs), "EPSG:4326", always_xy=True)
    return lambda g: shapely.transform(g, lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))


def coords(polygon):
    return [[[round(x, 5), round(y, 5)] for x, y in ring.coords] for ring in [polygon.exterior, *polygon.interiors]]


def update_from(folder):
    """data/land/aviation.json from ULC's BRA zips (or unpacked folders) in folder."""
    work = tempfile.mkdtemp(prefix="ulc_bra_")
    for z in glob.glob(os.path.join(folder, "*.zip")):
        with zipfile.ZipFile(z) as f:
            f.extractall(os.path.join(work, os.path.basename(z)[:-4]))
    roots = [work, folder]
    equipment, index, zones, rings, files = [], {}, [], [], {}

    def find(kind, layer):
        hits = [p for r in roots for p in glob.glob(os.path.join(r, "**", f"BRA{layer}_{kind}.shp"), recursive=True)]
        return sorted(hits)[-1] if hits else None

    for kind in KINDS:
        points, zone_file, ring_file = find(kind, "punkty"), find(kind, "strefy"), find(kind, "okregi")
        if not zone_file:
            print(f"Land aviation: no {kind} zones in {folder}")
            continue
        files[kind] = re.search(r"(\d{8})", zone_file).group(1) if re.search(r"(\d{8})", zone_file) else None
        if points:
            _, rows = read(points)
            for r in rows:
                key = (kind, text(r["idenLUN"]))
                if key in index or r["geometry"] is None:
                    continue
                index[key] = len(equipment)
                equipment.append([kind, text(r["rodzajLUN"]), text(r["nazwaZnak"]), text(r["miejsce"]),
                                  round(r["geometry"].y, 5), round(r["geometry"].x, 5), number(r["rzedna"])])
        crs, rows = read(zone_file)
        project = to_lonlat(crs)
        for r in rows:
            code = CODE.get((text(r["typStrefy"]) or "").lower())
            if code is None or r["geometry"] is None:
                continue
            key = (kind, text(r["idenLUN"]))
            g = r["geometry"].simplify(SIMPLIFY_M)
            if key not in index:   # equipment without a point: placed at the centre of its zone
                c = project(r["geometry"].centroid)
                index[key] = len(equipment)
                equipment.append([kind, text(r["rodzajLUN"]), None, None, round(c.y, 5), round(c.x, 5), None])
            for poly in getattr(project(g), "geoms", [project(g)]):
                if poly.geom_type == "Polygon" and not poly.is_empty:
                    zones.append([index[key], code, coords(poly)])
        if ring_file:
            crs, rows = read(ring_file)
            project = to_lonlat(crs)
            for r in rows:
                key, h, g = (kind, text(r["idenLUN"])), number(r["wysMAX"]), r["geometry"]
                if key not in index or not h or h <= 0 or g is None or not g.is_closed or len(g.coords) < 4:
                    continue
                poly = shapely.Polygon([(x, y) for x, y, *_ in g.coords]).simplify(SIMPLIFY_M)
                if poly.is_valid and not poly.is_empty:
                    rings.append([index[key], h, coords(project(poly))[0]])
    old = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    data = {"built": datetime.date.today().isoformat(), "files": files,
            "source": "Urząd Lotnictwa Cywilnego, BRA vector data (ulc.gov.pl/zegluga-powietrzna/mapy-on-line)",
            "equipment_fields": ["kind", "type", "name", "place", "lat", "lon", "ground_m"],
            "zone_codes": {"0": "within 600 m: anything needs ULC's agreement", "1": "objects over 15 m need ULC's agreement",
                           "2": "height limit for wind turbines only"},
            "equipment": equipment, "zones": zones, "rings": rings}
    data.update({k: v for k, v in old.items() if k.startswith(("airport", "ols"))})   # kept from --update-ols
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Land aviation: {len(equipment)} pieces of equipment, {len(zones)} zones, {len(rings)} height rings "
          f"({os.path.getsize(OUT) // 1024} KB), files {files}")


OLS_SERVICE = "https://services.arcgis.com/cqaIQEE6sk78TLdG/arcgis/rest/services/"
OLS_LAYERS = {"zones": ["OLS_poligony_obowi%C4%85zuj%C4%85ce/FeatureServer/0", "OLS_poligony_planowane/FeatureServer/0"],
              "lines": ["OLS_linie_obowiazujace/FeatureServer/200", "OLS_linie_planowane/FeatureServer/200"],
              "airports": ["Lotniska/FeatureServer/0"]}


def ols_query(path, fields):
    out, offset = [], 0
    while True:
        params = {"where": "1=1", "outFields": fields, "outSR": 4326, "f": "geojson", "resultOffset": offset, "resultRecordCount": 2000}
        for attempt in range(4):
            try:
                r = requests.get(OLS_SERVICE + path + "/query", params=params, timeout=120)
                r.raise_for_status()
                page = r.json()["features"]
                break
            except (requests.RequestException, ValueError, KeyError):
                if attempt == 3:
                    raise
                time.sleep(10 * (attempt + 1))
        out += page
        if len(page) < 2000:
            return out
        offset += 2000


def round_line(coords):
    return [[round(x, 5), round(y, 5)] for x, y, *_ in coords]


def update_ols():
    """The airports' obstacle limitation surfaces from ULC's public feature services, into data/land/aviation.json."""
    data = json.load(open(OUT, encoding="utf-8"))
    airports, index = [], {}
    for f in ols_query(OLS_LAYERS["airports"][0], "PelnaNazwa,Kod_ICAO,Wzniesieni,ARP_LL_ID"):
        p, g = f["properties"], f["geometry"]
        if not g:
            continue
        index[p["ARP_LL_ID"]] = len(airports)
        airports.append([text(p["Kod_ICAO"]), text(p["PelnaNazwa"]), round(g["coordinates"][1], 5), round(g["coordinates"][0], 5),
                         number(str(p["Wzniesieni"]).replace(",", "."))])

    def airport_of(p):
        if p["ARP_LL_ID"] not in index:   # a planned airport not yet in the register: named by its code
            index[p["ARP_LL_ID"]] = len(airports)
            airports.append([text(p["kodAD"]), None, None, None, None])
        return index[p["ARP_LL_ID"]]

    zones, rings, lines = [], [], []
    for path in OLS_LAYERS["zones"]:
        planned = int("planowane" in path)
        for f in ols_query(path, "kodAD,rodzajOLS,ARP_LL_ID"):
            g = shapely.geometry.shape(f["geometry"]) if f["geometry"] else None
            for poly in getattr(g, "geoms", [g]) if g is not None else []:
                if poly.geom_type == "Polygon" and not poly.is_empty:
                    zones.append([airport_of(f["properties"]), 1, planned, coords(poly.simplify(0.0001))])
    raw = []
    for path in OLS_LAYERS["lines"]:
        planned = int("planowane" in path)
        for f in ols_query(path, "kodAD,rodzajOLS,wysMAX,ARP_LL_ID"):
            p, g = f["properties"], f["geometry"]
            h = number(p["wysMAX"])
            if not g or not h or h <= 0:
                continue
            parts = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
            for part in parts:
                raw.append((airport_of(p), text(p["rodzajOLS"]) or "", h, planned, part))
    # Inner horizontal and conical surfaces: lines of equal height around the airport, often drawn as separate arcs and
    # straight pieces.  Each height becomes the convex hull of its pieces, grown to contain the ring below it, so the
    # rings nest; the shapes are racetracks round the runways, near enough convex, and a hull a little too big only
    # lowers the limit.  Heights more than 150 m above the airport's lowest ring are dropped (typos such as 2107 for
    # 210.7: the conical surface rises 100 m above the horizontal one).
    groups = {}
    for a, kind, h, planned, part in raw:
        if kind in ("Pozioma wewnętrzna", "Stożkowa"):
            groups.setdefault(a, {}).setdefault(h, []).append((planned, part))
    for a, by_h in groups.items():
        base, inner = min(by_h), None
        for h in sorted(by_h):
            if h > base + 150:
                continue
            pts = [c[:2] for _, part in by_h[h] for c in part]
            hull = shapely.MultiPoint(pts).convex_hull
            if inner is not None:
                hull = shapely.union(hull, inner).convex_hull
            if hull.geom_type != "Polygon" or hull.area == 0:
                continue
            inner = hull
            rings.append([a, h, max(pl for pl, _ in by_h[h]), round_line(hull.exterior.simplify(0.0001).coords)])
        if inner is not None:
            zones.append([a, 0, rings[-1][2], [round_line(inner.exterior.simplify(0.0001).coords)]])
    # Approach surfaces: lines across the approach, with their heights.
    for a, kind, h, planned, part in raw:
        if kind in ("Podejścia", "Podniesiona") and len(part) >= 2:
            line = shapely.LineString([c[:2] for c in part]).simplify(0.0001)
            lines.append([a, h, planned, round_line(line.coords)])
    data.update({"airport_fields": ["icao", "name", "lat", "lon", "elevation_m"], "airports": airports, "ols_zones": zones,
                 "ols_rings": rings, "ols_lines": lines, "ols_source": "Urząd Lotnictwa Cywilnego, Rejestr Lotnisk Cywilnych oraz OLS (ArcGIS feature services of wspolny_CAA_PL)",
                 "ols_read": datetime.date.today().isoformat()})
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Land aviation: {len(airports)} airports, {len(zones)} surface outlines, {len(rings)} rings, {len(lines)} approach lines "
          f"({os.path.getsize(OUT) // 1024} KB)")


def plot_shape(plot):
    polys = [shapely.Polygon(poly[0], poly[1:]) for poly in plot["geom"] if len(poly[0]) >= 3]
    shape = shapely.MultiPolygon(polys) if len(polys) > 1 else polys[0] if polys else None
    return shape if shape is None or shape.is_valid else shape.buffer(0)


def main():
    data = json.load(open(OUT, encoding="utf-8"))
    zone_geoms = [shapely.Polygon(z[2][0], z[2][1:]) for z in data["zones"]]
    zone_tree = shapely.STRtree(zone_geoms)
    rings_of = {}
    for eq, h, ring in data["rings"]:
        rings_of.setdefault(eq, []).append((shapely.Polygon(ring), h))
    for eq in rings_of:
        rings_of[eq].sort(key=lambda x: x[0].area)

    def limit(eq, point):
        """The height of the ring just inside the point (the lower edge of its step), or of the smallest ring."""
        rs = rings_of.get(eq)
        if not rs:
            return None
        inner = [h for poly, h in rs if not poly.contains(point)]   # rings smaller than the point's distance
        return round(inner[-1] if inner else rs[0][1], 1)

    ols_geoms = [shapely.Polygon(z[3][0], z[3][1:]) for z in data.get("ols_zones", [])]
    ols_tree = shapely.STRtree(ols_geoms) if ols_geoms else None
    ols_rings, ols_lines = {}, {}
    for a, h, planned, ring in data.get("ols_rings", []):
        ols_rings.setdefault(a, []).append((shapely.Polygon(ring), h))
    for a in ols_rings:
        ols_rings[a].sort(key=lambda x: x[0].area)
    for a, h, planned, line in data.get("ols_lines", []):
        ols_lines.setdefault(a, []).append((shapely.LineString(line), h))
    line_trees = {a: shapely.STRtree([g for g, _ in ls]) for a, ls in ols_lines.items()}

    def ols_limit(a, kind, point, zone):
        """The lowest surface at the point: the inner neighbour ring for the horizontal and conical surfaces; for an
        approach, the lower of the two nearest height lines inside its outline."""
        if kind == 0:
            rs = ols_rings.get(a) or []
            inner = [h for poly, h in rs if not poly.contains(point)]
            return round(inner[-1] if inner else rs[0][1], 1) if rs else None
        ls = ols_lines.get(a) or []
        near = [ls[i] for i in line_trees[a].query(zone, predicate="intersects")] if ls else []
        if not near:
            return None
        near.sort(key=lambda x: x[0].distance(point))
        return round(min(h for _, h in near[:2]), 1)

    changed = done = touching = touching_ols = 0
    for path in station_files():
        stations = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in stations["plots"]:
            shape = plot_shape(plot)
            if shape is None:
                continue
            best = {}
            for i in zone_tree.query(shape, predicate="intersects"):
                eq, code = data["zones"][i][0], data["zones"][i][1]
                best[eq] = min(code, best.get(eq, 9))
            point = shape.representative_point() if best else None
            av = sorted([eq, code, limit(eq, point)] for eq, code in best.items())
            touching += bool(av)
            ol = {}
            if ols_tree is not None:
                centre = shape.representative_point()
                for i in ols_tree.query(centre, predicate="within"):
                    a, kind, planned = data["ols_zones"][i][:3]
                    lim = ols_limit(a, kind, centre, ols_geoms[i])
                    if lim is not None and (a not in ol or lim < ol[a][1]):
                        ol[a] = [a, lim, kind, planned]
            ol = sorted(ol.values())
            touching_ols += bool(ol)
            if plot.get("av") != av or plot.get("ap") != ol:
                plot["av"], plot["ap"] = av, ol
                touched = True
            done += 1
        if touched:
            json.dump(stations, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    print(f"Land aviation: {done} plots checked, {touching} touch an equipment zone, {touching_ols} lie under an airport's surfaces; "
          f"{changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update-from", metavar="DIR")
    parser.add_argument("--update-ols", action="store_true")
    args = parser.parse_args()
    if args.update_from:
        update_from(args.update_from)
    elif args.update_ols:
        update_ols()
    else:
        main()
