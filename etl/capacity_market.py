"""
Poland's capacity market (rynek mocy): every auction's results, unit by unit,
and the derating factors (korekcyjne współczynniki dyspozycyjności, KWD).

    python etl/capacity_market.py

Sources:
  URE   the President's notices "w sprawie ogłoszenia ostatecznych wyników aukcji
        głównej / dodatkowych / uzupełniającej / dogrywkowej" (ure.gov.pl, rynek mocy,
        Informacje Prezesa URE): the capacity market units (JRM) that won, their type,
        capacity obligation (MW, already derated), contract length, provider and NIP,
        and the clearing prices (zł/kW/year) and totals.
  ELI   the Minister's regulations "w sprawie parametrów aukcji ..." (Dziennik Ustaw,
        api.sejm.gov.pl): the KWD of each technology group for each auction.

A unit's obligation is its derated capacity: the MW it may offer is at most its
net capacity times the KWD of its technology group (less a margin).  The lists do
not say the technology, so it is read from the unit's name and type (BESS, Magazyn,
FW, PV, CCGT...), and the KWD of that group is attached to each contract.

Writes data/capacity_market.json; keeps the PDFs in archive/cm/ (parsed once, by md5).
"""

import collections
import datetime
import hashlib
import html
import json
import os
import re
import sys
import time

import pdfplumber
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ARCHIVE = os.path.join(ROOT, "archive", "cm")
OUT = os.path.join(ROOT, "data", "capacity_market.json")
CACHE = os.path.join(ROOT, "data", "cm_parsed.json")
URE = "https://ure.gov.pl"
NOTICES = URE + "/pl/energia-elektryczna/rynek-mocy/informcjepure"
ELI = "https://api.sejm.gov.pl/eli/acts"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
          "września": 9, "października": 10, "listopada": 11, "grudnia": 12}
QUARTERS = {"I": 1, "II": 2, "III": 3, "IV": 4}


def get(url, binary=False):
    for attempt in range(6):
        response = requests.get(url, headers=UA, timeout=120)
        if response.status_code == 429:   # URE's limit: back off
            time.sleep(10 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.content if binary else response.text
    raise RuntimeError(f"{url}: rate-limited")


def number(text):
    text = (text or "").replace("\xa0", " ").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def polish_date(text):
    match = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", text or "")
    if not match:
        return None
    return datetime.date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()


# ------------------------------------------------------------- URE notices

def notices(known=None):
    """Every final-results notice: [{kind, year, title, url, page}].  known: {page: notice or None} from earlier
    runs, so only new notice pages are opened (URE answers 429 to bursts)."""
    known = {} if known is None else known
    pages, seen = [], set()
    for p in range(0, 20):
        text = get(NOTICES + (f"?page={p}" if p else ""))
        links = [l for l in dict.fromkeys(re.findall(r'href="(/pl/energia-elektryczna/rynek-mocy/informcjepure/\d+,[^"]+\.html)"', text)) if l not in seen]
        if not links:
            break
        seen.update(links)
        pages.extend(links)
        time.sleep(0.5)
    out = []
    for link in pages:
        if link in known:
            if known[link]:
                out.append(known[link])
            continue
        text = get(URE + link)
        body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))
        about = re.search(r"w sprawie (?:w sprawie )?ogłoszenia ostatecznych wyników (aukcji \w+)([^.]{0,160})", body)
        files = re.findall(r'href="(/download/[^"]+)"', text)
        if not about or not files:
            known[link] = None
            continue
        kind = {"głównej": "main", "dodatkowych": "additional", "dodatkowej": "additional", "uzupełniającej": "supplementary",
                "dogrywkowej": "tiebreak"}.get(about.group(1).split()[1], about.group(1))
        year = re.search(r"(20\d\d)", about.group(2))
        notice = {"kind": kind, "year": int(year.group(1)) if year else None,
                  "title": (about.group(1) + about.group(2)).strip(), "url": URE + files[0], "page": URE + link}
        known[link] = notice
        out.append(notice)
        time.sleep(0.4)
    return out


# ------------------------------------------------------------- results PDFs

COLUMNS = {"code": ["kod"], "name": ["nazwa jednostki"], "type": ["typ"], "mw": ["wielkość", "obowiązku"],
           "years": ["okres trwania", "w latach"], "provider": ["nazwa dostawcy"], "nip": ["nip"]}


def header_map(row):
    names = [re.sub(r"\s+", " ", (cell or "")).lower() for cell in row]
    out = {}
    for field, needles in COLUMNS.items():
        for i, name in enumerate(names):
            if i not in out.values() and any(n in name for n in needles):
                if field == "name" and "dostawcy" in name:
                    continue
                if field == "mw" and "okres" in name:
                    continue
                out[field] = i
                break
    return out


def parse_row(row):
    """One unit row by its cells' content, whatever the header (it changes between notices and sometimes
    spans several table rows): L.p., JRM code, [name], type, MW (3 decimals), [years], provider, NIP."""
    cells = [re.sub(r"\s+", " ", (cell or "")).strip() for cell in row]
    if not cells or not re.fullmatch(r"\d+\.?", cells[0]):
        return None
    code_at = next((i for i, c in enumerate(cells) if re.fullmatch(r"JRM/\s?\w+", c)), None)
    if code_at is None:
        return None
    nip_at = max((i for i, c in enumerate(cells) if re.fullmatch(r"[A-Z]{0,3}\s?[\d\- ]{7,}\w*", c)), default=None)
    mw_at = next((i for i in range(code_at + 1, len(cells)) if re.fullmatch(r"\d[\d ]*,\d{3}", cells[i])), None)
    type_at = next((i for i in range(code_at + 1, len(cells)) if "jednostk" in cells[i].lower()), None)
    if mw_at is None or type_at is None:
        return None
    years = cells[mw_at + 1] if mw_at + 1 < len(cells) and re.fullmatch(r"\d{1,2}", cells[mw_at + 1]) else ""
    names = [cells[i] for i in range(code_at + 1, type_at) if cells[i]]
    provider_at = (nip_at - 1) if nip_at is not None else len(cells) - 1
    while provider_at > mw_at and (not cells[provider_at] or re.fullmatch(r"\d{1,2}", cells[provider_at])):
        provider_at -= 1
    return {"code": cells[code_at].replace(" ", ""), "name": " ".join(names), "type": cells[type_at],
            "mw": number(cells[mw_at]), "years": int(years) if years else None,
            "provider": cells[provider_at] if provider_at > mw_at else "", "nip": cells[nip_at] if nip_at is not None else ""}


def parse_results(path, kind):
    """Units and auction figures from one URE notice.  Additional auctions have one section per quarter."""
    rows, sections, text_all = [], [], []
    quarter = None
    columns = None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            text_all.append(text)
            # Section headers ("a) Dla I kwartału roku dostaw 2026") above the tables on this page.
            words = page.extract_words()
            marks = []
            for i, w in enumerate(words):
                if w["text"] == "Dla" and i + 2 < len(words) and words[i + 1]["text"] in QUARTERS and words[i + 2]["text"].startswith("kwarta"):
                    marks.append((w["top"], QUARTERS[words[i + 1]["text"]]))
            for table in page.find_tables():
                above = [q for t, q in marks if t < table.bbox[1]]
                if above:
                    quarter = above[-1]
                for row in table.extract():
                    parsed = parse_row(row)
                    if parsed:
                        parsed["quarter"] = quarter
                        rows.append(parsed)
            if marks and not page.find_tables():
                quarter = marks[-1][1]
    text = "\n".join(text_all)
    flat = re.sub(r"\s+", " ", text)
    date = polish_date(re.search(r"przeprowadzon\w+ w dniu ([^.]*?r\.)", flat).group(1)) if re.search(r"przeprowadzon\w+ w dniu ([^.]*?r\.)", flat) else None
    # Clearing prices in order of appearance; for additional auctions one per quarter section.
    prices = [number(m.group(1)) for m in re.finditer(r"(?:cen[ąa] zamknięcia równą|wyniosła)\s*([\d\s]+,\d+)\s*zł/kW/rok", flat)]
    closing = [number(m) for m in re.findall(r"cen[ąa] zamknięcia (?:równą|rundy \d+\.? [^0-9]*?wyniosła)\s*([\d\s]+,\d+)\s*zł/kW/rok", flat)]
    rounds = [int(m) for m in re.findall(r"(?:w rundzie|rundy)\s*(\d+)", flat)]
    totals = [number(m) for m in re.findall(r"Sumaryczna wielkość obowiązków mocowych wynikająca z (?:zawartych|zawarcia) umów\s+mocowych(?: dla roku dostaw \d{4})? (?:wyniosła|wynosi)\s*([\d\s]+,\d+)\s*MW", flat)]
    foreign = []
    for m in re.finditer(r"obejmującej ([^,]+?), wyniosła ([\d\s]+,\d+) zł/kW/rok", flat):
        foreign.append({"zone": m.group(1).strip(), "price": number(m.group(2))})
    return {"rows": rows, "date": date, "closing": closing or prices[:1], "rounds": rounds, "totals": totals, "foreign": foreign}


# ------------------------------------------------------------- derating factors

def regulations():
    items = requests.get(f"{ELI}/search", params={"title": "parametrów aukcji", "limit": 200}, timeout=60).json()["items"]
    return sorted(({"eli": a["ELI"], "title": a["title"], "date": a.get("promulgation")} for a in items), key=lambda a: a["eli"])


def kwd_tables(eli):
    """{"main": {year: {group: %}}, "additional": {year: {group: %}}} from one regulation's text."""
    publisher, year, pos = eli.split("/")
    path = os.path.join(ARCHIVE, f"reg_{publisher}_{year}_{pos}.pdf")
    if not os.path.exists(path):
        content = get(f"{ELI}/{eli}/text.pdf", binary=True)
        os.makedirs(ARCHIVE, exist_ok=True)
        open(path, "wb").write(content)
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    flat = re.sub(r"\s+", " ", text.replace("-\n", "").replace("‑", "-"))
    flat = re.sub(r"(\w)- (\w)", r"\1\2", flat)
    out = []
    # "... dla aukcji dodatkowych na rok dostaw 2021 określa się na poziomie:", "... dla okresu dostaw przypadającego
    # na rok 2028 określa się na poziomie:" (the main auction, older wording), "... dla aukcji uzupełniającej ... wynoszą:".
    for match in re.finditer(r"Korekcyjne współczynniki dyspozycyjności dla poszczególnych grup technologii dla (.*?)(?:określa się na poziomie|wynoszą):(.*?)(?=§|\Z)", flat):
        scope, body = match.groups()
        kind = ("additional" if "dodatk" in scope else "supplementary" if "uzupełn" in scope else "tiebreak" if "dogryw" in scope
                else "main")
        years = [int(y) for y in re.findall(r"20\d\d", scope)]
        groups = {}
        for value, group in re.findall(r"(\d+(?:,\d+)?) ?% – dla ([^;.]+)", body):
            groups[group.strip()] = round(float(value.replace(",", ".")) / 100, 4)
        if groups:
            out.append({"kind": kind, "years": years, "groups": groups, "eli": eli})
    return out


# ------------------------------------------------------------- technology

def technology(name, kind_text, provider):
    text = " ".join([name or "", provider or ""])
    low = text.lower()
    kt = (kind_text or "").lower()
    if "zagraniczn" in kt:
        return "foreign"
    if "redukcji zapotrzebowania" in kt:
        return "dsr"
    if re.search(r"\b(bess|ess|storage|mee|me)\b|magazyn|akumulator|battery", low):
        return "storage"
    if re.search(r"żarnowiec|porąbka|solina|dychów|niedzica|szczytowo|\besp\b|pompow", low):
        return "pumped_hydro"
    if re.search(r"\bfw\b|wiatr|wind", low):
        return "wind"
    if re.search(r"\bpv\b|fotowolt|solar|słoneczn", low):
        return "solar"
    if re.search(r"\bew\b|wodn|hydro", low):
        return "hydro"
    if re.search(r"ccgt|gazowo|\bbgp\b|parowo-gaz|combined", low):
        return "ccgt"
    if re.search(r"silnik|\bgt\d?\b|turbin\w* gazow|gas engine|gazow|kogener|\bchp\b|\bec\b", low):
        return "gas_engine"
    if re.search(r"jądrow|nuclear", low):
        return "nuclear"
    return "thermal"


# Which KWD group a technology falls in: words to look for in the regulation's group name.
GROUP_WORDS = {
    "storage": ["akumulator"], "pumped_hydro": ["członem pompowym"], "wind": ["wiatrowych na lądzie", "wiatrowych"],
    "solar": ["słoneczn"], "hydro": ["przepływowych"], "ccgt": ["gazowo-parowych"], "gas_engine": ["turbin gazowych", "silnik"],
    "thermal": ["turbin parowych"], "nuclear": ["jądrow"], "dsr": ["redukcji"],
}


def kwd_for(tech, groups):
    for word in GROUP_WORDS.get(tech, []):
        for group, value in groups.items():
            if word in group:
                return value, group
    return None, None


# ------------------------------------------------------------- build

def main():
    os.makedirs(ARCHIVE, exist_ok=True)
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    before = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
    pages = cache.setdefault("_pages", {})
    urls = cache.setdefault("_urls", {})
    found = notices(pages)
    print(f"{len(found)} final-results notices")
    auctions, units, contracts = [], {}, []
    for notice in sorted(found, key=lambda n: (n["year"] or 0, n["kind"])):
        md5 = urls.get(notice["url"])
        if md5 not in cache:
            content = get(notice["url"], binary=True)
            if content[:5] != b"%PDF-":
                print(f"  {notice['title']}: not a PDF")
                continue
            md5 = hashlib.md5(content).hexdigest()
            urls[notice["url"]] = md5
            time.sleep(1)
        else:
            content = None
        path = os.path.join(ARCHIVE, f"{notice['kind']}_{notice['year']}_{md5[:8]}.pdf")
        if content and not os.path.exists(path):
            open(path, "wb").write(content)
        if md5 not in cache:
            cache[md5] = parse_results(path, notice["kind"])
        parsed = cache[md5]
        quarters = sorted({r["quarter"] for r in parsed["rows"] if r["quarter"]}) if notice["kind"] == "additional" else [None]
        for index, quarter in enumerate(quarters):
            aid = f"{notice['kind']}-{notice['year']}" + (f"-Q{quarter}" if quarter else "")
            mine = [r for r in parsed["rows"] if r["quarter"] == quarter] if quarter else parsed["rows"]
            price = parsed["closing"][index] if index < len(parsed["closing"]) else (parsed["closing"][0] if parsed["closing"] else None)
            auctions.append({"id": aid, "kind": notice["kind"], "delivery_year": notice["year"], "quarter": quarter,
                             "date": parsed["date"], "price": price, "units": len(mine),
                             "mw": round(sum(r["mw"] or 0 for r in mine), 3),
                             "total_reported": parsed["totals"][index] if index < len(parsed["totals"]) else None,
                             "foreign_prices": parsed["foreign"][index * 3:(index + 1) * 3] if quarter else parsed["foreign"][:3],
                             "notice": notice["title"], "url": notice["url"], "page": notice["page"]})
            for r in mine:
                unit = units.setdefault(r["code"], {"code": r["code"], "name": "", "provider": r["provider"], "nip": r["nip"], "types": []})
                unit["name"] = unit["name"] or r["name"]
                unit["provider"] = r["provider"] or unit["provider"]
                if r["type"] and r["type"] not in unit["types"]:
                    unit["types"].append(r["type"])
                start, end = (notice["year"], notice["year"] + (r["years"] or 1) - 1) if notice["kind"] == "main" else (notice["year"], notice["year"])
                contracts.append({"code": r["code"], "auction": aid, "mw": r["mw"], "years": r["years"] or (1 if notice["kind"] == "main" else None),
                                  "from": start, "to": end, "quarter": quarter, "price": price, "type": r["type"]})
        print(f"  {notice['kind']} {notice['year']}: {len(parsed['rows'])} rows, prices {parsed['closing']}, date {parsed['date']}")
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

    # Derating factors per auction.
    kwd = []
    for reg in regulations():
        try:
            if reg["eli"] not in cache.setdefault("_kwd", {}):
                cache["_kwd"][reg["eli"]] = kwd_tables(reg["eli"])
            for table in cache["_kwd"][reg["eli"]]:
                table["title"] = reg["title"]
                kwd.append(table)
        except Exception as error:  # a regulation without a text PDF yet
            print(f"  {reg['eli']}: {error}")
    print(f"{len(kwd)} KWD tables")

    def groups_for(auction):
        kind, year = auction["kind"], auction["delivery_year"]
        for table in kwd:
            if table["kind"] == kind and year in table["years"]:
                return table
        for table in kwd:   # the 2021-2023 regulation names one table for the three main auctions
            if table["kind"] == kind and table["years"] and min(table["years"]) <= year <= max(table["years"]):
                return table
        return None

    by_id = {a["id"]: a for a in auctions}
    for auction in auctions:
        table = groups_for(auction)
        auction["kwd"] = table["groups"] if table else None
        auction["kwd_source"] = f"Dz.U. {table['eli'].split('/')[1]} poz. {table['eli'].split('/')[2]}" if table and table["eli"].startswith("DU") else (table["eli"] if table else None)
    for unit in units.values():
        unit["technology"] = technology(unit["name"], " ".join(unit["types"]), unit["provider"])
        unit["new"] = any("nowa" in t.lower() for t in unit["types"])
    for contract in contracts:
        auction = by_id[contract["auction"]]
        tech = units[contract["code"]]["technology"]
        value, group = kwd_for(tech, auction["kwd"] or {})
        contract["kwd"], contract["kwd_group"] = value, group
        contract["derated_from"] = round(contract["mw"] / value, 3) if value and contract["mw"] else None
    out = {"updated": datetime.date.today().isoformat(), "currency": "PLN",
           "auctions": sorted(auctions, key=lambda a: (a["delivery_year"] or 0, a["kind"] != "main", a["quarter"] or 0)),
           "units": sorted(units.values(), key=lambda u: (len(u["code"]), u["code"])),
           "contracts": contracts, "kwd": kwd,
           "sources": {"results": NOTICES, "parameters": "https://api.sejm.gov.pl/eli (Dziennik Ustaw, rozporządzenia w sprawie parametrów aukcji)"}}
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    # The date alone does not make a change.
    changed = re.sub(r'"updated":"[^"]*"', "", text) != re.sub(r'"updated":"[^"]*"', "", before)
    if changed:
        open(OUT, "w", encoding="utf-8").write(text)
    print(f"{len(auctions)} auctions, {len(units)} units, {len(contracts)} contracts{'' if changed else ' (unchanged)'}")
    print(collections.Counter(u["technology"] for u in units.values()))
    return changed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
