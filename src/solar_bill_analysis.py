"""
solar_bill_analysis.py

Building-level simulation for comparing heat pump and gas furnace running
costs, with and without rooftop solar, batteries and plug-in solar, using
ResStock building data and PySAM PVWatts.

Main steps, in the order the notebook calls them:

    load_resstock_metadata(...)          ResStock metadata from S3
    build_building_hourly_profiles(...)  hourly electricity and gas load per building
    compute_pv_for_buildings(...)        rooftop PV sized and simulated per building
    compute_annual_flows(...)            annual grid imports/exports with and without
                                         a heat pump, each with and without a battery
    compute_plugin_solar(...)            plug-in solar production and self-consumption
    financed_annual_cost(...)            yearly cost of financed equipment

Supporting helpers cover HPXML roof parsing, PV sizing, ResStock weather files,
battery dispatch and hourly wholesale export rates.
"""

from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Any

import duckdb
import numpy as np
import pandas as pd
import shutil
import tempfile
import time
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

def download_to_temp(url: str, suffix: str = "", attempts: int = 3) -> str:
    """
    Download a file from a URL to a temporary local file.

    Parameters
    ----------
    url : str
        HTTP/HTTPS URL to download.
    suffix : str, optional
        File suffix/extension to use for the temporary file (e.g. ".zip",
        ".csv"). Defaults to "".
    attempts : int, optional
        Number of tries before giving up on transient network errors.
        Defaults to 3.

    Returns
    -------
    str
        Path to the temporary file on the local filesystem. The caller is
        responsible for deleting the file when done, if desired.
    """
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(url, timeout=60) as resp:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                with tmp:
                    shutil.copyfileobj(resp, tmp)
                return tmp.name
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(2 ** attempt)

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
      3. If none exist, use the plane facing closest to south, breaking
         ties (e.g. an east/west gable) by area.

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

    # Fallback: plane facing closest to south, largest first on ties.
    # (Averaging azimuths would turn an east/west gable into a south roof.)
    def degrees_from_south(r: Dict[str, float]) -> float:
        return abs(((r["azimuth"] - 180.0) + 180.0) % 360.0 - 180.0)

    best = min(roofs, key=lambda r: (degrees_from_south(r), -r["area"]))
    return pitch_to_tilt_deg(best["pitch"]), best["azimuth"]


# =============================================================================
# ResStock metadata
# =============================================================================


def load_resstock_metadata(
    upgrade: str = "2",
    filters: Optional[Dict[str, Any]] = None,
    bucket: str = "oedi-data-lake",
    base_prefix: str = (
        "nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/"
        "2024/resstock_tmy3_release_2/metadata"
    ),
    region: str = "us-west-2",
) -> pd.DataFrame:
    """
    Load ResStock building metadata for one upgrade directly from S3.

    The files follow the naming convention:

        s3://{bucket}/{base_prefix}/baseline.parquet       (upgrade "0")
        s3://{bucket}/{base_prefix}/upgradeYY.parquet      (other upgrades)

    where YY is the upgrade number zero-padded to 2 digits. Each file is
    ~200 MB, so optional column filters are pushed down to DuckDB to avoid
    pulling the full national file into memory.

    Parameters
    ----------
    upgrade : str, optional
        Upgrade label as a string (e.g. "0", "2"). Defaults to "2".
    filters : dict, optional
        Mapping of metadata column → value to keep. A list or tuple value
        keeps rows matching any of its elements, e.g.
        ``{"in.tenure": "Owner", "in.city": ["CO, Denver", "NJ, Newark"]}``.
        Filters are combined with AND. If None, all buildings are returned.
    bucket : str, optional
        S3 bucket name. Defaults to "oedi-data-lake".
    base_prefix : str, optional
        S3 prefix of the metadata directory. Defaults to the ResStock TMY3 2024
        Release 2 metadata.
    region : str, optional
        AWS region of the bucket. Defaults to "us-west-2".

    Returns
    -------
    pandas.DataFrame
        One row per building with all metadata and annual result columns.
        ``bldg_id`` is returned as a string column.
    """
    up_int = int(upgrade)
    fname = "baseline.parquet" if up_int == 0 else f"upgrade{up_int:02d}.parquet"
    path = f"s3://{bucket}/{base_prefix}/{fname}"

    con = duckdb.connect(database=":memory:")
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute("SET s3_region = ?;", [region])

    query = f"SELECT * FROM read_parquet('{path}')"
    clauses: List[str] = []
    params: List[Any] = []
    for col, val in (filters or {}).items():
        col_sql = '"' + col.replace('"', '""') + '"'
        if isinstance(val, (list, tuple)):
            clauses.append(f"{col_sql} IN (SELECT UNNEST(?))")
            params.append(list(val))
        else:
            clauses.append(f"{col_sql} = ?")
            params.append(val)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    df = con.execute(query, params).df()
    con.close()

    df["bldg_id"] = df["bldg_id"].astype(str)
    return df


# =============================================================================
# Building-hourly ResStock profile extraction
# =============================================================================


def build_building_hourly_profiles(
    bldg_ids: Sequence[str],
    metadata_df: pd.DataFrame,
    upgrades: Sequence[str] = ("0", "2"),
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
        - upgrade  : str ("0" for baseline, or an upgrade number)
        - ts_hour  : pandas.Timestamp (hourly)
        - elec_kwh : float (hourly electricity consumption)
        - gas_kwh  : float (hourly natural gas consumption)
        - in.city
        - in.state
        - in.county

    bldg_ids may span multiple states; each building's S3 path uses its
    state from metadata_df["in.state"].

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
        Upgrades to load. Defaults to ("0", "2").
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
                      use the smaller of the two.
    watts_per_ft2 : float
        Power density used for roof-based sizing (W/ft²).
    target_solar_fraction : float
        Target fraction of annual load to offset when sizing by load.
    assumed_cf : float or None
        Capacity factor used to convert annual load to kW when sizing by
        load. If None, each building's own simulated output per kW is used
        instead, as an installer sizing to local production would.
    max_system_kw : float or None
        Upper limit on system size (kW-dc), e.g. a typical residential net
        metering limit. None means no limit beyond roof area.
    dc_ac_ratio : float
        DC/AC ratio for PVWatts.
    gcr : float
        Ground coverage ratio for PVWatts.
    array_type : int
        PVWatts array type: 0 = fixed open rack, 1 = fixed roof mount,
        2 = 1-axis tracking, 3 = 1-axis backtracking, 4 = 2-axis tracking.
        Defaults to 1 (rooftop).
    tilt_default_deg : float
        Tilt in degrees used when the building has no pitched roof planes
        in its HPXML (e.g. flat roofs) or the HPXML can't be read.
    """

    sizing_mode: str = "roof"          # "roof", "load", or "min"
    watts_per_ft2: float = 15.0
    target_solar_fraction: float = 0.8
    assumed_cf: Optional[float] = 0.18
    max_system_kw: Optional[float] = None
    dc_ac_ratio: float = 1.2
    gcr: float = 0.4
    array_type: int = 1                # fixed roof mount
    tilt_default_deg: float = 25.0


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


def size_pv_system_kw(
    metadata_row: pd.Series,
    hourly_load_kwh: pd.Series,
    pv_config: PVSimulationConfig,
    yield_kwh_per_kw: Optional[float] = None,
) -> float:
    """
    PV system size (kW-dc) for a building under ``pv_config.sizing_mode``.

    - "roof": roof area × ``watts_per_ft2``
    - "load": enough to cover ``target_solar_fraction`` of the annual load
      in ``hourly_load_kwh``, at ``assumed_cf`` or, if that is None, at the
      building's simulated ``yield_kwh_per_kw``
    - "min" : the smaller of the two

    The result is capped at ``pv_config.max_system_kw`` if set.

    Parameters
    ----------
    metadata_row : pandas.Series
        ResStock metadata for the building (uses ``out.params.roof_area_ft_2``).
    hourly_load_kwh : pandas.Series
        Hourly electric load the system is sized against.
    pv_config : PVSimulationConfig
        PV sizing configuration.
    yield_kwh_per_kw : float, optional
        Simulated annual output per kW-dc. Required when
        ``pv_config.assumed_cf`` is None.

    Returns
    -------
    float
        DC system size in kW.
    """
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
        if pv_config.assumed_cf is not None:
            load_size_kw = _size_pv_from_load(
                hourly_load_kwh=hourly_load_kwh,
                target_solar_fraction=pv_config.target_solar_fraction,
                assumed_cf=pv_config.assumed_cf,
            )
        elif yield_kwh_per_kw is not None and yield_kwh_per_kw > 0:
            target_kwh = float(hourly_load_kwh.sum()) * pv_config.target_solar_fraction
            load_size_kw = target_kwh / yield_kwh_per_kw
        elif yield_kwh_per_kw is None:
            raise ValueError("yield_kwh_per_kw is required when pv_config.assumed_cf is None")

    if sizing_mode == "roof":
        size_kw = roof_size_kw
    elif sizing_mode == "load":
        size_kw = load_size_kw
    elif sizing_mode == "min":
        size_kw = min(roof_size_kw, load_size_kw)
    else:
        raise ValueError(
            f"Invalid sizing_mode '{pv_config.sizing_mode}'. "
            "Use 'roof', 'load', or 'min'."
        )

    if pv_config.max_system_kw is not None:
        size_kw = min(size_kw, pv_config.max_system_kw)
    return size_kw


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


def _solar_resource_for_building(metadata_row: pd.Series, weather_base_prefix: str) -> Dict[str, Any]:
    """
    PVWatts solar resource data for a building's ResStock weather file,
    cached by (state, county) so each county's file is downloaded once.
    """
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

    return solar_resource_data


def simulate_pv_per_kw(
    metadata_row: pd.Series,
    weather_base_prefix: str,
    tilt_deg: float,
    azimuth_deg: float,
    array_type: int = 1,
    dc_ac_ratio: float = 1.2,
    gcr: float = 0.4,
) -> pd.Series:
    """
    Hourly AC output (kWh) of a 1 kW-dc PV array at a building's location,
    from PVWatts with the building's ResStock weather file.

    Parameters
    ----------
    metadata_row : pandas.Series
        ResStock metadata for the building (state, county, weather coordinates).
    weather_base_prefix : str
        Base URL for ResStock TMY3 weather files.
    tilt_deg, azimuth_deg : float
        Array tilt and azimuth (degrees; 180 = south).
    array_type : int, optional
        PVWatts array type (0 = fixed open rack, 1 = fixed roof mount).
        Defaults to 1.
    dc_ac_ratio : float, optional
        DC/AC ratio. Defaults to 1.2.
    gcr : float, optional
        Ground coverage ratio. Defaults to 0.4.

    Returns
    -------
    pandas.Series
        Hourly output per kW-dc (8,760 values, positional index).
    """
    pv = Pvwatts.default("PVWattsSingleOwner")
    pv.SolarResource.solar_resource_data = _solar_resource_for_building(metadata_row, weather_base_prefix)

    pv.SystemDesign.system_capacity = 1.0
    pv.SystemDesign.azimuth = azimuth_deg
    pv.SystemDesign.tilt = tilt_deg
    pv.SystemDesign.dc_ac_ratio = dc_ac_ratio
    pv.SystemDesign.gcr = gcr
    pv.SystemDesign.array_type = array_type

    pv.execute()
    return pd.Series(pv.Outputs.ac) / 1000.0


def compute_pv_for_building(
    metadata_row: pd.Series,
    hourly_load_kwh: pd.Series,
    pv_config: PVSimulationConfig,
    weather_base_prefix: str,
    hpxml_zip_path: Optional[str] = None,
) -> Tuple[pd.Series, float]:
    """
    Size and simulate hourly rooftop PV generation for one building.

    1. Array geometry: the roof plane chosen by
       :func:`select_pv_roof_from_hpxml` if the HPXML can be read and has
       pitched planes. Otherwise ``pv_config.tilt_default_deg`` and the front
       or back of the house (``in.orientation`` or its opposite), whichever
       faces closer to south, as for a gable roof.
    2. Simulate a 1 kW system with :func:`simulate_pv_per_kw`.
    3. Size the system with :func:`size_pv_system_kw` against
       ``hourly_load_kwh`` and scale the 1 kW output to it.

    Parameters
    ----------
    metadata_row : pandas.Series
        ResStock metadata for the building: ``in.state``, ``in.county``,
        ``in.orientation``, ``out.params.roof_area_ft_2``,
        ``in.weather_file_latitude`` and ``in.weather_file_longitude``.
    hourly_load_kwh : pandas.Series
        Hourly electric load (kWh) the system is sized against, indexed by hour.
    pv_config : PVSimulationConfig
        PV sizing and PVWatts settings.
    weather_base_prefix : str
        Base path or URL to the ResStock weather directory.
    hpxml_zip_path : str, optional
        Local path or URL to the building's HPXML zip, used for roof geometry.

    Returns
    -------
    (pv_kwh, system_capacity_kw) : (pandas.Series, float)
        Hourly PV generation (kWh) aligned to ``hourly_load_kwh.index``, and
        the DC system size (kW).
    """
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
                try:
                    hpxml_result = select_pv_roof_from_hpxml(local_zip_path)
                finally:
                    os.remove(local_zip_path)
            else:
                hpxml_result = select_pv_roof_from_hpxml(hpxml_zip_path)
        except Exception:
            hpxml_result = None

    if hpxml_result is not None:
        tilt_deg, azimuth_deg = hpxml_result
    else:
        # No pitched roof planes (ResStock models homes with no attic as flat
        # roofs) or no HPXML: assume a gable roof whose faces point to the front
        # and back of the house, and use whichever face is closer to south.
        tilt_deg = pv_config.tilt_default_deg
        try:
            front = orientation_to_azimuth(metadata_row["in.orientation"])
            azimuth_deg = min(
                (front, (front + 180.0) % 360.0),
                key=lambda az: abs(((az - 180.0) + 180.0) % 360.0 - 180.0),
            )
        except Exception:
            azimuth_deg = 180.0  # south-facing fallback

    # ------------------------------------------------------------------
    # 2. Simulate a 1 kW system. Output scales linearly with capacity for a
    #    fixed geometry and DC/AC ratio, so the simulated output per kW can be
    #    used for sizing and then scaled.
    # ------------------------------------------------------------------
    ac_kw_per_kw = simulate_pv_per_kw(
        metadata_row,
        weather_base_prefix=weather_base_prefix,
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        array_type=pv_config.array_type,
        dc_ac_ratio=pv_config.dc_ac_ratio,
        gcr=pv_config.gcr,
    )

    # ------------------------------------------------------------------
    # 3. Size the system and scale the 1 kW output to it
    # ------------------------------------------------------------------
    system_capacity_kw = size_pv_system_kw(
        metadata_row, hourly_load_kwh, pv_config, yield_kwh_per_kw=float(ac_kw_per_kw.sum())
    )
    ac_kw = ac_kw_per_kw * system_capacity_kw
    n_pv = len(ac_kw)
    n_load = len(hourly_load_kwh)
    load_index = hourly_load_kwh.index

    # ResStock's interval-ending timestamps give 8,761 hourly buckets for 8,760
    # weather hours; the last (partial) hour gets no PV
    if n_load == n_pv + 1:
        load_index = load_index[:n_pv]
    elif n_pv == n_load + 1:
        ac_kw = ac_kw.iloc[:n_load]
    elif n_pv != n_load:
        raise ValueError(f"PVWatts output length {n_pv} does not match load length {n_load}")

    ac_kw.index = load_index

    return ac_kw, system_capacity_kw


def compute_pv_for_buildings(
    hourly_df: pd.DataFrame,
    metadata_df: pd.DataFrame,
    pv_config: PVSimulationConfig,
    weather_base_prefix: str,
    hp_upgrade: str = "2",
    hpxml_upgrade: str = "0",
    max_workers: int = 16,
    metadata_id_col: str = "bldg_id",
) -> pd.DataFrame:
    """
    Run :func:`compute_pv_for_building` for many buildings in parallel.

    Each building needs its own HPXML download, so the work is I/O bound
    and is spread across a thread pool. Buildings that fail are skipped
    and reported rather than aborting the whole run.

    Parameters
    ----------
    hourly_df : pandas.DataFrame
        Long-format hourly profiles from
        :func:`build_building_hourly_profiles` with columns ``bldg_id``,
        ``upgrade``, ``ts_hour`` and ``elec_kwh``.
    metadata_df : pandas.DataFrame
        ResStock metadata with the columns required by
        :func:`compute_pv_for_building`.
    pv_config : PVSimulationConfig
        PV sizing and simulation configuration.
    weather_base_prefix : str
        Base URL for ResStock TMY3 weather files.
    hp_upgrade : str, optional
        Upgrade whose hourly load is used for load-based sizing.
        Defaults to "2".
    hpxml_upgrade : str, optional
        Upgrade whose HPXML is used for roof geometry. Defaults to "0".
    max_workers : int, optional
        Number of worker threads. Defaults to 16.
    metadata_id_col : str, optional
        Column in metadata_df corresponding to bldg_id.

    Returns
    -------
    pandas.DataFrame
        Long-format PV output with columns ``bldg_id``, ``ts_hour``,
        ``pv_kwh`` and ``system_kw``.
    """
    hourly_hp = hourly_df[hourly_df["upgrade"] == hp_upgrade]
    meta_by_id = metadata_df.set_index(metadata_df[metadata_id_col].astype(str))

    def run_one(bldg_id: str, grp: pd.DataFrame) -> pd.DataFrame:
        hourly_load = grp.sort_values("ts_hour").set_index("ts_hour")["elec_kwh"]
        pv_kwh, system_kw = compute_pv_for_building(
            metadata_row=meta_by_id.loc[bldg_id],
            hourly_load_kwh=hourly_load,
            pv_config=pv_config,
            weather_base_prefix=weather_base_prefix,
            hpxml_zip_path=build_hpxml_zip_path(bldg_id, upgrade=hpxml_upgrade),
        )
        out = pv_kwh.to_frame("pv_kwh").reset_index()
        out["bldg_id"] = bldg_id
        out["system_kw"] = system_kw
        return out

    groups = list(hourly_hp.groupby(hourly_hp["bldg_id"].astype(str)))
    records: List[pd.DataFrame] = []
    failures: List[Tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(run_one, bid, grp): bid for bid, grp in groups}
        for i, fut in enumerate(as_completed(futures), start=1):
            bid = futures[fut]
            try:
                records.append(fut.result())
            except Exception as e:
                failures.append((bid, repr(e)))
            if i % 250 == 0 or i == len(futures):
                print(f"PV: {i}/{len(futures)} buildings done ({len(failures)} failed)")

    if failures:
        print(f"PV failed for {len(failures)} buildings, e.g. {failures[:3]}")

    return pd.concat(records, ignore_index=True)


def county_fips_from_resstock(county: str) -> str:
    """
    Convert a ResStock ``in.county`` GISJOIN code (e.g. "G1700310") to a
    5-digit county FIPS string (e.g. "17031").
    """
    county = str(county)
    return county[1:3] + county[4:7]


def load_wholesale_export_rates(path: str) -> pd.DataFrame:
    """
    Load hourly wholesale export rates by county from the dGen export file.

    Parameters
    ----------
    path : str
        Path to ``dgen_export_rates_hourly_by_county.csv(.gz)``: 8,760 rows,
        column ``hour_of_year`` (1–8760) plus one column per 5-digit county
        FIPS, values in $/kWh.

    Returns
    -------
    pandas.DataFrame
        8,760 rows (hour of year, in order) × county FIPS columns (strings).
    """
    rates = pd.read_csv(path).set_index("hour_of_year").sort_index()
    rates.columns = rates.columns.astype(str).str.zfill(5)
    return rates.astype("float32")


def simulate_battery(
    load_kwh: np.ndarray,
    pv_kwh: np.ndarray,
    capacity_kwh: float,
    power_kw: float,
    round_trip_efficiency: float = 0.90,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Hourly self-consumption battery dispatch for many buildings at once.

    Each hour, surplus solar charges the battery (up to its power and free
    capacity) and the rest is exported; when load exceeds solar, the battery
    discharges (up to its power and stored energy) and the rest is imported.
    The battery never charges from the grid. Losses are split evenly between
    charging and discharging. The battery starts empty.

    Parameters
    ----------
    load_kwh, pv_kwh : numpy.ndarray
        Arrays of shape (n_buildings, n_hours).
    capacity_kwh : float
        Usable storage capacity (kWh).
    power_kw : float
        Maximum charge/discharge power (kW).
    round_trip_efficiency : float, optional
        Fraction of stored energy recovered. Defaults to 0.90.

    Returns
    -------
    (import_kwh, export_kwh) : tuple of numpy.ndarray
        Hourly grid imports and exports, each (n_buildings, n_hours).
    """
    eta = np.sqrt(round_trip_efficiency)
    n, n_hours = load_kwh.shape
    soc = np.zeros(n)
    imports = np.empty_like(load_kwh)
    exports = np.empty_like(load_kwh)

    for t in range(n_hours):
        net = pv_kwh[:, t] - load_kwh[:, t]
        surplus = np.maximum(net, 0.0)
        deficit = np.maximum(-net, 0.0)

        charge = np.minimum(np.minimum(surplus, power_kw), (capacity_kwh - soc) / eta)
        soc += charge * eta
        discharge = np.minimum(np.minimum(deficit, power_kw), soc * eta)
        soc -= discharge / eta

        exports[:, t] = surplus - charge
        imports[:, t] = deficit - discharge

    return imports, exports


def compute_annual_flows(
    hourly_df: pd.DataFrame,
    pv_df: pd.DataFrame,
    metadata_df: pd.DataFrame,
    pv_config: PVSimulationConfig,
    wholesale_rates: pd.DataFrame,
    battery_kwh: float = 13.5,
    battery_kw: float = 5.0,
    round_trip_efficiency: float = 0.90,
    baseline_upgrade: str = "0",
    hp_upgrade: str = "2",
    metadata_id_col: str = "bldg_id",
) -> pd.DataFrame:
    """
    Annual energy use, grid imports, exports and wholesale export value for each building
    under four setups: with and without a heat pump, each with and without a
    battery.

    Solar is sized to each setup's own load (:func:`size_pv_system_kw`):
    ``pv_df`` holds the system sized to the heat pump load, and the smaller
    system a home would install without a heat pump is the same simulated
    profile scaled to its size (PV output scales linearly with capacity for a
    fixed array geometry). Flows are netted hourly; with a battery,
    :func:`simulate_battery` is applied first.

    Because tariffs here are flat volumetric rates, a setup's annual bill
    depends only on its annual imports and exports (valued at a flat export
    rate) or, under hourly wholesale export pricing, on the sum of hourly
    exports × the county's hourly wholesale rate.

    Parameters
    ----------
    hourly_df : pandas.DataFrame
        Long-format hourly profiles with columns ``bldg_id``, ``upgrade``,
        ``ts_hour`` and ``elec_kwh``.
    pv_df : pandas.DataFrame
        Output of :func:`compute_pv_for_buildings`.
    metadata_df : pandas.DataFrame
        ResStock metadata (roof area for sizing, ``in.county`` for wholesale rates).
    pv_config : PVSimulationConfig
        PV sizing configuration.
    wholesale_rates : pandas.DataFrame
        Output of :func:`load_wholesale_export_rates`.
    battery_kwh, battery_kw : float, optional
        Battery usable capacity (kWh) and power (kW). Defaults to 13.5 and 5.
    round_trip_efficiency : float, optional
        Battery round-trip efficiency. Defaults to 0.90.
    baseline_upgrade, hp_upgrade : str, optional
        Upgrade labels. Defaults to "0" and "2".
    metadata_id_col : str, optional
        Column in metadata_df corresponding to bldg_id.

    Returns
    -------
    pandas.DataFrame
        One row per building with:
            - ``elec_base_kwh``, ``elec_hp_kwh``: annual electricity use
              without and with the heat pump
            - ``gas_base_kwh``, ``gas_hp_kwh``: annual gas use (kWh)
            - ``system_kw``, ``system_kw_solar_only``: PV size with and
              without the heat pump
            - ``pv_kwh_per_kw``: annual simulated output per kW-dc
            - for each setup ``{hp, no_hp}_{no_batt, batt}``:
              ``<setup>_import_kwh``, ``<setup>_export_kwh`` and
              ``<setup>_export_wholesale_usd``
        Buildings whose county has no wholesale series get NaN wholesale values.
    """
    def load_matrix(upgrade: str, column: str = "elec_kwh") -> pd.DataFrame:
        df = hourly_df[hourly_df["upgrade"] == upgrade]
        mat = df.pivot_table(index=df["bldg_id"].astype(str), columns="ts_hour", values=column, aggfunc="sum")
        return mat.sort_index(axis=1)

    pv = pv_df.copy()
    pv["bldg_id"] = pv["bldg_id"].astype(str)
    ids = sorted(pv["bldg_id"].unique())

    base_all = load_matrix(baseline_upgrade).loc[ids]
    hp_all = load_matrix(hp_upgrade).loc[ids]
    hours = base_all.columns[:8760]
    # ResStock's interval-ending timestamps leave one partial hour at the end of the
    # year; it has no PV, so it's imported in every setup
    base_extra = base_all.iloc[:, 8760:].sum(axis=1).to_numpy()
    hp_extra = hp_all.iloc[:, 8760:].sum(axis=1).to_numpy()
    base = base_all[hours].to_numpy()
    hp = hp_all[hours].to_numpy()

    pv_mat = pv.pivot_table(index="bldg_id", columns="ts_hour", values="pv_kwh", aggfunc="sum").loc[ids]
    pv_mat = pv_mat.reindex(columns=pd.to_datetime(hours)).fillna(0.0).to_numpy()
    system_kw = pv.groupby("bldg_id")["system_kw"].first().loc[ids].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        pv_per_kw = np.where(system_kw[:, None] > 0, pv_mat / system_kw[:, None], 0.0)
    yield_kwh_per_kw = pv_per_kw.sum(axis=1)

    meta_by_id = metadata_df.set_index(metadata_df[metadata_id_col].astype(str)).loc[ids]
    system_kw_solar_only = np.array([
        size_pv_system_kw(
            meta_by_id.loc[bid], pd.Series(base[i]), pv_config,
            yield_kwh_per_kw=float(yield_kwh_per_kw[i]) if system_kw[i] > 0 else None,
        ) if system_kw[i] > 0 else 0.0
        for i, bid in enumerate(ids)
    ])

    fips = meta_by_id["in.county"].map(county_fips_from_resstock)
    price = np.full(base.shape, np.nan, dtype="float32")
    has_rates = fips.isin(wholesale_rates.columns).to_numpy()
    if has_rates.any():
        price[has_rates] = wholesale_rates[fips[has_rates].tolist()].to_numpy().T

    out = pd.DataFrame({
        "bldg_id": ids,
        "elec_base_kwh": base_all.sum(axis=1).to_numpy(),
        "elec_hp_kwh": hp_all.sum(axis=1).to_numpy(),
        "gas_base_kwh": load_matrix(baseline_upgrade, "gas_kwh").loc[ids].sum(axis=1).to_numpy(),
        "gas_hp_kwh": load_matrix(hp_upgrade, "gas_kwh").loc[ids].sum(axis=1).to_numpy(),
        "system_kw": system_kw,
        "system_kw_solar_only": system_kw_solar_only,
        "pv_kwh_per_kw": yield_kwh_per_kw,
    })
    setups = {
        "hp": (hp, pv_per_kw * system_kw[:, None], hp_extra),
        "no_hp": (base, pv_per_kw * system_kw_solar_only[:, None], base_extra),
    }
    for name, (load, solar, extra) in setups.items():
        for batt in ("no_batt", "batt"):
            if batt == "no_batt":
                imports = np.maximum(load - solar, 0.0)
                exports = np.maximum(solar - load, 0.0)
            else:
                imports, exports = simulate_battery(load, solar, battery_kwh, battery_kw, round_trip_efficiency)
            out[f"{name}_{batt}_import_kwh"] = imports.sum(axis=1) + extra
            out[f"{name}_{batt}_export_kwh"] = exports.sum(axis=1)
            out[f"{name}_{batt}_export_wholesale_usd"] = np.where(
                has_rates, (exports * np.nan_to_num(price)).sum(axis=1), np.nan
            )

    return out


def compute_plugin_solar(
    hourly_df: pd.DataFrame,
    metadata_df: pd.DataFrame,
    weather_base_prefix: str,
    sizes_kw: Sequence[float] = (0.391, 1.2),
    tilt_deg: float = 20.0,
    azimuth_deg: float = 180.0,
    array_type: int = 0,
    dc_ac_ratio: float = 1.2,
    upgrade: str = "2",
    max_workers: int = 16,
    metadata_id_col: str = "bldg_id",
) -> pd.DataFrame:
    """
    Annual production and self-consumption of small plug-in solar systems with
    no export compensation.

    Each building gets a south-facing array at a fixed tilt (PVWatts defaults:
    20°, fixed open rack), as plug-in panels typically sit on a balcony,
    railing or yard rather than the roof. Each hour the home uses what it can
    of the output; any surplus is lost.

    Parameters
    ----------
    hourly_df : pandas.DataFrame
        Long-format hourly profiles with columns ``bldg_id``, ``upgrade``,
        ``ts_hour`` and ``elec_kwh``.
    metadata_df : pandas.DataFrame
        ResStock metadata (state, county, weather coordinates).
    weather_base_prefix : str
        Base URL for ResStock TMY3 weather files.
    sizes_kw : sequence of float, optional
        Plug-in system sizes in kW-dc. Defaults to (0.391, 1.2).
    tilt_deg, azimuth_deg : float, optional
        Array tilt and azimuth. Defaults to 20° and 180° (south).
    array_type : int, optional
        PVWatts array type. Defaults to 0 (fixed open rack).
    dc_ac_ratio : float, optional
        DC/AC ratio. Defaults to 1.2.
    upgrade : str, optional
        Upgrade whose hourly load the system offsets. Defaults to "2".
    max_workers : int, optional
        Number of worker threads. Defaults to 16.
    metadata_id_col : str, optional
        Column in metadata_df corresponding to bldg_id.

    Returns
    -------
    pandas.DataFrame
        One row per building with ``bldg_id`` and, for each size (in watts,
        e.g. ``plugin_391w``), ``<size>_production_kwh`` and
        ``<size>_used_kwh``.
    """
    load = hourly_df[hourly_df["upgrade"] == upgrade]
    load = load.pivot_table(index=load["bldg_id"].astype(str), columns="ts_hour", values="elec_kwh", aggfunc="sum")
    load = load.sort_index(axis=1).iloc[:, :8760]
    meta_by_id = metadata_df.set_index(metadata_df[metadata_id_col].astype(str))

    def run_one(bid: str) -> Dict[str, float]:
        per_kw = simulate_pv_per_kw(
            meta_by_id.loc[bid], weather_base_prefix, tilt_deg, azimuth_deg, array_type, dc_ac_ratio
        ).to_numpy()[:8760]
        home = load.loc[bid].to_numpy()
        row = {"bldg_id": bid}
        for size in sizes_kw:
            pv = per_kw * size
            name = f"plugin_{round(size * 1000)}w"
            row[f"{name}_production_kwh"] = float(pv.sum())
            row[f"{name}_used_kwh"] = float(np.minimum(pv, home).sum())
        return row

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        rows = list(ex.map(run_one, load.index))
    return pd.DataFrame(rows)


# =============================================================================
# Solar costs
# =============================================================================


def _financed_present_value(
    capex: float,
    debt_fraction: float,
    interest_rate: float,
    discount_rate: float,
    inflation_rate: float,
    loan_term_years: int,
    lifetime_years: int,
    om_per_year: float = 0.0,
) -> float:
    """
    Present value (today's dollars) of everything a homeowner pays for
    equipment: the down payment, the loan payments and any O&M.

    The down payment (the non-financed share) is paid up front. The loan is a
    fixed nominal payment over ``loan_term_years``. O&M is set in today's
    dollars and rises with inflation. Payments are discounted at the nominal
    discount rate.
    """
    years = np.arange(1, lifetime_years + 1)
    loan_years = np.arange(1, loan_term_years + 1)

    loan = capex * debt_fraction
    payment = loan * interest_rate / (1 - (1 + interest_rate) ** -loan_term_years)

    return float(
        capex * (1 - debt_fraction)
        + np.sum(payment / (1 + discount_rate) ** loan_years)
        + np.sum(om_per_year * (1 + inflation_rate) ** years / (1 + discount_rate) ** years)
    )


def financed_annual_cost(
    capex: float,
    debt_fraction: float = 0.70,
    interest_rate: float = 0.07,
    discount_rate: float = 0.05,
    inflation_rate: float = 0.025,
    loan_term_years: int = 20,
    lifetime_years: int = 25,
    om_per_year: float = 0.0,
) -> float:
    """
    Yearly cost ($/yr, today's dollars) of financed equipment, for comparing
    against one year of energy bills.

    The homeowner's actual payments are lumpy: a down payment, then fixed
    loan payments, then nothing once the loan is paid off. This spreads them
    into an equal amount per year, in today's dollars, over the equipment's
    lifetime, with the same present value as the actual payments.

    Parameters
    ----------
    capex : float
        Installed cost ($).
    debt_fraction : float, optional
        Share of the installed cost financed with a loan. Defaults to 0.70.
    interest_rate : float, optional
        Nominal loan interest rate. Defaults to 0.07.
    discount_rate : float, optional
        Nominal discount rate. Defaults to 0.05.
    inflation_rate : float, optional
        Annual inflation rate. Defaults to 0.025.
    loan_term_years : int, optional
        Loan term in years. Defaults to 20.
    lifetime_years : int, optional
        Equipment lifetime in years. Defaults to 25.
    om_per_year : float, optional
        Fixed O&M in today's dollars ($/yr). Defaults to 0.

    Returns
    -------
    float
        Yearly cost in $/yr.
    """
    pv_costs = _financed_present_value(
        capex, debt_fraction, interest_rate, discount_rate,
        inflation_rate, loan_term_years, lifetime_years, om_per_year,
    )
    real_rate = (1 + discount_rate) / (1 + inflation_rate) - 1
    crf = real_rate / (1 - (1 + real_rate) ** -lifetime_years)
    return pv_costs * crf


def solar_annual_cost_per_kw(
    cost_per_w: float,
    debt_fraction: float = 0.70,
    interest_rate: float = 0.07,
    discount_rate: float = 0.05,
    inflation_rate: float = 0.025,
    loan_term_years: int = 20,
    lifetime_years: int = 25,
    om_per_kw_year: float = 0.0,
) -> float:
    """
    Yearly cost of a debt-financed residential PV system ($/kW-dc per year,
    today's dollars), for comparing against one year of energy bills.

    The homeowner's actual payments are lumpy: a down payment, then fixed
    loan payments, then nothing once the loan is paid off. This spreads them
    into an equal amount per year, in today's dollars, over the system's
    lifetime, with the same present value as the actual payments.

    For example, at $3.62/W with 70% financed at 7% over 20 years, a 1 kW
    system costs $3,620: $1,086 down plus ~$239/yr for 20 years. Spread over
    25 years in today's dollars that is ~$219/kW-yr.

    Parameters
    ----------
    cost_per_w : float
        Installed cost ($/W-dc).
    debt_fraction : float, optional
        Share of the installed cost financed with a loan. Defaults to 0.70.
    interest_rate : float, optional
        Nominal loan interest rate. Defaults to 0.07.
    discount_rate : float, optional
        Nominal discount rate. Defaults to 0.05.
    inflation_rate : float, optional
        Annual inflation rate. Defaults to 0.025.
    loan_term_years : int, optional
        Loan term in years. Defaults to 20.
    lifetime_years : int, optional
        System economic lifetime in years. Defaults to 25.
    om_per_kw_year : float, optional
        Fixed O&M in today's dollars ($/kW-yr). Defaults to 0.

    Returns
    -------
    float
        Yearly cost in $/kW-dc per year.
    """
    return financed_annual_cost(
        cost_per_w * 1000.0, debt_fraction, interest_rate, discount_rate,
        inflation_rate, loan_term_years, lifetime_years, om_per_kw_year,
    )
