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

## Maturity and developer record

Projects holding connection conditions get a maturity score on the queue page (column, card, CSV), from rules
backtested in November 2026 on TAURON solar projects of 50 kW-1 MW, with outcomes from the lists and URE's register of
small installations: of each band, this share reached an agreement or was built within two to three years, high 59%,
medium 37%, low 11%. For projects holding an agreement the same signals had no skill, so they get no score.

As evidence for those, `etl/developers.py` writes `data/developers.json`: each applicant's projects in every list
and how they ended (connected or running, withdrawn, refused or lapsed only when a list says so, still queued), and,
from the Energy Regulatory Office's public API (api.ure.gov.pl, reused with the source and date named), its electricity
generation and storage licences and its installations in the register of small installations. Names are matched
after cutting any appended address and the legal form; numbered project companies ("Greenvolt Energy 16") are also
grouped; URE's registers are matched only for names with a legal form. Only counts and dates are kept. The project
card shows the record; it is not part of the score until outcomes from 2027 can test it. Run it after the queue
updates (it reads two URE resources, within URE's limit of 3 requests per 10 seconds).

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

`land/` shows the registered land plots (działki ewidencyjne) of 0.5 ha or more (1 ha until November 2026) within
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

`etl/land_blocks.py` stores each plot's touching neighbours (`nx`,
written relative to the precinct). The page grows a site from a plot by adding its
largest neighbours that pass the filters, up to the number of plots the reader allows.
Joining all touching plots into blocks doesn't work: 70,000 of the plots form one
farmland mosaic.

`etl/land_search.py` writes `data/land/search/<province>.json` (the TERYT province code;
`index.json` lists them with sizes): every plot once, at its nearest substation, with only
what the filters read (area, usable area, building distance, width, slope, flags for
protected areas, flood, soil and permits, owner groups, general-plan zone, local-plan
area, centre, 10 distances and neighbours as row indices). 68 MB for the country, about
21 MB compressed; a file is rewritten only when its content changes. The page's
**area-wide search** ("Where to look") loads one province, or all of them, runs the
same filters and neighbour joining over every substation of the groups with room, and
lists the best sites; clicking one opens its group on the usual map with the plot card.
Neighbouring plots aren't joined across a province border, and the wind act's
distances to parks, reserves and 220/400 kV lines (which need the outline) are checked
when the site is opened.

Everything else on the page is worked out in the browser from these fields:

- **What would find more:** each active filter relaxed by one step (a shorter building
  distance, narrower strips, one protected-area class fewer, class I–III soil, the 1% flood
  area, any owner or zoning, one more plot combined, one more km), with the number of sites
  it adds. Click one to apply it.
- **Distances:** `etl/land_constraints.py` also stores `d`, the distance in metres from each
  plot's edge to the nearest object of 18 kinds in BDOT10k (homes, housing areas, public
  roads, any road, motorways and main roads, railways, forest, lakes, rivers, high- and
  medium-voltage lines, substations, solar farms, other power plants, industrial areas, waste
  sites, cemeteries, airfields), in the order `index.json` lists as `distance_keys`; 0 when one
  touches the plot, null when none is within 3 km. The page's distance filters leave a plot out
  when one is closer than the buffer (homes, housing, high-voltage lines, railways, main roads,
  cemeteries, airfields) or when no public road is near enough. The buffers aren't cut out of
  the plot's area. Two presets ("solar" and "battery, common buffers") are starting points used
  in practice, not legal minimums.
- **Terrain:** `etl/land_terrain.py` reads the Copernicus DEM GLO-30 (about 30 m, from the
  AWS open-data copy, only the window around each substation) and adds `el` (mean height, m),
  `sl` (mean slope, degrees), `sf` (% flatter than 2°) and `ss` (% of the sloping part facing
  south-east to south-west). It is a surface model, so trees and buildings steepen edges. The
  page filters on mean slope and checks it: flat ground for a battery (70% under 2°), a gentle
  or south-facing slope for solar.
- **Power lines on the map:** `etl/land_grid.py` writes `data/land/grid/<station>.json`, the
  BDOT10k power lines within 4 km of each shortlisted substation by class (medium voltage,
  110 kV, 220/400 kV) and its substations, plus the names and voltages of the 110 kV+
  substations OpenStreetMap has (`etl/osm_substations_110.json`). Only the two layers needed are
  read out of each county's BDOT10k zip, by HTTP range requests (about 2 MB of 100–400 MB, some
  12 s a county). Redone every 180 days. The map draws the lines by voltage with labels, above
  the plots but letting clicks through. Low-voltage lines and transformer stations aren't in
  BDOT10k: the map offers GUGiK's utility map (GESUT, layer `przewod_elektroenergetyczny`) as an
  extra layer, which draws only at 1:2,000 and doesn't answer from every country.
- **Possible cable-pooling hosts:** the same step stores BDOT10k's power plants with their outlines
  (`plants`, solar farms among them; no names, since some are sole traders') and wind turbines
  (`turbines`, `OT_BUWT_P` "turbina wiatrowa", with height and whether built). The map draws them,
  and the plot card notes a solar farm within 3 km as a possible host to share a connection with.
- **Steps for a developer:** a panel with the steps for the chosen project type: land and zoning,
  the environmental decision, connection conditions (since 30 April 2026, Dz.U. 2026 poz. 516: fee
  PLN 1/kW up to 100,000; advance PLN 60/kW up to 6 million; conditions valid one year), the
  connection agreement (security within 14 days; a final building permit within 24 months, 36 for
  wind, for 80% of the capacity, and for a battery 80% of the power and of the energy; art. 7(2a)),
  flexible and configurable agreements where the grid is full (art. 7¹ and 7²), the permit
  deadlines for agreements signed before 30 April 2026 (art. 13–14 of the amending act),
  sharing an existing connection (cable pooling for any installation or battery, art. 7(1f); a
  second point of consumption without new conditions, art. 7(1bb)–(1bg); brownfield checks),
  and the building permit. The amending act is the government's bill UC84 (Sejm print 2150); the
  deregulation act Dz.U. 2026 poz. 900 changed only a cross-reference in it. Checked against the
  acts in force on 25 November 2026.
- **Grid connection (plot card):** worked out in the browser for the clicked plot and the project in
  the side panel (`connectionHtml` in `land/index.html`, parameters in `CONNECT`): the voltage and
  connection group (above 20 MW: 110 kV, group II; grid regulation Dz.U. 2023 poz. 819 § 3) and the
  operator's deadline for conditions (150 or 120 days, Energy Law art. 7(8g)); the route (straight
  distance × 1.3) and the other registered plots the straight route crosses, with their owner groups
  (a lower bound: plots under 0.5 ha and roads aren't in the files); the nearest MV and 110 kV lines
  and the 110 kV lines ending at the GPZ (BDOT10k, `data/land/grid`); the options with cost ranges
  (110 kV cable to a line bay with the plant's own substation; 30 kV cables to a transformer beside
  the GPZ on short routes; an MV connection up to 20 MW; cable-pooling hosts within 3 km; a loop-in on
  the 110 kV line only when the group is full); the operator's fee (half of its actual cost for
  storage, art. 7(8) pkt 6; full for a source, half for renewables up to 5 MW, pkt 3; the
  "connection" being the section joining the plant to the network, art. 7(8¹)); the application fee,
  advance and security; and the permits (building permit above 15 kV and for batteries above
  2,000 kWh, Building Law art. 29; overhead 110 kV lines on the EIA list, § 3(1)(7) of Dz.U. 2019
  poz. 1839, cables not; road-lane permit, Roads Act art. 40; easements, Civil Code art. 305¹–305²).
  The 110 kV cable cost comes from recent projects (TAURON Krzywa–Bolesławiec 2025: 13 km for over
  PLN 42 m; PGE Łomianki–Czosnów: 14.8 km double circuit, PLN 51 m); the bay, on-site substation, MV
  cable and easement figures are assumptions marked in `CONNECT`, to replace with quotes. Checked
  against the acts in force on 26 November 2026.
- **Soil classes:** the legal soil class (I–VI) is on 86k of the 318k plots, where the county's
  EGiB service publishes it. KIEG (GUGiK's integration of the county services) has nothing more:
  none of 40 sampled counties without classes had them there (24 Nov 2026). The map offers GUGiK's
  soil-agricultural map (agricultural soil complexes, WMS layer `0` of
  `mapy.geoportal.gov.pl/wss/service/pub/guest/MapaGlebowoRolnicza/MapServer/WMSServer`, drawn from
  about 1:50,000 in) as a layer to look at: GUGiK says the free service is for viewing, so nothing
  is read from it into our data.
  - **The more granular version can be bought:** the soil-agricultural map as vector data (GML,
    1:5,000) from GUGiK's central resource (PZGiK), at the base rate of PLN 15 per map sheet (Table
    14, item 2, of the annex to the Geodetic and Cartographic Law, Dz.U. 2026 poz. 1389; indexed
    yearly, +4.5% in 2026; the exact fee comes in GUGiK's fee calculation). The area within 3 km of
    the 774 substations is about 16,800 km², some 6,500 sheets of 2.5 × 2 km: roughly PLN
    100,000–125,000. It is free for public tasks, education and research. With it, each plot could
    get an estimated soil quality (the complex, not the legal class).
- **Solar yield:** `etl/land_solar.py` adds `py`, the Global Solar Atlas PVOUT (kWh per kWp a
  year, 1999–2018, about 1 km) at each plot's centre, from `etl/gsa_pvout_poland.tif`. That file
  was downloaded once by hand from globalsolaratlas.info/download/poland, because the atlas's
  terms don't allow automated access, and is kept here under its CC BY 4.0 licence, with the
  citation the atlas asks for on the page. Across the plots it runs from 964 to 1,145 (median
  1,092), so it matters far less than the grid connection.
- **Wind:** `etl/land_wind.py` adds `ws`, the Global Wind Atlas 4.0 mean wind speed at 100 m and
  150 m (m/s), and `cf`, the capacity factor of its generic IEC class III turbine (%), at each
  plot's centre, from `etl/gwa_poland.tif` (6 MB: the three layers at 250 m, as bytes). The Poland
  files were downloaded once by hand from the atlas's GIS download page: its terms don't allow
  robots on the site and its API isn't for bulk downloads. CC BY 4.0, with the attribution the
  atlas asks for on the page. Across the plots the speed at 150 m runs from about 5.5 to 8.5 m/s
  (median 7.6) and the capacity factor from about 21% to 51% (median 40%). The page sorts wind
  sites by it. For screening only: a wind farm needs a year of measurements on site.
- **Aviation:** `etl/land_aviation.py --update-from DIR` turns the Civil Aviation Authority's
  shapefiles of building restricted areas (BRA) around civil aviation ground equipment (landing
  systems, radio stations, radio beacons, radars and weather radars; one zip per kind on
  ulc.gov.pl/zegluga-powietrzna/mapy-on-line, with no conditions for reuse) into
  `data/land/aviation.json` (237 pieces of equipment, 478 zones, the height rings; staff names in
  the edit fields left out). Each run then adds `av` to every plot: the zones that touch it, with
  the height limit above sea level at the plot, on the safe side of ULC's steps. Within 600 m of
  the equipment anything built needs ULC's agreement; in the zone around it, anything over 15 m;
  in the outer zone of beacons and weather radars (30 km for these), only wind turbines above the
  limit. Above a limit needs ULC's agreement (Aviation Law art. 86(7)), it isn't banned. About
  16% of the plots touch a zone, mostly a weather radar's. Download new zips by hand when ULC
  posts them. `--update-ols` reads the airports' obstacle limitation surfaces from the public
  feature services behind ULC's map of the civil airport register (77 airports with the planned
  ones such as CPK; approach outlines, the inner horizontal and conical surfaces as nested rings
  built from their height lines, approach height lines; heights of 0 and typos dropped), and each
  run adds `ap` per plot: the lowest surface at its centre, on the safe side of the steps. About
  5% of the plots lie under one. Military equipment is not included. PAŻP's obstacle and AIP data and openAIP are not used: their terms
  don't allow it.
- **Building permits:** `etl/land_permits.py` reads GUNB's register of building permit decisions
  since 2016 (one zip per province, every 28 days) and adds `gp`, each plot's own decisions as
  [date, type, new, MW], and `gn`, the distance to the nearest solar, battery and wind decision
  on another of our plots within 3 km. `data/land/permits.json` counts decisions by type per
  precinct, since 2016 and since 2022. The type and MW are read from the free text; the free
  text itself, the investor and the designer are never stored. A plot that already has a solar,
  battery or wind permit fails a check and gets a badge.
- **Project types:** solar PV, battery, onshore wind and data centre, each with its own size,
  checks and rules on the card.
  - **Onshore wind:** each plot is a possible turbine position (1 ha). With "apply the wind act" on
    (Dz.U. 2024 poz. 317, the consolidated text in force; the 2025 bill for 500 m was vetoed) a plot
    is left out when its edge is under 700 m from a home (art. 4(1), the least a local plan may set;
    otherwise 10 × the total height); when more than a tenth of it is in a national park, reserve,
    landscape park or Natura 2000 (art. 4c(1)); when it is closer than 10 × the height to a
    national park or 500 m to a reserve (art. 4c(2)); or when it is closer to a 220/400 kV line
    than 3 rotor diameters or 2 × the height (art. 4a). The page works these out from the protected
    areas and grid lines it already holds. The card also covers the local plan (art. 3), the
    environmental decision (regulation of 10 Sep 2019 as amended in 2026: 30 m or more, and 100 MW
    or more for a full assessment) and aviation (Aviation Law, art. 87 and 87¹).
  - **Data centre:** size from IT load × PUE × ha per MW, plus 2 ha; checks for the group's demand
    room in 2030, a high-voltage line within 1 km, flat ground, no flooding even at 0.2%, and a
    general-plan zone for industry or services. The card covers planning, generators (an
    environmental decision may be needed from 25 MW thermal input) and the 2026 connection fees.
- **Checks:** up to 13 yes/no checks per plot for the chosen project: size, within 1 km of the
  substation, distance to the nearest home (30 m for solar, 50 m for a battery: common
  buffers, not legal minimums; 150 m for a battery until November 2026, when a benchmark of
  the plots that already have a battery permit found half of them closer), a public road within 300 m, no high-voltage line across the
  plot, no buildings on the plot, width, protected areas, the 1% flood area (any part of it for
  a battery, because of the ban on storing polluting substances), class I–III soil, and the
  general-plan zone, the slope, and no energy permit already on the plot. A check without data
  counts as neither.
- **Data gaps:** what a source doesn't cover for the plot (soil classes or owner groups the
  county doesn't publish, no plan in the urban register, a step not run yet).
- **Downloads:** the sites in the list as CSV (one row per plot) or GeoJSON (with outlines),
  including the check results.

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
