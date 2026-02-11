"""
Tests for the webhook content parser.
"""
import pytest
from parser import parse_webhook_content, ParseError, ParsedSignal


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestParseStockBuySignal:
    """Standard buy signal with all fields present."""

    CONTENT = (
        "**New Buy Signal:**\n"
        "AAPL 5 Min Candle\n"
        "Strategy: bollinger_mean_reversion\n"
        "Mode: stock\n"
        "Volume: 1234567\n"
        "Price: 150.25\n"
        "Time: 2024-01-15T10:30:00Z"
    )

    def test_ticker(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.ticker == "AAPL"

    def test_side(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.side == "buy"

    def test_strategy(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.strategy == "bollinger_mean_reversion"

    def test_price(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.price == pytest.approx(150.25)

    def test_mode(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.mode == "stock"

    def test_volume(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.volume == pytest.approx(1234567.0)

    def test_time(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.time == "2024-01-15T10:30:00Z"


class TestParseSellSignalOptions:
    """Sell signal in options mode, missing optional fields."""

    CONTENT = (
        "**New Sell Signal:**\n"
        "TSLA 5 Min Candle\n"
        "Strategy: lorentzian_classification\n"
        "Mode: options\n"
        "Price: 245.50"
    )

    def test_ticker(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.ticker == "TSLA"

    def test_side(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.side == "sell"

    def test_strategy(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.strategy == "lorentzian_classification"

    def test_mode(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.mode == "options"

    def test_price(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.price == pytest.approx(245.50)

    def test_volume_missing(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.volume is None

    def test_time_missing(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.time is None


class TestDefaultMode:
    """When Mode: line is absent, default to 'stock'."""

    CONTENT = (
        "**New Buy Signal:**\n"
        "MSFT 5 Min Candle\n"
        "Strategy: bollinger_mean_reversion\n"
        "Price: 380.00"
    )

    def test_mode_defaults_to_stock(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.mode == "stock"


class TestExtraWhitespace:
    """Parser handles extra blank lines and whitespace."""

    CONTENT = (
        "\n\n  **New Buy Signal:**  \n\n"
        "  GOOG  5 Min Candle  \n\n"
        "  Strategy:  bollinger_mean_reversion  \n"
        "  Mode:  stock  \n"
        "  Price:  175.50  \n\n"
    )

    def test_parses_despite_whitespace(self):
        sig = parse_webhook_content(self.CONTENT)
        assert sig.ticker == "GOOG"
        assert sig.side == "buy"
        assert sig.strategy == "bollinger_mean_reversion"
        assert sig.price == pytest.approx(175.50)


# ---------------------------------------------------------------------------
# Error / edge-case tests
# ---------------------------------------------------------------------------

class TestParseErrors:
    """Ensure proper ParseError for bad input."""

    def test_empty_content(self):
        with pytest.raises(ParseError, match="Empty"):
            parse_webhook_content("")

    def test_whitespace_only(self):
        with pytest.raises(ParseError, match="Empty"):
            parse_webhook_content("   \n  \n  ")

    def test_missing_side(self):
        content = "AAPL\nStrategy: foo\nPrice: 100"
        with pytest.raises(ParseError, match="Cannot determine side"):
            parse_webhook_content(content)

    def test_missing_strategy(self):
        content = "**New Buy Signal:**\nAAPL 5 Min\nPrice: 100"
        with pytest.raises(ParseError, match="Missing 'Strategy:'"):
            parse_webhook_content(content)

    def test_missing_price(self):
        content = "**New Buy Signal:**\nAAPL 5 Min\nStrategy: bollinger_mean_reversion"
        with pytest.raises(ParseError, match="Missing 'Price:'"):
            parse_webhook_content(content)

    def test_invalid_mode(self):
        content = (
            "**New Buy Signal:**\n"
            "AAPL 5 Min\n"
            "Strategy: bollinger_mean_reversion\n"
            "Mode: crypto\n"
            "Price: 100"
        )
        with pytest.raises(ParseError, match="Invalid mode"):
            parse_webhook_content(content)


# ---------------------------------------------------------------------------
# Example webhook payloads (documenting the expected TradingView format)
# ---------------------------------------------------------------------------

EXAMPLE_STOCK_BUY = {
    "content": (
        "**New Buy Signal:**\n"
        "AAPL 5 Min Candle\n"
        "Strategy: bollinger_mean_reversion\n"
        "Mode: stock\n"
        "Volume: 2500000\n"
        "Price: 189.50\n"
        "Time: 2024-06-15T14:30:00Z"
    )
}

EXAMPLE_STOCK_SELL = {
    "content": (
        "**New Sell Signal:**\n"
        "NVDA 5 Min Candle\n"
        "Strategy: lorentzian_classification\n"
        "Mode: stock\n"
        "Volume: 8900000\n"
        "Price: 950.00\n"
        "Time: 2024-06-15T15:00:00Z"
    )
}

EXAMPLE_OPTIONS_BUY = {
    "content": (
        "**New Buy Signal:**\n"
        "SPY 5 Min Candle\n"
        "Strategy: bollinger_mean_reversion\n"
        "Mode: options\n"
        "Volume: 45000000\n"
        "Price: 540.25\n"
        "Time: 2024-06-15T14:45:00Z"
    )
}

EXAMPLE_OPTIONS_SELL = {
    "content": (
        "**New Sell Signal:**\n"
        "QQQ 5 Min Candle\n"
        "Strategy: lorentzian_classification\n"
        "Mode: options\n"
        "Price: 460.75"
    )
}


class TestExamplePayloads:
    """Verify all example payloads parse correctly."""

    @pytest.mark.parametrize("payload,expected_side,expected_mode", [
        (EXAMPLE_STOCK_BUY, "buy", "stock"),
        (EXAMPLE_STOCK_SELL, "sell", "stock"),
        (EXAMPLE_OPTIONS_BUY, "buy", "options"),
        (EXAMPLE_OPTIONS_SELL, "sell", "options"),
    ])
    def test_example_payload(self, payload, expected_side, expected_mode):
        sig = parse_webhook_content(payload["content"])
        assert sig.side == expected_side
        assert sig.mode == expected_mode
        assert sig.price > 0
        assert len(sig.ticker) >= 2
