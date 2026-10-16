"""
eia_rates.py

State-average residential electricity and natural gas rates from EIA, with
fixed customer charges removed to give marginal (volumetric) rates.

EIA publishes average prices as revenue / sales, which spreads fixed monthly
customer charges over every unit sold. For decisions that change how much
energy a home uses (e.g. switching from a gas furnace to a heat pump), the
fixed charges don't change, so the relevant price is the marginal rate:

    marginal = (revenue - fixed_charge * customer_months) / sales

Data sources (all public, no API key):
    - Electricity: EIA-861M monthly sales, revenue and customers by state
    - Natural gas: EIA monthly residential prices and volumes by state, and
      annual residential consumer counts by state

The main entry point is:

    build_state_residential_rates(...)
"""

from __future__ import annotations

import io
import time
from typing import Optional
from urllib.request import Request, urlopen

import pandas as pd


EIA_861M_URL = "https://www.eia.gov/electricity/data/eia861m/xls/sales_revenue.xlsx"
GAS_PRICE_URL = "https://www.eia.gov/dnav/ng/xls/NG_PRI_SUM_A_EPG0_PRS_DMCF_M.xls"
GAS_VOLUME_URL = "https://www.eia.gov/dnav/ng/xls/NG_CONS_SUM_A_EPG0_VRS_MMCF_M.xls"
GAS_CONSUMERS_URL = "https://www.eia.gov/dnav/ng/xls/NG_CONS_NUM_A_EPG0_VN3_COUNT_A.xls"

# Average heat content of natural gas delivered to consumers (~1,037 Btu/cf)
THERMS_PER_MCF = 10.37

STATE_ABBR = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
    'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'District of Columbia': 'DC',
    'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL',
    'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA',
    'Maine': 'ME', 'Maryland': 'MD', 'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN',
    'Mississippi': 'MS', 'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV',
    'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK', 'Oregon': 'OR',
    'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC', 'South Dakota': 'SD',
    'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT', 'Virginia': 'VA',
    'Washington': 'WA', 'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY',
}


# =============================================================================
# Download helpers
# =============================================================================


def _fetch(url: str, attempts: int = 3) -> io.BytesIO:
    """
    Download a file into memory, retrying on transient network errors.
    EIA rejects requests without a browser user agent.
    """
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=60) as resp:
                return io.BytesIO(resp.read())
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(2 ** attempt)


def _read_eia_series_by_state(url: str, suffix: str, value_name: str) -> pd.DataFrame:
    """
    Read an EIA natural gas series workbook (one column per state) into long format.

    Column headers look like "Alabama Price of Natural Gas Delivered to ...";
    the state name is the text before ``suffix``.
    """
    wide = pd.read_excel(_fetch(url), sheet_name="Data 1", header=2)
    records = []
    for col in wide.columns[1:]:
        name = col.split(suffix)[0].strip()
        if name not in STATE_ABBR:
            continue
        records.append(pd.DataFrame({
            "date": wide["Date"],
            "state": STATE_ABBR[name],
            value_name: pd.to_numeric(wide[col], errors="coerce"),
        }))
    return pd.concat(records, ignore_index=True)


# =============================================================================
# Raw EIA data
# =============================================================================


def load_electricity_monthly(url: str = EIA_861M_URL) -> pd.DataFrame:
    """
    Load EIA-861M monthly residential electricity sales, revenue and customers.

    Parameters
    ----------
    url : str, optional
        URL of the EIA-861M sales_revenue workbook.

    Returns
    -------
    pandas.DataFrame
        One row per state-month with columns ``date`` (month start),
        ``state``, ``revenue_usd``, ``sales_kwh`` and ``customers``.
    """
    raw = pd.read_excel(_fetch(url), header=2)

    # The residential block is the first set of Revenue/Sales/Customers columns
    df = pd.DataFrame({
        "year": pd.to_numeric(raw["Year"], errors="coerce"),
        "month": pd.to_numeric(raw["Month"], errors="coerce"),
        "state": raw["State"],
        "revenue_usd": pd.to_numeric(raw["Thousand Dollars"], errors="coerce") * 1000,
        "sales_kwh": pd.to_numeric(raw["Megawatthours"], errors="coerce") * 1000,
        "customers": pd.to_numeric(raw["Count"], errors="coerce"),
    })
    df = df[df["state"].isin(STATE_ABBR.values()) & df["year"].notna() & df["month"].notna()]
    df["date"] = pd.to_datetime(dict(year=df["year"].astype(int), month=df["month"].astype(int), day=1))
    return df[["date", "state", "revenue_usd", "sales_kwh", "customers"]].reset_index(drop=True)


def load_gas_monthly(
    price_url: str = GAS_PRICE_URL,
    volume_url: str = GAS_VOLUME_URL,
) -> pd.DataFrame:
    """
    Load EIA monthly residential natural gas prices and volumes by state.

    Parameters
    ----------
    price_url : str, optional
        URL of the residential price workbook ($/Mcf).
    volume_url : str, optional
        URL of the residential consumption workbook (MMcf).

    Returns
    -------
    pandas.DataFrame
        One row per state-month with columns ``date`` (month start),
        ``state``, ``price_usd_per_mcf`` and ``volume_mcf``. Months EIA
        withholds are NaN.
    """
    price = _read_eia_series_by_state(price_url, " Price of Natural Gas", "price_usd_per_mcf")
    volume = _read_eia_series_by_state(volume_url, " Natural Gas Residential", "volume_mmcf")
    df = price.merge(volume, on=["date", "state"], how="outer")
    df["volume_mcf"] = df.pop("volume_mmcf") * 1000
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp()
    return df


def load_gas_consumers(url: str = GAS_CONSUMERS_URL) -> pd.DataFrame:
    """
    Load EIA annual residential natural gas consumer counts by state.

    Returns
    -------
    pandas.DataFrame
        Columns ``year``, ``state`` and ``consumers``.
    """
    df = _read_eia_series_by_state(url, " Natural Gas Number of", "consumers")
    df["year"] = df.pop("date").dt.year
    return df[["year", "state", "consumers"]]


# =============================================================================
# Marginal rates
# =============================================================================


def build_state_residential_rates(
    end_month: Optional[str] = None,
    months: int = 12,
    elec_fixed_monthly: float = 10.0,
    gas_fixed_monthly: float = 11.25,
) -> pd.DataFrame:
    """
    Build state average and marginal residential electricity and gas rates.

    Both fuels use the same trailing window of ``months`` months ending at
    ``end_month``, so seasonal swings in price average out. The marginal
    rate removes a fixed monthly customer charge from total revenue:

        marginal = (revenue - fixed_monthly * customer_months) / sales

    For gas, revenue is price × volume for each month EIA reports, and the
    customer count is the latest annual figure available.

    Parameters
    ----------
    end_month : str, optional
        Last month of the window, e.g. "2026-07". Defaults to the latest
        month available in both the electricity and gas data.
    months : int, optional
        Length of the window in months. Defaults to 12.
    elec_fixed_monthly : float, optional
        Fixed electric customer charge ($/month). Defaults to 10.0, the
        value ResStock uses for every building.
    gas_fixed_monthly : float, optional
        Fixed gas customer charge ($/month). Defaults to 11.25, the value
        ResStock uses for every building.

    Returns
    -------
    pandas.DataFrame
        One row per state with columns:
            - ``state``
            - ``elec_avg_cents_per_kwh``, ``elec_marginal_cents_per_kwh``
            - ``elec_kwh_per_customer_month``
            - ``gas_avg_usd_per_therm``, ``gas_marginal_usd_per_therm``
            - ``gas_therms_per_customer_month``
            - ``gas_months_reported`` (months with both price and volume)
            - ``gas_consumers_year`` (year of the consumer count)
            - ``period_start``, ``period_end``
    """
    elec = load_electricity_monthly()
    gas = load_gas_monthly()
    consumers = load_gas_consumers()

    if end_month is None:
        end = min(elec["date"].max(), gas.dropna(subset=["price_usd_per_mcf"])["date"].max())
    else:
        end = pd.Timestamp(end_month).to_period("M").to_timestamp()
    start = end - pd.DateOffset(months=months - 1)

    # Electricity: revenue, sales and customer-months over the window
    e = elec[(elec["date"] >= start) & (elec["date"] <= end)]
    e = e.groupby("state").agg(
        revenue_usd=("revenue_usd", "sum"),
        sales_kwh=("sales_kwh", "sum"),
        customer_months=("customers", "sum"),
        n_months=("date", "nunique"),
    )
    if (e["n_months"] < months).any():
        raise ValueError(f"Electricity data incomplete for: {e.index[e['n_months'] < months].tolist()}")
    e["elec_avg_cents_per_kwh"] = e["revenue_usd"] / e["sales_kwh"] * 100
    e["elec_marginal_cents_per_kwh"] = (
        (e["revenue_usd"] - elec_fixed_monthly * e["customer_months"]) / e["sales_kwh"] * 100
    )
    e["elec_kwh_per_customer_month"] = e["sales_kwh"] / e["customer_months"]

    # Gas: volume-weighted price over months EIA reports, latest annual consumer count
    g = gas[(gas["date"] >= start) & (gas["date"] <= end)].dropna(subset=["price_usd_per_mcf", "volume_mcf"])
    g = g.assign(revenue_usd=g["price_usd_per_mcf"] * g["volume_mcf"]).groupby("state").agg(
        revenue_usd=("revenue_usd", "sum"),
        volume_mcf=("volume_mcf", "sum"),
        gas_months_reported=("date", "nunique"),
    )
    latest = consumers.dropna(subset=["consumers"])
    latest = latest[latest["year"] == latest.groupby("state")["year"].transform("max")]
    g = g.join(latest.set_index("state").rename(columns={"year": "gas_consumers_year"}))

    customer_months = g["consumers"] * g["gas_months_reported"]
    therms = g["volume_mcf"] * THERMS_PER_MCF
    g["gas_avg_usd_per_therm"] = g["revenue_usd"] / therms
    g["gas_marginal_usd_per_therm"] = (g["revenue_usd"] - gas_fixed_monthly * customer_months) / therms
    g["gas_therms_per_customer_month"] = therms / customer_months

    out = e.join(g, how="left", lsuffix="_elec", rsuffix="_gas").reset_index()
    out["period_start"] = start.strftime("%Y-%m")
    out["period_end"] = end.strftime("%Y-%m")

    return out[[
        "state",
        "elec_avg_cents_per_kwh", "elec_marginal_cents_per_kwh", "elec_kwh_per_customer_month",
        "gas_avg_usd_per_therm", "gas_marginal_usd_per_therm", "gas_therms_per_customer_month",
        "gas_months_reported", "gas_consumers_year",
        "period_start", "period_end",
    ]]
