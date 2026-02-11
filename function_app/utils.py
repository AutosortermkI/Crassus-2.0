"""
Crassus 2.0 -- Shared utilities.

Provides:
  - Correlation ID generation for request tracing
  - Structured logging helpers
  - Price rounding functions

Extension points:
  - Add custom log formatters for different sinks (App Insights, etc.)
  - Adjust rounding for fractional-penny assets or crypto
"""

import uuid
import logging
from typing import Any


def generate_correlation_id() -> str:
    """Generate a short unique correlation ID for request tracing.

    Returns an 8-character hex string -- sufficient for log correlation
    within a single function-app instance.
    """
    return uuid.uuid4().hex[:8]


def get_logger(name: str) -> logging.Logger:
    """Return a named logger instance.

    Azure Functions captures anything sent to the root logger,
    so named loggers help filter by module in Application Insights.
    """
    return logging.getLogger(name)


def log_structured(
    logger: logging.Logger,
    level: int,
    message: str,
    correlation_id: str,
    **fields: Any,
) -> None:
    """Emit a structured log line with correlation ID and key-value fields.

    Example output::

        [abc12345] Order submitted | symbol=AAPL side=buy qty=1 entry=150.00
    """
    parts = [f"[{correlation_id}]", message]
    if fields:
        kv = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        if kv:
            parts.append("|")
            parts.append(kv)
    logger.log(level, " ".join(parts))


def round_stock_price(price: float) -> float:
    """Round a stock price to the nearest cent (2 decimal places).

    Alpaca and US equity markets quote prices in dollars and cents.
    Adjust for assets that need different precision (e.g., crypto).
    """
    return round(price, 2)


def round_options_price(price: float) -> float:
    """Round an options price to 2 decimal places.

    US-listed equity options are quoted in dollars and cents.
    Some low-priced options may trade in nickel increments;
    Alpaca handles tick-size validation server-side.
    """
    return round(price, 2)
