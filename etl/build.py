"""
Build data/queue.json (every project, with its history) and
data/history.json (the queue month by month) from data/snapshots/.

PSE gives no stable project id.  Projects are followed from month to month
by their conditions date, connection station, MW, applicant and (since
July 2026) name; see link().  Each project then gets:

  milestones   application, completeness, conditions, agreement,
               contractual connection date, and the exits (refusal,
               withdrawal, expiry), with the month PSE first showed each;
  changes      what changed between two monthly lists (MW, connection
               date, applicant, status...), as gridstatus shows them;
  phase        where it stands in the latest list, or how it left.

"Past connection date" is a phase of its own: PSE keeps plants on the
list with their agreement in force long after they start (Kozienice 11,
2017; Jaworzno 910 MW, 2020), so an agreement whose connection date has
passed almost always means a plant already in service.
"""

import collections
import datetime
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stations import Stations, fold  # noqa: E402

ROOT = os.path.dirname(HERE)
SNAPSHOTS = os.path.join(ROOT, "data", "snapshots")
OUT = os.path.join(ROOT, "data")

TECHNOLOGIES = [
    ("solar", "Solar PV"), ("wind", "Onshore wind"), ("offshore", "Offshore wind"), ("storage", "Storage"),
    ("hybrid", "Hybrid (PV, wind, storage)"), ("gas", "Gas"), ("coal", "Coal"), ("nuclear", "Nuclear"),
    ("other", "Other generation"), ("distribution", "Distribution (DSO)"), ("demand", "Demand"),
]
PHASES = [
    ("application", "Application"), ("study", "Under study"), ("conditions", "Conditions issued"),
    ("agreement", "Agreement signed"), ("operating", "Past connection date"),
    ("refused", "Refused"), ("withdrawn", "Withdrawn or lapsed"),
]
STATUS_PHASE = {
    "verification": "application", "incomplete": "application", "study": "study", "conditions": "conditions",
    "agreement": "agreement", "connected": "operating", "refused": "refused", "withdrawn": "withdrawn",
    "expired": "withdrawn", "terminated": "withdrawn",
}
CODES = r"MFW|FW|PV|MEE|EJ|ESP|JBM|JW|BGP|BG|SG|JS|JG|EW|OGW|ODB|OZE|KJW|MIX|OSDn|OSD|SD"
GAS = {"BG", "BGP", "SG", "JS", "JG"}
HIDDEN = "nie podlega publikacji"


def today():
    return datetime.date.today().isoformat()


def load(doc):
    folder = os.path.join(SNAPSHOTS, doc)
    if not os.path.isdir(folder):
        return []
    return [json.load(open(os.path.join(folder, name), encoding="utf-8")) for name in sorted(os.listdir(folder))]


# ------------------------------------------------------------- technology

def codes_of(kind):
    return set(re.findall(CODES, (kind or "").replace("PVG", "PV").replace("PVD", "PV")))


def technology(kind, installed=None, words=""):
    """PSE's installation type ('MEE+PV', 'SD (FW, MEE)', 'MIX', 'OZE') -> one of TECHNOLOGIES."""
    upper = (kind or "").upper().strip()
    if upper.startswith(("SD", "OSD")):
        return "distribution"
    codes = codes_of(upper)
    if "MIX" in codes and installed:
        codes = {code for key, code in (("pv", "PV"), ("wind", "FW"), ("storage_out", "MEE"), ("load", "ODB"))
                 if installed.get(key)} or codes
    generation = codes - {"ODB", "MIX"}
    if not generation:
        return "demand" if "ODB" in codes else "other"
    if generation == {"PV"}:
        return "solar"
    if generation == {"FW"}:
        return "wind"
    if generation == {"MFW"}:
        return "offshore"
    if generation == {"MEE"}:
        return "storage"
    if generation <= {"PV", "FW", "MEE"}:
        return "hybrid"
    if "EJ" in generation:
        return "nuclear"
    if "JW" in generation:
        return "coal"
    if generation & GAS and not generation - GAS - {"MEE", "PV"}:
        return "gas"
    if generation == {"OZE"}:  # before May 2023 PSE only said "renewable"
        text = fold(words)
        if re.search(r"baltic|baltyk|offshore|mfw", text):
            return "offshore"
        if re.search(r"wiatr|wind|\bfw\b", text):
            return "wind"
        if re.search(r"solar|\bpv\b|fotowolt|slonecz", text):
            return "solar"
        return "other"
    if generation == {"KJW"} or generation == {"KJW", "OZE"}:
        return "conventional"
    return "other"


# ------------------------------------------------------------- normalised rows

def mw_of(row):
    values = [value for value in (row.get("mw_in"), row.get("mw_out")) if value is not None]
    return max(values) if values else None


def normalise(row, doc, asof, stations):
    """One snapshot row -> the fields linking and the page use."""
    status = row.get("status")
    if doc == "wykaz":
        status = "agreement" if row.get("agreement") else "conditions"
    applicant = row.get("applicant") or ""
    if applicant.strip().lower() == HIDDEN:
        applicant = ""
    names = stations.resolve(row.get("station"))
    return {
        "asof": asof, "doc": doc, "status": status,
        "name": row.get("name") or "", "applicant": applicant, "seat": row.get("seat") if row.get("seat", "").lower() != HIDDEN else "",
        "station_text": row.get("station") or "", "stations": names,
        "kv": row.get("kv") or "", "type": row.get("type") or "", "installed": row.get("installed") or {},
        "mw_in": row.get("mw_in"), "mw_out": row.get("mw_out"), "change": bool(row.get("change")),
        "applied": iso(row.get("applied")), "advance": iso(row.get("advance")),
        "conditions": iso(row.get("conditions")), "agreement": iso(row.get("agreement")),
        "deadline": iso(row.get("deadline") or row.get("delivery")),
        "refused": iso(row.get("refused")), "refusal_reason": row.get("refusal_reason") or "",
        "expired": iso(row.get("expired")), "terminated": iso(row.get("terminated") or row.get("lapsed")),
        "id": row.get("id") or "", "operator": row.get("operator") or "", "multi": bool(row.get("multi")),
        "note": row.get("note") or "",
    }


def iso(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def mw_close(a, b):
    if a is None or b is None:
        return False
    return abs(a - b) <= max(1.0, 0.02 * max(a, b))


def tech_compatible(a, b):
    ca, cb = codes_of(a["type"]), codes_of(b["type"])
    if not ca or not cb or {"OZE", "KJW", "MIX"} & (ca | cb):
        return True
    return bool(ca & cb)


def score(a, b):
    """How likely row b (this month) is project a's last row.  6 or more counts."""
    total = 0
    if a["conditions"] and b["conditions"]:
        total += 4 if a["conditions"] == b["conditions"] else -2
    if set(a["stations"]) & set(b["stations"]):
        total += 3
    if a["name"] and b["name"] and fold(a["name"]) == fold(b["name"]):
        total += 5
    if a["applicant"] and b["applicant"] and fold(a["applicant"]) == fold(b["applicant"]):
        total += 2
    if a["applied"] and b["applied"] and a["applied"] == b["applied"]:
        total += 2
    if mw_close(a["mw_in"], b["mw_in"]) or mw_close(mw_of(a), mw_of(b)):
        total += 2
    if tech_compatible(a, b):
        total += 1
    return total


# ------------------------------------------------------------- linking

def link(snapshots):
    """[(asof, doc, [rows])] in date order -> projects, each a list of its rows month by month."""
    projects = []
    active = {}   # project index -> number of snapshots since last seen
    for asof, doc, rows in snapshots:
        by_station, by_conditions, by_name = collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
        for index in active:
            last = projects[index][-1]
            for name in last["stations"]:
                by_station[name].add(index)
            if last["conditions"]:
                by_conditions[last["conditions"]].add(index)
            if last["name"]:
                by_name[fold(last["name"])].add(index)
        pairs = []
        for row_index, row in enumerate(rows):
            candidates = set()
            for name in row["stations"]:
                candidates |= by_station[name]
            if row["conditions"]:
                candidates |= by_conditions[row["conditions"]]
            if row["name"]:
                candidates |= by_name[fold(row["name"])]
            for index in candidates:
                value = score(projects[index][-1], row)
                # A month's gap costs a little, so the project seen last month wins a tie.
                if value >= 6:
                    pairs.append((value - 0.1 * active[index], row_index, index))
        pairs.sort(key=lambda pair: -pair[0])
        taken_rows, taken_projects = set(), set()
        for value, row_index, index in pairs:
            if row_index in taken_rows or index in taken_projects:
                continue
            taken_rows.add(row_index)
            taken_projects.add(index)
            projects[index].append(rows[row_index])
        for index in list(active):
            active[index] = 0 if index in taken_projects else active[index] + 1
            if active[index] > 3:
                del active[index]
        for row_index, row in enumerate(rows):
            if row_index not in taken_rows:
                projects.append([row])
                active[len(projects) - 1] = 0
    return projects


# ------------------------------------------------------------- one project

CHANGE_FIELDS = [
    ("status", "Status"), ("mw_in", "MW injected"), ("mw_out", "MW withdrawn"), ("deadline", "Connection date"),
    ("agreement", "Agreement date"), ("conditions", "Conditions date"), ("applicant", "Applicant"),
    ("type", "Installation type"), ("station_text", "Connection point"), ("name", "Name"),
]


def describe(rows, latest_asof, stations):
    first, last = rows[0], rows[-1]
    seen = [row["asof"] for row in rows]
    # Fields PSE stopped publishing (or never did) are taken from the latest row that had them.
    def latest(field):
        for row in reversed(rows):
            if row.get(field):
                return row[field]
        return None
    words = " ".join(filter(None, [latest("name"), latest("applicant")]))
    tech = technology(last["type"], last["installed"], words)
    if tech == "conventional":
        tech = next((technology(row["type"], row["installed"], words) for row in reversed(rows)
                     if technology(row["type"], row["installed"], words) != "conventional"), "gas" if "gaz" in fold(words) else "coal")

    in_latest = last["asof"] == latest_asof
    status = last["status"]
    phase = STATUS_PHASE.get(status, "conditions")
    if status == "agreement" and last["deadline"] and last["deadline"] < last["asof"]:
        phase = "operating"
    left = None
    if not in_latest:
        # Off the list without an exit PSE recorded: after its connection date it most likely
        # finished connecting; before, the conditions or the agreement were dropped.
        if phase in ("refused", "withdrawn"):
            left = {"asof": next_asof(last["asof"]), "reason": status}
        elif last["agreement"] and last["deadline"] and last["deadline"] <= next_asof(last["asof"]):
            phase, left = "operating", {"asof": next_asof(last["asof"]), "reason": "after_connection_date"}
        else:
            phase, left = "withdrawn", {"asof": next_asof(last["asof"]), "reason": "removed"}

    milestones = {}
    def mark(key, date, asof):
        if key not in milestones:
            milestones[key] = {"date": date, "seen": asof}
        elif date and not milestones[key].get("date"):
            milestones[key]["date"] = date
    for row in rows:
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
    if phase == "operating":
        mark("connected", latest("deadline"), left["asof"] if left else last["asof"])
    if left and left["reason"] == "removed":
        mark("removed", None, left["asof"])

    # The contractual connection date over time (gridstatus' "Projected COD timeline").
    cod = []
    for row in rows:
        if row["deadline"] and (not cod or cod[-1]["date"] != row["deadline"]):
            cod.append({"date": row["deadline"], "seen": row["asof"]})

    changes = []
    for before, after in zip(rows, rows[1:]):
        diffs = []
        for field, label in CHANGE_FIELDS:
            a, b = before.get(field), after.get(field)
            if field in ("mw_in", "mw_out"):
                if a is None or b is None or mw_close(a, b) and abs(a - b) < 0.01:
                    continue
            if field in ("applicant", "name", "station_text", "type") and (not a or not b or fold(a) == fold(b)):
                continue
            # Footnote letters come and go ("SE2C" -> "SE2D"); PSE retyped "OZE"/"KJW" in May 2023.
            if field == "station_text" and before["stations"] == after["stations"]:
                continue
            if field == "type" and codes_of(a) & {"OZE", "KJW"}:
                continue
            if field == "status" and before["doc"] != after["doc"] and {a, b} <= {"conditions", "agreement"} and a == b:
                continue
            if a != b and (a or b):
                diffs.append({"field": field, "label": label, "from": a, "to": b})
        if diffs:
            changes.append({"asof": after["asof"], "diffs": diffs})

    placed = [name for name in last["stations"] if stations.place(name)[0] is not None]
    lat, lon = stations.place(placed[0]) if placed else (None, None)
    region = stations.stations[placed[0]].get("region") if placed else None
    return {
        "name": latest("name") or "", "applicant": latest("applicant") or "", "seat": latest("seat") or "",
        "operator": last["operator"] or "PSE S.A.", "pse_id": latest("id") or "",
        "type": last["type"], "technology": tech, "installed": last["installed"],
        "mw_in": last["mw_in"], "mw_out": last["mw_out"], "mw": mw_of(last), "change_request": last["change"],
        "station": last["station_text"], "stations": last["stations"], "kv": last["kv"] or latest("kv") or "",
        "lat": lat, "lon": lon, "region": region, "status": status, "phase": phase, "left": left,
        "applied": latest("applied"), "advance": latest("advance"), "conditions": latest("conditions"),
        "agreement": latest("agreement"), "deadline": last["deadline"] or latest("deadline"),
        "refused": latest("refused"), "refusal_reason": latest("refusal_reason") or "",
        "expired": latest("expired"), "terminated": latest("terminated"),
        "first_seen": seen[0], "last_seen": seen[-1], "months_listed": len(seen),
        "milestones": milestones, "cod": cod, "changes": changes,
    }


ASOFS = []


def next_asof(asof):
    later = [value for value in ASOFS if value > asof]
    return later[0] if later else asof


# ------------------------------------------------------------- early stages

def early_stage_matches(projects, stations):
    """Find each project in the lists of applications awaiting verification and confirmed complete.

    Those lists give type, station, MW and a date, no names.  A project takes
    the row at its station with its MW (within 1 MW / 2 %) and a compatible
    type, inside a window that fits its own dates: completeness within the 13
    months before its conditions (PSE has 150 days to issue them, after
    completeness), the received date within the 18 months before that, and
    neither before the application date it states (since July 2026).  The
    latest fitting row wins; a row serves one project only, earliest
    conditions first.  "Received" is PSE's "Data wpłynięcia wniosku/
    uzupełnienia": the application or its last supplement.
    """
    def shift(date, months):
        value = datetime.date.fromisoformat(date)
        month = value.month - 1 + months
        year = value.year + month // 12
        return datetime.date(year, month % 12 + 1, min(value.day, 28)).isoformat()

    def candidates(doc):
        rows = {}
        for snapshot in load(doc):
            for row in snapshot["rows"]:
                if not row.get("date"):
                    continue
                names = tuple(stations.resolve(row["station"]))
                ident = (names, row["type"], round(mw_of(row) or 0, 1), row["date"])
                if ident not in rows:
                    rows[ident] = dict(row, stations=names, first_seen=snapshot["asof"], last_seen=snapshot["asof"])
                rows[ident]["last_seen"] = snapshot["asof"]
        pool = collections.defaultdict(list)
        for row in rows.values():
            for name in row["stations"][:1]:
                pool[name].append(row)
        return pool

    def pick(pool, project, low, high, used):
        best = None
        for name in project["stations"][:1]:
            for row in pool[name]:
                if id(row) in used or not (low <= row["date"] <= high):
                    continue
                if not (mw_close(row.get("mw_in"), project["mw_in"]) or mw_close(mw_of(row), project["mw"])):
                    continue
                if not tech_compatible({"type": row["type"]}, {"type": project["type"]}):
                    continue
                if best is None or row["date"] > best["date"]:
                    best = row
        if best:
            used.add(id(best))
        return best

    complete_pool, received_pool = candidates("kompletne"), candidates("weryfikacja")
    used = set()
    for project in sorted(projects, key=lambda item: item["conditions"] or item["refused"] or "9999"):
        decided = project["conditions"] or project["refused"]
        floor = shift(project["applied"], -1) if project["applied"] else "2000-01-01"
        if decided:
            complete = pick(complete_pool, project, max(floor, shift(decided, -13)), decided, used)
        elif project["phase"] == "study":
            complete = pick(complete_pool, project, max(floor, shift(project["first_seen"], -13)), project["last_seen"], used)
        else:
            complete = None
        if complete:
            project["milestones"]["complete"] = {"date": complete["date"], "seen": complete["first_seen"], "matched": True}
        ceiling = complete["date"] if complete else (decided or project["last_seen"])
        received = pick(received_pool, project, max(floor, shift(ceiling, -18)), ceiling, used)
        if received:
            project["milestones"]["received"] = {"date": received["date"], "seen": received["first_seen"], "matched": True}


# ------------------------------------------------------------- history

def history(projects, application_lists):
    """MW and counts by phase and technology at every month end."""
    months = sorted(ASOFS)
    out = []
    for asof in months:
        cells = collections.defaultdict(lambda: [0, 0.0])
        for project in projects:
            phase = project_phase_at(project, asof)
            if phase:
                cell = cells[f"{project['technology']}|{phase}"]
                cell[0] += 1
                cell[1] += project_mw_at(project, asof) or 0
        out.append({"asof": asof, "cells": {key: [count, round(mw, 1)] for key, (count, mw) in cells.items()}})
    for asof, doc, rows in application_lists:
        # The application lists are sometimes dated a day earlier (30 Aug vs 31 Aug): same month, same point.
        entry = next((item for item in out if item["asof"][:7] == asof[:7]), None)
        if entry is None:
            entry = {"asof": asof, "cells": {}}
            out.append(entry)
        phase = "application" if doc == "weryfikacja" else "study"
        entry.setdefault("lists", {})[phase] = {}
        for row in rows:
            tech = technology(row["type"])
            cell = entry["lists"][phase].setdefault(tech, [0, 0.0])
            cell[0] += 1
            cell[1] = round(cell[1] + (mw_of(row) or 0), 1)
    out.sort(key=lambda item: item["asof"])
    return out


def project_phase_at(project, asof):
    for entry in project["_timeline"]:
        if entry["asof"] == asof:
            return entry["phase"]
    return None


def project_mw_at(project, asof):
    for entry in project["_timeline"]:
        if entry["asof"] == asof:
            return entry["mw"]
    return None


def refusals_by_quarter():
    lists = load("odmowy")
    if not lists:
        return []
    latest = lists[-1]
    quarters = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0]))
    for row in latest["rows"]:
        if row.get("quarter"):
            cell = quarters[row["quarter"]][technology(row["type"])]
            cell[0] += 1
            cell[1] = round(cell[1] + (mw_of(row) or 0), 1)
    return [{"quarter": quarter, "cells": dict(cells)} for quarter, cells in sorted(quarters.items())], latest["asof"]


# ------------------------------------------------------------- main

def main():
    stations = Stations()
    snapshots = []
    for doc in ("wykaz", "art7"):
        for snapshot in load(doc):
            rows = [normalise(row, doc, snapshot["asof"], stations) for row in snapshot["rows"]]
            snapshots.append((snapshot["asof"], doc, rows, snapshot["source"]))
    snapshots.sort(key=lambda item: item[0])
    unknown, stations.unknown = sorted(stations.unknown), set()
    ASOFS[:] = [asof for asof, *_ in snapshots]
    latest_asof = ASOFS[-1]
    chains = link([(asof, doc, rows) for asof, doc, rows, _ in snapshots])

    projects = []
    for chain in chains:
        project = describe(chain, latest_asof, stations)
        project["_timeline"] = [{"asof": row["asof"], "mw": mw_of(row),
                                 "phase": ("operating" if row["status"] == "agreement" and row["deadline"] and row["deadline"] < row["asof"]
                                           else STATUS_PHASE.get(row["status"], "conditions"))} for row in chain]
        projects.append(project)
    early_stage_matches(projects, stations)

    application_lists = [(snapshot["asof"], doc, snapshot["rows"]) for doc in ("weryfikacja", "kompletne") for snapshot in load(doc)]
    monthly = history(projects, application_lists)
    refusals, refusals_asof = refusals_by_quarter() or ([], None)

    projects.sort(key=lambda item: (item["phase"] in ("withdrawn", "refused"), -(item["mw"] or 0)))
    phase_index = {key: index for index, (key, _) in enumerate(PHASES)}
    month_index = {asof: index for index, asof in enumerate(ASOFS)}
    for number, project in enumerate(projects, start=1):
        project["id"] = number
        # The map's month slider: [first month, last month, phase, MW] runs, months as indexes into "months".
        runs = []
        for entry in project.pop("_timeline"):
            index, phase, mw = month_index[entry["asof"]], phase_index[entry["phase"]], round(entry["mw"] or 0, 1)
            if runs and runs[-1][1] == index - 1 and runs[-1][2] == phase and runs[-1][3] == mw:
                runs[-1][1] = index
            else:
                runs.append([index, index, phase, mw])
        project["runs"] = runs
    sources = [{"asof": asof, "doc": doc, "rows": len(rows), "url": source["url"], "published": source.get("published")}
               for asof, doc, rows, source in snapshots]
    used = {name for project in projects for name in project["stations"]}
    queue = {
        "updated": today(), "asof": latest_asof, "months": ASOFS,
        "technologies": [{"key": key, "label": label} for key, label in TECHNOLOGIES],
        "phases": [{"key": key, "label": label} for key, label in PHASES],
        "stations": {name: {key: value for key, value in stations.stations[name].items() if key in ("lat", "lon", "code", "kv", "planned", "note", "osm_id", "region")}
                     for name in sorted(used) if name in stations.stations},
        "sources": sources, "projects": projects,
    }
    json.dump(queue, open(os.path.join(OUT, "queue.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"updated": today(), "monthly": monthly, "refusals": refusals, "refusals_asof": refusals_asof},
              open(os.path.join(OUT, "history.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    counts = collections.Counter(project["phase"] for project in projects)
    print(f"{len(projects)} projects from {len(snapshots)} monthly lists ({ASOFS[0]} to {latest_asof}); phases {dict(counts)}")
    if unknown:
        print("Connection points of listed projects not in etl/stations.json:", unknown)
    return queue


if __name__ == "__main__":
    main()
