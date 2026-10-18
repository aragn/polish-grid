"""
Parsers for PSE's connection-queue publications (Art. 7 ust. 8l of the
Energy Law), one per layout PSE has used:

  wykaz      Wykaz_podmiotow_ubiegajacych_sie_o_przylaczenie.pdf, monthly
             from March 2020 to June 2026: projects holding connection
             conditions or a connection agreement.  Generators only until
             June 2025; storage, distribution and demand from July 2025.
  kompletne  ..._dla_ktorych_potwierdzono_kompletnosc.xlsx: applications
             confirmed complete, under study (Feb 2025 - Jun 2026).
  weryfikacja ..._oczekujacych_na_weryfikacje.xlsx: applications awaiting
             the completeness check.
  odmowy     Wykaz_wydanych_odmow_..._od_01012023.xlsx: refusals.
  art7       Informacje_publikowane_zgodnie_z_Art_7_ust_8l_ustawy_PE_stan_na_
             DD_MM_YYYY.xlsx, one list of every application with its status
             and dates, from July 2026.

Every parser returns {"asof": "YYYY-MM-DD", "rows": [dict, ...]} with the
values as PSE wrote them, normalised only enough to compare across months
(numbers as floats, dates as ISO strings, footnote marks split off).
"""

import datetime
import re

POLISH_MONTHS = {
    "stycz": 1, "lut": 2, "marc": 3, "kwie": 4, "maj": 5, "czerw": 6,
    "lip": 7, "sierp": 8, "wrze": 9, "pa": 10, "listop": 11, "grud": 12,
}


def polish_date(text):
    """'31 października 2023' -> '2023-10-31'."""
    match = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", text or "", re.I)
    if not match:
        return None
    day, word, year = match.groups()
    word = word.lower()
    for stem, month in POLISH_MONTHS.items():
        if word.startswith(stem):
            return datetime.date(int(year), month, int(day)).isoformat()
    return None


def asof_from_text(text):
    match = re.search(r"stan\w*\s+na\s+(?:dzie[nń]\s+)?([^)\n]{6,40})", text or "", re.I)
    if not match:
        return None
    found = polish_date(match.group(1))
    if found:
        return found
    numeric = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", match.group(1))
    if numeric:
        day, month, year = map(int, numeric.groups())
        return datetime.date(year, month, day).isoformat()
    return None


NO_DATE = {"", "-", "n.d.", "n.d", "nd", "b/d", "n/d", "bd", "brak"}


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace(" ", " ")).strip()


def to_date(value):
    """PSE's dates: datetime cells, '2024.08.12', '12.08.2024', or a marker for none."""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = clean(value)
    if text.lower() in NO_DATE:
        return None
    match = re.fullmatch(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
        if not match:
            return text  # kept as written: the UI shows it, nothing computes with it
        day, month, year = map(int, match.groups())
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:  # e.g. '2031.09.31' in the August 2026 file
        return f"{year:04d}-{month:02d}-{min(day, 28):02d}" if 1 <= month <= 12 else text


def to_mw(value):
    """'319,75E' -> (319.75, True); '-' -> (None, False).  E marks a request to change the conditions."""
    if isinstance(value, (int, float)):
        return float(value), False
    text = clean(value)
    if not text or text in {"-", "–"}:
        return None, False
    changed = bool(re.search(r"\d\s*E\b|\dE$", text))
    number = re.search(r"\d+(?:[.,]\d+)?", text)
    if not number:
        return None, changed
    return float(number.group(0).replace(",", ".")), changed


def split_values(value):
    """'129;\n240' -> ['129', '240']: rows with several connection points give one value per point,
    one per line; the PDF text may flatten the lines into spaces ('27 45 21')."""
    if isinstance(value, (int, float)):
        return [value]
    return [part.strip() for part in re.split(r"[;\n]|\s+(?=\d)", str(value or "")) if part.strip()]


def sum_mw(value):
    parts = split_values(value)
    total, changed, seen = 0.0, False, False
    for part in parts:
        mw, flag = to_mw(part)
        changed = changed or flag
        if mw is not None:
            total += mw
            seen = True
    return (round(total, 6) if seen else None), changed


# --------------------------------------------------------------- wykaz (PDF)

def parse_wykaz_pdf(path):
    import pdfplumber
    rows, asof, notes, two_mw = [], None, [], False
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            asof = asof or asof_from_text(text)
            for line in text.splitlines():
                if re.match(r"^(\*+|[A-ZŁŚŻ]\d?)\s*[-–]\s+\S", line.strip()):
                    notes.append(line.strip())
            for table in page.extract_tables():
                for raw in table:
                    cells = [clean(cell) for cell in raw]
                    first = cells[0] if cells else ""
                    if first.startswith("L.p"):
                        # From July 2025 the MW splits into injected and withdrawn.
                        two_mw = any("pobierana" in cell for cell in cells)
                        continue
                    match = re.fullmatch(r"(\d+)\s*([A-ZŁ]\d?|\*+)?", first)
                    if not match:
                        continue
                    rows.append(wykaz_row(cells, match, two_mw, [cell or "" for cell in raw]))
    return {"asof": asof, "rows": rows, "notes": notes}


def wykaz_row(cells, match, two_mw, raw=None):
    """raw: the cells before whitespace was tidied, so values one per line stay apart."""
    raw = raw or cells
    if two_mw:
        lp, station, mw_in, mw_out, kind, applicant, seat, conditions, agreement, deadline = cells[:10]
        mw_in, mw_out = raw[2], raw[3]
    else:
        lp, station, mw_in, kind, applicant, seat, conditions, agreement, deadline = cells[:9]
        mw_in, mw_out = raw[2], None
    station = lines_text(raw[1]) or station
    mw_in_value, changed_in = sum_mw(mw_in)
    mw_out_value, changed_out = sum_mw(mw_out) if mw_out is not None else (None, False)
    return {
        "lp": int(match.group(1)), "note": match.group(2) or "",
        "station": station, "mw_in": mw_in_value, "mw_out": mw_out_value,
        "change": changed_in or changed_out, "type": kind,
        "applicant": applicant, "seat": seat,
        "conditions": to_date(conditions), "agreement": to_date(agreement), "deadline": to_date(deadline),
    }


# ------------------------------------------------------- the xlsx/xls lists

def sheet_rows(path):
    if str(path).lower().endswith(".xls"):
        import xlrd
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_index(0)
        out = []
        for index in range(sheet.nrows):
            row = []
            for cell in sheet.row(index):
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                else:
                    row.append(cell.value)
            out.append(row)
        return out
    import openpyxl
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [list(row) for row in book.worksheets[0].iter_rows(values_only=True)]
    finally:
        book.close()


def find_header(rows, *words):
    for index, row in enumerate(rows[:15]):
        text = " ".join(clean(cell).lower() for cell in row)
        if all(word in text for word in words):
            return index
    return None


def asof_from_rows(rows):
    for row in rows[:6]:
        for cell in row:
            found = asof_from_text(clean(cell))
            if found:
                return found
    return None


def columns_by_header(header, spec):
    """spec: {field: [substrings]} -> {field: column index} (first match wins)."""
    found = {}
    lowered = [clean(cell).lower() for cell in header]
    for field, needles in spec.items():
        for index, cell in enumerate(lowered):
            if any(needle in cell for needle in needles) and index not in found.values():
                found[field] = index
                break
    return found


def parse_wykaz_xls(path):
    """The .xls twin of the PDF list (same columns, ten-column layout)."""
    rows = sheet_rows(path)
    header_index = find_header(rows, "miejsce", "wnioskodawca")
    two_mw = any("pobierana" in clean(cell) for cell in rows[header_index])
    out = []
    for raw in rows[header_index + 1:]:
        cells = [clean(cell) if not isinstance(cell, (int, float, datetime.datetime)) else cell for cell in raw]
        first = cells[0]
        if isinstance(first, float):
            first = str(int(first))
        match = re.fullmatch(r"(\d+)\s*([A-ZŁ]\d?|\*+)?", clean(first))
        if not match:
            continue
        cells = [first] + [cell if isinstance(cell, datetime.datetime) else
                           (str(cell) if isinstance(cell, (int, float)) and index in (2, 3) else cell)
                           for index, cell in enumerate(cells[1:], start=1)]
        # Excel dates are serials in this file.
        for index in (7, 8, 9):
            if index < len(cells) and isinstance(raw[index], float) and raw[index] > 20000:
                cells[index] = (datetime.datetime(1899, 12, 30) + datetime.timedelta(days=raw[index])).date().isoformat()
        out.append(wykaz_row([clean(cell) if not isinstance(cell, datetime.datetime) else cell.date().isoformat()
                              for cell in cells], match, two_mw))
    return {"asof": asof_from_rows(rows), "rows": out, "notes": []}


def pdf_rows(path):
    """All table rows of a PDF, and its text (for the 'stan na' date)."""
    import pdfplumber
    rows, text = [], ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"
            for table in page.extract_tables():
                rows.extend(table)
    return rows, text


def lines_text(value):
    """Keep one line per connection point, tidy each line."""
    return "\n".join(clean(line) for line in str(value or "").splitlines() if clean(line))


SIMPLE_SPEC = {
    "type": ["typ", "rodzaj"],
    "station": ["miejsce", "lokalizacja", "stacja"],
    "kv": ["rozdzielnia", "napi"],
    "mw_in": ["wprowadzan"],
    "mw_out": ["pobieran"],
    "mw": ["moc"],
    "date": ["data"],
}


def parse_simple_list(path, kind):
    """kompletne / weryfikacja / odmowy: type, place, kV, MW in/out and a date (or a quarter) per row."""
    if str(path).lower().endswith(".pdf"):
        rows, text = pdf_rows(path)
        asof = asof_from_text(text)
    else:
        rows = sheet_rows(path)
        asof = asof_from_rows(rows)
    header_index = None
    for index, row in enumerate(rows[:12]):
        lowered = " ".join(clean(cell).lower() for cell in row)
        if ("typ" in lowered or "rodzaj" in lowered) and ("moc" in lowered or "miejsce" in lowered):
            header_index = index
            break
    if header_index is None:
        return {"asof": asof, "rows": [], "notes": ["header not found"]}
    header = [clean(cell) for cell in rows[header_index]]
    # The refusals put "stacja" and "poziom napięcia" on a second header row.
    if header_index + 1 < len(rows) and any("stacja" in clean(cell).lower() for cell in rows[header_index + 1]):
        header = [h or clean(sub) for h, sub in zip(header, rows[header_index + 1])] + header[len(rows[header_index + 1]):]
    columns = columns_by_header(header, SIMPLE_SPEC)
    if "mw_in" in columns:
        columns.pop("mw", None)
    out, quarter = [], None
    for raw in rows[header_index + 1:]:
        if not raw or all(clean(cell) == "" for cell in raw):
            continue
        first = clean(raw[0])
        found = re.match(r"^([IV]+)\s*kw\.?\s*(\d{4})", first)
        if found:
            quarter = f"{found.group(2)}-Q{['I', 'II', 'III', 'IV'].index(found.group(1)) + 1}"
            continue
        get = lambda field: raw[columns[field]] if field in columns and columns[field] < len(raw) else None
        kind_value = clean(get("type"))
        station = lines_text(get("station"))
        if not kind_value or not station or kind_value.lower().startswith(("typ", "[", "rodzaj")) or len(kind_value) > 40:
            continue
        mw_in, changed_in = sum_mw(get("mw_in") if "mw_in" in columns else get("mw"))
        mw_out, changed_out = sum_mw(get("mw_out"))
        out.append({
            "type": kind_value, "station": station, "kv": clean(str(get("kv") or "").replace("\n", " ")),
            "mw_in": mw_in, "mw_out": mw_out, "change": changed_in or changed_out,
            "date": to_date(get("date")) if "date" in columns else None, "quarter": quarter,
        })
    return {"asof": asof, "rows": out, "notes": []}


# ------------------------------------------------------------ art7 (xlsx)

ART7_STATUSES = {
    "WNIOSEK w weryfikacji": "verification",
    "WNIOSEK niekompletny": "incomplete",
    "WNIOSEK kompletny - w trakcie analizy technicznej i ekonomicznej": "study",
    "WNIOSEK wycofany": "withdrawn",
    "WARUNKI PRZYŁĄCZENIA wydane": "conditions",
    "ODMOWA PRZYŁĄCZENIA": "refused",
    "WARUNKI PRZYŁĄCZENIA utraciły ważność": "expired",
    "UMOWA O PRZYŁĄCZENIE obowiązująca": "agreement",
    "UMOWA O PRZYŁĄCZENIE rozwiązana": "terminated",
    "UMOWA O PRZYŁĄCZENIE - obiekt przyłączony": "connected",
}


def art7_status(text):
    text = clean(text)
    for label, key in ART7_STATUSES.items():
        if text.lower() == label.lower():
            return key
    lowered = text.lower()
    for needle, key in (("weryfikac", "verification"), ("niekomplet", "incomplete"), ("kompletny", "study"),
                        ("wycofan", "withdrawn"), ("odmow", "refused"), ("utracił", "expired"),
                        ("przyłączony", "connected"), ("rozwiązan", "terminated"), ("umowa", "agreement"),
                        ("warunki", "conditions")):
        if needle in lowered:
            return key
    return None


def parse_art7_xlsx(path):
    rows = sheet_rows(path)
    header_index = find_header(rows, "id obiektu", "nazwa obiektu")
    header = rows[header_index]
    spec = {
        "id": ["id obiektu"], "name": ["nazwa obiektu"], "operator": ["nazwa osd"],
        "applicant": ["podmiotu"], "station": ["lokalizacja"], "kv": ["poziom napi"],
        "multi": ["wiele miejsc"], "type": ["rodzaj"],
        "mw_in": ["wprowadzana"], "mw_out": ["pobierana"],
        "pv": ["pv"], "wind": ["fw"], "storage_out": ["rozładowania", "rozladowania"],
        "storage_in": ["ładowania", "ladowania"], "load": ["odb"], "other": ["inne"],
        "status": ["status"], "applied": ["złożenia wniosku"], "advance": ["zaliczki"],
        "conditions": ["określenia warunków"], "refused": ["odmowy wydania"], "refusal_reason": ["uzasadnienie"],
        "expired": ["utraty ważności"], "agreement": ["zawarcia umowy"], "delivery": ["rozpoczęcia dostarczania"],
        "terminated": ["rozwiązania"], "lapsed": ["wygaśnięcia"],
    }
    columns = columns_by_header(header, spec)
    out, notes = [], []
    for raw in rows[header_index + 1:]:
        if not raw or all(clean(cell) == "" for cell in raw):
            continue
        get = lambda field: raw[columns[field]] if field in columns and columns[field] < len(raw) else None
        first, name = clean(get("id")), clean(get("name"))
        if re.fullmatch(r"[A-ZŁŚŻ]\d?-", first) and not clean(get("status")):
            notes.append(f"{first[:-1]} - {name}")
            continue
        status = art7_status(get("status"))
        if not name and not status:
            continue
        mw_in, changed_in = sum_mw(get("mw_in"))
        mw_out, changed_out = sum_mw(get("mw_out"))
        installed = {}
        for field in ("pv", "wind", "storage_out", "storage_in", "load", "other"):
            value, _ = sum_mw(get(field))
            if value:
                installed[field] = value
        out.append({
            "id": "" if first in ("-", "") else first, "name": name,
            "operator": clean(get("operator")), "applicant": clean(get("applicant")),
            "station": lines_text(get("station")),
            "kv": clean(str(get("kv") or "").replace("\n", " ")), "multi": clean(get("multi")) == "TAK",
            "type": clean(get("type")), "mw_in": mw_in, "mw_out": mw_out, "change": changed_in or changed_out,
            "installed": installed, "status": status, "status_pl": clean(get("status")),
            "applied": to_date(get("applied")), "advance": to_date(get("advance")),
            "conditions": to_date(get("conditions")), "refused": to_date(get("refused")),
            "refusal_reason": clean(get("refusal_reason")) if clean(get("refusal_reason")) not in ("-", "") else "",
            "expired": to_date(get("expired")), "agreement": to_date(get("agreement")),
            "delivery": to_date(get("delivery")), "terminated": to_date(get("terminated")), "lapsed": to_date(get("lapsed")),
        })
    return {"asof": asof_from_rows(rows), "rows": out, "notes": notes}


PARSERS = {
    ("wykaz", "pdf"): parse_wykaz_pdf,
    ("wykaz", "xls"): parse_wykaz_xls,
    ("kompletne", "xlsx"): lambda path: parse_simple_list(path, "kompletne"),
    ("kompletne", "pdf"): lambda path: parse_simple_list(path, "kompletne"),
    ("weryfikacja", "xlsx"): lambda path: parse_simple_list(path, "weryfikacja"),
    ("weryfikacja", "pdf"): lambda path: parse_simple_list(path, "weryfikacja"),
    ("odmowy", "xlsx"): lambda path: parse_simple_list(path, "odmowy"),
    ("odmowy", "pdf"): lambda path: parse_simple_list(path, "odmowy"),
    ("art7", "xlsx"): parse_art7_xlsx,
}
