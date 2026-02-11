"""
Crassus 2.0 -- Webhook content parser.

Parses the multi-line ``content`` field from TradingView webhook alerts
into a structured signal object.

Expected format (whitespace / blank lines tolerated)::

    **New Buy Signal:**
    AAPL 5 Min Candle
    Strategy: bollinger_mean_reversion
    Mode: stock
    Volume: 1234567
    Price: 150.25
    Time: 2024-01-15T10:30:00Z

Required fields: side (inferred from header), ticker, strategy, price.
Optional fields: mode (default ``stock``), volume, time.

Extension points:
  - Add new line patterns by extending the regex set
  - Support additional signal types beyond Buy / Sell
"""

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParsedSignal:
    """Structured representation of a parsed TradingView webhook signal."""

    ticker: str
    side: str                          # "buy" or "sell"
    strategy: str
    price: float
    mode: str = "stock"                # "stock" or "options"
    volume: Optional[float] = None
    time: Optional[str] = None


class ParseError(Exception):
    """Raised when webhook content cannot be parsed into a valid signal."""


# ---------------------------------------------------------------------------
# Compiled regex patterns (built once at module load)
# ---------------------------------------------------------------------------

# Side detection: look for "Buy Signal" or "Sell Signal" anywhere in text
_SIDE_RE = re.compile(r"\b(buy|sell)\s+signal\b", re.IGNORECASE)

# Key-value lines: "Label: value" with optional surrounding whitespace
_STRATEGY_RE = re.compile(r"^\s*strategy\s*:\s*(.+)", re.IGNORECASE | re.MULTILINE)
_MODE_RE     = re.compile(r"^\s*mode\s*:\s*(\S+)",   re.IGNORECASE | re.MULTILINE)
_PRICE_RE    = re.compile(r"^\s*price\s*:\s*([\d.]+)", re.IGNORECASE | re.MULTILINE)
_VOLUME_RE   = re.compile(r"^\s*volume\s*:\s*([\d.]+)", re.IGNORECASE | re.MULTILINE)
_TIME_RE     = re.compile(r"^\s*time\s*:\s*(.+)",     re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_webhook_content(content: str) -> ParsedSignal:
    """Parse TradingView webhook *content* string into a structured signal.

    Args:
        content: The multi-line string from the webhook's ``content`` field.

    Returns:
        A :class:`ParsedSignal` with all extracted fields.

    Raises:
        ParseError: If any required field (side, ticker, strategy, price)
                    is missing or unparseable.
    """
    if not content or not content.strip():
        raise ParseError("Empty webhook content")

    # --- Side (buy / sell) ---
    side_match = _SIDE_RE.search(content)
    if not side_match:
        raise ParseError(
            "Cannot determine side: expected 'Buy Signal' or 'Sell Signal' in content"
        )
    side = side_match.group(1).lower()

    # --- Ticker ---
    ticker = _extract_ticker(content)
    if not ticker:
        raise ParseError("Cannot extract ticker symbol from content")

    # --- Strategy ---
    strategy_match = _STRATEGY_RE.search(content)
    if not strategy_match:
        raise ParseError("Missing 'Strategy:' line in webhook content")
    strategy = strategy_match.group(1).strip().lower()

    # --- Price ---
    price_match = _PRICE_RE.search(content)
    if not price_match:
        raise ParseError("Missing 'Price:' line in webhook content")
    try:
        price = float(price_match.group(1))
    except ValueError:
        raise ParseError(f"Invalid price value: {price_match.group(1)}")

    # --- Mode (optional, default "stock") ---
    mode = "stock"
    mode_match = _MODE_RE.search(content)
    if mode_match:
        mode = mode_match.group(1).strip().lower()
    if mode not in ("stock", "options"):
        raise ParseError(f"Invalid mode '{mode}': expected 'stock' or 'options'")

    # --- Volume (optional) ---
    volume: Optional[float] = None
    volume_match = _VOLUME_RE.search(content)
    if volume_match:
        try:
            volume = float(volume_match.group(1))
        except ValueError:
            pass  # Ignore unparseable volume; it's optional

    # --- Time (optional) ---
    time_str: Optional[str] = None
    time_match = _TIME_RE.search(content)
    if time_match:
        time_str = time_match.group(1).strip()

    return ParsedSignal(
        ticker=ticker,
        side=side,
        strategy=strategy,
        price=price,
        mode=mode,
        volume=volume,
        time=time_str,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _extract_ticker(content: str) -> Optional[str]:
    """Extract the ticker symbol from the content.

    Heuristic: scan lines after the signal header for the first word
    that looks like a US equity ticker (1-5 uppercase letters).
    Falls back to the first uppercase word in the entire content.
    """
    lines = content.splitlines()
    found_signal_line = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip the signal header line itself
        if _SIDE_RE.search(stripped):
            found_signal_line = True
            continue

        # After the signal line, the next content line should start with ticker
        if found_signal_line:
            # Skip key-value lines (Strategy:, Mode:, etc.)
            if re.match(r"^\s*\w+\s*:", stripped) and not stripped.startswith("*"):
                continue
            match = re.match(r"([A-Z]{1,5})\b", stripped)
            if match:
                return match.group(1)

    # Fallback: first uppercase word in content
    fallback = re.search(r"\b([A-Z]{1,5})\b", content)
    return fallback.group(1) if fallback else None
