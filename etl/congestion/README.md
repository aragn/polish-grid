# Congestion screening, 220/400 kV (proof of concept)

A DC power-flow model of Poland's transmission grid, run hour by hour through
2025 with public data. It answers two questions, in the spirit of EPEC's
NEMScene for Australia:

1. **Which lines would have been overloaded** in 2025, before (N-0) or after
   (N-1) the loss of one circuit, how often, after which outage, and at what
   time of day?
2. **What if** X MW of solar, wind or battery storage connected at a given
   substation: which lines would it load, and by how much?

Australia can do this with measured data, because AEMO publishes its constraint
equations (with each generator's coefficient) and SCADA line flows. European
TSOs publish neither for internal lines, so here the flows are **modelled**:
the grid topology comes from OpenStreetMap, the impedances and ratings are
estimates, and the injections come from PSE's metered data spread over the
substations by documented rules. It is a screening tool, not a grid study.

Nothing here is published on the site yet. The scripts write to a working
folder outside the repository (`--work`).

## Run

```
python etl/congestion/fetch_pse.py --work W --year 2025          # PSE API, ~1 h, no key
python <pypsa-eur env> etl/congestion/weather.py --cutout europe-2025-sarah3-era5.nc \
       --pop pop_layout_total.nc --work W                       # atlite, ~3 min
python etl/congestion/network.py --osm <osm-prebuilt 0.7 dir> --work W
python etl/congestion/inputs.py --work W --ure Mapa_OZE_2025-06-30.xlsx --year 2025
python etl/congestion/flows.py --work W --year 2025              # ~10 s
python etl/congestion/report.py --work W --year 2025 > report.md
python etl/congestion/whatif.py --work W --bus Mokre --tech solar --mw 100 300
```

Inputs:

| Input | Source | Licence / terms |
|---|---|---|
| 220/400 kV lines, substations, transformers | PyPSA-Eur's prebuilt OpenStreetMap network, release 0.7 (Zenodo) | ODbL |
| Station names and 110 kV transformation | `data/queue.json` (PSE stations), `etl/osm_places.json` | ODbL |
| Unit output, system balance, border flows | PSE reporting API (`gen-jw`, `his-wlk-cal`, `przeplywy-mocy`), the data PSE submits to ENTSO-E as 16.1.A, 6.1.A and 12.1.G | PSE, public |
| Wind and PV installations | URE "Mapa OZE" register of licensed installations (30 June 2025), by town | URE, public |
| Weather | ERA5 wind and SARAH-3 irradiance, PyPSA-Eur cutout `europe-2025-sarah3-era5` | Copernicus; Copyright (c) (2025) EUMETSAT |
| Population | PyPSA-Eur `pop_layout_total.nc` (JRC GHS population on the cutout grid) | |
| Day-ahead prices (battery profile) | `data/prices/PL/` (ENTSO-E) | |

## Network

`network.py`. Every Polish bus at 220 kV and above, every line with an end in
Poland, and the 400/220 kV transformers: 228 buses, 330 lines, 40 transformers.
OSM's pass-through tee points are merged away, and every line of n circuits
becomes n parallel branches, so a single-circuit outage is its own
contingency (437 branches in all).

**Borders.** Every cross-border line ends at one boundary node per neighbour
(DE, CZ, SK, LT, UA), and SwePol becomes an injection at Słupsk. A neighbour
meets Poland only through its own tie lines. The measured border flow injected
at its node therefore reaches Poland exactly, split between the tie lines by
impedance. Loop flows through the neighbours (DE → CZ/SK → PL) are therefore
in the data, not in the model. The phase shifters at Mikułowa and Krajnik,
which set the split of German imports between the two, are not modelled.

**Line parameters.** OSM has no electrical data, and PyPSA-Eur's defaults
(Al/St 240/40 bundles at 220 and 380 kV) do not fit PSE's conductors. Each
voltage gets one standard type:

| Voltage | Conductor assumed | x (Ω/km per circuit) | Rating per circuit |
|---|---|---:|---:|
| 220 kV | AFL-8 525 mm², single | 0.41 | 380 MVA (~1.0 kA) |
| 400 kV | 2 × AFL-8 525 mm² | 0.33 | 1,390 MVA (~2.0 kA) |
| 400/220 kV transformer | — | 0.12 pu on its rating | OSM rating clipped to 500–1,000 MVA |

So every line of one voltage has the same rating and the same reactance per
km. Real lines differ: older lines with lower ratings, newer ones with
high-temperature conductors, terminal equipment limits, and seasonal ratings.
Ratings are continuous MVA, and the DC flow ignores reactive power and losses.

## Injections

`inputs.py`, hourly (UTC) for 2025, MW at each bus, positive into the grid:

| Component | Rule | 2025 TWh |
|---|---|---:|
| JWCD units | PSE `gen-jw` per unit, at its plant's substation. The digit before the separator in the resource code is the connection voltage: `BEL 2-02` is 220 kV, `KOZ24-09` 400 kV, `x 1-xx` 110 kV (nearest 110 kV-feeding substation). Pumping is negative. | 94.7 |
| Wind | PSE's national wind output each hour, split by URE's licensed wind capacity at each town times the ERA5 capacity factor of its weather cell (Vestas V112 curve) | 21.9 |
| Solar | PSE's national PV less the JWCD PV unit, split by URE's licensed PV plus 13 GW of micro-installations (not in URE's register; spread by population), times the SARAH-3 capacity factor | 19.1 |
| Other generation | the rest of PSE's balance (jg + jnwrb less the above: CHP, industrial units, hydro, biomass), spread by population | 23.3 |
| Load | PSE's system demand, spread by population | 158.4 |
| Borders | PSE's metered flow per border section, net per neighbour | net import 0.96 |
| Closing | what remains (metering differences, pumping reported separately) goes onto the load | mean 100 MW |

Population, capacity and load reach the 220/400 kV grid through the 110 kV
network, which is not modelled. A place feeds its **three nearest substations
that transform to 110 kV** within 40 km, weighted by 1/distance². With a single
nearest substation instead, whole cities land on one bus, and Warsaw's 220 kV
ring overloads even with every line in service.

## Screening

`core.py` / `flows.py`: PTDF from the DC model, N-0 flows every hour, and N-1
flows from line outage distribution factors (LODF) for each of the 418
single-circuit and transformer outages that do not island part of the grid.
For every branch and hour, the worst post-contingency loading and the outage
behind it. `report.py` turns `screening_2025.json` into tables.

**Results for 2025** (`report.py`): 78 lines exceed 100% after some
contingency at least once, and 6 exceed it with everything in service. The
most frequent groups:

- **Warsaw 220 kV ring** (Mory, Praga, Towarowa, Miłosna, Ołtarzew): heavy
  load on 220 kV with the city's 110 kV network and cables missing. It is
  overloaded even in N-0, which is a modelling artefact.
- **Upper Silesia 220 kV** (Byczyna, Bieruń, Bujaków, Komorowice, Jamki,
  Katowice): a dense 220 kV mesh with plants and load, where OSM's topology
  around plant switchyards is the weakest. The top line (Katowice tee – Byczyna
  tee) is a line between two OSM tee points and probably an artefact.
- **East (Lubelskie)**: Abramowice – Mokre, Puławy – Abramowice, Mokre – Chełm,
  Mokre – Zamość after the loss of Kozienice – Lublin 400 kV or of the
  Rzeszów – Khmelnytskyi tie: the long 220 kV chain in the east backs up the
  400 kV lines.
- **South-east 400 kV**: Krosno Iskrzynia – Rzeszów and Tarnów – Krosno, each
  after the loss of the other, with exports to Slovakia.
- **North (Pomorze)**: Dunowo – Żydowo 220 kV after Dunowo – Słupsk 400 kV,
  with wind and SwePol.

On most of these lines the evening peak (17:00–22:00, 5 hours) holds 30–50%
of the N-1 overload hours, and solar hours (10:00–16:00, 6 hours) 15–30%.
The exception is Bujaków – Byczyna (45% in solar hours).

## What if

`whatif.py --bus <substation> --tech solar|wind|battery --mw ...`. The new
plant gets an hourly profile:

- solar and wind: the capacity factor of the substation's weather cell, scaled
  by the factor that makes the 2025 fleet's modelled energy equal PSE's (wind
  0.88, solar 0.78; atlite overstates both). Even so, a coastal wind site gets
  about 3,200 full-load hours, against about 2,000 for the 2025 fleet;
- battery: daily arbitrage on the Polish day-ahead price, charging in the
  cheapest `--hours` hours and discharging in the dearest.

Every MW it produces displaces the JWCD thermal units in proportion to their
output that hour, and charging is supplied the same way. The flow change is
PTDF(bus) − PTDF(sink) times the MW. The year is screened again, and the tool
lists the branches that carry at least `--min-share` (5%) of the plant's output
in some hour, with N-1 hours above 100% before and after and the contingency.
Examples:

- 300 MW of solar at Mokre 220 kV (850 full-load hours): Mokre – Chełm's
  worst N-1 loading goes from 1.43 to 1.64 (after Kozienice – Lublin), and the
  Chełm 400/220 kV transformer's from 0.52 to 0.60.
- 200 MW of wind at Słupsk Wierzbięcino 400 kV: Piła Krzewina – Żydowo 220 kV
  goes from 11 to 42 hours above 100% after Morzyczyn – Dunowo 400 kV.

The tool also lists lines that are already overloaded and only a little more
loaded. Those lines are flagged by the base screen, not by the new plant.

The displacement rule matters: in reality redispatch follows prices and
location, and the market may export the extra output instead.

## Caveats

- **Estimated impedances and ratings.** One conductor type per voltage, so
  lines with very different real ratings look alike.
- **No 110 kV grid.** In cities and Silesia the 110 kV network runs in
  parallel with 220 kV and carries part of the flow. Without it, flows
  concentrate on 220 kV, so 220 kV loadings there are overstated.
- **No switching states.** The model has every line in service and every
  busbar coupled. PSE splits busbars, opens lines and schedules outages
  (ENTSO-E 10.1.A, not used here), and it uses phase shifters and redispatch.
  Measured 2025 dispatch already includes redispatch, so modelled overloads
  are pressure on the grid, not congestion PSE failed to solve.
- **OSM topology.** Tee points and plant switchyards are where OSM is weakest
  (Jaworzno III's switchyard keeps only one 220 kV exit, for example). Plants
  are connected to real substations, never to tee points, with Bełchatów
  pinned to Rogowiec.
- **Spatial rules.** Load, non-JWCD generation and micro-PV follow population
  (no industrial load map). Wind and PV follow URE's register by town, which
  leaves out micro-installations.
- **Borders are inputs.** The border totals are measured, so they say nothing
  about the model. Only the split between parallel ties is modelled.
- **Reference flows.** A private comparison with the TSOs' published flow-based
  reference flows (not redistributable) is kept outside this repository.

## Good enough for the Grid capacity tab?

Not yet as a public layer of line congestion. The hot spots it ranks highest
(Warsaw, Upper Silesia 220 kV) are partly artefacts of the missing 110 kV
grid and of OSM's tee points. Its standard ratings make lines of one voltage
look alike. With no grid beyond the border, an outage on one border cannot
push flow onto another. A map of "hours above 100%" per line would look more
precise than it is.

Next steps, roughly in order of value:

1. **110 kV grid** from the same Geofabrik extract `osm_pl.py` reads
   (OSM maps it well in Poland), so city and Silesian load spreads as it
   really does.
2. **Neighbours' grids** (PyPSA-Eur's OSM network for DE, CZ, SK, with each
   country's measured net position), so loop flows and outages abroad are
   physical, plus the Mikułowa and Krajnik phase shifters as controllable
   branches.
3. **Line-specific ratings** from public sources (PSE's development plan and
   investment descriptions, TYNDP project sheets, conductor tags in OSM),
   instead of one type per voltage.
4. **Planned outages** (ENTSO-E 10.1.A) for switching states.

Once 1–3 are in, a cautious public layer is possible: per substation, the
share of a new MW that lands on the 400 kV corridors the screen flags. That is
a sensitivity map, and it is less sensitive to ratings than hours above 100%.
The what-if script can stay a local tool.
