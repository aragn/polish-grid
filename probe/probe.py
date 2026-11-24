import collections, io, os, sys, zipfile
sys.path.insert(0, "etl")
from land_grid import RangeFile, URL
from pyogrio import raw, read_info
for county in ("0620", "2212", "3021"):          # Zamość, Słupsk area, a wind county in Wielkopolska
    z = zipfile.ZipFile(io.BufferedReader(RangeFile(URL.format(woj=county[:2], county=county)), buffer_size=1 << 20))
    for suffix in ("OT_BUWT_P", "OT_KUPG_A", "OT_BUWT_A"):
        names = [n for n in z.namelist() if n.endswith(f"__{suffix}.gpkg")]
        if not names: print(county, suffix, "none"); continue
        path = f"/tmp/{suffix}.gpkg"; open(path, "wb").write(z.read(names[0]))
        info = read_info(path)
        _, _, _, fields = raw.read(path, read_geometry=False)
        print(county, suffix, info["features"], "features; fields:", [f for f in info["fields"]][:25])
        for name, col in zip(info["fields"], fields):
            vals = collections.Counter(str(v) for v in col)
            if name in ("RODZAJ", "RODZAJELEKTROWNI", "KATEGORIAISTNIENIA", "MOC", "NAZWA") or 1 < len(vals) <= 12:
                if name not in ("WERSJA", "POCZATEKWERSJIOBIEKTU", "OZNACZENIEZMIANY", "ZRODLODANYCHGEOMETRYCZNYCH", "KODKARTO10K"):
                    print("    ", name, dict(vals.most_common(10)))
