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

import bisect
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
    for table in page.find_tables():
        for row, extracted in zip(table.rows, table.extract()):
            if not extracted or "\n" not in (extracted[0] or "") or not re.fullmatch(r"\d+\.?(\n\d+\.?)+", (extracted[0] or "").strip()):
                out.append(extracted)
                continue
            # Several L.p. numbers in one cell (ENERGA puts dozens of rows in one): one sub-row per number,
            # from its line's height to the next one's.  Cells are read by cropping, as pdfplumber's text
            # extraction copes with the character spacing that splits ENERGA's words letter by letter.
            first = row.cells[0]
            lines = page.crop(first).extract_text_lines()
            tops = [line["top"] for line in lines if re.fullmatch(r"\d+\.?", line["text"].strip())]
            if len(tops) <= 1:
                out.append(extracted)
                continue
            bounds = [row.bbox[1] - 1] + [t - 1 for t in tops[1:]] + [row.bbox[3] + 1]
            # Each cell's text lines, whole, go to the sub-row whose band holds the line's middle
            # (slicing a cell by height cuts lines that sit a little off the L.p. lines).
            columns = []
            for bbox in row.cells:
                if bbox is None:
                    columns.append(None)
                    continue
                parts = [[] for _ in tops]
                for line in page.crop(bbox).extract_text_lines():
                    middle = (line["top"] + line["bottom"]) / 2
                    k = max(0, min(len(tops) - 1, next((i for i in range(len(tops)) if bounds[i] <= middle < bounds[i + 1]), len(tops) - 1)))
                    parts[k].append(line["text"])
                columns.append(parts)
            for k in range(len(tops)):
                out.append([None if parts is None else " ".join(parts[k]) for parts in columns])
    return out


def char_rows(page, min_columns=27):
    """Rows from the characters themselves, for lists whose tables pdfplumber only half finds (ENERGA's:
    dozens of rows share one ruled cell, row numbers overlap by a fraction of a point, part of each page
    sits outside any detected table).  Column edges come from the table's widest row; each row starts at
    the height of its number ("158.") in the first column; every character goes to the row and column
    its middle falls in."""
    tables = page.find_tables()
    rows = [row for table in tables for row in table.rows]
    if not rows:
        return []
    widest = max(rows, key=lambda row: sum(cell is not None for cell in row.cells))
    edges = [(cell[0], cell[2]) for cell in widest.cells if cell is not None]
    if len(edges) < min_columns:
        return []
    chars = list(page.chars)   # spaces kept: the words' only separators at ENERGA's 1-point letter widths
    middle_x = lambda c: (c["x0"] + c["x1"]) / 2
    middle_y = lambda c: (c["top"] + c["bottom"]) / 2
    first = [c for c in chars if edges[0][0] - 1 <= middle_x(c) <= edges[0][1] + 1]
    by_top = {}
    for c in first:
        by_top.setdefault(round(c["top"], 1), []).append(c)
    anchors = sorted(top for top, group in by_top.items()
                     if re.fullmatch(r"\d+\.?", "".join(c["text"] for c in sorted(group, key=lambda c: c["x0"]))))
    if not anchors:
        return []
    bounds = [a - 1 for a in anchors] + [max(c["bottom"] for c in chars) + 1]
    cells = [[[] for _ in edges] for _ in anchors]
    for c in chars:
        y, x = middle_y(c), middle_x(c)
        if y < bounds[0]:
            continue
        k = bisect.bisect_right(bounds, y) - 1
        if k >= len(anchors):
            continue
        for j, (x0, x1) in enumerate(edges):
            if x0 - 0.5 <= x <= x1 + 0.5:
                cells[k][j].append(c)
                break
    def text(cell):
        # Reading order: lines (tops within half a point) top to bottom, characters left to right.
        lines = {}
        for c in cell:
            lines.setdefault(round(c["top"] * 2) / 2, []).append(c)
        joined = " ".join("".join(c["text"] for c in sorted(line, key=lambda c: c["x0"])) for _, line in sorted(lines.items()))
        return re.sub(r"\s+", " ", joined).strip()
    return [[text(cell) for cell in row] for row in cells]


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
HIDDEN_APPLICANT = ("informacja niejawna", "osoba prawna", "osoba fizyczna", "nie podlega", "urząd/instytucja", "rolnik")


def parse_new(path, by_chars=False):
    """by_chars: read rows from character positions (char_rows) instead of pdfplumber's table cells."""
    rows, asof, seen = [], None, set()
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if asof is None:
                asof = asof_from_text(page.extract_text() or "")
            for cells in (char_rows(page) if by_chars else table_rows(page)):
                if len(cells) < 27 or not re.fullmatch(r"\d+\.?", clean(cells[0])):   # ENERGA writes "158."
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
    ("wodn", "EW"), ("biogaz", "BG"), ("biomas", "JBM"), ("kogenera", "SG"), ("ciepłown", "SG"), ("gaz ziemny", "SG"),
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


# Dates may start with a footnote star ("*20.02.2025 r.").  kW: thousands in groups of exactly three digits
# ("121 000"), so the figure cannot swallow the first digit of the date after it ("121 000 29.07.2019").
DATE = r"\*?(\d{1,2}\.\d\d\.\d{4}) r\."
KW = r"(\d+(?: \d{3})*(?:,\d+)?)"
STOEN_ROW = re.compile(
    r"^(\d+) (Osoba \w+) (.*?)\s*((?:Elektrownia|Elektrociepłownia|Magazyn|Instalacja|Układ|Agregat|Ogniwa?|Jednostka|Turbina|Silnik)[^\d(]*?)\s*"
    + KW + r"?(?:\s+\d(?=\s))?\s*(?:\(?[^()]*\)\s*)?" + DATE + r"(?:\s+" + DATE + r")?(?:\s+" + DATE + r")?")
LONE_NUMBER = re.compile(r"^\(?(?:zwiększenie o )?" + KW + r"\)?(?:\s|$)")


def parse_old_stoen(path):
    """Stoen Operator's "Zestawienie podmiotów ubiegających się o przyłączenie źródeł ... o napięciu wyższym niż
    1kV": no ruled table, one row per text line: L.p., applicant type, location (a Warsaw street), kind, kW,
    conditions, agreement, start of delivery (wytwarzania).  Where the kW cell wraps ("120 (zwiększenie o 70)")
    the total can sit on the line above or below."""
    rows, asof = [], None
    with pdfplumber.open(path) as pdf:
        lines = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            if asof is None:
                match = re.search(r"stan na dzień\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
                if match:
                    day, month, year = map(int, match.groups())
                    asof = datetime.date(year, month, day).isoformat()
            lines.extend(line.strip() for line in text.split("\n"))
    for i, line in enumerate(lines):
        match = STOEN_ROW.match(line)
        if not match:
            continue
        lp, form, place, kind, kw_text, conditions, agreement, started = match.groups()
        if not kw_text:
            for near in (lines[i - 1] if i else "", lines[i + 1] if i + 1 < len(lines) else ""):
                lone = LONE_NUMBER.match(near)
                if lone:
                    kw_text = lone.group(1)
                    break
        rows.append({"applicant_form": form, "place": place.strip(" ,"), "gmina": (place.split(",")[0].strip() or "Warszawa"),
                     "voivodeship": "Mazowieckie", "kind": kind.strip(), "type": kind_code(kind), "mw_in": kw(kw_text) if kw_text else None,
                     "conditions": to_date(conditions), "agreement": to_date(agreement) if agreement else None,
                     "started": to_date(started) if started else None})
    return {"asof": asof, "layout": "oldstoen", "rows": rows}


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
    parsed = {"new": parse_new, "old": parse_old_tauron, "oldenea": parse_old_enea, "oldstoen": parse_old_stoen}[layout](path)
    print(parsed["asof"], len(parsed["rows"]))
    for row in parsed["rows"][:3] + parsed["rows"][-2:]:
        print(row)
