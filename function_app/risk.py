"""
Crassus 2.0 -- Risk sizing.

Computes position size (number of contracts) for options trades
based on a fixed maximum dollar risk per trade.

Current implementation: fixed dollar risk from ``MAX_DOLLAR_RISK`` env var.
Future: percentage of account equity via ``RISK_PCT_OF_EQUITY``.

Extension points:
  - Equity-based sizing: query Alpaca account equity, apply percentage
  - Kelly criterion or other sizing models
  - Per-strategy risk overrides
  - Portfolio-level risk limits (max open positions, sector exposure)
"""

import os
from typing import Optional


def get_max_dollar_risk() -> float:
    """Return the maximum dollar risk per trade from environment.

    Default: $50 -- a conservative starting point for options.
    """
    return float(os.environ.get("MAX_DOLLAR_RISK", "50.0"))


def get_risk_pct_of_equity() -> Optional[float]:
    """Return the risk percentage of equity, if configured.

    This is a **future** feature -- not yet used in sizing calculations.
    When implemented, it will query account equity and compute::

        max_risk = equity * (pct / 100)

    Returns:
        The configured percentage, or ``None`` if not set.
    """
    val = os.environ.get("RISK_PCT_OF_EQUITY")
    if val is not None:
        return float(val)
    return None


def compute_options_qty(
    max_dollar_risk: float,
    stop_loss_pct: float,
    premium_price: float,
) -> int:
    """Compute the number of options contracts to trade.

    Formula::

        stop_distance = (stop_loss_pct / 100) * premium_price
        qty = max_dollar_risk / (stop_distance * 100)

    The x100 accounts for the options multiplier (each contract = 100 shares).

    Args:
        max_dollar_risk: Maximum dollars to risk on this trade.
        stop_loss_pct: Stop-loss as percentage of premium (e.g. 10.0 = 10 %).
        premium_price: The options premium (entry price per share).

    Returns:
        Number of contracts (integer, minimum 1).

    Examples::

        >>> compute_options_qty(50.0, 10.0, 5.00)
        1
        # stop_distance = 0.10 * 5.00 = $0.50
        # qty = 50 / (0.50 * 100) = 50 / 50 = 1

        >>> compute_options_qty(200.0, 10.0, 2.00)
        10
        # stop_distance = 0.10 * 2.00 = $0.20
        # qty = 200 / (0.20 * 100) = 200 / 20 = 10
    """
    if premium_price <= 0:
        return 1
    if stop_loss_pct <= 0:
        return 1

    stop_distance = (stop_loss_pct / 100.0) * premium_price
    if stop_distance <= 0:
        return 1

    # Options multiplier: 1 contract = 100 shares of underlying
    qty = max_dollar_risk / (stop_distance * 100.0)

    # Always trade at least 1 contract
    return max(1, int(qty))


def compute_stock_qty() -> int:
    """Return the default stock quantity per trade.

    Currently returns a fixed quantity from env (default 1).

    Extension point: integrate with equity-based sizing by replacing
    this function's body while keeping the same signature.
    """
    return int(os.environ.get("DEFAULT_STOCK_QTY", "1"))
