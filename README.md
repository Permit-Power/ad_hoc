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

## 4) Heating Electrification Bill Savings With and Without Rooftop PV (ResStock + PySAM)

**Primary artifacts**
- Notebook: `solar_bill_analysis.ipynb`
- Module: `solar_bill_analysis.py` 

### Purpose
Quantify how annual household utility bills change under:
- **Baseline** (ResStock Upgrade 0),
- **Heat pump** (Upgrade 7),
- **Heat pump + rooftop PV** (Upgrade 7 net of PV),
and visualize savings by city (median + bootstrap confidence intervals).

### Methodology

#### Data sources
1. **ResStock metadata** (local)
   - Notebook reads `../data/upgrade07.parquet`.
   - Used for:
     - building IDs and location fields (`in.city`, `in.state`, `in.county`)
     - roof area (`out.params.roof_area_ft_2`)
     - orientation (`in.orientation`)
     - weather file coordinates (`in.weather_file_latitude`, `in.weather_file_longitude`, optionally timezone/elevation)
     - simplified tariff fields:
       - `in.utility_bill_electricity_fixed_charges`
       - `in.utility_bill_electricity_marginal_rates`
       - `in.utility_bill_natural_gas_fixed_charges`
       - `in.utility_bill_natural_gas_marginal_rates` (in $/therm)

2. **ResStock hourly end-use profiles** (remote, OEDI data lake)
   - Read from `oedi-data-lake` ResStock timeseries Parquet files for upgrades `("0","7")`.
   - Hourly series used:
     - `out.electricity.total.energy_consumption` → `elec_kwh`
     - `out.natural_gas.total.energy_consumption` → `gas_kwh`

3. **ResStock HPXML building energy models** (remote, via URL)
   - Used to infer roof planes for PV placement (tilt + azimuth).

4. **ResStock TMY3 weather CSVs** (remote, via URL)
   - Used to construct PVWatts-compatible `solar_resource_data` for PySAM.

5. **PV generation cache** (local convenience)
   - The notebook loads a precomputed `../data/pv_all.csv` (hourly PV by building), which can be regenerated by uncommenting the PV loop.

#### Key calculations

1. **Filter target buildings**
   The notebook filters to buildings meeting conditions like:
   - natural gas primary heating fuel,
   - heat pump upgrade applicable,
   - owner-occupied single-family detached,
   - selected target cities.

2. **Build hourly electricity + gas profiles**
   `build_building_hourly_profiles(...)` pulls building-level time series (S3 → DuckDB), aggregates to hourly, and returns a tidy table with:
   - `bldg_id`, `upgrade`, `ts_hour`, `elec_kwh`, `gas_kwh`
   - location fields (`in.city`, `in.state`, `in.county`)

3. **Simulate hourly rooftop PV generation**
   `compute_pv_for_building(...)`:
   - Determines PV **tilt and azimuth**:
     - Prefer HPXML-derived roof plane: largest “south-ish” plane (azimuth 135°–225°),
     - Otherwise fall back to a default tilt + orientation-based azimuth.
   - Sizes PV capacity (kW) via `PVSimulationConfig`:
     - roof-based sizing: `kW = roof_area_ft2 * watts_per_ft2 / 1000`
     - load-based sizing: choose kW so annual PV ≈ `target_solar_fraction * annual_load`,
       using `assumed_cf` to convert kW → annual kWh (`assumed_cf * 8760`)
     - `sizing_mode="min"` uses the *minimum* of roof-based and load-based sizes (i.e., caps load-sized PV by roof feasibility).
   - Builds PVWatts weather input by converting ResStock TMY3 CSV columns to PVWatts keys.
   - Runs PySAM PVWatts and interprets hourly AC output as hourly kWh.

4. **Compute annual bills under each scenario**
   `compute_bills_for_buildings(...)` computes per-building annual bills for:
   - Baseline (U0): electricity + gas
   - Solar-only (U0 + PV): baseline electricity netted with PV (hourly), gas unchanged
   - Heat pump (U7): electricity + gas
   - Heat pump + PV (U7 + PV): U7 electricity netted with PV (hourly)

   Key billing assumptions:
   - Flat volumetric tariffs:
     - Electric bill = `fixed_monthly * 12 + annual_kwh * rate - annual_exports * export_rate`
     - Gas bill = `fixed_monthly * 12 + annual_gas_kwh * gas_rate_per_kwh`
   - Gas marginal rates are converted from \$/therm to $/kWh using:
     - `1 therm = 29.3 kWh`

5. **Aggregate + plot savings by city**
   - Savings are computed relative to baseline.
   - City-level summaries use median savings per city and bootstrap percentile confidence intervals.
   - `plot_city_bill_savings(...)` produces grouped bar charts (HP-only vs HP+PV vs solar-only).

### Outputs
- **PV output (optional intermediate)**: `pv_all.csv`
  - `bldg_id`, `ts_hour`, `pv_kwh`, (optionally `system_kw`)
- **Per-building bills and savings**: `bills_df` DataFrame
  - `bill_baseline`, `bill_solar_only`, `bill_hp`, `bill_hp_pv`
  - `savings_solar_only`, `savings_hp`, `savings_hp_pv`
- **Plots**: grouped bar charts by city (median savings; CI optional)

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
