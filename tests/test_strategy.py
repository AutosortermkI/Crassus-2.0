"""
Tests for strategy configuration and bracket-price computation.
"""
import pytest
from strategy import (
    StrategyConfig,
    compute_stock_bracket_prices,
    compute_options_exit_prices,
    get_strategy,
    UnknownStrategyError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bmr_config():
    """bollinger_mean_reversion-like config."""
    return StrategyConfig(
        name="bollinger_mean_reversion",
        stock_tp_pct=0.2,
        stock_sl_pct=0.1,
        stock_stop_limit_pct=0.15,
        options_tp_pct=20.0,
        options_sl_pct=10.0,
    )


@pytest.fixture
def lc_config():
    """lorentzian_classification-like config."""
    return StrategyConfig(
        name="lorentzian_classification",
        stock_tp_pct=1.0,
        stock_sl_pct=0.8,
        stock_stop_limit_pct=0.9,
        options_tp_pct=50.0,
        options_sl_pct=40.0,
    )


# ---------------------------------------------------------------------------
# Stock bracket price computation
# ---------------------------------------------------------------------------

class TestStockBracketBuy:
    """Buy-side bracket prices for bollinger_mean_reversion."""

    def test_take_profit(self, bmr_config):
        tp, _, _ = compute_stock_bracket_prices(100.0, "buy", bmr_config)
        # 100 * (1 + 0.002) = 100.20
        assert tp == pytest.approx(100.20)

    def test_stop_price(self, bmr_config):
        _, stop, _ = compute_stock_bracket_prices(100.0, "buy", bmr_config)
        # 100 * (1 - 0.001) = 99.90
        assert stop == pytest.approx(99.90)

    def test_stop_limit(self, bmr_config):
        _, _, sl = compute_stock_bracket_prices(100.0, "buy", bmr_config)
        # 100 * (1 - 0.0015) = 99.85
        assert sl == pytest.approx(99.85)


class TestStockBracketSell:
    """Sell-side bracket prices for bollinger_mean_reversion."""

    def test_take_profit(self, bmr_config):
        tp, _, _ = compute_stock_bracket_prices(100.0, "sell", bmr_config)
        # 100 * (1 - 0.002) = 99.80
        assert tp == pytest.approx(99.80)

    def test_stop_price(self, bmr_config):
        _, stop, _ = compute_stock_bracket_prices(100.0, "sell", bmr_config)
        # 100 * (1 + 0.001) = 100.10
        assert stop == pytest.approx(100.10)

    def test_stop_limit(self, bmr_config):
        _, _, sl = compute_stock_bracket_prices(100.0, "sell", bmr_config)
        # 100 * (1 + 0.0015) = 100.15
        assert sl == pytest.approx(100.15)


class TestStockBracketLorentzian:
    """lorentzian_classification at a realistic price."""

    def test_buy_bracket(self, lc_config):
        tp, stop, sl = compute_stock_bracket_prices(200.0, "buy", lc_config)
        assert tp == pytest.approx(202.0)      # 200 * 1.01
        assert stop == pytest.approx(198.40)   # 200 * (1 - 0.008)
        assert sl == pytest.approx(198.20)     # 200 * (1 - 0.009)

    def test_sell_bracket(self, lc_config):
        tp, stop, sl = compute_stock_bracket_prices(200.0, "sell", lc_config)
        assert tp == pytest.approx(198.0)      # 200 * 0.99
        assert stop == pytest.approx(201.60)   # 200 * 1.008
        assert sl == pytest.approx(201.80)     # 200 * 1.009


# ---------------------------------------------------------------------------
# Options exit-price computation
# ---------------------------------------------------------------------------

class TestOptionsExitPrices:
    """Options TP/SL as % of premium."""

    def test_bmr_options(self, bmr_config):
        # premium $5.00, TP 20%, SL 10%
        tp, sl = compute_options_exit_prices(5.0, "buy", bmr_config)
        assert tp == pytest.approx(6.0)    # 5.0 * 1.20
        assert sl == pytest.approx(4.5)    # 5.0 * 0.90

    def test_lc_options(self, lc_config):
        # premium $3.00, TP 50%, SL 40%
        tp, sl = compute_options_exit_prices(3.0, "buy", lc_config)
        assert tp == pytest.approx(4.5)    # 3.0 * 1.50
        assert sl == pytest.approx(1.8)    # 3.0 * 0.60

    def test_sell_signal_same_as_buy(self, bmr_config):
        """For options, TP/SL are always relative to the long premium."""
        tp_buy, sl_buy = compute_options_exit_prices(5.0, "buy", bmr_config)
        tp_sell, sl_sell = compute_options_exit_prices(5.0, "sell", bmr_config)
        assert tp_buy == tp_sell
        assert sl_buy == sl_sell


# ---------------------------------------------------------------------------
# Strategy registry lookup
# ---------------------------------------------------------------------------

class TestStrategyLookup:
    """get_strategy returns config or raises."""

    def test_known_strategy(self):
        cfg = get_strategy("bollinger_mean_reversion")
        assert cfg.name == "bollinger_mean_reversion"
        assert cfg.stock_tp_pct > 0

    def test_unknown_strategy(self):
        with pytest.raises(UnknownStrategyError, match="Unknown strategy"):
            get_strategy("nonexistent_strategy")
