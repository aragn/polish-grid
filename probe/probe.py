import collections, os, zipfile, requests, tempfile
from pyogrio import read_info, raw
url = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG/06/0620_GPKG.zip"
d = tempfile.mkdtemp(); path = os.path.join(d, "c.zip")
with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=600, stream=True) as r:
    r.raise_for_status()
    with open(path, "wb") as f:
        for ch in r.iter_content(1 << 20): f.write(ch)
z = zipfile.ZipFile(path)
names = sorted(n for n in z.namelist() if n.endswith(".gpkg"))
print("LAYERS", len(names))
for n in names:
    print("  ", n.split("__")[-1], round(z.getinfo(n).file_size / 1e6, 1), "MB")
want = ["OT_BUBD_A", "OT_SKDR_L", "OT_SKTR_L", "OT_SULN_L", "OT_PTWP_A", "OT_SWRS_L", "OT_PTZB_A", "OT_PTLZ_A", "OT_KUSK_A", "OT_KUHU_A", "OT_KUPG_A", "OT_KUKO_A", "OT_BUIN_L", "OT_BUZT_P", "OT_PTKM_A", "OT_PTTR_A", "OT_PTGN_A", "OT_PTUT_A", "OT_PTSO_A", "OT_PTNZ_A", "OT_PTRK_A", "OT_PTPL_A", "OT_KUOS_A", "OT_KUIK_A", "OT_KUMN_A", "OT_KUHI_A", "OT_KUSC_A", "OT_TCON_A", "OT_TCPK_A", "OT_TCPN_A", "OT_TCRZ_A", "OT_OIKM_P"]
for w in want:
    m = [n for n in names if n.endswith(f"__{w}.gpkg")]
    if not m:
        print("MISSING", w); continue
    z.extract(m[0], d); p = os.path.join(d, m[0])
    info = read_info(p)
    print("==", w, info["features"], "features; fields:", list(info["fields"]))
    _, _, _, fields = raw.read(p, read_geometry=False)
    for fname, col in zip(info["fields"], fields):
        vals = collections.Counter(str(v) for v in col)
        if 1 < len(vals) <= 40 and fname.lower() not in ("lokalnyid", "wersjaid"):
            print("   ", fname, dict(vals.most_common(15)))
    os.remove(p)
