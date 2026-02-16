"""
Tests for Black-Scholes Greeks computation and implied volatility solver.

Ground-truth values verified against:
  - Hull's "Options, Futures, and Other Derivatives" textbook examples
  - Known closed-form properties of Black-Scholes Greeks
"""
import math

import pytest
from greeks import (
    bs_price,
    compute_delta,
    compute_gamma,
    compute_theta,
    compute_vega,
    compute_all_greeks,
    implied_volatility,
    OptionGreeks,
    GreeksError,
    IVSolverError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def atm_call_params():
    """ATM call option parameters (Hull-style textbook example)."""
    return {
        "underlying_price": 100.0,
        "strike": 100.0,
        "dte_years": 0.25,        # 3 months
        "risk_free_rate": 0.05,
        "sigma": 0.20,
        "option_type": "call",
    }


@pytest.fixture
def atm_put_params():
    """ATM put option parameters."""
    return {
        "underlying_price": 100.0,
        "strike": 100.0,
        "dte_years": 0.25,
        "risk_free_rate": 0.05,
        "sigma": 0.20,
        "option_type": "put",
    }


@pytest.fixture
def deep_itm_call_params():
    """Deep ITM call: S=150, K=100."""
    return {
        "underlying_price": 150.0,
        "strike": 100.0,
        "dte_years": 0.5,
        "risk_free_rate": 0.05,
        "sigma": 0.30,
        "option_type": "call",
    }


@pytest.fixture
def deep_otm_call_params():
    """Deep OTM call: S=80, K=120."""
    return {
        "underlying_price": 80.0,
        "strike": 120.0,
        "dte_years": 0.25,
        "risk_free_rate": 0.05,
        "sigma": 0.20,
        "option_type": "call",
    }


@pytest.fixture
def near_expiry_params():
    """Near-expiry ATM call: 1 day to expiration."""
    return {
        "underlying_price": 100.0,
        "strike": 100.0,
        "dte_years": 1.0 / 365.0,  # 1 day
        "risk_free_rate": 0.05,
        "sigma": 0.20,
        "option_type": "call",
    }


# ---------------------------------------------------------------------------
# Black-Scholes pricing
# ---------------------------------------------------------------------------

class TestBSPrice:
    """Black-Scholes option pricing."""

    def test_atm_call_price(self, atm_call_params):
        """ATM call should have a positive, sensible price."""
        price = bs_price(**atm_call_params)
        # For S=100, K=100, T=0.25, r=0.05, sigma=0.20:
        # Textbook value ~4.615
        assert price == pytest.approx(4.615, abs=0.05)

    def test_atm_put_price(self, atm_put_params):
        """ATM put should have a positive, sensible price."""
        price = bs_price(**atm_put_params)
        # Put price ~3.37 (via put-call parity)
        assert price == pytest.approx(3.37, abs=0.05)

    def test_put_call_parity(self, atm_call_params, atm_put_params):
        """Put-call parity: C - P = S - K*e^(-rT)."""
        call_price = bs_price(**atm_call_params)
        put_price = bs_price(**atm_put_params)
        S = atm_call_params["underlying_price"]
        K = atm_call_params["strike"]
        r = atm_call_params["risk_free_rate"]
        T = atm_call_params["dte_years"]
        parity = S - K * math.exp(-r * T)
        assert (call_price - put_price) == pytest.approx(parity, abs=1e-6)

    def test_deep_itm_call(self, deep_itm_call_params):
        """Deep ITM call should be close to intrinsic value."""
        price = bs_price(**deep_itm_call_params)
        intrinsic = 150.0 - 100.0
        assert price > intrinsic  # Must exceed intrinsic
        assert price == pytest.approx(52.50, abs=1.0)  # Approximate

    def test_deep_otm_call(self, deep_otm_call_params):
        """Deep OTM call should be near zero."""
        price = bs_price(**deep_otm_call_params)
        assert price < 0.5
        assert price > 0

    def test_call_price_increases_with_underlying(self):
        """Higher underlying price -> higher call price."""
        base = bs_price(100.0, 100.0, 0.25, 0.05, 0.20, "call")
        higher = bs_price(110.0, 100.0, 0.25, 0.05, 0.20, "call")
        assert higher > base

    def test_put_price_increases_with_strike(self):
        """Higher strike -> higher put price."""
        base = bs_price(100.0, 100.0, 0.25, 0.05, 0.20, "put")
        higher = bs_price(100.0, 110.0, 0.25, 0.05, 0.20, "put")
        assert higher > base

    def test_invalid_option_type(self):
        """Invalid option type raises GreeksError."""
        with pytest.raises(GreeksError, match="Invalid option_type"):
            bs_price(100.0, 100.0, 0.25, 0.05, 0.20, "straddle")

    def test_zero_dte_raises(self):
        """Zero DTE raises ValueError."""
        with pytest.raises(ValueError, match="dte_years must be positive"):
            bs_price(100.0, 100.0, 0.0, 0.05, 0.20, "call")

    def test_negative_sigma_raises(self):
        """Negative sigma raises ValueError."""
        with pytest.raises(ValueError, match="sigma must be positive"):
            bs_price(100.0, 100.0, 0.25, 0.05, -0.20, "call")


# ---------------------------------------------------------------------------
# Delta
# ---------------------------------------------------------------------------

class TestDelta:
    """Option delta computation."""

    def test_atm_call_delta(self, atm_call_params):
        """ATM call delta should be approximately 0.5 (slightly above due to drift)."""
        delta = compute_delta(**atm_call_params)
        assert delta == pytest.approx(0.5, abs=0.07)
        assert 0 < delta < 1

    def test_atm_put_delta(self, atm_put_params):
        """ATM put delta should be approximately -0.5."""
        delta = compute_delta(**atm_put_params)
        assert delta == pytest.approx(-0.5, abs=0.07)
        assert -1 < delta < 0

    def test_deep_itm_call_delta(self, deep_itm_call_params):
        """Deep ITM call delta should be close to 1.0."""
        delta = compute_delta(**deep_itm_call_params)
        assert delta > 0.95

    def test_deep_otm_call_delta(self, deep_otm_call_params):
        """Deep OTM call delta should be close to 0.0."""
        delta = compute_delta(**deep_otm_call_params)
        assert delta < 0.05

    def test_call_put_delta_relationship(self, atm_call_params, atm_put_params):
        """Call delta - Put delta should equal 1.0."""
        call_delta = compute_delta(**atm_call_params)
        put_delta = compute_delta(**atm_put_params)
        assert (call_delta - put_delta) == pytest.approx(1.0, abs=1e-6)

    def test_near_expiry_atm_call_delta(self, near_expiry_params):
        """Near-expiry ATM call delta should be close to 0.5."""
        delta = compute_delta(**near_expiry_params)
        assert delta == pytest.approx(0.5, abs=0.10)


# ---------------------------------------------------------------------------
# Gamma
# ---------------------------------------------------------------------------

class TestGamma:
    """Option gamma computation."""

    def test_atm_gamma_positive(self, atm_call_params):
        """ATM gamma should be positive."""
        params = {k: v for k, v in atm_call_params.items() if k != "option_type"}
        gamma = compute_gamma(**params)
        assert gamma > 0

    def test_atm_has_highest_gamma(self):
        """ATM options have the highest gamma."""
        base_params = {
            "underlying_price": 100.0,
            "dte_years": 0.25,
            "risk_free_rate": 0.05,
            "sigma": 0.20,
        }
        atm_gamma = compute_gamma(strike=100.0, **base_params)
        otm_gamma = compute_gamma(strike=120.0, **base_params)
        itm_gamma = compute_gamma(strike=80.0, **base_params)
        assert atm_gamma > otm_gamma
        assert atm_gamma > itm_gamma

    def test_near_expiry_gamma_spike(self, near_expiry_params):
        """Near-expiry ATM gamma should be very high (gamma spike)."""
        params = {k: v for k, v in near_expiry_params.items() if k != "option_type"}
        near_gamma = compute_gamma(**params)
        far_params = dict(params, dte_years=0.5)
        far_gamma = compute_gamma(**far_params)
        assert near_gamma > far_gamma


# ---------------------------------------------------------------------------
# Theta
# ---------------------------------------------------------------------------

class TestTheta:
    """Option theta computation."""

    def test_call_theta_negative(self, atm_call_params):
        """Long call theta should be negative (time decay)."""
        theta = compute_theta(**atm_call_params)
        assert theta < 0

    def test_put_theta_negative(self, atm_put_params):
        """Long put theta should be negative."""
        theta = compute_theta(**atm_put_params)
        assert theta < 0

    def test_near_expiry_theta_larger(self, near_expiry_params):
        """Near-expiry theta magnitude should be larger than far-dated."""
        near_theta = compute_theta(**near_expiry_params)
        far_params = dict(near_expiry_params, dte_years=0.5)
        far_theta = compute_theta(**far_params)
        assert abs(near_theta) > abs(far_theta)

    def test_theta_is_per_day(self, atm_call_params):
        """Theta should be reasonable per-day value (not annual)."""
        theta = compute_theta(**atm_call_params)
        # For a $100 stock with 3mo expiry, daily theta ~$0.01-$0.10
        assert -1.0 < theta < 0


# ---------------------------------------------------------------------------
# Vega
# ---------------------------------------------------------------------------

class TestVega:
    """Option vega computation."""

    def test_vega_positive(self, atm_call_params):
        """Vega should be positive (higher IV -> higher price)."""
        params = {k: v for k, v in atm_call_params.items() if k != "option_type"}
        vega = compute_vega(**params)
        assert vega > 0

    def test_atm_has_highest_vega(self):
        """ATM options have the highest vega."""
        base_params = {
            "underlying_price": 100.0,
            "dte_years": 0.25,
            "risk_free_rate": 0.05,
            "sigma": 0.20,
        }
        atm_vega = compute_vega(strike=100.0, **base_params)
        otm_vega = compute_vega(strike=130.0, **base_params)
        assert atm_vega > otm_vega

    def test_vega_per_1pct(self, atm_call_params):
        """Vega should represent price change per 1% IV move."""
        params = {k: v for k, v in atm_call_params.items() if k != "option_type"}
        vega = compute_vega(**params)
        # Verify: price at sigma vs sigma+0.01 should differ by ~vega
        p1 = bs_price(**atm_call_params)
        p2 = bs_price(**dict(atm_call_params, sigma=0.21))
        assert (p2 - p1) == pytest.approx(vega, abs=0.01)


# ---------------------------------------------------------------------------
# compute_all_greeks
# ---------------------------------------------------------------------------

class TestComputeAllGreeks:
    """compute_all_greeks returns consistent results."""

    def test_returns_option_greeks(self, atm_call_params):
        """Should return an OptionGreeks dataclass."""
        greeks = compute_all_greeks(**atm_call_params)
        assert isinstance(greeks, OptionGreeks)

    def test_consistent_with_individual_functions(self, atm_call_params):
        """All-in-one should match individual function results."""
        greeks = compute_all_greeks(**atm_call_params)

        delta = compute_delta(**atm_call_params)
        params_no_type = {k: v for k, v in atm_call_params.items() if k != "option_type"}
        gamma = compute_gamma(**params_no_type)
        theta = compute_theta(**atm_call_params)
        vega = compute_vega(**params_no_type)

        assert greeks.delta == pytest.approx(delta, abs=1e-10)
        assert greeks.gamma == pytest.approx(gamma, abs=1e-10)
        assert greeks.theta == pytest.approx(theta, abs=1e-10)
        assert greeks.vega == pytest.approx(vega, abs=1e-10)

    def test_iv_field_equals_sigma(self, atm_call_params):
        """The iv field should store the input sigma."""
        greeks = compute_all_greeks(**atm_call_params)
        assert greeks.iv == atm_call_params["sigma"]

    def test_invalid_option_type_raises(self):
        """Invalid option type raises GreeksError."""
        with pytest.raises(GreeksError):
            compute_all_greeks(100.0, 100.0, 0.25, 0.05, 0.20, "invalid")

    def test_zero_dte_raises(self):
        """Zero DTE raises ValueError."""
        with pytest.raises(ValueError):
            compute_all_greeks(100.0, 100.0, 0.0, 0.05, 0.20, "call")

    def test_put_greeks(self, atm_put_params):
        """Put greeks should have correct signs."""
        greeks = compute_all_greeks(**atm_put_params)
        assert greeks.delta < 0
        assert greeks.gamma > 0
        assert greeks.theta < 0
        assert greeks.vega > 0


# ---------------------------------------------------------------------------
# Implied volatility solver
# ---------------------------------------------------------------------------

class TestImpliedVolatility:
    """IV solver tests."""

    def test_round_trip_call(self, atm_call_params):
        """Compute BS price at known sigma, then solve back to IV."""
        price = bs_price(**atm_call_params)
        iv = implied_volatility(
            market_price=price,
            underlying_price=atm_call_params["underlying_price"],
            strike=atm_call_params["strike"],
            dte_years=atm_call_params["dte_years"],
            risk_free_rate=atm_call_params["risk_free_rate"],
            option_type="call",
        )
        assert iv == pytest.approx(atm_call_params["sigma"], abs=1e-6)

    def test_round_trip_put(self, atm_put_params):
        """Compute BS price at known sigma, then solve back to IV."""
        price = bs_price(**atm_put_params)
        iv = implied_volatility(
            market_price=price,
            underlying_price=atm_put_params["underlying_price"],
            strike=atm_put_params["strike"],
            dte_years=atm_put_params["dte_years"],
            risk_free_rate=atm_put_params["risk_free_rate"],
            option_type="put",
        )
        assert iv == pytest.approx(atm_put_params["sigma"], abs=1e-6)

    def test_high_iv_round_trip(self):
        """High IV (~1.0) should round-trip correctly."""
        params = {
            "underlying_price": 100.0,
            "strike": 100.0,
            "dte_years": 0.25,
            "risk_free_rate": 0.05,
            "sigma": 1.0,
            "option_type": "call",
        }
        price = bs_price(**params)
        iv = implied_volatility(price, 100.0, 100.0, 0.25, 0.05, "call")
        assert iv == pytest.approx(1.0, abs=1e-4)

    def test_low_iv_round_trip(self):
        """Low IV (~0.05) should round-trip correctly."""
        params = {
            "underlying_price": 100.0,
            "strike": 100.0,
            "dte_years": 0.25,
            "risk_free_rate": 0.05,
            "sigma": 0.05,
            "option_type": "call",
        }
        price = bs_price(**params)
        iv = implied_volatility(price, 100.0, 100.0, 0.25, 0.05, "call")
        assert iv == pytest.approx(0.05, abs=1e-4)

    def test_zero_market_price_returns_nan(self):
        """Zero market price should return NaN."""
        iv = implied_volatility(0.0, 100.0, 100.0, 0.25, 0.05, "call")
        assert math.isnan(iv)

    def test_negative_market_price_returns_nan(self):
        """Negative market price should return NaN."""
        iv = implied_volatility(-1.0, 100.0, 100.0, 0.25, 0.05, "call")
        assert math.isnan(iv)

    def test_zero_dte_returns_nan(self):
        """Zero DTE should return NaN."""
        iv = implied_volatility(5.0, 100.0, 100.0, 0.0, 0.05, "call")
        assert math.isnan(iv)

    def test_price_below_intrinsic_returns_nan(self):
        """Market price below intrinsic value should return NaN."""
        # For a call with S=110, K=100, intrinsic = 10.
        # Setting market price to 5 (below intrinsic) should fail.
        iv = implied_volatility(5.0, 110.0, 100.0, 0.25, 0.05, "call")
        assert math.isnan(iv)

    @pytest.mark.parametrize("sigma", [0.10, 0.20, 0.50, 0.80, 2.0])
    def test_various_ivs_round_trip(self, sigma):
        """Parametrized round-trip test across a range of IVs."""
        price = bs_price(100.0, 100.0, 0.25, 0.05, sigma, "call")
        iv = implied_volatility(price, 100.0, 100.0, 0.25, 0.05, "call")
        assert iv == pytest.approx(sigma, abs=1e-4)

    def test_deep_itm_call_iv(self, deep_itm_call_params):
        """Deep ITM call IV round-trip."""
        price = bs_price(**deep_itm_call_params)
        iv = implied_volatility(
            price,
            deep_itm_call_params["underlying_price"],
            deep_itm_call_params["strike"],
            deep_itm_call_params["dte_years"],
            deep_itm_call_params["risk_free_rate"],
            "call",
        )
        assert iv == pytest.approx(deep_itm_call_params["sigma"], abs=1e-4)

    def test_deep_otm_put_iv(self):
        """Deep OTM put IV round-trip."""
        params = {
            "underlying_price": 150.0,
            "strike": 100.0,
            "dte_years": 0.25,
            "risk_free_rate": 0.05,
            "sigma": 0.30,
            "option_type": "put",
        }
        price = bs_price(**params)
        iv = implied_volatility(price, 150.0, 100.0, 0.25, 0.05, "put")
        assert iv == pytest.approx(0.30, abs=1e-3)
