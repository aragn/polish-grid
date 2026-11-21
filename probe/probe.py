import math, re, requests, time
U = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
def bbox(lat, lon, km):
    dlat = km / 110.574; dlon = km / (111.320 * math.cos(math.radians(lat)))
    return f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon},urn:ogc:def:crs:EPSG::4326"
def ask(b, **extra):
    p = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "ms:dzialki", "SRSNAME": "urn:ogc:def:crs:EPSG::4326", "BBOX": b, **extra}
    t = time.time(); r = requests.get(U, params=p, headers=UA, timeout=300)
    head = r.content[:3000].decode("utf-8", "replace")
    ids = re.findall(rb"<ms:ID_DZIALKI>([^<]*)<", r.content)
    print(extra, r.status_code, len(r.content), re.findall(r'number(?:Matched|Returned)="[^"]*"', head), "members", r.content.count(b"<ms:dzialki"), "ids", len(ids), "unique", len(set(ids)), f"{time.time()-t:.0f}s", flush=True)
    return set(ids)
lat, lon = 50.68329, 22.9858   # Szczebrzeszyn
b = bbox(lat, lon, 3)
ask(b, RESULTTYPE="hits")
a = ask(b, COUNT=1000, STARTINDEX=0)
c = ask(b, COUNT=1000, STARTINDEX=1000)
print("overlap page1/page2", len(a & c))
ask(b, COUNT=10000, STARTINDEX=0)
ask(b)
for km in (1.5, 0.75):
    ask(bbox(lat, lon, km), COUNT=1000, STARTINDEX=0)
    ask(bbox(lat, lon, km), COUNT=1000, STARTINDEX=1000)
