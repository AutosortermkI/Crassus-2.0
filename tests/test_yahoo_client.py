"""
Tests for Yahoo Finance client.

All tests are pure (no network calls). Yahoo API interactions are mocked.
"""
import math
from datetime import date, datetime
from unittest.mock import patch, MagicMock, PropertyMock

import pytest
from yahoo_client import (
    YahooCrumbClient,
    YahooOptionContract,
    YahooOptionChain,
    YahooCrumbError,
    YahooClientError,
    YahooDataError,
    pick_expiration,
    get_option_chain,
    get_expirations,
    _safe_float,
    _safe_int,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_yahoo_response():
    """Sample Yahoo Finance options API response."""
    return {
        "finance": {
            "result": [{
                "expirationDates": [1700000000, 1700600000, 1701200000],
                "options": [{
                    "calls": [
                        {
                            "contractSymbol": "AAPL240215C00150000",
                            "strike": 150.0,
                            "lastPrice": 5.20,
                            "bid": 5.10,
                            "ask": 5.30,
                            "volume": 1500,
                            "openInterest": 5000,
                            "impliedVolatility": 0.25,
                        },
                        {
                            "contractSymbol": "AAPL240215C00160000",
                            "strike": 160.0,
                            "lastPrice": 2.10,
                            "bid": 2.00,
                            "ask": 2.20,
                            "volume": 800,
                            "openInterest": 3000,
                            "impliedVolatility": 0.30,
                        },
                    ],
                    "puts": [
                        {
                            "contractSymbol": "AAPL240215P00140000",
                            "strike": 140.0,
                            "lastPrice": 3.50,
                            "bid": 3.40,
                            "ask": 3.60,
                            "volume": 900,
                            "openInterest": 4000,
                            "impliedVolatility": 0.28,
                        },
                    ],
                }],
            }],
            "error": None,
        }
    }


@pytest.fixture
def mock_expirations_response():
    """Sample Yahoo Finance expirations response."""
    return {
        "finance": {
            "result": [{
                "expirationDates": [1700000000, 1700600000, 1701200000],
            }],
            "error": None,
        }
    }


# ---------------------------------------------------------------------------
# YahooCrumbClient
# ---------------------------------------------------------------------------

class TestYahooCrumbClient:
    """YahooCrumbClient session and crumb management."""

    def test_init_creates_session(self):
        """Client should initialize with a session."""
        client = YahooCrumbClient()
        assert client._crumb is None
        assert client._retry_count > 0

    @patch.object(YahooCrumbClient, "refresh_credentials")
    def test_get_json_refreshes_on_first_call(self, mock_refresh):
        """First get_json call should trigger credential refresh."""
        client = YahooCrumbClient()
        client._crumb = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": "test"}
        client._session = MagicMock()
        client._session.get.return_value = mock_response

        # Simulate refresh setting the crumb
        def set_crumb(*args, **kwargs):
            client._crumb = "test_crumb"
        mock_refresh.side_effect = set_crumb

        result = client.get_json("https://example.com/api")
        mock_refresh.assert_called_once()
        assert result == {"data": "test"}

    def test_get_json_retries_on_401_invalid_crumb(self):
        """Should refresh crumb and retry on 401 Invalid Crumb."""
        client = YahooCrumbClient(retry_count=3, backoff_base=1)
        client._crumb = "old_crumb"

        # First call: 401 Invalid Crumb; second call: 200
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.text = "Invalid Crumb"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}

        client._session = MagicMock()
        client._session.get.side_effect = [resp_401, resp_200]

        with patch.object(client, "refresh_credentials"):
            result = client.get_json("https://example.com/api")
        assert result == {"ok": True}

    @patch("yahoo_client.time.sleep")
    def test_get_json_retries_on_429(self, mock_sleep):
        """Should back off and retry on 429 rate limiting."""
        client = YahooCrumbClient(retry_count=3, backoff_base=2)
        client._crumb = "test_crumb"

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Too Many Requests"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}

        client._session = MagicMock()
        client._session.get.side_effect = [resp_429, resp_200]

        result = client.get_json("https://example.com/api")
        assert result == {"ok": True}
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1

    @patch("yahoo_client.time.sleep")
    def test_get_json_exhausts_retries(self, mock_sleep):
        """Should raise after exhausting all retries."""
        client = YahooCrumbClient(retry_count=2, backoff_base=1)
        client._crumb = "test_crumb"

        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.text = "Internal Server Error"

        client._session = MagicMock()
        client._session.get.return_value = resp_500

        with pytest.raises(YahooClientError, match="failed after 2 retries"):
            client.get_json("https://example.com/api")


# ---------------------------------------------------------------------------
# pick_expiration
# ---------------------------------------------------------------------------

class TestPickExpiration:
    """Expiration date selection logic."""

    def test_empty_list_raises(self):
        """Empty expiration list should raise."""
        with pytest.raises(YahooDataError, match="No expirations"):
            pick_expiration([])

    @patch("yahoo_client.datetime")
    def test_picks_0dte_when_available(self, mock_dt):
        """Should prefer today's expiration (0DTE)."""
        # Set "today" to a known date
        mock_now = MagicMock()
        mock_now.date.return_value = date(2024, 1, 15)
        mock_dt.now.return_value = mock_now
        mock_dt.fromtimestamp = datetime.fromtimestamp

        # Create timestamps: one for today, one for tomorrow
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        today_ts = int(datetime(2024, 1, 15, 12, 0, tzinfo=tz).timestamp())
        tomorrow_ts = int(datetime(2024, 1, 16, 12, 0, tzinfo=tz).timestamp())

        result = pick_expiration([tomorrow_ts, today_ts])
        assert result == today_ts

    @patch("yahoo_client.datetime")
    def test_picks_nearest_future(self, mock_dt):
        """Without 0DTE, should pick the nearest future expiration."""
        mock_now = MagicMock()
        mock_now.date.return_value = date(2024, 1, 14)  # Sunday
        mock_dt.now.return_value = mock_now
        mock_dt.fromtimestamp = datetime.fromtimestamp

        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        ts1 = int(datetime(2024, 1, 15, 12, 0, tzinfo=tz).timestamp())  # Monday
        ts2 = int(datetime(2024, 1, 22, 12, 0, tzinfo=tz).timestamp())  # Next Monday

        result = pick_expiration([ts2, ts1])
        assert result == ts1  # Nearest future

    @patch("yahoo_client.datetime")
    def test_fallback_to_last_known(self, mock_dt):
        """If all expirations are in the past, pick the last one."""
        mock_now = MagicMock()
        mock_now.date.return_value = date(2024, 2, 1)
        mock_dt.now.return_value = mock_now
        mock_dt.fromtimestamp = datetime.fromtimestamp

        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        ts1 = int(datetime(2024, 1, 15, 12, 0, tzinfo=tz).timestamp())
        ts2 = int(datetime(2024, 1, 22, 12, 0, tzinfo=tz).timestamp())

        result = pick_expiration([ts1, ts2])
        assert result == ts2  # Last known


# ---------------------------------------------------------------------------
# get_option_chain
# ---------------------------------------------------------------------------

class TestGetOptionChain:
    """Option chain retrieval from Yahoo."""

    def test_parses_calls_and_puts(self, mock_yahoo_response):
        """Should parse both calls and puts from response."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = mock_yahoo_response

        chain = get_option_chain(client, "AAPL", 1700000000)

        assert isinstance(chain, YahooOptionChain)
        assert chain.underlying == "AAPL"
        assert len(chain.contracts) == 3  # 2 calls + 1 put

    def test_call_contract_fields(self, mock_yahoo_response):
        """Call contract should have all fields populated."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = mock_yahoo_response

        chain = get_option_chain(client, "AAPL", 1700000000)
        call = [c for c in chain.contracts if c.option_type == "call"][0]

        assert call.contract_symbol == "AAPL240215C00150000"
        assert call.strike == 150.0
        assert call.last_price == 5.20
        assert call.bid == 5.10
        assert call.ask == 5.30
        assert call.volume == 1500
        assert call.open_interest == 5000
        assert call.implied_volatility == 0.25

    def test_put_contract_fields(self, mock_yahoo_response):
        """Put contract should have correct type and fields."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = mock_yahoo_response

        chain = get_option_chain(client, "AAPL", 1700000000)
        puts = [c for c in chain.contracts if c.option_type == "put"]

        assert len(puts) == 1
        assert puts[0].strike == 140.0

    def test_error_response_raises(self):
        """API error in response should raise YahooDataError."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = {
            "finance": {
                "result": None,
                "error": {"code": "Not Found", "description": "No data"},
            }
        }

        with pytest.raises(YahooDataError, match="Yahoo API error"):
            get_option_chain(client, "INVALID", 1700000000)

    def test_empty_result_raises(self):
        """Empty result list should raise YahooDataError."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = {
            "finance": {"result": [], "error": None}
        }

        with pytest.raises(YahooDataError, match="No result"):
            get_option_chain(client, "AAPL", 1700000000)

    def test_no_options_raises(self):
        """Missing options key should raise YahooDataError."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = {
            "finance": {
                "result": [{"options": []}],
                "error": None,
            }
        }

        with pytest.raises(YahooDataError, match="No options chain"):
            get_option_chain(client, "AAPL", 1700000000)


# ---------------------------------------------------------------------------
# get_expirations
# ---------------------------------------------------------------------------

class TestGetExpirations:
    """Expiration dates retrieval."""

    def test_returns_expiration_list(self, mock_expirations_response):
        """Should return list of Unix timestamps."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = mock_expirations_response

        result = get_expirations(client, "AAPL")
        assert result == [1700000000, 1700600000, 1701200000]

    def test_empty_expirations_raises(self):
        """Empty expirations should raise YahooDataError."""
        client = MagicMock(spec=YahooCrumbClient)
        client.get_json.return_value = {
            "finance": {"result": [{"expirationDates": []}]}
        }

        with pytest.raises(YahooDataError, match="No expirations"):
            get_expirations(client, "AAPL")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestSafeConversions:
    """Safe type conversion helpers."""

    def test_safe_float_valid(self):
        assert _safe_float(3.14) == 3.14

    def test_safe_float_string(self):
        assert _safe_float("2.5") == 2.5

    def test_safe_float_none(self):
        assert _safe_float(None) == 0.0

    def test_safe_float_invalid(self):
        assert _safe_float("not_a_number") == 0.0

    def test_safe_int_valid(self):
        assert _safe_int(42) == 42

    def test_safe_int_string(self):
        assert _safe_int("100") == 100

    def test_safe_int_none(self):
        assert _safe_int(None) == 0

    def test_safe_int_invalid(self):
        assert _safe_int("abc") == 0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class TestYahooOptionContract:
    """YahooOptionContract dataclass."""

    def test_create_contract(self):
        """Should create a contract with all fields."""
        contract = YahooOptionContract(
            contract_symbol="AAPL240215C00150000",
            option_type="call",
            strike=150.0,
            expiration=date(2024, 2, 15),
            last_price=5.20,
            bid=5.10,
            ask=5.30,
            volume=1500,
            open_interest=5000,
            implied_volatility=0.25,
        )
        assert contract.contract_symbol == "AAPL240215C00150000"
        assert contract.option_type == "call"
        assert contract.strike == 150.0
