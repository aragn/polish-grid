"""
"What if" screening: add a plant at one substation and see how the year's N-0
and N-1 loading changes on the lines it affects.

    python etl/congestion/whatif.py --work D:/Power/pl-congestion --bus "Mokre" --tech solar --mw 100 200 400
    python etl/congestion/whatif.py --work D:/Power/pl-congestion --bus "Dunowo" --tech battery --mw 100 --hours 2

  --bus     a substation name (or code, or bus id) from network.json; with two
            voltages the 400 kV bus is taken unless --kv 220
  --tech    solar | wind  hourly profile from the 2025 weather cell of the
                          substation (atlite capacity factor, as in inputs.py)
            battery       daily arbitrage on Poland's day-ahead price: charges in
                          the --hours cheapest hours of every Polish day and
                          discharges in the --hours dearest (losses ignored)
  --mw      one or more sizes; each one is screened in full

Every MW the new plant produces displaces the JWCD thermal units in proportion
to their output that hour (when none run, the load takes it); battery
charging is supplied the same way.  This is a screening rule, not a market
model: the real redispatch depends on prices and on where the displaced
units are.

Prints and writes <work>/out/whatif_<bus>_<tech>.json: for every size, the
branches whose N-1 hours above 100% grow, with the contingency, the change in
hours and the worst loading.
"""

import argparse
import json
import math
import os
import re
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from core import Model  # noqa: E402
from flows import branch_label  # noqa: E402

NOT_THERMAL = ("Hydro", "Solar", "wind", "Wind")


def find_bus(m, key, kv):
    key_f = key.strip().lower()
    hits = [i for i, b in enumerate(m.buses) if not b["boundary"] and
            (b["id"] == key or (b["code"] or "").lower() == key_f or b["name"].lower() == key_f)]
    if not hits:
        hits = [i for i, b in enumerate(m.buses) if not b["boundary"] and key_f in b["name"].lower()]
    if not hits:
        raise SystemExit(f"no substation matches {key!r}")
    if kv:
        hits = [i for i in hits if m.buses[i]["kv"] == kv] or hits
    hits.sort(key=lambda i: (-m.buses[i]["kv"], m.buses[i]["id"].startswith("virtual")))
    return hits[0]


def profile(work, m, bus, tech, hours, duration, year, scale=1.0):
    """Hourly output per MW installed (negative: charging)."""
    b = m.buses[bus]
    if tech in ("solar", "wind"):
        w = np.load(os.path.join(work, "cache", "weather_pl.npz"))
        cell = int(np.argmin((w["y"] - b["lat"]) ** 2 + ((w["x"] - b["lon"]) * math.cos(math.radians(b["lat"]))) ** 2))
        t = pd.to_datetime(w["time"], unit="s", utc=True)
        # scaled so the existing fleet's modelled energy matches PSE's (inputs.py, k_wind / k_solar)
        return np.clip(w[tech][pd.Index(t).get_indexer(hours), cell].astype("float64") * scale, 0, 1)
    price = pd.Series(np.nan, index=hours)
    for month in range(1, 13):
        path = os.path.join(ROOT, "data", "prices", "PL", f"{year}-{month:02d}.json")
        if not os.path.exists(path):
            continue
        d = json.load(open(path, encoding="utf-8"))
        idx = pd.to_datetime(d["start"] + np.arange(len(d["v"])) * d["step"], unit="ms", utc=True)
        s = pd.Series(d["v"], index=idx, dtype="float64")
        s = s.resample("h").mean()  # 15-minute prices since October 2025
        price.update(s[s.index.isin(hours)])
    price = price.interpolate(limit_direction="both")
    p = np.zeros(len(hours))
    local_day = hours.tz_convert("Europe/Warsaw").date
    for day in np.unique(local_day):
        ix = np.where(local_day == day)[0]
        order = ix[np.argsort(price.values[ix], kind="stable")]
        p[order[:duration]] = -1.0
        p[order[-duration:]] = 1.0
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--bus", required=True)
    ap.add_argument("--kv", type=int)
    ap.add_argument("--tech", choices=["solar", "wind", "battery"], required=True)
    ap.add_argument("--mw", type=float, nargs="+", required=True)
    ap.add_argument("--hours", type=int, default=2, help="battery: hours of charging and of discharging a day")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-share", type=float, default=0.05,
                    help="list only branches that carry at least this share of the new plant's output in some hour")
    a = ap.parse_args()

    m = Model(a.work)
    inj = np.load(os.path.join(a.work, "out", f"injections_{a.year}.npz"))
    base = np.load(os.path.join(a.work, "out", f"flows_{a.year}.npz"))
    hours = pd.to_datetime(inj["hours"], unit="s", utc=True)
    F0, n1_0 = base["F"].astype("float64"), base["n1"]
    bus = find_bus(m, a.bus, a.kv)
    scale = float(inj[f"k_{a.tech}"]) if a.tech in ("solar", "wind") else 1.0
    p = profile(a.work, m, bus, a.tech, hours, a.hours, a.year, scale)

    # The sink: JWCD thermal units in proportion to their output that hour, else the load.
    plants = json.load(open(os.path.join(ROOT, "output", "plants.json"), encoding="utf-8"))["plants"]
    thermal = np.array([not any(w in plants.get(str(pl), {}).get("source", "") for w in NOT_THERMAL) for pl in inj["jwcd_plants"]])
    out_units = np.clip(inj["jwcd_units"][:, thermal], 0, None).astype("float64")  # [T, units]
    ubus = inj["jwcd_bus"][thermal]
    sink = np.zeros((len(hours), len(m.buses)))
    for j, ub in enumerate(ubus):
        sink[:, ub] += out_units[:, j]
    tot = sink.sum(1, keepdims=True)
    sink = np.where(tot > 1, sink / np.where(tot > 1, tot, 1), inj["pop_share"][None, :])
    # Flow change per MW of the new plant, every hour: PTDF of the bus less the PTDF of the sink.
    dF_per_mw = m.ptdf[:, bus][None, :] - sink @ m.ptdf.T  # [T, branch]

    n0_base = np.abs(F0) / m.s
    result = {"bus": m.buses[bus], "tech": a.tech, "battery_hours": a.hours if a.tech == "battery" else None,
              "full_load_hours": round(float(np.clip(p, 0, None).sum()), 1), "min_share": a.min_share, "sizes": []}
    print(f"{m.buses[bus]['name']} {m.buses[bus]['kv']} kV, {a.tech}: {result['full_load_hours']} full-load hours")
    for mw in a.mw:
        F = F0 + dF_per_mw * (p * mw)[:, None]
        n1, arg = m.screen(F)
        n0 = np.abs(F) / m.s
        rows = []
        for i, br in enumerate(m.branches):
            h0, h1 = int((n1_0[:, i] > 1).sum()), int((n1[:, i] > 1).sum())
            d0 = int((n0[:, i] > 1).sum()) - int((n0_base[:, i] > 1).sum())
            sens = float(np.abs(dF_per_mw[:, i]).max())
            if sens < a.min_share or (h1 == h0 and d0 == 0 and abs(float(n1[:, i].max() - n1_0[:, i].max())) < 0.01):
                continue
            over = n1[:, i] > 1
            cont = np.bincount(arg[over, i], minlength=len(m.branches)).argmax() if over.any() else None
            rows.append({"name": branch_label(m, i), "kv": br["kv"], "kind": br["kind"],
                         "n1_hours_before": h0, "n1_hours_after": h1, "n0_hours_change": d0,
                         "n1_max_before": round(float(n1_0[:, i].max()), 3), "n1_max_after": round(float(n1[:, i].max()), 3),
                         "contingency": branch_label(m, int(cont)) if cont is not None else None,
                         "share_of_output_max": round(sens, 3)})
        rows.sort(key=lambda r: (-(r["n1_hours_after"] - r["n1_hours_before"]), -(r["n1_max_after"] - r["n1_max_before"])))
        new_hours = sum(max(0, r["n1_hours_after"] - r["n1_hours_before"]) for r in rows)
        result["sizes"].append({"mw": mw, "added_n1_overload_hours": new_hours, "branches": rows})
        print(f"\n+{mw:g} MW: N-1 overload hours added on branches carrying {a.min_share:.0%}+ of its output: {new_hours}")
        for r in rows[:a.top]:
            print(f"  {r['n1_hours_before']:5d} -> {r['n1_hours_after']:5d} h  max {r['n1_max_before']:.2f} -> {r['n1_max_after']:.2f}"
                  f"  {r['name']}  [{r['contingency']}]")
    slug = re.sub(r"\W+", "_", m.buses[bus]["name"]).strip("_").lower()
    path = os.path.join(a.work, "out", f"whatif_{slug}_{a.tech}.json")
    json.dump(result, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n" + path)


if __name__ == "__main__":
    main()
