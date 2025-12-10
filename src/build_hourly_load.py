import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List, Optional, Sequence

import duckdb
import pandas as pd


def build_city_median_hourly_load(
    bldg_ids: Sequence[str],
    metadata_df: pd.DataFrame,
    upgrades: Sequence[str] = ("0",),
    state: str = "NJ",
    bucket: str = "oedi-data-lake",
    base_prefix: str = (
        "nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/"
        "resstock_tmy3_release_2/timeseries_individual_buildings/by_state"
    ),
    region: str = "us-west-2",
    chunk_size: int = 1000,
    max_workers: int = 8,
    output_dir: str = "nj_hourly_profiles_by_city",
    metadata_id_col: str = "bldg_id",
) -> List[str]:
    """
    Build median hourly electricity load profiles by city and write one CSV per city.

    This function reads building-level ResStock timeseries data from object
    storage using DuckDB and HTTPFS, aggregates consumption to the hourly level
    per (building, upgrade), joins to an in-memory metadata DataFrame to obtain
    city information, computes the median hourly load by city, and writes one
    CSV file per city.

    Parameters
    ----------
    bldg_ids : Sequence[str]
        Iterable of building ID strings to process. These IDs are expected to
        match the building identifier column in the metadata DataFrame and the
        filename prefix in the ResStock timeseries files (e.g., for a file
        ``100104-0.parquet``, the building ID is ``"100104"``).
    metadata_df : pandas.DataFrame
        Metadata DataFrame containing at least:
          - a building identifier column (see ``metadata_id_col``),
          - a city column named ``"in.city"``.
        The DataFrame is used instead of loading metadata from object storage.
    upgrades : Sequence[str], optional
        ResStock upgrade scenario identifiers (e.g., ``["0"]`` or ``["0", "7"]``).
    state : str, optional
        Two-letter state code, used only to construct the timeseries S3 paths.
    bucket : str, optional
        Name of the object storage bucket that contains the ResStock timeseries
        data (e.g., ``"oedi-data-lake"``).
    base_prefix : str, optional
        Path prefix within the bucket for the timeseries individual buildings
        by-state dataset. Timeseries files are expected at paths of the form::

            s3://{bucket}/{base_prefix}/upgrade={upgrade}/state={state}/{bldg_id}-{upgrade}.parquet
    region : str, optional
        Region in which the object storage bucket is located (e.g., ``"us-west-2"``).
        Used to configure DuckDB HTTPFS.
    chunk_size : int, optional
        Number of building IDs to process per worker when reading timeseries.
        Larger values can be more efficient but require more memory per worker.
    max_workers : int, optional
        Maximum number of worker threads to use when processing building
        chunks in parallel.
    output_dir : str, optional
        Directory to which the per-city CSV files will be written. The
        directory is created if it does not exist.
    metadata_id_col : str, optional
        Column name in ``metadata_df`` that contains the building identifier
        corresponding to ``bldg_ids`` and the filename prefix in the
        timeseries files (e.g., ``"building_id"``, ``"bldg_id"``, etc.).

    Returns
    -------
    List[str]
        List of filesystem paths to the per-city CSV files written under
        ``output_dir``. Each CSV contains columns:

        - ``city`` : str
        - ``upgrade`` : str
        - ``ts_hour`` : timestamp
        - ``median_kwh`` : float

    Notes
    -----
    - The ResStock timeseries Parquet files are expected to include:
        * ``timestamp`` (datetime-like),
        * ``"out.electricity.total.energy_consumption"`` (energy in kWh).
    - Building IDs are parsed from filenames by stripping the ``-<upgrade>``
      suffix; for example, ``100104-0.parquet`` yields a building ID of
      ``"100104"``.
    """

    if metadata_df is None:
        raise ValueError("metadata_df must be provided and cannot be None.")

    # Normalize upgrades: allow a single string or any sequence of strings.
    if isinstance(upgrades, str):
        upgrades_seq = (upgrades,)
    else:
        upgrades_seq = tuple(upgrades)

    # Ensure building IDs are strings for consistent matching.
    bldg_ids_str = [str(bid) for bid in bldg_ids]

    # Install HTTPFS extension once in the current process.
    duckdb.sql("INSTALL httpfs;")

    def process_chunk(ids_chunk: Sequence[str], out_file: str) -> str:
        """
        Process a subset of building IDs into an hourly parquet part file.

        For each building ID and upgrade in the chunk, this helper:
          - Constructs the S3 path to the ResStock timeseries file.
          - Reads the Parquet files using DuckDB and HTTPFS.
          - Aggregates electricity consumption to hourly kWh.
          - Writes the hourly subset to a local Parquet file.

        Parameters
        ----------
        ids_chunk : Sequence[str]
            Subset of building IDs to process in this worker.
        out_file : str
            Local path to write the Parquet part file.

        Returns
        -------
        str
            Path to the written Parquet part file.
        """
        con = duckdb.connect(database=":memory:")
        con.execute("LOAD httpfs;")
        con.execute(f"SET s3_region='{region}';")
        con.execute("SET s3_use_ssl=true;")
        con.execute("SET s3_url_style='path';")

        # Build S3 paths for each (building, upgrade) combination.
        paths = [
            f"s3://{bucket}/{base_prefix}/upgrade={u}/state={state}/{bid}-{u}.parquet"
            for u in upgrades_seq
            for bid in ids_chunk
        ]

        # Aggregate electricity consumption to the hourly level.
        # Building ID is parsed by stripping the "-<upgrade>" suffix from the filename.
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
              SUM("out.electricity.total.energy_consumption") AS kwh
            FROM read_parquet(?, filename = TRUE)
            GROUP BY 1, 2, 3
            """,
            parameters=[paths],
        )

        con.execute(
            "COPY hourly_part TO ? (FORMAT PARQUET)",
            parameters=[out_file],
        )
        con.close()
        return out_file

    # ------------------------------------------------------------------
    # Step 1: Process building IDs in parallel to create hourly part files.
    # ------------------------------------------------------------------
    part_files: List[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for start_idx in range(0, len(bldg_ids_str), chunk_size):
            chunk = bldg_ids_str[start_idx : start_idx + chunk_size]
            part_path = f"hourly_bldg_part_{start_idx:06d}.parquet"
            futures.append(executor.submit(process_chunk, chunk, part_path))

        for future in as_completed(futures):
            part_files.append(future.result())

    # ------------------------------------------------------------------
    # Step 2: Merge all part files into a single hourly table.
    # ------------------------------------------------------------------
    con = duckdb.connect(database=":memory:")

    con.execute(
        """
        CREATE OR REPLACE TABLE hourly AS
        SELECT * FROM read_parquet(?, filename = FALSE)
        """,
        parameters=[part_files],
    )

    # ------------------------------------------------------------------
    # Step 3: Prepare metadata and register in DuckDB.
    # ------------------------------------------------------------------
    if metadata_id_col not in metadata_df.columns:
        raise KeyError(
            f"metadata_id_col '{metadata_id_col}' not found in metadata_df columns."
        )
    if "in.city" not in metadata_df.columns:
        raise KeyError('metadata_df must contain a column named "in.city".')

    # Select and normalize the relevant metadata columns.
    meta_subset = (
        metadata_df[[metadata_id_col, "in.city"]]
        .copy()
        .rename(
            columns={
                metadata_id_col: "bldg_id",
                "in.city": "city",
            }
        )
    )
    meta_subset["bldg_id"] = meta_subset["bldg_id"].astype(str)

    # Optional: filter metadata to only building IDs we actually processed.
    meta_subset = meta_subset[
        meta_subset["bldg_id"].isin(
            pd.Series(bldg_ids_str, dtype=str).unique()
        )
    ]

    con.register("meta_df", meta_subset)
    con.execute("CREATE OR REPLACE TABLE meta AS SELECT * FROM meta_df")

    # ------------------------------------------------------------------
    # Step 4: Aggregate to median hourly kWh by (city, hour).
    # ------------------------------------------------------------------
    con.execute(
    """
    CREATE OR REPLACE TABLE city_hourly AS
    SELECT
      m.city,
      h.ts_hour,
      MEDIAN(h.kwh) AS median_kwh
    FROM hourly AS h
    JOIN meta AS m
      ON h.bldg_id = m.bldg_id
    WHERE year(h.ts_hour) <> 2019
    GROUP BY 1, 2
    """
    )

    # ------------------------------------------------------------------
    # Step 5: Write one CSV per city under output_dir.
    # ------------------------------------------------------------------
    os.makedirs(output_dir, exist_ok=True)

    cities_df = con.execute(
        "SELECT DISTINCT city FROM city_hourly ORDER BY city"
    ).df()

    csv_paths: List[str] = []

    for city in cities_df["city"].dropna().tolist():
        # Sanitize city name for filesystem usage.
        safe_city = re.sub(r"[^A-Za-z0-9_-]+", "_", str(city).strip())
        if not safe_city:
            safe_city = "unknown_city"

        city_df = con.execute(
            """
            SELECT
              city,
              ts_hour,
              median_kwh
            FROM city_hourly
            WHERE city = ?
            ORDER BY ts_hour
            """,
            parameters=[city],
        ).df()

        csv_path = os.path.join(output_dir, f"{safe_city}.csv")
        city_df.to_csv(csv_path, index=False)
        csv_paths.append(csv_path)

    # ------------------------------------------------------------------
    # Step 6: Clean up intermediate part files and close DuckDB.
    # ------------------------------------------------------------------
    con.close()

    for path in part_files:
        try:
            os.remove(path)
        except FileNotFoundError:
            continue

    return csv_paths
