"""
Planned and forced outages of transmission elements in Poland and on its
borders for a year, from the ENTSO-E Transparency Platform ("Unavailability
of transmission infrastructure", 10.1.A/B, documentType A78):

    ENTSOE_API_KEY=... python etl/congestion/fetch_outages.py --year 2025 --out outages_2025.json

ENTSO-E answers with a zip of Unavailability_MarketDocuments, at most 200 per
request (offset paging), one request a month per domain pair.  Every outage
keeps its latest revision only; withdrawn ones (docStatus A09) are dropped.

Writes [{mrid, revision, business: planned|forced, start, end (UTC ISO),
assets: [{mrid, name, type}], reason, domains}].  The script needs a key, so
it runs in GitHub Actions (.github/workflows/congestion-outages.yml).
"""

import argparse
import datetime as dt
import io
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

API = "https://web-api.tp.entsoe.eu/api"
PL = "10YPL-AREA-----S"
OTHERS = {"PL": PL, "DE": "10YDE-VE-------2", "CZ": "10YCZ-CEPS-----N", "SK": "10YSK-SEPS-----K",
          "LT": "10YLT-1001A0008Q", "SE4": "10Y1001A1001A47J"}
BUSINESS = {"A53": "planned", "A54": "forced"}
PSR = {"B21": "AC line", "B22": "DC line", "B23": "substation", "B24": "transformer"}


def text(e, path):
    x = e.find(path)
    return x.text.strip() if x is not None and x.text else None


def get(params):
    wait = 10
    for attempt in range(6):
        try:
            r = requests.get(API, params=params, timeout=180)
        except requests.RequestException:
            time.sleep(wait)
            wait *= 2
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(wait)
            wait *= 2
            continue
        return r
    raise RuntimeError("ENTSO-E kept failing")


def documents(r):
    """XML roots in a response: a zip of documents, one document, or an acknowledgement (no data)."""
    if r.content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            return [ET.fromstring(z.read(n)) for n in z.namelist() if n.endswith(".xml")]
    root = ET.fromstring(r.content)
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        reason = " ".join(e.text or "" for e in root.iter() if e.tag.endswith("text"))
        if "No matching data" not in reason:
            print("  ack:", reason.strip()[:200], flush=True)
        return []
    return [root]


def parse(root, domains):
    status = text(root, "{*}docStatus/{*}value")
    out = {"mrid": text(root, "{*}mRID"), "revision": int(text(root, "{*}revisionNumber") or 0),
           "withdrawn": status == "A09", "domains": domains, "assets": [], "reason": None}
    reason = root.find("{*}Reason")
    if reason is not None:
        out["reason"] = " ".join(filter(None, [text(reason, "{*}code"), text(reason, "{*}text")]))
    for ts in root.findall("{*}TimeSeries"):
        out["business"] = BUSINESS.get(text(ts, "{*}businessType"), text(ts, "{*}businessType"))
        sd, st = text(ts, "{*}start_DateAndOrTime.date"), text(ts, "{*}start_DateAndOrTime.time") or "00:00:00Z"
        ed, et = text(ts, "{*}end_DateAndOrTime.date"), text(ts, "{*}end_DateAndOrTime.time") or "00:00:00Z"
        out["start"], out["end"] = f"{sd}T{st}", f"{ed}T{et}"
        # what stays available during the outage (MW): 0 is a disconnection, more is a limit
        qty = [float(q.text) for q in ts.findall(".//{*}Available_Period/{*}Point/{*}quantity") if q.text]
        if qty:
            out["available_min"], out["available_max"] = min(qty), max(qty)
        nominal = text(ts, "{*}production_RegisteredResource.pSRType.powerSystemResources.nominalP")
        if nominal:
            out["nominal"] = float(nominal)
        for a in ts.findall("{*}Asset_RegisteredResource"):
            out["assets"].append({"mrid": text(a, "{*}mRID"), "name": text(a, "{*}name"),
                                  "type": PSR.get(text(a, "{*}asset_PSRType.psrType"), text(a, "{*}asset_PSRType.psrType")),
                                  "location": text(a, "{*}location.name")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        sys.exit("Set ENTSOE_API_KEY.")
    latest = {}
    for name, other in OTHERS.items():
        month = dt.datetime(a.year, 1, 1, tzinfo=dt.timezone.utc)
        while month.year == a.year:
            nxt = (month + dt.timedelta(days=32)).replace(day=1)
            for business in BUSINESS:
                offset = 0
                while True:
                    params = {"securityToken": token, "documentType": "A78", "businessType": business,
                              "In_Domain": PL, "Out_Domain": other, "periodStart": month.strftime("%Y%m%d%H%M"),
                              "periodEnd": nxt.strftime("%Y%m%d%H%M"), "offset": offset}
                    docs = documents(get(params))
                    for root in docs:
                        o = parse(root, f"PL-{name}")
                        if o["mrid"] and (o["mrid"] not in latest or o["revision"] > latest[o["mrid"]]["revision"]):
                            latest[o["mrid"]] = o
                    print(f"{name} {month:%Y-%m} {business} offset {offset}: {len(docs)}", flush=True)
                    if len(docs) < 200:
                        break
                    offset += 200
                    time.sleep(1)
                time.sleep(1)
            month = nxt
    rows = sorted((o for o in latest.values() if not o["withdrawn"]), key=lambda o: o.get("start") or "")
    json.dump(rows, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(f"{len(rows)} outages ({len(latest) - len(rows)} withdrawn)")


if __name__ == "__main__":
    main()
