"""
The app's data model (the Ariadne MVP), built from the files the other jobs write:

    python etl/model.py [--out model]

Writes model/ (not committed; uploaded to the app's private R2 bucket by etl/r2_sync.py):
  sources.json      every dataset the app cites: id, title, publisher, page, as-of date, date read, reuse terms
  groups.json       capacity groups (TAURON, Energa, PGE) and PSE stations: room by year and kind, queue summary, substations
  substations.json  every placed substation: id (the Land tab's station id), operator, voltage, position, groups,
                    TAURON's own medium-voltage room, plots near it
  meta.json         when it was built, the model version, counts, and the source files' own dates

Every figure in the app points at a source id here, so a card, a PDF and the agent cite it the same way.
ENEA and Stoen values are never included (they ask consent for per-station republication).
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from land import station_id  # noqa: E402  the same ids as the Land tab's station files

ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
VERSION = 1
KV = {"tauron": "110", "energa": "110", "pge": "110", "pse": "220/400"}


def load(*parts):
    path = os.path.join(DATA, *parts)
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None


def sources(grid, queue, dso, land, devs, permits):
    out = []
    for op, o in grid["operators"].items():
        if o.get("excluded"):
            continue
        out.append({"id": f"capacity-{op}", "title": f"Available connection capacity ({o['level']})", "publisher": o["name"], "page": o["page"],
                    "asof": o.get("asof"), "read": o.get("read"), "terms": "Public under Energy Law art. 7(8l); name the source"})
    if queue:
        last = max(queue["sources"], key=lambda s: s.get("asof") or "")
        out.append({"id": "queue-pse", "title": "Art. 7(8l) list of projects applying for connection", "publisher": "PSE S.A.", "page": last.get("url"),
                    "asof": queue.get("asof"), "read": queue.get("updated"), "terms": "Public under Energy Law art. 7(8l); name the source"})
    if dso:
        for op, o in (dso.get("operators") or {}).items():
            mine = [s for s in dso["sources"] if s.get("doc") == op]
            last = max(mine, key=lambda s: s.get("asof") or "") if mine else {}
            out.append({"id": f"queue-{op}", "title": "Art. 7(8l) list of applicants, conditions and refusals",
                        "publisher": o.get("name", op) if isinstance(o, dict) else op, "page": last.get("url"), "asof": last.get("asof"),
                        "read": dso.get("updated"), "terms": "Public under Energy Law art. 7(8l); name the source"})
    if land:
        out.append({"id": "plots-egib", "title": "Cadastral plots (EGiB collective service)", "publisher": "GUGiK and county registers", "page": land["source"],
                    "asof": land.get("updated"), "read": land.get("updated"), "terms": "Open data; owner names never fetched"})
    if devs:
        out.append({"id": "ure-registers", "title": "URE licences (ConcessionOtherFuel WEE, MEE) and register of small installations (MIOZERegistry)",
                    "publisher": "Urząd Regulacji Energetyki", "page": "https://api.ure.gov.pl/", "asof": devs.get("built"), "read": devs.get("built"),
                    "terms": "Name the source, the date read and the query; names and tax numbers not kept"})
    if permits:
        out.append({"id": "gunb-permits", "title": "Register of building-permit decisions (RWDZ)", "publisher": "GUNB", "page": "https://wyszukiwarka.gunb.gov.pl/pobranie.html",
                    "asof": permits.get("built"), "read": permits.get("built"), "terms": "Open data; no investor, designer or free text kept"})
    pse = grid["operators"].get("pse", {}).get("plans", {})
    if pse:
        out.append({"id": "pse-prsp", "title": "Transmission development plan PRSP 2025–2034 (and the 2027–2036 draft)", "publisher": "PSE S.A.", "page": pse.get("plan"),
                    "asof": None, "read": grid["operators"]["pse"].get("read"), "terms": "Public"})
    return out


def build(out_dir):
    grid, queue, dso = load("grid_capacity.json"), load("queue.json"), load("queue_dso.json")
    land, devs, permits = load("land", "index.json"), load("developers.json"), load("queue_permits.json")
    land_st = (land or {}).get("stations", {})

    groups, subs = [], {}
    for a in grid["areas"]:
        op = a["op"]
        g = {"id": a["id"], "op": op, "name": a.get("group") if op == "pse" else a["name"], "level": a["level"], "kv": KV.get(op),
             "lat": a.get("lat"), "lon": a.get("lon"), "room": {k: a.get(k) or {} for k in ("gen", "dem", "bess")},
             "queue": {k: (a.get("queue") or {}).get(k) for k in ("n_pending", "n_held", "pending_in", "pending_out", "held_in", "held_out", "storage_mw", "by_tech")},
             "source": f"capacity-{op}", "substations": []}
        if op == "pse":
            g["name"] = a["name"]
            g["pse_group"], g["pse_area"], g["future"] = a.get("group"), a.get("area"), a.get("future", False)
            g["room"]["gen_step1"] = a.get("gen_step1") or {}
        for s in a.get("stations") or []:
            if s.get("lat") is None:
                continue
            sid = station_id(op, s)
            g["substations"].append(sid)
            e = subs.setdefault(sid, {"id": sid, "name": s["name"], "code": s.get("code", ""), "op": op, "kv": KV.get(op), "lat": s["lat"], "lon": s["lon"],
                                      "placed": s.get("how"), "groups": []})
            e["groups"].append(a["id"])
            if s.get("mv_gen") or s.get("mv_dem"):
                e["mv_room"] = {"gen": s.get("mv_gen") or {}, "dem": s.get("mv_dem") or {}, "source": "capacity-tauron"}
            ls = land_st.get(sid)
            if ls:
                e["plots"] = {"n": ls.get("n"), "big": ls.get("big"), "max_ha": ls.get("max_ha"), "source": "plots-egib"}
        groups.append(g)

    srcs = sources(grid, queue, dso, land, devs, permits)
    os.makedirs(out_dir, exist_ok=True)
    files = {"sources.json": srcs, "groups.json": groups, "substations.json": sorted(subs.values(), key=lambda s: s["id"])}
    for name, value in files.items():
        json.dump(value, open(os.path.join(out_dir, name), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    meta = {"built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "version": VERSION, "country": "PL",
            "years": grid["years"], "counts": {k.split(".")[0]: len(v) for k, v in files.items()},
            "inputs": {"grid_capacity": grid.get("updated"), "queue": (queue or {}).get("updated"), "queue_dso": (dso or {}).get("updated"),
                       "land": (land or {}).get("updated"), "developers": (devs or {}).get("built"), "permits": (permits or {}).get("built")},
            "excluded": {op: o["excluded"] for op, o in grid["operators"].items() if o.get("excluded")}}
    json.dump(meta, open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Model v{VERSION}: {meta['counts']} -> {out_dir}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "model"))
    build(ap.parse_args().out)
