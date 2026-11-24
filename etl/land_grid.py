"""
Power lines and substations around each shortlisted substation, for the Land tab's map:

    python etl/land_grid.py                   # substations without a file, or with one older than REFRESH_DAYS
    python etl/land_grid.py --all
    python etl/land_grid.py --budget-minutes 90

Source: GUGiK's topographic database BDOT10k (schemat 2021), per county, from opendata.geoportal.gov.pl.  Only the
few layers needed are read out of each county's zip, by HTTP range requests (about 2 MB of a 100-400 MB file):

* OT_SULN_L, power lines: medium voltage (in Poland 15, 20 or 30 kV), high voltage (110 kV) and extra-high voltage
  (220 or 400 kV), as BDOT10k classes them;
* OT_KUPG_A, industrial sites: substations ("podstacja elektroenergetyczna"), with their names when given, and power
  plants ("elektrownia") by kind, solar farms among them, with their outlines (no names: some are sole traders');
* OT_BUWT_P, towers: wind turbines ("turbina wiatrowa"), with their height and whether they are built.

Solar farms and wind turbines near a substation are possible hosts for a battery or another plant sharing their
connection (cable pooling, Energy Law art. 7(1f) as amended by Dz.U. 2026 poz. 516).

Low-voltage lines and MV/LV transformer stations aren't in BDOT10k; the page offers GUGiK's utility map (GESUT) for
them, which draws only close in (1:2,000) and doesn't answer from every country.
The substations of 110 kV and above that OpenStreetMap names (etl/osm_substations_110.json) are added for their names.

Writes data/land/grid/<station>.json for each station: {"lines": {"ehv": [...], "hv": [...], "mv": [...]}, "subs":
[[kind, name, lon, lat]], "plants": [[kind, ha, [[lon, lat], ...]]], "turbines": [[lon, lat, height m, built 1/0]]},
lines as [[lon, lat], ...] within RADIUS_M of the station, simplified to SIMPLIFY_M.  VERSION bumps redo every file.
"""

import argparse
import datetime
import io
import json
import os
import sys
import time
import zipfile

import requests
import shapely
from pyogrio import raw
from pyproj import Transformer

from land_budget import capped
from land_protected import station_files

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
OUT = os.path.join(LAND, "grid")
URL = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG/{woj}/{county}_GPKG.zip"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
RADIUS_M = 4000
SIMPLIFY_M = 3.0
REFRESH_DAYS = 180
VERSION = 2              # 2: power plants and wind turbines
PLANT_KIND = {"słoneczna": "solar", "wodna": "hydro", "wiatrowa": "wind"}
LINE_CLASS = {"linia elektroenergetyczna najwyższego napięcia": "ehv", "linia elektroenergetyczna wysokiego napięcia": "hv",
              "linia elektroenergetyczna średniego napięcia": "mv"}
TO_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
TO_4326 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)


class RangeFile(io.RawIOBase):
    """A remote file read by HTTP range requests, so zipfile can pick single members out of a large archive."""

    def __init__(self, url):
        super().__init__()
        self.url, self.pos = url, 0
        self.size = int(self._request("HEAD").headers["Content-Length"])

    def _request(self, method, headers=None):
        for attempt in range(4):
            try:
                response = requests.request(method, self.url, headers={**UA, **(headers or {})}, timeout=180)
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(15 * (attempt + 1))

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def readinto(self, buffer):
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        data = self._request("GET", {"Range": f"bytes={self.pos}-{self.pos + n - 1}"}).content
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


def read_member(archive, suffix, folder, bbox, columns):
    names = [n for n in archive.namelist() if n.endswith(f"__{suffix}.gpkg")]
    if not names:
        return [], {c: [] for c in columns}
    path = os.path.join(folder, f"{suffix}.gpkg")
    with open(path, "wb") as handle:
        handle.write(archive.read(names[0]))
    try:
        meta, _fids, geometry, fields = raw.read(path, columns=list(columns), bbox=bbox)
    finally:
        os.remove(path)
    if geometry is None or len(geometry) == 0:
        return [], {c: [] for c in columns}
    shapes = shapely.from_wkb(geometry)
    present = list(meta["fields"]) if meta.get("fields") is not None else []
    cols = {c: (list(fields[present.index(c)]) if c in present else [None] * len(shapes)) for c in columns}
    return list(shapes), cols


def county_objects(county, bbox, folder):
    """Lines {class: [geometries]} and substations [(kind, name, point)] of one county within bbox (EPSG:2180)."""
    archive = zipfile.ZipFile(io.BufferedReader(RangeFile(URL.format(woj=county[:2], county=county)), buffer_size=1 << 20))
    lines = {"ehv": [], "hv": [], "mv": []}
    geoms, f = read_member(archive, "OT_SULN_L", folder, bbox, ["RODZAJ", "KATEGORIAISTNIENIA"])
    for i, g in enumerate(geoms):
        k = LINE_CLASS.get(f["RODZAJ"][i])
        if k and g is not None and f["KATEGORIAISTNIENIA"][i] not in ("zniszczony",):
            lines[k].append(g)
    subs, plants, turbines = [], [], []
    geoms, f = read_member(archive, "OT_KUPG_A", folder, bbox, ["RODZAJ", "NAZWA", "RODZAJELEKTROWNI"])
    for i, g in enumerate(geoms):
        if g is None:
            continue
        if f["RODZAJ"][i] == "podstacja elektroenergetyczna":
            subs.append(("substation", f["NAZWA"][i] or "", g.representative_point(), g))
        elif f["RODZAJ"][i] in ("elektrownia", "elektrociepłownia"):
            plants.append((PLANT_KIND.get(f["RODZAJELEKTROWNI"][i], "other"), g))
    geoms, f = read_member(archive, "OT_BUWT_P", folder, bbox, ["RODZAJ", "WYSOKOSC", "KATEGORIAISTNIENIA"])
    for i, g in enumerate(geoms):
        if g is not None and f["RODZAJ"][i] == "turbina wiatrowa" and f["KATEGORIAISTNIENIA"][i] != "zniszczony":
            height = f["WYSOKOSC"][i]
            turbines.append((g, int(round(float(height))) if height not in (None, "") and float(height) == float(height) else None,
                             0 if f["KATEGORIAISTNIENIA"][i] == "w budowie" else 1))
    return lines, subs, plants, turbines


def to_lonlat(geom):
    parts = getattr(geom, "geoms", [geom])
    out = []
    for part in parts:
        if part.is_empty or part.geom_type != "LineString":
            continue
        xs, ys = part.xy
        lon, lat = TO_4326.transform(list(xs), list(ys))
        out.append([[round(a, 5), round(b, 5)] for a, b in zip(lon, lat)])
    return out


def main(redo_all=False, budget_minutes=90):
    os.makedirs(OUT, exist_ok=True)
    index = json.load(open(os.path.join(LAND, "index.json"), encoding="utf-8"))
    today = datetime.date.today()
    # Counties of each station: those of its plots (the county is the first four digits of a plot id).
    counties_of = {}
    for path in station_files():
        sid = os.path.basename(path)[:-5]
        counties_of[sid] = {p["id"][:4] for p in json.load(open(path, encoding="utf-8"))["plots"]}
    todo = []
    for sid, st in index["stations"].items():
        out_path = os.path.join(OUT, f"{sid}.json")
        if not redo_all and os.path.exists(out_path):
            old = json.load(open(out_path, encoding="utf-8"))
            if old.get("v") == VERSION and (today - datetime.date.fromisoformat(old.get("made", "2000-01-01"))).days < REFRESH_DAYS:
                continue
        if counties_of.get(sid):
            todo.append(sid)
    print(f"Land grid: {len(todo)} substations to map", flush=True)
    if not todo:
        return False
    centres = {}
    for sid in todo:
        st = index["stations"][sid]
        x, y = TO_2180.transform(st["lon"], st["lat"])
        centres[sid] = shapely.Point(x, y).buffer(RADIUS_M)
    by_county = {}
    for sid in todo:
        for c in counties_of[sid]:
            by_county.setdefault(c, []).append(sid)
    osm = json.load(open(os.path.join(HERE, "osm_substations_110.json"), encoding="utf-8")).get("substations", [])
    deadline = time.time() + capped(budget_minutes, 20, "Land grid") * 60
    folder = os.path.join(OUT, ".tmp")
    os.makedirs(folder, exist_ok=True)
    found = {sid: {"lines": {"ehv": [], "hv": [], "mv": []}, "subs": [], "plants": [], "turbines": []} for sid in todo}
    done_counties, failed = set(), set()
    t0 = time.time()
    for n, (county, sids) in enumerate(sorted(by_county.items(), key=lambda kv: -len(kv[1]))):
        if time.time() > deadline:
            break
        area = shapely.union_all([centres[s] for s in sids])
        try:
            lines, subs, plants, turbines = county_objects(county, area.bounds, folder)
        except Exception as error:  # noqa: BLE001 -- a county that fails is tried again next run
            failed.add(county)
            print(f"Land grid: {county} failed: {str(error)[:160]}", flush=True)
            continue
        done_counties.add(county)
        for sid in sids:
            circle = centres[sid]
            for k, geoms in lines.items():
                for g in geoms:
                    if g.intersects(circle):
                        found[sid]["lines"][k] += to_lonlat(shapely.simplify(g.intersection(circle), SIMPLIFY_M))
            for kind, name, point, _shape in subs:
                if point.within(circle):
                    lon, lat = TO_4326.transform(point.x, point.y)
                    found[sid]["subs"].append([kind, name, round(lon, 5), round(lat, 5)])
            for kind, shape in plants:
                if shape.intersects(circle):
                    outline = shapely.simplify(shape, 5.0)
                    rings = [p.exterior for p in getattr(outline, "geoms", [outline]) if p.geom_type == "Polygon"]
                    for ring in rings:
                        xs, ys = ring.xy
                        lon, lat = TO_4326.transform(list(xs), list(ys))
                        found[sid]["plants"].append([kind, round(shape.area / 10000, 1), [[round(a, 5), round(b, 5)] for a, b in zip(lon, lat)]])
            for point, height, built in turbines:
                if point.within(circle):
                    lon, lat = TO_4326.transform(point.x, point.y)
                    found[sid]["turbines"].append([round(lon, 5), round(lat, 5), height, built])
        if (n + 1) % 20 == 0:
            print(f"Land grid: {n + 1}/{len(by_county)} counties, {time.time() - t0:.0f} s", flush=True)
    os.rmdir(folder)
    written = 0
    for sid in todo:
        if not counties_of[sid] <= done_counties:
            continue                                  # a county not read yet or failed: try the station next run
        circle = centres[sid]
        for _key, name, lat, lon, voltage, operator in osm:       # [key, name, lat, lon, voltage, operator]
            x, y = TO_2180.transform(lon, lat)
            if circle.contains(shapely.Point(x, y)):
                kv = "/".join(str(int(v) // 1000) for v in str(voltage).split(";") if v.strip().isdigit())
                label = " · ".join(t for t in (name, operator if operator != name else "", f"{kv} kV" if kv else "") if t)
                found[sid]["subs"].append(["osm", label, round(lon, 5), round(lat, 5)])
        json.dump({"station": sid, "made": today.isoformat(), "v": VERSION, "radius_m": RADIUS_M, **found[sid]},
                  open(os.path.join(OUT, f"{sid}.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        written += 1
    print(f"Land grid: {len(done_counties)} counties read ({len(failed)} failed) in {time.time() - t0:.0f} s; {written} substations mapped, "
          f"{len(todo) - written} left for the next run")
    return written > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--budget-minutes", type=float, default=90)
    args = parser.parse_args()
    main(args.all, args.budget_minutes)
