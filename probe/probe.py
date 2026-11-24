import json, os, subprocess, sys, time, glob, collections
# Map the stations of one group (pge-35, Szczebrzeszyn) only: stub the index to them.
idx = json.load(open("data/land/index.json", encoding="utf-8"))
keep = {k: v for k, v in idx["stations"].items() if "pge-35" in v["groups"] or "energa-13" in v["groups"]}
print("stations", len(keep))
idx["stations"] = keep
json.dump(idx, open("data/land/index.json", "w", encoding="utf-8"))
t = time.time()
subprocess.run([sys.executable, "etl/land_grid.py", "--budget-minutes", "25"], check=True)
print("took", round(time.time() - t), "s")
for f in sorted(glob.glob("data/land/grid/*.json"))[:30]:
    d = json.load(open(f, encoding="utf-8"))
    kinds = collections.Counter(s[0] for s in d["subs"])
    print(os.path.basename(f), os.path.getsize(f), {k: len(v) for k, v in d["lines"].items()}, dict(kinds), [s[1] for s in d["subs"] if s[1]][:4])
