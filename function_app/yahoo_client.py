"""
Crassus 2.0 -- Yahoo Finance market data client.

Ported from the ``fishpussy7.py`` (v7) project's ``YahooCrumbClient``.

Provides:
  - Cookie + crumb authenticated Yahoo Finance API access
  - Option chain retrieval with bid/ask/IV/volume/OI data
  - Intelligent expiration selection (0DTE or nearest future)
  - Automatic crumb refresh on 401 / "Invalid Crumb" responses
  - Exponential backoff on transient errors (429, 5xx)

Yahoo Finance is used as the **market data** source for options screening
because Alpaca's trading API returns limited price data (no real-time
bid/ask/IV for options).  Alpaca remains the execution venue.

Configuration (environment variables):
  - ``YAHOO_ENABLED``: Toggle Yahoo data source (default: ``true``)
  - ``YAHOO_RETRY_COUNT``: Max retries for Yahoo API (default: ``5``)
  - ``YAHOO_BACKOFF_BASE``: Exponential backoff base in seconds (default: ``2``)

Extension points:
  - Add quote snapshots for underlying price
  - Historical options data for IV rank / percentile
  - Earnings calendar integration
"""

import os
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests

from utils import log_structured, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TZ = ZoneInfo("America/New_York")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Environment-driven configuration
YAHOO_ENABLED = os.environ.get("YAHOO_ENABLED", "true").lower() == "true"
YAHOO_RETRY_COUNT = int(os.environ.get("YAHOO_RETRY_COUNT", "5"))
YAHOO_BACKOFF_BASE = int(os.environ.get("YAHOO_BACKOFF_BASE", "2"))


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class YahooClientError(Exception):
    """Base exception for Yahoo Finance client errors."""


class YahooCrumbError(YahooClientError):
    """Raised when crumb acquisition fails."""


class YahooDataError(YahooClientError):
    """Raised when the response data is missing or malformed."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class YahooOptionContract:
    """A single options contract from Yahoo Finance.

    Contains richer data than Alpaca's trading API: bid, ask, IV, volume.
    """

    contract_symbol: str       # OCC-style symbol
    option_type: str           # "call" or "put"
    strike: float
    expiration: date
    last_price: float
    bid: float
    ask: float
    volume: int
    open_interest: int
    implied_volatility: float  # Yahoo-provided IV (annualized)


@dataclass
class YahooOptionChain:
    """Complete option chain for a single expiration date.

    Attributes:
        underlying: Ticker symbol.
        expiration: Expiration date.
        contracts: List of call and put contracts.
    """

    underlying: str
    expiration: date
    contracts: List[YahooOptionContract] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Yahoo Finance crumb client
# ---------------------------------------------------------------------------

class YahooCrumbClient:
    """Yahoo Finance API client with cookie/crumb authentication.

    Yahoo's options endpoints require a valid cookie + crumb pair.
    This client fetches those once, caches them, and refreshes
    automatically on ``Invalid Crumb`` / 401 responses.

    Args:
        retry_count: Maximum number of retries for API requests.
        backoff_base: Base for exponential backoff (seconds).
    """

    def __init__(
        self,
        retry_count: int = YAHOO_RETRY_COUNT,
        backoff_base: int = YAHOO_BACKOFF_BASE,
    ):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._crumb: Optional[str] = None
        self._retry_count = retry_count
        self._backoff_base = backoff_base

    def refresh_credentials(self, correlation_id: str = "") -> None:
        """Fetch a fresh cookie + crumb pair from Yahoo Finance.

        Args:
            correlation_id: For log tracing.

        Raises:
            YahooCrumbError: If crumb cannot be obtained.
        """
        log_structured(
            logger, logging.DEBUG,
            "Refreshing Yahoo crumb credentials",
            correlation_id,
        )

        # Step 1: hit fc.yahoo.com to get cookies in the session
        self._session.cookies.clear()
        self._session.get("https://fc.yahoo.com", allow_redirects=True, timeout=15)

        # Step 2: fetch crumb (try query1 then query2)
        crumb_urls = [
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
        ]

        crumb = None
        for url in crumb_urls:
            r = self._session.get(url, allow_redirects=True, timeout=15)
            txt = (r.text or "").strip()
            if r.status_code == 200 and txt and "Too Many" not in txt and "Invalid" not in txt:
                crumb = txt
                break

        if not crumb:
            raise YahooCrumbError(
                "Could not obtain Yahoo crumb. Yahoo may be blocking requests."
            )

        self._crumb = crumb
        log_structured(
            logger, logging.DEBUG,
            "Yahoo crumb refreshed successfully",
            correlation_id,
        )

    def get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        correlation_id: str = "",
    ) -> dict:
        """Make an authenticated GET request to Yahoo Finance.

        Handles crumb injection, automatic crumb refresh on 401,
        and exponential backoff on transient errors.

        Args:
            url: The Yahoo Finance API endpoint URL.
            params: Optional query parameters.
            correlation_id: For log tracing.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            YahooClientError: If all retries are exhausted.
        """
        if not self._crumb:
            self.refresh_credentials(correlation_id)

        params = dict(params or {})
        params["crumb"] = self._crumb

        last_response = None
        for attempt in range(self._retry_count):
            r = self._session.get(url, params=params, timeout=20)
            last_response = r

            # Invalid crumb -> refresh and retry immediately
            if r.status_code == 401 and "Invalid Crumb" in (r.text or ""):
                log_structured(
                    logger, logging.WARNING,
                    "Yahoo crumb expired, refreshing",
                    correlation_id,
                    attempt=attempt,
                )
                self.refresh_credentials(correlation_id)
                params["crumb"] = self._crumb
                continue

            # Transient errors -> exponential backoff
            if r.status_code in (429, 500, 502, 503, 504):
                wait = self._backoff_base ** attempt
                log_structured(
                    logger, logging.WARNING,
                    f"Yahoo transient error {r.status_code}, backing off {wait}s",
                    correlation_id,
                    attempt=attempt,
                    status_code=r.status_code,
                )
                time.sleep(wait)
                continue

            r.raise_for_status()
            return r.json()

        # All retries exhausted
        status = getattr(last_response, "status_code", None)
        body = (getattr(last_response, "text", "") or "")[:200]
        raise YahooClientError(
            f"Yahoo request failed after {self._retry_count} retries. "
            f"Last status={status} body={body}"
        )


# ---------------------------------------------------------------------------
# Expiration selection
# ---------------------------------------------------------------------------

def pick_expiration(expiration_unix_list: List[int]) -> int:
    """Pick the best expiration from a list of Unix timestamps.

    Strategy:
      1. Prefer 0DTE (today's date) if available.
      2. Otherwise pick the nearest future expiration.
      3. Fallback to the last known expiration.

    Args:
        expiration_unix_list: List of expiration dates as Unix timestamps.

    Returns:
        Selected expiration as a Unix timestamp.

    Raises:
        YahooDataError: If the expiration list is empty.
    """
    if not expiration_unix_list:
        raise YahooDataError("No expirations returned for this ticker.")

    today = datetime.now(TZ).date()

    # Map unix -> date in Eastern time
    mapped = [(u, datetime.fromtimestamp(u, TZ).date()) for u in expiration_unix_list]

    # Prefer 0DTE if available
    same_day = [u for (u, d) in mapped if d == today]
    if same_day:
        return min(same_day)

    # Otherwise, next future expiration
    future = [(u, d) for (u, d) in mapped if d > today]
    if future:
        future.sort(key=lambda x: x[1])
        return future[0][0]

    # Fallback: last known expiration
    mapped.sort(key=lambda x: x[1])
    return mapped[-1][0]


# ---------------------------------------------------------------------------
# Option chain retrieval
# ---------------------------------------------------------------------------

def get_option_chain(
    client: YahooCrumbClient,
    ticker: str,
    expiration_unix: int,
    correlation_id: str = "",
) -> YahooOptionChain:
    """Fetch an option chain from Yahoo Finance for a specific expiration.

    Args:
        client: Authenticated :class:`YahooCrumbClient`.
        ticker: Underlying ticker symbol (e.g. ``"AAPL"``).
        expiration_unix: Expiration date as Unix timestamp.
        correlation_id: For log tracing.

    Returns:
        :class:`YahooOptionChain` with all call and put contracts.

    Raises:
        YahooDataError: If the response is missing or malformed.
    """
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}"

    log_structured(
        logger, logging.INFO,
        "Fetching Yahoo option chain",
        correlation_id,
        ticker=ticker,
        expiration_unix=expiration_unix,
    )

    data = client.get_json(url, params={"date": expiration_unix}, correlation_id=correlation_id)

    finance = data.get("finance", {})
    if finance.get("error"):
        raise YahooDataError(f"Yahoo API error: {finance['error']}")

    result = (finance.get("result") or [None])[0]
    if not result:
        raise YahooDataError("No result returned for options request.")

    options = (result.get("options") or [None])[0]
    if not options:
        raise YahooDataError("No options chain returned for this expiration.")

    exp_date = datetime.fromtimestamp(expiration_unix, TZ).date()

    contracts: List[YahooOptionContract] = []

    for opt_type, key in [("call", "calls"), ("put", "puts")]:
        raw_list = options.get(key) or []
        for raw in raw_list:
            contract = YahooOptionContract(
                contract_symbol=raw.get("contractSymbol", ""),
                option_type=opt_type,
                strike=_safe_float(raw.get("strike")),
                expiration=exp_date,
                last_price=_safe_float(raw.get("lastPrice")),
                bid=_safe_float(raw.get("bid")),
                ask=_safe_float(raw.get("ask")),
                volume=_safe_int(raw.get("volume")),
                open_interest=_safe_int(raw.get("openInterest")),
                implied_volatility=_safe_float(raw.get("impliedVolatility")),
            )
            contracts.append(contract)

    log_structured(
        logger, logging.INFO,
        f"Fetched {len(contracts)} contracts from Yahoo",
        correlation_id,
        ticker=ticker,
        expiration=exp_date.isoformat(),
    )

    return YahooOptionChain(
        underlying=ticker,
        expiration=exp_date,
        contracts=contracts,
    )


def get_expirations(
    client: YahooCrumbClient,
    ticker: str,
    correlation_id: str = "",
) -> List[int]:
    """Fetch available expiration dates for a ticker from Yahoo Finance.

    Args:
        client: Authenticated :class:`YahooCrumbClient`.
        ticker: Underlying ticker symbol.
        correlation_id: For log tracing.

    Returns:
        List of expiration dates as Unix timestamps.

    Raises:
        YahooDataError: If no expirations are returned.
    """
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}"

    data = client.get_json(url, correlation_id=correlation_id)

    finance = data.get("finance", {})
    result = (finance.get("result") or [None])[0]
    expirations = (result or {}).get("expirationDates") or []

    if not expirations:
        raise YahooDataError(
            f"No expirations returned for {ticker}. "
            "Yahoo may be blocking options data."
        )

    log_structured(
        logger, logging.INFO,
        f"Found {len(expirations)} expirations for {ticker}",
        correlation_id,
        ticker=ticker,
    )

    return expirations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value) -> float:
    """Convert a value to float, defaulting to 0.0 on failure."""
    try:
        return float(value) if value is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def _safe_int(value) -> int:
    """Convert a value to int, defaulting to 0 on failure."""
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return 0
