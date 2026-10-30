# Polish Grid

Power system data for Poland. First page: **PSE's grid-connection queue**, every
project that has asked PSE (the transmission system operator) to connect to its
grid, on a map, in a filterable table, and with each project's history since 2020.
Second page, `output/`: **unit output (JWCD)**, the metered quarter-hour output of
PSE's centrally dispatched units, read live from PSE's API (see below).

Open `index.html` through any static server (GitHub Pages serves it from `main`).

## What the page shows

- **Map**: projects at the PSE substation they asked to connect to, coloured by
  technology and shaded by phase, over the 220/400 kV grid from OpenStreetMap.
  The month slider replays PSE's monthly lists since March 2020.
- **Technology × phase matrix**: counts or GW per cell; click a row, column or
  cell to hide or show it. The table and the timing section follow the filter.
- **Table**: every project with its station, MW, phase and dates; a click opens
  its status milestones (application → completeness → conditions → agreement →
  connection date), the history of its connection date, and every change between
  monthly lists.
- **Timing**: the queue month by month by phase, and the median months between
  milestones per technology.

## Sources

All from PSE's page
[Wykaz obiektów planowanych do przyłączenia](https://www.pse.pl/obszary-dzialalnosci/krajowy-system-elektroenergetyczny/wykaz-obiektow-planowanych-do-przylaczenia)
(information published under Art. 7(8l) of the Energy Law).

| File | Layout | Months used |
|---|---|---|
| `Informacje_publikowane_zgodnie_z_Art_7_ust_8l_ustawy_PE_stan_na_DD_MM_YYYY.xlsx` | one list of every application with its status and all its dates, names and ids | from July 2026 |
| `Wykaz_podmiotow_ubiegajacych_sie_o_przylaczenie.pdf` / `.xls` | projects with conditions or an agreement; station, MW, type, applicant, conditions date, agreement date, connection date | March 2020 – June 2026 (scans Sept 2017 – Feb 2020, kept in `archive/unparsed/`) |
| `Informacja_na_temat_zlozonych_wnioskow_..._potwierdzono_kompletnosc` | applications confirmed complete (type, station, MW, date) | April 2024 – June 2026 |
| `Informacja_na_temat_wnioskow_..._oczekujacych_na_weryfikacje` | applications awaiting verification | April 2024 – June 2026 |
| `Wykaz_wydanych_odmow_..._od_01012023` | refusals since 2023, by quarter | March 2024 – June 2026 |

PSE's site keeps **every uploaded version** of a document: `<file>?version=1.N`
returns the N-th upload, with its upload time as `Last-Modified`. That is where
the history comes from. `data/sources.json` lists every version read (URL, upload
time, the date it describes, rows); the files themselves are in `archive/`.

Stations and lines: © OpenStreetMap contributors (ODbL). Voivodeships: GUGiK PRG
via [polska-geojson](https://github.com/ppatrzyk/polska-geojson) (MIT).

## How the history is built

`etl/build.py`. PSE gives projects no stable id (only some new applications
carry a `WWP/...` number), so a project is followed from one monthly list to the
next by its conditions date, station, MW, applicant and, since July 2026, its
name (`link()`, a scored one-to-one matching). The results to keep in mind:

- **Past connection date**: PSE keeps plants listed with their agreement in force
  long after they start (Kozienice 11 from 2017, Jaworzno 910 MW from 2020), and
  some projects leave the list at their deadline without being built (Elektrownia
  Północ, Pelplin). So this phase means *likely* in service, not confirmed.
- **Storage, distribution and demand before July 2025**: PSE listed them in a
  separate file (`Wykaz_MME_SD_ODB_...`) whose versions are no longer on its site.
  They enter the history in July 2025, with their real conditions dates.
- **Application dates**: given by PSE only since July 2026. For earlier projects
  the completeness date is matched from the list of complete applications (same
  station, MW and type, within the 13 months before the conditions).
- **Stations**: PSE writes free text with footnote marks glued on ("ChoczewoA",
  "CPKK", "WYPM", "SE1 *"). `etl/stations.json` maps them to OpenStreetMap
  substations or, for planned ones, to the place PSE names; its notes say which.
  The footnotes show, for example, that "SE1, future station near Słupsk" became
  Krzemienica and "SE2 near Żarnowiec" became Choczewo.

## Running it

```bash
pip install -r requirements.txt
python etl/update.py              # new monthly files only (what the daily job runs)
python etl/update.py --backfill   # every version of every old file (slow: PSE rate-limits)
python etl/update.py --rebuild    # rebuild data/ from data/snapshots/
```

PSE's firewall answers 401 after about a hundred quick requests; `etl/pse.py`
waits two seconds between requests and three minutes after a 401.

`.github/workflows/update.yml` runs the update every morning and commits
`data/` and `archive/` when PSE has published something new. A run fails if PSE's
page stops linking a file in the expected form, so a layout change shows up.

## Layout

```
index.html                    the page
data/queue.json               every project with its history (what the page reads)
data/history.json             the queue by month, phase and technology; refusals by quarter
data/snapshots/<list>/<date>.json  each monthly list as parsed
data/sources.json             every PSE file version read
archive/                      PSE's files, one per list and month
etl/pse.py                    where the files are, version crawling, ingest
etl/pse_parse.py              one parser per layout
etl/stations.py, stations.json  connection points -> stations
etl/build.py                  linking across months, milestones, phases
etl/update.py                 the entry point
geo/                          voivodeships, 220/400 kV lines
output/index.html             the unit output page (no build step, no stored data)
output/plants.json            fuel, operator and place per PSE plant name
```

## Unit output page

`output/` reads PSE's public API in the browser for the chosen dates (up to 31
days): [`gen-jw`](https://api.raporty.pse.pl/api/gen-jw) for metered output
(average MW per quarter-hour, generation and pumping) and
[`pdwkseub`](https://api.raporty.pse.pl/api/pdwkseub) for capacity (available +
declared non-usable + grid limit; published for roughly half the units, and a day
after the output). Energy is MW × 0.25 h. Rows are keyed by `dtime_utc`, since
PSE's local `dtime` is ambiguous on the October clock change (`gen-jw` writes the
repeated hour as `02a:15`, `pdwkseub` repeats `02:15`). The fuel, operator and
place in `output/plants.json` are plant-level labels from the TGE register and
operators' pages; edit that file when PSE adds a plant (unmapped plants show as
"Not mapped").
