"""
Evidence that a queue project is running, from registers of units in operation:

  storage registers  the DSOs' registers of energy storage (art. 43g), every
                     row a storage unit in operation with its start date
                     (data/storage_registers.json, written by etl/dso.py);
  PSE's unit output  gen-jw, the metered output of the centrally dispatched
                     units: the month a unit first produced, sampled on the
                     15th of every month since PSE's API starts (June 2024)
                     (data/running/jwcd.json, refreshed by `python etl/running.py`).

A project is matched only when the place, the kind of plant and the size
agree, and only when the unit started after the project's agreement (an
older unit at the same station is not the project).  A match makes the
project "operational" from the month the unit started.

The distribution lists carry their own evidence (dates of connection and of
first delivery; status "obiekt przyłączony"); see dso_build.py.
"""

import collections
import datetime
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stations import fold  # noqa: E402

ROOT = os.path.dirname(HERE)
JWCD = os.path.join(ROOT, "data", "running", "jwcd.json")
REGISTERS = os.path.join(ROOT, "data", "storage_registers.json")
PLANTS = os.path.join(ROOT, "output", "plants.json")
API = "https://api.raporty.pse.pl/api/gen-jw"
FIRST_MONTH = datetime.date(2024, 6, 15)
FUEL_TECH = {"Lignite": {"coal"}, "Hard coal": {"coal"}, "Hard coal + biomass": {"coal", "other"}, "Natural gas": {"gas"},
             "Offshore wind": {"offshore"}, "Onshore wind": {"wind", "hybrid"}, "Solar PV": {"solar", "hybrid"},
             "Hydro · pumped storage": {"other", "storage"}, "Biomass": {"other"}}


# ------------------------------------------------------------- PSE unit output

def fetch_jwcd():
    """Each JWCD unit's first month with output (sampled mid-month), kept and extended in data/running/jwcd.json."""
    data = json.load(open(JWCD, encoding="utf-8")) if os.path.exists(JWCD) else {"sampled": [], "units": {}}
    before = len(data["sampled"])
    if "listed_first" not in data:
        # Every plant PSE listed on the first day, producing or not: those are not new plants.
        url = f"{API}?$filter=business_date eq '{FIRST_MONTH.isoformat()}'&$select=power_plant&$first=12000"
        data["listed_first"] = sorted({row["power_plant"] for row in requests.get(url, timeout=90).json()["value"]})
    day, today = FIRST_MONTH, datetime.date.today()
    while day < today:
        key = day.isoformat()
        if key not in data["sampled"]:
            url = f"{API}?$filter=business_date eq '{key}'&$select=resource_code,power_plant,value,operating_mode_eng&$first=12000"
            for attempt in range(4):
                try:
                    rows = requests.get(url, timeout=90).json()["value"]
                    break
                except Exception:  # PSE often fails a cold query once
                    time.sleep(5)
            else:
                print(f"gen-jw {key}: failed")
                rows = None
            if rows is not None:
                for row in rows:
                    if row.get("operating_mode_eng") != "Generation" or not row.get("value"):
                        continue
                    unit = data["units"].setdefault(row["resource_code"], {"plant": row["power_plant"], "first_output": key})
                    unit["first_output"] = min(unit["first_output"], key)
                data["sampled"].append(key)
                time.sleep(1)
        day = (day.replace(day=1) + datetime.timedelta(days=32)).replace(day=15)
    data["sampled"].sort()
    os.makedirs(os.path.dirname(JWCD), exist_ok=True)
    json.dump(data, open(JWCD, "w", encoding="utf-8"), ensure_ascii=False, indent=0, sort_keys=True)
    print(f"{len(data['units'])} JWCD units, sampled {data['sampled'][0]} to {data['sampled'][-1]}")
    return len(data["sampled"]) - before


def jwcd_plants():
    """Plant -> first month any of its units produced, its fuel and town; plants already producing in June 2024 are older."""
    if not os.path.exists(JWCD):
        return []
    data = json.load(open(JWCD, encoding="utf-8"))
    labels = json.load(open(PLANTS, encoding="utf-8"))["plants"] if os.path.exists(PLANTS) else {}
    first = data["sampled"][0] if data["sampled"] else None
    by_plant = collections.defaultdict(list)
    for code, unit in data["units"].items():
        by_plant[unit["plant"]].append((unit["first_output"], code))
    out = []
    for plant, units in by_plant.items():
        start = min(date for date, _ in units)
        if start == first or plant in data.get("listed_first", []):
            continue  # listed when PSE's API starts: built before, not a queue project of the period
        label = labels.get(plant, {})
        town = (label.get("location") or "").split("·")[0].strip()
        fuel = label.get("source") or ("Onshore wind" if plant.startswith("FW ") else "Offshore wind" if plant.startswith("MFW ") else None)
        out.append({"plant": plant, "since": start, "units": sorted(code for _, code in units), "fuel": fuel,
                    "names": {fold(re.sub(r"^(EC|MFW|FW)\s+", "", plant)), fold(plant), fold(town)} - {""}})
    return out


# ------------------------------------------------------------- matching

def project_names(project):
    names = {fold(name) for name in project.get("stations") or []}
    if project.get("station"):
        names.add(fold(re.split(r"[,/(]", project["station"])[0]))
    return {name for name in names if len(name) >= 3}


def candidate(project):
    """Projects a register can confirm: an agreement (or past its connection date), or dropped after it."""
    if project["phase"] in ("agreement", "operating", "operational"):
        return True
    return bool(project.get("left") and project["left"].get("reason") == "after_connection_date")


def match_storage(projects, registers):
    found = []
    used = set()
    pool = [p for p in projects if p["technology"] in ("storage", "hybrid", "distribution") and candidate(p)]
    for register in registers:
        operator = register["operator"]
        for index, unit in enumerate(register["rows"]):
            if unit["mw"] < 1:
                continue  # behind-the-meter units of factories and depots, not queue projects
            text = fold(" ".join([unit["place"]] + unit["address"]))
            best = None
            for project in pool:
                if project.get("running") or id(project) in used:
                    continue
                if project.get("grid") not in (None, "pse", operator):
                    continue
                names = project_names(project)
                if not any(re.search(rf"\b{re.escape(name)}\b", text) for name in names):
                    continue
                size = project["installed"].get("storage_out") or project["mw"] or 0
                if not size or abs(size - unit["mw"]) > max(1.0, 0.35 * max(size, unit["mw"])):
                    continue
                since = unit["started"] or unit["registered"]
                if project.get("agreement") and since and since < project["agreement"]:
                    continue
                gap = abs(size - unit["mw"])
                if best is None or gap < best[0]:
                    best = (gap, project, since)
            if best:
                _, project, since = best
                used.add(id(project))
                project["running"] = {"source": f"{register['operator_name']} storage register", "unit": unit["owner"],
                                      "place": unit["place"], "mw": unit["mw"], "mwh": unit["mwh"], "since": since}
                found.append(project)
    return found


def match_jwcd(projects, plants):
    found = []
    for plant in plants:
        techs = FUEL_TECH.get(plant["fuel"], set())
        def named(project):
            # A station or project name inside the plant's name or town, as whole words ("Potęgowo" in "FW Potęgowo Wschód").
            own = project_names(project) | ({fold(project["name"])} if project.get("name") else set())
            return any(re.search(rf"\b{re.escape(a)}\b", b) or re.search(rf"\b{re.escape(b)}\b", a)
                       for a in own for b in plant["names"] if len(a) >= 4 and len(b) >= 4)
        hits = [p for p in projects if candidate(p) and not p.get("running") and (p.get("grid") in (None, "pse"))
                and (not techs or p["technology"] in techs) and named(p)
                and (not p.get("agreement") or p["agreement"] <= plant["since"])]
        for project in hits:
            project["running"] = {"source": "PSE metered unit output (gen-jw)", "unit": plant["plant"],
                                  "units": plant["units"], "since": plant["since"]}
            found.append(project)
    return found


def apply(projects, month_index):
    """Mark matched projects operational from the month their unit started."""
    registers = []
    if os.path.exists(REGISTERS):
        names = {"tauron": "TAURON Dystrybucja", "enea": "ENEA Operator"}
        for register in json.load(open(REGISTERS, encoding="utf-8"))["registers"]:
            register["operator_name"] = names.get(register["operator"], register["operator"])
            registers.append(register)
    found = match_storage(projects, registers) + match_jwcd(projects, jwcd_plants())
    for project in found:
        since = project["running"].get("since") or project.get("last_seen")
        project["phase"] = "operational"
        if project.get("left"):
            project["left"]["reason"] = "connected"
        project.setdefault("milestones", {}).setdefault("connected", {"date": since, "seen": since})
        project["milestones"]["connected"]["date"] = project["milestones"]["connected"].get("date") or since
        for entry in project["_timeline"]:
            if entry["asof"][:7] >= (since or "9999")[:7] and entry["phase"] in ("agreement", "operating", "conditions"):
                entry["phase"] = "operational"
    return found


if __name__ == "__main__":
    fetch_jwcd()
