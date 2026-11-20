"""
Markdown tables of a screening run (public data only):

    python etl/congestion/report.py --work W --year 2025 > report.md
"""

import argparse
import json
import os
import re

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
    rows = [r for r in s["rows_detail"] if r["kind"] == "line"]
    print(f"## Screening {a.year}\n")
    print(f"{s['rows']} monitored branches (Polish 220/400 kV circuits, tie lines, 400/220 kV transformers), "
          f"{s['contingencies']} single outages screened, phase shifters {s['pst_mode']}; {s['outage_topologies']} outage "
          f"topologies from ENTSO-E over {s['outage_hours']} hours ({len(s['outages_matched'])} elements matched). "
          f"Lines above 100% with nothing out at least once: {s['lines_n0_over']}; after an outage: {s['lines_n1_over']}.\n")
    t = inj["twh"]
    print(f"Energy in the year (TWh): demand {t['demand']}, JWCD units {t['jwcd']}, wind {t['wind']}, solar {t['solar']}, "
          f"other generation {t['other']}, net import {t['net_import_sections']} (PSE's balance: {t['net_import_swm']}). "
          f"Balance closed on the load: mean {inj['mismatch_mw']['mean']} MW, mean absolute {inj['mismatch_mw']['mean_abs']} MW. "
          f"Neighbours' mean net injection (MW): {s['neighbour_injection_mean_mw']}.\n")
    print("| N-1 h > 100% | N-0 h > 100% | N-1 p95 | Line | Rating MVA (30 °C) | Main outage (hours) | Solar hours | Evening peak |")
    print("|---:|---:|---:|---|---:|---|---:|---:|")
    seen, n = set(), 0
    for r in sorted(rows, key=lambda r: (-r["n1_hours"], -r["n1_p95"])):
        if n >= a.top:
            break
        key = re.sub(r" \(circuit \d+/\d+\)$", "", r["name"])
        if key in seen:
            continue
        seen.add(key)
        n += 1
        tot = max(1, r["n1_hours"])
        sol = sum(r["n1_by_hour"][h] for h in SOLAR) / tot
        eve = sum(r["n1_by_hour"][h] for h in EVENING) / tot
        c = r["n1_contingencies"][0] if r["n1_contingencies"] else {"name": "", "hours": 0}
        if r["n1_hours"] and r["n0_hours"] >= 0.9 * r["n1_hours"]:
            c = {"name": "already above 100% with nothing out (N-0)", "hours": r["n0_hours"]}
        print(f"| {r['n1_hours']} | {r['n0_hours']} | {r['n1_p95']:.2f} | {key}{' (plan)' if r['prsp_upgrade'] else ''} | {r['s_nom']} "
              f"| {c['name']} ({c['hours']}) | {sol:.0%} | {eve:.0%} |")
    print("\nSolar hours are 10:00-16:00 and the evening peak 17:00-22:00, Polish time. \"(plan)\": in PSE's modernisation plan, "
          "rated for a 60 °C conductor.\n")
    print("## Borders\n")
    print("The neighbours' injections are solved every hour so that each border carries PSE's metered flow; the split "
          "between a border's lines is the network's.\n")
    print("| Border | Model MW | Measured MW | Max error MW | Line | Mean MW | Share |")
    print("|---|---:|---:|---:|---|---:|---:|")
    for c, b in sorted(s["border"].items()):
        for ln in b["lines"]:
            print(f"| {c} | {b['model_mean_mw']} | {b['measured_mean_mw']} | {b['max_abs_error_mw']} | {ln['name']}{' (PST)' if ln['pst'] else ''} "
                  f"| {ln['mean_mw']} | {ln['share']:.2f} |")


if __name__ == "__main__":
    main()
