"""
Hourly wind and solar capacity factors per weather cell over Poland, and the
population per cell, from a PyPSA-Eur/atlite cutout:

    python etl/congestion/weather.py --cutout europe-2025-sarah3-era5.nc --pop pop_layout_total.nc --work D:/Power/pl-congestion

Needs atlite (run it in PyPSA-Eur's environment).  The cutout is ERA5 wind
with SARAH-3 irradiance on a 0.3 degree grid (data.pypsa.org, "Copyright (c)
(2025) EUMETSAT" for SARAH-3); pop_layout_total.nc is PyPSA-Eur's population
on the same grid.  Only the shapes matter later: every hour is rescaled to
PSE's national wind and solar output, so the turbine and panel choice only
moves output between places, not its total.

Writes <work>/cache/weather_pl.npz: x, y (cell centres), time (UTC, hour
starting), wind[t, cell], solar[t, cell], pop[cell].
"""

import argparse
import os

import atlite
import numpy as np
import xarray as xr

BOX = dict(x=slice(13.8, 24.4), y=slice(48.8, 55.2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutout", required=True)
    ap.add_argument("--pop", required=True)
    ap.add_argument("--work", required=True)
    a = ap.parse_args()
    cutout = atlite.Cutout(a.cutout).sel(**BOX)
    wind = cutout.wind(turbine="Vestas_V112_3MW", capacity_factor_timeseries=True)
    solar = cutout.pv(panel="CSi", orientation="latitude_optimal", capacity_factor_timeseries=True)
    pop = xr.open_dataarray(a.pop).sel(**BOX)
    x, y = np.meshgrid(cutout.data.x.values, cutout.data.y.values)
    w = wind.transpose("time", "y", "x").values.reshape(len(wind.time), -1).astype("float32")
    s = solar.transpose("time", "y", "x").values.reshape(len(solar.time), -1).astype("float32")
    p = pop.transpose("y", "x").values.reshape(-1)
    # 2 m air temperature (ERA5, kelvin in the cutout) for the lines' thermal ratings
    temp = (cutout.data["temperature"].transpose("time", "y", "x").values.reshape(len(wind.time), -1) - 273.15).astype("float32")
    os.makedirs(os.path.join(a.work, "cache"), exist_ok=True)
    np.savez_compressed(os.path.join(a.work, "cache", "weather_pl.npz"), x=x.reshape(-1), y=y.reshape(-1),
                        time=wind.time.values.astype("datetime64[s]").astype("int64"), wind=w, solar=s, pop=p, temp=temp)
    print(w.shape, float(w.mean()), float(s.mean()), float(np.nansum(p)))


if __name__ == "__main__":
    main()
