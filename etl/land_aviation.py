"""
Aviation limits on each land plot of the Land tab: the building restricted areas (BRA) around the civil aviation
ground equipment in Poland, from the Civil Aviation Authority (ULC):

    python etl/land_aviation.py                      # check every plot against data/land/aviation.json
    python etl/land_aviation.py --update-from DIR    # rebuild data/land/aviation.json from ULC's zips (or folders) in DIR

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
is on the safe side.  Airport obstacle limitation surfaces (OLS) are a separate map at ULC with no download, and
military equipment isn't in these files: both are still checked case by case.

Adds `av` per plot: [[equipment, code, limit]] for each piece of equipment whose zone touches the plot (its most
restrictive zone), with equipment an index into aviation.json's "equipment" and limit the height above sea level in
metres at the plot's centre, or null where ULC gives no rings; [] when no zone touches the plot.
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
import shapely

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
    data = {"built": datetime.date.today().isoformat(), "files": files,
            "source": "Urząd Lotnictwa Cywilnego, BRA vector data (ulc.gov.pl/zegluga-powietrzna/mapy-on-line)",
            "equipment_fields": ["kind", "type", "name", "place", "lat", "lon", "ground_m"],
            "zone_codes": {"0": "within 600 m: anything needs ULC's agreement", "1": "objects over 15 m need ULC's agreement",
                           "2": "height limit for wind turbines only"},
            "equipment": equipment, "zones": zones, "rings": rings}
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Land aviation: {len(equipment)} pieces of equipment, {len(zones)} zones, {len(rings)} height rings "
          f"({os.path.getsize(OUT) // 1024} KB), files {files}")


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

    changed = done = touching = 0
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
            if plot.get("av") != av:
                plot["av"] = av
                touched = True
            done += 1
        if touched:
            json.dump(stations, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    print(f"Land aviation: {done} plots checked, {touching} touch a zone; {changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update-from", metavar="DIR")
    args = parser.parse_args()
    if args.update_from:
        update_from(args.update_from)
    else:
        main()
