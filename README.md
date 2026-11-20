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
grid/index.html               the grid capacity page
data/grid_capacity.json       available connection capacity by group, 2026-2031 (etl/grid_capacity.py)
etl/congestion/               the screening model behind the page's modelled line loading (its README)
data/congestion/              the model's 2025 results the page draws (etl/congestion/export.py)
archive/grid_capacity/        the operators' capacity PDFs and TAURON's map data
land/index.html               the land page
data/land/                    plots around the shortlisted substations (etl/land.py)
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

## Grid capacity page

`grid/` shows the available connection capacity ("wartość łącznej dostępnej mocy
przyłączeniowej") each operator publishes under art. 7 ust. 8l of the Energy Law,
with a five-year outlook since 30 April 2026. `etl/grid_capacity.py` reads:

- TAURON: the JSON behind its map, every GPZ with coordinates, its group's 110 kV
  value and its own medium-voltage value, 2026-2031. The API answers 429 after a few
  hundred quick calls, so the script paces about 120 calls and reads it once a week
  (`--force` reads it now). The map gives no date.
- Energa: two PDFs (generation, demand), groups of 110 kV stations, 2026-2031 and 2035.
- PGE: one PDF, groups of 110 kV stations, 2026-2031.
- PSE: one PDF, 220/400 kV stations, 2026 and 2031, step 1 and step 2 (the published
  one, 0 everywhere in 2026), and the demand capacity of PSE's planned stations.

PSE's planned demand stations get the completion year of the investment that builds
them in PSE's development plans (agreed PRSP 2025-2034 and the 2027-2036 draft),
matched by hand in `etl/pse_new_stations.json`; the page fills them in once built by
the chosen year and charts them by year.

**Modelled line loading in 2025.** A second map on the page comes from
`etl/congestion/`. That is a DC power-flow model of the 110-400 kV grid, built
from OpenStreetMap with the neighbours' 220/400 kV grids, run for every hour of
2025 with PSE's metered unit output, demand, wind, solar and border flows and
ENTSO-E's outages. It shows how often each 220/400 kV line would have gone over
its estimated rating after a single outage, and what 100 MW of solar, wind or a
battery at each 220/400 kV substation would change. These are estimates from
public data: no operator publishes ratings or internal flows. The method and the
caveats are in `etl/congestion/README.md`.

ENEA and Stoen ask for consent before their values are republished station by
station, so they are left out. Groups are placed at their stations (TAURON's own
coordinates; for the others the nearest OpenStreetMap substation or town of that
name within 90 km of the group's other stations). Live queue projects are counted
per group by TAURON's GPZ code or by station name: applications filed or under
study are not yet in the operators' figures; conditions and agreements are.

The page also shows PSE's cuts of wind and solar output since 24 September 2024
(`etl/curtailment.py`, API dataset `poze-redoze`): hourly MW split into balancing
and network reasons, for the whole country. PSE revises past days, so the whole
series is read every run. The share of output uses ENTSO-E solar and wind output
from `data/generation/PL/`.

## Land page

`land/` shows the registered land plots (działki ewidencyjne) of 1 ha or more within
3 km of the substations of the groups with room for at least 50 MW of generation or
demand in 2030, on Esri satellite imagery, filtered by the area a solar farm (MWp x ha
per MWp) or a battery (about 2 ha per 100 MWh plus 0.5 ha) needs. `etl/land.py` reads
them from GUGiK's collective EGiB WFS (`ms:dzialki`), which answers from GitHub's
runners but not from every network, in `.github/workflows/land.yml` (weekly; each
substation re-read after 90 days; bump `VERSION` to re-read all). It keeps the plot
identifier, municipality, precinct, number, area computed from the outline, distance to
the substation, and where the county publishes them the land and soil classes and the
owner group (the kind of owner, never a name). Outlines are simplified to about 1.5 m.

Substations are placed at the operator's coordinates (TAURON), else at an OpenStreetMap
110 kV substation of that name (`etl/osm_substations.py` writes
`etl/osm_substations_110.json` from the Overpass API, cleaning names like
`Stacja elektroenergetyczna 110/15kV „GPZ Zamość Janowice”`), else at the 110 kV
substation within 3 km of the town of that name.
