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

**Station card.** Clicking a group, a PSE station or one of a group's stations opens,
beside the group's card, a card for that station: an aerial image of it (GUGiK's
orthophoto from the Geoportal WMS, loaded by the browser; Esri World Imagery when the
Geoportal doesn't answer, as from some networks), the room left in the chosen year
against what waits in the queue and what is already agreed, the operator's outlook by
year, TAURON's medium-voltage room for that GPZ, PSE switchyards with no free bays,
the modelled effect of a 100 MW battery and the land plots around it. Station names in
the group's card switch it.

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

`etl/land_constraints.py` then overlays GUGiK's topographic database BDOT10k on each
plot: forest (OT_PTLZ_A), surface water (OT_PTWP_A), built-up land (OT_PTZB_A), transport
land (OT_PTKM_A) and road and railway lines as 8 m strips, giving the usable hectares
left (overlaps counted once), the distance to the nearest building (OT_BUBD_A) and the
buildings on the plot. BDOT10k comes per county as a zip of GeoPackages from
`opendata.geoportal.gov.pl/bdot10k/schemat2021/GPKG/<woj>/<county>_GPKG.zip` (runners
only; about 70 s a county, 206 counties). The page filters on usable hectares, average
width (2 x area / perimeter, to drop narrow strips) and distance from buildings.

`etl/land_protected.py` adds the protected areas, from the European Environment Agency's
ArcGIS services (GDOŚ's own service refuses the runners): Natura 2000 (end-2024 release)
and the Nationally designated areas (national parks PL01, nature reserves PL02, landscape
parks PL03, protected landscape areas PL04). Each plot gets the hectares inside each kind
(`n2`, `np`, `lp`, `ol`), the distance to the nearest Natura 2000 site (`n2d`) and the ids
of the areas it touches (`pz`, named in `data/land/protected.json`).
`data/land/protected_shapes.json` holds the areas within 4 km of the substations,
simplified to 25 m, for the map. It takes about 4 minutes for all plots and runs before
BDOT10k in `land.yml`. Dispatch with `protected_all` after a new EEA release.

`etl/land_zoning.py` reads the Ministry of Development's urban register
(`rejestr-urbanistyczny.gov.pl/uslugi-sieciowe/app-mpzp/wfs` and `app-pog/wfs`, since
October 2026 the only national source of plans) over the box around each substation's
plots. The register returns at most 10 features a request, whatever `count` asks for, so
the script follows `numberMatched`. GML coordinates come northing first. Each plot gets:
- `mp` (hectares inside a published local plan) and `mpz` (the plans);
- `pg` (the general-plan zones it falls in, with hectares).

`data/land/zoning.json` names them, with each zone's type and the land uses ("profiles")
a local plan may allow there, such as `PEF`, solar farm. Local plans in the register
carry only their outline, title and date. Many municipalities haven't published their
plans yet, so "no plan" on the page means "none in the register".

`etl/land_flood.py` reads Wody Polskie's flood hazard maps (INSPIRE view service
`INSPIRE_NZ_HY_MZPMRP_WMS`, river and sea floods, second cycle, made public in September
2022 and still the official maps). Its WFS sends whole river reaches of several MB each,
so instead the WMS draws the areas with our own SLD (two FeatureTypeStyles, so that the
1% areas are painted over the 0.2% ones). The script asks for one image per substation at
5 m a pixel, since the service draws nothing above 1:50,000, and samples each plot every
10 m. `q1` is the hectares in the 1% (or 10%) area, an area of special flood danger
under the Water Law, art. 16(34). `q02` is the further hectares flooded only at 0.2% or
if an embankment fails.

`etl/land_blocks.py` stores each plot's touching neighbours of 1 ha or more (`nx`,
written relative to the precinct). The page grows a site from a plot by adding its
largest neighbours that pass the filters, up to the number of plots the reader allows.
Joining all touching plots into blocks doesn't work: 70,000 of the plots form one
farmland mosaic.

The page also decodes the register's land-use and soil-class symbols (`RIIIa`,
`W/ŁIV`...) and owner groups 1–16 (§ 14 of the regulation on land and building
registers, Dz.U. 2024 poz. 219). Its planning note follows art. 14(6a) of the planning
act (Dz.U. 2026 poz. 538): ground-mounted solar needs a local plan on class I–III
farmland and forest at any size, on class IV above 150 kW, elsewhere above 1 MW. It also
follows art. 7(2) of the farmland protection act: class I–III farmland needs the
agriculture minister's consent.

Substations are placed at the operator's coordinates (TAURON), else at an OpenStreetMap
110 kV substation of that name (`etl/osm_substations.py` writes
`etl/osm_substations_110.json` from the Overpass API, cleaning names like
`Stacja elektroenergetyczna 110/15kV „GPZ Zamość Janowice”`), else at the 110 kV
substation within 3 km of the town of that name.
