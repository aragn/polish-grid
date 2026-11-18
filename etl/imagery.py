"""
Aerial and satellite images of the projects whose site is known (etl/sites.json),
for the "Satellite image" card on the queue page.  A site given by land parcels
(as listed in GUNB's register of building permits) is outlined and centred on
those parcels, whose shapes come from GUGiK's parcel service (ULDK).

    python etl/imagery.py            # add what is new

Two sources, both free:

* GUGiK orthophotos (Geoportal.gov.pl WMS): the current image and the archive
  (the StandardResolutionTime service), one image per distinct flight since 2020,
  labelled with the year it was in use.  About 0.3 m per pixel.
  mapy.geoportal.gov.pl does not answer from every network (it does from
  GitHub's runners); when it fails, the orthophotos already saved stay.
* Sentinel-2 L2A (EU Copernicus), read from the AWS open-data copy through the
  Earth Search STAC catalogue, no account needed.  The clearest pass of each month
  since January 2024, cut to a square around the site.  10 m pixels.  A pass counts
  as clear when its scene classification shows no cloud, cloud shadow, cirrus or
  snow in a square three times the crop's size (at most 3 km across), and under 0.3%
  of the crop is bright and colourless where the site is not usually so (thin cloud
  the classification misses; ash fields and roofs are grey in most passes and do not
  count, nor do the permit parcels, where new white containers are the point).  Of the
  passes that qualify, each month keeps the one with the least.

Writes data/imagery/<key>/*.jpg and data/imagery.json.  main() returns True when
anything changed.
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
import re

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "data", "imagery")
OUT_JSON = os.path.join(ROOT, "data", "imagery.json")
SITES = os.path.join(HERE, "sites.json")

WMS = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/"
ULDK = "https://uldk.gugik.gov.pl/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
ORTHO_FROM = 2020
ORTHO_PX = 1000
STAC = "https://earth-search.aws.element84.com/v1"
S2_FROM = "2024-01-01"
S2_PX = 480
S2_SCENE_CLOUD = 30                # per cent of the whole 110 km tile: a first filter only
CLEAR_CLASSES = (4, 5, 6, 7)       # vegetation, bare, water, unclassified
CLOUD_CLASSES = (3, 8, 9, 10, 11)  # cloud shadow, cloud medium and high, cirrus, snow
WIDE_MAX_M = 1500                  # the cloud check's square: three times the crop, at most this half-side
                                   # (a larger one catches chimney shadows and plumes at power-plant sites)
GREY_SHARE = 0.003                 # share of the crop grey where the site is not usually grey that marks thin cloud
GREY = 110                       # true-colour value: brighter than this in every band and nearly colourless = cloud


def today():
    return dt.date.today().isoformat()


def bbox(lat, lon, half):
    dlat = half / 111320
    dlon = half / (111320 * math.cos(math.radians(lat)))
    return f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"


def parcel_rings(parcel_id):
    """A parcel's outline from GUGiK's parcel service (ULDK): a list of rings of [lon, lat]
    (outer rings and holes alike; the card draws them even-odd, so holes stay open)."""
    response = requests.get(ULDK, params={"request": "GetParcelById", "id": parcel_id, "result": "geom_wkt", "srid": "4326"},
                            headers=UA, timeout=60)
    response.raise_for_status()
    text = response.text.strip()
    if not text.startswith("0") or "POLYGON" not in text:
        raise RuntimeError(f"ULDK {parcel_id}: {text[:120]}")
    rings = []
    for ring in re.findall(r"\(([^()]+)\)", text.split(";", 1)[-1]):
        points = [[float(v) for v in pair.split()] for pair in ring.split(",")]
        # Poland's latitudes (49-55) are always larger than its longitudes (14-24): order the pair by that.
        rings.append([[round(min(a, b), 6), round(max(a, b), 6)] for a, b in points])
    return rings


def place(site, before):
    """lat, lon, half_m and the parcel outlines for a site: from its parcels when it has them."""
    parcels = site.get("parcels") or []
    if not parcels:
        return {"lat": site["lat"], "lon": site["lon"], "half_m": site.get("half_m", 300)}
    if before and before.get("parcels") == parcels and before.get("outline"):
        outline = before["outline"]
    else:
        outline = {parcel: parcel_rings(parcel) for parcel in parcels}
    points = [pt for rings in outline.values() for ring in rings for pt in ring]
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    lat, lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    extent = max((max(lats) - min(lats)) * 111320, (max(lons) - min(lons)) * 111320 * math.cos(math.radians(lat)))
    half = site.get("half_m") or int(min(900, max(250, extent * 0.8)) // 50 * 50)
    return {"lat": round(lat, 6), "lon": round(lon, 6), "half_m": half, "parcels": parcels, "outline": outline}


def reencode(raw, px):
    """JPEG bytes -> (smaller JPEG bytes, looks blank).  GUGiK returns a white image where it has none."""
    from PIL import Image, ImageStat
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    blank = max(ImageStat.Stat(image).stddev) < 3
    if image.width != px:
        image = image.resize((px, px), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82, optimize=True)
    return buffer.getvalue(), blank


def wms_get(service, lat, lon, half, extra=None):
    params = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": "Raster", "STYLES": "",
              "SRS": "EPSG:4326", "BBOX": bbox(lat, lon, half), "WIDTH": ORTHO_PX, "HEIGHT": ORTHO_PX, "FORMAT": "image/jpeg"}
    params.update(extra or {})
    response = requests.get(WMS + service, params=params, headers=UA, timeout=90)
    response.raise_for_status()
    if "image" not in response.headers.get("Content-Type", ""):
        raise RuntimeError(f"{service}: {response.text[:200]}")
    return response.content


def orthophotos(site, entry, folder):
    """The distinct GUGiK images since ORTHO_FROM.  Returns True when the set changed."""
    lat, lon, half = site["lat"], site["lon"], site.get("half_m", 300)
    seen, found = set(), []
    try:
        for year in range(ORTHO_FROM, dt.date.today().year + 1):
            raw = wms_get("StandardResolutionTime", lat, lon, half, {"TIME": f"{year}-12-01"})
            digest = hashlib.md5(raw).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            small, blank = reencode(raw, ORTHO_PX)
            if not blank:
                found.append({"year": year, "md5": hashlib.md5(small).hexdigest(), "jpg": small})
        small, blank = reencode(wms_get("StandardResolution", lat, lon, half), ORTHO_PX)
        digest = hashlib.md5(small).hexdigest()
        if not blank and digest not in {f["md5"] for f in found}:
            # The current image is newer than anything in the archive service.
            found.append({"year": dt.date.today().year, "md5": digest, "jpg": small, "current": True})
    except Exception as error:   # noqa: BLE001 -- keep what we have
        print(f"  orthophotos for {site['key']}: {error}")
        return False
    entry["ortho_checked"] = today()
    if [(o["year"], o["md5"]) for o in entry.get("ortho", [])] == [(o["year"], o["md5"]) for o in found]:
        return False
    for name in os.listdir(folder):
        if name.startswith("ortho_"):
            os.remove(os.path.join(folder, name))
    entry["ortho"] = []
    for image in found:
        name = f"ortho_{image['year']}{'_current' if image.get('current') else ''}.jpg"
        open(os.path.join(folder, name), "wb").write(image["jpg"])
        entry["ortho"].append({"year": image["year"], "file": name, "md5": image["md5"], **({"current": True} if image.get("current") else {})})
    print(f"  orthophotos for {site['key']}: {[o['year'] for o in entry['ortho']]}")
    return True


def grey_mask(rgb):
    """Pixels of a (3, h, w) true-colour crop that are bright in every band and nearly colourless."""
    import numpy as np
    low, high = rgb.min(axis=0).astype(int), rgb.max(axis=0).astype(int)
    return (low > GREY) & (high - low < 25)


def sentinel2(site, entry, folder):
    """Add the clearest pass of every month not yet filled.  Returns True when a pass was added."""
    try:
        import numpy as np
        import rasterio
        from PIL import Image
        from pystac_client import Client
        from rasterio.warp import transform
        from rasterio.windows import from_bounds
    except ImportError as error:
        print(f"  Sentinel-2 skipped ({error})")
        return False
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    lat, lon, half = site["lat"], site["lon"], site.get("s2_half_m", 500)
    have = {p["date"][:7] for p in entry.get("s2", [])}
    try:
        items = list(Client.open(STAC).search(
            collections=["sentinel-2-l2a"], intersects={"type": "Point", "coordinates": [lon, lat]},
            datetime=f"{S2_FROM}/{today()}", query={"eo:cloud_cover": {"lt": S2_SCENE_CLOUD}}, max_items=2000).items())
    except Exception as error:   # noqa: BLE001
        print(f"  Sentinel-2 search for {site['key']}: {error}")
        return False
    by_month = {}
    for item in items:
        by_month.setdefault(item.datetime.date().isoformat()[:7], []).append(item)
    # Every pass the classification calls clear, in the months still to fill.  Thin cloud the
    # classification misses shows as bright colourless pixels; but some sites are bright and grey
    # of themselves (ash fields, roofs), so only grey pixels that are not usually grey count.
    n = max(60, round(2 * half / 10))   # 10 m pixels
    candidates = {}
    for month in sorted(set(by_month) - have):
        days = set()
        for item in sorted(by_month[month], key=lambda i: i.properties.get("eo:cloud_cover", 100)):
            date = item.datetime.date().isoformat()
            if date in days:
                continue   # the same pass in the next tile
            try:
                with rasterio.open(item.assets["scl"].href) as src:
                    (x,), (y,) = transform("EPSG:4326", src.crs, [lon], [lat])
                    window = lambda h: from_bounds(x - h, y - h, x + h, y + h, src.transform)   # noqa: E731
                    wide = src.read(1, window=window(min(3 * half, WIDE_MAX_M)), boundless=True, fill_value=0)
                    near = src.read(1, window=window(half), boundless=True, fill_value=0)
                if np.isin(near, CLEAR_CLASSES).mean() < 0.99 or np.isin(wide, CLOUD_CLASSES).mean() > 0.002 or (wide == 0).mean() > 0.01:
                    continue
                with rasterio.open(item.assets["visual"].href) as src:
                    rgb = src.read([1, 2, 3], window=from_bounds(x - half, y - half, x + half, y + half, src.transform),
                                   out_shape=(3, n, n), boundless=True, fill_value=0)
            except Exception as error:   # noqa: BLE001
                print(f"  {item.id}: {error}")
                continue
            days.add(date)
            candidates.setdefault(month, []).append((date, item, rgb))
    # Where the site is usually grey: over the passes kept so far (clear by construction) and the new candidates.
    stack = [grey_mask(np.moveaxis(np.asarray(Image.open(os.path.join(folder, kept["file"])).convert("RGB").resize((n, n), Image.BILINEAR)), -1, 0))
             for kept in entry.get("s2", []) if os.path.exists(os.path.join(folder, kept["file"]))]
    stack += [grey_mask(rgb) for cands in candidates.values() for _, _, rgb in cands]
    # "Usually grey": in at least a sixth of the passes (ash fields are grey only when dry), widened by a pixel.
    usual = np.mean(stack, axis=0) >= 1 / 6 if len(stack) >= 6 else np.zeros((n, n), bool)
    usual = usual | np.roll(usual, 1, 0) | np.roll(usual, -1, 0) | np.roll(usual, 1, 1) | np.roll(usual, -1, 1)
    # The permit parcels themselves are left out: new white containers there are what we look for, not cloud.
    points = [pt for rings in (site.get("outline") or {}).values() for ring in rings for pt in ring]
    if points:
        cols = [((p[0] - lon) * 111320 * math.cos(math.radians(lat)) + half) / (2 * half) * n for p in points]
        rows = [(half - (p[1] - lat) * 111320) / (2 * half) * n for p in points]
        c0, c1 = max(0, int(min(cols)) - 3), min(n, int(max(cols)) + 4)
        r0, r1 = max(0, int(min(rows)) - 3), min(n, int(max(rows)) + 4)
        if (c1 - c0) * (r1 - r0) <= 0.4 * n * n:
            usual[r0:r1, c0:c1] = True
    added = []
    for month, cands in sorted(candidates.items()):
        share, date, item, rgb = min(((float((grey_mask(rgb) & ~usual).mean()), date, item, rgb) for date, item, rgb in cands), key=lambda c: c[0])
        if share > GREY_SHARE:
            continue
        name = f"s2_{date}.jpg"
        Image.fromarray(np.moveaxis(rgb, 0, -1)).resize((S2_PX, S2_PX), Image.LANCZOS).save(
            os.path.join(folder, name), "JPEG", quality=85, optimize=True)
        added.append({"date": date, "file": name, "item": item.id, "scene_cloud": round(item.properties.get("eo:cloud_cover", 0), 1)})
    entry["s2_checked"] = today()
    if not added:
        return False
    entry["s2"] = sorted(entry.get("s2", []) + added, key=lambda p: p["date"])
    print(f"  Sentinel-2 for {site['key']}: added {[a['date'] for a in added]}")
    return True


def main():
    sites = json.load(open(SITES, encoding="utf-8"))["sites"]
    try:
        old = {s["key"]: s for s in json.load(open(OUT_JSON, encoding="utf-8"))["sites"]}
    except (OSError, ValueError, KeyError):
        old = {}
    unchecked = lambda r: {k: v for k, v in (r or {}).items() if not k.endswith("_checked")}   # noqa: E731
    out, changed = [], False
    for site in sites:
        print(f"Imagery: {site['key']}")
        folder = os.path.join(OUT_DIR, site["key"])
        os.makedirs(folder, exist_ok=True)
        before = old.get(site["key"])
        try:
            placed = place(site, before)
        except Exception as error:   # noqa: BLE001 -- the parcel service is unreachable: keep the last record
            print(f"  parcels for {site['key']}: {error}")
            if before:
                out.append(before)
            continue
        site = {**site, **placed}
        site["s2_half_m"] = site.get("s2_half_m") or max(500, site["half_m"])
        entry = {k: v for k, v in (before or {}).items() if k in ("ortho", "ortho_checked", "s2", "s2_checked")}
        if before and (before["lat"], before["lon"], before.get("half_m"), before.get("s2_half_m")) != \
                (site["lat"], site["lon"], site["half_m"], site["s2_half_m"]):
            entry = {}   # a corrected location or size: start again
            for name in os.listdir(folder):
                os.remove(os.path.join(folder, name))
        orthophotos(site, entry, folder)
        sentinel2(site, entry, folder)
        record = {k: site[k] for k in ("key", "match", "label", "lat", "lon", "half_m", "s2_half_m", "parcels", "outline", "location", "note") if k in site}
        record.update(entry)
        changed |= unchecked(record) != unchecked(before)
        out.append(record)
    changed |= set(old) != {s["key"] for s in sites}
    if changed:
        json.dump({"updated": today(), "sites": out}, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return changed


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    print("changed" if main() else "no change")
