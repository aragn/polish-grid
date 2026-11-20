"""
Hourly N-0 and N-1 loading for a year, and the screening summary:

    python etl/congestion/flows.py --work W --year 2025 [--outages W/cache/outages/outages_2025.json]

Every hour:
  1. the topology: the elements ENTSO-E lists as out of service (planned or
     forced, 10.1.A/B) that the model can match are removed;
  2. the neighbours' net injections (DE, CZ, SK, LT, spread by population) are
     solved so that the model's flow on every Polish border equals PSE's metered
     flow; the split between a border's lines, and everything behind it, is the
     network's;
  3. N-0 flows, and N-1 flows from LODFs for every Polish 220/400 kV circuit,
     tie line and 400/220 kV transformer that does not island part of the grid
     (phase-shifter corridors to Germany keep their flow, see core.py);
  4. loading against the hour's temperature-dependent rating.

Writes W/out/flows_<year>.npz (F, n1, n1_cont, ratings, neighbour injections)
and W/out/screening_<year>.json (per monitored branch: N-0 and N-1 hours above
100%, the contingencies behind them, by hour of day and month; per border:
the split between its lines).
"""

import argparse
import collections
import json
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Model, fold  # noqa: E402

FOREIGN = {"vierraden": "DE", "hagenwerder": "DE", "alytus": "LT", "nosovice": "CZ", "detmarovice": "CZ",
           "liskovec": "CZ", "albrechtice": "CZ", "lemesany": "SK", "sterno": "SE", "khmelnytskyi": "UA"}


def branch_label(m, i):
    b = m.branches[i]
    return b["name"] + (f" (circuit {b['circuit']}/{b['of']})" if b["of"] > 1 else "")


# ---------------------------------------------------------------- outages
def match_outages(m, path, hours):
    """{hour index: frozenset(branch indices)} from ENTSO-E's outages, matched by station names."""
    rows = json.load(open(path, encoding="utf-8"))
    names = [fold(re.sub(r" tee$", "", b["name"] or "")) for b in m.buses]
    rowset = set(m.rows)
    by_pair = collections.defaultdict(list)  # (stationA, stationB-or-country, kv) -> circuits
    for r in m.rows:
        b = m.branches[r]
        n0, n1 = names[m.f[r]], names[m.t[r]]
        c0, c1 = m.buses[m.f[r]]["country"], m.buses[m.t[r]]["country"]
        end0 = n0 if c0 == "PL" else c0
        end1 = n1 if c1 == "PL" else c1
        by_pair[(end0, end1, b["kv"], b["kind"])].append(r)
        by_pair[(end1, end0, b["kv"], b["kind"])].append(r)

    def find(name, kind):
        n = re.sub(r"^\s*\d{3}(/\d{3})?\s*kV\s*", "", name or "")
        circuit = None
        mm = re.search(r"\s(\d)$", n)
        if mm:
            circuit, n = int(mm.group(1)), n[:mm.start()]
        if kind == "transformer":
            mt = re.match(r"(.+?)\s+AT\s?\d", n)
            if not mt:
                return []
            st = fold(mt.group(1)).split(" ")[0]
            return [r for (e0, e1, kv, k), rs in by_pair.items() if k == "trafo" and e0.startswith(st) for r in rs][:1]
        parts = [fold(p).strip() for p in re.split(r"\s*-\s*", n)]
        if len(parts) != 2:
            return []
        ends = []
        for p in parts:
            first = p.split(" ")[0]
            ends.append(FOREIGN.get(first, p))
        hits = []
        for (e0, e1, kv, k), rs in by_pair.items():
            if k != "line":
                continue
            ok0 = e0 == ends[0] if len(ends[0]) == 2 else e0.startswith(ends[0].split(" ")[0]) and e0[:4] == ends[0][:4]
            ok1 = e1 == ends[1] if len(ends[1]) == 2 else e1.startswith(ends[1].split(" ")[0]) and e1[:4] == ends[1][:4]
            if ok0 and ok1:
                hits.extend(rs)
        hits = sorted(set(hits), key=lambda r: m.branches[r]["circuit"])
        if circuit and len(hits) > 1:
            hits = [h for h in hits if m.branches[h]["circuit"] == circuit] or hits[:1]
        return hits

    out = collections.defaultdict(set)
    report = collections.Counter()
    h0 = hours[0]
    for o in rows:
        idx = []
        for a in o["assets"]:
            if a["type"] not in ("AC line", "transformer"):
                continue
            hit = find(a["name"], a["type"])
            report[("matched" if hit else "unmatched", a["name"])] += 1
            idx += hit
        if not idx:
            continue
        s, e = pd.Timestamp(o["start"]), pd.Timestamp(o["end"])
        lo = max(0, int(np.floor((s - h0) / pd.Timedelta(hours=1))))
        hi = min(len(hours), int(np.ceil((e - h0) / pd.Timedelta(hours=1))))
        for t in range(lo, hi):
            out[t].update(i for i in idx if i in rowset)
    return {t: frozenset(v) for t, v in out.items()}, report


# ---------------------------------------------------------------- run
def tie_rows(m):
    """[(row position, neighbour country, sign that makes the flow an import into Poland)]."""
    rpos = {r: j for j, r in enumerate(m.rows)}
    out = []
    for r in m.rows:
        if not m.branches[r]["tie"]:
            continue
        c0, c1 = m.buses[m.f[r]]["country"], m.buses[m.t[r]]["country"]
        out.append((rpos[r], c1 if c0 == "PL" else c0, -1.0 if c0 == "PL" else 1.0))
    return out


def outage_groups(m, path, hours):
    outages, report = (match_outages(m, path, hours) if path else ({}, collections.Counter()))
    groups = collections.defaultdict(list)
    for t in range(len(hours)):
        groups[outages.get(t, frozenset())].append(t)
    return groups, report


def screen_year(m, P, gsk, neigh, border, R, groups, extra=None, chunk=1500, subset=None):
    """Base flows F, worst N-1 loading n1 and its contingency, neighbour injections U, for every hour.
    extra(tt, Pr) may return a flow change [len(tt), rows] to add (whatif.py)."""
    T, L = P.shape[0], len(m.rows)
    ties = tie_rows(m)
    F = np.zeros((T, L), dtype="float32")
    n1 = np.zeros((T, L), dtype="float32")
    arg = np.zeros((T, L), dtype="int32")
    U = np.zeros((T, len(neigh)), dtype="float32")
    applied_hours = 0
    for out, ts in groups.items():
        Pr, lodf, cont, bridge, applied = m.state(out)
        if applied:
            applied_hours += len(ts)
        Mb = np.zeros((len(neigh), m.nb))
        for j, c, sgn in ties:
            if c in neigh:
                Mb[neigh.index(c)] += sgn * Pr[j]
        A = np.vstack([Mb @ gsk, gsk.sum(0, keepdims=True)])
        for k0 in range(0, len(ts), chunk):
            tt = np.array(ts[k0:k0 + chunk])
            Pk = P[tt].astype("float64")
            rhs = np.hstack([border[tt] - Pk @ Mb.T, -Pk.sum(1, keepdims=True)])
            u = np.linalg.lstsq(A, rhs.T, rcond=None)[0].T  # [t, neighbours]
            f = (Pk + u @ gsk.T) @ Pr.T
            if extra is not None:
                f = f + extra(tt, Pr)
            F[tt] = f
            U[tt] = u
            n1[tt], arg[tt] = m.screen(f, R[tt], lodf, cont, subset=subset)
    return F, n1, arg, U, applied_hours


def summarise(m, F, R, n1, arg, hours, threshold=1.0, top=5):
    n0 = np.abs(F) / R
    local = hours.tz_convert("Europe/Warsaw")
    hod, month = local.hour.values, local.month.values
    out = []
    for j, i in enumerate(m.rows):
        b = m.branches[i]
        over0, over1 = n0[:, j] > threshold, n1[:, j] > threshold
        conts = collections.Counter(arg[over1, j].tolist())
        out.append({
            "j": j, "i": int(i), "id": b["id"], "name": branch_label(m, i), "kind": b["kind"], "kv": b["kv"],
            "tie": bool(b["tie"]), "pst": bool(m.pst[i]), "s_nom": round(float(m.s[i])), "length_km": b["length_km"],
            "conductor": b.get("conductor"), "prsp_upgrade": bool(b.get("prsp_upgrade")), "rating_source": b.get("rating_source"),
            "bus0": b["bus0"], "bus1": b["bus1"],
            "n0_mean": round(float(n0[:, j].mean()), 3), "n0_p95": round(float(np.percentile(n0[:, j], 95)), 3),
            "n0_max": round(float(n0[:, j].max()), 3), "n0_hours": int(over0.sum()),
            "n1_p95": round(float(np.percentile(n1[:, j], 95)), 3), "n1_max": round(float(n1[:, j].max()), 3),
            "n1_hours": int(over1.sum()),
            "n1_contingencies": [{"i": int(k), "name": branch_label(m, k), "hours": n} for k, n in conts.most_common(top)],
            "n1_by_hour": np.bincount(hod[over1], minlength=24).tolist(),
            "n1_by_month": np.bincount(month[over1] - 1, minlength=12).tolist(),
            "mean_flow_mw": round(float(F[:, j].mean()), 1),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--outages", help="ENTSO-E outages JSON (fetch_outages.py)")
    ap.add_argument("--pst", default="fixed", choices=["fixed", "free"])
    a = ap.parse_args()
    m = Model(a.work, pst=a.pst)
    inj = np.load(os.path.join(a.work, "out", f"injections_{a.year}.npz"))
    hours = pd.to_datetime(inj["hours"], unit="s", utc=True)
    T, L = len(hours), len(m.rows)
    P = inj["P"]
    gsk = inj["gsk"].astype("float64")
    neigh = list(inj["neighbours"])
    border = inj["border"].astype("float64")
    R = m.ratings(a.work, hours)

    groups, oreport = outage_groups(m, a.outages, hours)

    ties = tie_rows(m)
    F, n1, arg, U, applied_hours = screen_year(m, P, gsk, neigh, border, R, groups)
    rows = summarise(m, F, R, n1, arg, hours)
    np.savez_compressed(os.path.join(a.work, "out", f"flows_{a.year}.npz"), F=F, n1=n1, n1_cont=arg, R=R, U=U,
                        rows=m.rows, hours=inj["hours"])
    # border check and split between each border's lines
    border_out = {}
    for c in neigh + ["UA"]:
        js = [(j, sgn) for j, cc, sgn in ties if cc == c]
        if not js:
            continue
        tot = sum(sgn * F[:, j] for j, sgn in js)
        meas = border[:, neigh.index(c)] if c in neigh else None
        border_out[c] = {"model_mean_mw": round(float(tot.mean()), 1),
                         "measured_mean_mw": round(float(meas.mean()), 1) if meas is not None else None,
                         "max_abs_error_mw": round(float(np.abs(tot - meas).max()), 1) if meas is not None else None,
                         "lines": [{"name": branch_label(m, m.rows[j]), "pst": bool(m.pst[m.rows[j]]),
                                    "mean_mw": round(float(sgn * F[:, j].mean()), 1),
                                    "share": round(float(np.sum(sgn * F[:, j] * tot) / max(np.sum(tot * tot), 1e-9)), 3)} for j, sgn in js]}
    res = {"year": a.year, "rows": len(m.rows), "contingencies": int(len(m.cont_all)), "pst_mode": a.pst,
           "outage_topologies": len(groups), "outage_hours": applied_hours,
           "outages_matched": sorted({k[1] for k in oreport if k[0] == "matched"}),
           "outages_unmatched": sorted({k[1] for k in oreport if k[0] == "unmatched"}),
           "neighbour_injection_mean_mw": {c: round(float(U[:, i].mean()), 1) for i, c in enumerate(neigh)},
           "lines_n0_over": sum(r["n0_hours"] > 0 for r in rows if r["kind"] == "line"),
           "lines_n1_over": sum(r["n1_hours"] > 0 for r in rows if r["kind"] == "line"),
           "border": border_out, "rows_detail": rows}
    json.dump(res, open(os.path.join(a.work, "out", f"screening_{a.year}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(m.rows)} monitored branches, {len(m.cont_all)} contingencies, {int(m.pst.sum())} PST circuits; "
          f"{len(groups)} outage topologies over {applied_hours} h; matched {len(res['outages_matched'])} outage elements, "
          f"unmatched {len(res['outages_unmatched'])}")
    print("border check:", {c: (b["model_mean_mw"], b["measured_mean_mw"], b["max_abs_error_mw"]) for c, b in border_out.items()})
    print(f"lines with N-0 > 100%: {res['lines_n0_over']}, with N-1 > 100%: {res['lines_n1_over']}")
    for r in sorted([r for r in rows if r["kind"] == "line"], key=lambda r: -r["n1_hours"])[:30]:
        c = r["n1_contingencies"][0]["name"] if r["n1_contingencies"] else ""
        print(f"{r['n1_hours']:5d} h N-1 | {r['n0_hours']:5d} h N-0 | p95 {r['n1_p95']:.2f} | {r['name']} | {c}")


if __name__ == "__main__":
    main()
