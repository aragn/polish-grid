"""
Poland's grid from 110 kV up, from OpenStreetMap as it stood on a date, in the
raw format PyPSA-Eur's clean_osm_data / build_osm_network read:

    python etl/congestion/osm_overpass.py --date 2025-07-01 --out <pypsa-eur>/data/osm/build/unknown/PL

Same features as PyPSA-Eur's retrieve_osm_data.py (lines, cables, circuit
relations, substations), but
  - from Overpass's history ("attic") at --date, so the grid matches the year
    modelled (areas are not available in attic queries, hence a bounding box;
    PyPSA-Eur clips to the country shape);
  - lines, cables and substations only at 110 kV and above (the query would
    otherwise bring every medium-voltage line).
"""

import argparse
import json
import os
import time

import requests

URL = "https://overpass-api.de/api/interpreter"
BBOX = "48.95,14.05,54.95,24.20"  # south, west, north, east
HV = '["voltage"~"(^|;)(110000|220000|380000|400000|450000|750000)(;|$)"]'
FEATURES = {
    "cables_way": [f'way["power"="cable"]{HV}', f'way["construction:power"="cable"]{HV}'],
    "lines_way": [f'way["power"="line"]{HV}', f'way["construction:power"="line"]{HV}'],
    "routes_relation": ['relation["route"="power"]', 'relation["power"="circuit"]'],
    "substations_way": [f'way["power"="substation"]{HV}', f'way["construction:power"="substation"]{HV}'],
    "substations_relation": [f'relation["power"="substation"]{HV}'],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2025-07-01")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for name, parts in FEATURES.items():
        path = os.path.join(a.out, f"{name}.json")
        if os.path.exists(path):
            continue
        q = (f'[out:json][timeout:900][date:"{a.date}T00:00:00Z"][bbox:{BBOX}];\n('
             + " ".join(f"{p};" for p in parts) + ");\nout body geom;")
        for attempt in range(5):
            try:
                r = requests.post(URL, data={"data": q}, timeout=1000,
                                  headers={"User-Agent": "polish-grid congestion screening (research)"})
                if r.status_code == 200:
                    data = r.json()
                    break
                print(name, "HTTP", r.status_code, r.text[:200], flush=True)
            except (requests.RequestException, ValueError) as e:
                print(name, "error", e, flush=True)
            time.sleep(60 * (attempt + 1))
        else:
            raise SystemExit(f"Overpass failed for {name}")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
        print(name, len(data.get("elements", [])), flush=True)
        time.sleep(10)


if __name__ == "__main__":
    main()
