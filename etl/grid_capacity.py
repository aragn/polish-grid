"""
Available connection capacity ("wartość łącznej dostępnej mocy przyłączeniowej"), which every operator
must publish under art. 7 ust. 8l of the Energy Law, with a five-year outlook since 30 April 2026
(Dz.U. 2026 poz. 516).  The distribution operators update it at least quarterly, PSE monthly.

    python etl/grid_capacity.py            # TAURON at most weekly, the PDFs every run
    python etl/grid_capacity.py --force    # TAURON now too

Sources:
  TAURON  the JSON behind its map (dostepnemoce.tauron-dystrybucja.pl): every GPZ with its code and
          coordinates, the 110 kV value of its group and its own medium-voltage value, 2026-2031, for
          generation ("wytwórcy") and demand ("odbiorcy").
  Energa  two PDFs (generation, demand): groups of 110 kV stations ("węzły koherentne") with their
          stations, 2026-2031 and 2035.  Generation has two tables: 2.3 (the network factor) and 2.4
          (the approach of the expert assessments under the offshore wind act); 2.3 is the headline.
  PGE     one PDF: groups of 110 kV stations with their stations, 2026-2031, generation and demand.
  PSE     one PDF: areas, groups and 220/400 kV stations for 2026 and 2031, step 1 (the transmission
          grid alone) and step 2 (less the conditions agreed for the 110 kV grid, the published
          value), plus the demand capacity of the new stations PSE plans.
ENEA (Power BI dashboards) and Stoen are left out: both ask for consent before their values are
republished station by station.

The values are each operator's estimate, already net of the connection conditions issued; only
connection conditions are binding.  A value for a group is the total for the whole group.

Placing: TAURON gives coordinates; Energa's and PGE's station names go to an OpenStreetMap substation
(etl/dso_build.Places), PSE's to etl/stations.json.  Queue projects are assigned to a group by their
station: TAURON by code, the others by name.

PSE's planned demand stations get the completion year of the item that builds them in PSE's development
plans (the agreed PRSP 2025-2034 and the 2027-2036 draft), from etl/pse_new_stations.json, kept by hand.

Writes data/grid_capacity.json; keeps the source files in archive/grid_capacity/.
"""

import datetime
import hashlib
import io
import json
import os
import re
import sys
import time

import pdfplumber
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dso import verify_for  # noqa: E402
from dso_build import Places, clean_name  # noqa: E402
from stations import Stations, fold  # noqa: E402

ROOT = os.path.dirname(HERE)
ARCHIVE = os.path.join(ROOT, "archive", "grid_capacity")
OUT = os.path.join(ROOT, "data", "grid_capacity.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
YEARS = list(range(2026, 2032))
# Completion years of PSE's planned demand stations, from its development plans (matched by hand).
PLAN_YEARS = json.load(open(os.path.join(HERE, "pse_new_stations.json"), encoding="utf-8"))
TAURON_EVERY_DAYS = 7          # TAURON's map takes ~120 paced calls (and waits out its rate limit): read it weekly

TAURON_PAGE = "https://www.tauron-dystrybucja.pl/przylaczenie-do-sieci/dostepne-moce/dla-wytworcow1"
TAURON_API = "https://dostepnemoce.tauron-dystrybucja.pl/wp-json/tauron-connector/v1/"
ENERGA_PAGE = "https://energa-operator.pl/przylaczenie-do-sieci/informacje-o-stanie-przylaczen"
PGE_PAGE = "https://pgedystrybucja.pl/przylaczenia/procedury-przylaczeniowe/dostepne-moce-dla-zrodel-wytworczych"
PSE_PAGE = "https://www.pse.pl/obszary-dzialalnosci/krajowy-system-elektroenergetyczny/informacja-o-dostepnosci-mocy-przylaczeniowej"
ENEA_PAGE = "https://www.operator.enea.pl/przylaczone-i-dostepne-moce-oze"
STOEN_PAGE = "https://www.stoen.pl/strona/siec"
LIVE = {"application", "study", "conditions", "agreement"}
PENDING = {"application", "study"}          # not yet in the operators' values
NAMES = {"tauron": "TAURON Dystrybucja", "energa": "Energa-Operator", "pge": "PGE Dystrybucja", "pse": "PSE"}


def get(url, timeout=90, **kwargs):
    for attempt in range(4):
        try:
            response = requests.get(url, headers=UA, timeout=timeout, verify=verify_for(url), **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def archived(operator, name, content):
    folder = os.path.join(ARCHIVE, operator)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    if not os.path.exists(path) or open(path, "rb").read() != content:
        open(path, "wb").write(content)
    return path


def number(cell):
    text = str(cell or "").strip().replace(",", ".")
    return float(text) if re.fullmatch(r"\d+(\.\d+)?", text) else None


def names_of(cell):
    """'Białogard, Dunowo, ... Kołobrzeg 6 Dywizji,\nKołobrzeg Koszalińska' -> names; '*' marks a station the operator doesn't own."""
    text = re.sub(r"\s*\n\s*", " ", cell or "")
    out = []
    for part in text.split(","):
        part = part.strip()
        if part:
            out.append({"name": part.replace("*", "").strip(), "own": "*" not in part})
    return out


# ------------------------------------------------------------------ TAURON

def tauron_post(route, body):
    """TAURON's API answers 429 (MAP_RATE_LIMITED) after a few hundred quick calls: pace them, and wait it out."""
    for attempt in range(6):
        try:
            response = requests.post(TAURON_API + route, json=body, headers=UA, timeout=60)
            if response.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            response.raise_for_status()
            time.sleep(1.0)
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == 5:
                raise
            time.sleep(60 * (attempt + 1))


def series(points):
    return {str(p["year"]): p["value_mw"] for p in points or [] if p.get("year") in YEARS}


def fetch_tauron():
    """About 120 calls: the GPZ list (3), one summary per group for its 110 kV values (about 75), and a search
    per year and direction for every GPZ's own medium-voltage value (a GPZ not returned has none)."""
    gpz, offset = [], 0
    while True:
        items = tauron_post("map/sources", {"kind": "gpz", "query": "", "page": {"offset": offset, "limit": 200}})["data"]["items"]
        gpz.extend(items)
        if len(items) < 200:
            break
        offset += 200
    groups = {}
    for item in gpz:
        if item["group_id"] in groups:
            continue
        data = tauron_post("summary", {"layer_type": "wytworcy", "target": {"type": "gpz", "id": item["id"]}, "page": {"offset": 0, "limit": 50}})["data"]
        groups[item["group_id"]] = {"name": item["group_name"], "gen": series(data["forecasts"].get("wytworcy")), "dem": series(data["forecasts"].get("odbiorcy"))}
    print(f"TAURON: {len(gpz)} GPZ in {len(groups)} groups", flush=True)
    mv = {}
    for layer, key in (("wytworcy", "mv_gen"), ("odbiorcy", "mv_dem")):
        for year in YEARS:
            offset = 0
            while True:
                data = tauron_post("gpz/search", {"layer_type": layer, "criteria": {"minimum_mw": 0.001, "year_from": year, "year_to": year, "province": ""},
                                                  "page": {"offset": offset, "limit": 100}})["data"]
                for match in data["matches"]:
                    mv.setdefault(match["gpz_id"], {}).setdefault(key, {})[str(year)] = match["value_mw"]
                offset += 100
                if offset >= data["total"]:
                    break
    stations = []
    for item in gpz:
        own = mv.get(item["id"], {})
        stations.append({"id": item["id"], "code": item.get("gpz_code") or "", "name": item["name"], "group": item["group_id"],
                         "lat": (item.get("location") or {}).get("lat"), "lon": (item.get("location") or {}).get("lng"),
                         "active": item.get("is_active", True), "external": item.get("is_external", False),
                         "mv_gen": {str(y): own.get("mv_gen", {}).get(str(y), 0) for y in YEARS},
                         "mv_dem": {str(y): own.get("mv_dem", {}).get(str(y), 0) for y in YEARS}, "construction": {}})
    snapshot = {"groups": groups, "stations": stations}
    text = json.dumps(snapshot, ensure_ascii=False, indent=0, sort_keys=True)
    archived("tauron", "latest.json", text.encode("utf-8"))
    return snapshot


# ------------------------------------------------------------------ Energa

def energa_parse(path, kind):
    """kind 'gen' or 'dem' -> (asof, {no: group})."""
    groups, tables = {}, {}
    with pdfplumber.open(path) as pdf:
        head = pdf.pages[0].extract_text() or ""
        date = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", head)
        asof = f"{date.group(3)}-{date.group(2)}-{date.group(1)}" if date else None
        for page in pdf.pages:
            text = page.extract_text() or ""
            caption = re.search(r"Tabela (2\.\d)", text)
            for table in page.extract_tables():
                header = " ".join(str(c or "") for c in table[0])
                for row in table:
                    cells = [str(c or "").strip() for c in row]
                    if len(cells) < 3 or not re.fullmatch(r"\d+", cells[0]):
                        continue
                    no = int(cells[0])
                    if "Model sieci" in header:          # the stations of each group
                        group = groups.setdefault(no, {"no": no})
                        group["name"] = re.sub(r"\s+", " ", cells[1])
                        group["stations"] = names_of(cells[2])
                        group["changes"] = re.sub(r"\s+", " ", cells[3]).strip() if len(cells) > 3 else ""
                    elif len(cells) >= 9:                # name + 2026..2031 + 2035
                        values = [number(c) for c in cells[2:9]]
                        key = caption.group(1) if caption else "?"
                        tables.setdefault(key, {})[no] = {"name": re.sub(r"\s+", " ", cells[1]), "values": values}
    return asof, groups, tables


def fetch_energa():
    page = get(ENERGA_PAGE).text
    found = {}
    for kind, word in (("gen", "wytworczej"), ("dem", "odbiorczej")):
        links = re.findall(rf'href="(https://cdn-netpr\.pl/[^"]*informacja_o_wartosci_lacznej_dostepnej_{word}_mocy_przylaczeniowej_(\d\d)_(\d\d)_(\d{{4}})\.pdf)"', page)
        if not links:
            raise RuntimeError(f"Energa's page links no '{word}' capacity PDF: check {ENERGA_PAGE}")
        url, d, m, y = sorted(links, key=lambda l: (l[3], l[2], l[1]))[-1]
        path = archived("energa", f"{y}-{m}-{d}_{kind}.pdf", get(url).content)
        found[kind] = (url, *energa_parse(path, kind))
    groups = {}
    for kind in ("gen", "dem"):
        url, asof, members, tables = found[kind]
        for no, group in members.items():
            groups.setdefault(no, {"no": no, "name": group["name"], "stations": group["stations"], "changes": group["changes"]})
        main = tables.get("2.3") or {}
        if kind == "dem":
            main = tables.get("2.3") or next(iter(tables.values()), {})
        for no, row in main.items():
            target = groups.setdefault(no, {"no": no, "name": row["name"], "stations": [], "changes": ""})
            target[kind] = dict(zip([str(y) for y in YEARS], row["values"][:6]))
            target[kind + "_2035"] = row["values"][6]
        if kind == "gen" and "2.4" in tables:
            for no, row in tables["2.4"].items():
                if no in groups:
                    groups[no]["gen_alt"] = dict(zip([str(y) for y in YEARS], row["values"][:6]))
    return {"asof": found["gen"][1], "asof_dem": found["dem"][1], "urls": {k: v[0] for k, v in found.items()}, "groups": groups}


# ------------------------------------------------------------------ PGE

def pge_parse(path):
    groups = {}
    with pdfplumber.open(path) as pdf:
        head = " ".join((pdf.pages[i].extract_text() or "") for i in range(min(2, len(pdf.pages))))
        date = re.search(r"stan na (\d{2})\.(\d{2})\.(\d{4})", head)
        asof = f"{date.group(3)}-{date.group(2)}-{date.group(1)}" if date else None
        for page in pdf.pages:
            text = page.extract_text() or ""
            kind = "gen" if "wytwórcz" in text and "Tabela" in text else "dem" if "odbiorcz" in text and "Tabela" in text else None
            if not kind:
                continue
            for table in page.extract_tables():
                for row in table:
                    cells = [str(c or "").strip() for c in row]
                    if len(cells) < 9 or not re.fullmatch(r"\d+", cells[0]):
                        continue
                    no = int(cells[0])
                    group = groups.setdefault(no, {"no": no, "name": re.sub(r"\s+", " ", cells[1]), "stations": names_of(cells[2]), "changes": ""})
                    group[kind] = dict(zip([str(y) for y in YEARS], [number(c) for c in cells[3:9]]))
    return asof, groups


def fetch_pge():
    page = get(PGE_PAGE).text
    links = re.findall(r'href="(/content/download/[^"]*informacja-o-lacznej-dostepnej-mocy-przylaczeniowej\.pdf[^"]*)"', page)
    if not links:
        raise RuntimeError(f"PGE's page links no capacity PDF: check {PGE_PAGE}")
    url = "https://pgedystrybucja.pl" + links[0].replace("&amp;", "&")
    content = get(url).content
    asof, groups = pge_parse(io.BytesIO(content))
    archived("pge", f"{asof or 'undated'}.pdf", content)
    return {"asof": asof, "urls": {"gen": url}, "groups": groups}


# ------------------------------------------------------------------ PSE

def pse_parse(path):
    """The tree of areas, groups and stations with [step 1 2026, step 1 2031, step 2 2026, step 2 2031],
    and the new stations with their demand capacity."""
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    head = re.search(r"STAN NA (\d{1,2}) (\w+) (\d{4})", pages[0])
    asof = None
    if head:
        from pse_parse import polish_date
        asof = polish_date(f"{head.group(1)} {head.group(2).lower()} {head.group(3)}")
    text = "\n".join(pages)
    start, end = text.find("2. Zestawienie dostępnych mocy przyłączeniowych wytwórczych"), text.find("Legenda:")
    # A long group name wraps: "Grupa AGLOMERACJA" / "1.1.1 0 0 0 0" / "SZCZECIŃSKA".
    nodes, area, group, pending = [], None, None, ""
    for line in text[start:end].splitlines()[2:]:
        line = line.strip()
        m = re.match(r"^(.*?)\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$", line)
        if not m:
            if re.fullmatch(r"[A-ZĄĆĘŁŃÓŚŹŻ\- ]{3,}", line) and nodes and nodes[-1]["level"] == "group" and pending == "":
                nodes[-1]["name"] += " " + line          # the wrapped end of the last group's name
                group = nodes[-1]["name"]
                for node in nodes:
                    if node.get("group_ref") is nodes[-1]:
                        node["group"] = group
            elif line and not re.fullmatch(r"[\d.]+", line):
                pending = (pending + " " + line).strip()
            continue
        label, values = re.sub(r"^\d+(\.\d+)*\.?\s*", "", m.group(1)).strip(), [int(m.group(i)) for i in range(2, 6)]
        if pending:
            label = (pending + " " + label).strip()
            pending = ""
        if label.startswith("POLSKA"):
            nodes.append({"level": "country", "name": "POLSKA", "values": values})
            continue
        if label.startswith("Obszar"):
            area = label.replace("Obszar ", "").strip()
            nodes.append({"level": "area", "name": area, "values": values})
        elif label.startswith("Podobszar"):
            nodes.append({"level": "subarea", "name": label.replace("Podobszar ", ""), "area": area, "values": values})
        elif label.startswith("Grupa"):
            group = label.replace("Grupa ", "").strip()
            nodes.append({"level": "group", "name": group, "area": area, "values": values})
        else:
            node = {"level": "station", "name": label, "area": area, "group": group, "values": values, "future": "w przyszłości" in label}
            node["group_ref"] = next((n for n in reversed(nodes) if n["level"] == "group"), None)
            nodes.append(node)
    for node in nodes:
        node.pop("group_ref", None)
    new = []
    for match in re.finditer(r"^\s*\d+\.\s+([A-Z0-9]{2,4})\s+[–-]\s+(.+?)\s+(\d{2,4})\s*$", text[text.find("5. Zestawienie dostępnych mocy przyłączeniowych odbiorczych"):], re.M):
        new.append({"code": match.group(1), "name": match.group(2).strip(), "mw": int(match.group(3))})
    return asof, nodes, new


def fetch_pse():
    page = get(PSE_PAGE).text
    links = re.findall(r'href="(/documents/[^"]*informacja_o_dostepnosci_mocy_przylaczeniowej_do_sieci_przesylowej\.pdf[^"]*)"', page)
    if not links:
        raise RuntimeError(f"PSE's page links no capacity PDF: check {PSE_PAGE}")
    url = "https://www.pse.pl" + links[0].replace("&amp;", "&")
    content = get(url).content
    asof, nodes, new = pse_parse(io.BytesIO(content))
    archived("pse", f"{asof or 'undated'}.pdf", content)
    return {"asof": asof, "urls": {"gen": url.split("?")[0]}, "nodes": nodes, "new": new}


# ------------------------------------------------------------------ build

def battery(gen, dem):
    return {y: (min(gen[y], dem[y]) if gen.get(y) is not None and dem.get(y) is not None else None) for y in gen}


def centroid(points):
    points = [p for p in points if p.get("lat") is not None]
    if not points:
        return None, None
    return round(sum(p["lat"] for p in points) / len(points), 5), round(sum(p["lon"] for p in points) / len(points), 5)


def km(a, b):
    import math
    dlat, dlon = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def median(values):
    values = sorted(values)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


GROUP_RADIUS_KM = 90   # a group's stations load the same lines: one placed farther than this from the rest is a namesake


def place_group(places, names, op):
    """Places a group's stations together.  A first pass takes each name's OpenStreetMap substation (or town) in the
    operator's voivodeships; the median of what is found is the group's centre (namesakes far away don't move it);
    then each name takes its candidate nearest that centre, and none farther than GROUP_RADIUS_KM."""
    first = [places.substation(name, op) for name in names]
    anchors = [(p[0], p[1]) for p in first if p]
    if not anchors:
        return [None] * len(names)
    centre = (median([a[0] for a in anchors]), median([a[1] for a in anchors]))
    out = []
    for name, found in zip(names, first):
        key = clean_name(name)
        candidates = [(lat, lon, "substation") for lat, lon, *_ in places.subs.get(key, [])]
        words = key.split()
        if len(words) >= 2 and len(words[-1]) > 4:
            candidates += [(lat, lon, "substation") for lat, lon, *_ in places.subs.get(" ".join(words[1:]), [])]
        candidates += [(lat, lon, "place") for lat, lon, _ in places.places.get(key, []) + places.places.get(fold(name), [])]
        if found:
            candidates.append((found[0], found[1], found[3]))
        candidates = [c for c in candidates if km(centre, c[:2]) <= GROUP_RADIUS_KM]
        # The nearest substation, else the nearest town.
        candidates.sort(key=lambda c: (c[2] != "substation", km(centre, c[:2])))
        out.append(candidates[0] if candidates else None)
    return out


def queue_projects():
    out = []
    for name in ("queue.json", "queue_dso.json"):
        path = os.path.join(ROOT, "data", name)
        if os.path.exists(path):
            for p in json.load(open(path, encoding="utf-8"))["projects"]:
                if p.get("phase") in LIVE and not p.get("left"):
                    out.append(p)
    return out


def tally(projects):
    q = {"pending_in": 0.0, "pending_out": 0.0, "held_in": 0.0, "held_out": 0.0, "n_pending": 0, "n_held": 0, "storage_mw": 0.0, "by_tech": {}}
    for p in projects:
        side = "pending" if p["phase"] in PENDING else "held"
        q[f"{side}_in"] += p.get("mw_in") or 0
        q[f"{side}_out"] += p.get("mw_out") or 0
        q[f"n_{side}"] += 1
        if p.get("technology") in ("storage", "hybrid"):
            q["storage_mw"] += p.get("mw") or 0
        q["by_tech"][p.get("technology") or "other"] = round(q["by_tech"].get(p.get("technology") or "other", 0) + (p.get("mw") or 0), 1)
    return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in q.items()}


def build(tauron, energa, pge, pse):
    places, stations_db = Places(), Stations()
    projects = queue_projects()
    areas, unplaced = [], []

    # TAURON: by code ("WZA4" in the queue, "WZA3" or "BLA3-1" in the map: the three letters match).
    by_code = {}
    for p in projects:
        if p.get("grid") == "tauron":
            for code in re.split(r"\s+", p.get("station") or ""):
                if re.fullmatch(r"[A-Z][A-Z0-9]{2}\d.*", code):
                    by_code.setdefault(code[:3], []).append(p)
                    break
    for gid, group in tauron["groups"].items():
        members = [s for s in tauron["stations"] if s["group"] == gid]
        seen, mine = set(), []
        for s in members:
            for p in by_code.get(s["code"][:3], []):
                if id(p) not in seen:
                    seen.add(id(p))
                    mine.append(p)
        lat, lon = centroid(members)
        areas.append({"id": f"tauron-{gid}", "op": "tauron", "name": group["name"], "level": "group", "lat": lat, "lon": lon,
                      "gen": group["gen"], "dem": group["dem"], "bess": battery(group["gen"], group["dem"]),
                      "stations": [{"name": s["name"], "code": s["code"], "lat": s["lat"], "lon": s["lon"], "how": "operator",
                                    "own": not s["external"], "mv_gen": s["mv_gen"], "mv_dem": s["mv_dem"]} for s in members],
                      "queue": tally(mine)})

    # Energa and PGE: groups of named stations.
    for op, source in (("energa", energa), ("pge", pge)):
        by_name = {}
        for p in projects:
            if p.get("grid") == op:
                for name in p.get("stations") or []:
                    by_name.setdefault(clean_name(name), []).append(p)
        for no, group in sorted(source["groups"].items()):
            if "gen" not in group and "dem" not in group:
                continue
            members, mine, seen = [], [], set()
            placed = place_group(places, [s["name"] for s in group["stations"]], op)
            for station, place in zip(group["stations"], placed):
                entry = {"name": station["name"], "own": station["own"], "lat": None, "lon": None, "how": None}
                if place:
                    entry.update(lat=round(place[0], 5), lon=round(place[1], 5), how=place[2])
                else:
                    unplaced.append(f"{op}: {station['name']}")
                members.append(entry)
                for p in by_name.get(clean_name(station["name"]), []):
                    if id(p) not in seen:
                        seen.add(id(p))
                        mine.append(p)
            lat, lon = centroid(members)
            gen = group.get("gen") or {str(y): None for y in YEARS}
            dem = group.get("dem") or {str(y): None for y in YEARS}
            area = {"id": f"{op}-{no}", "op": op, "no": no, "name": group["name"], "level": "group", "lat": lat, "lon": lon,
                    "gen": gen, "dem": dem, "bess": battery(gen, dem), "stations": members, "queue": tally(mine)}
            for extra in ("gen_alt", "gen_2035", "dem_2035", "changes"):
                if group.get(extra) not in (None, "", {}):
                    area[extra] = group[extra]
            areas.append(area)

    # PSE: stations, values for 2026 and 2031 only (step 2, the published one; step 1 kept).
    pse_queue = {}
    for p in projects:
        if p.get("grid", "pse") == "pse" and p.get("operator", "").startswith("PSE"):
            for name in p.get("stations") or []:
                pse_queue.setdefault(fold(name), []).append(p)
    pse_groups = {}
    for node in pse["nodes"]:
        if node["level"] == "group":
            pse_groups[node["name"]] = node
    for node in pse["nodes"]:
        if node["level"] != "station":
            continue
        name = re.sub(r"\s*\(w przyszłości\)", "", node["name"]).strip()
        canonical = stations_db.one(name) or name
        info = stations_db.stations.get(canonical, {})
        lat, lon = info.get("lat"), info.get("lon")
        if lat is None:
            place = places.substation(name, "pge") or places.substation(name, "energa") or places.substation(name, "tauron") or places.substation(name, "enea")
            if place:
                lat, lon = round(place[0], 5), round(place[1], 5)
        if lat is None:
            unplaced.append(f"pse: {name}")
        v = node["values"]
        gen = {"2026": v[2], "2031": v[3]}
        areas.append({"id": f"pse-{fold(name).replace(' ', '-')}", "op": "pse", "name": name, "level": "station", "lat": lat, "lon": lon,
                      "group": node.get("group"), "area": node.get("area"), "future": node["future"],
                      "gen": gen, "gen_step1": {"2026": v[0], "2031": v[1]}, "dem": {}, "bess": {},
                      "group_gen": {"2026": pse_groups.get(node.get("group"), {}).get("values", [0, 0, 0, 0])[2],
                                    "2031": pse_groups.get(node.get("group"), {}).get("values", [0, 0, 0, 0])[3]},
                      "stations": [{"name": name, "lat": lat, "lon": lon, "how": "station" if info else ("substation" if lat else None)}],
                      "queue": tally(pse_queue.get(fold(canonical), []) + ([] if fold(canonical) == fold(name) else pse_queue.get(fold(name), [])))})

    # PSE's planned new stations for demand ("stacja w rejonie Opola" is placed at Opole, approximately).
    new = []
    for s in pse["new"]:
        lat = lon = None
        how = None
        named = re.search(r"\(([^)]+)\)", s["name"]) or re.search(r"stacja\s+([A-ZŁŚŻ][\wąćęłńóśźż]+)$", s["name"])
        region = re.search(r"w rejonie\s+(.+?)$", s["name"])
        for candidate in [named.group(1) if named else None, s["name"].split("–")[-1].strip()]:
            if candidate:
                place = places.substation(candidate, "pge") or places.substation(candidate, "energa") or places.substation(candidate, "tauron") or places.substation(candidate, "enea")
                if place:
                    lat, lon, how = round(place[0], 5), round(place[1], 5), "named"
                    break
        if lat is None and region:
            town = region.group(1).strip()
            stem = {"Warszawy": "Warszawa", "Legnicy": "Legnica", "Stalowej Woli": "Stalowa Wola", "Opola": "Opole", "Złoczewa": "Złoczew",
                    "Poznania": "Poznań", "Konina": "Konin", "Trójmiasta": "Gdańsk", "Torunia": "Toruń", "Portu Polska": "Baranów",
                    "Kutna": "Kutno", "Włocławka": "Włocławek"}.get(town, town)
            for op in ("pge", "energa", "tauron", "enea"):
                place = places.settlement(stem, [r for r in __import__("dso_build").OPERATORS[op]["regions"]])
                if place:
                    lat, lon, how = round(place[0], 5), round(place[1], 5), "near"
                    break
        years = PLAN_YEARS["stations"].get(s["code"], {})
        new.append({**s, "lat": lat, "lon": lon, "how": how,
                    **{f"{k}_{f}": years[k][f] for k in ("plan", "draft") if k in years for f in ("item", "end")},
                    **({"title": years.get("draft", years.get("plan", {})).get("title")} if years else {})})

    operators = {
        "tauron": {"name": NAMES["tauron"], "asof": tauron.get("asof"), "page": TAURON_PAGE,
                   "level": "110 kV groups of GPZ, with each GPZ's medium-voltage value"},
        "energa": {"name": NAMES["energa"], "asof": energa["asof"], "asof_dem": energa.get("asof_dem"), "page": ENERGA_PAGE, "urls": energa["urls"],
                   "level": "groups of 110 kV stations (coherent nodes)"},
        "pge": {"name": NAMES["pge"], "asof": pge["asof"], "page": PGE_PAGE, "urls": pge["urls"], "level": "groups of 110 kV stations"},
        "pse": {"name": NAMES["pse"], "asof": pse["asof"], "page": PSE_PAGE, "urls": pse["urls"], "level": "220/400 kV stations, 2026 and 2031 only",
                "plans": PLAN_YEARS["sources"]},
        "enea": {"name": "ENEA Operator", "page": ENEA_PAGE, "excluded": "asks for consent before its values are republished station by station"},
        "stoen": {"name": "Stoen Operator", "page": STOEN_PAGE, "excluded": "asks for consent before its values are republished station by station"},
    }
    return {"years": YEARS, "operators": operators, "areas": areas, "pse_new": new, "unplaced": sorted(set(unplaced))}


def tauron_saved(before):
    """TAURON's last snapshot from the archive when it was read less than a week ago, else None."""
    path = os.path.join(ARCHIVE, "tauron", "latest.json")
    read = ((before or {}).get("operators", {}).get("tauron", {}) or {}).get("read")
    if not os.path.exists(path) or not read:
        return None
    if (datetime.date.today() - datetime.date.fromisoformat(read)).days >= TAURON_EVERY_DAYS:
        return None
    return json.load(open(path, encoding="utf-8"))


def main(force=False):
    before = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else None
    sources, errors, read = {}, [], {}
    saved = None if force else tauron_saved(before)
    for op, fetch in (("tauron", fetch_tauron), ("energa", fetch_energa), ("pge", fetch_pge), ("pse", fetch_pse)):
        if op == "tauron" and saved:
            sources[op] = saved
            read[op] = before["operators"]["tauron"]["read"]
            continue
        try:
            sources[op] = fetch()
            read[op] = datetime.date.today().isoformat()
        except Exception as error:  # noqa: BLE001 -- one operator's failure keeps the others' latest values
            errors.append(f"{op}: {error}")
            print(f"Grid capacity: {op} failed: {error}")
    if errors and before is None:
        raise RuntimeError("; ".join(errors))
    if "tauron" in sources:
        # TAURON's map gives no date: "asof" is the first day these values were read.
        digest = hashlib.sha1(json.dumps({k: sources["tauron"][k] for k in ("groups", "stations")}, sort_keys=True).encode()).hexdigest()[:10]
        previous = (before or {}).get("operators", {}).get("tauron", {})
        sources["tauron"]["asof"] = previous.get("asof") if previous.get("digest") == digest else read["tauron"]
        sources["tauron"]["digest"] = digest
    if len(sources) < 4:
        # Keep the operator's previous areas when its source failed today.
        result = build(*(sources.get(op) or {"groups": {}, "stations": [], "nodes": [], "new": [], "asof": None, "urls": {}} for op in ("tauron", "energa", "pge", "pse")))
        for op in ("tauron", "energa", "pge", "pse"):
            if op not in sources and before:
                result["areas"] = [a for a in result["areas"] if a["op"] != op] + [a for a in before["areas"] if a["op"] == op]
                result["operators"][op] = before["operators"][op]
                if op == "pse":
                    result["pse_new"] = before.get("pse_new", [])
    else:
        result = build(sources["tauron"], sources["energa"], sources["pge"], sources["pse"])
    if "tauron" in sources:
        result["operators"]["tauron"]["digest"] = sources["tauron"]["digest"]
    for op, day in read.items():
        result["operators"][op]["read"] = day
    result["updated"] = datetime.date.today().isoformat()
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    old = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
    changed = re.sub(r'"updated":"[^"]*"', "", text) != re.sub(r'"updated":"[^"]*"', "", old)
    if changed:
        open(OUT, "w", encoding="utf-8").write(text)
    counts = {op: sum(1 for a in result["areas"] if a["op"] == op) for op in ("tauron", "energa", "pge", "pse")}
    print(f"Grid capacity: {counts}, {len(result['pse_new'])} new PSE stations, {len(result['unplaced'])} stations not placed"
          f"{'' if changed else ' (unchanged)'}")
    return changed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(force="--force" in sys.argv)
