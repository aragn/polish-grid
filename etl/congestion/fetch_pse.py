"""
A year of PSE's public reporting API (api.raporty.pse.pl, no key) for the
congestion screening model:

    python etl/congestion/fetch_pse.py --work D:/Power/pl-congestion --year 2025

  gen-jw          metered output of every centrally dispatched (JWCD) unit, 15 min
  his-wlk-cal     system balance: jg, jnwrb, wi, pv, jgm, swm_p, swm_np, demand
  przeplywy-mocy  metered cross-border flows per section (CZ-PL, PL-CZ, DE-PL, ...)
  poze-redoze     MW of wind and solar PSE curtailed, for balance and for the network

These are the data PSE submits to ENTSO-E (16.1.A per unit, 6.1.A load,
12.1.G physical flows), read at the source so no ENTSO-E key is needed.
Raw days go to <work>/cache/pse/<endpoint>/<date>.json; days on disk are not
fetched again.  PSE pages by a cursor (nextLink) and often fails a cold query
once, so every page is retried.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import time

import requests

API = "https://api.raporty.pse.pl/api/"
ENDPOINTS = {
    "gen-jw": "dtime_utc,power_plant,resource_code,value,operating_mode_eng",
    "his-wlk-cal": "dtime_utc,jg,jnwrb,wi,pv,jgm,swm_p,swm_np,demand",
    "przeplywy-mocy": "dtime_utc,section_code,value",
    "poze-redoze": "dtime_utc,pv_red_balance,pv_red_network,wi_red_balance,wi_red_network",
}


def get(url):
    for attempt in range(6):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(3 + 5 * attempt)
    raise RuntimeError(f"PSE failed: {url}")


def fetch_day(work, endpoint, day):
    path = os.path.join(work, "cache", "pse", endpoint, f"{day}.json")
    if os.path.exists(path):
        return 0
    url = f"{API}{endpoint}?$filter=business_date eq '{day}'&$select={ENDPOINTS[endpoint]}&$first=12000"
    rows = []
    while url:
        page = get(url)
        rows += page.get("value", [])
        url = page.get("nextLink")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--year", type=int, default=2025)
    a = ap.parse_args()
    # Business days are Polish local time: the day before 1 January holds 23:00-24:00 UTC of 31 December.
    first = datetime.date(a.year - 1, 12, 31)
    days = [(first + datetime.timedelta(d)).isoformat() for d in range((datetime.date(a.year, 12, 31) - first).days + 1)]
    jobs = [(e, d) for e in ENDPOINTS for d in days]
    done = 0
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        futures = {pool.submit(fetch_day, a.work, e, d): (e, d) for e, d in jobs}
        for fut in concurrent.futures.as_completed(futures):
            e, d = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                print(f"{e} {d}: {exc}", flush=True)
            done += 1
            if done % 100 == 0:
                print(f"{done}/{len(jobs)}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
