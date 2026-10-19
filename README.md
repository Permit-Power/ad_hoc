# Ad Hoc Work Documentation

This repo contains a set of small, mostly notebook-driven analyses (“ad hoc projects”). Each project below has a short **purpose**, **methodology** (with an emphasis on data sources + calculations), and the **expected outputs**.

> Note: Most notebooks assume a repo structure where inputs live under `../data/` and outputs are written to `../outputs/` or `../data/<project_output_dir>/`.

---

## 1) Avoided Gas Capacity Equivalent (baseline vs policy net load)

**Primary artifacts**
- Notebook: `avoided_gas_capacity.ipynb`
- Module: `avoided_gas_capacity.py` 

### Purpose
Estimate how much **natural gas generation** (MWh) and **equivalent gas plant capacity** (MW/GW) could be displaced if cheaper rooftop PV spurred additional solar installations (as modeled by the As Cheap As Our Peers report).

### Methodology

#### Data sources
1. **State-level hourly net load outputs** (two scenarios)
   - Input files are expected as per-state CSVs following a template like:
     ```
     .../per_state_outputs/{state_abbr}/run_all_states_net_savings_adjust_loan_params/{scenario}_state_hourly.csv
     ```
   - Each file contains a row per `(state_abbr, year, scenario, schema)` with an embedded 8760-hour series stored as a string in `net_sum_text` (MW values serialized like `{v1,v2,...,v8760}`).

2. **State-level natural gas generation shares** (optional, if using EIA-based shares)
   - A CSV like `natural_gas_generation_share_state.csv` with:
     - `year`, `state_abbr`, `energy_source` in `{"Total","Natural Gas"}`, `generation_mwh`.

#### Key calculations
1. **Parse the hourly series**  
   Parse `net_sum_text` into a numeric array of hourly MW for both baseline and policy scenarios.

2. **Compute annual avoided net load**
   - Hourly delta:
     - `delta_mw[h] = baseline_mw[h] - policy_mw[h]`
   - Optionally clip negative deltas:
     - `delta_mw[h] = max(delta_mw[h], 0)`
   - Annual total (since each timestep is 1 hour):
     - `delta_mwh_total = sum_h(delta_mw[h])`

3. **Attribute avoided load to natural gas**
- The delta in load is probably served by a mix of generating resources, so we need to make an assumption on how much is served by natural gas-fired generation. There are two modes:
   1) Use EIA data, where the gas share is the state-level proportion of total electricity generation that is produced from gas-fired power plants:
      - `gas_share_mode="state_eia"`:  
     `gas_share_state = gas_mwh_state / total_mwh_state`
   2) Assume a fixed fraction of natural gas fired generation:
      - `gas_share_mode="fixed_fraction"`:  
     `gas_share_state = fixed_gas_share` (e.g., 0.75)

   Then:
   - `gas_mwh_displaced = delta_mwh_total * gas_share_state`

4. **Convert annual displaced gas MWh → capacity equivalent**
   With capacity factor `CF`:
   - `gas_capacity_equiv_mw = gas_mwh_displaced / (CF * 8760)`
   - `gas_capacity_equiv_gw = gas_capacity_equiv_mw / 1000`

5. **Aggregation + visualization**
   - Aggregate state results to national totals.
   - Optional plots (utilities included in the module):
     - Weekly national energy totals (baseline vs policy)
     - Representative-week hourly profiles
     - Smoothed daily national load (moving average)

### Outputs
- **State-level results** (DataFrame):
  - `delta_mwh_total`, `gas_share`, `gas_mwh_displaced`, `gas_capacity_equiv_mw`, `gas_capacity_equiv_gw`
- **National summary** (Series / dict-like)
- **Plots** (matplotlib figures) showing baseline vs policy load at weekly/daily/hourly resolutions

---

## 2) NJ City Median Hourly Load Profiles (ResStock)

**Primary artifacts**
- Notebook: `nj_hourly_profiles.ipynb`
- Module: `build_hourly_load.py`

### Purpose
Create **city-level median hourly electricity load profiles** for **New Jersey** by aggregating ResStock building-level time series (Upgrade 0 baseline) into one **CSV per city**. This was produced to enable a PySAM-based analysis by Bill Brooks on how small amounts of residential solar exports will affectthe grid.

### Methodology

#### Data sources
1. **ResStock metadata** (local)
   - The notebook expects a local Parquet like `../data/baseline.parquet` containing at least:
     - `bldg_id` (building identifier),
     - `in.state` and `in.city`.

2. **ResStock building-level timeseries** (remote, OEDI data lake)
   - S3 bucket: `oedi-data-lake`
   - Dataset: `end-use-load-profiles-for-us-building-stock/2024/resstock_tmy3_release_2`
   - Per-building files (Parquet), organized by upgrade and state:
     ```
     s3://oedi-data-lake/.../timeseries_individual_buildings/by_state/upgrade={u}/state={state}/{bldg_id}-{u}.parquet
     ```
   - Uses `out.electricity.total.energy_consumption` and `timestamp`.

#### Key calculations
1. **Select building IDs**
   - Filter metadata to `in.state == "NJ"` and take unique `bldg_id` values.

2. **Hourly aggregation (per building)**
   - Read per-building Parquet files directly from S3 using DuckDB + `httpfs`.
   - Truncate timestamps to the hour and sum electricity consumption to hourly kWh.

3. **Join to city + compute city median**
   - Join hourly building profiles to metadata on `bldg_id` to assign `city`.
   - Compute, for each `(city, hour)`, the **median** hourly kWh across buildings.

4. **Write outputs**
   - Write one CSV per city to an output directory such as `../data/nj_hourly_profiles_by_city/`.

### Outputs
- **Per-city CSV files**: `../data/nj_hourly_profiles_by_city/<City>.csv`
  - Expected columns:
    - `city` (string)
    - `ts_hour` (timestamp, hourly)
    - `median_kwh` (float)
- **Optional QA plot**
  - The notebook demonstrates resampling a city file to daily totals and plotting `kWh/day`.

---

## 3) CO City Median Hourly Load Profiles (ResStock)

**Primary artifacts**
- Notebook: `co_hourly_profiles.ipynb`
- Module: `build_hourly_load.py` fileciteturn0file0

### Purpose
Same workflow as the NJ project, but producing city-level median hourly electricity profiles for **Colorado**.

### Methodology
Identical to the NJ workflow with these differences:
- Filter metadata to `in.state == "CO"`.
- Set `state="CO"` when building S3 paths.
- Write outputs to `../data/co_hourly_profiles_by_city/`.

### Outputs
- **Per-city CSVs** in `../data/co_hourly_profiles_by_city/` with `city`, `ts_hour`, `median_kwh`.
- **Optional QA plot** (example shown for `CO_Denver.csv`).

---

## 4) Heat Pump Operating Costs With and Without Solar (ResStock + PySAM + EIA)

**Primary artifacts**
- Notebook: `solar_bill_analysis.ipynb`
- Modules: `solar_bill_analysis.py`, `eia_rates.py`

### Purpose
For U.S. single-family, owner-occupied homes that heat with gas, answer three questions:
1. **Is a heat pump cheaper to run than the gas furnace it replaces?** On grid power, and with rooftop solar at $3.62/W (today, LBNL), $2/W and $1/W (peer-country cost).
2. **Do overall energy bills go down** with a heat pump + rooftop solar package, compared with today?
3. **How much would small plug-in solar systems** (391W and 1,200W) lower a heat pump home's bills?

Questions 1 and 2 are also run with a home battery and under two export policies (current policy, and net metering phased out).

The heat pump is ResStock Upgrade 2 (high-efficiency cold-climate heat pump with electric backup, no envelope measures). Heat pump equipment costs are not included; results compare the cost to run each option.

### Key results (5,000-home run, October 2026)

**1. Heat pump vs. gas furnace** (median gas heating bill $970/yr; share of homes where the heat pump is cheaper to run, median savings)

| Scenario | Current policy | Net metering phased out |
|---|---|---|
| Heat pump on grid power | 49%, −$17/yr | 49%, −$17/yr |
| + solar $3.62/W | 47%, −$56/yr | 31%, −$491/yr |
| + solar $2/W | 69%, +$272/yr | 44%, −$100/yr |
| + solar $1/W | 82%, +$420/yr | 60%, +$123/yr |
| + solar $1/W + battery $400/kWh | 83%, +$418/yr | 64%, +$143/yr |

**2. Overall energy bills, heat pump + solar vs. today** (share of homes that save, median savings)

| Scenario | Current policy | Net metering phased out |
|---|---|---|
| Heat pump on grid power | 49%, −$17/yr | 49%, −$17/yr |
| + solar $3.62/W | 31%, −$376/yr | 6%, −$1,301/yr |
| + solar $2/W | 77%, +$627/yr | 42%, −$147/yr |
| + solar $1/W | 96%, +$1,282/yr | 82%, +$534/yr |

Battery scenarios pair $1,200, $800 and $400/kWh with $3.62, $2 and $1/W solar; all rows are in the notebook and output CSVs.

**3. Plug-in solar on a heat pump home** (national medians)

| | 391W | 1,200W |
|---|---|---|
| Production | ~500 kWh/yr | ~1,535 kWh/yr |
| Share used (no export credit) | 100% | 94% |
| Bill savings | ~$82/yr | ~$239/yr |
| Share of heat pump electricity cost (heating + cooling) | ~6% | ~16% |

### Methodology

#### Data sources
1. **ResStock 2024 Release 2 (TMY3) metadata and annual results** (remote, OEDI data lake)
   - `load_resstock_metadata(...)` reads `metadata/upgradeNN.parquet` directly from S3 with filters pushed down to DuckDB.
   - Used for building selection, location, roof area, orientation, weather file coordinates and annual end-use energy.

2. **ResStock hourly end-use profiles** (remote, OEDI data lake)
   - Upgrades `("0", HP_UPGRADE)`; hourly series `out.electricity.total.energy_consumption` → `elec_kwh` and `out.natural_gas.total.energy_consumption` → `gas_kwh`.

3. **ResStock HPXML building energy models** (remote) — roof planes for PV tilt and azimuth.

4. **ResStock TMY3 weather CSVs** (remote) — PVWatts solar resource data.

5. **EIA rates** (remote, via `eia_rates.py`)
   - Electricity: EIA-861M monthly residential revenue, sales and customers by state.
   - Gas: monthly residential prices and volumes, and annual residential consumer counts, by state.

6. **DSIRE net metering map** (local) — `../data/dsire_iou_net_metering_may2026.csv`, residential IOU export compensation policy by state. DC is not in the file and is treated as net billing.

7. **Hourly wholesale export rates** (local, from dGen) — `../data/dgen_export_rates_hourly_by_county.csv.gz`, 8,760 hourly $/kWh values per county, loaded with `load_wholesale_export_rates(...)`. Used for the "net metering phased out" policy. About 31% of counties (non-RTO regions) only have monthly values, which overstate the value of midday exports.

#### Key calculations

1. **Sample buildings**
   - Natural gas primary heating fuel, heat pump upgrade applicable, owner-occupied single-family detached (137,144 eligible).
   - Simple random sample of 5,000 buildings (seed 42). ResStock weights are uniform, so the sample is nationally representative.

2. **Simulate rooftop PV** — `compute_pv_for_buildings(...)` runs `compute_pv_for_building(...)` in parallel:
   - Fixed roof-mount array (`array_type=1`; PySAM's `PVWattsSingleOwner` default is 1-axis tracking, so this must be set).
   - Tilt and azimuth from the largest south-ish HPXML roof plane, else the plane facing closest to south. Homes with no pitched roof planes (flat roofs) use the front or back of the house, whichever faces closer to south, at a default tilt.
   - `simulate_pv_per_kw(...)` runs PVWatts for a 1 kW system; output scales linearly with size.
   - `sizing_mode="min"` (`size_pv_system_kw(...)`): the smaller of roof-based size (`roof_area_ft2 * 15 W/ft2`) and load-based size (100% of annual load, using the home's simulated output per kW), capped at 25 kW (`max_system_kw`).
   - Homes size to their own load: with a heat pump, against the heat pump scenario's load (median 12.1 kW); without one, a smaller system against the baseline load (median 7.5 kW), scaling the same simulated profile.

3. **Hourly netting** — `compute_annual_flows(...)` returns each home's annual electricity and gas use with and without the heat pump, and nets PV against hourly load for four setups (with/without a heat pump, each with/without a battery), returning annual grid imports, exports, and exports valued at hourly wholesale prices. `simulate_battery(...)` dispatches a 13.5 kWh / 5 kW battery (90% round-trip) to store surplus solar for later use in the home; it never charges from the grid. The notebook checks that, without a battery, imports − exports + solar output equals load.

4. **Rates** — `build_state_residential_rates(...)` returns state marginal rates over the latest 12 months: average price with a fixed monthly customer charge removed (`(revenue - fixed * customer_months) / sales`; defaults $10/month electric, $11.25/month gas, matching ResStock). Fixed charges are paid in every scenario, so only marginal rates affect the comparisons.

5. **Export credit by state** (DSIRE), under current policy: retail rate (or the stated % of retail, e.g. Nevada 75%) in net metering states, $0.05/kWh in net billing states and those with no policy (Tennessee, DC), and the average of the two where policy is mixed (Texas). Illinois (10.3¢, Price to Compare) and Michigan (7.5¢, DTE power supply less estimated transmission) credit exports at the utility's supply rate and are set via `STATE_EXPORT_RATE_OVERRIDES`. With net metering phased out, exports in every state are credited at the county's hourly wholesale price (set `WHOLESALE_IN_NET_BILLING_STATES = False` to switch only net metering and mixed-policy states).

6. **Solar and battery cost** — `financed_annual_cost(...)` spreads a down payment and loan into an equal yearly amount in today's dollars over the equipment's life. Solar (`solar_annual_cost_per_kw(...)` × system size): 30% down, the rest financed at 7% over 20 years, 25-year life, 5% nominal discount rate, 2.5% inflation, no O&M, no incentives. Batteries: same terms over a 15-year life and loan. `solar_lcoe(...)` reports the equivalent cost per kWh for reference.

7. **Scenarios** — solar at $3.62, $2 and $1/W, each with and without a battery ($1,200, $800 and $400/kWh), under both export policies.

8. **Result 1: heat pump running cost** — the gas heating bill the heat pump replaces vs. the heat pump's electricity cost (net of the AC it replaces). On grid power, its extra kWh at the marginal rate; with solar, the difference in electricity costs (grid, exports, panels and any battery) between the home with the heat pump and larger system and the home without one and the smaller system. Solar's savings on other loads cancel, so this isolates the heat pump. Covers heating and cooling.

9. **Result 2: overall bills** — total annual energy costs (gas + electricity + solar and battery payments) with the heat pump + solar package vs. today's gas-heated home with no solar. Includes solar's savings on all of the home's electricity.

10. **Result 3: plug-in solar** — `compute_plugin_solar(...)` simulates 391W and 1,200W systems facing south at a 20° tilt (PVWatts defaults, fixed open rack) on each home with the heat pump. With no export compensation, each hour the home uses what it can and any surplus is lost. Savings are self-consumed kWh at the state's marginal rate, also shown as a share of the heat pump's heating and cooling electricity cost (from ResStock annual end uses). Plug-in system costs are not included.

Batches of 500 buildings are cached to `../data/solar_bill_national_sample_u{HP_UPGRADE}_{SAMPLE_SIZE}_seed{RANDOM_SEED}/` as they finish, so re-runs skip the S3 downloads and PVWatts runs (a full run takes about an hour; from the cache, about two minutes).

#### Known limitations
- Flat volumetric rates: no time-of-use or tiered pricing, which understates battery value where evening prices are high (e.g. California).
- Typical-year (TMY3) weather and hourly netting; real net billing meters exports instantaneously, so self-consumption is slightly overstated.
- Utility rates and export policies are applied at the state level; co-op and municipal customers are treated like the state's IOU customers.
- The Michigan transmission deduction is an estimate, and Consumers Energy's rates were not checked.
- No equipment costs for the heat pump, no incentives, and no value for backup power.

### Outputs
- **Per-building annual results** (`annual` in the notebook, cached as CSV batches): annual electricity and gas use, system sizes, solar output per kW, annual imports/exports for each setup, and plug-in production and self-consumption.
- `../outputs/heat_pump_running_cost_vs_gas.csv` — result 1, by scenario and export policy.
- `../outputs/heat_pump_solar_overall_bills_vs_today.csv` — result 2, by scenario and export policy.
- `../outputs/plugin_solar_with_heat_pump_by_state.csv` — result 3, nationally and by state.

---

## 5) Solar Installation Cost Breakdown (Illinois)

**Primary artifacts**
- Notebook: `il_cost_breakdown.ipynb`

### Purpose
Create a **visual cost breakdown** of solar installation costs in Illinois, comparing:
- “Today” (baseline)
- “Australia” (policy)
  - The Australia data is based on an analysis done by Andrew Birch that was shared with Permit Power.

### Methodology

#### Data sources
- Local CSV: `../data/il_cost_breakdown.csv`
  - Expected to include (at minimum):
    - `scenario` (e.g., `baseline`, `policy`)
    - `type` (e.g., `soft`, `hard`)
    - `value` (cost contribution, in $/W)
    - plus a component/category label used for bar segment labeling

#### Key calculations
- For each scenario:
  - Sort components so that **soft costs** stack below **hard costs** (using `type_order`).
  - Plot a stacked bar where each segment height equals its `$ / W` contribution.
  - Compute totals and shares:
    - `soft_total = sum(value where type == "soft")`
    - `hard_total = sum(value where type == "hard")`
    - `soft_pct = soft_total / (soft_total + hard_total)`
    - `hard_pct = 1 - soft_pct`
  - Label larger segments inline (a threshold is used to suppress labels on very small segments).

### Outputs
- A Matplotlib figure titled **“Solar Installation Cost Breakdown, Illinois”** comparing total costs and the split of soft vs hard costs under both scenarios.

---

## 7) Illinois Panel Upgrade Constraint Analysis (ResStock)

**Primary artifacts**
- Notebook: `illinois_panel_analysis.ipynb`

### Purpose
Quantify how many Illinois residential homes face **electric panel constraints** when adding a heat pump or EV charger, broken down by panel size. Uses ResStock 2024 metadata and NEC 2023 existing dwelling load calculations.

### Methodology

#### Data sources
- **ResStock metadata CSVs** (local):
  - `../data/IL_upgrade0.csv` — baseline, used for panel size and building type
  - `../data/IL_upgrade4.csv` — heat pump upgrade scenario
  - `../data/IL_upgrade23.csv` — EV charger upgrade scenario

#### Key columns
- `in.electric_panel_service_rating_bin..a` — panel size bucket (<100, 100, 125, 200, etc.)
- `out.params.panel_constraint_capacity.2023_nec_existing_dwelling_load_based` — boolean, True if panel is over capacity after upgrade
- `out.params.panel_constraint_breaker_space` — boolean, True if no breaker slots available
- `out.params.panel_constraint_overall.2023_nec_existing_dwelling_load_based` — categorical: "No Constraint", "Capacity Constrained Only", "Space Constrained Only", "Capacity and Space Constrained"

All results are weighted by `weight` to represent actual housing units.

#### Key calculations
- Panel size distribution (all homes and single-family only)
- % of homes with ≤100A panels
- Crosstab: panel size × capacity/space constraint after heat pump upgrade
- Overall: share of single-family homes constrained for HP or EV upgrade

### Outputs
- Summary tables and crosstabs displayed inline in the notebook

---

## 8) Illinois HVAC Permit Analysis (Shovels API)

**Primary artifacts**
- Notebook: `illinois_hvac_permit_analysis.ipynb`

### Purpose
Estimate the volume of **heat pump** and **electric panel upgrade** permits pulled in Illinois using the Shovels permit API, to understand real-world electrification activity.

### Methodology

#### Data sources
- **Shovels API** (`https://api.shovels.ai/v2/permits/search`) — residential permits for Illinois, queried by keyword and permit tags with cursor-based pagination
- Date range: 2014–2021 (heat pump permits); 2020–2021 for initial panel keyword volume gauging

#### Key calculations
- Fetch heat pump permits using multiple keyword variants (`heat pump`, `mini split`, `mini-split`, `minisplit`, `geothermal`, `ground source`) and deduplicate on permit ID
- Fetch panel upgrade permits using keyword variants (`electric panel`, `panel upgrade`, `service upgrade`, etc.)
- Aggregate permit counts by year and keyword to identify trends

### Outputs
- Permit count summaries by keyword and year, displayed inline in the notebook

---

## 6) Rockefeller / EIG Index × Solar Jobs Distribution (State-level rollups)

**Primary artifacts**
- Notebook: `rockefeller_jobs.ipynb`

### Purpose
Estimate how changes in distributed solar adoption under a “$ / watt” policy scenario translate into **job changes**, and how those job changes distribute across counties by **economic description (EIG/Rockefeller quintile)** and then aggregate to **state-level totals**.

### Methodology

#### Data sources
- `../data/irec_solar_jobs.csv`
  - Includes a 2023 baseline job measure for installation/project development and a `projected_growth_2024` used to project 2024 jobs.
- `../data/dollar_per_watt_agent_results.csv`
  - DGen-style county results including `state_abbr`, `county_id`, `year`, `scenario`, and fields used to estimate household counts and adoption.
- `../data/dgen_county_fips_mapping.csv`
  - Maps `county_id` → `geoid10` (county FIPS-like ID).
- `../data/rockefeller_eig_index.csv`
  - Contains county `geoid10` and an economic `quintile` label (later renamed to: `prosperous`, `comfortable`, `mid_tier`, `at_risk`, `distressed`).
- `../data/states.shp`
  - State boundaries (loaded via GeoPandas; likely used for mapping/QA plots).

#### Key calculations
1. **Project jobs to 2024**
   - `installation_proj_dev_jobs_2024 = installation_proj_dev_jobs_2023 * (1 + projected_growth_2024)`

2. **Allocate state jobs down to counties**
   - Merge DGen results with `geoid10`.
   - Estimate households represented by each record:
     - `hh = customers_in_bin / pct_of_bldgs_developable`
   - Compute each county’s share of state households (`county_prop`) and aggregate by `geoid10`.
   - Use `county_prop` to allocate state-level jobs to counties (so each county receives a proportion of state jobs).

3. **Translate adoption changes → job changes (2040)**
   - Extract 2040 cumulative adoption by county for baseline and policy scenarios.
   - Compute an adoption ratio (policy vs baseline); the notebook labels this as `pct_diff`.
   - Scale baseline county jobs by this ratio to estimate policy jobs:
     - `policy_jobs = county_jobs_baseline * pct_diff`
   - Compute the difference:
     - `job_increase = policy_jobs - county_jobs_baseline`

4. **Stratify by economic quintile and aggregate**
   - Join county quintiles from the Rockefeller/EIG dataset.
   - Group and sum job changes by `state_abbr` and `quintile`.
   - Pivot to a wide state table with one column per quintile category.

### Outputs
- `../outputs/rockefeller_jobs_by_state.csv`
  - Columns:
    - `state_abbr`, `prosperous`, `comfortable`, `mid_tier`, `at_risk`, `distressed`
  - Values are aggregated job changes (or job increases) by quintile within each state.
- Printed QA checks (e.g., column-wise sums).

---

## 9) Texas Panel Size Analysis (ResStock)

**Primary artifacts**
- Notebook: `texas_panel_analysis.ipynb`

### Purpose
Replicate the Illinois panel size analysis for **Texas**, characterizing the distribution of residential electric panel sizes using ResStock 2024 baseline metadata. Focused on single-family homes and the share with panels ≤125A.

### Methodology

#### Data sources
- **ResStock metadata CSV** (local): `../data/TX_upgrade0.csv` — baseline (no upgrades applied)

#### Key columns
- `in.electric_panel_service_rating_bin..a` — panel size bucket (<100, 100, 125, 200, etc.)
- `in.geometry_building_type_acs` — used to filter to single-family attached/detached
- `weight` — housing unit weight for statewide estimates

#### Key calculations
- Panel size distribution (all homes and single-family only), weighted by `weight`
- Share of homes with panels ≤125A

### Outputs
- Summary tables displayed inline in the notebook

---

## 10) California Inspection Timelines (SF Permits + Shovels)

**Primary artifacts**
- Notebook: `california_inspection_timelines.ipynb`

### Purpose
Analyze residential building inspection timelines in San Francisco to understand how long inspections take and whether remote inspections could speed up the process. Originally scoped to multiple California cities; SF was the only city with sufficient structured data for a permit-level timeline analysis.

### Methodology

#### Data sources
- **SF Building Inspections** (local): `../data/Building_Inspections_20260519.csv` — ~686K rows, one per inspection event
- **SF Building Permits** (local): `../data/Building_Permits_20260519.csv` — ~1.3M rows, one per permit
- **SD County Building Inspections** (local): `../data/SanDiegoCounty_Building_Inspections_20260520.csv` — explored but lacks a "request received" timestamp for timeline comparison

#### Key calculations
1. **Join**: filter inspections to `reference_number_type == 'permit'`, join to permits on `reference_number` = `Permit Number` (100% match rate)
2. **Collapse to permit level**: `first_request_taken_date`, `last_appointment_date`, `span_days`, `inspection_count`, `any_failed`, `has_sequential_stages`, `has_duplicate_descriptions`, `has_cancellation_or_siteverif`
3. **Filter**: residential occupancy only (exact R-2 or R-3), completed permits, 2016–2026
4. **Work type classification**: regex-based flags for AB 1738 categories (Tier 1: homeowner discretion; Tier 2: inspector discretion) applied to permit `Description` field

#### Key flags
- `span_days` — days from first inspection request to last inspection appointment
- `any_failed` — whether any inspection in the permit's history failed
- `is_tier1` / `is_tier2` — whether the permit falls under AB 1738 remote inspection categories
- `is_revision` — whether the permit description contains "revision to"

### Outputs
- `../outputs/building_inspections_permits_full.csv` — full filtered dataset with all flags, ready for Excel pivot analysis

---

## 11) Florida Shovels Exploration (Shovels API)

**Primary artifacts**
- Notebook: `florida_shovels_exploration.ipynb`

### Purpose
Exploratory pull of Florida residential permits from the Shovels API (2026 YTD) to understand what inspection timeline and person/identity fields are available in the data.

### Methodology

#### Data sources
- **Shovels API** (`https://api.shovels.ai/v2/permits/search`) — residential permits for Florida, Jan–Apr 2026, sample of 100 records

#### Key calculations
- Fetch a sample and print all available fields
- Auto-detect inspection/timeline columns and person/ID columns, with sample values
- Print one full raw record for detailed field inspection

### Outputs
- Field inventory displayed inline — no file outputs (exploratory only)

---

---

## 12) Plug-in Solar at 10% Household Adoption (ResStock + PySAM + EIA)

**Primary artifacts**
- Notebook: `plugin_solar_penetration.ipynb`
- Modules: `solar_bill_analysis.py` (`compute_plugin_solar`), `eia_rates.py`

### Purpose
Estimate bill savings from 391W and 1,200W plug-in solar systems across all U.S. households: per adopting household, and national and state totals if 10% of households adopt.

### Key results (5,000-home run, October 2026)

| | 391W | 1,200W |
|---|---|---|
| Bill savings per adopting household (mean) | $86/yr | $241/yr |
| Share of output used (no export credit) | 100% | 92% |
| Adopting households (10% of 121.9M) | 12.2M | 12.2M |
| National annual savings | $1.06B (95% interval $1.055–1.069B) | $2.96B ($2.94–2.98B) |
| National 10-year savings | $10.4B | $29.0B |
| Annual electricity offset | 5.8 TWh | 16.3 TWh |

Apartments with a vertical railing array save about 35–40% less than houses with a south-facing yard array ($59 vs. $93/yr at 391W; $160 vs. $260/yr at 1,200W). State totals are in the notebook and `../outputs/plugin_solar_10pct_adoption_by_state.csv`.

### Methodology

#### Data sources
- **ResStock 2024 Release 2 (TMY3) baseline metadata and hourly electricity use** (remote, OEDI data lake), occupied units only. Uniform sample weights give household counts (121.9M occupied households in the lower 48 states and DC).
- **ResStock TMY3 weather files** for PVWatts.
- **EIA state marginal residential electricity rates** via `eia_rates.py` (latest 12 months, $10/month fixed charge removed).

#### Key calculations
1. **Sample:** 5,000 occupied housing units drawn at random (seed 42), all building types, tenures and heating fuels.
2. **Mounting:**
   - Single-family detached and attached homes and mobile homes: south-facing, 20° tilt (yard, patio or roof edge).
   - Apartments (2–4 and 5+ units): vertical (90°) railing array on the back of the unit, at `in.orientation` + 180°. ResStock's `in.orientation` is the direction the unit's front faces the street, drawn from a national distribution (OpenStreetMap via Radiant Labs) with no dependence on location or building type; ResStock models apartments with an interior corridor at the front, so the exterior wall is at the back.
   - PVWatts fixed open rack for all.
3. **Self-consumption:** `compute_plugin_solar(...)` nets hourly output against the home's hourly electricity use; surplus is lost (no export compensation). Savings are used kWh at the state's marginal rate.
4. **Adoption:** a random 10% of each state's households, drawn from homes that would adopt. ASSUMPTION: apartments whose balcony faces north, northeast or northwest (about a third of apartments) don't adopt.
5. **Totals:** each state's adopting households × the mean savings of its sampled adopters; states are summed to the national total. The 95% interval comes from resampling homes within each state (1,000 draws).
6. **10-year savings:** annual savings × 9.778, the sum of 0.5%/yr panel degradation over 10 years, holding the share of output used and today's electricity rates constant.

#### Known limitations
- No shading from neighboring buildings, trees or balconies above, so apartment output is likely overstated.
- `in.orientation` is a statistical assignment, not each building's actual direction.
- No system costs, incentives, or changes in electricity rates.
- Alaska and Hawaii are not in ResStock.

### Outputs
- `../outputs/plugin_solar_per_household.csv` — result 1, by home type.
- `../outputs/plugin_solar_10pct_adoption_national.csv` — result 2.
- `../outputs/plugin_solar_10pct_adoption_by_state.csv` — result 3.
