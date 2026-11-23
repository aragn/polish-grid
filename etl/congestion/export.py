"""
The screening results the Grid capacity page draws, from public data only:

    python etl/congestion/export.py --work W --year 2025

Writes data/congestion/screening_<year>.json:
  lines     every monitored Polish 220/400 kV line and 400/220 kV transformer,
            circuits of one line merged (the worst circuit's numbers): route
            from OSM, rating basis, N-0 and N-1 hours above 100%, N-1 loading
            at the 95th percentile, the outages behind the N-1 hours, and those
            hours by hour of day (Polish time) and month;
  stations  every 220/400 kV substation: 100 MW of solar, wind or a 2-hour
            battery there, and the lines it would push over 100% more often
            (whatif.py --batch);
  borders   the split of each border's measured flow between its lines;
  meta      the year, inputs and the rules the page quotes.
Nothing here comes from JAO.
"""

import argparse
import collections
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def base_name(name):
    return re.sub(r" \(circuit \d+/\d+\)$", "", name)


def summary(v):
    """A substation's what-if, lines only (the page leaves transformers out: their ratings are mostly a default)."""
    lines = [x for x in v["branches"] if x.get("kind", "line") == "line"]
    worst = max(lines, key=lambda x: x["n1_max_after"]) if lines else None
    return {"full_load_hours": v["full_load_hours"],
            "into_curtailment": v.get("output_into_curtailment"), "from_curtailment": v.get("charging_from_curtailment"),
            "added_hours": sum(max(0, x["n1_hours_after"] - x["n1_hours_before"]) for x in lines),
            "worst_after": worst["n1_max_after"] if worst else None,
            "worst_line": base_name(worst["name"]) if worst else None,
            "lines": dedupe(lines)[:5]}


def dedupe(branches):
    """One row per line (its worst circuit), in whatif.py's order."""
    out = {}
    for x in branches:
        k = base_name(x["name"])
        if k not in out or x["n1_hours_after"] - x["n1_hours_before"] > out[k]["after"] - out[k]["before"]:
            out[k] = {"name": k, "before": x["n1_hours_before"], "after": x["n1_hours_after"], "max_before": x["n1_max_before"],
                      "max_after": x["n1_max_after"], "outage": x["contingency"], "share": x["share_of_output_max"]}
    return list(out.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--year", type=int, default=2025)
    a = ap.parse_args()
    out = os.path.join(a.work, "out")
    net = json.load(open(os.path.join(out, "network.json"), encoding="utf-8"))
    scr = json.load(open(os.path.join(out, f"screening_{a.year}.json"), encoding="utf-8"))
    inj = json.load(open(os.path.join(out, f"injections_{a.year}.json"), encoding="utf-8"))
    sens_path = os.path.join(out, f"sensitivity_{a.year}.json")
    sens = json.load(open(sens_path, encoding="utf-8")) if os.path.exists(sens_path) else None
    batch_path = os.path.join(out, f"whatif_batch_{a.year}.json")
    batch = json.load(open(batch_path, encoding="utf-8")) if os.path.exists(batch_path) else {}
    geom = {b["id"]: b.get("geom") for b in net["branches"]}
    buses = {b["id"]: b for b in net["buses"]}

    lines = collections.OrderedDict()
    for r in scr["rows_detail"]:
        key = (r["id"], base_name(r["name"]))
        e = lines.get(key)
        if e is None or r["n1_hours"] > e["n1_hours"] or (r["n1_hours"] == e["n1_hours"] and r["n1_p95"] > e["n1_p95"]):
            b0, b1 = buses[r["bus0"]], buses[r["bus1"]]
            g = geom.get(r["id"]) or [[p["lon"], p["lat"]] for p in (b0, b1) if p.get("lat") is not None]
            circuits = (e or {}).get("circuits", 0)
            lines[key] = {
                "name": base_name(r["name"]), "kind": r["kind"], "kv": r["kv"], "tie": r["tie"], "pst": r["pst"],
                "rating_mva": r["s_nom"], "conductor": r.get("conductor"), "plan_upgrade": r.get("prsp_upgrade", False),
                "rating_source": r.get("rating_source"), "length_km": r["length_km"],
                "n0_hours": r["n0_hours"], "n1_hours": r["n1_hours"], "n1_p95": r["n1_p95"], "n0_p95": r["n0_p95"],
                "n1_max": r["n1_max"],
                "outages": [{"name": c["name"], "hours": c["hours"]} for c in r["n1_contingencies"][:4]],
                "by_hour": r["n1_by_hour"], "by_month": r["n1_by_month"], "geom": g, "circuits": circuits,
            }
        lines[key]["circuits"] = lines[key].get("circuits", 0) + 1

    stations = []
    for bid, s in batch.items():
        stations.append({"name": s["name"], "kv": s["kv"], "lat": s["lat"], "lon": s["lon"],
                         "tech": {t: summary(v) for t, v in s["tech"].items()}})
    stations.sort(key=lambda s: s["name"])

    res = {
        "year": a.year,
        "meta": {"monitored": len(lines), "contingencies": scr["contingencies"], "outage_hours": scr["outage_hours"],
                 "outage_elements": scr["outages_matched"], "pst_mode": scr["pst_mode"], "sensitivity": sens,
                 "curtailed_hours": inj.get("curtailed_hours"), "curtailed_gwh": inj.get("curtailed_gwh"),
                 "twh": inj["twh"], "weather_scale": inj["weather_scale"], "load_buses": inj["load_buses"],
                 "classes": net["classes"], "t_ref": net["t_ref"], "t_design": net["t_design"], "t_limited": net["t_limited"],
                 "sources": ["OpenStreetMap contributors (ODbL): Geofabrik extract of 1 January 2026 (Poland, 110 kV and up), "
                             "PyPSA-Eur prebuilt OSM network 0.7 (DE, CZ, SK, LT)",
                             "PSE reporting API: unit output (gen-jw), system balance (his-wlk-cal), border flows (przeplywy-mocy), wind and solar curtailment (poze-redoze)",
                             "ENTSO-E Transparency Platform: unavailability of transmission infrastructure (10.1.A/B)",
                             "URE: register of licensed renewable installations (Mapa OZE, 30 June 2025)",
                             "PSE: development plan 2025-2034 (lines it modernises)",
                             "ERA5 (Copernicus) wind and temperature, SARAH-3 (EUMETSAT) irradiance, via PyPSA-Eur's 2025 cutout"]},
        "borders": scr["border"],
        "lines": list(lines.values()),
        "stations": stations,
    }
    dst = os.path.join(ROOT, "data", "congestion")
    os.makedirs(dst, exist_ok=True)
    path = os.path.join(dst, f"screening_{a.year}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    print(path, os.path.getsize(path) // 1024, "kB;", len(lines), "lines,", len(stations), "stations")


if __name__ == "__main__":
    main()
