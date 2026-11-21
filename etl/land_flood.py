"""
Flood hazard on each land plot of the Land tab, from Wody Polskie's flood hazard maps (mapy zagrożenia powodziowego):

    python etl/land_flood.py          # plots not yet checked
    python etl/land_flood.py --all

Source: the INSPIRE view service of the flood hazard and flood risk maps (ISOK), INSPIRE_NZ_HY_MZPMRP_WMS, river and
sea floods.  These are the maps of the second planning cycle, made public on 7 September 2022 and still the official
ones (the third-cycle update hasn't been adopted; the services label them valid to 22 December 2025).

The WFS of the same maps sends whole river reaches (megabytes each), so instead the WMS draws the areas with our own
style (SLD_BODY), filled: the 10% and 1% scenarios in one colour and the 0.2% and embankment-failure scenarios in
another, one image per substation at RES_M per pixel (the service draws nothing above 1:50,000).  Each plot is sampled
on a GRID_M grid.  The 1% area (which contains the 10% one) is, with the land between a river and its embankment, what
the Water Law calls an area of special flood danger (obszar szczególnego zagrożenia powodzią), where building needs a
derogation.

Adds `q1`, the hectares in the 1% (or 10%) flood area, and `q02`, the further hectares flooded only in the 0.2% or
embankment-failure scenario.
"""

import argparse
import glob
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
import shapely
from PIL import Image

from land_protected import plot_shape, station_files

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
WMS = "https://wody.isok.gov.pl/wss/INSPIRE/INSPIRE_NZ_HY_MZPMRP_WMS"
UA = {"User-Agent": "polish-grid land screening (github.com/aragn/polish-grid)"}
RES_M = 5.0
GRID_M = 10.0
TILE_PX = 1600

# The scenario names exactly as the service's legend gives them (PropertyIsLike doesn't match them).
_LIKELY = ("<ogc:Or>" + "".join(
    f"<ogc:PropertyIsEqualTo><ogc:PropertyName>loo_qualitativelikelihood</ogc:PropertyName><ogc:Literal>{name}</ogc:Literal></ogc:PropertyIsEqualTo>"
    for name in ("scenariusz Q 10% (raz na 10 lat)", "scenariusz Q 1% (raz na 100 lat)", "scenariusz H 1% (raz na 100 lat)")) + "</ogc:Or>")
# Two FeatureTypeStyles, so that every 1% area is painted after (over) all the 0.2% ones; within one, GeoServer paints
# feature by feature and the larger 0.2% areas would cover them.
_RULES = ('<FeatureTypeStyle><Rule><PolygonSymbolizer><Fill><CssParameter name="fill">#0000ff</CssParameter></Fill></PolygonSymbolizer></Rule></FeatureTypeStyle>'
          f'<FeatureTypeStyle><Rule><ogc:Filter>{_LIKELY}</ogc:Filter><PolygonSymbolizer><Fill><CssParameter name="fill">#00ff00</CssParameter></Fill></PolygonSymbolizer></Rule></FeatureTypeStyle>')
SLD = ('<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">'
       + "".join(f'<NamedLayer><Name>{layer}</Name><UserStyle>{_RULES}</UserStyle></NamedLayer>'
                 for layer in ("NZ.Fluvial", "NZ.SeaWater"))
       + '</StyledLayerDescriptor>')



def get_map(x0, y0, x1, y1):
    """RGBA array of the styled flood map over [x0, x1] x [y0, y1] (EPSG:2180, x = easting)."""
    width, height = int(round((x1 - x0) / RES_M)), int(round((y1 - y0) / RES_M))
    params = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": "NZ.Fluvial,NZ.SeaWater", "STYLES": ",",
              "SRS": "EPSG:2180", "BBOX": f"{x0},{y0},{x1},{y1}", "WIDTH": width, "HEIGHT": height,
              "FORMAT": "image/png", "TRANSPARENT": "true", "SLD_BODY": SLD}
    for attempt in range(5):
        try:
            response = requests.get(WMS, params=params, headers=UA, timeout=180)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image"):
                raise ValueError(response.text[:200])
            return np.asarray(Image.open(io.BytesIO(response.content)).convert("RGBA"))
        except (requests.RequestException, ValueError, OSError):
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))


def station_values(shapes):
    """{plot id: (q1 ha, q02 ha)} for the plots of one substation."""
    xmin, ymin, xmax, ymax = shapely.total_bounds(list(shapes.values()))
    xmin, ymin = np.floor(xmin / RES_M) * RES_M - RES_M, np.floor(ymin / RES_M) * RES_M - RES_M
    xmax, ymax = np.ceil(xmax / RES_M) * RES_M + RES_M, np.ceil(ymax / RES_M) * RES_M + RES_M
    span = TILE_PX * RES_M
    tiles = []
    for tx in np.arange(xmin, xmax, span):
        for ty in np.arange(ymin, ymax, span):
            x1, y1 = min(tx + span, xmax), min(ty + span, ymax)
            tiles.append((tx, ty, x1, y1, get_map(tx, ty, x1, y1)))
    out = {}
    for pid, shape in shapes.items():
        bx0, by0, bx1, by1 = shape.bounds
        xs = np.arange(np.floor(bx0 / GRID_M) * GRID_M + GRID_M / 2, bx1, GRID_M)
        ys = np.arange(np.floor(by0 / GRID_M) * GRID_M + GRID_M / 2, by1, GRID_M)
        gx, gy = np.meshgrid(xs, ys)
        gx, gy = gx.ravel(), gy.ravel()
        inside = shapely.contains_xy(shape, gx, gy)
        gx, gy = gx[inside], gy[inside]
        if not len(gx):
            out[pid] = (0.0, 0.0)
            continue
        green = blue = 0
        for tx, ty, x1, y1, img in tiles:
            sel = (gx >= tx) & (gx < x1) & (gy >= ty) & (gy < y1)
            if not sel.any():
                continue
            cols = ((gx[sel] - tx) / RES_M).astype(int).clip(0, img.shape[1] - 1)
            rows = ((y1 - gy[sel]) / RES_M).astype(int).clip(0, img.shape[0] - 1)
            px = img[rows, cols]
            solid = px[:, 3] > 128
            green += int((solid & (px[:, 1] > 128) & (px[:, 2] < 128)).sum())
            blue += int((solid & (px[:, 2] > 128) & (px[:, 1] < 128)).sum())
        n = len(gx)
        ha = shape.area / 10000
        out[pid] = (round(ha * green / n, 2), round(ha * blue / n, 2))
    return out


def main(redo_all=False, only=None):
    files = station_files()
    by_station, plots = {}, {}
    for path in files:
        sid = os.path.basename(path)[:-5]
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            if redo_all or "q1" not in plot:
                if plot["id"] not in plots:
                    by_station.setdefault(sid, []).append(plot["id"])
                plots[plot["id"]] = plot
    if only:
        by_station = {s: v for s, v in by_station.items() if s in only}
    print(f"Land flood: {sum(len(v) for v in by_station.values())} plots to check around {len(by_station)} substations", flush=True)
    if not by_station:
        return False
    t0 = time.time()

    def run(sid):
        shapes = {pid: plot_shape(plots[pid]) for pid in by_station[sid]}
        return station_values({k: v for k, v in shapes.items() if v is not None and not v.is_empty})

    results, failed = {}, 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, (sid, future) in enumerate([(s, pool.submit(run, s)) for s in by_station]):
            try:
                results.update(future.result())
            except Exception as error:  # noqa: BLE001
                failed += 1
                print(f"Land flood: {sid} failed: {str(error)[:160]}", flush=True)
            if (i + 1) % 100 == 0:
                print(f"Land flood: {i + 1}/{len(by_station)} substations, {time.time() - t0:.0f} s", flush=True)
    changed = 0
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if plot["id"] in results:
                plot["q1"], plot["q02"] = results[plot["id"]]
                touched = True
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    print(f"Land flood: {len(results)} plots checked in {time.time() - t0:.0f} s ({failed} substations failed), {changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    main(args.all, args.only)
