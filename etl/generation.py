"""
Poland's actual generation per production type from the ENTSO-E Transparency
Platform (A75, which PSE submits), one file a month.

    python etl/generation.py              # daily: the last 10 days
    python etl/generation.py --backfill   # every month since June 2024

The unit-output page uses it only to split PSE's "rest of the system"
(generation outside the JWCD units, less wind and solar) by fuel; the level
of that rest stays PSE's own.  Token from ENTSOE_API_KEY, as in prices.py.

data/generation/PL/<YYYY-MM>.json holds {"start": UTC ms of the first point,
"step": ms between points, "types": {"B04": [MW or null, ...], ...}} with
ENTSO-E's production-type codes (B01 biomass, B02 lignite, B03 coal-derived
gas, B04 gas, B05 hard coal, B06 oil, B10-B12 hydro, B15 other renewable,
B16 solar, B18-B19 wind, B20 other, B25 storage).  Only generation is kept,
not the consumption series of pumped storage.
"""

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from prices import AREA, FIRST_MONTH, STEP_MS, child_text, month_start, next_month, request, utc_midnight  # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), "data", "generation", "PL")


def parse(root):
    """{type: {utc_ms: (step_ms, MW)}} for the generation series."""
    out = {}
    for series in root.iter():
        if not series.tag.endswith("}TimeSeries"):
            continue
        # Generation comes with inBiddingZone; pumped-storage consumption with outBiddingZone.
        if series.find("{*}inBiddingZone_Domain.mRID") is None:
            continue
        psr = series.find("{*}MktPSRType")
        kind = child_text(psr, "psrType") if psr is not None else None
        if not kind:
            continue
        points = out.setdefault(kind, {})
        for period in series.findall("{*}Period"):
            interval = period.find("{*}timeInterval")
            start = dt.datetime.strptime(child_text(interval, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
            step = STEP_MS[child_text(period, "resolution")]
            start_ms = int(start.timestamp() * 1000)
            for point in period.findall("{*}Point"):
                quantity = child_text(point, "quantity")
                if quantity is None:
                    continue
                ms = start_ms + (int(child_text(point, "position")) - 1) * step
                if ms not in points or step < points[ms][0]:
                    points[ms] = (step, float(quantity))
    return out


def write_month(month, by_type):
    path = os.path.join(OUT, f"{month:%Y-%m}.json")
    merged = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            old = json.load(handle)
        for kind, values in old["types"].items():
            merged[kind] = {old["start"] + i * old["step"]: (old["step"], v) for i, v in enumerate(values) if v is not None}
    lo, hi = utc_midnight(month).timestamp() * 1000, utc_midnight(next_month(month)).timestamp() * 1000
    for kind, points in by_type.items():
        target = merged.setdefault(kind, {})
        for ms, point in points.items():
            if lo <= ms < hi:
                target[ms] = point
    stamps = [ms for points in merged.values() for ms in points]
    if not stamps:
        return False
    step = min(s for points in merged.values() for s, _ in points.values())
    first, last = min(stamps), max(stamps)
    types = {}
    for kind in sorted(merged):
        values = [None] * ((last - first) // step + 1)
        for ms, (own_step, value) in merged[kind].items():
            for k in range(own_step // step):
                index = (ms - first) // step + k
                if index < len(values) and (k == 0 or values[index] is None):
                    values[index] = round(value)
        if any(v is not None for v in values):
            types[kind] = values
    body = {"area": "PL", "unit": "MW", "start": first, "step": step, "types": types}
    text = json.dumps(body, separators=(",", ":")) + "\n"
    os.makedirs(OUT, exist_ok=True)
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
    end = today + dt.timedelta(days=1)
    month = month_start(start)
    changed = []
    while month <= end:
        lo = max(utc_midnight(month), utc_midnight(start)) - dt.timedelta(hours=2)
        hi = min(utc_midnight(next_month(month)), utc_midnight(end))
        root = request(token, lo, hi, {"documentType": "A75", "processType": "A16", "in_Domain": AREA})
        by_type = parse(root) if root is not None else {}
        print(f"{month:%Y-%m}: " + (", ".join(f"{k} {len(v)}" for k, v in sorted(by_type.items())) or "no data"))
        if write_month(month, by_type):
            changed.append(f"{month:%Y-%m}")
        month = next_month(month)
    print("Changed:", ", ".join(changed) or "none")


if __name__ == "__main__":
    main()
