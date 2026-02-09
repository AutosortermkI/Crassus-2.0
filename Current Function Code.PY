mport os
import logging
import azure.functions as func

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.common.exceptions import APIError


# --------------------------------------------------
# App settings (Environment Variables)
# --------------------------------------------------
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# REQUIRED token that must be present in TradingView JSON payload
WEBHOOK_AUTH_TOKEN = os.environ["WEBHOOK_AUTH_TOKEN"]

# Percent defaults for bracket legs (used when TradingView sends nothing extra)
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "1.00"))   # 1.00 = 1%
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.75"))       # 0.75 = 0.75%
STOP_LIMIT_PCT = float(os.environ.get("STOP_LIMIT_PCT", "0.85"))     # 0.85 = 0.85%

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="trade", methods=["POST"])
def trade(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Processing trade request...")
    try:
        data = req.get_json()

        symbol = data.get("symbol")
        side = data.get("side")  # "buy" or "sell"
        qty = data.get("qty")
        price = data.get("price")  # parent limit price

        token = data.get("token")

        if symbol is None or side is None or qty is None or price is None or token is None:
            return func.HttpResponse(
                "Missing required parameters: symbol, side, qty, price, token",
                status_code=400
            )

        if token != WEBHOOK_AUTH_TOKEN:
            return func.HttpResponse(
                "Unauthorized: Invalid token.",
                status_code=401
            )

        side_str = str(side).lower()
        if side_str not in ("buy", "sell"):
            return func.HttpResponse("Invalid side. Must be 'buy' or 'sell'.", status_code=400)

        qty = float(qty)
        entry_price = float(price)

        is_buy = side_str == "buy"
        order_side = OrderSide.BUY if is_buy else OrderSide.SELL

        # Optional explicit values (if you ever decide to send them later)
        take_profit_price = data.get("take_profit_price")
        stop_price = data.get("stop_price")
        stop_limit_price = data.get("stop_limit_price")

        # If any explicit value is missing, compute defaults from env percent settings
        tp_mult = TAKE_PROFIT_PCT / 100.0
        sl_mult = STOP_LOSS_PCT / 100.0
        sl_limit_mult = STOP_LIMIT_PCT / 100.0

        if is_buy:
            default_tp = entry_price * (1 + tp_mult)
            default_stop = entry_price * (1 - sl_mult)
            default_stop_limit = entry_price * (1 - sl_limit_mult)
        else:
            default_tp = entry_price * (1 - tp_mult)
            default_stop = entry_price * (1 + sl_mult)
            default_stop_limit = entry_price * (1 + sl_limit_mult)

        take_profit_price = float(take_profit_price) if take_profit_price is not None else default_tp
        stop_price = float(stop_price) if stop_price is not None else default_stop
        stop_limit_price = float(stop_limit_price) if stop_limit_price is not None else default_stop_limit

        # Round to cents (adjust if you trade assets needing different increments)
        take_profit_price = round(take_profit_price, 2)
        stop_price = round(stop_price, 2)
        stop_limit_price = round(stop_limit_price, 2)
        entry_price = round(entry_price, 2)

        logging.info(
            "Order | Symbol=%s Side=%s Qty=%s Entry=%.2f TP=%.2f Stop=%.2f StopLimit=%.2f Paper=%s",
            symbol, side_str, qty, entry_price, take_profit_price, stop_price, stop_limit_price, ALPACA_PAPER
        )

        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            limit_price=entry_price,
            side=order_side,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price, limit_price=stop_limit_price),
        )

        order = trading_client.submit_order(order_data=order_request)
        logging.info("Order submitted: %s", order)

        return func.HttpResponse(
            f"Order processed successfully. Order ID: {order.id}",
            status_code=200
        )

    except APIError as e:
        logging.exception("Alpaca API error")
        return func.HttpResponse(f"Alpaca API error: {str(e)}", status_code=502)

    except Exception as e:
        logging.exception("Error processing order")
        return func.HttpResponse(f"Error processing order: {str(e)}", status_code=500)
