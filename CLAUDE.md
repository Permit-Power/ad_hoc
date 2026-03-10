# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ad-hoc energy policy analyses for Permit Power. Each analysis is a self-contained Jupyter notebook backed by a reusable Python module in `src/`. The work centers on ResStock building stock data, solar PV simulation (PySAM/PVWatts), and utility bill modeling.

## Environment Setup

```bash
poetry install          # Install all dependencies
poetry shell            # Activate virtualenv
poetry run jupyter lab  # Launch notebooks
```

Python 3.13+ is required. Package management is via Poetry (`pyproject.toml`).

## Repository Structure

- `notebooks/` — Jupyter notebooks (the primary deliverable for each analysis)
- `src/` — Reusable Python modules imported by notebooks
- `data/` — Local input data (CSV, Parquet, shapefiles); also used for intermediate outputs
- `outputs/` — Final output files (e.g., CSVs for external use)

Notebooks assume paths like `../data/` and `../outputs/` relative to the `notebooks/` directory.

## Architecture Pattern

Each analysis follows the same two-layer pattern:

1. **`src/<module>.py`** — Pure Python functions with no notebook-specific code. These handle data fetching, computation, and I/O. Designed to be importable and reusable.
2. **`notebooks/<analysis>.ipynb`** — Orchestrates the module, applies filters/parameters, and produces charts/tables.

Current module–notebook pairs:
| Module | Notebook(s) | Purpose |
|---|---|---|
| `build_hourly_load.py` | `nj_hourly_profiles.ipynb`, `co_hourly_profiles.ipynb`, `il_hourly_profiles.ipynb`, `ma_hourly_profiles.ipynb` | City-level median hourly electricity profiles from ResStock |
| `solar_bill_analysis.py` | `solar_bill_analysis.ipynb` | Household utility bill savings under heat pump + rooftop PV scenarios |
| `avoided_gas_capacity.py` | `avoided_gas_capacity.ipynb` | Natural gas displacement from increased solar adoption |

## Key Data Sources

- **ResStock timeseries** (remote): S3 bucket `oedi-data-lake` (us-west-2), accessed via DuckDB + HTTPFS without credentials (public bucket). Path pattern: `s3://oedi-data-lake/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/resstock_tmy3_release_2/timeseries_individual_buildings/by_state/upgrade={u}/state={state}/{bldg_id}-{u}.parquet`
- **ResStock metadata** (local): Parquet files like `../data/baseline.parquet`, `../data/upgrade07.parquet`
- **PV generation cache** (local): `../data/pv_all.csv` — precomputed to avoid re-running PySAM for every notebook run

## Key Libraries

- **DuckDB** — SQL queries over remote S3 Parquet files via HTTPFS; parallelized with `ThreadPoolExecutor`
- **nrel-pysam** — PVWatts simulation for rooftop solar generation
- **GeoPandas** — State/county shapefiles for spatial joins
- **pandas/numpy** — Data wrangling and aggregation
- **matplotlib/seaborn/plotly** — Visualization
