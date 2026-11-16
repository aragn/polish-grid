"""
Fetch the distribution operators' connection lists and storage registers,
keep each file in archive/<operator>/ and its parsed rows in
data/snapshots/<operator>/<asof>.json.

    python etl/dso.py              # the files linked on the operators' pages now
    python etl/dso.py --backfill   # also every earlier list still online or in the Wayback Machine

TAURON overwrites one fixed file name with each new list; its quarterly
lists of 2024-2026 stay online under their own names and older ones are
in the Wayback Machine.  ENEA links its newest list from its page under a
new name each time; two earlier ones survive in the Wayback Machine.
A file is parsed once: sources.json keeps its md5, so the daily run only
reads files that changed.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dso_parse  # noqa: E402

ROOT = os.path.dirname(HERE)
ARCHIVE = os.path.join(ROOT, "archive")
SNAPSHOTS = os.path.join(ROOT, "data", "snapshots")
SOURCES = os.path.join(ROOT, "data", "dso_sources.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}

TAURON = "https://www.tauron-dystrybucja.pl"
TAURON_MEDIA = TAURON + "/-/media/offer-documents/dystrybucja/przylaczenie"
ENEA = "https://www.operator.enea.pl"
WAYBACK = "https://web.archive.org/web/{stamp}id_/{url}"

OPERATORS = {
    "tauron": {"name": "TAURON Dystrybucja S.A.", "short": "TAURON"},
    "enea": {"name": "ENEA Operator Sp. z o.o.", "short": "ENEA"},
    "pge": {"name": "PGE Dystrybucja S.A.", "short": "PGE"},
    "energa": {"name": "Energa-Operator S.A.", "short": "ENERGA"},
}
PGE = "https://pgedystrybucja.pl"
ENERGA = "https://energa-operator.pl"


# PGE's server sends its certificate without the intermediate (Certum DV TLS G2 R39 CA): browsers fetch it
# themselves, Python does not.  The intermediate, downloaded from the address in PGE's certificate, is kept in
# etl/certs/ and added to certifi's roots for that host; verification stays on.
INTERMEDIATES = {"pgedystrybucja.pl": os.path.join(HERE, "certs", "certum_dv_tls_g2_r39_ca.pem")}
_BUNDLES = {}


def verify_for(url):
    host = url.split("/")[2]
    if host not in INTERMEDIATES:
        return True
    if host not in _BUNDLES:
        import certifi
        import tempfile
        path = os.path.join(tempfile.gettempdir(), f"ca_bundle_{host}.pem")
        with open(path, "w", encoding="ascii") as handle:
            handle.write(open(certifi.where(), encoding="ascii").read() + "\n" + open(INTERMEDIATES[host], encoding="ascii").read())
        _BUNDLES[host] = path
    return _BUNDLES[host]


def fetch(url, timeout=60):
    return requests.get(url, headers=UA, timeout=timeout, verify=verify_for(url))


def tauron_targets(backfill):
    targets = [("tauron", "new", TAURON_MEDIA + "/dostepne-moce/informacja-o-przylaczanych-obiektach-i-wydanych-odmowach.ashx", None)]
    if backfill:
        year = time.gmtime().tm_year
        for y in range(2024, year + 1):
            for q in ("i", "ii", "iii", "iv"):
                targets.append(("tauron", "old", f"{TAURON_MEDIA}/dostepne-moce/informacja-{q}-kw-{y}.ashx", None))
        # Gone from TAURON's site, kept by the Wayback Machine (the 2023 copies there are truncated).
        for stamp, name in (("20220517015210", "informacja-iv-kw-2021"), ("20221207152505", "informacja-ikw-2022"),
                            ("20240405152207", "informacja-ii-kw-2023")):
            url = f"{TAURON_MEDIA}/dostepne-moce/{name}.ashx"
            targets.append(("tauron", "old", WAYBACK.format(stamp=stamp, url=url), url))
    return targets


def enea_targets(backfill):
    page = requests.get(ENEA + "/przylaczenie-do-sieci/informacje-o-przylaczeniach", headers=UA, timeout=60).text
    links = sorted(set(re.findall(r'href="(/media/\d+/[^"]*wspolny-wykaz[^"]*)"', page)))
    if not links:
        sys.exit("ENEA's page links no 'wspolny-wykaz' file: check the page and etl/dso.py")
    targets = [("enea", "new", ENEA + link, None) for link in links]
    if backfill:
        for stamp, path in (("20260101130305", "/media/2734/zestawieniepodmiotow30092025r-werdo-podpisusig-003-sigpdf.pdf"),
                            ("20260719062032", "/media/3460/przylaczenie-do-sieci/zestawieniepodmiotow30062026rpdf.pdf")):
            targets.append(("enea", "oldenea", WAYBACK.format(stamp=stamp, url=ENEA + path), ENEA + path))
    return targets


def pge_targets(backfill):
    """PGE links its newest list from one page (eZ Publish 'content/download' URL; older versions are not served)."""
    page = fetch(PGE + "/przylaczenia/procedury-przylaczeniowe/informacja-o-podmiotach-ubiegajacych-sie-o-przylaczenie-do-sieci-powyzej-1kv").text
    links = re.findall(r'href="(/content/download/[^"]*podmiotach[^"]*)"', page)
    if not links:
        sys.exit("PGE's page links no list of applicants: check the page and etl/dso.py")
    return [("pge", "new", PGE + links[0].replace("&amp;", "&"), None)]


def energa_targets(backfill):
    """ENERGA links its newest list ('..._pusop_wnioski_i_odmowy_eop.pdf' on its media CDN) from one page."""
    page = fetch(ENERGA + "/przylaczenie-do-sieci/informacje-o-stanie-przylaczen").text
    links = re.findall(r'href="(https://[^"]*pusop_wnioski[^"]*\.pdf)"', page)
    if not links:
        sys.exit("ENERGA's page links no 'pusop_wnioski' file: check the page and etl/dso.py")
    return [("energa", "new", links[0], None)]


def storage_targets():
    page = requests.get(ENEA + "/przylaczenie-do-sieci/magazyny-energii-elektrycznej", headers=UA, timeout=60).text
    enea = re.findall(r'href="(/media/\d+/rejestr-magazynow[^"]*)"', page)
    out = [("tauron", TAURON_MEDIA + "/rejestr-magazynow-energii/rejestr-magazynow-energii.ashx")]
    if enea:
        out.append(("enea", ENEA + enea[0]))
    return out


def download(url):
    for attempt in range(4):
        try:
            response = fetch(url, timeout=180)
            if response.status_code == 404:
                return None, None
            response.raise_for_status()
            return response.content, response.headers.get("Last-Modified")
        except requests.RequestException as error:
            if attempt == 3:
                print(f"  {url}: {error}")
                return None, None
            time.sleep(5 * (attempt + 1))
    return None, None


def is_pdf(content):
    return content[:5] == b"%PDF-"


def parse_file(job):
    operator, layout, path = job
    if layout == "new" and operator == "energa":
        # ENERGA's PDF: rows packed into shared cells, overlapping and partly outside the detected tables.
        return job, dso_parse.parse_new(path, by_chars=True)
    parser = {"new": dso_parse.parse_new, "old": dso_parse.parse_old_tauron, "oldenea": dso_parse.parse_old_enea}[layout]
    return job, parser(path)


def load_sources():
    if os.path.exists(SOURCES):
        return json.load(open(SOURCES, encoding="utf-8"))
    return {"lists": [], "registers": []}


def run(backfill=False, workers=6):
    """Fetch, archive and parse; returns how many new lists or changed registers there were."""
    sources = load_sources()
    known = {entry["md5"]: entry for entry in sources["lists"]}

    jobs, fetched = [], []
    for operator, layout, url, original in tauron_targets(backfill) + enea_targets(backfill) + pge_targets(backfill) + energa_targets(backfill):
        content, modified = download(url)
        time.sleep(1)
        if not content or not is_pdf(content):
            continue
        md5 = hashlib.md5(content).hexdigest()
        if md5 in known:
            continue
        folder = os.path.join(ARCHIVE, operator)
        os.makedirs(folder, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", (original or url).split("?")[0].rsplit("/", 1)[-1]).replace(".ashx", "")[:80]
        path = os.path.join(folder, f"{md5[:8]}_{name}" + ("" if name.endswith(".pdf") else ".pdf"))
        open(path, "wb").write(content)
        jobs.append((operator, layout, path))
        fetched.append({"operator": operator, "layout": layout, "url": original or url, "via": url if original else None,
                        "published": modified, "md5": md5, "file": os.path.relpath(path, ROOT).replace("\\", "/")})
        print(f"{operator} {layout}: new file {os.path.basename(path)}")

    by_path = {entry["file"]: entry for entry in fetched}
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        for (operator, layout, path), parsed in pool.map(parse_file, jobs):
            entry = by_path[os.path.relpath(path, ROOT).replace("\\", "/")]
            entry.update(asof=parsed["asof"], rows=len(parsed["rows"]))
            if not parsed["asof"] or not parsed["rows"]:
                print(f"  {path}: nothing parsed")
                continue
            folder = os.path.join(SNAPSHOTS, operator)
            os.makedirs(folder, exist_ok=True)
            out = {"doc": operator, "asof": parsed["asof"], "layout": layout, "source": entry, "rows": parsed["rows"]}
            target = os.path.join(folder, f"{parsed['asof']}.json")
            # Two files with the same date (the quarterly list and the new monthly list): the new layout wins.
            if os.path.exists(target) and layout != "new" and json.load(open(target, encoding="utf-8")).get("layout") == "new":
                print(f"  {parsed['asof']}: keeping the new-layout list")
            else:
                json.dump(out, open(target, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            print(f"{operator} {parsed['asof']}: {len(parsed['rows'])} rows ({layout})")
            sources["lists"].append(entry)

    # Storage registers: the latest file of each operator, kept whole (small).
    registers = []
    for operator, url in storage_targets():
        content, modified = download(url)
        if not content:
            continue
        folder = os.path.join(ARCHIVE, operator)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "rejestr-magazynow-energii.xlsx")
        open(path, "wb").write(content)
        rows = dso_parse.parse_storage_register(path, OPERATORS[operator]["name"])
        registers.append({"operator": operator, "url": url, "published": modified, "rows": rows})
        print(f"{operator} storage register: {len(rows)} units")
    changed = len(jobs)
    if registers:
        path = os.path.join(ROOT, "data", "storage_registers.json")
        old = json.load(open(path, encoding="utf-8"))["registers"] if os.path.exists(path) else []
        strip = lambda items: [{k: v for k, v in r.items() if k != "published"} for r in items]
        if strip(old) != strip(registers):
            json.dump({"registers": registers}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            changed += 1
    sources["lists"].sort(key=lambda entry: (entry["operator"], entry.get("asof") or ""))
    json.dump(sources, open(SOURCES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    print(f"{run(args.backfill, args.workers)} new lists or changed registers")


if __name__ == "__main__":
    main()
