"""
Poland's grid from 110 kV up, from a Geofabrik extract, written in the raw
Overpass format PyPSA-Eur's clean_osm_data / build_osm_network read:

    python etl/congestion/osm_pbf.py poland-260101-power.osm.pbf <pypsa-eur>/data/osm/build/unknown/PL

The extract is Geofabrik's dated snapshot of 1 January 2026
(poland-260101.osm.pbf), filtered to power features with osmium-tool in
GitHub Actions (.github/workflows/congestion-sources.yml): the grid as it
stood at the end of the year modelled.  osm_overpass.py does the same from
Overpass's history, when the public servers are not overloaded.

Writes lines_way, cables_way, routes_relation, substations_way,
substations_relation (PyPSA-Eur's five files) and transformers (units with
their rating tag, for network.py).  Needs pyosmium.
"""

import json
import os
import re
import sys

import osmium

HV = re.compile(r"(^|;)(110000|220000|380000|400000|450000|750000)(;|$)")


def hv(tags):
    return bool(HV.search(tags.get("voltage", "")))


class Relations(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.subs, self.routes, self.member_ways = [], [], set()

    def relation(self, r):
        t = dict(r.tags)
        members = [{"type": {"w": "way", "n": "node", "r": "relation"}[m.type], "ref": m.ref, "role": m.role} for m in r.members]
        if t.get("power") == "substation" and hv(t):
            self.subs.append({"type": "relation", "id": r.id, "members": members, "tags": t})
        elif t.get("route") == "power" or t.get("power") == "circuit":
            self.routes.append({"type": "relation", "id": r.id, "members": members, "tags": t})
        else:
            return
        self.member_ways.update(m["ref"] for m in members if m["type"] == "way")


class Ways(osmium.SimpleHandler):
    def __init__(self, member_ways):
        super().__init__()
        self.member_ways = member_ways
        self.out = {"lines_way": [], "cables_way": [], "substations_way": [], "transformers": []}
        self.geom = {}

    def node(self, n):
        t = dict(n.tags)
        if t.get("power") == "transformer" and hv(t):
            self.out["transformers"].append({"type": "node", "id": n.id, "lat": n.location.lat, "lon": n.location.lon, "tags": t})

    def way(self, w):
        t = dict(w.tags)
        power = t.get("power") or t.get("construction:power") or (t.get("construction") if t.get("power") == "construction" else None)
        wanted = (power in ("line", "cable", "substation", "transformer") and hv(t)) or w.id in self.member_ways
        if not wanted:
            return
        try:
            geom = [{"lat": nd.location.lat, "lon": nd.location.lon} for nd in w.nodes]
        except osmium.InvalidLocationError:
            return
        if not geom:
            return
        el = {"type": "way", "id": w.id,
              "bounds": {"minlat": min(g["lat"] for g in geom), "minlon": min(g["lon"] for g in geom),
                         "maxlat": max(g["lat"] for g in geom), "maxlon": max(g["lon"] for g in geom)},
              "nodes": [nd.ref for nd in w.nodes], "geometry": geom, "tags": t}
        if w.id in self.member_ways:
            self.geom[w.id] = geom
        if power in ("line", "cable") and hv(t):
            self.out["lines_way" if power == "line" else "cables_way"].append(el)
        elif power == "substation" and hv(t):
            self.out["substations_way"].append(el)
        elif power == "transformer" and hv(t):
            self.out["transformers"].append(el)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rel = Relations()
    rel.apply_file(src)
    ways = Ways(rel.member_ways)
    ways.apply_file(src, locations=True, idx="flex_mem")
    for r in rel.subs + rel.routes:
        for m in r["members"]:
            if m["type"] == "way" and m["ref"] in ways.geom:
                m["geometry"] = ways.geom[m["ref"]]
        # PyPSA-Eur builds a line from every member: keep the ways whose geometry is in the extract
        r["members"] = [m for m in r["members"] if "geometry" in m]
        pts = [g for m in r["members"] for g in m.get("geometry", [])]
        if pts:
            r["bounds"] = {"minlat": min(g["lat"] for g in pts), "minlon": min(g["lon"] for g in pts),
                           "maxlat": max(g["lat"] for g in pts), "maxlon": max(g["lon"] for g in pts)}
    files = dict(ways.out, substations_relation=[r for r in rel.subs if "bounds" in r],
                 routes_relation=[r for r in rel.routes if "bounds" in r])
    os.makedirs(dst, exist_ok=True)
    for name, els in files.items():
        with open(os.path.join(dst, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 0.6, "generator": "etl/congestion/osm_pbf.py", "elements": els}, f)
        print(name, len(els))


if __name__ == "__main__":
    main()
