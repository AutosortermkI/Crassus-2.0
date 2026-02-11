"""
Crassus 2.0 -- Azure Function entry point.

HTTP trigger that receives TradingView webhook alerts and routes them
to stock or options bracket-order logic.

Flow:
  1. Authenticate via ``X-Webhook-Token`` header
  2. Parse the TradingView ``content`` string
  3. Look up strategy configuration
  4. Route to stock or options order path
  5. Return structured JSON HTTP response

Endpoint: ``POST /api/trade``
"""

import os
import json
import logging

import azure.functions as func
from alpaca.trading.client import TradingClient
from alpaca.common.exceptions import APIError

from parser import parse_webhook_content, ParseError
from strategy import (
    get_strategy,
    compute_stock_bracket_prices,
    compute_options_exit_prices,
    UnknownStrategyError,
)
from stock_orders import StockBracketParams, submit_stock_order
from options_screener import (
    screen_option_contracts,
    NoContractFoundError,
)
from options_orders import OptionsOrderParams, submit_options_entry_order
from risk import compute_options_qty, compute_stock_qty, get_max_dollar_risk
from utils import (
    generate_correlation_id,
    log_structured,
    get_logger,
    round_stock_price,
    round_options_price,
)

logger = get_logger(__name__)

# ------------------------------------------------------------------
# App settings (Environment Variables)
# ------------------------------------------------------------------
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
WEBHOOK_AUTH_TOKEN = os.environ.get("WEBHOOK_AUTH_TOKEN", "")

# Alpaca client -- reused across requests for connection pooling.
# Module-level init runs once per Azure Functions cold start.
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# ======================================================================
# HTTP Trigger
# ======================================================================

@app.route(route="trade", methods=["POST"])
def trade(req: func.HttpRequest) -> func.HttpResponse:
    """Main webhook handler: authenticate -> parse -> route -> order."""

    correlation_id = generate_correlation_id()
    log_structured(logger, logging.INFO, "Received trade request", correlation_id)

    # ----------------------------------------------------------------
    # 1. Authentication: validate X-Webhook-Token header
    # ----------------------------------------------------------------
    token = req.headers.get("X-Webhook-Token", "")
    if not token or token != WEBHOOK_AUTH_TOKEN:
        log_structured(logger, logging.WARNING, "Unauthorized request", correlation_id)
        return _json_response({"error": "Unauthorized", "correlation_id": correlation_id}, 401)

    # ----------------------------------------------------------------
    # 2. Parse webhook body
    # ----------------------------------------------------------------
    try:
        data = req.get_json()
    except ValueError:
        log_structured(logger, logging.WARNING, "Invalid JSON body", correlation_id)
        return _json_response({"error": "Invalid JSON", "correlation_id": correlation_id}, 400)

    content = data.get("content", "")
    if not content:
        log_structured(logger, logging.WARNING, "Missing content field", correlation_id)
        return _json_response({"error": "Missing 'content' field", "correlation_id": correlation_id}, 400)

    try:
        signal = parse_webhook_content(content)
    except ParseError as e:
        log_structured(logger, logging.WARNING, f"Parse error: {e}", correlation_id)
        return _json_response({"error": str(e), "correlation_id": correlation_id}, 400)

    log_structured(
        logger, logging.INFO, "Signal parsed", correlation_id,
        ticker=signal.ticker, side=signal.side,
        strategy=signal.strategy, price=signal.price, mode=signal.mode,
    )

    # ----------------------------------------------------------------
    # 3. Strategy lookup
    # ----------------------------------------------------------------
    try:
        strategy_config = get_strategy(signal.strategy)
    except UnknownStrategyError as e:
        log_structured(logger, logging.WARNING, f"Unknown strategy: {e}", correlation_id)
        return _json_response({"error": str(e), "correlation_id": correlation_id}, 400)

    # ----------------------------------------------------------------
    # 4. Route: stock or options
    # ----------------------------------------------------------------
    try:
        if signal.mode == "stock":
            return _handle_stock_order(signal, strategy_config, correlation_id)
        elif signal.mode == "options":
            return _handle_options_order(signal, strategy_config, correlation_id)
        else:
            return _json_response(
                {"error": f"Unsupported mode: {signal.mode}", "correlation_id": correlation_id},
                400,
            )

    except APIError as e:
        log_structured(
            logger, logging.ERROR, f"Alpaca API error: {e}", correlation_id,
            symbol=signal.ticker, mode=signal.mode,
        )
        return _json_response(
            {"error": f"Alpaca API error: {str(e)}", "correlation_id": correlation_id},
            502,
        )

    except Exception as e:
        log_structured(
            logger, logging.ERROR, f"Internal error: {e}", correlation_id,
            symbol=signal.ticker, mode=signal.mode,
        )
        return _json_response(
            {"error": f"Internal error: {str(e)}", "correlation_id": correlation_id},
            500,
        )


# ======================================================================
# Internal route handlers
# ======================================================================

def _handle_stock_order(signal, strategy_config, correlation_id: str) -> func.HttpResponse:
    """Process a stock bracket order."""

    # Compute bracket prices from strategy config
    tp, stop, stop_limit = compute_stock_bracket_prices(
        entry_price=signal.price,
        side=signal.side,
        config=strategy_config,
    )

    qty = compute_stock_qty()

    params = StockBracketParams(
        symbol=signal.ticker,
        side=signal.side,
        qty=qty,
        entry_price=signal.price,
        take_profit_price=tp,
        stop_price=stop,
        stop_limit_price=stop_limit,
    )

    order_id = submit_stock_order(trading_client, params, correlation_id)

    log_structured(
        logger, logging.INFO, "Stock order completed", correlation_id,
        order_id=order_id, symbol=signal.ticker,
        side=signal.side, strategy=signal.strategy,
    )

    return _json_response({
        "status": "ok",
        "mode": "stock",
        "order_id": order_id,
        "symbol": signal.ticker,
        "side": signal.side,
        "qty": qty,
        "entry_price": round_stock_price(signal.price),
        "take_profit": round_stock_price(tp),
        "stop_loss": round_stock_price(stop),
        "stop_limit": round_stock_price(stop_limit),
        "strategy": signal.strategy,
        "correlation_id": correlation_id,
    }, 200)


def _handle_options_order(signal, strategy_config, correlation_id: str) -> func.HttpResponse:
    """Process an options order with contract screening and risk sizing."""

    # 1. Screen for the best options contract
    try:
        contract = screen_option_contracts(
            client=trading_client,
            underlying=signal.ticker,
            side=signal.side,
            entry_price=signal.price,
            correlation_id=correlation_id,
        )
    except NoContractFoundError as e:
        log_structured(
            logger, logging.WARNING, f"No contract found: {e}", correlation_id,
            symbol=signal.ticker,
        )
        return _json_response(
            {"error": f"No suitable options contract: {str(e)}", "correlation_id": correlation_id},
            400,
        )

    # 2. Compute exit targets (% of premium)
    tp_price, sl_price = compute_options_exit_prices(
        premium=contract.premium,
        side=signal.side,
        config=strategy_config,
    )

    # 3. Risk sizing
    max_risk = get_max_dollar_risk()
    qty = compute_options_qty(
        max_dollar_risk=max_risk,
        stop_loss_pct=strategy_config.options_sl_pct,
        premium_price=contract.premium,
    )

    # 4. Submit entry order
    params = OptionsOrderParams(
        contract_symbol=contract.symbol,
        underlying=signal.ticker,
        side=signal.side,
        qty=qty,
        limit_price=contract.premium,
        take_profit_price=tp_price,
        stop_loss_price=sl_price,
    )

    order_id = submit_options_entry_order(trading_client, params, correlation_id)

    log_structured(
        logger, logging.INFO, "Options order completed", correlation_id,
        order_id=order_id, contract=contract.symbol,
        underlying=signal.ticker, side=signal.side, strategy=signal.strategy,
    )

    return _json_response({
        "status": "ok",
        "mode": "options",
        "order_id": order_id,
        "contract": contract.symbol,
        "underlying": signal.ticker,
        "side": signal.side,
        "qty": qty,
        "premium": round_options_price(contract.premium),
        "take_profit": round_options_price(tp_price),
        "stop_loss": round_options_price(sl_price),
        "strike": contract.strike,
        "expiration": contract.expiration.isoformat(),
        "dte": contract.dte,
        "contract_type": contract.contract_type,
        "strategy": signal.strategy,
        "max_dollar_risk": max_risk,
        "correlation_id": correlation_id,
    }, 200)


# ======================================================================
# Helpers
# ======================================================================

def _json_response(body: dict, status_code: int) -> func.HttpResponse:
    """Return an ``HttpResponse`` with JSON content type."""
    return func.HttpResponse(
        json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )
