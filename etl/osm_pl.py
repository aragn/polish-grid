"""
Named substations and settlements of Poland from an OpenStreetMap extract,
for placing the distribution operators' projects:

    python etl/osm_pl.py poland-latest.osm.pbf

writes etl/osm_places.json: {"substations": [[name, lat, lon, voltage, operator]],
"places": [[name, lat, lon, kind]]}.  The extract (Geofabrik's
poland-latest.osm.pbf, about 2 GB) is not kept in the repository; the small
JSON is, so the build never needs it.  Needs pyosmium.
"""

import json
import os
import sys

import osmium

HERE = os.path.dirname(os.path.abspath(__file__))
KINDS = {"city", "town", "village", "suburb", "hamlet", "quarter"}


class Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.substations, self.places = [], []

    def node(self, n):
        tags = n.tags
        name = tags.get("name")
        if not name:
            return
        if tags.get("power") == "substation":
            self.substations.append([name, round(n.location.lat, 5), round(n.location.lon, 5), tags.get("voltage", ""), tags.get("operator", "")])
        elif tags.get("place") in KINDS:
            self.places.append([name, round(n.location.lat, 5), round(n.location.lon, 5), tags.get("place")])

    def way(self, w):
        tags = w.tags
        if tags.get("power") != "substation" or not tags.get("name"):
            return
        points = [(node.location.lat, node.location.lon) for node in w.nodes if node.location.valid()]
        if not points:
            return
        lat = sum(p[0] for p in points) / len(points)
        lon = sum(p[1] for p in points) / len(points)
        self.substations.append([tags.get("name"), round(lat, 5), round(lon, 5), tags.get("voltage", ""), tags.get("operator", "")])


def main():
    handler = Handler()
    handler.apply_file(sys.argv[1], locations=True, idx="flex_mem")
    out = {"source": "OpenStreetMap contributors (ODbL), Geofabrik poland-latest.osm.pbf",
           "substations": sorted(handler.substations), "places": sorted(handler.places)}
    json.dump(out, open(os.path.join(HERE, "osm_places.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"{len(handler.substations)} named substations, {len(handler.places)} places")


if __name__ == "__main__":
    main()
