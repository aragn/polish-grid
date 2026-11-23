"""
"What if" screening: add a plant at one substation and see how the year's N-0
and N-1 loading changes on the lines it affects.

    python etl/congestion/whatif.py --work W --bus "Mokre" --tech solar --mw 100 300
    python etl/congestion/whatif.py --work W --bus "Dunowo" --tech battery --mw 100 --hours 2

  --bus     a substation name (or bus id) from network.json; with several
            voltages the highest is taken unless --kv is given
  --tech    solar | wind  hourly profile from the substation's weather cell
                          (atlite capacity factor scaled so the 2025 fleet's
                          modelled energy equals PSE's; inputs.py k_wind/k_solar)
            battery       daily arbitrage on Poland's day-ahead price: charges in
                          the --hours cheapest hours of every Polish day and
                          discharges in the --hours dearest (losses ignored)
  --mw      one or more sizes; each one is screened in full

What balances the new plant each hour:
  - in hours when PSE curtailed wind or solar (poze-redoze, 2,535 hours in 2025),
    up to the MW curtailed: existing wind and solar, curtailed more when the
    plant produces and less when a battery charges, located like the fleet's
    output that hour (the system had too much, so nothing else moves);
  - otherwise, and for the rest: the JWCD thermal units in proportion to their
    output that hour (when none run, the load).
Border flows and the neighbours stay as measured.  The year is screened again
with the same outages, ratings and contingencies as flows.py.

Prints and writes W/out/whatif_<bus>_<tech>.json.
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
from flows import branch_label, outage_groups, screen_year  # noqa: E402

NOT_THERMAL = ("Hydro", "Solar", "wind", "Wind")


def find_bus(m, key, kv=None):
    key_f = key.strip().lower()
    pl = [i for i, b in enumerate(m.buses) if b["country"] == "PL" and not b["boundary"] and not b.get("virtual")]
    hits = [i for i in pl if m.buses[i]["id"] == key or (m.buses[i]["name"] or "").lower() == key_f]
    if not hits:
        hits = [i for i in pl if key_f in (m.buses[i]["name"] or "").lower()]
    if not hits:
        raise SystemExit(f"no substation matches {key!r}")
    if kv:
        hits = [i for i in hits if m.buses[i]["kv"] == kv] or hits
    hits.sort(key=lambda i: -m.buses[i]["kv"])
    return hits[0]


def prices(year, hours):
    price = pd.Series(np.nan, index=hours)
    for month in range(1, 13):
        path = os.path.join(ROOT, "data", "prices", "PL", f"{year}-{month:02d}.json")
        if not os.path.exists(path):
            continue
        d = json.load(open(path, encoding="utf-8"))
        idx = pd.to_datetime(d["start"] + np.arange(len(d["v"])) * d["step"], unit="ms", utc=True)
        s = pd.Series(d["v"], index=idx, dtype="float64").resample("h").mean()  # 15-minute prices since October 2025
        price.update(s[s.index.isin(hours)])
    return price.interpolate(limit_direction="both")


def profile(work, m, bus, tech, hours, duration, year, scale=1.0):
    """Hourly output per MW installed (negative: charging)."""
    b = m.buses[bus]
    if tech in ("solar", "wind"):
        w = np.load(os.path.join(work, "cache", "weather_pl.npz"))
        cell = int(np.argmin((w["y"] - b["lat"]) ** 2 + ((w["x"] - b["lon"]) * math.cos(math.radians(b["lat"]))) ** 2))
        t = pd.to_datetime(w["time"], unit="s", utc=True)
        return np.clip(w[tech][pd.Index(t).get_indexer(hours), cell].astype("float64") * scale, 0, 1)
    price = prices(year, hours)
    p = np.zeros(len(hours))
    local_day = hours.tz_convert("Europe/Warsaw").date
    for day in np.unique(local_day):
        ix = np.where(local_day == day)[0]
        order = ix[np.argsort(price.values[ix], kind="stable")]
        p[order[:duration]] = -1.0
        p[order[-duration:]] = 1.0
    return p


class WhatIf:
    """Everything one screening needs, loaded once (whatif runs many sizes and buses)."""

    def __init__(self, work, year, outages=None):
        self.work, self.year = work, year
        self.m = Model(work)
        self.inj = np.load(os.path.join(work, "out", f"injections_{year}.npz"))
        base = np.load(os.path.join(work, "out", f"flows_{year}.npz"))
        self.hours = pd.to_datetime(self.inj["hours"], unit="s", utc=True)
        self.F0, self.n1_0, self.R = base["F"], base["n1"], base["R"]
        self.groups, _ = outage_groups(self.m, outages, self.hours, self.inj["border"], list(self.inj["neighbours"]))
        plants = json.load(open(os.path.join(ROOT, "output", "plants.json"), encoding="utf-8"))["plants"]
        thermal = np.array([not any(w in plants.get(str(pl), {}).get("source", "") for w in NOT_THERMAL)
                            for pl in self.inj["jwcd_plants"]])
        out_units = np.clip(self.inj["jwcd_units"][:, thermal], 0, None).astype("float64")
        self.sink_buses = np.unique(self.inj["jwcd_bus"][thermal])
        sink = np.zeros((len(self.hours), len(self.sink_buses)))
        for j, ub in enumerate(self.inj["jwcd_bus"][thermal]):
            sink[:, np.searchsorted(self.sink_buses, ub)] += out_units[:, j]
        tot = sink.sum(1, keepdims=True)
        self.sink = sink / np.where(tot > 1, tot, 1)
        self.no_thermal = tot[:, 0] <= 1
        self.pop_share = self.inj["pop_share"]
        # Hours when PSE curtailed wind or solar: the system already had too much, so a new plant's output
        # is matched by curtailing existing wind and solar more (and battery charging by curtailing them
        # less), up to the MW curtailed that hour.  Where that wind and solar sits: the fleet's output that
        # hour (URE capacity and micro-PV times the weather), as in inputs.py.
        self.curt = (self.inj["curt_pv"] + self.inj["curt_wi"]).astype("float64") if "curt_pv" in self.inj else np.zeros(len(self.hours))
        idx = np.where(self.curt > 1.0)[0]
        self.res_pos = np.full(len(self.hours), -1)
        self.res_pos[idx] = np.arange(len(idx))
        if len(idx):
            def share(cf, cap):
                x = cf[idx].astype("float64") @ cap.T.astype("float64")
                s = x.sum(1, keepdims=True)
                return x / np.where(s > 0, s, 1)
            pv, wi = self.inj["curt_pv"][idx, None], self.inj["curt_wi"][idx, None]
            res = pv * share(self.inj["cf_solar"], self.inj["pv_cap"]) + wi * share(self.inj["cf_wind"], self.inj["wind_cap"])
            self.res = (res / self.curt[idx, None]).astype("float32")  # [curtailment hours, bus], rows sum to 1
        else:
            self.res = np.zeros((0, self.m.nb), dtype="float32")
        Pr0 = self.m.state(frozenset())[0]
        self._thF0 = self.sink @ Pr0[:, self.sink_buses].T  # [T, rows]: flow per MW taken from the thermal units
        self._resF0 = self.res @ Pr0.T                      # [curtailment hours, rows]: per MW from wind and solar

    def balance_weight(self, x, tt):
        """Share of each hour's injection x (MW) met by curtailed wind and solar (the rest: thermal units)."""
        c = self.curt[tt]
        return np.where((np.abs(x) > 1e-9) & (c > 1.0), np.minimum(1.0, c / np.maximum(np.abs(x), 1e-9)), 0.0)

    def run(self, bus, p, mw, min_share=0.05):
        m = self.m

        def extra(tt, Pr):
            # flow change per MW: PTDF of the bus less the PTDF of what balances it that hour
            x = p[tt] * mw
            d = Pr[:, bus][None, :] - self.sink[tt] @ Pr[:, self.sink_buses].T
            if self.no_thermal[tt].any():
                d[self.no_thermal[tt]] = Pr[:, bus][None, :] - (self.pop_share @ Pr.T)[None, :]
            a = self.balance_weight(x, tt)
            k = np.where(a > 0)[0]
            if len(k):
                d_res = Pr[:, bus][None, :] - self.res[self.res_pos[tt[k]]] @ Pr.T
                d[k] = a[k, None] * d_res + (1 - a[k, None]) * d[k]
            self._sens = np.maximum(getattr(self, "_sens", 0), np.abs(d).max(0))
            return d * x[:, None]

        # lines that carry min_share of the plant's output in some hour (base topology): only those are screened
        Pr = m.state(frozenset())[0]
        sens = np.abs(Pr[:, bus][None, :] - self._thF0).max(0)
        if len(self.res):
            sens = np.maximum(sens, np.abs(Pr[:, bus][None, :] - self._resF0).max(0))
        subset = np.where(sens >= min_share)[0]
        x_all = p * mw
        w = self.balance_weight(x_all, np.arange(len(p)))
        self._res_share = float((w * np.abs(x_all)).sum() / max(np.abs(x_all).sum(), 1e-9))
        out_, in_ = x_all > 0, x_all < 0
        # of the energy it produces, the share that only adds to curtailment; of a battery's charging, the
        # share that takes wind and solar PSE would otherwise have curtailed
        self._into_curt = float((w * x_all)[out_].sum() / max(x_all[out_].sum(), 1e-9))
        self._from_curt = float((w * -x_all)[in_].sum() / max(-x_all[in_].sum(), 1e-9)) if in_.any() else None
        self._sens = np.zeros(len(m.rows))
        inj = self.inj
        F, n1, arg, _, _ = screen_year(m, inj["P"], inj["gsk"].astype("float64"), list(inj["neighbours"]),
                                       inj["border"].astype("float64"), self.R, self.groups, extra=extra, subset=subset)
        n1 = np.where(np.isin(np.arange(len(m.rows)), subset)[None, :], n1, self.n1_0)
        n0, n0_base = np.abs(F) / self.R, np.abs(self.F0) / self.R
        rows = []
        for j, r in enumerate(m.rows):
            if self._sens[j] < min_share:
                continue
            h0, h1 = int((self.n1_0[:, j] > 1).sum()), int((n1[:, j] > 1).sum())
            d0 = int((n0[:, j] > 1).sum()) - int((n0_base[:, j] > 1).sum())
            if h1 == h0 and d0 == 0 and abs(float(n1[:, j].max() - self.n1_0[:, j].max())) < 0.01:
                continue
            over = n1[:, j] > 1
            cont = np.bincount(arg[over, j]).argmax() if over.any() else None
            rows.append({"j": j, "name": branch_label(m, r), "kv": m.branches[r]["kv"], "kind": m.branches[r]["kind"],
                         "n1_hours_before": h0, "n1_hours_after": h1, "n0_hours_change": d0,
                         "n1_max_before": round(float(self.n1_0[:, j].max()), 3), "n1_max_after": round(float(n1[:, j].max()), 3),
                         "contingency": branch_label(m, int(cont)) if cont is not None else None,
                         "share_of_output_max": round(float(self._sens[j]), 3)})
        rows.sort(key=lambda r: (-(r["n1_hours_after"] - r["n1_hours_before"]), -(r["n1_max_after"] - r["n1_max_before"])))
        added = sum(max(0, r["n1_hours_after"] - r["n1_hours_before"]) for r in rows)
        return {"mw": mw, "added_n1_overload_hours": added, "balanced_by_curtailment": round(self._res_share, 3),
                "output_into_curtailment": round(self._into_curt, 3),
                "charging_from_curtailment": None if self._from_curt is None else round(self._from_curt, 3),
                "branches": rows}


def batch(a):
    """Every Polish 220/400 kV substation (highest voltage of each), 100 MW of each technology:
    W/out/whatif_batch_<year>.json, written as it goes (a rerun resumes)."""
    w = WhatIf(a.work, a.year, a.outages)
    path = os.path.join(a.work, "out", f"whatif_batch_{a.year}.json")
    done = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    seen, stations = set(), []
    for i in sorted(range(len(w.m.buses)), key=lambda i: -w.m.buses[i]["kv"]):
        b = w.m.buses[i]
        if b["country"] != "PL" or b["kv"] < 220 or b.get("virtual") or not b["name"] or b["name"] in seen:
            continue
        seen.add(b["name"])
        stations.append(i)
    if a.limit:
        stations = stations[:a.limit]
    for n, bus in enumerate(stations):
        b = w.m.buses[bus]
        if b["id"] in done:
            continue
        entry = {"name": b["name"], "kv": b["kv"], "lat": b["lat"], "lon": b["lon"], "tech": {}}
        for tech in ("solar", "wind", "battery"):
            scale = float(w.inj[f"k_{tech}"]) if tech in ("solar", "wind") else 1.0
            p = profile(a.work, w.m, bus, tech, w.hours, 2, a.year, scale)
            r = w.run(bus, p, a.batch_mw, a.min_share)
            entry["tech"][tech] = {"full_load_hours": round(float(np.clip(p, 0, None).sum())),
                                   "added_n1_overload_hours": r["added_n1_overload_hours"],
                                   "balanced_by_curtailment": r["balanced_by_curtailment"],
                                   "output_into_curtailment": r["output_into_curtailment"],
                                   "charging_from_curtailment": r["charging_from_curtailment"], "branches": r["branches"][:8]}
        done[b["id"]] = entry
        json.dump(done, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"{n + 1}/{len(stations)} {b['name']} {b['kv']} kV: " +
              ", ".join(f"{t} +{v['added_n1_overload_hours']} h" for t, v in entry["tech"].items()), flush=True)
    print(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--batch", action="store_true", help="every 220/400 kV substation, --batch-mw of each technology")
    ap.add_argument("--batch-mw", type=float, default=100.0)
    ap.add_argument("--limit", type=int, help="batch: only the first N substations (a test)")
    ap.add_argument("--bus")
    ap.add_argument("--kv", type=int)
    ap.add_argument("--tech", choices=["solar", "wind", "battery"])
    ap.add_argument("--mw", type=float, nargs="+")
    ap.add_argument("--hours", type=int, default=2, help="battery: hours of charging and of discharging a day")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--outages", help="the outages JSON flows.py used")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-share", type=float, default=0.05,
                    help="list only branches that carry at least this share of the new plant's output in some hour")
    a = ap.parse_args()
    if a.batch:
        return batch(a)
    if not (a.bus and a.tech and a.mw):
        ap.error("--bus, --tech and --mw (or --batch)")
    w = WhatIf(a.work, a.year, a.outages)
    bus = find_bus(w.m, a.bus, a.kv)
    scale = float(w.inj[f"k_{a.tech}"]) if a.tech in ("solar", "wind") else 1.0
    p = profile(a.work, w.m, bus, a.tech, w.hours, a.hours, a.year, scale)
    b = w.m.buses[bus]
    result = {"bus": b, "tech": a.tech, "battery_hours": a.hours if a.tech == "battery" else None,
              "full_load_hours": round(float(np.clip(p, 0, None).sum()), 1), "min_share": a.min_share, "sizes": []}
    print(f"{b['name']} {b['kv']} kV, {a.tech}: {result['full_load_hours']} full-load hours")
    for mw in a.mw:
        r = w.run(bus, p, mw, a.min_share)
        result["sizes"].append(r)
        print(f"\n+{mw:g} MW: N-1 overload hours added on branches carrying {a.min_share:.0%}+ of its output: {r['added_n1_overload_hours']}")
        for x in r["branches"][:a.top]:
            print(f"  {x['n1_hours_before']:5d} -> {x['n1_hours_after']:5d} h  max {x['n1_max_before']:.2f} -> {x['n1_max_after']:.2f}"
                  f"  {x['name']}  [{x['contingency']}]")
    slug = re.sub(r"\W+", "_", b["name"]).strip("_").lower()
    path = os.path.join(a.work, "out", f"whatif_{slug}_{a.tech}.json")
    json.dump(result, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n" + path)


if __name__ == "__main__":
    main()
