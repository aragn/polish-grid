"""
Building permits on and near each land plot of the Land tab, from GUNB's register (RWDZ):

    python etl/land_permits.py              # if the last read is older than REFRESH_DAYS
    python etl/land_permits.py --force

Source: the Chief Building Inspectorate's public register of applications, decisions and notifications (Rejestr
Wniosków, Decyzji i Zgłoszeń), downloaded per province from wyszukiwarka.gunb.gov.pl/pobranie.html: building permit
decisions since 1 January 2016, one row per decision and parcel, parcels given by TERYT unit, precinct and number,
as in our plot ids.  Only the provinces with plots are read.

What is kept, per decision: its date, a type worked out from the free text (solar, battery, wind, a house, other),
whether it builds something new, and the MW when the text gives it.  Never the investor or the designer (personal
data), nor the free text itself.

Adds to each plot `gp`, its own decisions as [date, type, new 1/0, MW or null], newest first; and `gn`, the distance
in metres to the nearest solar, battery and wind decision on another plot of data/land within NEAR_M, as
[solar, battery, wind] (null when none; only decisions on parcels of 1 ha or more within 3 km of a substation have
an outline here).  data/land/permits.json counts the decisions by type in each precinct of the plots, since 2016
and since PRECINCT_RECENT.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import time
import zipfile

import requests
import shapely

from land_budget import capped
from land_protected import plot_shape, station_files

HERE = os.path.dirname(os.path.abspath(__file__))
LAND = os.path.join(os.path.dirname(HERE), "data", "land")
URL = "https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/wynik_{name}.zip"
UA = {"User-Agent": "polish-grid land screening (github.com/aragn/polish-grid)"}
PROVINCES = {"02": "dolnoslaskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie", "10": "lodzkie", "12": "malopolskie",
             "14": "mazowieckie", "16": "opolskie", "18": "podkarpackie", "20": "podlaskie", "22": "pomorskie", "24": "slaskie",
             "26": "swietokrzyskie", "28": "warminsko-mazurskie", "30": "wielkopolskie", "32": "zachodniopomorskie"}
REFRESH_DAYS = 28
NEAR_M = 3000
PRECINCT_RECENT = "2022-01-01"
TYPES = ("solar", "battery", "wind")
# Only these columns are read; the investor and designer columns never leave the file.
COLUMNS = ("numer_urzad", "numer_decyzji_urzedu", "data_wydania_decyzji", "rodzaj_inwestycji", "nazwa_zamierzenia_bud", "nazwa_zam_budowlanego",
           "jednosta_numer_ew", "obreb_numer", "numer_dzialki")


def kind(text, house):
    t = text.lower()
    if re.search(r"magazyn\w*\s+energii|magazynowani\w*\s+energii|\bbess\b|akumulator", t):
        return "battery"
    if "fotowolt" in t or "słoneczn" in t or re.search(r"\bpv\b", t):
        return "solar"
    if "wiatrow" in t:
        return "wind"
    return "house" if house else "other"


def megawatts(text):
    """The first power in the text, in MW (kW and kWp converted), or None."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(mwp?|kwp?)\b", text.lower())
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    value = value / 1000 if m.group(2).startswith("k") else value
    return round(value, 3) if 0 < value < 5000 else None


def read_province(name):
    """Rows of one province's file as dicts of COLUMNS only."""
    for attempt in range(4):
        try:
            response = requests.get(URL.format(name=name), headers=UA, timeout=1800)
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(30 * (attempt + 1))
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    member = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
    with archive.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline=""), delimiter=";")
        head = next(reader)
        where = {c: head.index(c) for c in COLUMNS if c in head}
        for row in reader:
            if len(row) >= len(head):
                yield {c: row[i].strip() for c, i in where.items()}


def main(force=False, only=None, budget_minutes=90):
    files = station_files()
    lookup_path = os.path.join(LAND, "permits.json")
    old = json.load(open(lookup_path, encoding="utf-8")) if os.path.exists(lookup_path) else {}
    plots = {}
    for path in files:
        for plot in json.load(open(path, encoding="utf-8"))["plots"]:
            plots[plot["id"]] = plot
    missing = any("gp" not in p for p in plots.values())
    if not force and not missing and old.get("fetched") and (datetime.date.today() - datetime.date.fromisoformat(old["fetched"])).days < REFRESH_DAYS:
        print(f"Land permits: read on {old['fetched']}, not due yet")
        return False
    precincts = {pid.rsplit(".", 1)[0] for pid in plots}
    provinces = sorted({pid[:2] for pid in plots} & set(PROVINCES) & (set(only) if only else set(PROVINCES)))
    print(f"Land permits: {len(plots)} plots in {len(precincts)} precincts and {len(provinces)} provinces", flush=True)
    on_plot, energy, counts, t0, failed = {}, {}, {}, time.time(), set()
    deadline = t0 + capped(budget_minutes, 30, "Land permits") * 60
    for code in provinces:
        if time.time() > deadline:
            failed.add(code)
            print(f"Land permits: {PROVINCES[code]} left for the next run", flush=True)
            continue
        try:
            seen, n = set(), 0
            for row in read_province(PROVINCES[code]):
                n += 1
                unit, precinct, number = row.get("jednosta_numer_ew", ""), row.get("obreb_numer", ""), row.get("numer_dzialki", "")
                if not unit or not precinct or not number:
                    continue
                prec = f"{unit}.{precinct.zfill(4)}"
                pid = f"{prec}.{number}"
                date = row.get("data_wydania_decyzji", "")[:10]
                text = row.get("nazwa_zam_budowlanego", "")
                k = kind(text, row.get("rodzaj_inwestycji", "").startswith("Budynek mieszkalny"))
                new = 1 if row.get("nazwa_zamierzenia_bud", "").startswith("budowa nowego") else 0
                decision = (row.get("numer_urzad", ""), row.get("numer_decyzji_urzedu", ""), date)
                if pid in plots:
                    on_plot.setdefault(pid, set()).add((date, k, new, megawatts(text) if k in TYPES else None, decision))
                    if k in TYPES:
                        energy.setdefault(k, set()).add(pid)
                if prec in precincts and (decision, prec) not in seen:
                    seen.add((decision, prec))
                    c = counts.setdefault(prec, {})
                    c[k] = c.get(k, 0) + 1
                    if date >= PRECINCT_RECENT:
                        c[k + "_recent"] = c.get(k + "_recent", 0) + 1
            print(f"Land permits: {PROVINCES[code]}: {n} rows, {time.time() - t0:.0f} s", flush=True)
        except Exception as error:  # noqa: BLE001 -- a province that fails keeps its old values
            failed.add(code)
            print(f"Land permits: {PROVINCES[code]} failed: {str(error)[:160]}", flush=True)
    if len(failed) == len(provinces):
        print("Land permits: nothing read")
        return False

    # Nearest solar, battery and wind decision on another plot, from the plots' outlines.
    ids = list(plots)
    shapes = [plot_shape(plots[i]) for i in ids]
    nearest = {}
    for k in TYPES:
        geoms = [s for pid, s in zip(ids, shapes) if pid in energy.get(k, ()) and s is not None]
        tids = [pid for pid, s in zip(ids, shapes) if pid in energy.get(k, ()) and s is not None]
        out = {}
        if geoms:
            tree = shapely.STRtree(geoms)
            valid = [(i, s) for i, s in enumerate(shapes) if s is not None]
            left, right = tree.query([s for _, s in valid], predicate="dwithin", distance=NEAR_M)
            for a, b in zip(left, right):
                pid = ids[valid[int(a)][0]]
                if tids[int(b)] == pid:
                    continue
                d = int(round(float(shapely.distance(valid[int(a)][1], geoms[int(b)]))))
                if pid not in out or d < out[pid]:
                    out[pid] = d
        nearest[k] = out

    changed = 0
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        touched = False
        for plot in data["plots"]:
            pid = plot["id"]
            if pid[:2] not in provinces or pid[:2] in failed:
                continue
            # Newest first; the MW can be missing, so it isn't part of the sort key.
            gp = sorted(([d, k, n, mw] for d, k, n, mw, _ in on_plot.get(pid, ())), key=lambda g: (g[0], g[1], g[2]), reverse=True)
            gn = [nearest[k].get(pid) for k in TYPES]
            if plot.get("gp") != gp or plot.get("gn") != gn:
                plot["gp"], plot["gn"] = gp, gn
                touched = True
        if touched:
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            changed += 1
    keep = {p: c for p, c in old.get("precincts", {}).items() if p[:2] not in provinces or p[:2] in failed}
    keep.update(counts)
    json.dump({"source": "GUNB, Rejestr Wniosków, Decyzji i Zgłoszeń (wyszukiwarka.gunb.gov.pl), building permit decisions since 2016",
               "fetched": datetime.date.today().isoformat() if not failed else old.get("fetched"), "recent_from": PRECINCT_RECENT, "precincts": dict(sorted(keep.items()))},
              open(lookup_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"Land permits: {sum(1 for v in on_plot.values() if v)} plots with a decision on them; solar, battery and wind decisions on "
          f"{', '.join(f'{len(energy.get(k, ()))} {k}' for k in TYPES)} plots of ours; {changed} station files updated, {len(failed)} provinces failed")
    return changed > 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--provinces", nargs="*", help="only these province codes (test), e.g. 06")
    parser.add_argument("--budget-minutes", type=float, default=90)
    args = parser.parse_args()
    main(args.force, args.provinces, args.budget_minutes)
