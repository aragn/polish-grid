"""
What lies on each land plot of the Land tab, from GUGiK's topographic database BDOT10k:

    python etl/land_constraints.py                       # the counties with plots not yet checked, within the budget
    python etl/land_constraints.py --budget-minutes 240
    python etl/land_constraints.py --county 1210         # one county (test)

For every plot in data/land/<station>.json it adds, in hectares: forest and wooded land (OT_PTLZ_A), surface water
(OT_PTWP_A), built-up land (OT_PTZB_A) and transport land (OT_PTKM_A, plus road and railway centre lines buffered
by ROAD_HALF_WIDTH_M); the usable area left once all of them are taken out (overlaps counted once); the distance to
the nearest building (OT_BUBD_A, 0 when one stands on the plot) and the number of buildings on it.  Short keys:
f, w, b, r (ha), u (usable ha), bm (m), nb.

`d` is the distance in metres from the plot to the nearest object of each kind in DISTANCES (0 when one touches the
plot, null when there is none within DISTANCE_SEARCH_M), in the order index.json lists as `distance_keys`.  These let
the page apply any buffer (homes, public roads, power lines, forest, water...), as site-screening tools do.  A plot
checked before the distances existed (no `d`), or before the last kinds were added (a shorter `d`), is checked again
in full.  New kinds go at the end of DISTANCES, so the positions of the others never change.

Kinds and classes are those of the 2021 schema (Dz.U. 2021 poz. 1412, annex 3; GUGiK's BDOT10k user guide, 2024).
Gas and oil mains aren't among them: BDOT10k's pipe class (OT_SUPR_L) holds only pipes above ground.

BDOT10k comes per county (powiat) as a zip of GeoPackages, one per layer, from opendata.geoportal.gov.pl (schemat
2021, EPSG:2180), which answers from GitHub's runners but not from every network.  The county is the first four
digits of a plot's identifier.  Each county is downloaded once per run, read only within the box of its plots, and
deleted.  A plot keeps its values until land.py fetches it again (which drops them), so later runs only do what is new.
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

import requests
import shapely
from pyogrio import raw
from pyproj import Transformer

from land_protected import station_files
from land_budget import capped

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAND = os.path.join(ROOT, "data", "land")
URL = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG/{woj}/{county}_GPKG.zip"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
ROAD_HALF_WIDTH_M = 4.0         # a road or railway line counts as an 8 m strip
BUILDING_SEARCH_M = 3000
LAYERS = {"forest": "OT_PTLZ_A", "water": "OT_PTWP_A", "built": "OT_PTZB_A", "transport": "OT_PTKM_A",
          "roads": "OT_SKDR_L", "rail": "OT_SKTR_L", "buildings": "OT_BUBD_A"}
DISTANCE_SEARCH_M = 3000
# Distances: key -> [(layer, test on the layer's attributes at row i)].  Attribute values as in BDOT10k schemat 2021.
MAJOR_ROADS = ("autostrada", "droga ekspresowa", "droga główna ruchu przyśpieszonego", "droga główna")
DISTANCES = {
    "home": [("OT_BUBD_A", lambda f, i: f["FUNKCJAOGOLNABUDYNKU"][i] == "budynki mieszkalne" and f["KATEGORIAISTNIENIA"][i] != "zniszczony")],
    "housing": [("OT_PTZB_A", lambda f, i: f["RODZAJ"][i] in ("jednorodzinna", "wielorodzinna"))],
    "public_road": [("OT_SKDR_L", lambda f, i: f["KATEGORIAZARZADZANIA"][i] not in (None, "wewnętrzna"))],
    "any_road": [("OT_SKDR_L", lambda f, i: True)],
    "major_road": [("OT_SKDR_L", lambda f, i: f["KLASADROGI"][i] in MAJOR_ROADS)],
    "railway": [("OT_SKTR_L", lambda f, i: f["KATEGORIAISTNIENIA"][i] != "nieczynny")],
    "forest": [("OT_PTLZ_A", lambda f, i: f["RODZAJ"][i] == "las")],
    "standing_water": [("OT_PTWP_A", lambda f, i: f["RODZAJ"][i] == "woda stojąca")],
    "river": [("OT_PTWP_A", lambda f, i: f["RODZAJ"][i] == "woda płynąca"), ("OT_SWRS_L", lambda f, i: True)],
    "hv_line": [("OT_SULN_L", lambda f, i: "wysokiego" in (f["RODZAJ"][i] or "") or "najwyższego" in (f["RODZAJ"][i] or ""))],
    "mv_line": [("OT_SULN_L", lambda f, i: "średniego" in (f["RODZAJ"][i] or ""))],
    "substation": [("OT_KUPG_A", lambda f, i: f["RODZAJ"][i] == "podstacja elektroenergetyczna")],
    "solar_farm": [("OT_KUPG_A", lambda f, i: f["RODZAJ"][i] == "elektrownia" and f["RODZAJELEKTROWNI"][i] == "słoneczna")],
    "power_plant": [("OT_KUPG_A", lambda f, i: f["RODZAJ"][i] in ("elektrownia", "elektrociepłownia") and f["RODZAJELEKTROWNI"][i] != "słoneczna")],
    "industry": [("OT_PTZB_A", lambda f, i: f["RODZAJ"][i] == "przemysłowo-składowa")],
    "waste": [("OT_KUPG_A", lambda f, i: f["RODZAJ"][i] == "składowisko odpadów")],
    "cemetery": [("OT_KUSC_A", lambda f, i: str(f["RODZAJ"][i] or "").startswith("cmentarz"))],
    "airfield": [("OT_KUKO_A", lambda f, i: f["RODZAJ"][i] == "lotnisko lub lądowisko")],
    # Added Nov 2026: places that are sensitive to noise and fire risk, heritage, military ranges, existing turbines.
    "school": [("OT_KUOS_A", lambda f, i: f["RODZAJ"][i] in ("przedszkole", "szkoła lub zespół szkół", "szkoła wyższa"))],
    "care": [("OT_KUOZ_A", lambda f, i: True)],          # hospitals and sanatoria, care homes and children's homes, nurseries
    "heritage": [("OT_KUZA_A", lambda f, i: True)],      # historic complexes: historic buildings, museums, open-air museums, forts, memorials
    "military": [("OT_KUPW_A", lambda f, i: True)],      # military training ranges
    "wind_turbine": [("OT_BUWT_P", lambda f, i: f["RODZAJ"][i] == "turbina wiatrowa")],
}
DISTANCE_KEYS = list(DISTANCES)
DISTANCE_COLUMNS = {"OT_BUBD_A": ["FUNKCJAOGOLNABUDYNKU", "KATEGORIAISTNIENIA"], "OT_PTZB_A": ["RODZAJ"], "OT_SKDR_L": ["KATEGORIAZARZADZANIA", "KLASADROGI"],
                    "OT_SKTR_L": ["KATEGORIAISTNIENIA"], "OT_PTLZ_A": ["RODZAJ"], "OT_PTWP_A": ["RODZAJ"], "OT_SWRS_L": [], "OT_SULN_L": ["RODZAJ"],
                    "OT_KUPG_A": ["RODZAJ", "RODZAJELEKTROWNI"], "OT_KUSC_A": ["RODZAJ"], "OT_KUKO_A": ["RODZAJ"],
                    "OT_KUOS_A": ["RODZAJ"], "OT_KUOZ_A": [], "OT_KUZA_A": [], "OT_KUPW_A": [], "OT_BUWT_P": ["RODZAJ"]}
TO_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)


def plot_shape(plot):
    """The plot's outline ([[[lon, lat], ...], holes...] per polygon) as a shapely geometry in EPSG:2180."""
    polygons = []
    for poly in plot["geom"]:
        rings = []
        for ring in poly:
            xs, ys = TO_2180.transform([c[0] for c in ring], [c[1] for c in ring])
            rings.append(list(zip(xs, ys)))
        if len(rings[0]) >= 3:
            polygons.append(shapely.Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3]))
    shape = shapely.make_valid(shapely.MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]) if polygons else None
    return shape


def download(county, folder):
    url = URL.format(woj=county[:2], county=county)
    path = os.path.join(folder, f"{county}.zip")
    for attempt in range(4):
        try:
            with requests.get(url, headers=UA, timeout=300, stream=True) as response:
                response.raise_for_status()
                with open(path, "wb") as handle:
                    for chunk in response.iter_content(1 << 20):
                        handle.write(chunk)
            return path
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))


def read_layer(archive, suffix, folder, bbox, columns=()):
    """The geometries of one layer within bbox (EPSG:2180) and {column: values} of the columns asked for; empty when
    the county has none.  A column the layer lacks comes back as all None."""
    names = [n for n in archive.namelist() if n.endswith(f"__{suffix}.gpkg")]
    if not names:
        return [], {c: [] for c in columns}
    archive.extract(names[0], folder)
    path = os.path.join(folder, names[0])
    try:
        meta, _fids, geometry, fields = raw.read(path, columns=list(columns), bbox=bbox)
    finally:
        os.remove(path)
    if geometry is None or len(geometry) == 0:
        return [], {c: [] for c in columns}
    shapes = shapely.from_wkb(geometry)
    present = list(meta["fields"]) if meta.get("fields") is not None else []
    cols = {c: (list(fields[present.index(c)]) if c in present else [None] * len(shapes)) for c in columns}
    keep = [i for i, g in enumerate(shapes) if g is not None and not g.is_empty]
    return [shapes[i] for i in keep], {c: [v[i] for i in keep] for c, v in cols.items()}


def distances(shapes, tree_geoms):
    """For each shape (in order), the distance in metres to the nearest of tree_geoms, 0 when touching, None beyond
    DISTANCE_SEARCH_M."""
    out = [None] * len(shapes)
    if not tree_geoms:
        return out
    tree = shapely.STRtree(tree_geoms)
    (inputs, _hits), dist = tree.query_nearest(shapes, max_distance=DISTANCE_SEARCH_M, return_distance=True, all_matches=False)
    for i, d in zip(inputs, dist):
        out[int(i)] = int(round(float(d)))
    return out


def county_values(county, plots, workdir):
    """{plot id: values} for the plots of one county."""
    shapes = {pid: plot_shape(p) for pid, p in plots.items()}
    shapes = {pid: s for pid, s in shapes.items() if s is not None and not s.is_empty}
    if not shapes:
        return {}
    xmin, ymin, xmax, ymax = shapely.total_bounds(list(shapes.values()))
    pad = BUILDING_SEARCH_M
    bbox = (xmin - pad, ymin - pad, xmax + pad, ymax + pad)
    archive = zipfile.ZipFile(download(county, workdir))
    try:
        # Every layer once, with the columns the distances test.
        read = {}
        for suffix in sorted(set(LAYERS.values()) | set(DISTANCE_COLUMNS)):
            read[suffix] = read_layer(archive, suffix, workdir, bbox, DISTANCE_COLUMNS.get(suffix, ()))
        layers = {key: read[suffix][0] for key, suffix in LAYERS.items()}
    finally:
        archive.close()
        os.remove(os.path.join(workdir, f"{county}.zip"))
    ids = list(shapes)
    plot_list = [shapes[pid] for pid in ids]
    nearest = {}
    for key, sources in DISTANCES.items():
        geoms = []
        for suffix, test in sources:
            layer_geoms, fields = read[suffix]
            geoms += [g for i, g in enumerate(layer_geoms) if test(fields, i)]
        nearest[key] = distances(plot_list, geoms)
    lines = [shapely.buffer(g, ROAD_HALF_WIDTH_M, cap_style="flat") for g in layers["roads"] + layers["rail"]]
    groups = {"f": layers["forest"], "w": layers["water"], "b": layers["built"], "r": layers["transport"] + lines}
    trees = {k: shapely.STRtree(v) if v else None for k, v in groups.items()}
    buildings = layers["buildings"]
    building_tree = shapely.STRtree(buildings) if buildings else None
    out, position = {}, {pid: k for k, pid in enumerate(ids)}
    for pid, shape in shapes.items():
        area = shape.area
        values, cut = {}, []
        for key, tree in trees.items():
            if tree is None:
                values[key] = 0.0
                continue
            hits = [groups[key][i] for i in tree.query(shape, predicate="intersects")]
            if not hits:
                values[key] = 0.0
                continue
            part = shapely.intersection(shapely.union_all(hits), shape)
            values[key] = round(min(part.area, area) / 10000, 2)   # simplified outlines can overshoot slightly
            if not part.is_empty:
                cut.append(part)
        taken = shapely.union_all(cut).area if cut else 0.0
        values["u"] = round(max(area - taken, 0.0) / 10000, 2)
        if building_tree is not None:
            inside = building_tree.query(shape, predicate="intersects")
            values["nb"] = int(len(inside))
            if len(inside):
                values["bm"] = 0
            else:
                index, distance = building_tree.query_nearest(shape, max_distance=BUILDING_SEARCH_M, return_distance=True)
                values["bm"] = int(round(float(distance[0]))) if len(index) else None
        else:
            values["nb"], values["bm"] = 0, None
        k = position[pid]
        values["d"] = [nearest[key][k] for key in DISTANCE_KEYS]
        out[pid] = values
    return out


def needs_check(plot):
    """Not checked yet, or checked before the distances (or their latest kinds) were added."""
    return "u" not in plot or "d" not in plot or len(plot["d"]) < len(DISTANCE_KEYS)


def main(budget_minutes=240, only=None):
    files = station_files()
    # Plots still to check, by county (a plot may appear in several stations' files: checked once).
    todo = {}
    for path in files:
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            if needs_check(plot):
                todo.setdefault(plot["id"][:4], {})[plot["id"]] = plot
    counties = sorted(todo, key=lambda c: -len(todo[c]))
    if only:
        counties = [c for c in counties if c == only]
    print(f"Land constraints: {sum(len(v) for v in todo.values())} plots to check in {len(todo)} counties", flush=True)
    deadline = time.time() + capped(budget_minutes, 15, "Land constraints") * 60
    workdir = tempfile.mkdtemp(prefix="bdot10k_")
    results, done = {}, 0
    try:
        for county in counties:
            if time.time() > deadline:
                break
            t0 = time.time()
            try:
                values = county_values(county, todo[county], workdir)
            except Exception as error:  # noqa: BLE001 -- one county's failure doesn't stop the others
                print(f"Land constraints: {county} failed: {str(error)[:200]}", flush=True)
                continue
            results.update(values)
            done += 1
            print(f"Land constraints: {county} {len(values)} plots in {time.time() - t0:.0f} s ({done}/{len(counties)})", flush=True)
            if done % 10 == 0:
                write(files, results)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    changed = write(files, results)
    left = len(counties) - done
    print(f"Land constraints: {done} counties done, {left} left for the next run, {changed} station files updated")
    return changed > 0


def write(files, results):
    if not results:
        return 0
    changed = 0
    index_path = os.path.join(LAND, "index.json")
    index = json.load(open(index_path, encoding="utf-8"))
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if needs_check(plot) and plot["id"] in results:
                plot.update(results[plot["id"]])
                touched = True
        if touched:
            data["checked"] = datetime.date.today().isoformat()
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
        sid = os.path.basename(path)[:-5]
        if sid in index["stations"]:
            plots = data["plots"]
            index["stations"][sid]["checked"] = sum(1 for p in plots if "u" in p)
            index["stations"][sid]["big_usable"] = sum(1 for p in plots if p.get("u", 0) >= 5)
    index["constraints_source"] = "BDOT10k (GUGiK), schemat 2021"
    index["distance_keys"] = DISTANCE_KEYS
    index["distance_search_m"] = DISTANCE_SEARCH_M
    json.dump(index, open(index_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return changed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget-minutes", type=float, default=240)
    parser.add_argument("--county")
    args = parser.parse_args()
    main(args.budget_minutes, args.county)
