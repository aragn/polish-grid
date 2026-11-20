# Congestion screening: modelled line loading in 2025

A DC power-flow model of Poland's grid, run hour by hour through 2025 with
public data, in the spirit of EPEC's NEMScene for Australia. It answers two
questions:

1. **Which lines would have gone over their rating** in 2025, with every
   element in service (N-0) or after the loss of one circuit (N-1)? How
   often, after which outage, and at what time of day?
2. **What if** 100 MW of solar, wind or a battery connected at a given
   substation: which lines would it load, and how many more hours would they
   spend over 100%?

The Grid capacity page draws the results (`data/congestion/screening_2025.json`).

Australia can do this with measured data, because AEMO publishes its
constraint equations and SCADA line flows. European TSOs publish neither for
internal lines, so the flows here are **modelled**. The topology comes from
OpenStreetMap, ratings are built from public signals (none is published),
and the injections are PSE's metered data spread over the substations by
documented rules. This is a screen for where to look, not a connection study.

## Run

```
python etl/congestion/fetch_pse.py --work W --year 2025                     # PSE API, no key, ~1 h
python <pypsa-eur env> etl/congestion/weather.py --cutout europe-2025-sarah3-era5.nc --pop pop_layout_total.nc --work W
python etl/congestion/osm_pbf.py poland-260101-power.osm.pbf <pypsa-eur>/data/osm/build/unknown/PL
pixi run snakemake -c2 resources/plgrid/osm/build/lines.csv --configfile <path>/etl/congestion/plgrid.yaml   # in PyPSA-Eur
pdftotext -enc UTF-8 -layout PRSP_2025_2034.pdf prsp.txt && python etl/congestion/prsp.py prsp.txt W/cache/prsp_lines.json
python etl/congestion/network.py --work W --pl <pypsa-eur>/resources/plgrid/osm/build \
       --raw <pypsa-eur>/data/osm/build/unknown/PL --osm <pypsa-eur>/data/osm/archive/0.7 --prsp W/cache/prsp_lines.json
python etl/congestion/inputs.py --work W --ure Mapa_OZE_2025-06-30.xlsx --pop pop_layout_total.nc
python etl/congestion/flows.py --work W --outages outages_2025.json         # ~20 s
python etl/congestion/whatif.py --work W --outages outages_2025.json --batch  # every 220/400 kV substation, ~1.5 h
python etl/congestion/export.py --work W                                    # data/congestion/screening_2025.json
python etl/congestion/report.py --work W > report.md
python etl/congestion/whatif.py --work W --outages outages_2025.json --bus Mokre --tech solar --mw 100 300
```

Two inputs come from GitHub Actions. ENTSO-E's key is only in the repository
secrets, and pse.pl and Geofabrik are slow or unreliable from a home
connection:

- `.github/workflows/congestion-outages.yml` runs `fetch_outages.py`;
- `.github/workflows/congestion-sources.yml` fetches PSE's development plan
  and filters Geofabrik's dated Poland extract to its power features with
  osmium-tool.

| Input | Source | Licence / terms |
|---|---|---|
| Poland's grid from 110 kV up | OpenStreetMap, Geofabrik's snapshot of 1 January 2026 (`poland-260101.osm.pbf`), cleaned by PyPSA-Eur's `clean_osm_data` and `build_osm_network` | ODbL |
| Neighbours' 220/400 kV grids (DE, CZ, SK, LT) and the tie lines | PyPSA-Eur's prebuilt OSM network, release 0.7 | ODbL |
| Station names | `data/queue.json` (PSE's stations), then OSM's names | ODbL |
| Unit output, system balance, border flows | PSE reporting API (`gen-jw`, `his-wlk-cal`, `przeplywy-mocy`). PSE submits the same data to ENTSO-E as 16.1.A, 6.1.A and 12.1.G | PSE, public |
| Outages of transmission elements | ENTSO-E Transparency Platform, unavailability of transmission infrastructure (10.1.A/B) | ENTSO-E, reuse with attribution |
| Wind and PV installations | URE "Mapa OZE" register of licensed installations (30 June 2025), by town | URE, public |
| Lines PSE modernises | PSE, Plan rozwoju sieci przesyłowej 2025–2034 (agreed 20 December 2024) | PSE, public |
| Weather | ERA5 wind and 2 m temperature, SARAH-3 irradiance: PyPSA-Eur cutout `europe-2025-sarah3-era5` | Copernicus; Copyright (c) (2025) EUMETSAT |
| Population | PyPSA-Eur `pop_layout_total.nc` | |
| Day-ahead prices (battery profile) | `data/prices/PL/` (ENTSO-E) | |

Nothing here comes from JAO.

## Network

`network.py`.

**Poland.** Every bus, line and transformer from 110 kV up in the OSM
extract, as cleaned by PyPSA-Eur (`config/plgrid.yaml` in the PyPSA-Eur
workspace). Only buses on Polish territory are kept: the extract reaches over
the border (Dětmarovice, Hagenwerder, Lemešany). Nothing under construction
is kept. OSM's pass-through points are merged away. Every 220/400 kV line of
n circuits becomes n parallel branches, so a single-circuit outage is a
contingency of its own. About 2,300 Polish buses and 3,000 lines remain,
2,700 of them at 110 kV.

**Neighbours.** Germany's, the Czech Republic's, Slovakia's and Lithuania's
220/400 kV grids from PyPSA-Eur's prebuilt network, cut at their other
borders, joined to Poland at the real tie lines (Krajnik – Vierraden,
Mikułowa – Hagenwerder, Dobrzeń – Dětmarovice, Wielopole – Nošovice, the
Liskovec pair, Krosno – Lemešany, Ełk – Alytus). Ukraine stays one boundary
node, and SwePol is an injection at its converter's bus. Each hour the
neighbours' net injections, spread over their substations by population, are
solved so that every Polish border carries PSE's metered flow. They are
fitted to the borders, not the neighbours' real net positions: with
Germany's, the Czech Republic's and Slovakia's other borders cut, they absorb
the loop flows of the wider grid. The split between a border's lines, and
what happens behind it after an outage, is the network's.

**Phase shifters.** The German corridors have phase shifters: PSE's at
Mikułowa, 50Hertz's at Vierraden on the Krajnik lines. They take the
network's base-case flow, but after another element trips they keep it. Their
LODFs are computed with those branches out and their flows held.

**Line parameters.** No operator publishes line ratings, so each line gets a
standard conductor:

| Voltage, OSM `wires` | Conductor assumed | x (Ω/km per circuit) | Rating per circuit at 30 °C air, 80 °C conductor |
|---|---|---:|---:|
| 110 kV | AFL-6 240 | 0.40 | 115 MVA |
| 220 kV, single | AFL-8 525 | 0.41 | 380 MVA |
| 220 kV, double | 2 × AFL-8 525 | 0.31 | 760 MVA |
| 400 kV, double | 2 × AFL-8 525 | 0.33 | 1,390 MVA |
| 400 kV, triple | 3 × AFL-8 350 | 0.29 | 1,660 MVA |

Two public signals make the ratings line-specific:

- **PSE's development plan.** Lines PSE modernises or rebuilds in 2025–2034
  (`prsp.py`, 42 sections) are taken as built for a 60 °C conductor, the
  others for 80 °C. The plan gives the reason for most of these upgrades:
  raising the permitted current. At 30 °C a plan-listed line therefore has
  77% of the standard rating.
- **Air temperature.** Every hour, each line's rating is scaled with the ERA5
  2 m temperature at the line: I ∝ √((Tc − Ta) / (Tc − 30 °C)), within
  0.6–1.3. This is a static ampacity relation without wind cooling. A winter
  hour gives more, a heatwave less.

Transformers (400/220, 400/110 and 220/110 kV) take OSM's unit ratings where
tagged: 23 of about 200 stations. The others get two units at 500, 330 or
160 MVA. The page shows lines only, because transformer ratings rest mostly
on that default.

## Injections

`inputs.py`, hourly (UTC) for 2025, MW at each bus, positive into the grid:

| Component | Rule | 2025 TWh |
|---|---|---:|
| JWCD units | PSE `gen-jw` per unit, at its plant's substation. The digit before the separator in the resource code is the connection voltage: `BEL 2-02` is 220 kV, `KOZ24-09` 400 kV, `x 1-xx` 110 kV. Units connect to real substations, never to OSM tee points; Bełchatów is pinned to Rogowiec. Pumping is negative. | 94.7 |
| Wind | PSE's national wind output each hour, split by URE's licensed wind capacity at each town times the ERA5 capacity factor of its cell | 21.9 |
| Solar | PSE's national PV less the JWCD PV unit, split by URE's licensed PV plus 13 GW of micro-installations (not in URE's register, spread by population), times the SARAH-3 capacity factor | 19.1 |
| Other generation | the rest of PSE's balance (CHP, industrial units, hydro, biomass), spread by population | 23.3 |
| Load | PSE's system demand, spread by population | 158.4 |
| SwePol | PSE's metered flow at the converter's bus (Słupsk) | |
| Borders | PSE's metered flow per section (DE, CZ, SK, LT; Ukraine at its boundary node), met by the neighbours' injections | net import 0.96 |
| Closing | what remains (metering differences) goes onto the load | mean 100 MW |

A place's population or capacity feeds its **three nearest 110 kV
substations** within 25 km, weighted by 1/distance²: about 1,800 load buses.

## Screening

`core.py` / `flows.py`. Every hour:

1. **Topology.** Elements ENTSO-E lists as disconnected that hour are taken
   out of service: planned or forced outages with no capacity left.
   ENTSO-E's limits, where some capacity stays available, are not
   disconnections and are left in. In 2025 that is mainly the Krajnik –
   Vierraden circuits, out one at a time for most of the year, the Mikułowa –
   Hagenwerder circuits, Ełk – Alytus, Liskovec – Kopanina and Gdańsk I –
   Żydowo Kierzkowo. PSE publishes only outages that affect cross-border
   capacity, so most internal ones are missing. A neighbour's last tie is
   never taken out while PSE metered more than 50 MW across that border.
   Outage topologies are low-rank updates of the base PTDF.
2. **Neighbours.** Their injections are set to match the measured border flows.
3. **N-0 and N-1.** N-0 flows, and N-1 flows from LODFs, for each of the 437
   Polish 220/400 kV circuits, tie lines and 400/220 kV transformers whose loss
   does not island part of the grid. The 110 kV grid and the neighbours' lines
   carry flow but are neither monitored nor tripped.
4. **Loading.** Against the hour's temperature-dependent rating.

The full year takes about 20 seconds; `whatif.py --batch` takes about
an hour and a half for 123 substations × 3 technologies.

**Results for 2025** (`report.py`). 21 of 328 lines (circuits of one line
merged) go over 100% after an outage at least once, and a few with nothing
out, all on the Czech border:

- **The east.** Zamość – Ukraine 220 kV and Mokre – Zamość after the loss of
  the Rzeszów – Khmelnytskyi 400 kV tie (275 and 60 hours). Puławy –
  Abramowice 220 kV after Kozienice – Lublin 400 kV (46 hours, mostly
  evenings).
- **Central and south 220 kV.** Joachimów – Łośnice, Pątnów – Konin,
  Świebodzice – Ząbkowice and Krajnik – Morzyczyn, each after the parallel
  400 kV line or its own other circuit (7–29 hours, mostly evenings).
- **The Czech border.** The ties and Dobrzeń – Wielopole for a few hours in N-0.

Most of the lines flagged are in PSE's modernisation plan. That is partly
circular, since the plan is also what lowers their modelled rating.

Compared with a 220/400 kV-only model, the 110 kV grid carries part of the
flow in parallel and far fewer lines go over. That is the main sensitivity:
with 110 kV reactances ×2, ×4 and ×8 (`network.py --x110`, a stand-in for
open points the distribution operators run), 28, 38 and 42 lines go over
after an outage, against 21. The default is a closed 110 kV grid, because PSE coordinates
the 110 kV network as one that runs in parallel with 220/400 kV.

## What if

`whatif.py --bus <substation> --tech solar|wind|battery --mw ...`, or
`--batch` for every 220/400 kV substation at 100 MW. The new plant gets an
hourly profile:

- **Solar and wind:** the capacity factor of the substation's weather cell,
  scaled so that the 2025 fleet's modelled energy equals PSE's (wind ×0.88,
  solar ×0.78).
- **Battery:** daily arbitrage on Poland's day-ahead price, charging in the
  two cheapest and discharging in the two dearest hours.

Every MW it produces displaces the JWCD thermal units in proportion to their
output that hour, and charging is supplied the same way. Border flows stay as
measured. The year is screened again with the same outages, ratings and
contingencies, on the lines that carry at least 5% of the plant's output.
Two caveats:

- The displacement rule matters. In reality redispatch follows prices and
  location, and the market may export the extra output instead.
- A line already over 100% and a little more loaded also shows up.

## Caveats

- **Estimated ratings.** Conductor classes, plan-based design temperatures
  and air temperature, not the operator's values. Terminal equipment,
  seasonal ratings and dynamic line rating are not known.
- **Switching states.** The model has every busbar coupled and every line in
  service, except ENTSO-E's outages, most internal ones missing. PSE splits
  busbars, opens lines and uses redispatch, and the measured dispatch already
  includes the redispatch. A line over 100% here is a line under pressure,
  not one that was overloaded.
- **The 110 kV grid.** Its open points are unknown (see the sensitivity
  above). Its own lines are not monitored.
- **OSM topology.** The grid is the snapshot of 1 January 2026, so anything
  energised late in 2025 is in all year. Plant switchyards are where OSM is
  weakest.
- **Spatial rules.** Load, other generation and micro-PV follow population,
  with no industrial load map.
- **Neighbours.** Their injections are fitted to the measured border flows;
  Austria, Hungary and the rest of Europe are not modelled. Ukraine is one
  node, so an outage on the Ukrainian ties cannot reroute through Slovakia or
  Hungary.
- **A private comparison** with the TSOs' flow-based reference flows (not
  redistributable) is kept outside this repository.
