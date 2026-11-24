import requests
EEA = "https://bio.discomap.eea.europa.eu/arcgis/rest/services/ProtectedSites/CDDA_Dyna_WM/MapServer/4/query"
UA = {"User-Agent": "polish-grid land screening (github.com/aragn/polish-grid)"}
r = requests.get(EEA, params={"where": "countryCode='PL'", "outFields": "designationTypeCode,designationType", "returnGeometry": "false", "returnDistinctValues": "true", "f": "json"}, headers=UA, timeout=120)
for f in r.json().get("features", []): print("EEA", f["attributes"])
for code in ("PL01", "PL02", "PL03", "PL04", "PL05", "PL06", "PL07", "PL08", "PL09", "PL10"):
    r = requests.get(EEA, params={"where": f"countryCode='PL' AND designationTypeCode='{code}'", "returnCountOnly": "true", "f": "json"}, headers=UA, timeout=120)
    print("EEA count", code, r.text[:80])
