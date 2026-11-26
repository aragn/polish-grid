"""
Wind resource of each land plot of the Land tab, from the Global Wind Atlas:

    python etl/land_wind.py                    # plots without a value
    python etl/land_wind.py --all
    python etl/land_wind.py --make-copy DIR    # rebuild etl/gwa_poland.tif from the atlas's Poland GeoTIFFs in DIR

Source: etl/gwa_poland.tif, made from the Global Wind Atlas 4.0 country files for Poland (250 m grid, long-term
averages): band 1 the mean wind speed at 100 m and band 2 at 150 m (m/s × 10), band 3 the capacity factor of the
atlas's generic IEC class III turbine, the class for low-wind sites like most of Poland (% of the year at full power).
The files POL_wind-speed_100m.tif, POL_wind-speed_150m.tif and POL_capacity-factor_IEC3.tif were downloaded once by
hand from globalwindatlas.info (Download, GIS files): the atlas's terms don't allow robots on the site, and its API
isn't meant for bulk downloads.  The data is CC BY 4.0, so the copy is kept in the repository.  Replace it with
--make-copy to update it.

Adds `ws`, [wind speed at 100 m, at 150 m] in m/s, and `cf`, the capacity factor in %, at the plot's centre (the
nearest valid cell within two cells when the centre's is empty).  Across the plots it runs from about 5 to 9 m/s at
150 m: higher on the coast and on open high ground.  The atlas is for screening only: DTU advises against using it
for financial commitments, and a wind farm needs at least a year of measurements on site.

Data obtained from the Global Wind Atlas version 4.0, a free, web-based application developed, owned and operated by
the Technical University of Denmark (DTU).  The Global Wind Atlas version 4.0 is released in partnership with the
World Bank Group, utilizing data provided by Vortex, using funding provided by the Energy Sector Management
Assistance Program (ESMAP).  For additional information: https://globalwindatlas.info.  Main reference: Davis et al.,
The Global Wind Atlas, Bulletin of the American Meteorological Society 104(8), E1507-E1525, 2023,
https://doi.org/10.1175/BAMS-D-21-0075.1
"""

import argparse
import json
import os
import sys

import numpy as np
import rasterio

from land_protected import station_files

HERE = os.path.dirname(os.path.abspath(__file__))
RASTER = os.path.join(HERE, "gwa_poland.tif")
NODATA = 255
LAYERS = [("wind-speed_100m", 10), ("wind-speed_150m", 10), ("capacity-factor_IEC3", 100)]


def make_copy(folder):
    bands, profile = [], None
    for name, scale in LAYERS:
        with rasterio.open(os.path.join(folder, f"POL_{name}.tif")) as src:
            grid = src.read(1)
            profile = profile or src.profile
        bands.append(np.where(np.isnan(grid), NODATA, np.clip(np.round(grid * scale), 0, NODATA - 1)).astype(np.uint8))
    profile.update(dtype="uint8", count=len(bands), nodata=NODATA, compress="deflate", zlevel=9, predictor=2, tiled=True,
                   blockxsize=256, blockysize=256)
    with rasterio.open(RASTER, "w", **profile) as dst:
        for i, band in enumerate(bands, 1):
            dst.write(band, i)
    print(f"Land wind: wrote {RASTER} ({os.path.getsize(RASTER) // 1024} KB)")


def centre(plot):
    ring = plot["geom"][0][0]
    return sum(c[0] for c in ring) / len(ring), sum(c[1] for c in ring) / len(ring)


def sample(grid, r, c):
    """The cell's value, or the median of the valid cells within two cells when it is empty."""
    if not (0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]):
        return None
    for radius in range(3):
        window = grid[max(r - radius, 0): r + radius + 1, max(c - radius, 0): c + radius + 1]
        valid = window[window != NODATA]
        if valid.size:
            return int(grid[r, c]) if radius == 0 else int(np.median(valid))
    return None


def main(redo_all=False):
    with rasterio.open(RASTER) as src:
        grids = src.read()
        transform = src.transform
    changed = done = 0
    for path in station_files():
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if not redo_all and "cf" in plot:
                continue
            lon, lat = centre(plot)
            col, row = ~transform * (lon, lat)
            v100, v150, cf = (sample(g, int(row), int(col)) for g in grids)
            ws = None if v100 is None or v150 is None else [v100 / 10, v150 / 10]
            if plot.get("ws") != ws or plot.get("cf") != cf or "cf" not in plot:
                plot["ws"], plot["cf"] = ws, cf
                touched = True
            done += 1
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    print(f"Land wind: {done} plots read, {changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--make-copy", metavar="DIR")
    args = parser.parse_args()
    if args.make_copy:
        make_copy(args.make_copy)
    else:
        main(args.all)
