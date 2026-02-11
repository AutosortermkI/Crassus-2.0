"""
Crassus 2.0 -- Stock bracket order construction and submission.

Builds and submits Alpaca bracket orders (limit parent + take-profit + stop-loss)
for equity / stock trades.

This module preserves the core logic from the original single-file
``function_app.py`` while adding strategy-aware bracket pricing and
structured logging.
"""

import logging
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

from utils import log_structured, round_stock_price, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class StockBracketParams:
    """All parameters needed to submit a stock bracket order."""

    symbol: str
    side: str               # "buy" or "sell"
    qty: int
    entry_price: float
    take_profit_price: float
    stop_price: float
    stop_limit_price: float


# ---------------------------------------------------------------------------
# Order construction (pure -- no side effects, easy to test)
# ---------------------------------------------------------------------------

def build_stock_bracket_order(params: StockBracketParams) -> LimitOrderRequest:
    """Build an Alpaca bracket order request for a stock trade.

    Args:
        params: :class:`StockBracketParams` with all order details.

    Returns:
        A :class:`LimitOrderRequest` configured as a ``BRACKET`` order
        with take-profit and stop-loss legs.
    """
    order_side = OrderSide.BUY if params.side == "buy" else OrderSide.SELL

    # Round all prices to cents
    entry      = round_stock_price(params.entry_price)
    tp         = round_stock_price(params.take_profit_price)
    stop       = round_stock_price(params.stop_price)
    stop_limit = round_stock_price(params.stop_limit_price)

    return LimitOrderRequest(
        symbol=params.symbol,
        qty=params.qty,
        limit_price=entry,
        side=order_side,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=tp),
        stop_loss=StopLossRequest(stop_price=stop, limit_price=stop_limit),
    )


# ---------------------------------------------------------------------------
# Submission (has side effects -- calls Alpaca API)
# ---------------------------------------------------------------------------

def submit_stock_order(
    client: TradingClient,
    params: StockBracketParams,
    correlation_id: str,
) -> str:
    """Submit a stock bracket order to Alpaca and return the order ID.

    Args:
        client: Authenticated Alpaca :class:`TradingClient`.
        params: :class:`StockBracketParams` with all order details.
        correlation_id: Request correlation ID for log tracing.

    Returns:
        The Alpaca order ID as a string.

    Raises:
        alpaca.common.exceptions.APIError: On Alpaca API errors.
    """
    log_structured(
        logger, logging.INFO,
        "Submitting stock bracket order",
        correlation_id,
        symbol=params.symbol,
        side=params.side,
        qty=params.qty,
        entry=round_stock_price(params.entry_price),
        tp=round_stock_price(params.take_profit_price),
        stop=round_stock_price(params.stop_price),
        stop_limit=round_stock_price(params.stop_limit_price),
    )

    order_request = build_stock_bracket_order(params)
    order = client.submit_order(order_data=order_request)

    log_structured(
        logger, logging.INFO,
        "Stock bracket order submitted",
        correlation_id,
        order_id=order.id,
        symbol=params.symbol,
    )

    return str(order.id)
