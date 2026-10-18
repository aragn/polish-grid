"""
PSE's connection-queue files: where they are, how to fetch every version,
and how each one becomes a dated snapshot.

PSE publishes on
https://www.pse.pl/obszary-dzialalnosci/krajowy-system-elektroenergetyczny/wykaz-obiektow-planowanych-do-przylaczenia
Since the data for July 2026 it is one Excel file a month whose name
carries the date ("..._stan_na_31_08_2026.xlsx").  Until June 2026 it was
four files with fixed names, overwritten every month.

The site runs Liferay, which keeps every upload: "<file>?version=1.N"
returns the N-th one (with its upload time as Last-Modified).  That is the
archive this project is built from: the main list monthly since 2017 (as
scans until February 2020, as text from March 2020), the application
lists since 2024.

PSE's firewall answers 401 after about a hundred quick requests, so every
request here waits, and a 401 means a few minutes' pause, not a failure.
"""

import hashlib
import json
import os
import re
import time

import requests

import pse_parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(ROOT, "archive")
SNAPSHOTS = os.path.join(ROOT, "data", "snapshots")
SOURCES = os.path.join(ROOT, "data", "sources.json")

PAGE = "https://www.pse.pl/obszary-dzialalnosci/krajowy-system-elektroenergetyczny/wykaz-obiektow-planowanych-do-przylaczenia"
BASE = "https://www.pse.pl/documents/20182/51490/"

# The fixed-name files of the old layout: (doc, title, extension we parse).
LEGACY = [
    ("wykaz", "Wykaz_podmiotow_ubiegajacych_sie_o_przylaczenie", "pdf"),
    ("kompletne", "Informacja_na_temat_zlozonych_wnioskow_o_okreslenie_warunkow_przylaczenia_do_sieci_przesylowej_dla_ktorych_potwierdzono_kompletnosc", "pdf"),
    ("kompletne", "Informacja_na_temat_zlozonych_wnioskow_o_okreslenie_warunkow_przylaczenia_do_sieci_przesylowej_dla_ktorych_potwierdzono_kompletnosc", "xlsx"),
    ("weryfikacja", "Informacja_na_temat_wnioskow_o_okreslenie_warunkow_przylaczenia_do_sieci_przesylowej_oczekujacych_na_weryfikacje", "pdf"),
    ("weryfikacja", "Informacja_na_temat_wnioskow_o_okreslenie_warunkow_przylaczenia_do_sieci_przesylowej_oczekujacych_na_weryfikacje", "xlsx"),
    ("odmowy", "Wykaz_wydanych_odmow_okreslenia_warunkow_przylaczenia_do_sieci_od_01012023", "pdf"),
    ("odmowy", "Wykaz_wydanych_odmow_okreslenia_warunkow_przylaczenia_do_sieci_od_01012023", "xlsx"),
]
ART7 = re.compile(r"Informacje_publikowane_zgodnie_z_Art_7_ust_8l_ustawy_PE_stan_na_(\d{2})_(\d{2})_(\d{4})\.xlsx")
# Where both exist for a month, the spreadsheet wins over the PDF.
PREFERENCE = {"xlsx": 0, "xls": 1, "pdf": 2}

DELAY = 2.0
_session = None


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = "polish-grid (https://github.com/aragn) queue history; contact via GitHub"
    return _session


def get(url):
    for _ in range(8):
        response = session().get(url, timeout=90)
        time.sleep(DELAY)
        if response.status_code in (401, 403, 429, 503):
            print(f"  PSE answered {response.status_code}; waiting 3 minutes", flush=True)
            time.sleep(180)
            continue
        return response
    raise RuntimeError(f"PSE kept refusing {url}")


def is_file(response):
    return response.status_code == 200 and not response.headers.get("Content-Type", "").startswith("text/html")


def load_sources():
    return json.load(open(SOURCES, encoding="utf-8")) if os.path.exists(SOURCES) else []


def save_sources(sources):
    os.makedirs(os.path.dirname(SOURCES), exist_ok=True)
    sources.sort(key=lambda entry: (entry["doc"], entry["title"], entry["ext"], version_key(entry["version"])))
    json.dump(sources, open(SOURCES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def discover():
    """The monthly files linked from PSE's page now: [(doc, title, ext)]."""
    html = get(PAGE).text
    found = []
    for match in sorted(set(ART7.findall(html))):
        day, month, year = match
        found.append(("art7", f"Informacje_publikowane_zgodnie_z_Art_7_ust_8l_ustawy_PE_stan_na_{day}_{month}_{year}", "xlsx"))
    return found


def crawl(doc, title, ext, sources, start=None):
    """Fetch the versions of one file not yet in sources: 1.0, 1.1, ... until three misses in a row."""
    known = {entry["version"] for entry in sources if entry["title"] == title and entry["ext"] == ext}
    new, misses, major, minor = [], 0, 1, 0
    if known:
        major, minor = max(version_key(version) for version in known)
        minor += 1
    while misses < 3:
        version = f"{major}.{minor}"
        response = get(f"{BASE}{title}.{ext}?version={version}")
        if is_file(response):
            misses = 0
            new.append((version, response))
        else:
            misses += 1
        minor += 1
        if misses == 3 and (new or known):  # a major version may follow (2.0)
            response = get(f"{BASE}{title}.{ext}?version={major + 1}.0")
            if is_file(response):
                major, minor, misses = major + 1, 1, 0
                new.append((f"{major}.0", response))
    entries = []
    for version, response in new:
        entries.append(ingest(doc, title, ext, version, response.content, response.headers.get("Last-Modified")))
    return entries


def ingest(doc, title, ext, version, content, published):
    """Parse one file, keep it under archive/ and its rows under data/snapshots/."""
    import tempfile
    entry = {"doc": doc, "title": title, "ext": ext, "version": version, "published": published,
             "md5": hashlib.md5(content).hexdigest(), "bytes": len(content), "url": f"{BASE}{title}.{ext}?version={version}"}
    parser = pse_parse.PARSERS.get((doc, ext))
    if not parser:
        entry["skipped"] = "no parser"
        return entry
    with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as handle:
        handle.write(content)
        path = handle.name
    try:
        snapshot = parser(path)
    except Exception as error:  # a scanned PDF or an unexpected layout: kept, reported, not fatal
        entry["skipped"] = f"{type(error).__name__}: {error}"
        return entry
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not snapshot.get("asof") or not snapshot.get("rows"):
        # The main list was a photocopy scan until February 2020: kept for a later OCR pass.
        entry["skipped"] = "no date or no rows (scan?)"
        folder = os.path.join(ARCHIVE, "unparsed")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, f"{doc}_v{version}.{ext}"), "wb").write(content)
        entry["archived"] = f"archive/unparsed/{doc}_v{version}.{ext}"
        return entry
    entry["asof"] = snapshot["asof"]
    entry["rows"] = len(snapshot["rows"])
    write_snapshot(doc, ext, entry, snapshot, content)
    return entry


def write_snapshot(doc, ext, entry, snapshot, content):
    """One snapshot per document and date; a later version, or a spreadsheet over a PDF, replaces it."""
    folder = os.path.join(SNAPSHOTS, doc)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{snapshot['asof']}.json")
    if os.path.exists(path):
        current = json.load(open(path, encoding="utf-8"))["source"]
        same_ext_newer = current["ext"] == ext and version_key(current["version"]) > version_key(entry["version"])
        better_ext = PREFERENCE.get(current["ext"], 9) < PREFERENCE.get(ext, 9)
        if same_ext_newer or better_ext:
            return
    source = {key: entry[key] for key in ("title", "ext", "version", "published", "url", "md5")}
    json.dump({"doc": doc, "asof": snapshot["asof"], "source": source, "notes": snapshot.get("notes", []),
               "rows": snapshot["rows"]}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    archive = os.path.join(ARCHIVE, doc)
    os.makedirs(archive, exist_ok=True)
    for old in os.listdir(archive):
        if old.startswith(snapshot["asof"] + "."):
            os.remove(os.path.join(archive, old))
    open(os.path.join(archive, f"{snapshot['asof']}.{ext}"), "wb").write(content)
    entry["archived"] = f"archive/{doc}/{snapshot['asof']}.{ext}"
