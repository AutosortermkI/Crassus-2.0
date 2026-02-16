"""
Crassus 2.0 -- Options contract screening and selection.

Queries Alpaca's options API for available contracts on an underlying symbol
and selects the best contract based on configurable criteria:

  - Days to expiration (DTE) window
  - Delta-based filtering via Black-Scholes Greeks (greeks.py)
  - Liquidity filters (open interest, volume, bid-ask spread)
  - Price range constraints

When Yahoo Finance is enabled (``YAHOO_ENABLED=true``), the screener uses
Yahoo for richer market data (bid/ask/IV/volume) and computes real Greeks
for delta-based contract selection.  When Yahoo is unavailable, it falls
back to Alpaca-only screening with moneyness as a delta proxy.

Extension points:
  - Plug in IV rank / percentile filtering
  - Multi-leg strategies (spreads, straddles)
  - Custom scoring / ranking beyond simple filters
"""

import os
import logging
from datetime import date, timedelta
from dataclasses import dataclass
from typing import List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus

from greeks import (
    compute_delta,
    compute_all_greeks,
    implied_volatility,
    DEFAULT_RISK_FREE_RATE,
    OptionGreeks,
)
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
    delta_min: float = 0.30             # Minimum absolute delta
    delta_max: float = 0.70             # Maximum absolute delta
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
# Scoring
# ---------------------------------------------------------------------------

def _compute_candidate_score(
    delta: Optional[float],
    target_delta: float,
    oi: int,
    spread_pct: Optional[float],
    iv: Optional[float],
) -> float:
    """Compute a composite score for ranking candidates.

    Lower score is better.  Weights:
      - Delta proximity to target: 40%
      - OI (higher is better, inverted): 30%
      - Spread tightness (lower is better): 20%
      - IV (lower is preferred for buying): 10%

    Args:
        delta: Absolute delta of the contract (None if unavailable).
        target_delta: Midpoint of delta_min and delta_max.
        oi: Open interest.
        spread_pct: Bid-ask spread as percentage of mid (None if unavailable).
        iv: Implied volatility (None if unavailable).

    Returns:
        Composite score (lower is better).
    """
    # Delta proximity (0 = perfect match)
    if delta is not None:
        delta_score = abs(abs(delta) - target_delta)
    else:
        delta_score = 0.5  # Penalty for unknown delta

    # OI score: normalize to [0,1] range; higher OI -> lower score
    # Use log scale to avoid extreme values dominating
    oi_score = 1.0 / (1.0 + oi / 1000.0)

    # Spread score
    if spread_pct is not None and spread_pct >= 0:
        spread_score = spread_pct / 100.0  # Normalize
    else:
        spread_score = 0.05  # Default penalty

    # IV score (lower is better for buying)
    if iv is not None and iv > 0:
        iv_score = iv  # Already 0-1+ range typically
    else:
        iv_score = 0.5  # Default

    return (
        0.40 * delta_score
        + 0.30 * oi_score
        + 0.20 * spread_score
        + 0.10 * iv_score
    )


# ---------------------------------------------------------------------------
# Yahoo-enhanced screening
# ---------------------------------------------------------------------------

def screen_with_yahoo(
    alpaca_client: TradingClient,
    underlying: str,
    side: str,
    entry_price: float,
    criteria: Optional[ScreeningCriteria] = None,
    correlation_id: str = "",
) -> SelectedContract:
    """Screen options using Yahoo Finance data for richer filtering.

    Uses Yahoo for market data (bid/ask/IV/volume) and computes real
    Black-Scholes Greeks for delta-based selection.  Maps the selected
    contract symbol back to Alpaca for order submission.

    Args:
        alpaca_client: Authenticated Alpaca :class:`TradingClient`.
        underlying: Underlying ticker symbol (e.g. ``"AAPL"``).
        side: Signal direction (``"buy"`` or ``"sell"``).
        entry_price: Current price of the underlying.
        criteria: Screening criteria (uses env defaults if ``None``).
        correlation_id: For log tracing.

    Returns:
        :class:`SelectedContract` with the best matching contract.

    Raises:
        NoContractFoundError: If no contracts pass all filters.
    """
    # Lazy import to avoid hard dependency when Yahoo is disabled
    from yahoo_client import (
        YahooCrumbClient,
        get_expirations,
        get_option_chain,
        pick_expiration,
        YahooClientError,
    )

    if criteria is None:
        criteria = get_screening_criteria()

    contract_type = "call" if side == "buy" else "put"
    risk_free_rate = DEFAULT_RISK_FREE_RATE
    target_delta = (criteria.delta_min + criteria.delta_max) / 2.0
    today = date.today()

    log_structured(
        logger, logging.INFO,
        "Screening options via Yahoo Finance",
        correlation_id,
        underlying=underlying,
        type=contract_type,
    )

    yahoo_client = YahooCrumbClient()
    expirations = get_expirations(yahoo_client, underlying, correlation_id)

    # Filter expirations to DTE window
    valid_expirations: List[int] = []
    for exp_unix in expirations:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        exp_date = datetime.fromtimestamp(exp_unix, ZoneInfo("America/New_York")).date()
        dte = (exp_date - today).days
        if criteria.dte_min <= dte <= criteria.dte_max:
            valid_expirations.append(exp_unix)

    if not valid_expirations:
        raise NoContractFoundError(
            f"No Yahoo expirations for {underlying} within "
            f"DTE {criteria.dte_min}-{criteria.dte_max}"
        )

    candidates: List[dict] = []

    for exp_unix in valid_expirations:
        chain = get_option_chain(yahoo_client, underlying, exp_unix, correlation_id)

        for ycontract in chain.contracts:
            if ycontract.option_type != contract_type:
                continue

            # Price filter
            price = ycontract.last_price
            if price <= 0:
                continue
            if price < criteria.min_price or price > criteria.max_price:
                continue

            # OI filter
            if ycontract.open_interest < criteria.min_open_interest:
                continue

            # Volume filter
            if ycontract.volume < criteria.min_volume:
                continue

            # Spread filter (bid/ask available from Yahoo)
            spread_pct = None
            if ycontract.bid > 0 and ycontract.ask > 0:
                mid = (ycontract.bid + ycontract.ask) / 2.0
                if mid > 0:
                    spread_pct = ((ycontract.ask - ycontract.bid) / mid) * 100.0
                    if spread_pct > criteria.max_spread_pct:
                        continue

            # Compute DTE
            dte = (ycontract.expiration - today).days
            if dte <= 0:
                continue
            dte_years = dte / 365.0

            # Compute Greeks with Yahoo's IV (or solve for IV from market price)
            iv = ycontract.implied_volatility
            if iv <= 0:
                iv = implied_volatility(
                    market_price=price,
                    underlying_price=entry_price,
                    strike=ycontract.strike,
                    dte_years=dte_years,
                    risk_free_rate=risk_free_rate,
                    option_type=contract_type,
                )

            delta = None
            greeks = None
            if iv > 0 and not (iv != iv):  # Check for NaN
                greeks = compute_all_greeks(
                    underlying_price=entry_price,
                    strike=ycontract.strike,
                    dte_years=dte_years,
                    risk_free_rate=risk_free_rate,
                    sigma=iv,
                    option_type=contract_type,
                )
                delta = greeks.delta

                # Delta filter
                if abs(delta) < criteria.delta_min or abs(delta) > criteria.delta_max:
                    continue

            score = _compute_candidate_score(
                delta=delta,
                target_delta=target_delta,
                oi=ycontract.open_interest,
                spread_pct=spread_pct,
                iv=iv if (iv and iv == iv) else None,
            )

            candidates.append({
                "symbol": ycontract.contract_symbol,
                "strike": ycontract.strike,
                "dte": dte,
                "expiration": ycontract.expiration,
                "premium": price,
                "oi": ycontract.open_interest,
                "delta": delta,
                "score": score,
            })

    if not candidates:
        raise NoContractFoundError(
            f"No {contract_type} contracts for {underlying} passed Yahoo-enhanced filters"
        )

    # Sort by composite score (lower is better)
    candidates.sort(key=lambda c: c["score"])
    best = candidates[0]

    selected = SelectedContract(
        symbol=best["symbol"],
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
        "Selected contract via Yahoo-enhanced screening",
        correlation_id,
        contract=selected.symbol,
        strike=selected.strike,
        dte=selected.dte,
        premium=selected.premium,
        oi=selected.open_interest,
        delta=best.get("delta"),
    )

    return selected


# ---------------------------------------------------------------------------
# Alpaca-only screening (fallback)
# ---------------------------------------------------------------------------

def _screen_alpaca_only(
    client: TradingClient,
    underlying: str,
    side: str,
    entry_price: float,
    criteria: ScreeningCriteria,
    correlation_id: str = "",
) -> SelectedContract:
    """Alpaca-only screening fallback (no Yahoo data).

    Uses moneyness as a delta proxy and basic OI/price filters.
    This is the original screening logic, retained as a fallback
    when Yahoo Finance is unavailable.

    Args:
        client: Authenticated Alpaca :class:`TradingClient`.
        underlying: Underlying ticker symbol.
        side: Signal direction (``"buy"`` or ``"sell"``).
        entry_price: Current price of the underlying.
        criteria: Screening criteria.
        correlation_id: For log tracing.

    Returns:
        :class:`SelectedContract` with the best matching contract.

    Raises:
        NoContractFoundError: If no contracts pass all filters.
    """
    contract_type = "call" if side == "buy" else "put"
    risk_free_rate = DEFAULT_RISK_FREE_RATE
    target_delta = (criteria.delta_min + criteria.delta_max) / 2.0
    today = date.today()

    log_structured(
        logger, logging.INFO,
        "Screening options contracts (Alpaca-only fallback)",
        correlation_id,
        underlying=underlying,
        type=contract_type,
        dte_range=f"{criteria.dte_min}-{criteria.dte_max}",
    )

    # Compute expiration-date window
    exp_min = today + timedelta(days=criteria.dte_min)
    exp_max = today + timedelta(days=criteria.dte_max)

    # Wider strike range to capture more candidates for delta filtering
    strike_low = entry_price * 0.85
    strike_high = entry_price * 1.15

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
        if close_price <= 0:
            continue
        if close_price < criteria.min_price or close_price > criteria.max_price:
            continue

        # Volume filter
        raw_volume = getattr(contract, "volume", 0)
        volume = int(raw_volume) if raw_volume else 0
        if volume < criteria.min_volume:
            continue

        # Compute delta from Black-Scholes using implied IV from market price
        delta = None
        iv = None
        if dte > 0:
            dte_years = dte / 365.0
            iv = implied_volatility(
                market_price=close_price,
                underlying_price=entry_price,
                strike=strike,
                dte_years=dte_years,
                risk_free_rate=risk_free_rate,
                option_type=contract_type,
            )
            # Filter by delta if IV solve succeeded
            if iv and iv == iv:  # Not NaN
                delta = compute_delta(
                    underlying_price=entry_price,
                    strike=strike,
                    dte_years=dte_years,
                    risk_free_rate=risk_free_rate,
                    sigma=iv,
                    option_type=contract_type,
                )
                if abs(delta) < criteria.delta_min or abs(delta) > criteria.delta_max:
                    continue

        score = _compute_candidate_score(
            delta=delta,
            target_delta=target_delta,
            oi=oi,
            spread_pct=None,  # No bid/ask from Alpaca trading API
            iv=iv if (iv and iv == iv) else None,
        )

        candidates.append({
            "contract": contract,
            "strike": strike,
            "dte": dte,
            "expiration": exp,
            "premium": close_price,
            "oi": oi,
            "delta": delta,
            "score": score,
        })

    if not candidates:
        raise NoContractFoundError(
            f"No {contract_type} contracts for {underlying} passed filters "
            f"(OI >= {criteria.min_open_interest}, "
            f"volume >= {criteria.min_volume}, "
            f"price {criteria.min_price}-{criteria.max_price}, "
            f"delta {criteria.delta_min}-{criteria.delta_max})"
        )

    # Sort by composite score (lower is better)
    candidates.sort(key=lambda c: c["score"])
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
        "Selected options contract (Alpaca-only)",
        correlation_id,
        contract=selected.symbol,
        strike=selected.strike,
        dte=selected.dte,
        premium=selected.premium,
        oi=selected.open_interest,
        delta=best.get("delta"),
    )

    return selected


# ---------------------------------------------------------------------------
# Main entry point (with Yahoo fallback)
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

    When ``YAHOO_ENABLED=true`` (default), uses Yahoo Finance for
    richer market data and real Greeks computation.  Falls back to
    Alpaca-only screening if Yahoo is disabled or unavailable.

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

    yahoo_enabled = os.environ.get("YAHOO_ENABLED", "true").lower() == "true"

    if yahoo_enabled:
        try:
            return screen_with_yahoo(
                alpaca_client=client,
                underlying=underlying,
                side=side,
                entry_price=entry_price,
                criteria=criteria,
                correlation_id=correlation_id,
            )
        except NoContractFoundError:
            raise
        except Exception as e:
            # Yahoo unavailable -- fall back to Alpaca-only with warning
            log_structured(
                logger, logging.WARNING,
                f"Yahoo screening failed, falling back to Alpaca-only: {e}",
                correlation_id,
                underlying=underlying,
            )

    return _screen_alpaca_only(
        client=client,
        underlying=underlying,
        side=side,
        entry_price=entry_price,
        criteria=criteria,
        correlation_id=correlation_id,
    )
