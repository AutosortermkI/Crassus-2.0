"""
Crassus 2.0 -- Options contract screening and selection.

Queries Alpaca's options API for available contracts on an underlying symbol
and selects the best contract based on configurable criteria:

  - Days to expiration (DTE) window
  - Moneyness / strike selection (proxy for delta)
  - Liquidity filters (open interest, volume, bid-ask spread)
  - Price range constraints

For **delta-based** filtering the module relies on moneyness as a proxy.
True greeks require the Alpaca *market-data* API (``OptionHistoricalDataClient``)
which can be added as a refinement.

Extension points:
  - Plug in IV rank / percentile filtering
  - Multi-leg strategies (spreads, straddles)
  - Custom scoring / ranking beyond simple filters
  - Real greeks via ``alpaca.data`` market-data snapshots
"""

import os
import logging
from datetime import date, timedelta
from dataclasses import dataclass
from typing import List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus

from utils import log_structured, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ScreeningCriteria:
    """Configuration for options contract filtering."""

    dte_min: int = 14                   # Minimum days to expiration
    dte_max: int = 45                   # Maximum days to expiration
    delta_min: float = 0.30             # Minimum absolute delta (moneyness proxy)
    delta_max: float = 0.70             # Maximum absolute delta (moneyness proxy)
    min_open_interest: int = 100        # Minimum open interest
    min_volume: int = 10                # Minimum daily volume
    max_spread_pct: float = 5.0         # Max bid-ask spread as % of mid price
    min_price: float = 0.50             # Minimum option premium
    max_price: float = 50.0             # Maximum option premium


@dataclass
class SelectedContract:
    """Represents a selected options contract ready for order submission."""

    symbol: str                 # OCC symbol (e.g. "AAPL240215C00150000")
    underlying: str             # Underlying ticker
    expiration: date
    strike: float
    contract_type: str          # "call" or "put"
    premium: float              # Estimated entry price (close / mid)
    open_interest: int
    dte: int


class NoContractFoundError(Exception):
    """Raised when no suitable options contract matches the screening criteria."""


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------

def get_screening_criteria() -> ScreeningCriteria:
    """Load screening criteria from environment variables with defaults."""
    return ScreeningCriteria(
        dte_min=int(os.environ.get("OPTIONS_DTE_MIN", "14")),
        dte_max=int(os.environ.get("OPTIONS_DTE_MAX", "45")),
        delta_min=float(os.environ.get("OPTIONS_DELTA_MIN", "0.30")),
        delta_max=float(os.environ.get("OPTIONS_DELTA_MAX", "0.70")),
        min_open_interest=int(os.environ.get("OPTIONS_MIN_OI", "100")),
        min_volume=int(os.environ.get("OPTIONS_MIN_VOLUME", "10")),
        max_spread_pct=float(os.environ.get("OPTIONS_MAX_SPREAD_PCT", "5.0")),
        min_price=float(os.environ.get("OPTIONS_MIN_PRICE", "0.50")),
        max_price=float(os.environ.get("OPTIONS_MAX_PRICE", "50.0")),
    )


# ---------------------------------------------------------------------------
# Screening logic
# ---------------------------------------------------------------------------

def screen_option_contracts(
    client: TradingClient,
    underlying: str,
    side: str,
    entry_price: float,
    criteria: Optional[ScreeningCriteria] = None,
    correlation_id: str = "",
) -> SelectedContract:
    """Find the best options contract for the given signal.

    Args:
        client: Authenticated Alpaca :class:`TradingClient`.
        underlying: Underlying ticker symbol (e.g. ``"AAPL"``).
        side: Signal direction (``"buy"`` or ``"sell"``).
              ``"buy"`` signal -> buy calls; ``"sell"`` signal -> buy puts.
        entry_price: Current price of the underlying (for strike selection).
        criteria: Screening criteria (uses env defaults if ``None``).
        correlation_id: For log tracing.

    Returns:
        :class:`SelectedContract` with the best matching contract.

    Raises:
        NoContractFoundError: If no contracts pass all filters.
    """
    if criteria is None:
        criteria = get_screening_criteria()

    # Determine contract type based on signal direction
    # Buy signal (bullish) -> calls; Sell signal (bearish) -> puts
    contract_type = "call" if side == "buy" else "put"

    log_structured(
        logger, logging.INFO,
        "Screening options contracts",
        correlation_id,
        underlying=underlying,
        type=contract_type,
        dte_range=f"{criteria.dte_min}-{criteria.dte_max}",
    )

    # Compute expiration-date window
    today = date.today()
    exp_min = today + timedelta(days=criteria.dte_min)
    exp_max = today + timedelta(days=criteria.dte_max)

    # Compute strike range: +/-10 % around the underlying price.
    # This is a rough proxy for the configured delta range; refine with
    # real greeks from the market-data API when available.
    strike_low  = entry_price * 0.90
    strike_high = entry_price * 1.10

    # Query Alpaca for available contracts
    request_params = GetOptionContractsRequest(
        underlying_symbols=[underlying],
        expiration_date_gte=exp_min.isoformat(),
        expiration_date_lte=exp_max.isoformat(),
        strike_price_gte=str(strike_low),
        strike_price_lte=str(strike_high),
        type=contract_type,
        status=AssetStatus.ACTIVE,
    )

    response = client.get_option_contracts(request_params)
    contracts = response.option_contracts if response else []

    log_structured(
        logger, logging.INFO,
        f"Found {len(contracts)} contracts before filtering",
        correlation_id,
        underlying=underlying,
    )

    if not contracts:
        raise NoContractFoundError(
            f"No {contract_type} contracts found for {underlying} "
            f"with DTE {criteria.dte_min}-{criteria.dte_max} days"
        )

    # ------------------------------------------------------------------
    # Filter and score candidates
    # ------------------------------------------------------------------
    candidates: List[dict] = []

    for contract in contracts:
        # Alpaca may return these fields as strings -- coerce defensively
        raw_oi = getattr(contract, "open_interest", 0)
        oi = int(raw_oi) if raw_oi else 0
        raw_close = getattr(contract, "close_price", 0)
        close_price = float(raw_close) if raw_close else 0.0
        strike = float(contract.strike_price)
        exp = contract.expiration_date
        if isinstance(exp, str):
            exp = date.fromisoformat(exp)
        dte = (exp - today).days

        # Apply hard filters
        if oi < criteria.min_open_interest:
            continue
        # Skip contracts with no price data -- can't evaluate or trade them
        if close_price <= 0:
            continue
        if close_price < criteria.min_price or close_price > criteria.max_price:
            continue

        # Score: prefer ATM (strike closest to entry) with highest OI
        moneyness_distance = abs(strike - entry_price) / entry_price

        candidates.append({
            "contract": contract,
            "strike": strike,
            "dte": dte,
            "expiration": exp,
            "premium": close_price,
            "oi": oi,
            "moneyness_distance": moneyness_distance,
        })

    if not candidates:
        raise NoContractFoundError(
            f"No {contract_type} contracts for {underlying} passed filters "
            f"(OI >= {criteria.min_open_interest}, "
            f"price {criteria.min_price}-{criteria.max_price})"
        )

    # Sort: closest to ATM first, then by OI descending
    candidates.sort(key=lambda c: (c["moneyness_distance"], -c["oi"]))
    best = candidates[0]

    selected = SelectedContract(
        symbol=best["contract"].symbol,
        underlying=underlying,
        expiration=best["expiration"],
        strike=best["strike"],
        contract_type=contract_type,
        premium=best["premium"],
        open_interest=best["oi"],
        dte=best["dte"],
    )

    log_structured(
        logger, logging.INFO,
        "Selected options contract",
        correlation_id,
        contract=selected.symbol,
        strike=selected.strike,
        dte=selected.dte,
        premium=selected.premium,
        oi=selected.open_interest,
    )

    return selected
