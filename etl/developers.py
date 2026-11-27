"""
Each applicant's track record, for the project cards of the connection-queue page:

    python etl/developers.py

From the operators' lists (data/queue.json, data/queue_dso.json): how many projects the applicant has had in any list
since 2020, how many were connected or are running, how many were withdrawn, refused, expired, terminated or lapsed
(only when a list says so: a project that just disappears is not counted either way, since most leave when connected),
and how many are still in the queue.  From the Energy Regulatory Office's public API (api.ure.gov.pl, read when this
runs; reuse with the source, date and query named): electricity generation and storage licences (ConcessionOtherFuel,
types WEE and MEE) and installations in the register of small installations (MIOZERegistry, all types).

Names are matched after cutting any address the list appended (from the first comma, street marker or postal code)
and dropping quotes, punctuation and the legal form.  Developers apply through numbered project companies
("Greenvolt Energy 16"), so each record also has a group, the name without its trailing number or letter.  URE's
registers are matched only for names that carry a legal form, so private individuals are never matched.  No tax
number, address or person's name is kept: data/developers.json holds counts and dates, per normalised name ("names")
and per group ("groups"), for the applicants of projects now in a list.
"""
import collections
import datetime
import json
import os
import re
import sys
import time
import unicodedata

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
API = "https://api.ure.gov.pl/api/"
UA = {"User-Agent": "polish-grid connection queue (github.com/aragn/polish-grid)"}
ADDRESS = re.compile(r",|(?:\s|\.)(?:u\s?l|a\s?l)\.\s?|\s(?:aleja|ulica|os\.|pl\.)\s|\s\d\s?\d\s*-\s*\d\s?\d\s?\d\b", re.I)
# Groups made only of these words would lump unrelated companies together ("Elektrownia PV 12").
GENERIC = {"elektrownia", "elektrownie", "pv", "farma", "farmy", "fotowoltaiczna", "fotowoltaika", "solar", "solarna", "energy", "energia", "park", "parki",
           "wiatrowa", "wind", "magazyn", "magazyny", "energii", "projekt", "projekty", "green", "sun", "power", "farm", "fw", "oze", "invest", "inwestycje"}
LEGAL = re.compile(r"\b(sp(ó|o)?ł?ka|sp\.?|z\s*o\.?\s*o\.?|s\.?\s*a\.?|spółka\s+akcyjna|komandytowa|komandytowo|akcyjna|jawna|"
                   r"partnerska|ograniczon\w*|odpowiedzialno\w*|s\.?\s*k\.?\s*a\.?|sp\.?\s*k\.?|sp\.?\s*j\.?|p\.?\s*s\.?\s*a\.?|"
                   r"spó[łl]dzielnia|gmbh|ltd|limited|b\.?\s*v\.?|a\.?\s*s\.?)\b", re.I)
HAS_LEGAL = re.compile(r"sp\.?\s*z\s*o\.?\s*o|spółka|s\.\s*a\.|\bS\.?A\.?$|\bsp\.\s*k\b|\bsp\.\s*j\b|p\.\s*s\.\s*a|sp[oó]łdzielnia|gmbh|\bltd\b|\bb\.v\.", re.I)
BUILT_PHASES = {"operational", "operating"}
DROPPED = {"withdrawn", "refused", "expired", "terminated", "lapsed"}
LIVE = {"application", "study", "conditions", "agreement"}


def norm(name):
    name = ADDRESS.split(name or "", maxsplit=1)[0]
    s = unicodedata.normalize("NFKD", name.lower().replace("ł", "l"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\"'„”«»]", " ", s)
    s = LEGAL.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"(^|\s)\d{2}\s\d{3}\b.*$", "", s).strip()     # a postal code left after the cut: drop it and what follows


def group(n):
    """The developer behind numbered project companies: 'greenvolt energy 16' -> 'greenvolt energy'."""
    g = re.sub(r"(\s+(\d+|[ivxlc]+|[a-z]))+$", "", n).strip()
    return g if len(g) >= 4 and not set(g.split()) <= GENERIC else n


def get(resource, params=None):
    for attempt in range(4):
        try:
            r = requests.get(API + resource, params=params, headers=UA, timeout=180)
            r.raise_for_status()
            time.sleep(4)                       # URE's limit: 3 requests per 10 s, 25 per 2 minutes
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))


def new_record():
    return {"projects": 0, "built": 0, "dropped": 0, "in_queue": 0, "mw": 0.0, "first": None, "operators": set(), "companies": set()}


def add(r, p, n):
    r["projects"] += 1
    r["mw"] += p.get("mw") or 0
    r["operators"].add(p.get("grid") or "pse")
    r["companies"].add(n)
    first = p.get("first_seen")
    if first and (not r["first"] or first < r["first"]):
        r["first"] = first
    connected = (p.get("left") or {}).get("reason") == "connected" or (p.get("milestones") or {}).get("connected")
    if p.get("phase") in BUILT_PHASES or connected or p.get("status") == "connected":
        r["built"] += 1
    elif p.get("status") in DROPPED or p.get("lapsed") or p.get("phase") == "refused":
        r["dropped"] += 1
    elif not p.get("left") and p.get("phase") in LIVE:
        r["in_queue"] += 1


def main():
    projects = json.load(open(os.path.join(DATA, "queue.json"), encoding="utf-8"))["projects"]
    projects += json.load(open(os.path.join(DATA, "queue_dso.json"), encoding="utf-8"))["projects"]
    names, groups = collections.defaultdict(new_record), collections.defaultdict(new_record)
    wanted = set()
    for p in projects:
        n = norm(p.get("applicant"))
        if not n:
            continue
        add(names[n], p, n)
        add(groups[group(n)], p, n)
        if not p.get("left") and p.get("phase") in LIVE:
            wanted.add(n)

    read = datetime.date.today().isoformat()
    licences = collections.defaultdict(list)
    for c in get("ConcessionOtherFuel"):
        if c.get("rodzajKoncesji") in ("WEE", "MEE") and HAS_LEGAL.search(c.get("nazwa") or ""):
            licences[norm(c["nazwa"])].append((c["rodzajKoncesji"], c.get("dataOd") or c.get("dataWydania")))
    small = collections.defaultdict(lambda: [0, 0.0, None])
    for i in get("MIOZERegistry"):
        if not HAS_LEGAL.search(i.get("nazwa") or ""):
            continue
        s = small[norm(i["nazwa"])]
        s[0] += 1
        s[1] += float((i.get("mocEEInstalacji") or "0").replace(",", "."))
        d = i.get("dataRozpoczeciaDzialalnosci")
        if d and (not s[2] or d < s[2]):
            s[2] = d

    def ure(keys):
        lic = [x for k in keys for x in licences.get(k, [])]
        sm = [small[k] for k in keys if k in small]
        return {"generation_licences": sum(1 for t, _ in lic if t == "WEE"), "storage_licences": sum(1 for t, _ in lic if t == "MEE"),
                "licence_since": min((d for _, d in lic if d), default=None),
                "small_installations": sum(s[0] for s in sm), "small_mw": round(sum(s[1] for s in sm), 2),
                "small_since": min((s[2] for s in sm if s[2]), default=None)}

    def record(r, keys):
        return {"projects": r["projects"], "built": r["built"], "dropped": r["dropped"], "in_queue": r["in_queue"], "mw": round(r["mw"], 1),
                "first": r["first"], "operators": sorted(r["operators"]), **ure(keys)}

    out_names = {n: record(names[n], [n]) for n in sorted(wanted)}
    out_groups = {}
    for n in wanted:
        g = group(n)
        if g != n and g not in out_groups and len(groups[g]["companies"]) > 1:
            out_groups[g] = {**record(groups[g], sorted(groups[g]["companies"])), "companies": len(groups[g]["companies"])}
    for n in out_names:
        if group(n) in out_groups:
            out_names[n]["group"] = group(n)
    json.dump({"built": read,
               "sources": {"queues": "operators' connection-queue lists (data/queue.json, data/queue_dso.json)",
                           "ure": f"Urząd Regulacji Energetyki, api.ure.gov.pl, read {read}: ConcessionOtherFuel (types WEE, MEE) and MIOZERegistry "
                                  "(all records); matched by company name after dropping the legal form; only counts and dates kept"},
               "names": out_names, "groups": out_groups,
               # the applicant as each list prints it -> its name key, so the page needs no matching of its own
               "applicants": {a: norm(a) for a in sorted({p["applicant"] for p in projects if p.get("applicant") and norm(p["applicant"]) in wanted})}},
              open(os.path.join(DATA, "developers.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    with_ure = sum(1 for v in out_names.values() if v["generation_licences"] or v["small_installations"])
    print(f"Developers: {len(out_names)} applicants with a project in a list now, {len(out_groups)} groups of numbered companies; "
          f"{sum(1 for v in out_names.values() if v['built'])} with a project connected or running; {with_ure} found in URE's registers")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
