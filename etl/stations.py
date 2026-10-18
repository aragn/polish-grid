"""
Resolve PSE's connection-point text to stations in etl/stations.json.

PSE writes the place as free text and glues footnote marks onto it:
"ChoczewoA" (Choczewo, footnote A), "CPKK" (the planned CPK station,
footnote K), "WYPM", "NORy", "SE1 *" (a future station near Słupsk),
"Jarosław (planowana)".  Rows with several connection points list them
one per line ("Kromolice\nKromolice"), which the PDF text flattens into
"CPKK Stryków (Dmosin)".  Unknown names come back as-is, unplaced, so a
new station shows up in the build log instead of silently on the wrong
spot.
"""

import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))


def fold(text):
    text = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c)).lower()).strip()


class Stations:
    def __init__(self, path=os.path.join(HERE, "stations.json")):
        data = json.load(open(path, encoding="utf-8"))
        self.stations = data["stations"]
        self.lookup = {fold(name): name for name in self.stations}
        for alias, name in data["aliases"].items():
            self.lookup[fold(alias)] = name
        self.unknown = set()

    def one(self, text):
        """A single name, footnote marks and '(planowana)' stripped -> canonical name or None."""
        text = re.sub(r"\(\s*planowana\s*\)", " ", text, flags=re.I)
        text = re.sub(r"[*/]+", " ", text).strip()
        if not text:
            return None
        candidates = [text]
        # "SED", "SE EJF", "SE1B", "SE2 **": placeholders with a footnote letter.
        match = re.fullmatch(r"SE\s*(\d)?\s*([A-ZŁ]{0,3})", text)
        if match:
            number, letters = match.groups()
            if letters.startswith("EJ"):
                candidates.append("SE EJ")
            elif number:
                candidates.append(f"SE{number}")
            elif letters == "D":
                candidates.append("SE3")
            else:
                candidates.append("SE")
        candidates.append(re.sub(r"(?<=[a-ząćęłńóśźż])[A-ZŁŚŻ]$", "", text))   # ChoczewoA, NorkiY, KuźnicaŁ
        candidates.append(re.sub(r"^([A-Z]{3})[A-ZŁa-z]$", r"\1", text))     # CPKK, WYPM, NORy, WIGY
        for candidate in candidates:
            name = self.lookup.get(fold(candidate))
            if name:
                return name
        return None

    def resolve(self, text):
        """Free text -> list of canonical station names, in PSE's order, without repeats."""
        found = []
        text = re.sub(r"\n\s*\(", " (", str(text or ""))  # "Jarosław\n(planowana)" is one name
        for part in re.split(r"[;\n]", text):
            part = part.strip()
            if not re.sub(r"\(\s*planowana\s*\)", "", part).strip():  # PSE sometimes gives only "(planowana)"
                continue
            name = self.one(part)
            if name:
                found.append(name)
                continue
            # Several names flattened into one line: take the longest known run of words each time.
            tokens = re.split(r"\s+|/", part)
            index = 0
            while index < len(tokens):
                for end in range(len(tokens), index, -1):
                    name = self.one(" ".join(tokens[index:end]))
                    if name:
                        found.append(name)
                        index = end
                        break
                else:
                    if re.search(r"[A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ]{3,}", tokens[index]) and tokens[index].lower() != "planowana":
                        self.unknown.add(part)
                    index += 1
        return list(dict.fromkeys(found))

    def place(self, name):
        station = self.stations.get(name) or {}
        return station.get("lat"), station.get("lon")
