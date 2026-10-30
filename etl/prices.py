"""
Poland's day-ahead price from the ENTSO-E Transparency Platform, one file a month.

    python etl/prices.py              # daily: the last 10 days and tomorrow
    python etl/prices.py --backfill   # every month since June 2024 (the unit-output page's first day)

The token comes from the ENTSOE_API_KEY environment variable (a repository
secret in .github/workflows/prices.yml); it is never written to the repository.

data/prices/PL/<YYYY-MM>.json holds {"start": UTC ms of the first point,
"step": ms between points, "v": [EUR/MWh or null, ...]}.  The day-ahead market
went from hours to quarter-hours on 1 Oct 2025; a month keeps its finest step
and an hourly price is repeated over its quarter-hours when a month mixes both.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

import requests

API_URL = "https://web-api.tp.entsoe.eu/api"
AREA = "10YPL-AREA-----S"
FIRST_MONTH = dt.date(2024, 6, 1)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "prices", "PL")
STEP_MS = {"PT15M": 900000, "PT30M": 1800000, "PT60M": 3600000}


def request(token, start, end):
    """Day-ahead prices (A44) between two UTC datetimes; the XML root, or None for no data."""
    params = {
        "securityToken": token, "documentType": "A44", "in_Domain": AREA, "out_Domain": AREA,
        "periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M"),
    }
    wait = 10
    for attempt in range(6):
        try:
            response = requests.get(API_URL, params=params, timeout=120)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 5:
                raise
            time.sleep(wait)
            wait *= 2
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 5:
                response.raise_for_status()
            time.sleep(wait)
            wait *= 2
            continue
        root = ET.fromstring(response.content)
        if root.tag.endswith("Acknowledgement_MarketDocument"):
            reason = " ".join(e.text or "" for e in root.iter() if e.tag.endswith("text"))
            if "No matching data" in reason or response.status_code == 200:
                return None
            sys.exit(f"ENTSO-E refused the request: {reason}")
        response.raise_for_status()
        return root
    return None


def child_text(elem, tag):
    found = elem.find(f"{{*}}{tag}")
    return found.text if found is not None else None


def parse(root):
    """{utc_ms: (step_ms, price)} for the day-ahead (A01) series in euro."""
    points = {}
    for series in root.iter():
        if not series.tag.endswith("}TimeSeries"):
            continue
        contract = child_text(series, "contract_MarketAgreement.type")
        currency = child_text(series, "currency_Unit.name")
        if (contract and contract != "A01") or (currency and currency != "EUR"):
            continue
        for period in series.findall("{*}Period"):
            interval = period.find("{*}timeInterval")
            start = dt.datetime.strptime(child_text(interval, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
            end = dt.datetime.strptime(child_text(interval, "end"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
            step = STEP_MS[child_text(period, "resolution")]
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            given = {}
            for point in period.findall("{*}Point"):
                price = child_text(point, "price.amount")
                if price is not None:
                    given[int(child_text(point, "position"))] = float(price)
            # Curve A03 leaves out a point that repeats the one before.
            last = None
            for position in range(1, (end_ms - start_ms) // step + 1):
                last = given.get(position, last)
                if last is None:
                    continue
                ms = start_ms + (position - 1) * step
                # Where a finer series covers the same instant, keep it.
                if ms not in points or step < points[ms][0]:
                    points[ms] = (step, last)
    return points


def month_start(day):
    return dt.date(day.year, day.month, 1)


def next_month(day):
    return dt.date(day.year + day.month // 12, day.month % 12 + 1, 1)


def utc_midnight(day):
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


def write_month(month, points):
    """Merge points into the month's file (months are UTC calendar months)."""
    path = os.path.join(OUT, f"{month:%Y-%m}.json")
    merged = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            old = json.load(handle)
        for i, value in enumerate(old["v"]):
            if value is not None:
                merged[old["start"] + i * old["step"]] = (old["step"], value)
    lo, hi = utc_midnight(month).timestamp() * 1000, utc_midnight(next_month(month)).timestamp() * 1000
    for ms, point in points.items():
        if lo <= ms < hi:
            merged[ms] = point
    if not merged:
        return False
    step = min(s for s, _ in merged.values())
    first, last = min(merged), max(merged)
    values = [None] * ((last - first) // step + 1)
    for ms, (own_step, value) in merged.items():
        for k in range(own_step // step):
            index = (ms - first) // step + k
            if index < len(values) and (k == 0 or values[index] is None):
                values[index] = round(value, 2)
    body = {"area": "PL", "unit": "EUR/MWh", "start": first, "step": step, "v": values}
    os.makedirs(OUT, exist_ok=True)
    text = json.dumps(body, separators=(",", ":")) + "\n"
    if os.path.exists(path) and open(path, encoding="utf-8").read() == text:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true", help="every month since June 2024")
    args = parser.parse_args()
    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        sys.exit("Set ENTSOE_API_KEY.")

    today = dt.datetime.now(dt.timezone.utc).date()
    start = FIRST_MONTH if args.backfill else today - dt.timedelta(days=10)
    end = today + dt.timedelta(days=2)
    # One request per month, from the day before (Warsaw midnight is 22:00 or 23:00 UTC).
    month = month_start(start)
    changed = []
    while month <= end:
        lo = max(utc_midnight(month), utc_midnight(start)) - dt.timedelta(hours=2)
        hi = min(utc_midnight(next_month(month)), utc_midnight(end))
        root = request(token, lo, hi)
        points = parse(root) if root is not None else {}
        print(f"{month:%Y-%m}: {len(points)} prices")
        if write_month(month, points):
            changed.append(f"{month:%Y-%m}")
        month = next_month(month)
    print("Changed:", ", ".join(changed) or "none")


if __name__ == "__main__":
    main()
