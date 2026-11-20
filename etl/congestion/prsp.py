"""
Lines that PSE's development plan (PRSP 2025-2034, agreed with URE on
20 December 2024) modernises or rebuilds, as station pairs:

    pdftotext -enc UTF-8 -layout PRSP_2025_2034.pdf prsp.txt
    python etl/congestion/prsp.py prsp.txt W/cache/prsp_lines.json

PSE modernises its old lines mostly to raise their permitted current (the
plan's own words: "zwiększenie dopuszczalnej obciążalności prądowej"),
because many were built for a 40-60 degrees C conductor.  network.py treats
the lines listed here as thermally limited in 2025.  Chains ("Morzyczyn-
Dunowo-Słupsk") become consecutive pairs; items about fibre (OPGW) or field
emissions are skipped.
"""

import json
import re
import sys

ITEM = re.compile(r"(Modernizacja|Przebudowa|Zwiększenie)[^\n]{0,40}?lini[iaę]\s+(220|400)\s*kV\s+([^\n]{3,90})")
SKIP = ("OPGW", "oddziaływania")


def stations(text):
    text = re.sub(r"\s{2,}.*", "", text)            # the next table column
    text = re.sub(r"\b(tor\s+I+|tor\s+\w+|na odcinku|z budową|wraz z|w zakresie).*", "", text)
    text = text.replace("(", "").replace(")", "")
    parts = [p.strip(" ,.") for p in re.split(r"\s*[–-]\s*", text)]
    out = []
    for p in parts:
        p = p.split("/")[0].strip()
        if len(p) >= 3 and not p[0].islower():
            out.append(p)
    return out


def main():
    text = open(sys.argv[1], encoding="utf-8").read()
    pairs = set()
    for m in ITEM.finditer(text):
        if any(s in m.group(3) for s in SKIP):
            continue
        st = stations(m.group(3))
        for a, b in zip(st, st[1:]):
            pairs.add((a, b, int(m.group(2))))
    rows = sorted(pairs, key=lambda p: (p[2], p[0]))
    json.dump(rows, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    for r in rows:
        print(*r)
    print(len(rows), "line sections")


if __name__ == "__main__":
    main()
