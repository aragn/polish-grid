"""
Terrain of each land plot of the Land tab, from the Copernicus DEM GLO-30:

    python etl/land_terrain.py          # plots not yet checked
    python etl/land_terrain.py --all

Source: Copernicus DEM GLO-30 (1 arc-second, about 30 m), read straight from the AWS open-data copy
(copernicus-dem-30m.s3.amazonaws.com, cloud-optimised GeoTIFFs, one per degree square).  Only the window around each
substation's plots is read.  It is a surface model: trees and buildings raise it, so slopes at forest edges and
around buildings are overstated (those parts are already taken out of the usable area).

Adds, per plot: `el`, mean height above sea level (m); `sl`, mean slope (degrees); `sf`, the share of the plot
flatter than FLAT_DEG (%); `ss`, the share of its sloping part (steeper than FLAT_DEG) that faces south, between
south-east and south-west (%), or null when the plot is flat.  Ground-mounted solar copes with gentle slopes, best
facing south; a battery wants flat ground.

© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union
and ESA; all rights reserved.  (The licence allows free use, including commercial, with this notice.)
"""

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import shapely
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

from land_budget import capped
from land_protected import station_files

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
TILE = "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM/Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM.tif"
FLAT_DEG = 2.0
PAD_DEG = 0.002
GDAL_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif", "GDAL_HTTP_MAX_RETRY": "4", "GDAL_HTTP_RETRY_DELAY": "5"}


def plot_lonlat(plot):
    """The plot's outline as a shapely geometry in lon/lat."""
    polys = [shapely.Polygon(poly[0], poly[1:]) for poly in plot["geom"] if len(poly[0]) >= 3]
    if not polys:
        return None
    return shapely.make_valid(polys[0] if len(polys) == 1 else shapely.MultiPolygon(polys))


def read_window(west, south, east, north):
    """Heights (2-D array, north row first) over the box, mosaicked from the degree tiles it touches, and the
    box actually covered (west, north, pixel size in degrees x and y)."""
    # Copernicus tiles between 50 and 60 N are 3600 x 2400 pixels (1" north-south, 1.5" east-west); finer ones are resampled to this.
    res_y, res_x = 1 / 3600, 1.5 / 3600
    west, east = math.floor(west / res_x) * res_x, math.ceil(east / res_x) * res_x
    south, north = math.floor(south / res_y) * res_y, math.ceil(north / res_y) * res_y
    width, height = int(round((east - west) / res_x)), int(round((north - south) / res_y))
    out = np.full((height, width), np.nan, dtype="float32")
    with rasterio.Env(**GDAL_ENV):
        for lat in range(math.floor(south), math.floor(north - 1e-9) + 1):
            for lon in range(math.floor(west), math.floor(east - 1e-9) + 1):
                url = "/vsicurl/" + TILE.format(lat=lat, lon=lon)
                for attempt in range(4):
                    try:
                        with rasterio.open(url) as src:
                            w, s, e, n = max(west, lon), max(south, lat), min(east, lon + 1), min(north, lat + 1)
                            # Tiles south of 50 N have finer east-west pixels: resample every tile onto the same grid.
                            shape = (int(round((n - s) / res_y)), int(round((e - w) / res_x)))
                            data = src.read(1, window=from_bounds(w, s, e, n, src.transform), out_shape=shape, boundless=True,
                                            fill_value=np.nan, resampling=Resampling.bilinear).astype("float32")
                        break
                    except rasterio.errors.RasterioIOError:
                        if attempt == 3:
                            raise
                        time.sleep(10 * (attempt + 1))
                r0, c0 = int(round((north - n) / res_y)), int(round((w - west) / res_x))
                out[r0:r0 + data.shape[0], c0:c0 + data.shape[1]] = data[: height - r0, : width - c0]
    return out, west, north, res_x, res_y


def station_values(plots):
    """{plot id: (el, sl, sf, ss)} for one substation's plots."""
    shapes = {pid: plot_lonlat(p) for pid, p in plots.items()}
    shapes = {pid: s for pid, s in shapes.items() if s is not None and not s.is_empty}
    if not shapes:
        return {}
    west, south, east, north = shapely.total_bounds(list(shapes.values()))
    z, x0, y0, rx, ry = read_window(west - PAD_DEG, south - PAD_DEG, east + PAD_DEG, north + PAD_DEG)
    lat0 = (south + north) / 2
    dx, dy = rx * 111320 * math.cos(math.radians(lat0)), ry * 110574
    gy, gx = np.gradient(z, dy, dx)          # rows run north to south: gy is the change going south
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    # Downslope direction (the way the ground faces), clockwise from north: east is +x, north is -row.
    aspect = (np.degrees(np.arctan2(-gx, gy)) + 360) % 360
    rows, cols = z.shape
    xs = x0 + (np.arange(cols) + 0.5) * rx
    ys = y0 - (np.arange(rows) + 0.5) * ry
    out = {}
    for pid, shape in shapes.items():
        bx0, by0, bx1, by1 = shape.bounds
        c0, c1 = max(int((bx0 - x0) / rx) - 1, 0), min(int((bx1 - x0) / rx) + 2, cols)
        r0, r1 = max(int((y0 - by1) / ry) - 1, 0), min(int((y0 - by0) / ry) + 2, rows)
        gxs, gys = np.meshgrid(xs[c0:c1], ys[r0:r1])
        inside = shapely.contains_xy(shape, gxs, gys)
        if not inside.any():                 # a plot narrower than a pixel: take the pixel at its centre
            c = shape.representative_point()
            ri, ci = int((y0 - c.y) / ry), int((c.x - x0) / rx)
            if not (0 <= ri < rows and 0 <= ci < cols):
                continue
            inside = np.zeros_like(gxs, dtype=bool)
            r0, c0, inside = ri, ci, np.ones((1, 1), dtype=bool)
            r1, c1 = ri + 1, ci + 1
        zz, ss_, aa = z[r0:r1, c0:c1][inside], slope[r0:r1, c0:c1][inside], aspect[r0:r1, c0:c1][inside]
        ok = ~np.isnan(zz) & ~np.isnan(ss_)
        if not ok.any():
            continue
        zz, ss_, aa = zz[ok], ss_[ok], aa[ok]
        sloped = ss_ >= FLAT_DEG
        south_facing = sloped & (aa >= 135) & (aa <= 225)
        out[pid] = (int(round(float(zz.mean()))), round(float(ss_.mean()), 1), int(round(100 * float((~sloped).mean()))),
                    int(round(100 * south_facing.sum() / sloped.sum())) if sloped.any() else None)
    return out


def main(redo_all=False, only=None, budget_minutes=120):
    files = station_files()
    by_station, plots = {}, {}
    for path in files:
        sid = os.path.basename(path)[:-5]
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            if redo_all or "sl" not in plot:
                if plot["id"] not in plots:
                    by_station.setdefault(sid, []).append(plot["id"])
                plots[plot["id"]] = plot
    if only:
        by_station = {s: v for s, v in by_station.items() if s in only}
    print(f"Land terrain: {sum(len(v) for v in by_station.values())} plots to check around {len(by_station)} substations", flush=True)
    if not by_station:
        return False
    t0, results, failed, skipped = time.time(), {}, 0, 0
    deadline = t0 + capped(budget_minutes, 30, "Land terrain") * 60

    def work(sid):
        if time.time() > deadline:
            return None
        return station_values({pid: plots[pid] for pid in by_station[sid]})

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [(sid, pool.submit(work, sid)) for sid in by_station]
        for i, (sid, future) in enumerate(futures):
            try:
                values = future.result()
                if values is None:
                    skipped += 1
                    continue
                results.update(values)
            except Exception as error:  # noqa: BLE001 -- one substation's failure doesn't stop the others
                failed += 1
                print(f"Land terrain: {sid} failed: {str(error)[:160]}", flush=True)
            if (i + 1) % 100 == 0:
                print(f"Land terrain: {i + 1}/{len(futures)} substations, {time.time() - t0:.0f} s", flush=True)
    changed = 0
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if plot["id"] in results and (redo_all or "sl" not in plot):
                plot["el"], plot["sl"], plot["sf"], plot["ss"] = results[plot["id"]]
                touched = True
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    print(f"Land terrain: {len(results)} plots checked in {time.time() - t0:.0f} s ({failed} substations failed, {skipped} left for the next run), {changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--budget-minutes", type=float, default=120)
    args = parser.parse_args()
    main(args.all, args.only, args.budget_minutes)
