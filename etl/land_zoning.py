"""
Zoning on each land plot of the Land tab, from the Ministry of Development's urban register (Rejestr Urbanistyczny):

    python etl/land_zoning.py                       # plots not yet checked, within the budget
    python etl/land_zoning.py --all                 # every plot again
    python etl/land_zoning.py --budget-minutes 60

Two WFS services of rejestr-urbanistyczny.gov.pl (since October 2026 the only national source; GUGiK's own
plan integration ended in September 2026):
  - app-mpzp: the outline, title and date of every local plan (MPZP) a municipality has published to the register.
    The register doesn't carry what each plan allows, only its outline; the plan's drawing and text are linked.
  - app-pog: the zones (strefy planistyczne) of the general plans (plany ogólne gmin): the zone type and the land
    uses ("profiles") a local plan may later allow there, among them "teren elektrowni słonecznej" (solar farm).

For every plot it adds: `mp`, the hectares inside a local plan, and `mpz`, the ids of those plans; `pg`, the general-
plan zones it falls in as [[zone id, ha], ...] (largest first).  data/land/zoning.json names the plans and the zones.
A plot outside every plan in the register may still have a plan the municipality hasn't published there yet.

Queried per substation, over the box around its plots; plans and zones repeat between neighbouring substations, so
each is kept once.
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests
import shapely
from pyproj import Transformer

from land_protected import plot_shape, station_files
from land_budget import capped

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
WFS = "https://rejestr-urbanistyczny.gov.pl/uslugi-sieciowe/{service}/wfs"
UA = {"User-Agent": "polish-grid land screening (github.com/aragn/polish-grid)"}
PAGE = 10                       # the most the register returns a page
NS = {"wfs": "http://www.opengis.net/wfs/2.0", "gml": "http://www.opengis.net/gml/3.2", "xlink": "http://www.w3.org/1999/xlink"}
TO_4326 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)
TO_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
XLINK_TITLE = "{http://www.w3.org/1999/xlink}title"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"



def polygons(element):
    """Shapely geometry (EPSG:2180, x = easting) from the gml:Polygon elements under element; the register writes
    northing first."""
    out = []
    for poly in element.iter("{http://www.opengis.net/gml/3.2}Polygon"):
        rings = []
        for tag in ("exterior", "interior"):
            for ring in poly.iter("{http://www.opengis.net/gml/3.2}" + tag):
                pos = ring.find(".//gml:posList", NS)
                if pos is None or not pos.text:
                    continue
                v = [float(x) for x in pos.text.split()]
                pts = [(v[i + 1], v[i]) for i in range(0, len(v) - 1, 2)]
                if len(pts) >= 4:
                    rings.append(pts)
        if rings:
            out.append(shapely.Polygon(rings[0], rings[1:]))
    if not out:
        return None
    return shapely.make_valid(out[0] if len(out) == 1 else shapely.MultiPolygon(out))


def get_features(service, typename, box):
    """All features of typename within box (lat/lon: south, west, north, east), as ElementTree members."""
    members, start = [], 0
    while True:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": typename, "count": PAGE,
                  "startIndex": start, "bbox": f"{box[0]},{box[1]},{box[2]},{box[3]},urn:ogc:def:crs:EPSG::4326"}
        for attempt in range(5):
            try:
                response = requests.get(WFS.format(service=service), params=params, headers=UA, timeout=180)
                response.raise_for_status()
                root = ET.fromstring(response.content)
                if root.tag.endswith("ExceptionReport"):
                    raise ValueError(response.text[:200])
                break
            except (requests.RequestException, ET.ParseError, ValueError):
                if attempt == 4:
                    raise
                time.sleep(10 * (attempt + 1))
        page = [m[0] for m in root.findall("wfs:member", NS) if len(m)]
        members += page
        matched = int(root.get("numberMatched", "0") or 0) if (root.get("numberMatched") or "").isdigit() else None
        start += len(page)
        # The register returns at most 10 features a page, whatever count asks for: follow numberMatched.
        if not page or (matched is not None and start >= matched) or (matched is None and len(page) < 10):
            return members


def text(el, local):
    found = [c for c in el if c.tag.endswith("}" + local)]
    return found[0].text if found and found[0].text else None


def titles(el, local):
    return [(c.get(XLINK_HREF) or "", c.get(XLINK_TITLE) or "") for c in el if c.tag.endswith("}" + local)]


def kpt(href):
    """'.../KPT#KPT-MPZP-PEF' -> 'PEF'."""
    return href.rsplit("-", 1)[-1] if "KPT-" in href else href.rsplit("/", 1)[-1]


def local_plans(box):
    out = {}
    for f in get_features("app-mpzp", "app-mpzp:AktPlanowaniaPrzestrzennego", box):
        status = (titles(f, "status") or [("", "")])[0][1]
        if status and "wiążący" not in status:
            continue
        el = f.find(".//{*}zasiegPrzestrzenny")
        geom = polygons(el if el is not None else f)
        if geom is None:
            continue
        ident = f.find(".//{*}idIIP//{*}przestrzenNazw")
        lokal = f.find(".//{*}idIIP//{*}lokalnyId")
        pid = f"{ident.text}/{lokal.text}" if ident is not None and lokal is not None else f.get("{http://www.opengis.net/gml/3.2}id")
        out[pid] = {"title": " ".join((text(f, "tytul") or "").split()), "from": text(f, "obowiazujeOd"), "geom": geom}
    return out


def plan_zones(box):
    out = {}
    for f in get_features("app-pog", "app-pog:StrefaPlanistyczna", box):
        status = (titles(f, "status") or [("", "")])[0][1]
        if status and "wiążący" not in status:
            continue
        el = f.find(".//{*}geometria")
        geom = polygons(el if el is not None else f)
        if geom is None:
            continue
        ident = f.find(".//{*}idIIP//{*}przestrzenNazw")
        lokal = f.find(".//{*}idIIP//{*}lokalnyId")
        zid = f"{ident.text}/{lokal.text}" if ident is not None and lokal is not None else f.get("{http://www.opengis.net/gml/3.2}id")
        name = (titles(f, "nazwa") or [("", "")])[0][1]
        basic = titles(f, "profilPodstawowy")
        extra = titles(f, "profilDodatkowy")
        out[zid] = {"symbol": text(f, "symbol"), "label": text(f, "oznaczenie"), "name": name, "from": text(f, "obowiazujeOd"),
                    "basic": [kpt(h) for h, _ in basic], "extra": [kpt(h) for h, _ in extra],
                    "profile_names": {kpt(h): t for h, t in basic + extra},
                    "built": text(f, "maksUdzialPowierzchniZabudowy"), "height": text(f, "maksWysokoscZabudowy"), "geom": geom}
    return out


def box_of(shapes, pad_m=50):
    xmin, ymin, xmax, ymax = shapely.total_bounds(shapes)
    lon, lat = TO_4326.transform([xmin - pad_m, xmax + pad_m], [ymin - pad_m, ymax + pad_m])
    return (round(min(lat), 5), round(min(lon), 5), round(max(lat), 5), round(max(lon), 5))


def overlap(shape, items, tree, keys):
    hits = []
    for i in tree.query(shape, predicate="intersects"):
        ha = shapely.intersection(shape, items[keys[i]]["geom"]).area / 10000
        if ha >= 0.01:
            hits.append([keys[i], round(min(ha, shape.area / 10000), 2)])
    return sorted(hits, key=lambda h: -h[1])


def main(budget_minutes=120, redo_all=False, only=None):
    files = station_files()
    by_station, plots = {}, {}
    for path in files:
        sid = os.path.basename(path)[:-5]
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            if redo_all or "mp" not in plot:
                by_station.setdefault(sid, []).append(plot["id"])
                plots[plot["id"]] = plot
    if only:
        by_station = {s: v for s, v in by_station.items() if s in only}
        plots = {pid: plots[pid] for v in by_station.values() for pid in v}
    print(f"Land zoning: {len(plots)} plots to check around {len(by_station)} substations", flush=True)
    if not plots:
        return False
    shapes = {pid: plot_shape(p) for pid, p in plots.items()}
    shapes = {pid: s for pid, s in shapes.items() if s is not None and not s.is_empty}
    deadline = time.time() + capped(budget_minutes, 30, "Land zoning") * 60
    plans, zones, done_ids, done, failed = {}, {}, set(), 0, 0
    t0 = time.time()

    def fetch(sid):
        mine = [shapes[pid] for pid in by_station[sid] if pid in shapes]
        if not mine:
            return sid, {}, {}
        box = box_of(mine)
        return sid, local_plans(box), plan_zones(box)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        for sid in sorted(by_station, key=lambda s: -len(by_station[s])):
            futures.append(pool.submit(fetch, sid))
        for future in futures:
            if time.time() > deadline:
                future.cancel()
                continue
            try:
                sid, p, z = future.result()
            except Exception as error:  # noqa: BLE001 -- one substation's failure doesn't stop the others
                failed += 1
                print(f"Land zoning: a substation failed: {str(error)[:160]}", flush=True)
                continue
            plans.update(p)
            zones.update(z)
            done_ids.update(by_station[sid])
            done += 1
            if done % 50 == 0:
                print(f"Land zoning: {done}/{len(by_station)} substations, {len(plans)} local plans, {len(zones)} zones, {time.time() - t0:.0f} s", flush=True)
    print(f"Land zoning: {done} substations read ({failed} failed), {len(plans)} local plans and {len(zones)} general-plan zones", flush=True)

    plan_keys, zone_keys = list(plans), list(zones)
    plan_tree = shapely.STRtree([plans[k]["geom"] for k in plan_keys]) if plans else None
    zone_tree = shapely.STRtree([zones[k]["geom"] for k in zone_keys]) if zones else None
    results = {}
    for pid in done_ids:
        shape = shapes.get(pid)
        if shape is None:
            continue
        mp = overlap(shape, plans, plan_tree, plan_keys) if plan_tree is not None else []
        pg = overlap(shape, zones, zone_tree, zone_keys) if zone_tree is not None else []
        if mp:
            covered = shapely.intersection(shape, shapely.union_all([plans[k]["geom"] for k, _ in mp])).area / 10000
        else:
            covered = 0.0
        results[pid] = {"mp": round(min(covered, shape.area / 10000), 2), "mpz": [k for k, _ in mp][:5], "pg": pg[:4]}

    # Write the plots, then the lookup of the plans and zones they point at.
    used_plans, used_zones, changed = set(), set(), 0
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            if plot["id"] in results:
                plot.update(results[plot["id"]])
                touched = True
            used_plans.update(plot.get("mpz", []))
            used_zones.update(z for z, _ in plot.get("pg", []))
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    lookup_path = os.path.join(LAND, "zoning.json")
    old = json.load(open(lookup_path, encoding="utf-8")) if os.path.exists(lookup_path) else {"plans": {}, "zones": {}, "profiles": {}}
    profiles = old.get("profiles", {})
    for k, p in plans.items():
        old["plans"][k] = [p["title"], p["from"]]
    for k, z in zones.items():
        old["zones"][k] = [z["symbol"], z["label"], z["name"], z["from"], z["basic"], z["extra"], z["built"], z["height"]]
        profiles.update(z["profile_names"])
    lookup = {"source": "Rejestr Urbanistyczny (Ministry of Development and Technology): app-mpzp and app-pog WFS",
              "fetched": datetime.date.today().isoformat(),
              "plans": {k: v for k, v in sorted(old["plans"].items()) if k in used_plans},
              "zones": {k: v for k, v in sorted(old["zones"].items()) if k in used_zones},
              "profiles": dict(sorted(profiles.items()))}
    json.dump(lookup, open(lookup_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    left = len(by_station) - done
    print(f"Land zoning: {len(results)} plots written, {changed} station files updated, {left} substations left for the next run")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget-minutes", type=float, default=120)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--only", nargs="*", help="substation ids (test)")
    args = parser.parse_args()
    main(args.budget_minutes, args.all, args.only)
