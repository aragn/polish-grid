import json, subprocess, sys, glob, os, collections
idx = json.load(open("data/land/index.json", encoding="utf-8"))
groups = {"pge-35"} | {g for sid, s in idx["stations"].items() if sid.startswith("energa-slupsk") for g in s["groups"]}
print("groups", groups)
idx["stations"] = {k: v for k, v in idx["stations"].items() if set(v["groups"]) & groups}
json.dump(idx, open("data/land/index.json", "w", encoding="utf-8"))
subprocess.run([sys.executable, "etl/land_grid.py", "--all", "--budget-minutes", "25"], check=True)
for f in sorted(glob.glob("data/land/grid/*.json")):
    d = json.load(open(f, encoding="utf-8"))
    print(os.path.basename(f), os.path.getsize(f), "plants", collections.Counter(p[0] for p in d["plants"]), "turbines", len(d["turbines"]), "built", sum(t[3] for t in d["turbines"]))
