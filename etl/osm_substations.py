"""
Every OpenStreetMap substation in Poland with a 110 kV (or higher) voltage, named or not, from the Overpass API:

    python etl/osm_substations.py

Writes etl/osm_substations_110.json: {"source", "read", "substations": [[key, name, lat, lon, voltage, operator]]},
where key is the station's own name folded for matching: the part in quotes when there is one
("Stacja elektroenergetyczna 110/15kV „GPZ Zamość Janowice”" -> "zamosc janowice"), else the name without
"stacja elektroenergetyczna", GPZ/GPO/RPZ/SE and voltages.  Run it now and then (substations change slowly);
the result is committed so the daily build never needs Overpass.
"""

import datetime
import json
import os
import re
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stations import fold  # noqa: E402

OUT = os.path.join(HERE, "osm_substations_110.json")
MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
QUERY = '[out:json][timeout:240];area["ISO3166-1"="PL"][admin_level=2]->.pl;(nwr["power"="substation"]["voltage"~"(110|220|400)000"](area.pl););out center tags;'
NOISE = r"\b(stacja|stacji|podstacja|elektroenergetyczna|elektroenergetyczny|transformatorowa|rozdzielnia|gpz|gpo|rpz|rsm|se|sto|gls|kv|wn|sn|abonencka|glowny|punkt|zasilajacy|odbiorczy)\b"


def key_of(name):
    """The station's own name, folded."""
    quoted = re.search(r"[\"„“”«](.+?)[\"”“»]", name or "")
    text = quoted.group(1) if quoted else (name or "")
    text = re.sub(r"\d+\s*(/\s*\d+)*\s*kv\b", " ", text, flags=re.I)     # 110/15kV, 400/220/110 kV
    text = fold(text)
    text = re.sub(NOISE, " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main():
    for mirror in MIRRORS:
        try:
            response = requests.post(mirror, data={"data": QUERY}, headers={"User-Agent": "polish-grid (github.com/aragn/polish-grid)"}, timeout=300)
            response.raise_for_status()
            elements = response.json()["elements"]
            break
        except (requests.RequestException, ValueError) as error:
            print(f"{mirror}: {error}")
    else:
        sys.exit("No Overpass mirror answered")
    out = []
    for e in elements:
        tags = e.get("tags", {})
        lat, lon = (e.get("lat"), e.get("lon")) if "lat" in e else (e.get("center", {}).get("lat"), e.get("center", {}).get("lon"))
        if lat is None:
            continue
        name = tags.get("name", "")
        out.append([key_of(name), name, round(lat, 5), round(lon, 5), tags.get("voltage", ""), tags.get("operator", "")])
    out.sort()
    json.dump({"source": "OpenStreetMap contributors (ODbL), via the Overpass API", "read": datetime.date.today().isoformat(), "substations": out},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out)} substations at 110 kV or more, {sum(1 for s in out if s[0])} with a usable name")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
