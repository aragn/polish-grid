import io, re, time, zipfile, requests
URL = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG/06/0620_GPKG.zip"
UA = {"User-Agent": "Mozilla/5.0"}
h = requests.head(URL, headers=UA, timeout=60)
print("HEAD", h.status_code, h.headers.get("Accept-Ranges"), h.headers.get("Content-Length"))
r = requests.get(URL, headers={**UA, "Range": "bytes=0-99"}, timeout=60)
print("RANGE", r.status_code, len(r.content), r.headers.get("Content-Range"))

class RangeFile(io.RawIOBase):
    def __init__(self, url):
        self.url, self.pos, self.fetched = url, 0, 0
        self.size = int(requests.head(url, headers=UA, timeout=60).headers["Content-Length"])
    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos
    def read(self, n=-1):
        if n is None or n < 0: n = self.size - self.pos
        if n == 0 or self.pos >= self.size: return b""
        end = min(self.pos + n, self.size) - 1
        data = requests.get(self.url, headers={**UA, "Range": f"bytes={self.pos}-{end}"}, timeout=120).content
        self.pos += len(data); self.fetched += len(data)
        return data
    def readinto(self, b):
        d = self.read(len(b)); b[:len(d)] = d; return len(d)

t = time.time()
f = RangeFile(URL)
z = zipfile.ZipFile(io.BufferedReader(f, buffer_size=1 << 20))
names = [n for n in z.namelist() if n.endswith(("OT_SULN_L.gpkg", "OT_KUPG_A.gpkg", "OT_BUZT_P.gpkg", "OT_BUZT_A.gpkg"))]
for n in names:
    data = z.read(n)
    print(n.split("__")[-1], len(data), "bytes")
print("fetched", f.fetched, "of", f.size, "bytes in", round(time.time() - t, 1), "s")

for svc in ("KrajowaIntegracjaUzbrojeniaTerenu", "KrajowaIntegracjaEwidencjiGruntow"):
    try:
        c = requests.get(f"https://integracja.gugik.gov.pl/cgi-bin/{svc}?service=WMS&request=GetCapabilities", timeout=60).text
        print(svc, "layers:", re.findall(r"<Name>([^<]+)</Name>", c)[:40])
        print("  scales:", sorted(set(re.findall(r"<MaxScaleDenominator>([^<]+)<", c)))[:10])
    except Exception as e:
        print(svc, "ERR", e)
