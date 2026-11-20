"""
A DC power-flow model of Poland's grid from 110 kV up, with its neighbours'
220/400 kV grids, built from OpenStreetMap:

    python etl/congestion/network.py --work W \
        --pl <PyPSA-Eur run>/resources/plgrid/osm/build      # Poland, raw OSM at 1 July 2025, 110 kV and up
        --raw W/cache/osm_raw/PL                              # the raw Overpass files (names, conductor tags, transformers)
        --osm <PyPSA-Eur>/data/osm/archive/0.7                # neighbours and tie lines (prebuilt OSM network 0.7)
        --prsp W/cache/prsp_lines.json                        # lines PSE's development plan upgrades (prsp.py)

Poland comes from PyPSA-Eur's OSM pipeline run on raw OSM data (osm_overpass.py
fetches it as of a date; config/plgrid.yaml in the PyPSA-Eur workspace runs
clean_osm_data and build_osm_network down to 110 kV).  The neighbours DE,
CZ, SK and LT are their 220 kV and higher grids from PyPSA-Eur's prebuilt
network 0.7, cut at their other borders, so loop flows through Germany, the
Czech Republic and Slovakia, and outages abroad, follow the physics.  Ukraine
stays one boundary node; SwePol is an injection at its converter's bus.

Every Polish line of n circuits becomes n parallel branches.  Line parameters
come from one conductor class per line: voltage and OSM's `wires` (bundle)
tag, with lines that PSE's development plan schedules for an upgrade treated
as thermally limited (60 instead of 80 degrees C design temperature).  The
rating given here is at 30 degrees C air; flows.py scales it every hour with
the line's air temperature.  Transformers take OSM's unit ratings where
tagged.  The neighbours' lines keep PyPSA-Eur's types.

Writes W/out/network.json.
"""

import argparse
import collections
import json
import math
import os
import re
import unicodedata

import networkx as nx
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BASE_MVA = 100.0
EXTERNAL = ["DE", "CZ", "SK", "LT"]
BOUNDARY = {"UA", "BY", "SE"}
T_REF, T_DESIGN, T_LIMITED = 30.0, 80.0, 60.0

# Conductor classes, per circuit: reactance (ohm/km) and rating at 30 C air, 80 C conductor (MVA).
CLASSES = {
    (110, 1): {"x": 0.40, "s": 115.0, "label": "AFL-6 240"},
    (110, 2): {"x": 0.30, "s": 230.0, "label": "2 x AFL-6 240"},
    (220, 1): {"x": 0.41, "s": 380.0, "label": "AFL-8 525"},
    (220, 2): {"x": 0.31, "s": 760.0, "label": "2 x AFL-8 525"},
    (400, 2): {"x": 0.33, "s": 1390.0, "label": "2 x AFL-8 525"},
    (400, 3): {"x": 0.29, "s": 1660.0, "label": "3 x AFL-8 350"},
    (400, 4): {"x": 0.27, "s": 2000.0, "label": "4 x AFL-8 350"},
}
DEFAULT_BUNDLE = {110: 1, 220: 1, 400: 2}
WIRES = {"single": 1, "double": 2, "triple": 3, "quad": 4, "quadruple": 4}
TRAFO_UNIT = {(400, 220): 500.0, (400, 110): 330.0, (220, 110): 160.0}  # MVA per unit when OSM has no rating
TRAFO_UNITS_DEFAULT = 2
TRAFO_X_PU = 0.12


def read(path):
    return pd.read_csv(path, quotechar="'")


def level(v):
    v = float(v)
    return 400 if v >= 300 else 220 if v >= 200 else 110


def fold(s):
    s = unicodedata.normalize("NFKD", str(s or "").replace("ł", "l").replace("Ł", "L")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def km(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371 * math.hypot(x, math.radians(lat2 - lat1))


def clean_name(s):
    s = str(s or "")
    s = re.sub(r"(?i)\b(stacja|rozdzielnia|elektroenergetyczna|transformatorowa|GPZ|RPZ|SE|PSE|GSZ|RS)\b\.?", " ", s)
    s = re.sub(r"\b\d{2,3}(/\d{2,3})*\s*kV\b", "", s)
    s = s.replace('"', "").replace("„", "").replace("”", "")
    return re.sub(r"\s+", " ", s).strip(" -,")


def geom(wkt, kv):
    """Simplified [[lon, lat], ...] of a 220/400 kV line, for the map; None below 220 kV."""
    if kv < 220 or not isinstance(wkt, str) or "LINESTRING" not in wkt:
        return None
    from shapely import wkt as W
    g = W.loads(wkt).simplify(0.003)
    return [[round(x, 4), round(y, 4)] for x, y in g.coords]


def raw_elements(raw, name):
    path = os.path.join(raw, f"{name}.json")
    return json.load(open(path, encoding="utf-8"))["elements"] if os.path.exists(path) else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--pl", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--osm", required=True)
    ap.add_argument("--prsp", help="JSON list of [station A, station B, kV] that PSE's plan upgrades")
    a = ap.parse_args()

    # ---------------------------------------------------------------- Poland
    pb = read(os.path.join(a.pl, "buses.csv"))
    pl_ = read(os.path.join(a.pl, "lines.csv"))
    pt = read(os.path.join(a.pl, "transformers.csv"))
    pk = read(os.path.join(a.pl, "links.csv")) if os.path.exists(os.path.join(a.pl, "links.csv")) else pd.DataFrame()
    pb = pb[(pb.country == "PL") & (pb.dc == "f") & (pb.voltage >= 100)].set_index("bus_id")
    ids = set(pb.index)

    # raw OSM: names of substations, tags of line ways, transformer units
    sub_name = {}
    for el in raw_elements(a.raw, "substations_way") + raw_elements(a.raw, "substations_relation"):
        if el.get("tags", {}).get("name"):
            sub_name[f"{el['type']}/{el['id']}"] = el["tags"]["name"]
    way_tags = {f"way/{el['id']}": el.get("tags", {}) for el in raw_elements(a.raw, "lines_way") + raw_elements(a.raw, "cables_way")}
    units = []
    for el in raw_elements(a.raw, "transformers"):
        t = el.get("tags", {})
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        else:
            g = el.get("geometry") or []
            if not g:
                continue
            lat, lon = np.mean([p["lat"] for p in g]), np.mean([p["lon"] for p in g])
        mva = None
        m = re.search(r"([\d.,]+)\s*(MVA|kVA)?", t.get("rating", ""))
        if m:
            mva = float(m.group(1).replace(",", ".")) / (1000 if m.group(2) == "kVA" else 1)
        volts = sorted({level(int(v) / 1000) for v in re.findall(r"\d{5,6}", t.get("voltage", ""))}, reverse=True)
        if len(volts) >= 2:
            units.append({"lat": lat, "lon": lon, "hv": volts[0], "lv": volts[-1], "mva": mva})

    prsp = set()
    if a.prsp and os.path.exists(a.prsp):
        for s0, s1, kv in json.load(open(a.prsp, encoding="utf-8")):
            prsp.add((frozenset((fold(s0), fold(s1))), int(kv)))

    def name_of(bid):
        osm = bid.rsplit("-", 1)[0]
        return clean_name(sub_name.get(osm, "")) or None

    buses = {}
    for bid, r in pb.iterrows():
        buses[bid] = {"id": bid, "name": name_of(bid), "kv": level(r.voltage), "lat": round(float(r.y), 5),
                      "lon": round(float(r.x), 5), "country": "PL", "boundary": False,
                      "virtual": bid.startswith("virtual")}

    branches = []

    def line_class(kv, tags_list):
        bundles = [WIRES.get(str(way_tags.get(t, {}).get("wires", "")).lower()) for t in tags_list]
        bundles = [b for b in bundles if b]
        bundle = collections.Counter(bundles).most_common(1)[0][0] if bundles else DEFAULT_BUNDLE[kv]
        key = (kv, bundle) if (kv, bundle) in CLASSES else (kv, DEFAULT_BUNDLE[kv])
        return key, bool(bundles)

    for r in pl_.itertuples():
        if r.bus0 not in ids or r.bus1 not in ids or r.bus0 == r.bus1:
            continue
        kv = level(r.voltage)
        tags = [t for t in re.split(r"[;,]", str(r.tags)) if t.startswith("way/")]
        key, tagged = line_class(kv, tags)
        c = CLASSES[key]
        n = max(1, int(r.circuits))
        length = float(r.length) / 1000.0
        branches.append({"id": r.line_id, "kind": "line", "bus0": r.bus0, "bus1": r.bus1, "kv": kv, "country": "PL",
                         "length_km": round(length, 2), "circuits": n,
                         "x_pu": c["x"] * max(length, 0.3) / n / (kv ** 2 / BASE_MVA), "s_nom": c["s"] * n,
                         "conductor": c["label"], "bundle_tagged": tagged, "t_design": T_DESIGN,
                         "underground": str(r.underground) == "t", "tie": False, "osm": ";".join(tags),
                         "geom": geom(r.geometry, kv)})
    # transformers: units from OSM's power=transformer within 400 m of the substation
    for r in pt.itertuples():
        if r.bus0 not in ids or r.bus1 not in ids:
            continue
        hv, lv = sorted((level(r.voltage_bus0), level(r.voltage_bus1)), reverse=True)
        if hv == lv:
            continue
        b0 = buses[r.bus0]
        near = [u for u in units if u["hv"] == hv and u["lv"] == lv and km(b0["lat"], b0["lon"], u["lat"], u["lon"]) < 0.4]
        default = TRAFO_UNIT.get((hv, lv), 300.0)
        if near:
            mvas = [u["mva"] or default for u in near]
            how = f"{len(near)} OSM unit(s)" + ("" if all(u["mva"] for u in near) else ", untagged ones at default")
        else:
            mvas = [default] * TRAFO_UNITS_DEFAULT
            how = "default units"
        s = float(sum(mvas))
        branches.append({"id": r.transformer_id, "kind": "trafo", "bus0": r.bus0, "bus1": r.bus1, "kv": hv, "lv": lv,
                         "country": "PL", "length_km": 0, "circuits": 1, "x_pu": TRAFO_X_PU * BASE_MVA / s, "s_nom": s,
                         "units": len(mvas), "rating_source": how, "tie": False, "osm": ""})

    # ---------------------------------------------------------------- neighbours (0.7) and ties
    ob = read(os.path.join(a.osm, "buses.csv"))
    ol = read(os.path.join(a.osm, "lines.csv"))
    ot = read(os.path.join(a.osm, "transformers.csv"))
    ok = read(os.path.join(a.osm, "links.csv"))
    oc = read(os.path.join(a.osm, "converters.csv"))
    ctry = ob.set_index("bus_id")["country"]
    ext = ob[ob.country.isin(EXTERNAL) & (ob.dc == "f")].set_index("bus_id")
    ext_ids = set(ext.index)
    for bid, r in ext.iterrows():
        buses[bid] = {"id": bid, "name": None, "kv": level(r.voltage), "lat": round(float(r.y), 5), "lon": round(float(r.x), 5),
                      "country": r.country, "boundary": False, "virtual": bid.startswith("virtual")}
    pl_by_level = collections.defaultdict(list)
    for b in buses.values():
        if b["country"] == "PL":
            pl_by_level[b["kv"]].append(b)

    def to_pl(old_bus):
        """A 0.7 Polish bus -> the nearest bus of the same voltage in the 2025 build."""
        r = ob.set_index("bus_id").loc[old_bus]
        kv = level(r.voltage)
        cand = min(pl_by_level[kv], key=lambda b: km(float(r.y), float(r.x), b["lat"], b["lon"]))
        d = km(float(r.y), float(r.x), cand["lat"], cand["lon"])
        return cand["id"] if d < 3.0 else None

    ties_skipped = []
    for r in ol.itertuples():
        c0, c1 = ctry.get(r.bus0), ctry.get(r.bus1)
        if c0 in EXTERNAL and c1 in EXTERNAL:
            b0, b1, tie = r.bus0, r.bus1, False
        elif "PL" in (c0, c1) and ({c0, c1} - {"PL"}) & (set(EXTERNAL) | BOUNDARY):
            other = c1 if c0 == "PL" else c0
            pl_old = r.bus0 if c0 == "PL" else r.bus1
            pl_new = to_pl(pl_old)
            if pl_new is None:
                ties_skipped.append(r.line_id)
                continue
            far = (r.bus1 if c0 == "PL" else r.bus0) if other in EXTERNAL else f"X_{other}"
            b0, b1, tie = pl_new, far, True
        else:
            continue
        if b0 == b1:
            continue
        kv = level(r.voltage)
        n = max(1, int(r.circuits))
        length = float(r.length) / 1000.0
        if tie:
            c = CLASSES[(kv, DEFAULT_BUNDLE[kv])]
            x_pu, s_nom, label = c["x"] * length / n / (kv ** 2 / BASE_MVA), c["s"] * n, c["label"]
        else:
            x_pu, s_nom, label = float(r.x) / (r.voltage ** 2 / BASE_MVA), float(r.s_nom), str(r.type)
        branches.append({"id": r.line_id, "kind": "line", "bus0": b0, "bus1": b1, "kv": kv,
                         "country": "PL" if tie else (c0 if c0 == c1 else f"{c0}-{c1}"), "length_km": round(length, 2),
                         "circuits": n, "x_pu": x_pu, "s_nom": s_nom, "conductor": label, "t_design": T_DESIGN,
                         "geom": geom(r.geometry, kv) if tie else None,
                         "tie": tie, "osm": r.tags})
    for r in ot.itertuples():
        if r.bus0 in ext_ids and r.bus1 in ext_ids and level(r.voltage_bus0) != level(r.voltage_bus1):
            s = min(max(float(r.s_nom), 500.0), 2000.0)
            branches.append({"id": r.transformer_id, "kind": "trafo", "bus0": r.bus0, "bus1": r.bus1, "kv": 400, "lv": 220,
                             "country": ctry.get(r.bus0), "length_km": 0, "circuits": 1, "x_pu": TRAFO_X_PU * BASE_MVA / s,
                             "s_nom": s, "tie": False, "osm": ""})
    for x in {b["bus1"] for b in branches if str(b["bus1"]).startswith("X_")}:
        buses[x] = {"id": x, "name": f"Boundary {x[2:]}", "kv": 0, "lat": None, "lon": None, "country": x[2:],
                    "boundary": True, "virtual": False}
    # SwePol: the AC bus of the Polish converter
    ac_of = dict(zip(oc.bus0, oc.bus1))
    hvdc = []
    for r in ok.itertuples():
        for here, there in ((r.bus0, r.bus1), (r.bus1, r.bus0)):
            if ctry.get(here) == "PL" and ctry.get(there) in BOUNDARY | {"SE"}:
                ac = to_pl(ac_of[here]) if here in ac_of else None
                hvdc.append({"id": r.link_id, "pl_ac_bus": ac, "neighbour": f"X_{ctry.get(there)}", "p_nom": float(r.p_nom)})

    # ---------------------------------------------------------------- the connected grid around Poland
    g = nx.MultiGraph()
    for b in branches:
        g.add_edge(b["bus0"], b["bus1"])
    start = next(b["id"] for b in buses.values() if b["country"] == "PL" and b["kv"] == 400)
    main_cc = nx.node_connected_component(g, start)
    dropped = collections.Counter(buses[n]["country"] for n in set(g.nodes) - main_cc if n in buses)
    branches = [b for b in branches if b["bus0"] in main_cc]
    keep = {h["pl_ac_bus"] for h in hvdc}

    def join_geom(p, q, node):
        """Geometry of two lines merged at `node`, in the order of the merged branch."""
        if not p.get("geom") or not q.get("geom"):
            return p.get("geom") or q.get("geom")
        b = buses[node]
        def near_end(g):  # orient g so that it ends at the node
            d0 = km(b["lat"], b["lon"], g[0][1], g[0][0])
            d1 = km(b["lat"], b["lon"], g[-1][1], g[-1][0])
            return g[::-1] if d0 < d1 else g
        gp = near_end(p["geom"])
        gq = near_end(q["geom"])[::-1]
        return gp + gq[1:]

    # series reduction of pass-through virtual buses
    while True:
        deg = collections.defaultdict(list)
        for i, b in enumerate(branches):
            deg[b["bus0"]].append(i)
            deg[b["bus1"]].append(i)
        node = next((n for n, ix in deg.items() if n.startswith("virtual") and n not in keep and len(ix) == 2
                     and all(branches[i]["kind"] == "line" for i in ix)
                     and branches[ix[0]]["kv"] == branches[ix[1]]["kv"]
                     and branches[ix[0]]["circuits"] == branches[ix[1]]["circuits"]
                     and {branches[ix[0]]["bus0"], branches[ix[0]]["bus1"]} != {branches[ix[1]]["bus0"], branches[ix[1]]["bus1"]}), None)
        if node is None:
            break
        i, j = deg[node]
        p, q = branches[i], branches[j]
        a0 = p["bus1"] if p["bus0"] == node else p["bus0"]
        a1 = q["bus1"] if q["bus0"] == node else q["bus0"]
        merged = dict(p, id=p["id"] + "|" + q["id"], bus0=a0, bus1=a1, length_km=round(p["length_km"] + q["length_km"], 2),
                      x_pu=p["x_pu"] + q["x_pu"], s_nom=min(p["s_nom"], q["s_nom"]), tie=p["tie"] or q["tie"],
                      osm=f"{p['osm']};{q['osm']}", geom=join_geom(p, q, node), bundle_tagged=p.get("bundle_tagged", False) or q.get("bundle_tagged", False))
        branches = [b for k, b in enumerate(branches) if k not in (i, j)] + [merged]
        main_cc.discard(node)

    out_buses = [buses[n] for n in sorted(main_cc) if n in buses]
    # names: own substation name, else "<nearest named> tee" for virtual points
    named = [b for b in out_buses if b["name"] and b["country"] == "PL"]
    for b in out_buses:
        if b["name"] or b["boundary"]:
            continue
        if b["country"] != "PL":
            b["name"] = f"{b['country']} {b['kv']} kV {b['lat']:.2f},{b['lon']:.2f}"
            continue
        n = min(named, key=lambda c: km(b["lat"], b["lon"], c["lat"], c["lon"]))
        d = km(b["lat"], b["lon"], n["lat"], n["lon"])
        b["name"] = f"{n['name']} tee" if d < 15 else f"Node {b['lat']:.3f},{b['lon']:.3f}"
    for b in out_buses:
        b["load"] = b["country"] == "PL" and b["kv"] == 110 and not b["virtual"]
    bname = {b["id"]: b["name"] for b in out_buses}
    # thermally limited lines: PSE's plan upgrades them (matched by end names and voltage)
    limited = 0
    for b in branches:
        if b["kind"] == "line" and b["country"] == "PL" and b["kv"] >= 220:
            ends = frozenset((fold(re.sub(r" tee$", "", bname[b["bus0"]])).split(" ")[0],
                              fold(re.sub(r" tee$", "", bname[b["bus1"]])).split(" ")[0]))
            if any(len(ends) == 2 and kv == b["kv"] and all(any(e.startswith(p.split(" ")[0]) for p in key) for e in ends)
                   for key, kv in prsp):
                b["t_design"] = T_LIMITED
                b["s_nom"] *= math.sqrt((T_LIMITED - T_REF) / (T_DESIGN - T_REF))
                b["prsp_upgrade"] = True
                limited += 1
        b["name"] = (f"{bname[b['bus0']]} – {bname[b['bus1']]} {b['kv']} kV" if b["kind"] == "line"
                     else f"{bname[b['bus0']]} {b['kv']}/{b.get('lv', '')} kV transformer")
        b["monitored"] = b["country"] == "PL" and b["kv"] >= 220 and (b["kind"] == "line" or b.get("lv") == 220)

    out = {"source": "OpenStreetMap contributors (ODbL): raw OSM at 2025-07-01 for Poland via PyPSA-Eur's OSM pipeline; "
                     "PyPSA-Eur's prebuilt OSM network 0.7 for DE, CZ, SK, LT",
           "base_mva": BASE_MVA, "classes": {f"{k[0]}|{k[1]}": v for k, v in CLASSES.items()},
           "t_ref": T_REF, "t_design": T_DESIGN, "t_limited": T_LIMITED,
           "buses": out_buses, "branches": branches, "hvdc": hvdc, "ties_skipped": ties_skipped,
           "dropped_islands": dict(dropped)}
    os.makedirs(os.path.join(a.work, "out"), exist_ok=True)
    json.dump(out, open(os.path.join(a.work, "out", "network.json"), "w", encoding="utf-8"), ensure_ascii=False)
    pl_lines = [b for b in branches if b["kind"] == "line" and b["country"] == "PL"]
    print(f"{len(out_buses)} buses ({sum(b['country'] == 'PL' for b in out_buses)} in Poland), {len(branches)} branches; "
          f"PL lines by kV {collections.Counter(b['kv'] for b in pl_lines)}, ties {sum(b['tie'] for b in branches)}, "
          f"bundle tagged {sum(b.get('bundle_tagged', False) for b in pl_lines)}, plan-limited {limited}, "
          f"transformers PL {sum(b['kind'] == 'trafo' and b['country'] == 'PL' for b in branches)}; "
          f"ties skipped {len(ties_skipped)}; dropped islands {dict(dropped)}")


if __name__ == "__main__":
    main()
