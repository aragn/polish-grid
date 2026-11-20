"""
A DC power-flow model of Poland's 220/400 kV grid, built from OpenStreetMap:

    python etl/congestion/network.py --osm <dir with buses.csv, lines.csv, transformers.csv, links.csv> --work D:/Power/pl-congestion

<osm> is PyPSA-Eur's prebuilt OSM transmission network, release 0.7 ("Prebuilt
electricity network for PyPSA-Eur based on OpenStreetMap data" on Zenodo,
ODbL; PyPSA-Eur downloads it to data/osm/archive/0.7).  It is already cleaned into substations
per voltage level, lines between them (merged ways, circuits counted) and
transformers between voltage levels of one substation.

Kept: every Polish bus at 220 kV and above, every line with an end in Poland
and the 400/220 kV transformers.  The far end of every cross-border line is
replaced by one boundary node per neighbour (X_DE, X_CZ, X_SK, X_LT, X_UA),
and SwePol (HVDC, Słupsk) becomes X_SE.  Each neighbour therefore meets
Poland only through its own tie lines: a measured border flow injected at its
node reaches Poland exactly, split between the tie lines by impedance.

Impedances and ratings are not OSM's: they are typical values for PSE's
standard conductors (README, "Line parameters"), so every line of one voltage
has the same reactance per km and the same rating per circuit.

Writes <work>/out/network.json.
"""

import argparse
import json
import math
import re
import os

import networkx as nx
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# Typical PSE conductors (see README): reactance per circuit [ohm/km] and continuous rating per circuit [MVA].
LINE_TYPES = {
    220: {"x_ohm_km": 0.41, "s_mva": 380.0, "conductor": "AFL-8 525 mm2, single, ~1.0 kA"},
    400: {"x_ohm_km": 0.33, "s_mva": 1390.0, "conductor": "2 x AFL-8 525 mm2, ~2.0 kA"},
}
TRAFO_X_PU = 0.12            # on the transformer's rating
TRAFO_S_MVA = (500.0, 1000.0)  # clip OSM-derived ratings into this range
BASE_MVA = 100.0
NEIGHBOURS = {"DE", "CZ", "SK", "LT", "UA", "SE", "BY"}


def read(path):
    return pd.read_csv(path, quotechar="'")


def level(v):
    return 400 if v >= 300 else 220


def names(work_root):
    """OSM id -> (name, code) from the queue page's PSE stations, then OSM's named substations by position."""
    q = json.load(open(os.path.join(ROOT, "data", "queue.json"), encoding="utf-8"))["stations"]
    by_osm = {}
    for name, s in q.items():
        if s.get("osm_id"):
            by_osm[s["osm_id"]] = {"name": name, "code": s.get("code", ""), "kv": s.get("kv", ""), "lat": s["lat"], "lon": s["lon"]}
    places = json.load(open(os.path.join(ROOT, "etl", "osm_places.json"), encoding="utf-8"))["substations"]
    ehv = [p for p in places if any(v in str(p[3]) for v in ("220000", "400000", "380000"))]
    return by_osm, [s for s in q.items() if s[1].get("lat") is not None], ehv


def km(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


def clean_name(s):
    for w in ("Stacja Elektroenergetyczna", "Stacja elektroenergetyczna", "Rozdzielnia", "Stacja", "SE "):
        s = s.replace(w, "")
    s = re.sub(r"\b\d{2,3}(/\d{2,3})*\s*kV\b", "", s)
    return re.sub(r"\s+", " ", s.replace('"', "").replace("„", "").replace("”", "")).strip(" -,")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", required=True)
    ap.add_argument("--work", required=True)
    a = ap.parse_args()

    buses = read(os.path.join(a.osm, "buses.csv"))
    lines = read(os.path.join(a.osm, "lines.csv"))
    trafos = read(os.path.join(a.osm, "transformers.csv"))
    links = read(os.path.join(a.osm, "links.csv"))
    country = buses.set_index("bus_id")["country"]
    pl = buses[(buses.country == "PL") & (buses.dc == "f") & (buses.voltage >= 200)].set_index("bus_id")
    ids = set(pl.index)

    def end(bus):
        if bus in ids:
            return bus
        c = country.get(bus, "")
        return f"X_{c}" if c in NEIGHBOURS else None

    branches = []
    sel = lines[lines.bus0.isin(ids) | lines.bus1.isin(ids)]
    for r in sel.itertuples():
        b0, b1 = end(r.bus0), end(r.bus1)
        if b0 is None or b1 is None or b0 == b1:
            continue
        kv = level(r.voltage)
        t = LINE_TYPES[kv]
        length = r.length / 1000.0
        circuits = int(r.circuits)
        x_ohm = t["x_ohm_km"] * max(length, 0.5) / circuits
        branches.append({"id": r.line_id, "kind": "line", "bus0": b0, "bus1": b1, "kv": kv, "length_km": round(length, 2),
                         "circuits": circuits, "x_pu": x_ohm / (kv ** 2 / BASE_MVA), "s_nom": t["s_mva"] * circuits,
                         "tie": b0.startswith("X_") or b1.startswith("X_"), "osm": r.tags})
    for r in trafos[trafos.bus0.isin(ids) & trafos.bus1.isin(ids)].itertuples():
        if level(r.voltage_bus0) == level(r.voltage_bus1):
            continue
        s = min(max(float(r.s_nom), TRAFO_S_MVA[0]), TRAFO_S_MVA[1])
        branches.append({"id": r.transformer_id, "kind": "trafo", "bus0": r.bus0, "bus1": r.bus1, "kv": 400, "length_km": 0,
                         "circuits": 1, "x_pu": TRAFO_X_PU * BASE_MVA / s, "s_nom": s, "tie": False, "osm": ""})
    # SwePol: the Polish converter end of the HVDC link becomes the boundary node's connection.
    converters = read(os.path.join(a.osm, "converters.csv"))
    ac_of = {}
    for r in converters.itertuples():
        ac_of[r.bus0] = r.bus1
    hvdc = []
    for r in links.itertuples():
        for here, there in ((r.bus0, r.bus1), (r.bus1, r.bus0)):
            if country.get(here) == "PL" and country.get(there) in NEIGHBOURS:
                hvdc.append({"id": r.link_id, "pl_dc_bus": here, "pl_ac_bus": ac_of.get(here),
                             "neighbour": f"X_{country.get(there)}", "p_nom": float(r.p_nom)})

    # Largest connected component (OSM leaves a few islands: plant switchyards with a missing line, etc.).
    g = nx.MultiGraph()
    for b in branches:
        g.add_edge(b["bus0"], b["bus1"])
    main_cc = max(nx.connected_components(g), key=len)
    dropped = sorted(set(g.nodes) - main_cc)
    branches = [b for b in branches if b["bus0"] in main_cc]

    # Series reduction: OSM's virtual buses (tee points, line splits) with exactly two lines of one voltage
    # and nothing else are pass-through points; the two lines become one (reactances add, the rating is the lower).
    keep = {h["pl_ac_bus"] for h in hvdc}
    while True:
        deg = {}
        for i, b in enumerate(branches):
            for e in (b["bus0"], b["bus1"]):
                deg.setdefault(e, []).append(i)
        node = next((n for n, ix in deg.items() if n.startswith("virtual") and n not in keep and len(ix) == 2
                     and all(branches[i]["kind"] == "line" for i in ix)
                     and branches[ix[0]]["kv"] == branches[ix[1]]["kv"]
                     and {branches[ix[0]]["bus0"], branches[ix[0]]["bus1"]} != {branches[ix[1]]["bus0"], branches[ix[1]]["bus1"]}), None)
        if node is None:
            break
        i, j = deg[node]
        p, q = branches[i], branches[j]
        a0 = p["bus1"] if p["bus0"] == node else p["bus0"]
        a1 = q["bus1"] if q["bus0"] == node else q["bus0"]
        merged = dict(p, id=p["id"] + "|" + q["id"], bus0=a0, bus1=a1, length_km=round(p["length_km"] + q["length_km"], 2),
                      circuits=min(p["circuits"], q["circuits"]), x_pu=p["x_pu"] + q["x_pu"], s_nom=min(p["s_nom"], q["s_nom"]),
                      tie=p["tie"] or q["tie"], osm=f"{p['osm']};{q['osm']}")
        branches = [b for k, b in enumerate(branches) if k not in (i, j)] + [merged]
        main_cc.discard(node)

    by_osm, stations, ehv = names(a.work)
    out_buses = []
    for bid in sorted(main_cc):
        if bid.startswith("X_"):
            out_buses.append({"id": bid, "name": f"Boundary {bid[2:]}", "code": bid, "kv": 0, "lat": None, "lon": None,
                              "feeds110": False, "boundary": True})
            continue
        r = pl.loc[bid]
        osm = bid.rsplit("-", 1)[0]
        lat, lon = float(r.y), float(r.x)
        hit = by_osm.get(osm)
        name, code, feeds = None, "", None
        if hit:
            name, code, feeds = clean_name(hit["name"]), hit["code"], "110" in hit["kv"]
        else:
            near = min(stations, key=lambda s: km(lat, lon, s[1]["lat"], s[1]["lon"]))
            if km(lat, lon, near[1]["lat"], near[1]["lon"]) < 1.5:
                name, code, feeds = clean_name(near[0]), near[1].get("code", ""), "110" in near[1].get("kv", "")
        if name is None and ehv:
            p = min(ehv, key=lambda p: km(lat, lon, p[1], p[2]))
            if km(lat, lon, p[1], p[2]) < 1.5:
                name, feeds = clean_name(p[0]), "110000" in str(p[3])
        if name is None:
            name = f"{'Tee' if osm.startswith('virtual') else 'Node'} {lat:.3f},{lon:.3f}"
            feeds = False
        out_buses.append({"id": bid, "name": name, "code": code, "kv": level(int(r.voltage)), "lat": round(lat, 5),
                          "lon": round(lon, 5), "feeds110": bool(feeds), "boundary": False})
    # Tee points take the name of the nearest named substation ("Byczyna tee").
    named = [b for b in out_buses if not b["boundary"] and not b["name"].startswith(("Tee ", "Node "))]
    for b in out_buses:
        if b["name"].startswith(("Tee ", "Node ")) and named:
            n = min(named, key=lambda c: km(b["lat"], b["lon"], c["lat"], c["lon"]))
            b["name"] = f"{n['name']} tee" if km(b["lat"], b["lon"], n["lat"], n["lon"]) < 25 else b["name"]
    bname = {b["id"]: b["name"] for b in out_buses}
    for b in branches:
        n0, n1 = bname[b["bus0"]], bname[b["bus1"]]
        b["name"] = f"{n0} – {n1} {b['kv']} kV" if b["kind"] == "line" else f"{n0} {'400/220'} kV transformer"

    os.makedirs(os.path.join(a.work, "out"), exist_ok=True)
    out = {"source": "OpenStreetMap contributors (ODbL), via PyPSA-Eur's prebuilt OSM network 0.7",
           "base_mva": BASE_MVA, "line_types": LINE_TYPES, "trafo_x_pu": TRAFO_X_PU,
           "buses": out_buses, "branches": branches, "hvdc": hvdc, "dropped_islands": dropped}
    json.dump(out, open(os.path.join(a.work, "out", "network.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    nl = sum(b["kind"] == "line" for b in branches)
    print(f"{len(out_buses)} buses, {nl} lines ({sum(b['tie'] for b in branches)} ties), "
          f"{len(branches) - nl} transformers, {len(hvdc)} HVDC; dropped {len(dropped)} island buses")
    print("unnamed:", sum(b["name"].startswith(("Tee", "Node")) for b in out_buses),
          "feeding 110 kV:", sum(b["feeds110"] for b in out_buses))


if __name__ == "__main__":
    main()
