"""
The distribution operators' projects (TAURON Dystrybucja, ENEA Operator)
from data/snapshots/<operator>/, in the shape build.py gives PSE's.

Linking:
  new layout   by the operator's object id (TAURON "1866467", ENEA "6163/2026");
  old layouts  by place, kind, kW and conditions date from quarter to quarter,
               and into the first new-layout list by conditions date, MW and
               kind (one candidate only, or no link).
Placing:
  TAURON's station codes (WZA4) through etl/tauron_stations.json (from TAURON's
  "dostępna moc" lists) to a name; ENEA's station text to a name; names to an
  OpenStreetMap substation of that name in the operator's voivodeships
  (etl/osm_places.json), else a settlement of that name.  The old layouts only
  give a gmina or a village: placed at that settlement.
Phases are PSE's; the old layouts' "may sign a distribution contract" and
"delivery started" dates, and the new layout's "obiekt przyłączony", make a
project operational.
"""

import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stations import fold  # noqa: E402

ROOT = os.path.dirname(HERE)
SNAPSHOTS = os.path.join(ROOT, "data", "snapshots")
OPERATORS = {
    "tauron": {"name": "TAURON Dystrybucja S.A.", "regions": ["Dolnośląskie", "Opolskie", "Śląskie", "Małopolskie", "Podkarpackie", "Łódzkie", "Świętokrzyskie"]},
    "enea": {"name": "ENEA Operator Sp. z o.o.", "regions": ["Zachodniopomorskie", "Lubuskie", "Wielkopolskie", "Kujawsko-pomorskie", "Dolnośląskie", "Pomorskie"]},
}
NOISE = r"\b(gpz|rpz|rs|st|se|sto|stacja|elektroenergetyczna|transformatorowa|rozdzielnia|rozdzielcza|glowny|punkt|zasilajacy|zasilania|abonencka|kv|wn|sn|nn)\b"


def clean_name(text):
    text = fold(re.sub(r"\(.*?\)", " ", text or ""))
    text = re.sub(r"\b\d+(\s+\d+)*\b", " ", text)
    return re.sub(r"\s+", " ", re.sub(NOISE, " ", text)).strip()


def station_name(text):
    """ENEA's 'Oborniki pole nr 14 / L-Oborniki-Żydowo', 'GPZ Łużycka (24)', 'Nagradowice, GPZ' -> 'Oborniki', ..."""
    text = (text or "").split("/")[0]
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.split(r"(?i)\b(?:pole|pola|sekcja|sekc\.|szyna|linia|zalicznikowo|nr|rozdz\w*|ZK|złącze)\b|\bL-", text)[0]
    return re.sub(r"\s+", " ", re.sub(r"(?i)\b(GPZ|RPZ|RS|ST|SE|stacja)\b|[,;:]", " ", text)).strip()


class Places:
    def __init__(self):
        path = os.path.join(HERE, "osm_places.json")
        data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"substations": [], "places": []}
        geo = json.load(open(os.path.join(ROOT, "geo", "poland_voivodeships.json"), encoding="utf-8"))
        self.regions = [(f["properties"]["name"], self._rings(f["geometry"])) for f in geo["features"]]
        self.subs, self.places = collections.defaultdict(list), collections.defaultdict(list)
        for name, lat, lon, voltage, operator in data["substations"]:
            key = clean_name(name)
            if key:
                self.subs[key].append((lat, lon, voltage, operator))
        for name, lat, lon, kind in data["places"]:
            self.places[fold(name)].append((lat, lon, kind))
        self.codes = json.load(open(os.path.join(HERE, "tauron_stations.json"), encoding="utf-8"))
        self.cache = {}

    @staticmethod
    def _rings(geometry):
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        return [polygon[0] for polygon in polygons]

    def region(self, lat, lon):
        for name, rings in self.regions:
            for ring in rings:
                inside, j = False, len(ring) - 1
                for i in range(len(ring)):
                    xi, yi = ring[i]
                    xj, yj = ring[j]
                    if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                        inside = not inside
                    j = i
                if inside:
                    return name
        return None

    def _pick(self, candidates, regions):
        found = []
        for candidate in candidates:
            region = self.region(candidate[0], candidate[1])
            if region in regions:
                found.append((candidate, region))
        return found

    def substation(self, name, operator):
        key = (clean_name(name), operator)
        if key in self.cache:
            return self.cache[key]
        regions = OPERATORS[operator]["regions"]
        result = None
        if key[0]:
            found = self._pick(self.subs.get(key[0], []), regions)
            words = key[0].split()
            if not found and len(words) >= 2:
                # "Poznań Sołacz" may be mapped as "Sołacz"; "Stargard Zachód" as "Stargard Zachód" only.
                found = [f for f in self._pick(self.subs.get(" ".join(words[1:]), []), regions) if len(words[-1]) > 4]
            # Several of that name: the one at 110 kV (a GPZ), else the first.
            found.sort(key=lambda item: ("110000" not in str(item[0][2]), operator not in fold(item[0][3])))
            if found:
                (lat, lon, *_), region = found[0]
                result = (lat, lon, region, "substation")
            else:
                found = self._pick(self.places.get(key[0], []), regions) or self._pick(self.places.get(fold(name), []), regions)
                found.sort(key=lambda item: ["city", "town", "village", "suburb", "quarter", "hamlet"].index(item[0][2]))
                if found:
                    (lat, lon, _), region = found[0]
                    result = (lat, lon, region, "place")
                elif len(key[0].split()) >= 2:
                    # Last resort: the town the station is named after ("Poznań Rataje" -> Poznań).
                    found = self._pick(self.places.get(key[0].split()[0], []), regions)
                    found = [f for f in found if f[0][2] in ("city", "town")]
                    if found:
                        (lat, lon, _), region = found[0]
                        result = (lat, lon, region, "place")
        self.cache[key] = result
        return result

    def settlement(self, name, regions):
        key = (fold(name), tuple(regions))
        if key not in self.cache:
            found = self._pick(self.places.get(fold(name), []), regions)
            found.sort(key=lambda item: ["city", "town", "village", "suburb", "quarter", "hamlet"].index(item[0][2]))
            self.cache[key] = (found[0][0][0], found[0][0][1], found[0][1], "place") if found else None
        return self.cache[key]

    def tauron_code(self, code):
        match = re.fullmatch(r"([A-Z][A-Z0-9]{2})(\d*)", (code or "").strip())
        if not match:
            return None
        return self.codes.get(match.group(1))


# ------------------------------------------------------------- rows

def row_phase(row, phase_of_status):
    if row["layout"] == "new":
        if row["status"] == "connected":
            return "operational"
        if row["status"] == "agreement" and row["deadline"] and row["deadline"] < row["asof"]:
            return "operating"
        return phase_of_status.get(row["status"], "conditions")
    if row.get("started") or row.get("connected"):
        return "operational"
    if "odmów" in (row.get("remarks") or "").lower():
        return "refused"
    if row.get("agreement"):
        return "agreement"
    return "conditions" if row.get("conditions") else "study"


def normalise(raw, layout, asof):
    row = {"asof": asof, "layout": layout, "id": raw.get("id") or "", "applicant": raw.get("applicant") or "",
           "type": raw.get("type") or "", "installed": raw.get("installed") or {}, "mw_in": raw.get("mw_in"),
           "mw_out": raw.get("mw_out"), "kv": raw.get("kv") or ("SN" if layout != "new" else ""),
           "applied": raw.get("applied"), "advance": raw.get("advance"), "conditions": raw.get("conditions"),
           "agreement": raw.get("agreement"), "deadline": raw.get("delivery"), "refused": raw.get("refused"),
           "refusal_reason": raw.get("refusal_reason") or "", "expired": raw.get("expired"),
           "terminated": raw.get("terminated") or raw.get("lapsed"), "connected": raw.get("connected"),
           "started": raw.get("started"), "remarks": raw.get("remarks") or "", "status_pl": raw.get("status_pl") or "",
           "status": raw.get("status"), "station_text": raw.get("station") or "", "voivodeship": raw.get("voivodeship") or "",
           "gmina": raw.get("gmina") or raw.get("place") or "", "seat": raw.get("seat") or "", "kind": raw.get("kind") or "",
           "applicant_form": raw.get("applicant_form") or ""}
    if layout != "new":
        row["status"] = "connected" if row["started"] or row["connected"] else "agreement" if row["agreement"] else "conditions"
        if "odmów" in row["remarks"].lower():
            row["status"] = "refused"
    for field in ("applied", "advance", "conditions", "agreement", "deadline", "refused", "expired", "terminated", "connected", "started"):
        value = row[field]
        row[field] = value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    return row


def mw(row):
    values = [v for v in (row["mw_in"], row["mw_out"]) if v is not None]
    return max(values) if values else None


def codes_close(a, b):
    group = lambda t: "PV" if "PV" in t else "FW" if "FW" in t else "MEE" if "MEE" in t else t
    return not a or not b or "MIX" in (a, b) or "OZE" in (a, b) or group(a) == group(b)


def link(snapshots):
    """[(asof, layout, rows)] -> chains of rows, one chain per project."""
    chains, by_id, by_old = [], {}, {}
    for asof, layout, rows in snapshots:
        seen_old = collections.Counter()
        claimed = set()
        for row in rows:
            if layout == "new" and row["id"]:
                chain = by_id.get(row["id"])
                if chain is None:
                    chain = by_id[row["id"]] = []
                    chains.append(chain)
                if chain and chain[-1]["asof"] == asof:
                    # The same object on another line: another connection point or voltage level; MW add up.
                    prior = chain[-1]
                    if row["station_text"] and row["station_text"] not in prior["station_text"]:
                        prior["station_text"] = (prior["station_text"] + "\n" + row["station_text"]).strip()
                        for field in ("mw_in", "mw_out"):
                            if row[field] is not None:
                                prior[field] = round((prior[field] or 0) + row[field], 4)
                    continue
                chain.append(row)
                continue
            base = (row["voivodeship"], fold(row["gmina"]), row["type"], round(mw(row) or 0, 3), row["conditions"], fold(row["seat"]))
            seen_old[base] += 1
            key = base + (seen_old[base],)
            chain = by_old.get(key)
            if chain is None or id(chain) in claimed:
                chain = by_old[key] = []
                chains.append(chain)
            claimed.add(id(chain))
            chain.append(row)
    # Old-layout projects into the first new-layout list: same conditions date, MW within 0.06, same kind.
    new_firsts = collections.defaultdict(list)
    for chain in chains:
        if chain and chain[0]["layout"] == "new" and chain[0]["conditions"]:
            new_firsts[chain[0]["conditions"]].append(chain)
    # Every pair, closest MW first; each project on either side linked once.
    pairs = []
    for index, chain in enumerate(chains):
        if not chain or chain[-1]["layout"] == "new" or not chain[-1]["conditions"] or mw(chain[-1]) is None:
            continue
        last = chain[-1]
        for target in new_firsts.get(last["conditions"], []):
            if target[0]["asof"] > last["asof"] and mw(target[0]) is not None and codes_close(target[0]["type"], last["type"]):
                gap = abs(mw(target[0]) - mw(last))
                if gap <= 0.06:
                    same = (last["agreement"] or "") == (target[0]["agreement"] or "")
                    pairs.append((not same, gap, index, id(target), target))
    pairs.sort(key=lambda item: item[:2])
    merged, used_old, used_new = 0, set(), set()
    for _, _, index, target_id, target in pairs:
        if index in used_old or target_id in used_new:
            continue
        used_old.add(index)
        used_new.add(target_id)
        target[:0] = chains[index]
        chains[index] = []
        merged += 1
    return [chain for chain in chains if chain], merged


# ------------------------------------------------------------- projects

def place_of(chain, operator, places):
    regions = OPERATORS[operator]["regions"]
    for row in reversed(chain):
        if row["layout"] == "new" and row["station_text"]:
            text = row["station_text"].split("\n")[0]
            name = places.tauron_code(text) if operator == "tauron" else station_name(text)
            if name:
                found = places.substation(name, operator)
                if found:
                    return name, found
    for row in reversed(chain):
        if row["gmina"]:
            wanted = [row["voivodeship"]] if row["voivodeship"] in regions or row["voivodeship"] else regions
            wanted = [r for r in regions if fold(r) == fold(row["voivodeship"])] or regions
            found = places.settlement(row["gmina"], wanted)
            if found:
                return row["gmina"], found
    return None, None


def describe(chain, operator, operator_asofs, places, technology, phase_of_status, layouts=None):
    last = chain[-1]
    phases = [row_phase(row, phase_of_status) for row in chain]
    phase = phases[-1]
    latest = operator_asofs[-1]
    left = None
    if last["asof"] != latest:
        following = [a for a in operator_asofs if a > last["asof"]][0]
        if phase == "operational":
            left = {"asof": following, "reason": "connected"}
        elif phase in ("refused", "withdrawn"):
            left = {"asof": following, "reason": last["status"]}
        elif last["agreement"] and last["deadline"] and last["deadline"] <= following:
            phase, left = "operating", {"asof": following, "reason": "after_connection_date"}
        elif layouts and last["layout"] != "new" and layouts.get(following) == "new":
            # Not found in the operator's first list of the new layout: connected, dropped, or not linked.
            phase, left = "withdrawn", {"asof": following, "reason": "not_in_new_list"}
        else:
            phase, left = "withdrawn", {"asof": following, "reason": "removed"}

    def latest_of(field):
        for row in reversed(chain):
            if row.get(field):
                return row[field]
        return None

    milestones = {}

    def mark(key, date, asof):
        if key not in milestones:
            milestones[key] = {"date": date, "seen": asof}
        elif date and not milestones[key].get("date"):
            milestones[key]["date"] = date
    for row, row_ph in zip(chain, phases):
        if row["applied"]:
            mark("applied", row["applied"], row["asof"])
        if row["status"] == "study":
            mark("complete", None, row["asof"])
        if row["conditions"]:
            mark("conditions", row["conditions"], row["asof"])
        if row["agreement"]:
            mark("agreement", row["agreement"], row["asof"])
        if row["refused"] or row["status"] == "refused":
            mark("refused", row["refused"], row["asof"])
        if row["status"] in ("withdrawn", "expired", "terminated"):
            mark(row["status"], row["expired"] or row["terminated"], row["asof"])
        if row_ph == "operational":
            mark("connected", row["started"] or row["connected"] or row["deadline"], row["asof"])
    if phase == "operating":
        mark("connected", latest_of("deadline"), left["asof"] if left else last["asof"])
    if left and left["reason"] == "removed":
        mark("removed", None, left["asof"])
    cod = []
    for row in chain:
        if row["deadline"] and (not cod or cod[-1]["date"] != row["deadline"]):
            cod.append({"date": row["deadline"], "seen": row["asof"]})
    changes = []
    for before, after in zip(chain, chain[1:]):
        diffs = []
        for field, label in (("status", "Status"), ("mw_in", "MW to the grid"), ("mw_out", "MW from the grid"), ("deadline", "Connection date"),
                             ("agreement", "Agreement date"), ("applicant", "Applicant")):
            a, b = before.get(field), after.get(field)
            if field.startswith("mw") and (a is None or b is None or abs(a - b) < 0.01):
                continue
            if field == "applicant" and (not a or not b):
                continue
            if field == "status" and before["layout"] != after["layout"]:
                continue
            if a != b and (a or b):
                diffs.append({"field": field, "label": label, "from": a, "to": b})
        if diffs:
            changes.append({"asof": after["asof"], "diffs": diffs})

    name, found = place_of(chain, operator, places)
    lat, lon, region, how = found if found else (None, None, None, None)
    if not region:
        region = next((r for r in OPERATORS[operator]["regions"] if fold(r) == fold(last["voivodeship"])), None)
    kind = latest_of("type") or ""
    tech = technology(kind, last["installed"], last["kind"])
    if tech == "conventional":
        tech = "other"
    station_text = last["station_text"] or ", ".join(filter(None, [last["gmina"], last["voivodeship"]]))
    return {
        "grid": operator, "name": "", "applicant": latest_of("applicant") or "", "seat": latest_of("seat") or "",
        "operator": OPERATORS[operator]["name"], "pse_id": latest_of("id") or "",
        "type": kind, "technology": tech, "installed": last["installed"],
        "mw_in": last["mw_in"], "mw_out": last["mw_out"], "mw": mw(last), "change_request": False,
        "station": station_text, "stations": [name] if name else [], "kv": latest_of("kv") or "",
        "lat": lat, "lon": lon, "region": region, "placed": how,
        "status": last["status"] or "", "phase": phase, "left": left,
        "applied": latest_of("applied"), "advance": latest_of("advance"), "conditions": latest_of("conditions"),
        "agreement": latest_of("agreement"), "deadline": latest_of("deadline"),
        "refused": latest_of("refused"), "refusal_reason": latest_of("refusal_reason") or "",
        "expired": latest_of("expired"), "terminated": latest_of("terminated"),
        "connected": latest_of("started") or latest_of("connected"),
        "first_seen": chain[0]["asof"], "last_seen": last["asof"], "months_listed": len(chain),
        "milestones": milestones, "cod": cod, "changes": changes,
        "_timeline": [{"asof": row["asof"], "mw": mw(row), "phase": ph} for row, ph in zip(chain, phases)],
    }


def load_operator(operator):
    folder = os.path.join(SNAPSHOTS, operator)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        snapshot = json.load(open(os.path.join(folder, name), encoding="utf-8"))
        rows = [normalise(raw, snapshot["layout"], snapshot["asof"]) for raw in snapshot["rows"]
                if not (snapshot["layout"] == "old" and raw.get("applicant_form") == "2")]   # the column-number row
        out.append((snapshot["asof"], snapshot["layout"], rows, snapshot["source"]))
    return out


def projects(technology, phase_of_status):
    places = Places()
    out, sources, report = [], [], {}
    for operator in OPERATORS:
        snapshots = load_operator(operator)
        if not snapshots:
            continue
        chains, merged = link([(asof, layout, rows) for asof, layout, rows, _ in snapshots])
        asofs = [asof for asof, *_ in snapshots]
        layouts = {asof: layout for asof, layout, *_ in snapshots}
        built = [describe(chain, operator, asofs, places, technology, phase_of_status, layouts) for chain in chains]
        out.extend(built)
        sources.extend({"asof": asof, "doc": operator, "rows": len(rows), "url": source["url"], "published": source.get("published"), "layout": layout}
                       for asof, layout, rows, source in snapshots)
        report[operator] = {"projects": len(built), "linked_old_to_new": merged,
                            "placed": sum(1 for p in built if p["lat"] is not None),
                            "phases": dict(collections.Counter(p["phase"] for p in built))}
    return out, sources, report
