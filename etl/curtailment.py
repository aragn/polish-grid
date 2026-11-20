"""
How much wind and solar output PSE has cut back, and why, from PSE's report of reductions of wind and
solar generation ("redukcje generacji źródeł wiatrowych i fotowoltaicznych", API dataset `poze-redoze`):

    python etl/curtailment.py

Each hour (repeated in its four quarter-hours) gives the MW cut for solar and for wind, split into
  balance   too much generation for the system as a whole (low demand, not enough export or storage);
  network   a grid limit somewhere (a line or transformer would be overloaded).
These are PSE's non-market redispatch of wind and solar: under Regulation (EU) 2019/943 art. 13 the
plants are compensated, unless they agreed to limits.  The data is for all of Poland, with no location.
PSE revises past days (May 2025 was republished in November 2025), so the whole series is read every run:
about 80,000 rows since 24 September 2024, in pages of 5,000.

Solar and wind output for the share curtailed comes from data/generation/PL/<month>.json (ENTSO-E A75,
etl/generation.py): B16 solar, B18 offshore and B19 onshore wind.

Writes data/curtailment.json: months (MWh by type and reason, and output), the average day by hour,
the largest days, and yearly totals.
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
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "curtailment.json")
GENERATION = os.path.join(ROOT, "data", "generation", "PL")
API = "https://api.raporty.pse.pl/api/poze-redoze"
PAGE = "https://raporty.pse.pl/report/poze-redoze"
FIELDS = ("pv_red_balance", "pv_red_network", "wi_red_balance", "wi_red_network")
KEYS = {"pv_red_balance": "pv_bal", "pv_red_network": "pv_net", "wi_red_balance": "wi_bal", "wi_red_network": "wi_net"}


def get(url, params=None):
    # PSE's API often answers 500 to a cold query after ~30 s, then fast: retry with a per-attempt timeout.
    for attempt in range(5):
        try:
            response = requests.get(url, params=params, timeout=90)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))


def fetch():
    rows, url = [], API
    params = {"$select": "dtime,business_date," + ",".join(FIELDS), "$orderby": "dtime asc", "$first": 5000}
    while url:
        page = get(url, params)
        rows.extend(page["value"])
        url, params = page.get("nextLink"), None
    return rows


def generation_by_month():
    """MWh of solar and wind output per month from the ENTSO-E files (15-minute MW)."""
    out = {}
    if not os.path.isdir(GENERATION):
        return out
    for name in sorted(os.listdir(GENERATION)):
        if not re.fullmatch(r"\d{4}-\d{2}\.json", name):
            continue
        data = json.load(open(os.path.join(GENERATION, name), encoding="utf-8"))
        hours = data.get("step", 900000) / 3600000
        types = data.get("types", {})
        total = lambda codes: round(sum(v for c in codes for v in types.get(c, []) if v is not None) * hours)
        out[name[:7]] = {"pv_gen": total(["B16"]), "wi_gen": total(["B18", "B19"])}
    return out


def build(rows):
    months = collections.defaultdict(lambda: collections.Counter())
    days = collections.defaultdict(lambda: collections.Counter())
    hours = collections.defaultdict(lambda: collections.Counter())     # (year, hour) -> MWh
    last_with_data = None
    for row in rows:
        values = {KEYS[f]: -(row[f] or 0) * 0.25 for f in FIELDS}       # MW (negative) for a quarter-hour -> MWh
        if any(row[f] is not None for f in FIELDS):
            last_with_data = row["business_date"]
        # dtime is the end of the quarter-hour in Polish time ("00:15" .. "24:00" written as next day 00:00);
        # the repeated autumn hour is written "02a:15".
        h, m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2})a?:(\d{2})", row["dtime"]).groups()
        hour = ((int(h) * 60 + int(m) - 15) // 60) % 24
        day = row["business_date"]
        for key, mwh in values.items():
            if mwh:
                months[day[:7]][key] += mwh
                days[day][key] += mwh
                hours[(day[:4], hour)][key] += mwh
    first = rows[0]["business_date"] if rows else None
    gen = generation_by_month()
    month_list = []
    for month in sorted(set(months) | {m for m in gen if first and m >= first[:7]}):
        if not first or month < first[:7] or (last_with_data and month > last_with_data[:7]):
            continue
        entry = {"month": month, **{k: round(months[month][k]) for k in KEYS.values()}, **gen.get(month, {})}
        # The first month starts on 24 September 2024; the latest is incomplete: their share of output is not comparable.
        if (month == first[:7] and not first.endswith("-01")) or (last_with_data and month == last_with_data[:7]):
            entry["partial"] = True
        month_list.append(entry)
    years = {}
    for entry in month_list:
        y = years.setdefault(entry["month"][:4], collections.Counter())
        for k in list(KEYS.values()) + ["pv_gen", "wi_gen"]:
            y[k] += entry.get(k) or 0
        y["months"] += 1
    year_days = collections.Counter(d[:4] for d in days)
    profile = {}
    for (year, hour), counter in hours.items():
        profile.setdefault(year, [dict.fromkeys(KEYS.values(), 0) for _ in range(24)])
        for k in KEYS.values():
            profile[year][hour][k] = round(counter[k] / max(1, year_days[year]), 1)   # average MWh in that hour per day
    top = sorted(days.items(), key=lambda item: -sum(item[1].values()))[:15]
    return {
        "source": PAGE, "api": API, "first": first, "last": last_with_data,
        "months": month_list,
        "years": {y: {k: round(v) for k, v in c.items()} for y, c in sorted(years.items())},
        "profile": profile, "profile_days": dict(year_days),
        "days_with": {y: sum(1 for d, c in days.items() if d[:4] == y and sum(c.values()) > 0) for y in sorted(year_days)},
        "top_days": [{"day": d, **{k: round(c[k]) for k in KEYS.values()}} for d, c in top],
    }


def main():
    rows = fetch()
    if not rows:
        raise RuntimeError(f"PSE's {API} returned no rows")
    result = build(rows)
    result["updated"] = datetime.date.today().isoformat()
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    before = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
    changed = re.sub(r'"updated":"[^"]*"', "", text) != re.sub(r'"updated":"[^"]*"', "", before)
    if changed:
        open(OUT, "w", encoding="utf-8").write(text)
    totals = {y: round(sum(v[k] for k in KEYS.values()) / 1000) for y, v in result["years"].items()}
    print(f"Curtailment: {len(rows)} rows {result['first']} to {result['last']}, GWh by year {totals}{'' if changed else ' (unchanged)'}")
    return changed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
