import json, glob, os, statistics, subprocess, sys, time
t = time.time()
subprocess.run([sys.executable, "etl/land_constraints.py", "--county", "0620", "--budget-minutes", "30"], check=True)
print("took", round(time.time() - t), "s")
idx = json.load(open("data/land/index.json", encoding="utf-8"))
keys = idx.get("distance_keys")
print("keys", keys)
seen = {}
for f in glob.glob("data/land/*.json"):
    if os.path.basename(f) in ("index.json", "protected.json", "protected_shapes.json", "zoning.json"): continue
    for p in json.load(open(f, encoding="utf-8"))["plots"]:
        if p["id"].startswith("0620") and "d" in p: seen[p["id"]] = p
print("plots with d:", len(seen))
for k, key in enumerate(keys):
    vals = [p["d"][k] for p in seen.values()]
    have = [v for v in vals if v is not None]
    print(f"  {key:15s} within 3 km: {len(have):5d}/{len(vals)}  zero: {sum(1 for v in have if v == 0):5d}  median: {statistics.median(have) if have else '-'}")
ex = list(seen.values())[:2]
for p in ex: print(p["id"], p["bm"], dict(zip(keys, p["d"])))
