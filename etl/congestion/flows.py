"""
Hourly N-0 and N-1 line loading for a year, and the screening summary:

    python etl/congestion/flows.py --work D:/Power/pl-congestion --year 2025

Reads network.json and injections_<year>.npz; writes
  <work>/out/flows_<year>.npz     F (N-0 MW), n1 (worst post-contingency loading,
                                  share of rating), n1_cont (the contingency)
  <work>/out/screening_<year>.json  per branch: N-0 and N-1 hours above 100%,
                                  the contingencies behind them, by hour of day
                                  (Polish time) and month.
"""

import argparse
import collections
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Model  # noqa: E402


def branch_label(m, i):
    b = m.branches[i]
    return b["name"] + (f" (circuit {b['circuit']}/{b['of']})" if b["of"] > 1 else "")


def summarise(m, F, n1, arg, hours, threshold=1.0, top=5):
    n0 = np.abs(F) / m.s
    local = hours.tz_convert("Europe/Warsaw")
    hod, month = local.hour.values, local.month.values
    out = []
    for i, b in enumerate(m.branches):
        over0 = n0[:, i] > threshold
        over1 = n1[:, i] > threshold
        conts = collections.Counter(arg[over1, i].tolist())
        out.append({
            "i": i, "id": b["id"], "name": branch_label(m, i), "kind": b["kind"], "kv": b["kv"], "tie": bool(b["tie"]),
            "s_nom": round(float(m.s[i])), "length_km": b["length_km"], "bridge": bool(m.bridge[i]),
            "bus0": b["bus0"], "bus1": b["bus1"],
            "n0_mean": round(float(n0[:, i].mean()), 3), "n0_p95": round(float(np.percentile(n0[:, i], 95)), 3),
            "n0_max": round(float(n0[:, i].max()), 3), "n0_hours": int(over0.sum()),
            "n1_p95": round(float(np.percentile(n1[:, i], 95)), 3), "n1_max": round(float(n1[:, i].max()), 3),
            "n1_hours": int(over1.sum()),
            "n1_contingencies": [{"i": k, "name": branch_label(m, k), "hours": n} for k, n in conts.most_common(top)],
            "n1_by_hour": np.bincount(hod[over1], minlength=24).tolist(),
            "n1_by_month": np.bincount(month[over1] - 1, minlength=12).tolist(),
            "mean_flow_mw": round(float(F[:, i].mean()), 1),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--year", type=int, default=2025)
    a = ap.parse_args()
    m = Model(a.work)
    inj = np.load(os.path.join(a.work, "out", f"injections_{a.year}.npz"))
    P = inj["P"].astype("float64")
    hours = pd.to_datetime(inj["hours"], unit="s", utc=True)
    F = m.flows(P)
    n1, arg = m.screen(F)
    np.savez_compressed(os.path.join(a.work, "out", f"flows_{a.year}.npz"), F=F.astype("float32"), n1=n1, n1_cont=arg,
                        hours=inj["hours"])
    rows = summarise(m, F, n1, arg, hours)
    # Border check: the tie lines of each neighbour carry its measured flow, split by impedance.
    ties = collections.defaultdict(list)
    for i, b in enumerate(m.branches):
        if b["tie"]:
            x = b["bus0"] if b["bus0"].startswith("X_") else b["bus1"]
            sign = -1.0 if b["bus0"].startswith("X_") else 1.0  # flow towards Poland, positive
            ties[x].append((branch_label(m, i), -sign * F[:, i]))
    border = {}
    for x, lst in ties.items():
        tot = sum(f for _, f in lst)
        border[x] = {"mean_import_mw": round(float(tot.mean()), 1),
                     "lines": [{"name": n, "mean_mw": round(float(f.mean()), 1),
                                "share": round(float(np.sum(f * tot) / max(np.sum(tot * tot), 1e-9)), 3)} for n, f in lst]}
    res = {"year": a.year, "branches": len(m.branches), "contingencies": int(len(m.cont)), "bridges": int(m.bridge.sum()),
           "lines_n0_over": sum(r["n0_hours"] > 0 for r in rows if r["kind"] == "line"),
           "lines_n1_over": sum(r["n1_hours"] > 0 for r in rows if r["kind"] == "line"),
           "border": border, "rows": rows}
    json.dump(res, open(os.path.join(a.work, "out", f"screening_{a.year}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    lines = sorted([r for r in rows if r["kind"] == "line"], key=lambda r: -r["n1_hours"])
    print(f"{len(m.branches)} branches, {len(m.cont)} contingencies, {int(m.bridge.sum())} radial; "
          f"lines with N-0 > 100%: {res['lines_n0_over']}, with N-1 > 100%: {res['lines_n1_over']}")
    for r in lines[:30]:
        c = r["n1_contingencies"][0]["name"] if r["n1_contingencies"] else ""
        print(f"{r['n1_hours']:5d} h N-1 | {r['n0_hours']:5d} h N-0 | p95 {r['n1_p95']:.2f} | {r['name']} | {c}")


if __name__ == "__main__":
    main()
