"""
Markdown tables of a screening run (public data only):

    python etl/congestion/report.py --work D:/Power/pl-congestion --year 2025 > report.md
"""

import argparse
import json
import os

SOLAR = range(10, 16)   # 10:00-15:59 Polish time
EVENING = range(17, 22)  # 17:00-21:59


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    s = json.load(open(os.path.join(a.work, "out", f"screening_{a.year}.json"), encoding="utf-8"))
    inj = json.load(open(os.path.join(a.work, "out", f"injections_{a.year}.json"), encoding="utf-8"))
    rows = [r for r in s["rows"] if r["kind"] == "line"]
    print(f"## Screening {a.year}\n")
    print(f"{s['branches']} branches (each circuit counted), {s['contingencies']} single-circuit and transformer "
          f"contingencies, {s['bridges']} radial branches not screened as outages. Lines above 100% in N-0 at least once: "
          f"{s['lines_n0_over']}; after a contingency: {s['lines_n1_over']}.\n")
    t = inj["twh"]
    print(f"Energy in the year (TWh): demand {t['demand']}, JWCD units {t['jwcd']}, wind {t['wind']}, solar {t['solar']}, "
          f"other generation {t['other']}, net import {t['net_import_sections']} (PSE's balance: {t['net_import_swm']}). "
          f"Balance closed on the load: mean {inj['mismatch_mw']['mean']} MW, mean absolute {inj['mismatch_mw']['mean_abs']} MW.\n")
    print("| N-1 h > 100% | N-0 h > 100% | N-1 p95 | Line | Rating MVA | Main contingency (hours) | Solar hours | Evening peak |")
    print("|---:|---:|---:|---|---:|---|---:|---:|")
    seen = set()
    n = 0
    for r in sorted(rows, key=lambda r: -r["n1_hours"]):
        if r["n1_hours"] == 0 or n >= a.top:
            break
        key = r["name"].split(" (circuit")[0]
        if key in seen:
            continue
        seen.add(key)
        n += 1
        tot = max(1, r["n1_hours"])
        sol = sum(r["n1_by_hour"][h] for h in SOLAR) / tot
        eve = sum(r["n1_by_hour"][h] for h in EVENING) / tot
        c = r["n1_contingencies"][0] if r["n1_contingencies"] else {"name": "", "hours": 0}
        print(f"| {r['n1_hours']} | {r['n0_hours']} | {r['n1_p95']:.2f} | {r['name']} | {r['s_nom']} | {c['name']} ({c['hours']}) "
              f"| {sol:.0%} | {eve:.0%} |")
    print("\nSolar hours are 10:00-16:00 and the evening peak 17:00-22:00, Polish time (6 and 5 of the 24 hours).\n")
    print("## Borders\n")
    print("Each neighbour's measured flow (PSE, przeplywy-mocy) is injected at its boundary node, so the border totals are "
          "inputs; the model only splits them between the tie lines.\n")
    print("| Boundary | Mean import MW | Tie line | Mean MW | Share |")
    print("|---|---:|---|---:|---:|")
    for x, b in sorted(s["border"].items()):
        for ln in b["lines"]:
            print(f"| {x[2:]} | {b['mean_import_mw']} | {ln['name']} | {ln['mean_mw']} | {ln['share']:.2f} |")


if __name__ == "__main__":
    main()
