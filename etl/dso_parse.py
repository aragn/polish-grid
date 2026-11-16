"""
Parse the distribution operators' connection lists (Art. 7 ust. 8l of the
Energy Law) and their storage registers.

Three layouts:

  new   since the 2025 amendment: the same 27 columns as PSE's monthly
        xlsx (L.p., ID Obiektu, operator, applicant, station, voltage,
        type, MW in/out, installed PV/FW/storage/load/other, status and
        every date), published as a PDF.  TAURON writes the station as
        its internal code (WZA4: station WZA, 4 = the 110 kV side),
        ENEA as text ("GPZ Łużycka (24)", "Oborniki pole nr 14 / L-...").
        ENEA adds a 28th column, Uwagi.
  old   TAURON's quarterly "Informacje dotyczące podmiotów ubiegających się
        o przyłączenie źródeł" (sources above 1 kV only): voivodeship,
        gmina, kind of plant, kW, conditions, agreement, the date it was
        told it may sign a distribution contract (the plant is connected)
        and, since 2023, the date energy delivery started.
  oldenea  ENEA's "Informacja dotycząca podmiotów ubiegających się o
        przyłączenie źródeł": seat, place of connection, kW, kind,
        conditions, agreement, start of delivery.

pdfplumber reads the tables; where two rows share one table row (no rule
between them) the words are split by their height on the page.
"""

import datetime
import os
import re
import sys

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pse_parse import art7_status, clean, sum_mw, to_date


def asof_from_text(text):
    match = re.search(r"stan\s+na(?:\s+dzień)?\s*:?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", text, re.I)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    return datetime.date(year, month, day).isoformat()


def table_rows(page):
    """Every table row on the page as a list of cell strings, rows merged into one cell split apart."""
    out = []
    words = None
    for table in page.find_tables():
        for row, extracted in zip(table.rows, table.extract()):
            if not extracted or "\n" not in (extracted[0] or "") or not re.fullmatch(r"\d+(\n\d+)+", (extracted[0] or "").strip()):
                out.append(extracted)
                continue
            if words is None:
                words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            top, bottom = row.bbox[1] - 1, row.bbox[3] + 1
            band = [w for w in words if w["top"] >= top and w["bottom"] <= bottom]
            texts = []
            for bbox in row.cells:
                texts.append(None if bbox is None else [w for w in band if w["x0"] >= bbox[0] - 1 and w["x1"] <= bbox[2] + 1])
            numbers = sorted({round(w["top"], 1) for w in texts[0] or [] if re.fullmatch(r"\d+", w["text"])})
            # Several L.p. numbers in one cell: one sub-row per number, from its height to the next one's.
            bounds = numbers[1:] + [float("inf")]
            for k, top in enumerate(numbers):
                lo = top - 2 if k else -float("inf")
                hi = bounds[k] - 2
                out.append([None if t is None else join_words([w for w in t if lo <= w["top"] < hi]) for t in texts])
    return out


def join_words(words):
    if not words:
        return ""
    words = sorted(words, key=lambda w: (round(w["top"] / 3), w["x0"]))
    return " ".join(w["text"] for w in words)


def kw(value):
    """'1 500,0' (kW, space as thousands separator) -> MW."""
    text = clean(value).replace(" ", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(match.group(0)) / 1000, 4) if match else None


def mw(value):
    total, _ = sum_mw(clean(value).replace(" ", ""))
    return total


# ---------------------------------------------------------------- new layout

NEW_FIELDS = ["lp", "id", "operator", "applicant", "station", "kv", "multi", "type", "mw_in", "mw_out",
              "pv", "wind", "storage_out", "storage_in", "load", "other", "status", "applied", "advance",
              "conditions", "refused", "refusal_reason", "expired", "agreement", "delivery", "terminated", "lapsed", "remarks"]
HIDDEN_APPLICANT = ("informacja niejawna", "osoba prawna", "osoba fizyczna", "nie podlega")


def parse_new(path):
    rows, asof, seen = [], None, set()
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if asof is None:
                asof = asof_from_text(page.extract_text() or "")
            for cells in table_rows(page):
                if len(cells) < 27 or not re.fullmatch(r"\d+", clean(cells[0])):
                    continue
                raw = dict(zip(NEW_FIELDS, [clean(c) for c in cells]))
                status = art7_status(raw["status"])
                if not status:
                    continue
                key = tuple(v for k, v in raw.items() if k != "lp")
                if key in seen:   # one project written on several lines (several connection points, same figures)
                    continue
                seen.add(key)
                installed = {}
                for field in ("pv", "wind", "storage_out", "storage_in", "load", "other"):
                    value = mw(raw[field])
                    if value:
                        installed[field] = value
                applicant = raw["applicant"]
                rows.append({
                    "id": raw["id"], "operator": raw["operator"],
                    "applicant": "" if applicant.lower().startswith(HIDDEN_APPLICANT) else applicant,
                    "applicant_form": applicant if applicant.lower().startswith(("osoba prawna", "osoba fizyczna")) else "",
                    "station": "" if "nie zostało określone" in raw["station"] else raw["station"],
                    "kv": raw["kv"], "multi": raw["multi"].upper() == "TAK", "type": raw["type"],
                    "mw_in": mw(raw["mw_in"]), "mw_out": mw(raw["mw_out"]), "installed": installed,
                    "status": status, "status_pl": raw["status"],
                    "applied": to_date(raw["applied"]), "advance": to_date(raw["advance"]),
                    "conditions": to_date(raw["conditions"]), "refused": to_date(raw["refused"]),
                    "refusal_reason": raw["refusal_reason"] if raw["refusal_reason"] not in ("-", "") else "",
                    "expired": to_date(raw["expired"]), "agreement": to_date(raw["agreement"]),
                    "delivery": to_date(raw["delivery"]), "terminated": to_date(raw["terminated"]),
                    "lapsed": to_date(raw["lapsed"]), "remarks": raw.get("remarks", "") if raw.get("remarks") not in ("-", None) else "",
                })
    return {"asof": asof, "layout": "new", "rows": rows}


# ---------------------------------------------------------------- old layouts

# The kind of plant as the old lists write it -> the art. 7 type code.
KIND = [
    ("słoneczn", "PV"), ("fotowolta", "PV"), ("pv", "PV"), ("wiatr", "FW"), ("magazyn", "MEE"),
    ("wodn", "EW"), ("biogaz", "BG"), ("biomas", "JBM"), ("kogenera", "SG"), ("ciepłown", "SG"),
    ("gazow", "SG"), ("hybryd", "MIX"),
]


def kind_code(text):
    lowered = clean(text).lower()
    for needle, code in KIND:
        if needle in lowered:
            return code
    return "OZE" if lowered else ""


def parse_old_tauron(path):
    rows, asof = [], None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if asof is None:
                asof = asof_from_text(page.extract_text() or "")
            for cells in table_rows(page):
                cells = [clean(c) for c in cells]
                if len(cells) < 9 or not re.fullmatch(r"\d+", cells[0]) or cells[1] == "2":
                    continue   # cells[1] == "2": the row numbering the columns, repeated on every page
                connected = to_date(cells[8])
                started = to_date(cells[9]) if len(cells) > 9 else None
                rows.append({
                    "applicant_form": cells[1], "voivodeship": cells[2], "gmina": cells[3], "kind": cells[4],
                    "type": kind_code(cells[4]), "mw_in": kw(cells[5]),
                    "conditions": to_date(cells[6]), "agreement": to_date(cells[7]),
                    "connected": connected if connected and connected[:1].isdigit() else None,
                    "started": started if started and started[:1].isdigit() else None,
                })
    return {"asof": asof, "layout": "old", "rows": rows}


def parse_old_enea(path):
    rows, asof = [], None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if asof is None:
                asof = asof_from_text(page.extract_text() or "")
            for cells in table_rows(page):
                cells = [clean(c) for c in cells]
                if len(cells) < 8 or not cells[0].lower().startswith(("osoba", "podmiot")) or cells[0].lower() == "podmiot":
                    continue
                started = to_date(cells[7])
                rows.append({
                    "applicant_form": cells[0], "seat": cells[1], "place": cells[2], "kind": cells[4],
                    "type": kind_code(cells[4]), "mw_in": kw(cells[3]),
                    "conditions": to_date(cells[5]), "agreement": to_date(cells[6]),
                    "started": started if started and started[:1].isdigit() else None,
                    "remarks": cells[8] if len(cells) > 8 and cells[8] not in ("-", "") else "",
                })
    return {"asof": asof, "layout": "oldenea", "rows": rows}


# ---------------------------------------------------------------- storage registers

def parse_storage_register(path, operator):
    """The DSO's register of energy storage (art. 43g): every row is a storage unit in operation."""
    import openpyxl
    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    header = None
    for index, row in enumerate(rows[:12]):
        text = " | ".join(clean(c).lower() for c in row if c is not None)
        if "moc zainstalowana" in text or "łączna moc" in text:
            header = index
            break
    if header is None:
        raise ValueError(f"{path}: no storage register header")
    names = [clean(c).lower() for c in rows[header]]

    def column(*needles, after=0):
        for i, name in enumerate(names):
            if i >= after and any(n in name for n in needles):
                return i
        return None
    c_power, c_energy = column("łączna moc"), column("pojemno")
    c_place, c_start = column("miejsce\n przyłączenia".lower(), "miejsce przyłączenia", "miejsce  przyłączenia"), column("data rozpoczęcia")
    c_first = column("data pierwszego wpisu")
    c_name = column("nazwa")
    if c_place is None:
        c_place = next((i for i, n in enumerate(names) if n.startswith("miejsce") and "przyłącz" in n), None)
    out = []
    for row in rows[header + 1:]:
        if not row or not isinstance(row[0], (int, float)):
            continue
        get = lambda i: row[i] if i is not None and i < len(row) else None
        power = get(c_power)
        if not isinstance(power, (int, float)):
            continue
        # The site of the business is the address block just before the start date.
        address = [clean(get(i)) for i in range((c_start or 0) - 8, c_start or 0)] if c_start else []
        out.append({
            "operator": operator, "owner": clean(get(c_name)), "mw": round(power / 1000, 3),
            "mwh": round(get(c_energy) / 1000, 3) if isinstance(get(c_energy), (int, float)) else None,
            # Place names only (town, gmina, county): no street, house number or postcode.
            "place": clean(get(c_place)), "address": [a for a in address if a and not any(ch.isdigit() for ch in a)],
            "started": to_date(get(c_start)), "registered": to_date(get(c_first)),
        })
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    layout, path = sys.argv[1], sys.argv[2]
    parsed = {"new": parse_new, "old": parse_old_tauron, "oldenea": parse_old_enea}[layout](path)
    print(parsed["asof"], len(parsed["rows"]))
    for row in parsed["rows"][:3] + parsed["rows"][-2:]:
        print(row)
