"""Road files for the Land tab's connection configuration (land/connect.js): the OpenStreetMap roads, tracks,
watercourses and railways within 4.5 km of each substation, one compact file per substation.

    python etl/land_roads.py poland-latest.osm.pbf data/land/roads data/land/index.json

The extract is Geofabrik's (download.geofabrik.de/europe/poland-latest.osm.pbf, ~2 GB; not kept in the repository).
One pass with node locations in memory (~6 GB of RAM), about 6 minutes.  Rerun when substations are added.

stations.json: {key: {"lat": .., "lon": ..}} (the Land tab's data/land/index.json "stations" works as it is).
Writes <out dir>/<key>.json and <out dir>/index.json (the keys written).

File: {"v": 1, "s": 1e5, "hw": [highway classes], "wk": [waterway kinds], "names": [watercourse names],
       "roads": [[hw index, x0, y0, dx1, dy1, ...]], "water": [[kind index, name index or -1, x0, y0, dx, dy, ...]],
       "rail": [[x0, y0, dx, dy, ...]]}
x, y: longitude and latitude × 1e5 (about a metre), each after the first as a difference from the one before.  Ways are
clipped to the substation's square (points outside it dropped, keeping one beyond each edge).  Data © OpenStreetMap
contributors, ODbL.
"""
import collections
import json
import math
import os
import sys

import osmium

def dp(pts, keep, tol):
    """Douglas-Peucker on integer points (1e-5 deg), never dropping the points in keep (road junctions)."""
    if len(pts) < 3:
        return pts
    k = math.cos(math.radians(pts[0][1] / 1e5))
    def d(p, a, b):
        ax, ay, bx, by, px, py = a[0] * k, a[1], b[0] * k, b[1], p[0] * k, p[1]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0 if not L else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
        return math.hypot(px - ax - t * dx, py - ay - t * dy) * 1.11      # metres (1e-5 deg ~ 1.11 m)
    out = [pts[0]]
    def rec(i, j):
        best, bi = -1, None
        for m in range(i + 1, j):
            if pts[m] in keep:
                bi, best = m, float("inf"); break
            dd = d(pts[m], pts[i], pts[j])
            if dd > best:
                best, bi = dd, m
        if bi is not None and best > tol:
            rec(i, bi); out.append(pts[bi]); rec(bi, j)
    rec(0, len(pts) - 1)
    out.append(pts[-1])
    return out


def unpack(a, start):
    pts, x, y = [], 0, 0
    for i in range(start, len(a), 2):
        x += a[i]; y += a[i + 1]; pts.append((x, y))
    return pts


def pack(pts):
    flat = [pts[0][0], pts[0][1]]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        flat += [x1 - x0, y1 - y0]
    return flat


def shrink(j, tol=3.0):
    """Thin the geometry: roads keep every point shared with another road (the junctions route() needs), else within tol
    metres of the line; watercourses and railways (used only for crossings) within 2 × tol."""
    roads = [(r[0], unpack(r, 1)) for r in j["roads"]]
    seen, junction = set(), set()
    for _, pts in roads:
        for i, p in enumerate(pts):
            if p in seen or i in (0, len(pts) - 1):
                junction.add(p)
            seen.add(p)
    j["roads"] = [[hw] + pack(dp(pts, junction, tol)) for hw, pts in roads]
    j["water"] = [w[:2] + pack(dp(unpack(w, 2), set(), 2 * tol)) for w in j["water"]]
    j["rail"] = [pack(dp(unpack(r, 0), set(), 2 * tol)) for r in j["rail"]]
    return j


if len(sys.argv) > 2 and sys.argv[1] == "--shrink":          # thin files written before (in place)
    import glob
    tot = 0
    for f in glob.glob(os.path.join(sys.argv[2], "*.json")):
        if f.endswith("index.json"):
            continue
        j = shrink(json.load(open(f, encoding="utf-8")))
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(j, fh, ensure_ascii=False, separators=(",", ":"))
        tot += os.path.getsize(f)
    print("thinned:", round(tot / 1e6, 1), "MB")
    sys.exit(0)

PBF, OUT, STATIONS = sys.argv[1], sys.argv[2], sys.argv[3]
KM = float(sys.argv[4]) if len(sys.argv) > 4 else 4.5
os.makedirs(OUT, exist_ok=True)
HW = ["trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
      "unclassified", "road", "residential", "living_street", "service", "track"]
HW_I = {k: i for i, k in enumerate(HW)}
SKIP_SERVICE = {"parking_aisle", "driveway", "drive-through"}
WK = ["river", "stream", "canal", "ditch", "drain"]
WK_I = {k: i for i, k in enumerate(WK)}
RAIL = {"rail", "light_rail", "narrow_gauge"}

st = json.load(open(STATIONS, encoding="utf-8"))
st = st.get("stations", st)
boxes = {}
for k, v in st.items():
    if v.get("lat") is None:
        continue
    dlat = KM / 111.32
    dlon = KM / (111.32 * math.cos(math.radians(v["lat"])))
    boxes[k] = (v["lon"] - dlon, v["lat"] - dlat, v["lon"] + dlon, v["lat"] + dlat)
# a 0.1° grid of the boxes, to find the substations near a way quickly
cells = collections.defaultdict(list)
for k, (w, s, e, n) in boxes.items():
    for cx in range(int(math.floor(w * 10)), int(math.floor(e * 10)) + 1):
        for cy in range(int(math.floor(s * 10)), int(math.floor(n * 10)) + 1):
            cells[(cx, cy)].append(k)
print(len(boxes), "substations,", len(cells), "grid cells")

out = {k: {"roads": [], "water": [], "rail": []} for k in boxes}
names = {}


def runs(pts, box):
    """The parts of the way inside the box, each with one point beyond each edge."""
    w, s, e, n = box
    inside = [w <= x <= e and s <= y <= n for x, y in pts]
    res, cur = [], None
    for i, ok in enumerate(inside):
        if ok:
            if cur is None:
                cur = [pts[i - 1]] if i > 0 else []
            cur.append(pts[i])
        elif cur is not None:
            cur.append(pts[i])
            res.append(cur)
            cur = None
    if cur is not None:
        res.append(cur)
    return [r for r in res if len(r) >= 2]


def enc(pts):
    q = [(round(x * 1e5), round(y * 1e5)) for x, y in pts]
    flat = [q[0][0], q[0][1]]
    for (x0, y0), (x1, y1) in zip(q, q[1:]):
        flat += [x1 - x0, y1 - y0]
    return flat


class H(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.n = 0

    def way(self, wy):
        t = wy.tags
        hw, ww, rw = t.get("highway"), t.get("waterway"), t.get("railway")
        if hw in HW_I:
            if hw == "service" and t.get("service") in SKIP_SERVICE:
                return
            kind = ("road", HW_I[hw])
        elif ww in WK_I:
            kind = ("water", WK_I[ww])
        elif rw in RAIL:
            kind = ("rail", 0)
        else:
            return
        try:
            pts = [(nd.lon, nd.lat) for nd in wy.nodes]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2:
            return
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        near = set()
        for cx in range(int(math.floor(min(xs) * 10)), int(math.floor(max(xs) * 10)) + 1):
            for cy in range(int(math.floor(min(ys) * 10)), int(math.floor(max(ys) * 10)) + 1):
                near.update(cells.get((cx, cy), ()))
        if not near:
            return
        for k in near:
            for r in runs(pts, boxes[k]):
                if kind[0] == "road":
                    out[k]["roads"].append([kind[1]] + enc(r))
                elif kind[0] == "water":
                    nm = t.get("name") or ""
                    ni = names.setdefault(nm, len(names)) if nm else -1
                    out[k]["water"].append([kind[1], ni] + enc(r))
                else:
                    out[k]["rail"].append(enc(r))
        self.n += 1
        if self.n % 500000 == 0:
            print(" ways kept", self.n, flush=True)


h = H()
h.apply_file(PBF, locations=True, idx="sparse_mem_array")
names_list = [None] * len(names)
for nm, i in names.items():
    names_list[i] = nm
written, total = [], 0
for k, v in out.items():
    if not v["roads"]:
        continue
    # only the names this file uses
    used = sorted({w[1] for w in v["water"] if w[1] >= 0})
    remap = {old: new for new, old in enumerate(used)}
    water = [[w[0], remap.get(w[1], -1)] + w[2:] for w in v["water"]]
    j = {"v": 1, "s": 1e5, "hw": HW, "wk": WK, "names": [names_list[i] for i in used], "roads": v["roads"], "water": water, "rail": v["rail"]}
    j = shrink(j)
    path = os.path.join(OUT, k + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, separators=(",", ":"))
    total += os.path.getsize(path)
    written.append(k)
json.dump(sorted(written), open(os.path.join(OUT, "index.json"), "w"), separators=(",", ":"))
print(len(written), "files,", round(total / 1e6, 1), "MB; ways kept", h.n)
