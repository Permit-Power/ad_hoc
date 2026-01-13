"""
hp_solar_bill_analysis.py

End-to-end utilities to:

1. Pull ResStock building-level hourly electricity and natural gas profiles.
2. Extract roof tilt and azimuth from HPXML for PV placement.
3. Use NREL's PySAM PVWatts to simulate hourly rooftop PV generation.
4. Compute annual utility bills for:
   - Baseline (Upgrade 0),
   - Heat pump only (Upgrade 7),
   - Heat pump + rooftop PV (Upgrade 7 net of PV),
   using simple volumetric tariffs from ResStock metadata.
5. Generate grouped bar plots by city showing bill savings from
   heat pumps and heat pumps + solar, with 95% confidence intervals.

The design follows four main functions:

    1) build_building_hourly_profiles(...)
    2) compute_pv_for_building(...)
    3) compute_bills_for_buildings(...)
    4) plot_city_bill_savings(...)

Additional helpers are provided for:
    - HPXML parsing (roof tilt/azimuth),
    - orientation mapping,
    - simple tariff calculations.

Dependencies
------------
- duckdb
- pandas
- numpy
- matplotlib
- PySAM (Pvwattsv8)
- xml.etree.ElementTree (standard library)
- zipfile (standard library)
- math (standard library)
"""

from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Any

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shutil
import tempfile
from urllib.request import urlopen

import PySAM.Pvwattsv8 as Pvwatts

import xml.etree.ElementTree as ET
from zipfile import ZipFile

# Global cache for solar resource data keyed by (state, county)
_SOLAR_RESOURCE_CACHE: dict[tuple[str, str], dict] = {}


# =============================================================================
# Orientation and HPXML helpers
# =============================================================================

ORIENTATION_TO_AZIMUTH: Dict[str, float] = {
    "North": 0.0,
    "Northeast": 45.0,
    "East": 90.0,
    "Southeast": 135.0,
    "South": 180.0,
    "Southwest": 225.0,
    "West": 270.0,
    "Northwest": 315.0,
}


def orientation_to_azimuth(orientation: str) -> float:
    """
    Map ResStock orientation labels to azimuth degrees for PVWatts/PySAM.

    Parameters
    ----------
    orientation : str
        One of:
            'North', 'Northeast', 'East', 'Southeast',
            'South', 'Southwest', 'West', 'Northwest'.

    Returns
    -------
    float
        Azimuth in degrees, where 0 = North, 90 = East, 180 = South,
        and 270 = West.

    Raises
    ------
    KeyError
        If the orientation value is not recognized.
    """
    if orientation is None:
        raise KeyError("orientation is None")

    cleaned = str(orientation).strip().title()
    return ORIENTATION_TO_AZIMUTH[cleaned]


def pitch_to_tilt_deg(pitch: float) -> float:
    """
    Convert roof pitch (rise per 12 units run) to tilt in degrees.

    Parameters
    ----------
    pitch : float
        Roof pitch as rise per 12 units of run, e.g. 6.0 for "6 in 12".

    Returns
    -------
    float
        Tilt angle in degrees corresponding to the pitch.
    """
    return math.degrees(math.atan(pitch / 12.0))


def _extract_roof_planes_from_hpxml(xml_bytes: bytes) -> List[Dict[str, float]]:
    """
    Parse HPXML bytes and extract roof planes with area, azimuth, and pitch.

    Parameters
    ----------
    xml_bytes : bytes
        Raw HPXML file contents.

    Returns
    -------
    List[dict]
        A list of roof plane dicts of the form:
            {
                "area": float,
                "azimuth": float,
                "pitch": float,
            }
        If no valid roofs are found, the list is empty.
    """
    # Namespace used in recent HPXML schemas; adjust if needed.
    ns = {"h": "http://hpxmlonline.com/2023/09"}

    root = ET.fromstring(xml_bytes)

    roofs: List[Dict[str, float]] = []
    for roof in root.findall(
        ".//h:Building/h:BuildingDetails/h:Enclosure/h:Roofs/h:Roof", ns
    ):
        area_el = roof.find("h:Area", ns)
        az_el = roof.find("h:Azimuth", ns)
        pitch_el = roof.find("h:Pitch", ns)

        if area_el is None or az_el is None or pitch_el is None:
            continue

        try:
            area = float(area_el.text)
            azimuth = float(az_el.text)
            pitch = float(pitch_el.text)
        except (TypeError, ValueError):
            continue

        roofs.append({"area": area, "azimuth": azimuth, "pitch": pitch})

    return roofs

def build_hpxml_zip_path(
    bldg_id: str | int,
    upgrade: str = "0",
    hpxml_base_prefix: str = (
        "https://oedi-data-lake.s3.amazonaws.com/"
        "nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/"
        "2024/resstock_tmy3_release_2/model_and_schedule_files/"
        "building_energy_models"
    ),
) -> str:
    """
    Build the path or URL to a ResStock building-energy-model ZIP (HPXML)
    for a given building and upgrade.

    The naming convention is:

        {hpxml_base_prefix}/upgrade={upgrade}/bldgXXXXXXX-upYY.zip

    where:
        - XXXXXXX is the ResStock building ID zero-padded to 7 digits
          (e.g. bldg_id=123 → "0000123"),
        - YY is the upgrade number zero-padded to 2 digits
          (e.g. upgrade="0" → "00", upgrade="7" → "07").

    Parameters
    ----------
    bldg_id : str or int
        ResStock building ID. Will be converted to int and zero-padded
        to 7 digits in the filename.
    upgrade : str, optional
        Upgrade label as a string (e.g. "0", "7"). Defaults to "0".
    hpxml_base_prefix : str, optional
        Base path or URL to the building_energy_models directory. A sensible
        default is provided for the ResStock TMY3 2024 release.

    Returns
    -------
    str
        Full path or URL to the HPXML ZIP file.
    """
    bldg_int = int(bldg_id)
    bldg_str = f"{bldg_int:07d}"
    up_int = int(upgrade)
    up_str = f"{up_int:02d}"
    return f"{hpxml_base_prefix}/upgrade={up_int}/bldg{bldg_str}-up{up_str}.zip"

def download_to_temp(url: str, suffix: str = "") -> str:
    """
    Download a file from a URL to a temporary local file.

    Parameters
    ----------
    url : str
        HTTP/HTTPS URL to download.
    suffix : str, optional
        File suffix/extension to use for the temporary file (e.g. ".zip",
        ".csv"). Defaults to "".

    Returns
    -------
    str
        Path to the temporary file on the local filesystem. The caller is
        responsible for deleting the file when done, if desired.
    """
    with urlopen(url) as resp:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        with tmp:
            shutil.copyfileobj(resp, tmp)
        return tmp.name

def select_pv_roof_from_hpxml(
    zip_path: str,
    xml_name: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    """
    Select a representative roof plane for PV placement from a ResStock HPXML.

    The function opens a ResStock building ZIP (e.g.
    ".../building_energy_models/upgrade=0/bldg0000001-up00.zip"),
    locates an HPXML file, and extracts roof planes. It then chooses a
    tilt and azimuth using the following strategy:

      1. Parse all roof planes to get (area, azimuth, pitch).
      2. Prefer the largest "south-ish" plane whose azimuth is between
         135° and 225° (SE–S–SW).
      3. If none exist, compute an area-weighted average tilt and azimuth
         across all roof planes.

    Parameters
    ----------
    zip_path : str
        Path to the building ZIP file containing an HPXML document.
    xml_name : str, optional
        Name of the HPXML file inside the ZIP. If None, the first "*.xml"
        entry in the ZIP will be used.

    Returns
    -------
    (tilt_deg, azimuth_deg) or None
        Tilt and azimuth in degrees. If no roof planes can be parsed,
        returns None.
    """
    with ZipFile(zip_path, "r") as zf:
        # Infer XML filename if not provided
        if xml_name is None:
            xml_candidates = [
                name for name in zf.namelist() if name.lower().endswith(".xml")
            ]
            if not xml_candidates:
                return None
            xml_name = xml_candidates[0]

        with zf.open(xml_name) as f:
            xml_bytes = f.read()

    roofs = _extract_roof_planes_from_hpxml(xml_bytes)
    if not roofs:
        return None

    # Prefer largest "south-ish" roof plane (135° to 225°)
    southish = [
        r for r in roofs if 135.0 <= r["azimuth"] <= 225.0
    ]
    if southish:
        best = max(southish, key=lambda r: r["area"])
        return pitch_to_tilt_deg(best["pitch"]), best["azimuth"]

    # Fallback: area-weighted average tilt & azimuth
    total_area = sum(r["area"] for r in roofs)
    if total_area <= 0.0:
        return None

    tilt_weighted = sum(
        pitch_to_tilt_deg(r["pitch"]) * r["area"] for r in roofs
    ) / total_area
    az_weighted = sum(r["azimuth"] * r["area"] for r in roofs) / total_area

    return tilt_weighted, az_weighted


# =============================================================================
# Building-hourly ResStock profile extraction
# =============================================================================


def build_building_hourly_profiles(
    bldg_ids: Sequence[str],
    metadata_df: pd.DataFrame,
    upgrades: Sequence[str] = ("0", "7"),
    bucket: str = "oedi-data-lake",
    base_prefix: str = (
        "nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/"
        "2024/resstock_tmy3_release_2/timeseries_individual_buildings"
    ),
    region: str = "us-west-2",
    chunk_size: int = 500,
    max_workers: int = 8,
    metadata_id_col: str = "bldg_id",
) -> pd.DataFrame:
    """
    Build building-level hourly electricity and natural gas profiles.

    This function reads ResStock individual-building timeseries Parquet
    files directly from S3 (via DuckDB HTTPFS), aggregates electricity
    and natural gas consumption to hourly resolution, and returns a
    tidy DataFrame with:

        - bldg_id  : str
        - upgrade  : str ("0" for baseline, "7" for heat pump, etc.)
        - ts_hour  : pandas.Timestamp (hourly)
        - elec_kwh : float (hourly electricity consumption)
        - gas_kwh  : float (hourly natural gas consumption)
        - in.city
        - in.state
        - in.county

    Unlike a single-state implementation, bldg_ids may span multiple
    states. In that case, the S3 paths are constructed using the
    per-building state from metadata_df["in.state"].

    Parameters
    ----------
    bldg_ids : Sequence[str]
        Building IDs to process, as strings matching the IDs used in
        ResStock timeseries filenames (e.g. "100104").
    metadata_df : pandas.DataFrame
        Metadata table containing at least:

            - ``metadata_id_col`` (e.g. "bldg_id")
            - ``in.city``
            - ``in.state``
            - ``in.county``

        Additional columns (tariffs, roof area, etc.) may be present
        and will be joined later by other functions.
    upgrades : Sequence[str], optional
        Upgrades to load (default is both "0" and "7").
    bucket : str, optional
        S3 bucket name for OEDI data.
    base_prefix : str, optional
        Base S3 prefix to the ResStock timeseries_individual_buildings
        directory, excluding the "upgrade=" and "state=" segments.
    region : str, optional
        AWS S3 region, used to configure DuckDB's S3 settings.
    chunk_size : int, optional
        Number of building IDs per chunk for parallel processing.
    max_workers : int, optional
        Maximum number of worker threads for parallel fetching.
    metadata_id_col : str, optional
        Column name in metadata_df that corresponds to bldg_ids.

    Returns
    -------
    pandas.DataFrame
        A DataFrame with one row per (bldg_id, upgrade, ts_hour), including
        hourly electricity and natural gas usage and metadata fields
        in.city, in.state, in.county.

    Raises
    ------
    KeyError
        If required columns are missing from metadata_df or if a bldg_id
        does not have an associated state.
    """
    required_meta_cols = {metadata_id_col, "in.city", "in.state", "in.county"}
    missing = required_meta_cols - set(metadata_df.columns)
    if missing:
        raise KeyError(
            f"metadata_df is missing required columns: {sorted(missing)}"
        )

    # Normalize building IDs as strings
    bldg_ids = [str(bid) for bid in bldg_ids]
    upgrades_seq = list(upgrades)

    # Build a mapping from bldg_id -> state (from metadata)
    meta_state = (
        metadata_df[[metadata_id_col, "in.state"]]
        .dropna()
        .copy()
    )
    meta_state[metadata_id_col] = meta_state[metadata_id_col].astype(str)
    state_map = dict(
        zip(meta_state[metadata_id_col], meta_state["in.state"])
    )

    # Ensure every requested bldg_id has a state entry
    missing_states = [bid for bid in bldg_ids if bid not in state_map]
    if missing_states:
        raise KeyError(
            f"No in.state found in metadata_df for bldg_ids: {missing_states[:10]} "
            f"(and possibly more)."
        )

    # Prepare chunks for concurrent processing
    def chunk_iterable(seq: Sequence[str], n: int) -> Iterable[List[str]]:
        for i in range(0, len(seq), n):
            yield list(seq[i : i + n])

    part_files: List[str] = []

    # ----------------------------------------------------------------------
    # Step 1: Fetch hourly electricity and natural gas per chunk in parallel
    # ----------------------------------------------------------------------
    def process_chunk(ids_chunk: List[str], part_path: str) -> str:
        con = duckdb.connect(database=":memory:")

        # Configure HTTPFS and S3 settings
        con.execute("INSTALL httpfs;")
        con.execute("LOAD httpfs;")
        con.execute("SET s3_region = ?;", [region])
        con.execute("SET s3_use_ssl = true;")
        con.execute("SET s3_url_style = 'path';")

        # Build S3 paths for this chunk
        # Note: state is now per-building from metadata_df, not a single arg.
        paths = []
        for u in upgrades_seq:
            for bid in ids_chunk:
                st = str(state_map[bid]).upper()
                paths.append(
                    f"s3://{bucket}/{base_prefix}/upgrade={u}/state={st}/{bid}-{u}.parquet"
                )

        con.execute(
            """
            CREATE OR REPLACE TABLE hourly_part AS
            SELECT
              regexp_extract(filename, '.*/upgrade=([^/]+)/', 1) AS upgrade,
              regexp_extract(
                  filename,
                  '.*/state=[A-Z]{2}/([^-]+)-[^/]+\\.parquet$',
                  1
              ) AS bldg_id,
              date_trunc('hour', CAST(timestamp AS TIMESTAMP)) AS ts_hour,
              SUM("out.electricity.total.energy_consumption") AS elec_kwh,
              SUM("out.natural_gas.total.energy_consumption") AS gas_kwh
            FROM read_parquet(?, filename = TRUE)
            GROUP BY 1, 2, 3
            """,
            parameters=[paths],
        )

        con.execute(
            """
            COPY hourly_part TO ? (FORMAT 'parquet');
            """,
            parameters=[part_path],
        )
        con.close()
        return part_path

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os

    # Dispatch chunks
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, chunk in enumerate(chunk_iterable(bldg_ids, chunk_size)):
            part_path = f"hourly_bldg_part_{i:06d}.parquet"
            futures.append(executor.submit(process_chunk, chunk, part_path))

        for fut in as_completed(futures):
            part_files.append(fut.result())

    # ----------------------------------------------------------------------
    # Step 2: Merge part files into a single DuckDB table
    # ----------------------------------------------------------------------
    con = duckdb.connect(database=":memory:")
    con.execute(
        """
        CREATE OR REPLACE TABLE hourly AS
        SELECT * FROM read_parquet(?, filename = FALSE)
        """,
        parameters=[part_files],
    )

    # ----------------------------------------------------------------------
    # Step 3: Register metadata and join
    # ----------------------------------------------------------------------
    meta_df = metadata_df[[metadata_id_col, "in.city", "in.state", "in.county"]].copy()
    meta_df = meta_df.rename(columns={metadata_id_col: "bldg_id"})
    meta_df["bldg_id"] = meta_df["bldg_id"].astype(str)
    con.register("meta_df", meta_df)

    con.execute(
        """
        CREATE OR REPLACE TABLE hourly_with_meta AS
        SELECT
          h.bldg_id,
          h.upgrade,
          h.ts_hour,
          h.elec_kwh,
          h.gas_kwh,
          m."in.city"  AS city,
          m."in.state" AS state,
          m."in.county" AS county
        FROM hourly AS h
        JOIN meta_df AS m
          ON h.bldg_id = m.bldg_id
        """
    )

    result_df = con.execute("SELECT * FROM hourly_with_meta").df()
    con.close()

    # Clean up part files
    for path in part_files:
        try:
            os.remove(path)
        except FileNotFoundError:
            continue

    # Rename columns for clarity
    result_df = result_df.rename(
        columns={
            "city": "in.city",
            "state": "in.state",
            "county": "in.county",
        }
    )

    return result_df


# =============================================================================
# PySAM PV simulation per building
# =============================================================================

@dataclass
class PVSimulationConfig:
    """
    Configuration for PV simulation and system sizing.

    Attributes
    ----------
    sizing_mode : str
        One of {"roof", "load", "min"}:
            - "roof": size PV from roof area and watts_per_ft2.
            - "load": size PV to cover a target fraction of annual load.
            - "min" : compute both roof-based and load-based sizes and
                      use the larger of the two.
    watts_per_ft2 : float
        Power density used for roof-based sizing (W/ft²).
    target_solar_fraction : float
        Target fraction of annual load to offset when sizing by load.
    assumed_cf : float
        Assumed capacity factor used for load-based sizing.
    dc_ac_ratio : float
        DC/AC ratio for PVWatts.
    gcr : float
        Ground coverage ratio for PVWatts.
    tilt_default_deg : float
        Default tilt in degrees. Used only when HPXML-based tilt cannot
        be determined, in which case tilt_default_deg is combined with
        in.orientation to define array geometry.
    export_rate_per_kwh : float
        Export credit rate ($/kWh) used for bill calculation in net
        billing mode when required.
    """

    sizing_mode: str = "roof"          # "roof", "load", or "min"
    watts_per_ft2: float = 15.0
    target_solar_fraction: float = 0.8
    assumed_cf: float = 0.18
    dc_ac_ratio: float = 1.2
    gcr: float = 0.4
    tilt_default_deg: float = 25.0
    export_rate_per_kwh: float = 0.0


def _size_pv_from_roof_area(roof_area_ft2: float, watts_per_ft2: float) -> float:
    """
    Compute DC system size in kW from available roof area.

    Parameters
    ----------
    roof_area_ft2 : float
        Roof area in ft² that is available for PV placement.
    watts_per_ft2 : float
        Panel power density in W/ft² (e.g. 15–20 W/ft²).

    Returns
    -------
    float
        DC system size in kW.
    """
    return (roof_area_ft2 * watts_per_ft2) / 1000.0


def _size_pv_from_load(
    hourly_load_kwh: pd.Series,
    target_solar_fraction: float,
    assumed_cf: float,
) -> float:
    """
    Size PV such that annual PV generation equals a target fraction of annual load.

    Parameters
    ----------
    hourly_load_kwh : pandas.Series
        Hourly electric load in kWh.
    target_solar_fraction : float
        Fraction of annual load to offset (0–1).
    assumed_cf : float
        Assumed PV capacity factor (e.g. 0.18).

    Returns
    -------
    float
        DC system size in kW.
    """
    annual_kwh = float(hourly_load_kwh.sum())
    target_kwh = annual_kwh * target_solar_fraction
    annual_per_kw = assumed_cf * 8760.0
    if annual_per_kw <= 0.0:
        return 0.0
    return target_kwh / annual_per_kw


def build_weather_path(
    state_abbr: str,
    county_id: str,
    weather_base_prefix: str,
) -> str:
    """
    Build the path or URL to a ResStock TMY3 weather CSV given state and county.

    Parameters
    ----------
    state_abbr : str
        Two-letter state abbreviation, e.g. "CA".
    county_id : str
        County ID string matching the TMY3 filename prefix, e.g. "G0600010".
    weather_base_prefix : str
        Base path or URL to the weather directory, e.g.:

            "https://oedi-data-lake.s3.amazonaws.com/"
            "nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/"
            "2024/resstock_tmy3_release_2/weather"

        This function appends "/state=XX/<county_id>_TMY3.csv".

    Returns
    -------
    str
        Full path or URL to the TMY3 CSV.
    """
    state_abbr = state_abbr.upper()
    return f"{weather_base_prefix}/state={state_abbr}/{county_id}_TMY3.csv"

def build_solar_resource_from_resstock_csv(
    csv_path: str,
    lat: float,
    lon: float,
    tz: Optional[float] = None,
    elev: Optional[float] = None,
) -> dict:
    """
    Build a Pvwattsv8-compatible solar_resource_data dictionary from a
    ResStock TMY3-style weather CSV file.

    The ResStock weather CSV has columns such as:

        - ``date_time`` (timestamp string)
        - ``Dry Bulb Temperature [°C]``
        - ``Wind Speed [m/s]``
        - ``Global Horizontal Radiation [W/m2]``
        - ``Direct Normal Radiation [W/m2]``
        - ``Diffuse Horizontal Radiation [W/m2]``

    Pvwattsv8 expects solar_resource_data to be a dictionary containing
    at least:

        - ``lat`` (float): Latitude [deg]
        - ``lon`` (float): Longitude [deg]
        - ``tz``  (float): Time zone offset from GMT [hours]
        - ``elev`` (float): Elevation [m]
        - ``year`` (list[int])
        - ``month`` (list[int])
        - ``day`` (list[int])
        - ``hour`` (list[int])
        - ``minute`` (list[int])
        - ``dn`` (list[float]): Direct normal irradiance [W/m2]
        - ``df`` (list[float]): Diffuse horizontal irradiance [W/m2]
        - ``gh`` (list[float]): Global horizontal irradiance [W/m2]
        - ``tdry`` (list[float]): Dry bulb temperature [°C]
        - ``wspd`` (list[float]): Wind speed [m/s]

    This helper parses the ResStock CSV, derives the required time
    fields from ``date_time``, and maps the radiation and meteorological
    columns into the schema expected by Pvwattsv8.

    Parameters
    ----------
    csv_path : str
        Local filesystem path to the ResStock weather CSV file.
    lat : float
        Site latitude in degrees (from ResStock metadata, e.g.
        ``in.weather_file_latitude``).
    lon : float
        Site longitude in degrees (from ResStock metadata, e.g.
        ``in.weather_file_longitude``).
    tz : float, optional
        Time zone offset from GMT in hours. If None or NaN, an
        approximate time zone is inferred from longitude as
        ``round(lon / 15.0)``.
    elev : float, optional
        Site elevation in meters. If None or NaN, defaults to 0.0.

    Returns
    -------
    dict
        Dictionary suitable for assignment to
        ``pv.SolarResource.solar_resource_data``.
    """
    df = pd.read_csv(csv_path)

    # Parse timestamps
    dt = pd.to_datetime(df["date_time"])

    # Time zone: approximate if not provided
    if tz is None or (isinstance(tz, float) and math.isnan(tz)):
        tz_val = float(round(lon / 15.0))
    else:
        tz_val = float(tz)

    # Elevation: default to 0.0 if unknown
    if elev is None or (isinstance(elev, float) and math.isnan(elev)):
        elev_val = 0.0
    else:
        elev_val = float(elev)

    # Map ResStock columns → PVWatts keys
    dn = df["Direct Normal Radiation [W/m2]"].tolist()
    dfuse = df["Diffuse Horizontal Radiation [W/m2]"].tolist()
    gh = df["Global Horizontal Radiation [W/m2]"].tolist()
    tdry = df["Dry Bulb Temperature [°C]"].tolist()
    wspd = df["Wind Speed [m/s]"].tolist()

    # ResStock weather is hourly → minute = 0 for all timesteps
    minute = [0] * len(df)

    solar_resource_data = {
        "lat": float(lat),
        "lon": float(lon),
        "tz": tz_val,
        "elev": elev_val,
        "year": dt.dt.year.tolist(),
        "month": dt.dt.month.tolist(),
        "day": dt.dt.day.tolist(),
        "hour": dt.dt.hour.tolist(),
        "minute": minute,
        "dn": dn,
        "df": dfuse,
        "gh": gh,
        "tdry": tdry,
        "wspd": wspd,
    }

    return solar_resource_data


def compute_pv_for_building(
    metadata_row: pd.Series,
    hourly_u7: pd.Series,
    pv_config: PVSimulationConfig,
    weather_base_prefix: str,
    hpxml_zip_path: Optional[str] = None,
) -> Tuple[pd.Series, float]:
    """
    Compute hourly rooftop PV generation for a single building using PySAM
    PVWatts (Pvwattsv8), with tilt and azimuth derived from HPXML where
    available and weather data from the ResStock TMY3 CSV.

    Steps
    -----
    1. Determine array tilt and azimuth:

        - If ``hpxml_zip_path`` is provided, attempt to parse the HPXML
          ZIP (downloading it first if it is an HTTP(S) URL) using
          :func:`select_pv_roof_from_hpxml`. On success, use the
          returned tilt and azimuth (degrees) for the array.

        - If HPXML is not provided or parsing fails, fall back to:

              * Tilt = ``pv_config.tilt_default_deg``
              * Azimuth = :func:`orientation_to_azimuth` applied to
                ``metadata_row["in.orientation"]``.

          If orientation mapping fails, azimuth defaults to 180° (south).

    2. Size the system according to ``pv_config.sizing_mode``:

        - ``"roof"``:
            Size from roof area and ``pv_config.watts_per_ft2`` using
            :func:`_size_pv_from_roof_area` and the metadata field
            ``"out.params.roof_area_ft_2"`` (default 0.0 if missing).

        - ``"load"``:
            Size from the annual Upgrade 7 load using
            :func:`_size_pv_from_load`, targeting
            ``pv_config.target_solar_fraction`` of annual load and
            ``pv_config.assumed_cf`` as capacity factor.

        - ``"min"``:
            Compute both roof-based and load-based sizes and use the
            minimum of the two.

       If the resulting ``system_capacity_kw`` is <= 0, the function
       returns a zero Series (aligned with ``hourly_u7.index``) and a
       capacity of 0.0 kW.

    3. Build the solar resource data from ResStock weather:

        - Construct the ResStock weather CSV path (or URL) using
          :func:`build_weather_path` with ``metadata_row["in.state"]``,
          ``metadata_row["in.county"]``, and ``weather_base_prefix``.

        - If the resulting path is an HTTP(S) URL, download it to a
          temporary local CSV file using :func:`download_to_temp`.

        - Read the CSV and build a
          ``solar_resource_data`` dictionary via
          :func:`build_solar_resource_from_resstock_csv`, using:

              * Latitude  = ``metadata_row["in.weather_file_latitude"]``
              * Longitude = ``metadata_row["in.weather_file_longitude"]``
              * Optional time zone and elevation if present in metadata.

        - Assign ``pv.SolarResource.solar_resource_data = solar_resource_data``.

          Note that we **do not** use ``solar_resource_file`` here,
          because the ResStock CSV is not in SAM’s native weather
          format, and PVWatts requires latitude and longitude when using
          custom resource data.

    4. Configure and run PVWatts:

        - Create a default Pvwattsv8 object (e.g. "PVWattsSingleOwner").
        - Set:

              * ``pv.SolarResource.solar_resource_data``
              * ``pv.SystemDesign.system_capacity``
              * ``pv.SystemDesign.azimuth``
              * ``pv.SystemDesign.tilt``
              * ``pv.SystemDesign.dc_ac_ratio``
              * ``pv.SystemDesign.gcr``

        - Call ``pv.execute()``.

        The output ``pv.Outputs.ac`` is an array of hourly AC power in
        kW. Because the timestep is 1 hour, each value is interpreted
        as kWh for that hour.

        The function wraps this in a pandas Series and aligns the index
        with ``hourly_u7.index``.

    Parameters
    ----------
    metadata_row : pandas.Series
        Single-row metadata for the building. Must contain:

            - ``"in.state"``: two-letter state abbreviation or name.
            - ``"in.county"``: county ID used in weather filenames.
            - ``"in.orientation"``: ResStock orientation label.
            - ``"out.params.roof_area_ft_2"``: roof area (ft²), for
              roof-based sizing (0 if missing).
            - ``"in.weather_file_latitude"``: latitude (deg).
            - ``"in.weather_file_longitude"``: longitude (deg).

        If present, the following are used to refine the solar resource:

            - ``"in.weather_file_time_zone"``: timezone offset from GMT.
            - ``"in.weather_file_elevation"``: elevation (m).

    hourly_u7 : pandas.Series
        Hourly electric load (kWh) for Upgrade 7 (heat pump scenario),
        indexed by hour (DatetimeIndex or similar).

    pv_config : PVSimulationConfig
        Configuration for PV sizing and PVWatts parameters.

    weather_base_prefix : str
        Base path or URL to the ResStock weather directory.

    hpxml_zip_path : str, optional
        Local path or HTTP(S) URL to the HPXML zip for this building
        (typically upgrade 0). If provided, HPXML is used to infer tilt
        and azimuth where possible.

    Returns
    -------
    (pv_kwh, system_capacity_kw) : (pandas.Series, float)
        pv_kwh :
            Hourly PV generation in kWh, aligned to ``hourly_u7.index``.
        system_capacity_kw :
            DC system capacity (kW) used in the PVWatts simulation.

    Raises
    ------
    RuntimeError
        If PySAM.Pvwattsv8 is not available.
    ValueError
        If an invalid sizing mode is specified.
    """
    if Pvwatts is None:
        raise RuntimeError(
            "PySAM.Pvwattsv8 is not available. Install PySAM before "
            "calling compute_pv_for_building."
        )

    # ------------------------------------------------------------------
    # 1. Determine tilt and azimuth: HPXML if available, else orientation
    # ------------------------------------------------------------------
    tilt_deg: float
    azimuth_deg: float
    hpxml_result: Optional[Tuple[float, float]] = None

    if hpxml_zip_path is not None:
        try:
            # If HPXML path is a URL, download to a temporary ZIP file
            if str(hpxml_zip_path).startswith(("http://", "https://")):
                local_zip_path = download_to_temp(hpxml_zip_path, suffix=".zip")
            else:
                local_zip_path = hpxml_zip_path

            hpxml_result = select_pv_roof_from_hpxml(local_zip_path)
        except Exception:
            hpxml_result = None

    if hpxml_result is not None:
        tilt_deg, azimuth_deg = hpxml_result
    else:
        tilt_deg = pv_config.tilt_default_deg
        try:
            azimuth_deg = orientation_to_azimuth(metadata_row["in.orientation"])
        except Exception:
            azimuth_deg = 180.0  # south-facing fallback

    # ------------------------------------------------------------------
    # 2. Size the system
    # ------------------------------------------------------------------
    sizing_mode = pv_config.sizing_mode.lower()

    roof_size_kw = 0.0
    if sizing_mode in ("roof", "min"):
        roof_area = float(metadata_row.get("out.params.roof_area_ft_2", 0.0))
        roof_size_kw = _size_pv_from_roof_area(
            roof_area_ft2=roof_area,
            watts_per_ft2=pv_config.watts_per_ft2,
        )

    load_size_kw = 0.0
    if sizing_mode in ("load", "min"):
        load_size_kw = _size_pv_from_load(
            hourly_load_kwh=hourly_u7,
            target_solar_fraction=pv_config.target_solar_fraction,
            assumed_cf=pv_config.assumed_cf,
        )

    if sizing_mode == "roof":
        system_capacity_kw = roof_size_kw
    elif sizing_mode == "load":
        system_capacity_kw = load_size_kw
    elif sizing_mode == "min":
        system_capacity_kw = min(roof_size_kw, load_size_kw)
    else:
        raise ValueError(
            f"Invalid sizing_mode '{pv_config.sizing_mode}'. "
            "Use 'roof', 'load', or 'min'."
        )

    if system_capacity_kw <= 0.0:
        return pd.Series(0.0, index=hourly_u7.index), 0.0

    # ------------------------------------------------------------------
    # 3. Build solar_resource_data from ResStock weather + metadata lat/lon
    #    with caching by (state, county) to avoid re-reading the same CSV
    # ------------------------------------------------------------------
    global _SOLAR_RESOURCE_CACHE

    state_abbr = str(metadata_row["in.state"])
    county_id = str(metadata_row["in.county"])
    cache_key = (state_abbr, county_id)

    solar_resource_data = _SOLAR_RESOURCE_CACHE.get(cache_key)

    if solar_resource_data is None:
        weather_path = build_weather_path(
            state_abbr=state_abbr,
            county_id=county_id,
            weather_base_prefix=weather_base_prefix,
        )

        # Download TMY3 CSV if needed
        if weather_path.startswith(("http://", "https://")):
            local_weather_path = download_to_temp(weather_path, suffix=".csv")
        else:
            local_weather_path = weather_path

        lat = float(metadata_row["in.weather_file_latitude"])
        lon = float(metadata_row["in.weather_file_longitude"])
        tz_val = metadata_row.get("in.weather_file_time_zone", None)
        elev_val = metadata_row.get("in.weather_file_elevation", None)

        solar_resource_data = build_solar_resource_from_resstock_csv(
            csv_path=local_weather_path,
            lat=lat,
            lon=lon,
            tz=float(tz_val) if tz_val is not None else None,
            elev=float(elev_val) if elev_val is not None else None,
        )

        _SOLAR_RESOURCE_CACHE[cache_key] = solar_resource_data

    # ------------------------------------------------------------------
    # 4. Configure and run PVWatts
    # ------------------------------------------------------------------
    pv = Pvwatts.default("PVWattsSingleOwner")
    pv.SolarResource.solar_resource_data = solar_resource_data

    pv.SystemDesign.system_capacity = system_capacity_kw
    pv.SystemDesign.azimuth = azimuth_deg
    pv.SystemDesign.tilt = tilt_deg
    pv.SystemDesign.dc_ac_ratio = pv_config.dc_ac_ratio
    pv.SystemDesign.gcr = pv_config.gcr

    pv.execute()

    ac_kw = pd.Series(pv.Outputs.ac) / 1000.0
    n_pv = len(ac_kw)
    n_load = len(hourly_u7)

    # Work on a local copy of the load index so we don't mutate the caller's Series
    load_index = hourly_u7.index

    if n_pv != n_load:
        # Case 1: load has one extra hour (your observed case)
        if n_load == n_pv + 1:
            # Drop the last hour from the load for alignment
            load_index = load_index[:n_pv]

        # Case 2 (less common): PV has one extra hour
        elif n_pv == n_load + 1:
            ac_kw = ac_kw.iloc[:n_load]

        else:
            raise ValueError(
                f"PVWatts AC output length {n_pv} does not match load length {n_load} "
                "and cannot be trivially aligned (difference != 1)."
            )

    # Align the PV series to the (possibly trimmed) load index
    ac_kw.index = load_index

    return ac_kw, system_capacity_kw


# =============================================================================
# Billing calculations
# =============================================================================


def compute_annual_electric_bill(
    annual_kwh: float,
    fixed_charge_monthly: float,
    marginal_rate_per_kwh: float,
    annual_export_kwh: float = 0.0,
    export_rate_per_kwh: float = 0.0,
) -> float:
    """
    Compute an annual electric bill in dollars for a single building.

    This function assumes a simple flat volumetric tariff with:
        - A fixed monthly charge ($/month).
        - A constant marginal energy rate ($/kWh).
        - An optional export credit for PV exports ($/kWh).

    Parameters
    ----------
    annual_kwh : float
        Annual electric consumption in kWh (net of PV if modeling a
        net-load case).
    fixed_charge_monthly : float
        Fixed electric charge per month ($/month).
    marginal_rate_per_kwh : float
        Volumetric electric rate applied to consumption ($/kWh).
    annual_export_kwh : float, optional
        Annual exported PV energy in kWh. Defaults to 0.0. This is used
        to compute an export credit at ``export_rate_per_kwh``.
    export_rate_per_kwh : float, optional
        Credit rate for exported energy ($/kWh). Defaults to 0.0.

    Returns
    -------
    float
        Annual electric bill in dollars.

    Notes
    -----
    The bill is computed as:

        bill = fixed_charge_monthly * 12
               + annual_kwh * marginal_rate_per_kwh
               - annual_export_kwh * export_rate_per_kwh
    """
    annual_kwh = float(annual_kwh)
    annual_export_kwh = float(annual_export_kwh)

    fixed_charge = fixed_charge_monthly * 12.0
    energy_charge = annual_kwh * marginal_rate_per_kwh
    export_credit = annual_export_kwh * export_rate_per_kwh

    return fixed_charge + energy_charge - export_credit


def compute_annual_gas_bill(
    annual_gas_kwh: float,
    fixed_charge_monthly: float,
    marginal_rate_per_kwh: float,
) -> float:
    """
    Compute an annual natural gas bill in dollars for a single building.

    This function assumes a simple flat volumetric tariff with:
        - A fixed monthly charge ($/month).
        - A constant marginal energy rate ($/kWh).

    Parameters
    ----------
    annual_gas_kwh : float
        Annual gas consumption in kWh.
    fixed_charge_monthly : float
        Fixed natural gas charge per month ($/month).
    marginal_rate_per_kwh : float
        Volumetric natural gas rate ($/kWh).

    Returns
    -------
    float
        Annual natural gas bill in dollars.

    Notes
    -----
    The bill is computed as:

        bill = fixed_charge_monthly * 12
               + annual_gas_kwh * marginal_rate_per_kwh
    """
    annual_gas_kwh = float(annual_gas_kwh)

    fixed_charge = fixed_charge_monthly * 12.0
    energy_charge = annual_gas_kwh * marginal_rate_per_kwh

    return fixed_charge + energy_charge


def compute_bills_for_buildings(
    hourly_df: pd.DataFrame,
    metadata_df: pd.DataFrame,
    pv_df: pd.DataFrame,
    export_rate_per_kwh: float = 0.0,
    baseline_upgrade: str = "0",
    hp_upgrade: str = "7",
    metadata_id_col: str = "bldg_id",
) -> pd.DataFrame:
    """
    Compute annual bills and bill savings for baseline, solar-only,
    heat pump-only, and heat pump+PV for a set of buildings.

    Cases
    -----
    1) Baseline (Upgrade 0):
       - bill_baseline

    2) Solar only (Upgrade 0 + PV):
       - bill_solar_only
       - savings_solar_only = bill_baseline - bill_solar_only

    3) Heat pump only (Upgrade 7):
       - bill_hp
       - savings_hp = bill_baseline - bill_hp

    4) Heat pump + PV (Upgrade 7 + PV):
       - bill_hp_pv
       - savings_hp_pv = bill_baseline - bill_hp_pv

    Notes
    -----
    - PV is applied to electricity only via hourly netting:
        net_elec_kwh  = max(load_kwh - pv_kwh, 0)
        export_kwh    = max(pv_kwh - load_kwh, 0)

    - Gas rates in ResStock metadata are in $/therm and are converted
      internally to $/kWh using 1 therm = 29.3 kWh.

    Parameters
    ----------
    hourly_df : pandas.DataFrame
        Output of build_building_hourly_profiles.
    metadata_df : pandas.DataFrame
        ResStock metadata including tariff columns.
    pv_df : pandas.DataFrame
        Hourly PV generation with columns ['bldg_id', 'ts_hour', 'pv_kwh'].
        (May also include 'system_kw' or other columns; they are ignored.)
    export_rate_per_kwh : float, optional
        Export credit rate ($/kWh) applied to annual exports.
    baseline_upgrade : str, optional
        Baseline upgrade label, default "0".
    hp_upgrade : str, optional
        Heat pump upgrade label, default "7".
    metadata_id_col : str, optional
        Column in metadata_df corresponding to bldg_id.

    Returns
    -------
    pandas.DataFrame
        Per-building bills and savings including solar-only metrics.
    """
    hourly_df = hourly_df.copy()
    metadata_df = metadata_df.copy()
    pv_df = pv_df.copy()

    # Ensure IDs are strings
    hourly_df["bldg_id"] = hourly_df["bldg_id"].astype(str)
    pv_df["bldg_id"] = pv_df["bldg_id"].astype(str)
    metadata_df[metadata_id_col] = metadata_df[metadata_id_col].astype(str)

    # Ensure ts_hour is datetime for merges
    if "ts_hour" in hourly_df.columns:
        hourly_df["ts_hour"] = pd.to_datetime(hourly_df["ts_hour"])
    if "ts_hour" in pv_df.columns:
        pv_df["ts_hour"] = pd.to_datetime(pv_df["ts_hour"])

    # Split hourly profiles
    hourly_baseline = hourly_df[hourly_df["upgrade"] == baseline_upgrade]
    hourly_hp = hourly_df[hourly_df["upgrade"] == hp_upgrade]

    # Annual baseline and HP energy
    agg_baseline = (
        hourly_baseline.groupby("bldg_id")
        .agg(
            annual_elec_0_kwh=("elec_kwh", "sum"),
            annual_gas_0_kwh=("gas_kwh", "sum"),
            city=("in.city", "first"),
            state=("in.state", "first"),
            county=("in.county", "first"),
        )
        .reset_index()
    )

    agg_hp = (
        hourly_hp.groupby("bldg_id")
        .agg(
            annual_elec_7_kwh=("elec_kwh", "sum"),
            annual_gas_7_kwh=("gas_kwh", "sum"),
        )
        .reset_index()
    )

    annual = pd.merge(agg_baseline, agg_hp, on="bldg_id", how="inner")

    # ---- Solar-only (baseline + PV) hourly netting ----
    base_with_pv = pd.merge(
        hourly_baseline[["bldg_id", "ts_hour", "elec_kwh"]],
        pv_df[["bldg_id", "ts_hour", "pv_kwh"]],
        on=["bldg_id", "ts_hour"],
        how="left",
    )
    base_with_pv["pv_kwh"] = base_with_pv["pv_kwh"].fillna(0.0)

    base_with_pv["net_elec_kwh"] = (base_with_pv["elec_kwh"] - base_with_pv["pv_kwh"]).clip(lower=0.0)
    base_with_pv["export_kwh"] = (base_with_pv["pv_kwh"] - base_with_pv["elec_kwh"]).clip(lower=0.0)

    agg_base_pv = (
        base_with_pv.groupby("bldg_id")
        .agg(
            annual_elec_0_net_kwh=("net_elec_kwh", "sum"),
            annual_exports_0_kwh=("export_kwh", "sum"),
        )
        .reset_index()
    )
    annual = pd.merge(annual, agg_base_pv, on="bldg_id", how="left")
    annual["annual_elec_0_net_kwh"] = annual["annual_elec_0_net_kwh"].fillna(annual["annual_elec_0_kwh"])
    annual["annual_exports_0_kwh"] = annual["annual_exports_0_kwh"].fillna(0.0)

    # ---- HP + PV hourly netting ----
    hp_with_pv = pd.merge(
        hourly_hp[["bldg_id", "ts_hour", "elec_kwh"]],
        pv_df[["bldg_id", "ts_hour", "pv_kwh"]],
        on=["bldg_id", "ts_hour"],
        how="left",
    )
    hp_with_pv["pv_kwh"] = hp_with_pv["pv_kwh"].fillna(0.0)

    hp_with_pv["net_elec_kwh"] = (hp_with_pv["elec_kwh"] - hp_with_pv["pv_kwh"]).clip(lower=0.0)
    hp_with_pv["export_kwh"] = (hp_with_pv["pv_kwh"] - hp_with_pv["elec_kwh"]).clip(lower=0.0)

    agg_hp_pv = (
        hp_with_pv.groupby("bldg_id")
        .agg(
            annual_elec_7_net_kwh=("net_elec_kwh", "sum"),
            annual_exports_kwh=("export_kwh", "sum"),
        )
        .reset_index()
    )

    annual = pd.merge(annual, agg_hp_pv, on="bldg_id", how="left")
    annual["annual_elec_7_net_kwh"] = annual["annual_elec_7_net_kwh"].fillna(annual["annual_elec_7_kwh"])
    annual["annual_exports_kwh"] = annual["annual_exports_kwh"].fillna(0.0)

    # Tariffs
    meta_cols = [
        metadata_id_col,
        "in.utility_bill_electricity_fixed_charges",
        "in.utility_bill_electricity_marginal_rates",
        "in.utility_bill_natural_gas_fixed_charges",
        "in.utility_bill_natural_gas_marginal_rates",
    ]
    missing_meta = [c for c in meta_cols if c not in metadata_df.columns]
    if missing_meta:
        raise KeyError(f"metadata_df is missing required tariff columns: {missing_meta}")

    meta_tariffs = metadata_df[meta_cols].copy()
    meta_tariffs = meta_tariffs.rename(columns={metadata_id_col: "bldg_id"})
    meta_tariffs["bldg_id"] = meta_tariffs["bldg_id"].astype(str)
    annual = pd.merge(annual, meta_tariffs, on="bldg_id", how="left")

    # Bills per building
    records: list[dict[str, Any]] = []
    for _, row in annual.iterrows():
        bid = row["bldg_id"]

        elec_fixed = float(row["in.utility_bill_electricity_fixed_charges"])
        elec_rate = float(row["in.utility_bill_electricity_marginal_rates"])

        gas_fixed = float(row["in.utility_bill_natural_gas_fixed_charges"])
        gas_rate_therm = float(row["in.utility_bill_natural_gas_marginal_rates"])
        gas_rate = gas_rate_therm / 29.3  # $/therm -> $/kWh

        # Energies
        annual_elec_0 = float(row["annual_elec_0_kwh"])
        annual_gas_0 = float(row["annual_gas_0_kwh"])

        annual_elec_0_net = float(row["annual_elec_0_net_kwh"])
        annual_exports_0 = float(row["annual_exports_0_kwh"])

        annual_elec_7 = float(row["annual_elec_7_kwh"])
        annual_gas_7 = float(row["annual_gas_7_kwh"])

        annual_elec_7_net = float(row["annual_elec_7_net_kwh"])
        annual_exports_7 = float(row["annual_exports_kwh"])

        # Baseline
        bill_ele_0 = compute_annual_electric_bill(
            annual_kwh=annual_elec_0,
            fixed_charge_monthly=elec_fixed,
            marginal_rate_per_kwh=elec_rate,
        )
        bill_gas_0 = compute_annual_gas_bill(
            annual_gas_kwh=annual_gas_0,
            fixed_charge_monthly=gas_fixed,
            marginal_rate_per_kwh=gas_rate,
        )
        bill_baseline = bill_ele_0 + bill_gas_0

        # Solar only (baseline + PV)
        bill_ele_0_pv = compute_annual_electric_bill(
            annual_kwh=annual_elec_0_net,
            fixed_charge_monthly=elec_fixed,
            marginal_rate_per_kwh=elec_rate,
            annual_export_kwh=annual_exports_0,
            export_rate_per_kwh=export_rate_per_kwh,
        )
        bill_solar_only = bill_ele_0_pv + bill_gas_0

        # Heat pump only
        bill_ele_7 = compute_annual_electric_bill(
            annual_kwh=annual_elec_7,
            fixed_charge_monthly=elec_fixed,
            marginal_rate_per_kwh=elec_rate,
        )
        bill_gas_7 = compute_annual_gas_bill(
            annual_gas_kwh=annual_gas_7,
            fixed_charge_monthly=gas_fixed,
            marginal_rate_per_kwh=gas_rate,
        )
        bill_hp = bill_ele_7 + bill_gas_7

        # Heat pump + PV
        bill_ele_7_pv = compute_annual_electric_bill(
            annual_kwh=annual_elec_7_net,
            fixed_charge_monthly=elec_fixed,
            marginal_rate_per_kwh=elec_rate,
            annual_export_kwh=annual_exports_7,
            export_rate_per_kwh=export_rate_per_kwh,
        )
        bill_hp_pv = bill_ele_7_pv + bill_gas_7

        records.append(
            {
                "bldg_id": bid,
                "in.city": row["city"],
                "in.state": row["state"],
                "in.county": row["county"],

                "bill_baseline": bill_baseline,
                "bill_solar_only": bill_solar_only,
                "bill_hp": bill_hp,
                "bill_hp_pv": bill_hp_pv,

                "savings_solar_only": bill_baseline - bill_solar_only,
                "savings_hp": bill_baseline - bill_hp,
                "savings_hp_pv": bill_baseline - bill_hp_pv,

                "annual_elec_0_kwh": annual_elec_0,
                "annual_elec_0_net_kwh": annual_elec_0_net,
                "annual_exports_0_kwh": annual_exports_0,

                "annual_elec_7_kwh": annual_elec_7,
                "annual_elec_7_net_kwh": annual_elec_7_net,
                "annual_exports_kwh": annual_exports_7,

                "annual_gas_0_kwh": annual_gas_0,
                "annual_gas_7_kwh": annual_gas_7,
            }
        )

    return pd.DataFrame.from_records(records)


# =============================================================================
# Plotting: grouped bar by city with 95% CI
# =============================================================================


def _summarize_city_savings(
    results_df: pd.DataFrame,
    n_boot: int = 1000,
    ci: float = 0.95,
    random_seed: int = 0,
) -> pd.DataFrame:
    """
    Compute median savings and bootstrap confidence intervals by city for:
      - Solar only
      - Heat pump only
      - Heat pump + solar

    Parameters
    ----------
    results_df : pandas.DataFrame
        DataFrame with at least:
            - "in.city"
            - "savings_solar_only"
            - "savings_hp"
            - "savings_hp_pv"
    n_boot : int, optional
        Number of bootstrap resamples per city (default 1000).
    ci : float, optional
        Central confidence level (default 0.95). Uses the percentile
        bootstrap interval.
    random_seed : int, optional
        Seed for reproducibility (default 0).

    Returns
    -------
    pandas.DataFrame
        One row per city with:
            - in.city
            - median_savings_solar_only, ci_savings_solar_only
            - median_savings_hp,         ci_savings_hp
            - median_savings_hp_pv,      ci_savings_hp_pv

    Notes
    -----
    Confidence intervals are bootstrap percentile intervals around the median.
    The returned ``ci_*`` values are half-widths (median minus lower bound or
    upper bound minus median, whichever is larger), suitable for symmetric
    error bars.
    """
    rng = np.random.default_rng(random_seed)
    alpha = (1.0 - ci) / 2.0
    lo_q = 100.0 * alpha
    hi_q = 100.0 * (1.0 - alpha)

    def median_ci_halfwidth(values: np.ndarray) -> Tuple[float, float]:
        values = values.astype(float)
        values = values[~np.isnan(values)]
        if values.size == 0:
            return (np.nan, np.nan)
        med = float(np.median(values))
        if values.size == 1:
            return (med, 0.0)

        boots = np.empty(n_boot, dtype=float)
        n = values.size
        for i in range(n_boot):
            sample = rng.choice(values, size=n, replace=True)
            boots[i] = np.median(sample)

        lo = float(np.percentile(boots, lo_q))
        hi = float(np.percentile(boots, hi_q))
        halfwidth = max(med - lo, hi - med)
        return (med, halfwidth)

    rows = []
    for city, g in results_df.groupby("in.city"):
        m_solar, ci_solar = median_ci_halfwidth(g["savings_solar_only"].to_numpy())
        m_hp, ci_hp = median_ci_halfwidth(g["savings_hp"].to_numpy())
        m_hp_pv, ci_hp_pv = median_ci_halfwidth(g["savings_hp_pv"].to_numpy())

        rows.append(
            {
                "in.city": city,
                "median_savings_solar_only": m_solar,
                "ci_savings_solar_only": ci_solar,
                "median_savings_hp": m_hp,
                "ci_savings_hp": ci_hp,
                "median_savings_hp_pv": m_hp_pv,
                "ci_savings_hp_pv": ci_hp_pv,
                "n": int(len(g)),
            }
        )

    return pd.DataFrame(rows)


def plot_city_bill_savings(
    results_df: pd.DataFrame,
    cities: Sequence[str],
    figsize: Tuple[int, int] = (11, 6),
    n_boot: int = 1000,
    ci: float = 0.95,
    random_seed: int = 0,
) -> None:
    """
    Grouped bar plot by city showing median annual bill savings for:
        - Heat pump only
        - Heat pump + solar
        - Solar only

    Includes bootstrap confidence intervals (default 95%) as error bars.

    Cities are filtered to `cities` and ordered by highest to lowest
    median heat pump + solar savings.
    """
    df = results_df[results_df["in.city"].isin(cities)].copy()
    if df.empty:
        raise ValueError("No buildings found for the specified cities.")

    summary = _summarize_city_savings(
        df, n_boot=n_boot, ci=ci, random_seed=random_seed
    )

    # Order cities by HP + PV savings (descending)
    summary = summary.sort_values(
        "median_savings_hp_pv", ascending=False
    ).reset_index(drop=True)

    x = np.arange(len(summary))
    width = 0.25

    fig, ax = plt.subplots(figsize=figsize)

    # ---- Bar order: HP | HP + PV | Solar only ----
    ax.bar(
        x - width,
        summary["median_savings_hp"],
        width,
        #yerr=summary["ci_savings_hp"],
        capsize=4,
        label="Heat pump only",
    )
    ax.bar(
        x,
        summary["median_savings_hp_pv"],
        width,
        #yerr=summary["ci_savings_hp_pv"],
        capsize=4,
        label="Heat pump + solar",
    )
    ax.bar(
        x + width,
        summary["median_savings_solar_only"],
        width,
        #yerr=summary["ci_savings_solar_only"],
        capsize=4,
        label="Solar only",
    )

    ax.set_xticks(x)
    ax.set_xticklabels(summary["in.city"], rotation=45, ha="right")
    ax.set_ylabel("Median Annual bill savings ($/year)")
    ax.set_title(
        "Bill savings by city: heat pump vs. heat pump + solar vs. solar"
    )
    ax.legend()
    plt.tight_layout()
    plt.show()

