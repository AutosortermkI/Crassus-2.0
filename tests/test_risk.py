"""
Tests for risk sizing calculations.
"""
import pytest
from risk import compute_options_qty, compute_stock_qty


# ---------------------------------------------------------------------------
# Options quantity computation
# ---------------------------------------------------------------------------

class TestOptionsQty:
    """compute_options_qty: qty = max_risk / (stop_distance * 100)."""

    def test_basic_sizing(self):
        """$50 risk, 10% SL, $5.00 premium -> 1 contract."""
        # stop_distance = 0.10 * 5.00 = 0.50
        # qty = 50 / (0.50 * 100) = 50 / 50 = 1
        assert compute_options_qty(50.0, 10.0, 5.00) == 1

    def test_larger_risk(self):
        """$200 risk, 10% SL, $2.00 premium -> 10 contracts."""
        # stop_distance = 0.10 * 2.00 = 0.20
        # qty = 200 / (0.20 * 100) = 200 / 20 = 10
        assert compute_options_qty(200.0, 10.0, 2.00) == 10

    def test_fractional_rounds_down(self):
        """Fractional contracts round down to int."""
        # 75 / (0.10 * 5.00 * 100) = 75 / 50 = 1.5 -> 1
        assert compute_options_qty(75.0, 10.0, 5.00) == 1

    def test_minimum_one_contract(self):
        """Even tiny risk returns at least 1."""
        assert compute_options_qty(1.0, 50.0, 100.0) >= 1

    def test_zero_premium_returns_one(self):
        """Edge case: zero premium -> minimum 1."""
        assert compute_options_qty(50.0, 10.0, 0.0) == 1

    def test_zero_stop_loss_pct_returns_one(self):
        """Edge case: zero SL % -> minimum 1."""
        assert compute_options_qty(50.0, 0.0, 5.0) == 1

    def test_negative_premium_returns_one(self):
        """Edge case: negative premium -> minimum 1."""
        assert compute_options_qty(50.0, 10.0, -1.0) == 1

    def test_high_risk_many_contracts(self):
        """$500 risk, 20% SL, $1.00 premium -> 25 contracts."""
        # stop_distance = 0.20 * 1.00 = 0.20
        # qty = 500 / (0.20 * 100) = 500 / 20 = 25
        assert compute_options_qty(500.0, 20.0, 1.00) == 25


# ---------------------------------------------------------------------------
# Stock quantity
# ---------------------------------------------------------------------------

class TestStockQty:
    """compute_stock_qty returns default from env (or 1)."""

    def test_default_qty(self):
        """Without env override, returns 1."""
        assert compute_stock_qty() >= 1
