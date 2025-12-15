"""
natgas_capacity_equivalent.py

Utilities for estimating state-level and national natural gas generation
displacement and equivalent gas capacity from scenario-based hourly load
outputs and natural gas generation shares.

Overview
--------
The workflow assumes:

1. State-level hourly load/net-load outputs for two scenarios:
   - A baseline scenario (e.g., current rooftop solar costs).
   - A policy scenario (e.g., cheaper rooftop solar).

   For each state and scenario, the data are stored in CSV files with the
   following path template:

       /Volumes/Seagate Portabl/permit_power/dgen_runs/per_state_outputs/
           {state_abbr}/run_all_states_net_savings_adjust_loan_params/
           {scenario}_state_hourly.csv

   The file schema is:

       scenario       : str
       schema         : str
       state_abbr     : str (two-letter uppercase abbreviation, e.g. "MN")
       year           : int (e.g. 2040)
       n_hours        : int
       net_sum_text   : str, serialized as "{v1,v2,...,v8760}" in MW

   There is one row per (state_abbr, year, scenario, schema) combination,
   with net_sum_text containing the 8760 hourly values.

2. A state-level natural gas generation share CSV:

       natural_gas_generation_share_state.csv

   The schema is:

       year           : int (e.g., 2024)
       state_abbr     : str (two-letter abbreviation, e.g., "MN")
       energy_source  : str, one of {"Total", "Natural Gas"}
       generation_mwh : int or float

   This file may contain multiple rows per (state_abbr, energy_source),
   which are aggregated by summing generation_mwh.

High-level method
-----------------
1. For each state and target year:
   - Load the baseline and policy hourly series.
   - Compute the hourly difference: delta = baseline - policy (MW).
   - Optionally clip negative deltas to zero.
   - Sum to get total annual delta MWh.

2. Determine the fraction of the delta attributable to natural gas using one
   of two modes:

   - "state_eia":
       Use state-level natural gas shares from the EIA-like CSV:

           gas_share_state = gas_mwh_state / total_mwh_state

   - "fixed_fraction":
       Use a fixed fraction for all states:

           gas_share_state = fixed_gas_share   (e.g., 0.75)

   The displaced gas MWh for a state is then:

       gas_mwh_displaced = delta_mwh_total * gas_share_state

3. Aggregate across states to obtain national totals of:
   - Total delta MWh (all fuels).
   - Gas MWh displaced.

4. Convert national gas MWh displaced to an equivalent natural gas capacity
   assuming a capacity factor CF:

       gas_capacity_equiv_mw = gas_mwh_displaced / (CF * 8760)
       gas_capacity_equiv_gw = gas_capacity_equiv_mw / 1000

The main entry point is `compute_natgas_capacity_equivalent`, which returns
a state-level results DataFrame and a national summary Series.
"""

import os
from typing import Dict, Iterable, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib.pyplot as plt

# Default path template for state-hourly CSVs.
DEFAULT_STATE_HOURLY_PATH_TEMPLATE = (
    "/Volumes/Seagate Portabl/permit_power/dgen_runs/per_state_outputs/"
    "{state_abbr}/run_all_states_net_savings_adjust_loan_params/"
    "{scenario}_state_hourly.csv"
)


def _parse_net_sum_text(net_sum_text: str) -> np.ndarray:
    """
    Parse a net_sum_text field into an array of hourly values.

    Parameters
    ----------
    net_sum_text : str
        String representation of an array of hourly values, with a format
        like "{1532.3239,1265.1080,...}". Values are assumed to be MW and
        separated by commas.

    Returns
    -------
    np.ndarray
        One-dimensional array of floats representing the hourly values. The
        array is typically of length 8760 for full-year data. If the input
        is empty or NaN, an empty array is returned.
    """
    if pd.isna(net_sum_text):
        return np.array([], dtype=float)

    s = str(net_sum_text).strip()
    if s.startswith("{"):
        s = s[1:]
    if s.endswith("}"):
        s = s[:-1]
    s = s.strip()
    if not s:
        return np.array([], dtype=float)

    parts = s.split(",")
    return np.array([float(p) for p in parts], dtype=float)


def _load_state_hourly_series(
    state_abbr: str,
    scenario: str,
    year: int,
    path_template: str = DEFAULT_STATE_HOURLY_PATH_TEMPLATE,
) -> np.ndarray:
    """
    Load the hourly series for a given state, scenario, and year.

    Parameters
    ----------
    state_abbr : str
        Two-letter uppercase state abbreviation (e.g., "MN").
    scenario : str
        Scenario name used in the state-hourly file naming convention
        (e.g., "baseline" or "policy").
    year : int
        Target year for which the hourly data should be extracted
        (e.g., 2040).
    path_template : str, optional
        Format string defining the path to the state-hourly CSV. The
        template must include the placeholders {state_abbr} and {scenario}.
        Defaults to DEFAULT_STATE_HOURLY_PATH_TEMPLATE.

    Returns
    -------
    np.ndarray
        One-dimensional numpy array of hourly values (MW) for the given
        (state, scenario, year). The array length is typically 8760.

    Raises
    ------
    FileNotFoundError
        If the state-hourly CSV file does not exist at the formatted path.
    ValueError
        If no rows are found for the requested year, or if the parsed
        net_sum_text is empty.
    """
    csv_path = path_template.format(state_abbr=state_abbr, scenario=scenario)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"Hourly CSV not found for state_abbr={state_abbr}, "
            f"scenario={scenario}: {csv_path}"
        )

    df = pd.read_csv(csv_path)

    df_year = df[df["year"] == year]
    if df_year.empty:
        raise ValueError(
            f"No rows found for state_abbr={state_abbr}, "
            f"scenario={scenario}, year={year} in {csv_path}"
        )

    # If multiple rows exist for the same (state, scenario, year), select
    # the first row. This can be adapted to aggregate rows if needed.
    if len(df_year) > 1:
        df_year = df_year.iloc[[0]]

    net_sum_text = df_year["net_sum_text"].iloc[0]
    series = _parse_net_sum_text(net_sum_text)

    if series.size == 0:
        raise ValueError(
            f"Parsed empty net_sum_text array for state_abbr={state_abbr}, "
            f"scenario={scenario}, year={year}"
        )

    return series


def _load_state_gas_share(
    gas_share_csv_path: str,
    share_year: int = 2024,
) -> Dict[str, float]:
    """
    Load natural gas generation share by state for a specified year.

    Parameters
    ----------
    gas_share_csv_path : str
        Path to the CSV containing state-level generation data by energy
        source. The expected schema is:

            year           : int
            state_abbr     : str (two-letter abbreviation)
            energy_source  : str, one of {"Total", "Natural Gas"}
            generation_mwh : numeric (int or float)

        Multiple rows per (state_abbr, energy_source) are allowed and will
        be aggregated by summing generation_mwh.

    share_year : int, optional
        Year to use when computing the natural gas share (default is 2024).

    Returns
    -------
    Dict[str, float]
        Dictionary mapping state_abbr (e.g., "MN") to natural gas generation
        share (a float between 0 and 1). The share is computed as:

            gas_share = generation_mwh("Natural Gas") / generation_mwh("Total")

        If either gas or total generation is missing or zero for a state,
        the share is set to 0.0.

    Raises
    ------
    ValueError
        If no records are found for the specified share_year, or if required
        energy_source categories are missing.
    """
    df = pd.read_csv(gas_share_csv_path)

    df = df[df["year"] == share_year].copy()
    if df.empty:
        raise ValueError(
            f"No records found in {gas_share_csv_path} for year={share_year}"
        )

    # Ensure generation_mwh is numeric.
    df["generation_mwh"] = pd.to_numeric(df["generation_mwh"], errors="coerce")

    # Aggregate generation by (state_abbr, energy_source).
    grouped = (
        df.groupby(["state_abbr", "energy_source"], as_index=False)["generation_mwh"]
        .sum()
    )

    # Pivot to wide format: index = state_abbr, columns = energy_source.
    pivot = grouped.pivot(
        index="state_abbr",
        columns="energy_source",
        values="generation_mwh",
    ).fillna(0.0)

    # Ensure required columns exist.
    if "Total" not in pivot.columns:
        raise ValueError(
            "Expected 'Total' in energy_source column but it was not found."
        )
    if "Natural Gas" not in pivot.columns:
        raise ValueError(
            "Expected 'Natural Gas' in energy_source column but it was not found."
        )

    total = pivot["Total"]
    gas = pivot["Natural Gas"]

    share_dict: Dict[str, float] = {}
    for state_abbr, total_val in total.items():
        gas_val = gas.loc[state_abbr]
        if (
            pd.isna(total_val)
            or total_val <= 0.0
            or pd.isna(gas_val)
            or gas_val < 0.0
        ):
            share = 0.0
        else:
            share = float(gas_val / total_val)
        share_dict[state_abbr] = share

    return share_dict


def compute_natgas_capacity_equivalent(
    states: Iterable[str],
    gas_share_csv_path: Optional[str] = None,
    target_year: int = 2040,
    gas_share_year: int = 2024,
    capacity_factor: float = 0.80,
    baseline_scenario: str = "baseline",
    policy_scenario: str = "policy",
    state_hourly_path_template: str = DEFAULT_STATE_HOURLY_PATH_TEMPLATE,
    clip_negative_deltas: bool = True,
    gas_share_mode: str = "state_eia",
    fixed_gas_share: float = 0.75,
) -> Tuple[pd.DataFrame, pd.Series]:
    """
    Compute state-level and national natural gas displacement and capacity
    equivalent from baseline vs policy state-hourly outputs.

    This function performs the following steps:

    1. For each state:
       - Load baseline and policy hourly series for the specified target year.
       - Compute the hourly difference in net load (baseline - policy).
       - Optionally clip negative differences to zero.
       - Sum across hours to obtain total annual delta MWh for the state.

    2. Determine the natural gas fraction of delta MWh, using one of two
       gas share modes:

       - gas_share_mode = "state_eia":
           Use state-level natural gas shares from the EIA-like CSV:

               gas_share_state = gas_mwh_state / total_mwh_state

       - gas_share_mode = "fixed_fraction":
           Use a fixed fraction for all states:

               gas_share_state = fixed_gas_share

       The displaced gas MWh for a state is then computed as:

           gas_mwh_displaced = delta_mwh_total * gas_share_state

    3. For each state:
       - Compute the state-level gas capacity equivalent:

             gas_capacity_equiv_mw = gas_mwh_displaced / (capacity_factor * 8760)
             gas_capacity_equiv_gw = gas_capacity_equiv_mw / 1000

    4. Aggregate state-level results to produce national totals.

    Parameters
    ----------
    states : Iterable[str]
        Iterable of two-letter state abbreviations to process (e.g.,
        ["MN", "CA", "TX"]). Case-insensitive; state abbreviations are
        converted to uppercase internally.
    gas_share_csv_path : str, optional
        Path to the CSV file containing state-level natural gas generation
        shares. Required if gas_share_mode = "state_eia". Ignored if
        gas_share_mode = "fixed_fraction".
    target_year : int, optional
        Year in the state-hourly files to use for the hourly series
        (default is 2040).
    gas_share_year : int, optional
        Year in the gas-share CSV to use when computing the gas share
        (default is 2024). Only used when gas_share_mode = "state_eia".
    capacity_factor : float, optional
        Capacity factor to use when converting annual gas MWh displaced to
        an equivalent gas capacity (default is 0.80). For fleet-average
        combined-cycle plants, a value around 0.57 may be appropriate.
    baseline_scenario : str, optional
        Scenario name for the baseline state-hourly files (default "baseline").
    policy_scenario : str, optional
        Scenario name for the policy state-hourly files (default "policy").
    state_hourly_path_template : str, optional
        Path template for state-hourly files. The template must include the
        placeholders {state_abbr} and {scenario}. Defaults to
        DEFAULT_STATE_HOURLY_PATH_TEMPLATE.
    clip_negative_deltas : bool, optional
        If True (default), negative hourly deltas (where policy load exceeds
        baseline load) are set to zero before summing. This avoids treating
        increases in load as negative displacement. If False, negative deltas
        are retained.
    gas_share_mode : str, optional
        Mode for determining the natural gas share of the delta MWh. Allowed
        values are:

            "state_eia"      : use state-level shares from the EIA-like CSV.
            "fixed_fraction" : use a fixed fraction for all states.

        The default is "state_eia".
    fixed_gas_share : float, optional
        Fixed fraction of delta MWh to attribute to natural gas when
        gas_share_mode = "fixed_fraction". Must lie between 0 and 1.
        The default is 0.75.

    Returns
    -------
    state_results : pandas.DataFrame
        DataFrame with one row per state, containing the following columns:

            state                      : two-letter state abbreviation (str)
            delta_mwh_total            : annual sum of baseline - policy
                                         MWh for target_year (float)
            gas_share                  : natural gas generation share used
                                         for this state (0–1) (float)
            gas_mwh_displaced          : delta_mwh_total * gas_share (float)
            gas_capacity_equiv_mw      : capacity-equivalent natural gas capacity
                                         in MW (float)
            gas_capacity_equiv_gw      : capacity-equivalent natural gas capacity
                                         in GW (float)

    national_summary : pandas.Series
        Series with national totals aggregated across all states:

            delta_mwh_total            : sum of state delta_mwh_total (float)
            gas_mwh_displaced          : sum of state gas_mwh_displaced (float)
            gas_capacity_equiv_mw      : national gas capacity equivalent in MW
                                         (float)
            gas_capacity_equiv_gw      : national gas capacity equivalent in GW
                                         (float)

    Raises
    ------
    FileNotFoundError
        If any required state-hourly file cannot be found.
    ValueError
        If any state-hourly file does not contain data for the target year,
        if baseline and policy arrays have mismatched shapes for a state,
        if gas share data for the specified gas_share_year are missing or
        malformed when gas_share_mode = "state_eia", or if gas_share_mode
        or fixed_gas_share are invalid.
    """
    gas_share_mode_normalized = gas_share_mode.lower()
    if gas_share_mode_normalized not in {"state_eia", "fixed_fraction"}:
        raise ValueError(
            f"Invalid gas_share_mode='{gas_share_mode}'. "
            "Expected 'state_eia' or 'fixed_fraction'."
        )

    if gas_share_mode_normalized == "fixed_fraction":
        if not (0.0 <= fixed_gas_share <= 1.0):
            raise ValueError(
                f"fixed_gas_share must be between 0 and 1, got {fixed_gas_share}."
            )
        gas_share_dict: Dict[str, float] = {}
    else:
        if gas_share_csv_path is None:
            raise ValueError(
                "gas_share_csv_path must be provided when gas_share_mode='state_eia'."
            )
        gas_share_dict = _load_state_gas_share(
            gas_share_csv_path=gas_share_csv_path,
            share_year=gas_share_year,
        )

    results = []

    for st in states:
        state_abbr = st.upper()

        baseline_series = _load_state_hourly_series(
            state_abbr=state_abbr,
            scenario=baseline_scenario,
            year=target_year,
            path_template=state_hourly_path_template,
        )
        policy_series = _load_state_hourly_series(
            state_abbr=state_abbr,
            scenario=policy_scenario,
            year=target_year,
            path_template=state_hourly_path_template,
        )

        if baseline_series.shape != policy_series.shape:
            raise ValueError(
                f"Shape mismatch for state_abbr={state_abbr}, year={target_year}: "
                f"baseline shape={baseline_series.shape}, "
                f"policy shape={policy_series.shape}"
            )

        # Hourly reduction in net load (MW) due to the policy scenario.
        delta_mw = baseline_series - policy_series

        if clip_negative_deltas:
            delta_mw = np.where(delta_mw < 0.0, 0.0, delta_mw)

        # Convert MW to MWh by summing over hourly timesteps.
        delta_mwh_total = float(delta_mw.sum())

        if gas_share_mode_normalized == "state_eia":
            gas_share = float(gas_share_dict.get(state_abbr, 0.0))
        else:
            gas_share = float(fixed_gas_share)

        gas_mwh_displaced = delta_mwh_total * gas_share

        if capacity_factor > 0.0:
            gas_capacity_equiv_mw = gas_mwh_displaced / (capacity_factor * 8760.0)
        else:
            gas_capacity_equiv_mw = 0.0

        gas_capacity_equiv_gw = gas_capacity_equiv_mw / 1000.0

        results.append(
            {
                "state": state_abbr,
                "delta_mwh_total": delta_mwh_total,
                "gas_share": gas_share,
                "gas_mwh_displaced": gas_mwh_displaced,
                "gas_capacity_equiv_mw": gas_capacity_equiv_mw,
                "gas_capacity_equiv_gw": gas_capacity_equiv_gw,
            }
        )

    state_results = (
        pd.DataFrame(results)
        .sort_values("state")
        .reset_index(drop=True)
    )

    # Compute national totals.
    total_delta_mwh = float(state_results["delta_mwh_total"].sum())
    total_gas_mwh_displaced = float(state_results["gas_mwh_displaced"].sum())

    if capacity_factor > 0.0:
        total_gas_capacity_equiv_mw = total_gas_mwh_displaced / (
            capacity_factor * 8760.0
        )
    else:
        total_gas_capacity_equiv_mw = 0.0

    total_gas_capacity_equiv_gw = total_gas_capacity_equiv_mw / 1000.0

    national_summary = pd.Series(
        {
            "delta_mwh_total": total_delta_mwh,
            "gas_mwh_displaced": total_gas_mwh_displaced,
            "gas_capacity_equiv_mw": total_gas_capacity_equiv_mw,
            "gas_capacity_equiv_gw": total_gas_capacity_equiv_gw,
        }
    )

    return state_results, national_summary

def plot_weekly_national_load(
    states: Iterable[str],
    target_year: int = 2040,
    baseline_scenario: str = "baseline",
    policy_scenario: str = "policy",
    state_hourly_path_template: str = DEFAULT_STATE_HOURLY_PATH_TEMPLATE,
    figsize: Tuple[int, int] = (12, 6),
    energy_units: str = "TWh",
) -> None:
    """
    Plot weekly total national energy consumption under baseline and
    policy scenarios, with shading indicating the policy-induced load
    reduction (i.e., baseline minus policy).

    This function reuses the same state-level hourly CSVs and path
    template as the capacity-equivalent calculations, aggregating
    hourly net load across all states before resampling to weekly
    totals.

    Parameters
    ----------
    states : Iterable[str]
        Iterable of two-letter state abbreviations to include in the
        national aggregation (e.g., ["CA", "MN", "TX"]). Case-insensitive;
        abbreviations are converted to uppercase internally.
    target_year : int, optional
        Year to extract from the state-hourly files (default 2040).
    baseline_scenario : str, optional
        Scenario name for the baseline state-hourly files (default "baseline").
    policy_scenario : str, optional
        Scenario name for the policy state-hourly files (default "policy").
    state_hourly_path_template : str, optional
        Path template for state-hourly files. Must include the placeholders
        {state_abbr} and {scenario}. Defaults to
        DEFAULT_STATE_HOURLY_PATH_TEMPLATE.
    figsize : tuple[int, int], optional
        Matplotlib figure size in inches (default (12, 6)).
    energy_units : {"MWh", "GWh", "TWh"}, optional
        Units for the y-axis. The underlying data are hourly MW, which
        become MWh when summed over hours. This parameter controls the
        scaling for plotting:
            - "MWh" : no additional scaling
            - "GWh" : divide MWh by 1e3
            - "TWh" : divide MWh by 1e6 (default)

    Returns
    -------
    None
        Displays the plot using matplotlib.

    Raises
    ------
    FileNotFoundError
        If any required state-hourly file cannot be found.
    ValueError
        If baseline and policy arrays have mismatched shapes for any state,
        or if energy_units is invalid.
    """
    # ------------------------------------------------------------------
    # 1. Aggregate baseline and policy hourly series across all states
    # ------------------------------------------------------------------
    states_list = [st.upper() for st in states]

    baseline_nat: Optional[np.ndarray] = None
    policy_nat: Optional[np.ndarray] = None

    for st in states_list:
        baseline_series = _load_state_hourly_series(
            state_abbr=st,
            scenario=baseline_scenario,
            year=target_year,
            path_template=state_hourly_path_template,
        )
        policy_series = _load_state_hourly_series(
            state_abbr=st,
            scenario=policy_scenario,
            year=target_year,
            path_template=state_hourly_path_template,
        )

        if baseline_series.shape != policy_series.shape:
            raise ValueError(
                f"Shape mismatch for state_abbr={st}, year={target_year}: "
                f"baseline shape={baseline_series.shape}, "
                f"policy shape={policy_series.shape}"
            )

        if baseline_nat is None:
            baseline_nat = baseline_series.astype(float)
            policy_nat = policy_series.astype(float)
        else:
            baseline_nat += baseline_series
            policy_nat += policy_series

    if baseline_nat is None or policy_nat is None:
        raise ValueError("No states provided or no data loaded for aggregation.")

    # ------------------------------------------------------------------
    # 2. Build a datetime index and resample to weekly totals
    # ------------------------------------------------------------------
    n_hours = baseline_nat.shape[0]
    start = pd.Timestamp(f"{target_year}-01-01 00:00:00")
    idx = pd.date_range(start=start, periods=n_hours, freq="H")

    df = pd.DataFrame(
        {
            "baseline_mw": baseline_nat,
            "policy_mw": policy_nat,
        },
        index=idx,
    )

    # Sum hourly MW over each week → weekly MWh
    weekly = df.resample("W").sum()

    # Decide scaling for plotting units
    energy_units = energy_units.upper()
    if energy_units == "MWH":
        scale = 1.0
        unit_label = "MWh"
    elif energy_units == "GWH":
        scale = 1e-3
        unit_label = "GWh"
    elif energy_units == "TWH":
        scale = 1e-6
        unit_label = "TWh"
    else:
        raise ValueError(
            f"Invalid energy_units='{energy_units}'. "
            "Expected one of 'MWh', 'GWh', 'TWh'."
        )

    weekly_baseline = weekly["baseline_mw"] * scale
    weekly_policy = weekly["policy_mw"] * scale

    # ------------------------------------------------------------------
    # 3. Plot weekly total energy with shading for policy savings
    # ------------------------------------------------------------------
    fig, ax = plt.subplots(figsize=figsize)

    ax.plot(
        weekly.index,
        weekly_baseline,
        label="Baseline",
        linewidth=2.0,
    )
    ax.plot(
        weekly.index,
        weekly_policy,
        label="Policy (cheap rooftop solar)",
        linewidth=2.0,
    )

    # Shaded area where baseline > policy → avoided load
    ax.fill_between(
        weekly.index,
        weekly_baseline,
        weekly_policy,
        where=weekly_baseline > weekly_policy,
        interpolate=True,
        alpha=0.2,
        label="Load reduction (baseline − policy)",
    )

    ax.set_ylabel(f"Weekly total energy ({unit_label})")
    ax.set_xlabel("Week")
    ax.set_title(
        f"Weekly national load: baseline vs. policy (year {target_year})"
    )
    ax.legend()
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.show()

    def plot_representative_week_national_load(
        states: Iterable[str],
        target_year: int = 2040,
        baseline_scenario: str = "baseline",
        policy_scenario: str = "policy",
        state_hourly_path_template: str = DEFAULT_STATE_HOURLY_PATH_TEMPLATE,
        week_start: Optional[pd.Timestamp] = None,
        figsize: Tuple[int, int] = (12, 6),
    ) -> None:
        """
        Plot a representative week of national hourly load for baseline and
        policy scenarios, with shading indicating the load reduction due to
        rooftop solar.

        By default, the function selects the week in the target year with
        the largest total baseline minus policy energy (i.e., the week where
        rooftop solar reduces load the most). You can override this by
        passing an explicit `week_start` timestamp.

        Parameters
        ----------
        states : Iterable[str]
            Iterable of two-letter state abbreviations to include in the
            national aggregation (e.g., ["CA", "TX", "MN", "NY"]).
        target_year : int, optional
            Year to extract from the state-hourly files (default 2040).
        baseline_scenario : str, optional
            Scenario name for the baseline state-hourly files (default "baseline").
        policy_scenario : str, optional
            Scenario name for the policy state-hourly files (default "policy").
        state_hourly_path_template : str, optional
            Path template for state-hourly files. Must include the placeholders
            {state_abbr} and {scenario}. Defaults to DEFAULT_STATE_HOURLY_PATH_TEMPLATE.
        week_start : pandas.Timestamp, optional
            Start timestamp of the week to plot (inclusive). If None, the
            function automatically selects the week with the largest total
            baseline - policy energy reduction.
        figsize : tuple[int, int], optional
            Matplotlib figure size in inches, default (12, 6).

        Returns
        -------
        None
            Displays the plot using matplotlib.
        """
        states_list = [st.upper() for st in states]

        baseline_nat: Optional[np.ndarray] = None
        policy_nat: Optional[np.ndarray] = None

        # Aggregate hourly series nationally
        for st in states_list:
            baseline_series = _load_state_hourly_series(
                state_abbr=st,
                scenario=baseline_scenario,
                year=target_year,
                path_template=state_hourly_path_template,
            )
            policy_series = _load_state_hourly_series(
                state_abbr=st,
                scenario=policy_scenario,
                year=target_year,
                path_template=state_hourly_path_template,
            )

            if baseline_series.shape != policy_series.shape:
                raise ValueError(
                    f"Shape mismatch for state={st}, year={target_year}: "
                    f"baseline shape={baseline_series.shape}, "
                    f"policy shape={policy_series.shape}"
                )

            if baseline_nat is None:
                baseline_nat = baseline_series.astype(float)
                policy_nat = policy_series.astype(float)
            else:
                baseline_nat += baseline_series
                policy_nat += policy_series

        if baseline_nat is None or policy_nat is None:
            raise ValueError("No states provided or no data loaded for aggregation.")

        n_hours = baseline_nat.shape[0]
        start = pd.Timestamp(f"{target_year}-01-01 00:00:00")
        idx = pd.date_range(start=start, periods=n_hours, freq="H")

        df = pd.DataFrame(
            {
                "baseline_mw": baseline_nat,
                "policy_mw": policy_nat,
            },
            index=idx,
        )

        # If no explicit week_start, choose the week with the largest total
        # baseline - policy energy (i.e. most rooftop solar benefit).
        if week_start is None:
            weekly_diff = (
                (df["baseline_mw"] - df["policy_mw"])
                .resample("W")
                .sum()
            )
            best_week_end = weekly_diff.idxmax()
            # Week ends at best_week_end; start is 7 days earlier
            week_start = best_week_end - pd.Timedelta(days=6)
            week_start = week_start.normalize()

        # Slice the chosen week: 168 hours from week_start
        week_end = week_start + pd.Timedelta(hours=167)
        mask = (df.index >= week_start) & (df.index <= week_end)
        week_df = df.loc[mask].copy()

        if week_df.empty or len(week_df) < 24:
            raise ValueError(
                f"No data found for the week starting at {week_start}."
            )

        fig, ax = plt.subplots(figsize=figsize)

        ax.plot(
            week_df.index,
            week_df["baseline_mw"],
            label="Baseline",
            linewidth=2.0,
        )
        ax.plot(
            week_df.index,
            week_df["policy_mw"],
            label="Policy (cheap rooftop solar)",
            linewidth=2.0,
        )

        # Shade where baseline > policy (load reduction)
        ax.fill_between(
            week_df.index,
            week_df["baseline_mw"],
            week_df["policy_mw"],
            where=week_df["baseline_mw"] > week_df["policy_mw"],
            interpolate=True,
            alpha=0.2,
            label="Load reduction (baseline − policy)",
        )

        ax.set_ylabel("Load (MW)")
        ax.set_xlabel("Hour")
        ax.set_title(
            f"National hourly load: baseline vs. policy\n"
            f"Representative week starting {week_start.date()} (year {target_year})"
        )
        ax.legend()
        fig.autofmt_xdate()
        plt.tight_layout()
        plt.show()

def plot_smoothed_daily_national_load(
    states: Iterable[str],
    target_year: int = 2040,
    baseline_scenario: str = "baseline",
    policy_scenario: str = "policy",
    state_hourly_path_template: str = DEFAULT_STATE_HOURLY_PATH_TEMPLATE,
    ma_window_days: int = 7,
    figsize: Tuple[int, int] = (12, 6),
    energy_units: str = "TWh",
) -> None:
    """
    Plot smoothed national daily load for baseline and policy scenarios,
    highlighting seasonal variation and annual differences without
    showing intraday solar effects.

    This plot:
      - Aggregates hourly state-level load to daily total MWh
      - Applies a centered moving average (default 7 days)
      - Plots both curves with shading for baseline - policy

    Parameters
    ----------
    states : Iterable[str]
        Two-letter state abbreviations (e.g., ["CA", "TX", "NY"]).
    target_year : int
        Year to plot (default 2040).
    baseline_scenario : str
        Name of baseline scenario (default "baseline").
    policy_scenario : str
        Name of policy scenario (default "policy").
    state_hourly_path_template : str
        Path template matching your per-state hourly CSV locations.
    ma_window_days : int
        Number of days for moving average smoothing (default 7).
    figsize : Tuple[int, int]
        Matplotlib figure size.
    energy_units : {"MWh", "GWh", "TWh"}
        Units for plotting daily totals (default "TWh").

    Returns
    -------
    None
    """
    # Aggregate hourly → national
    states_list = [st.upper() for st in states]

    baseline_nat = None
    policy_nat = None

    for st in states_list:
        baseline_series = _load_state_hourly_series(
            state_abbr=st,
            scenario=baseline_scenario,
            year=target_year,
            path_template=state_hourly_path_template,
        )
        policy_series = _load_state_hourly_series(
            state_abbr=st,
            scenario=policy_scenario,
            year=target_year,
            path_template=state_hourly_path_template,
        )

        if baseline_nat is None:
            baseline_nat = baseline_series.astype(float)
            policy_nat = policy_series.astype(float)
        else:
            baseline_nat += baseline_series
            policy_nat += policy_series

    # Build datetime index
    start = pd.Timestamp(f"{target_year}-01-01 00:00")
    idx = pd.date_range(start=start, periods=len(baseline_nat), freq="H")

    df = pd.DataFrame(
        {"baseline_mw": baseline_nat, "policy_mw": policy_nat},
        index=idx,
    )

    # Convert hourly MW → daily MWh
    daily = df.resample("D").sum()

    # Convert to units
    energy_units = energy_units.upper()
    if energy_units == "MWH":
        scale = 1.0
        unit_label = "MWh"
    elif energy_units == "GWH":
        scale = 1e-3
        unit_label = "GWh"
    elif energy_units == "TWH":
        scale = 1e-6
        unit_label = "TWh"
    else:
        raise ValueError("energy_units must be one of MWh, GWh, TWh")

    daily_scaled = daily * scale

    # Apply moving average smoothing
    smoothed = daily_scaled.rolling(
        window=ma_window_days, center=True, min_periods=1
    ).mean()

    # Extract series
    base = smoothed["baseline_mw"]
    poli = smoothed["policy_mw"]

    # Plot
    fig, ax = plt.subplots(figsize=figsize)
    ax.plot(base.index, base, label="Baseline", linewidth=2)
    ax.plot(poli.index, poli, label="Policy (cheap rooftop solar)", linewidth=2)

    # Shaded reduction
    ax.fill_between(
        base.index,
        base,
        poli,
        where=(base > poli),
        color="lightblue",
        alpha=0.25,
        label="Load reduction (baseline − policy)",
    )

    ax.set_ylabel(f"Daily energy ({unit_label})")
    ax.set_title(
        f"Smoothed National Daily Load\n"
        f"{ma_window_days}-day moving average"
    )
    ax.legend()
    plt.tight_layout()
    plt.show()


