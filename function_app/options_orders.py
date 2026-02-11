"""
Crassus 2.0 -- Options order submission and exit management.

Submits options orders to Alpaca and manages bracket-style exits.

.. important:: Design decision -- bracket exits for options

   Alpaca does **not** support bracket orders (OTO / OCO / BRACKET order
   class) for options contracts.  Bracket orders are stock-only.

   **Current approach (v1):**
     - Submit the entry order as a simple limit order.
     - Log the computed TP / SL target prices for external monitoring.
     - A future Azure Timer Function can poll open positions and submit
       exit orders when TP or SL targets are hit.

   **Why not use bracket orders?**
     The Alpaca trading API returns an error if you submit a BRACKET-class
     order with an options symbol.  There is no server-side OCO support for
     options either.

   See :func:`monitor_options_exits` for the timer-based approach outline.

Extension points:
  - Implement a Timer Trigger Azure Function for position monitoring
  - Add trailing-stop logic for options
  - Support multi-leg strategies (spreads)
"""

import logging
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from utils import log_structured, round_options_price, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class OptionsOrderParams:
    """Parameters for an options order."""

    contract_symbol: str    # OCC symbol (e.g. "AAPL240215C00150000")
    underlying: str         # Underlying ticker
    side: str               # "buy" or "sell" (signal direction)
    qty: int                # Number of contracts
    limit_price: float      # Entry premium
    take_profit_price: float  # Target exit price (for logging / monitoring)
    stop_loss_price: float    # Stop exit price  (for logging / monitoring)


# ---------------------------------------------------------------------------
# Entry-order submission
# ---------------------------------------------------------------------------

def submit_options_entry_order(
    client: TradingClient,
    params: OptionsOrderParams,
    correlation_id: str,
) -> str:
    """Submit an options entry order to Alpaca.

    Submits a **simple limit order** (not bracket -- Alpaca does not support
    bracket orders for options).  TP / SL targets are logged for external
    monitoring.

    Args:
        client: Authenticated Alpaca :class:`TradingClient`.
        params: :class:`OptionsOrderParams` with order details.
        correlation_id: Request correlation ID for log tracing.

    Returns:
        The Alpaca order ID as a string.

    Raises:
        alpaca.common.exceptions.APIError: On Alpaca API errors.
    """
    # For options: both "buy" and "sell" signals result in *buying* an
    # option contract (calls for buy signals, puts for sell signals).
    order_side = OrderSide.BUY

    limit = round_options_price(params.limit_price)

    log_structured(
        logger, logging.INFO,
        "Submitting options entry order",
        correlation_id,
        contract=params.contract_symbol,
        underlying=params.underlying,
        side=params.side,
        qty=params.qty,
        limit_price=limit,
        tp_target=round_options_price(params.take_profit_price),
        sl_target=round_options_price(params.stop_loss_price),
    )

    order_request = LimitOrderRequest(
        symbol=params.contract_symbol,
        qty=params.qty,
        limit_price=limit,
        side=order_side,
        time_in_force=TimeInForce.DAY,  # Options typically use DAY, not GTC
    )

    order = client.submit_order(order_data=order_request)

    log_structured(
        logger, logging.INFO,
        "Options entry order submitted",
        correlation_id,
        order_id=order.id,
        contract=params.contract_symbol,
        note="Bracket exits not supported for options; monitor externally",
    )

    return str(order.id)


# ---------------------------------------------------------------------------
# Future: Timer-based options exit monitoring
# ---------------------------------------------------------------------------

def monitor_options_exits() -> None:
    """**PLACEHOLDER** -- Timer-triggered function to monitor options positions.

    Implementation plan:

    1. Query open options positions via ``client.get_all_positions()``.
    2. For each position, look up the TP / SL targets (stored in a
       persistent store like Azure Table Storage or Cosmos DB).
    3. Get the current option price via a market-data snapshot.
    4. If price >= TP target --> submit a limit sell order at TP.
    5. If price <= SL target --> submit a market sell order.
    6. Log all monitoring activity with correlation IDs.

    This function would be registered as a Timer Trigger in
    ``function_app.py``::

        @app.timer_trigger(schedule="0 */1 * * * *", ...)
        def check_options_exits(timer: func.TimerRequest):
            monitor_options_exits()

    **Not implemented yet** -- requires:

    - Persistent target storage (positions -> TP / SL mapping)
    - Market-data client setup
    - Error handling for partially filled orders
    """
    raise NotImplementedError(
        "Options exit monitoring is not yet implemented. "
        "See docstring for implementation plan."
    )
