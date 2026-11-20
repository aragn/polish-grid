"""
Hourly nodal injections for a year on the 220/400 kV model:

    python etl/congestion/inputs.py --work D:/Power/pl-congestion --ure <URE "Mapa OZE" xlsx> --year 2025

Reads <work>/out/network.json (network.py), <work>/cache/pse/ (fetch_pse.py)
and <work>/cache/weather_pl.npz (weather.py).  Writes
<work>/out/injections_<year>.npz: P[t, bus] in MW, positive into the grid,
with load already netted, plus the components for the report.

Rules (README, "Injections"):
  JWCD units      PSE gen-jw per unit, at the substation of its plant; the
                  digit in the resource code is the connection voltage
                  (2 = 220 kV, 4 = 400 kV, 1 = 110 kV: nearest 110 kV-feeding
                  substation).  Pumping is negative.
  wind            PSE's national wind output (wi), split every hour in
                  proportion to URE's licensed wind capacity at each place times
                  the ERA5 capacity factor of its cell.
  solar           PSE's national PV (pv) less the JWCD PV unit, split by URE's
                  licensed PV plus micro-installations (not in URE's register,
                  spread by population), times the SARAH-3 capacity factor.
  other           the rest of PSE's generation (jg + jnwrb less JWCD, wind and
                  solar: CHP, industrial and small units, hydro, biomass),
                  spread by population.
  load            PSE's system demand, spread by population.
  borders         PSE's metered flows per section, net per neighbour, at the
                  neighbour's boundary node (SwePol at Słupsk).
  closing         what is left (metering differences) goes onto the load.
Every population or capacity share lands on the nearest substation that
transforms to 110 kV.
"""

import argparse
import collections
import datetime
import glob
import json
import math
import os
import re

import numpy as np
import openpyxl
import pandas as pd
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

SHARE_K, SHARE_KM = 3, 40.0
MICRO_PV_MW = 13000.0  # micro-installations (<50 kW, mostly prosumers), mid-2025; only the spatial split depends on it

# Plant sites (lat, lon).  Units connect to the nearest bus of the voltage in their resource code.
PLANTS = {
    "Bełchatów": (51.266, 19.330), "Chorzów": (50.290, 18.950), "Dolna Odra": (53.205, 14.462), "Gryfino": (53.205, 14.462),
    "EC Czechnica-2": (51.030, 17.150), "EC Rzeszów": (50.030, 22.030), "EC Siekierki": (52.190, 21.090),
    "EC Stalowa Wola": (50.570, 22.050), "EC Wrotków": (51.210, 22.550), "EC Włocławek": (52.680, 19.140),
    "EC Łódź-4": (51.740, 19.530), "EC Żerań 2": (52.300, 20.990), "Jaworzno 2 JWCD": (50.210, 19.250),
    "Jaworzno 3": (50.210, 19.250), "Karolin 2": (52.450, 16.990), "Katowice": (50.260, 19.040),
    "Kozienice 1": (51.665, 21.465), "Kozienice 2": (51.665, 21.465), "Kraków Łęg": (50.070, 20.010),
    "Opole": (50.745, 17.885), "Ostrołęka B": (53.090, 21.570), "Porąbka Żar": (49.800, 19.220),
    "Połaniec": (50.430, 21.290), "Połaniec 2-Pasywna": (50.430, 21.290), "Pątnów 1": (52.300, 18.250),
    "Pątnów 2": (52.300, 18.250), "Płock": (52.580, 19.700), "Rybnik": (50.120, 18.510), "Siersza": (50.200, 19.460),
    "Skawina": (50.030, 19.830), "Turów": (50.950, 14.910), "Wrocław": (51.120, 17.050), "Zielona Góra": (51.960, 15.520),
    "Zwartowo": (54.640, 17.640), "Łagisza": (50.350, 19.140), "Łaziska 3": (50.140, 18.850), "Żarnowiec": (54.720, 18.080),
    "Adamów": (52.020, 18.540), "Baltic Power": (54.775, 17.915), "Ostrołęka C": (53.090, 21.570),
}
# Where the nearest real substation is not the plant's: Bełchatów's switchyard is an OSM tee point
# with many short circuits to Rogowiec, while the mine's substation is nearer.
PLANT_BUS = {"Bełchatów": "Rogowiec"}
JWCD_PV = {"Zwartowo"}
JWCD_WIND = {"Baltic Power"}
SECTIONS = {"DE": "X_DE", "CZ": "X_CZ", "SK": "X_SK", "LT": "X_LT", "UA": "X_UA", "SE": "X_SE"}


def km(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371 * math.hypot(x, math.radians(lat2 - lat1))


def fold(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def hour_index(year):
    return pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", inclusive="left", tz="UTC")


def read_pse(work, endpoint):
    rows = []
    for f in sorted(glob.glob(os.path.join(work, "cache", "pse", endpoint, "*.json"))):
        rows += json.load(open(f, encoding="utf-8"))
    df = pd.DataFrame(rows)
    # dtime_utc is the end of the quarter-hour.
    df["hour"] = (pd.to_datetime(df["dtime_utc"]).dt.tz_localize("UTC") - pd.Timedelta(minutes=15)).dt.floor("h")
    return df


class Places:
    def __init__(self):
        geo = json.load(open(os.path.join(ROOT, "geo", "poland_voivodeships.json"), encoding="utf-8"))
        self.voiv = {fold(f["properties"]["name"]): shape(f["geometry"]).buffer(0) for f in geo["features"]}
        self.poland = None
        for g in self.voiv.values():
            self.poland = g if self.poland is None else self.poland.union(g)
        self.by_name = collections.defaultdict(list)
        for name, lat, lon, kind in json.load(open(os.path.join(ROOT, "etl", "osm_places.json"), encoding="utf-8"))["places"]:
            self.by_name[fold(name)].append((lat, lon, kind))

    def locate(self, town, gmina, voiv):
        """(lat, lon, how) of a URE installation: the town inside its voivodeship, nearest the gmina's seat."""
        poly = self.voiv.get(fold(voiv))
        if poly is None:
            return None
        inside = lambda cands: [c for c in cands if poly.contains(Point(c[1], c[0]))]
        seat_name = re.sub(r"^(m\.|gm\.)\s*", "", fold(gmina).split(" – ")[0].split(" - ")[0])
        seats = inside(self.by_name.get(seat_name, []))
        towns = inside(self.by_name.get(fold(re.split(r"[,;]", str(town))[0]), []))
        if towns:
            if len(towns) > 1 and seats:
                towns.sort(key=lambda c: km(c[0], c[1], seats[0][0], seats[0][1]))
            return towns[0][0], towns[0][1], "town"
        if seats:
            return seats[0][0], seats[0][1], "gmina"
        p = poly.representative_point()
        return p.y, p.x, "voivodeship"


def read_ure(path, places):
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    out, how = [], collections.Counter()
    for r in list(ws.iter_rows(values_only=True))[1:]:
        if not isinstance(r[0], (int, float)) or r[6] is None or r[7] not in ("WIL", "PVA"):
            continue
        loc = places.locate(r[2], r[5], r[3])
        if loc is None:
            continue
        how[(r[7], loc[2])] += float(r[6])
        out.append((r[7], float(r[6]), loc[0], loc[1]))
    return out, how


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--ure", required=True, help="URE 'Mapa OZE' register (xlsx), e.g. as of 30 June of the year")
    ap.add_argument("--year", type=int, default=2025)
    a = ap.parse_args()
    hours = hour_index(a.year)
    T = len(hours)

    net = json.load(open(os.path.join(a.work, "out", "network.json"), encoding="utf-8"))
    buses = net["buses"]
    bidx = {b["id"]: i for i, b in enumerate(buses)}
    load_buses = [i for i, b in enumerate(buses) if b["feeds110"]]
    lb_xy = np.array([[buses[i]["lat"], buses[i]["lon"]] for i in load_buses])

    def nearest(lat, lon, kv=None, name=None):
        if kv is None:
            d = (lb_xy[:, 0] - lat) ** 2 + ((lb_xy[:, 1] - lon) * math.cos(math.radians(lat))) ** 2
            return load_buses[int(np.argmin(d))]
        # Plants connect to a real substation: OSM's virtual tee points at plant switchyards keep only some
        # of the circuits, which would bottle the plant up behind one line.
        cand = [i for i, b in enumerate(buses) if b["kv"] == kv and not b["id"].startswith("virtual")
                and (name is None or b["name"] == name)]
        return min(cand, key=lambda i: km(lat, lon, buses[i]["lat"], buses[i]["lon"]))

    def share(lat, lon):
        """The 110 kV grid is meshed: a place feeds its SHARE_K nearest 110 kV-feeding substations within
        SHARE_KM (always at least the nearest), weighted by 1 / distance^2."""
        d = np.array([km(lat, lon, x[0], x[1]) for x in lb_xy])
        order = np.argsort(d)[:SHARE_K]
        order = [o for j, o in enumerate(order) if j == 0 or d[o] <= SHARE_KM]
        wgt = 1.0 / np.maximum(d[order], 3.0) ** 2
        v = np.zeros(len(buses))
        v[[load_buses[o] for o in order]] = wgt / wgt.sum()
        return v

    # ---- weather cells inside Poland
    w = np.load(os.path.join(a.work, "cache", "weather_pl.npz"))
    places = Places()
    in_pl = np.array([places.poland.contains(Point(x, y)) for x, y in zip(w["x"], w["y"])])
    cells = np.where(in_pl)[0]
    wtime = pd.to_datetime(w["time"], unit="s", utc=True)
    pos = pd.Index(wtime).get_indexer(hours)
    assert (pos >= 0).all(), "weather cutout does not cover the year"
    cf_wind, cf_solar = w["wind"][pos][:, cells], w["solar"][pos][:, cells]
    cx, cy = w["x"][cells], w["y"][cells]
    pop = np.nan_to_num(w["pop"][cells])
    cell_share = np.array([share(y, x) for x, y in zip(cx, cy)])  # [cell, bus]
    cell_bus = cell_share.argmax(axis=1)

    def cell_of(lat, lon):
        return int(np.argmin((cy - lat) ** 2 + ((cx - lon) * math.cos(math.radians(lat))) ** 2))

    nb = len(buses)

    def spread(weights_cell):
        """bus share vector from a weight per cell."""
        v = weights_cell @ cell_share
        return v / v.sum()

    pop_share = spread(pop)

    # ---- URE installations -> (bus, cell) capacity matrices
    ure, how = read_ure(a.ure, places)
    cap = {"WIL": np.zeros((nb, len(cells))), "PVA": np.zeros((nb, len(cells)))}
    for kind, mw, lat, lon in ure:
        cap[kind][:, cell_of(lat, lon)] += mw * share(lat, lon)
    micro = (cell_share * (MICRO_PV_MW * pop / pop.sum())[:, None]).T  # [bus, cell]

    def weather_split(capmat, cf):
        """hourly bus shares: capacity x capacity factor, normalised every hour."""
        x = cf @ capmat.T  # [T, nb]
        s = x.sum(1, keepdims=True)
        s[s == 0] = 1
        return x / s

    wind_share = weather_split(cap["WIL"], cf_wind)
    solar_share = weather_split(cap["PVA"] + micro, cf_solar)

    # ---- PSE system balance
    sysb = read_pse(a.work, "his-wlk-cal")
    num = ["jg", "jnwrb", "wi", "pv", "jgm", "swm_p", "swm_np", "demand"]
    sysb[num] = sysb[num].apply(pd.to_numeric, errors="coerce")
    sysh = sysb.groupby("hour")[num].mean().reindex(hours)
    gaps = int(sysh["demand"].isna().sum())
    sysh = sysh.interpolate(limit_direction="both")

    # ---- JWCD units
    jw = read_pse(a.work, "gen-jw")
    jw["value"] = pd.to_numeric(jw["value"], errors="coerce").fillna(0.0)
    unit_h = jw.groupby(["resource_code", "hour"])["value"].mean().unstack(0).reindex(hours).fillna(0.0)
    unit_plant = jw.groupby("resource_code")["power_plant"].first()
    P = np.zeros((T, nb))
    units, missing = [], []
    for code in unit_h.columns:
        plant = unit_plant[code]
        if plant not in PLANTS:
            missing.append(plant)
            continue
        # "BEL 2-02", "KOZ12S01", "JW2_4-07", "ZWA_1P03": the digit just before the separator is the voltage.
        m = re.search(r"(\d)[-SP]\d+$", code)
        digit = m.group(1) if m else "1"
        lat, lon = PLANTS[plant]
        kv = {"2": 220, "4": 400}.get(digit)
        bus = nearest(lat, lon, kv, PLANT_BUS.get(plant) if kv else None)
        P[:, bus] += unit_h[code].values
        units.append({"code": code, "plant": plant, "bus": buses[bus]["id"], "bus_name": buses[bus]["name"],
                      "km": round(km(lat, lon, buses[bus]["lat"], buses[bus]["lon"]), 1),
                      "twh": round(float(unit_h[code].clip(lower=0).sum()) / 1e6, 3)})
    jw_gen = unit_h.clip(lower=0).sum(axis=1).values
    jw_pv = unit_h[[c for c in unit_h.columns if unit_plant[c] in JWCD_PV]].clip(lower=0).sum(axis=1).values
    jw_wind = unit_h[[c for c in unit_h.columns if unit_plant[c] in JWCD_WIND]].clip(lower=0).sum(axis=1).values

    wind = np.clip(sysh["wi"].values - jw_wind, 0, None)
    solar = np.clip(sysh["pv"].values - jw_pv, 0, None)
    other = sysh["jg"].values + sysh["jnwrb"].values - jw_gen - wind - solar
    P += wind[:, None] * wind_share + solar[:, None] * solar_share + np.clip(other, 0, None)[:, None] * pop_share
    P -= sysh["demand"].values[:, None] * pop_share
    # How far the weather model overshoots: PSE's energy over the fleet's modelled energy (whatif.py scales by it).
    k_wind = float(wind.sum() / (cf_wind @ cap["WIL"].sum(0)).sum())
    k_solar = float(solar.sum() / (cf_solar @ (cap["PVA"] + micro).sum(0)).sum())

    # ---- borders
    fl = read_pse(a.work, "przeplywy-mocy")
    fl["value"] = pd.to_numeric(fl["value"], errors="coerce").fillna(0.0)
    fl["nb"] = fl["section_code"].str.replace("PL", "").str.strip("-")
    # Both directions of a border are listed every quarter-hour (one of them 0): add them, then average the quarters.
    fl["nb"] = fl["nb"].replace({"UA_DOB": "UA"})
    quarter = fl.groupby(["dtime_utc", "hour", "nb"])["value"].sum().reset_index()
    border = quarter.groupby(["hour", "nb"])["value"].mean().unstack(1).reindex(hours).interpolate(limit_direction="both").fillna(0.0)
    hv = {h["neighbour"]: h["pl_ac_bus"] for h in net["hvdc"]}
    for c, node in SECTIONS.items():
        if c not in border.columns:
            continue
        target = bidx.get(node) if node in bidx else bidx.get(hv.get(node))
        P[:, target] += border[c].values
    net_import = border.sum(axis=1).values

    # ---- closing: metering differences onto the load
    mismatch = P.sum(1)
    P -= mismatch[:, None] * pop_share

    os.makedirs(os.path.join(a.work, "out"), exist_ok=True)
    np.savez_compressed(os.path.join(a.work, "out", f"injections_{a.year}.npz"), P=P.astype("float32"),
                        hours=(hours - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1), demand=sysh["demand"].values, wind=wind, solar=solar, other=other,
                        jwcd=jw_gen, net_import=net_import, swm=(sysh["swm_p"] + sysh["swm_np"]).values, mismatch=mismatch,
                        border=border.values, border_names=np.array(list(border.columns)),
                        pop_share=pop_share, k_wind=k_wind, k_solar=k_solar, cell_x=cx, cell_y=cy, cell_bus=cell_bus,
                        jwcd_bus=np.array([bidx[u["bus"]] for u in units]),
                        jwcd_units=unit_h[[u["code"] for u in units]].values.astype("float32"),
                        jwcd_codes=np.array([u["code"] for u in units]), jwcd_plants=np.array([u["plant"] for u in units]))
    meta = {"year": a.year, "hours": T, "pse_gap_hours": gaps, "units": units, "unplaced_plants": sorted(set(missing)),
            "ure_mw_by_how": {f"{k[0]}|{k[1]}": round(v) for k, v in how.items()},
            "micro_pv_mw": MICRO_PV_MW, "cells_in_poland": int(len(cells)),
            "weather_scale": {"wind": round(k_wind, 3), "solar": round(k_solar, 3)},
            "fleet_mw": {"wind_ure": round(float(cap["WIL"].sum())), "pv_ure": round(float(cap["PVA"].sum())),
                         "pv_micro": MICRO_PV_MW},
            "twh": {k: round(float(np.nansum(v)) / 1e6, 2) for k, v in
                    {"demand": sysh["demand"].values, "jwcd": jw_gen, "wind": wind, "solar": solar, "other": other,
                     "net_import_sections": net_import, "net_import_swm": (sysh["swm_p"] + sysh["swm_np"]).values}.items()},
            "mismatch_mw": {"mean": round(float(np.mean(mismatch)), 1), "mean_abs": round(float(np.mean(np.abs(mismatch))), 1),
                            "p99_abs": round(float(np.percentile(np.abs(mismatch), 99)), 1)},
            "other_negative_hours": int((other < 0).sum())}
    json.dump(meta, open(os.path.join(a.work, "out", f"injections_{a.year}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in meta.items() if k != "units"}, ensure_ascii=False, indent=1))
    for u in units:
        if u["km"] > 8:
            print("far:", u)


if __name__ == "__main__":
    main()
