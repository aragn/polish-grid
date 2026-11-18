"""
Link capacity-market units (data/capacity_market.json) to connection-queue projects.

URE's lists give a unit's code, its name only in the 2028 and 2029 notices, its type
(new, existing, refurbished, DSR, foreign) and the provider (the company that holds
the contract).  A unit is linked to a project when the technology fits and

  - the unit is new or refurbished (existing units were running before they bid);
  - a place word of the unit's name ("Zabrze 2 BESS" -> Zabrze) is the project's
    station or appears in its name, or the provider is the project's applicant (and
    has at most three projects: an SPV, not a utility); and
  - the size fits: the obligation divided by the derating factor (the most net
    capacity it can stand for) is between a third and one and a half times the
    project's MW (storage: its discharge MW).

Only one project per unit: the best score, then the closest size.  Foreign units and
demand response are not queue projects and are skipped.  The match is this site's
and can be wrong; the card says how it was made.
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
CM = os.path.join(ROOT, "data", "capacity_market.json")
TECH_FITS = {
    "storage": {"storage", "hybrid", "distribution"}, "pumped_hydro": {"other"}, "wind": {"wind", "hybrid"},
    "solar": {"solar", "hybrid"}, "hydro": {"other"}, "ccgt": {"gas"}, "gas_engine": {"gas", "other"},
    "thermal": {"coal", "gas", "other"}, "nuclear": {"nuclear"},
}
GENERIC = {"magazyn", "magazynu", "energii", "energia", "elektrycznej", "bess", "storage", "blok", "bloku", "elektrownia", "elektrowni",
           "farma", "wiatrowa", "fotowoltaiczna", "park", "solar", "battery", "jednostka", "mocy", "ii", "iii", "iv", "sp", "z", "o",
           "power", "polska", "poland", "projekt", "green", "energy", "plant", "unit", "station", "stacja", "pole",
           "stare", "stary", "stara", "nowe", "nowy", "nowa", "wielkie", "wielki", "wielka", "mala", "maly", "male", "gorna", "gorny",
           "dolna", "dolny", "polnoc", "poludnie", "wschod", "zachod", "srodek", "centrum", "osiedle", "kolonia", "tesla", "mega"}
LEGAL = r"\b(sp|spolka|z|o|oo|s|a|sa|sk|komandytowa|ograniczona|odpowiedzialnoscia|akcyjna|spzoo|zoo)\b"


def company(name):
    return re.sub(r"\s+", " ", re.sub(LEGAL, " ", fold(name or ""))).strip()


def place_words(name):
    return {w for w in fold(name or "").split() if len(w) >= 4 and w not in GENERIC and not w.isdigit()}


def project_size(project):
    installed = project.get("installed") or {}
    return installed.get("storage_out") or project.get("mw") or 0


def apply(projects):
    if not os.path.exists(CM):
        return []
    data = json.load(open(CM, encoding="utf-8"))
    auctions = {a["id"]: a for a in data["auctions"]}
    by_unit = collections.defaultdict(list)
    for contract in data["contracts"]:
        by_unit[contract["code"]].append(contract)
    applicants = collections.defaultdict(list)
    for project in projects:
        if project.get("applicant"):
            applicants[company(project["applicant"])].append(project)
    texts = {id(p): " ".join([fold(p.get("name") or ""), " ".join(fold(s) for s in p.get("stations") or []), fold((p.get("station") or "").split("\n")[0])])
             for p in projects}
    linked = []
    taken = set()
    for unit in data["units"]:
        tech = unit["technology"]
        if tech in ("foreign", "dsr") or tech not in TECH_FITS:
            continue
        # Existing units were running before they bid: a queue project is new capacity (or a refurbishment).
        if not any(word in t.lower() for t in unit["types"] for word in ("nowa", "modernizowana", "niepotwierdzona")):
            continue
        contracts = by_unit[unit["code"]]
        reach = max((c["derated_from"] or 0) for c in contracts) if contracts else 0
        words = place_words(unit["name"])
        provider = company(unit["provider"])
        candidates = []
        pool = set()
        # The provider as applicant counts only for a company with few projects (one SPV per plant), not a utility.
        if provider and len(applicants.get(provider, [])) <= 3:
            pool.update(id(p) for p in applicants.get(provider, []))
        # A provider named after a distribution operator builds in that operator's area (TAURON BME4: Kraków-Dąbie).
        brand = next((grid for grid in ("tauron", "enea", "pge", "energa") if re.search(rf"\b{grid}\b", fold(unit["provider"]))), None)
        for project in projects:
            if project["technology"] not in TECH_FITS[tech] or id(project) in taken:
                continue
            if brand and project.get("grid") not in (None, "pse", brand):
                continue
            # Every place word of the unit must be there ("Ruda Śląska" is not "Nowa Ruda").
            by_name = bool(words) and all(re.search(rf"\b{re.escape(w)}\b", texts[id(project)]) for w in words)
            by_provider = id(project) in pool
            if not by_name and not by_provider:
                continue
            size = project_size(project)
            if not size or (reach and not (size / 3 <= reach <= size * 1.5)):
                continue   # the size must be known and fit
            score = 3 * by_name + 3 * by_provider - (2 if project["phase"] in ("withdrawn", "refused") else 0)
            candidates.append((score, abs(size - reach) if reach else 1e9, project, by_name, by_provider))
        if not candidates:
            continue
        candidates.sort(key=lambda c: (-c[0], c[1]))
        best = candidates[0]
        # A provider-only match among several projects of that company needs a clear size winner.
        if not best[3] and len(candidates) > 1 and candidates[1][0] == best[0] and abs(candidates[1][1] - best[1]) < 0.15 * max(1, reach):
            continue
        score, _, project, by_name, by_provider = best
        taken.add(id(project))
        project.setdefault("cm", []).append({
            "code": unit["code"], "name": unit["name"], "provider": unit["provider"], "technology": tech, "new": unit["new"],
            "how": " and ".join(filter(None, ["unit name matches the station or project" if by_name else "", "provider is the applicant" if by_provider else ""])),
            "contracts": [{"auction": c["auction"], "kind": auctions[c["auction"]]["kind"], "from": c["from"], "to": c["to"], "quarter": c["quarter"],
                           "mw": c["mw"], "price": c["price"], "kwd": c["kwd"], "derated_from": c["derated_from"]}
                          for c in sorted(contracts, key=lambda c: (c["from"], c["quarter"] or 0))],
        })
        linked.append((unit["code"], project))
    return linked
