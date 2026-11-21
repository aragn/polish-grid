"""
Neighbouring plots on the Land tab:

    python etl/land_blocks.py

A solar farm or battery site is often several neighbouring plots.  This finds, for every plot in data/land/, the plots
of 1 ha or more that touch it (within TOUCH_M, since outlines are simplified to about 1.5 m), and stores them as `nx`.
A neighbour in the same precinct is written as just its number ("98/2" for "280205_5.0022.98/2").  The page grows a
site from a plot by adding its largest neighbours that pass the reader's filters, until the project fits or the site
has as many plots as the reader allows.  Joining every touching plot into one block doesn't work: Polish farmland is a
continuous mosaic, and 70,000 of 77,545 plots would form blocks of up to 6,000 ha.

Smaller plots between them aren't in data/land/, so they don't link plots.  Fast; recomputed in full each run.
"""

import glob
import json
import os
import sys

import shapely

from land_protected import plot_shape, station_files

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
TOUCH_M = 2.0



def short(pid, other):
    """other relative to pid's precinct: '280205_5.0022.98/2' next to '280205_5.0022.98/1' -> '98/2'."""
    prefix = pid.rsplit(".", 1)[0] + "."
    return other[len(prefix):] if other.startswith(prefix) else other


def neighbours(plots):
    """{plot id: [neighbour ids]}."""
    ids = list(plots)
    shapes = [plot_shape(plots[i]) for i in ids]
    keep = [k for k, s in enumerate(shapes) if s is not None and not s.is_empty]
    ids, shapes = [ids[k] for k in keep], [shapes[k] for k in keep]
    tree = shapely.STRtree(shapes)
    left, right = tree.query(shapes, predicate="dwithin", distance=TOUCH_M)
    out = {}
    for a, b in zip(left, right):
        if a != b:
            out.setdefault(ids[int(a)], []).append(ids[int(b)])
    return {pid: sorted(short(pid, n) for n in nbrs) for pid, nbrs in out.items()}


def main():
    files = station_files()
    plots = {}
    for path in files:
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            plots[plot["id"]] = plot
    result = neighbours(plots)
    changed = 0
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            nx = result.get(plot["id"])
            if plot.pop("bk", None) is not None:
                touched = True
            if plot.get("nx") != nx:
                if nx is None:
                    plot.pop("nx", None)
                else:
                    plot["nx"] = nx
                touched = True
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    counts = [len(v) for v in result.values()]
    print(f"Land neighbours: {len(plots)} plots, {len(result)} touch another plot of 1 ha or more "
          f"(on average {sum(counts) / max(len(counts), 1):.1f} neighbours), {changed} station files updated")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
