"""
PSE's list of switchyards with no free connection bays ("Wykaz rozdzielni na stacjach bez możliwości
przyłączenia nowych obiektów"), every version linked on PSE's queue page:

    python etl/saturation.py

Each file has three sections:
  full         existing PSE switchyards with no available connection bays: applications there are
               refused (no technical and economic conditions) without being examined;
  not_owned    planned switchyards PSE will not own or whose construction is not decided;
  conditional  planned PSE switchyards included only conditionally (PRSP 2025-2034 position given).
For the last two, applications stay unexamined.  Every row says 0 available bays: the list is a yes/no,
not an amount of spare capacity.

Stations are matched to etl/stations.json by PSE's three-letter code, else to an OpenStreetMap
substation of that name.  Writes data/saturation.json; keeps the files in archive/saturation/.
"""

import datetime
import json
import os
import re
import sys

import openpyxl
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stations import fold  # noqa: E402

ROOT = os.path.dirname(HERE)
PAGE = "https://www.pse.pl/obszary-dzialalnosci/krajowy-system-elektroenergetyczny/wykaz-obiektow-planowanych-do-przylaczenia"
FILES = "https://www.pse.pl/documents/20182/51490/"
ARCHIVE = os.path.join(ROOT, "archive", "saturation")
OUT = os.path.join(ROOT, "data", "saturation.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
SECTIONS = [("full", "dla których brak jest dostępnych miejsc"), ("not_owned", "nie będą właścicielem"), ("conditional", "uwzględniona warunkowo")]


def versions():
    page = requests.get(PAGE, headers=UA, timeout=60).text
    dates = sorted(set(re.findall(r"(\d{8})_Lista_rozdzielni-brak_mozliwosci_przylaczenia\.xlsx", page)))
    if not dates:
        sys.exit("PSE's page links no 'Lista_rozdzielni-brak_mozliwosci_przylaczenia.xlsx': check the page and etl/saturation.py")
    return dates


def parse(path):
    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    section, rows = None, []
    for raw in sheet.iter_rows(values_only=True):
        cells = [c for c in raw if c not in (None, "")]
        text = " ".join(str(c) for c in cells)
        for key, words in SECTIONS:
            if words in text:
                section = key
        # A switchyard row names its voltage ("Rozdzielnia 220 kV"); the column header says just "Rozdzielnia".
        if section and len(cells) == 5 and re.match(r"Rozdzielnia \d", str(cells[3])):
            zko, code, third, yard, bays = cells
            code = str(code).strip()
            rows.append({"section": section, "zko": str(zko).strip(), "code": code.split()[0], "label": code,
                         "name": str(third).strip() if section == "full" else "", "prsp": str(third).strip() if section != "full" else "",
                         "kv": re.sub(r"\D", "", str(yard)), "bays": int(float(bays)) if str(bays).replace(".", "").isdigit() else None})
    return rows


def main():
    os.makedirs(ARCHIVE, exist_ok=True)
    stations = json.load(open(os.path.join(HERE, "stations.json"), encoding="utf-8"))["stations"]
    by_code = {}
    for name, station in stations.items():
        if station.get("code"):
            by_code.setdefault(station["code"], name)
    osm = {}
    path = os.path.join(HERE, "osm_places.json")
    if os.path.exists(path):
        for name, lat, lon, voltage, operator in json.load(open(path, encoding="utf-8"))["substations"]:
            if "220000" in voltage or "400000" in voltage:
                osm.setdefault(fold(re.sub(r"(?i)\b(stacja|elektroenergetyczna|gpz|rpz|se)\b|\d+/\d+\s*kv", " ", name)).strip(), (lat, lon))
    listed = {}
    found = []
    for date in versions():
        name = f"{date}_Lista_rozdzielni-brak_mozliwosci_przylaczenia.xlsx"
        target = os.path.join(ARCHIVE, name)
        if not os.path.exists(target):
            response = requests.get(FILES + name, headers=UA, timeout=60)
            response.raise_for_status()
            open(target, "wb").write(response.content)
        asof = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        found.append({"asof": asof, "url": FILES + name})
        for row in parse(target):
            key = (row["section"], row["code"])
            entry = listed.setdefault(key, {**{k: row[k] for k in ("section", "zko", "code", "label", "name", "prsp")}, "switchyards": {}})
            entry["name"] = entry["name"] or row["name"]
            entry["switchyards"].setdefault(row["kv"], []).append(asof)
    latest = found[-1]["asof"]
    out = []
    for (section, code), entry in sorted(listed.items(), key=lambda item: (item[0][0] != "full", item[0][1])):
        station = by_code.get(code)
        info = stations.get(station, {}) if station else {}
        lat, lon = info.get("lat"), info.get("lon")
        if lat is None and entry["name"]:
            lat, lon = osm.get(fold(entry["name"]), (None, None))
        yards = [{"kv": kv, "first": dates[0], "last": dates[-1], "now": dates[-1] == latest} for kv, dates in sorted(entry["switchyards"].items(), key=lambda kv: -int(kv[0] or 0))]
        station_kvs = sorted({part for part in re.split(r"\D+", info.get("kv", "")) if part}, key=int, reverse=True)
        full_now = [y["kv"] for y in yards if y["now"]]
        out.append({**{k: entry[k] for k in ("section", "zko", "code", "label", "prsp")}, "name": entry["name"] or station or code,
                    "station": station, "lat": lat, "lon": lon, "station_kv": station_kvs, "switchyards": yards,
                    "now": bool(full_now), "all_full": bool(full_now) and bool(station_kvs) and set(station_kvs) <= set(full_now)})
    result = {"updated": datetime.date.today().isoformat(), "source": PAGE, "versions": found, "latest": latest, "stations": out}
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    before = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
    changed = re.sub(r'"updated":"[^"]*"', "", text) != re.sub(r'"updated":"[^"]*"', "", before)
    if changed:
        open(OUT, "w", encoding="utf-8").write(text)
    placed = sum(1 for s in out if s["lat"] is not None)
    print(f"{len(found)} versions ({found[0]['asof']} to {latest}): {len(out)} stations ({sum(1 for s in out if s['section'] == 'full')} existing), "
          f"{placed} placed{'' if changed else ' (unchanged)'}")
    return changed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
