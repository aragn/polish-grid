"""
Solar yield of each land plot of the Land tab, from the Global Solar Atlas:

    python etl/land_solar.py          # plots without a value
    python etl/land_solar.py --all

Source: etl/gsa_pvout_poland.tif, the Global Solar Atlas 2.0 PVOUT layer for Poland (long-term yearly average of
photovoltaic power potential, kWh per kWp per year, 1999-2018, 30 arc-seconds, about 1 km), rounded to whole kWh.
It was downloaded once by hand from globalsolaratlas.info/download/poland, since the atlas's terms don't allow
automated access, and is kept in the repository under its licence (CC BY 4.0).  Replace the file to update it.

Adds `py`, the PVOUT at the plot's centre (the nearest valid cell within two cells when the centre's is empty).
Across Poland it runs from about 1,020 to 1,130 kWh/kWp: a site's yield matters less than its grid connection.

Data/information obtained from the "Global Solar Atlas 2.0, a free, web-based application is developed and operated
by the company Solargis s.r.o. on behalf of the World Bank Group, utilizing Solargis data, with funding provided by
the Energy Sector Management Assistance Program (ESMAP).  For additional information: https://globalsolaratlas.info
"""

import argparse
import json
import os
import sys

import numpy as np
import rasterio

from land_protected import station_files

HERE = os.path.dirname(os.path.abspath(__file__))
RASTER = os.path.join(HERE, "gsa_pvout_poland.tif")


def centre(plot):
    ring = plot["geom"][0][0]
    return sum(c[0] for c in ring) / len(ring), sum(c[1] for c in ring) / len(ring)


def main(redo_all=False):
    with rasterio.open(RASTER) as src:
        grid = src.read(1)
        transform = src.transform
    files = station_files()
    changed = done = 0
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if not redo_all and "py" in plot:
                continue
            lon, lat = centre(plot)
            col, row = ~transform * (lon, lat)
            r, c = int(row), int(col)
            value = None
            for radius in range(3):
                window = grid[max(r - radius, 0): r + radius + 1, max(c - radius, 0): c + radius + 1]
                valid = window[window > 0]
                if valid.size:
                    value = int(grid[r, c]) if radius == 0 else int(np.median(valid))
                    break
            if plot.get("py") != value:
                plot["py"] = value
                touched = True
            done += 1
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    print(f"Land solar yield: {done} plots read, {changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    main(args.all)
