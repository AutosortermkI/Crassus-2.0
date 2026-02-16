"""
Crassus 2.0 -- Black-Scholes Greeks computation and implied volatility solver.

Provides:
  - European-style Black-Scholes option pricing
  - Greeks: Delta, Gamma, Theta (per day), Vega (per 1% IV move)
  - Implied volatility solver using Brent's method (scipy.optimize.brentq)

The Black-Scholes model is an adequate approximation for US equity options
given the use case (contract screening and risk assessment, not market-making).

Formulas:
    d1 = (ln(S/K) + (r + sigma^2/2)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)

    Call = S*N(d1) - K*e^(-rT)*N(d2)
    Put  = K*e^(-rT)*N(-d2) - S*N(-d1)

    Delta_call = N(d1)              Delta_put = N(d1) - 1
    Gamma      = phi(d1) / (S*sigma*sqrt(T))
    Theta_call = -(S*phi(d1)*sigma)/(2*sqrt(T)) - r*K*e^(-rT)*N(d2)
    Theta_put  = -(S*phi(d1)*sigma)/(2*sqrt(T)) + r*K*e^(-rT)*N(-d2)
    Vega       = S*phi(d1)*sqrt(T)

Where N() is the standard normal CDF and phi() is the standard normal PDF.

Extension points:
  - Add Rho for longer-dated options
  - Dividend yield adjustment
  - American-style pricing via binomial tree
"""

import math
import os
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default risk-free rate (5%), configurable via environment variable.
# In production, fetch from FRED or Treasury API.
DEFAULT_RISK_FREE_RATE = float(os.environ.get("RISK_FREE_RATE", "0.05"))

# IV solver bounds (annualized volatility)
IV_LOWER_BOUND = 0.001
IV_UPPER_BOUND = 10.0


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GreeksError(Exception):
    """Base exception for greeks computation errors."""


class IVSolverError(GreeksError):
    """Raised when the implied volatility solver fails to converge."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class OptionGreeks:
    """Computed Greeks for an options contract.

    Attributes:
        delta: Option delta (rate of change of price w.r.t. underlying).
        gamma: Option gamma (rate of change of delta w.r.t. underlying).
        theta: Option theta per calendar day (time decay).
        vega: Option vega per 1% move in implied volatility.
        iv: Implied volatility (annualized).
    """

    delta: float
    gamma: float
    theta: float   # per day, not per year
    vega: float    # per 1% move in IV
    iv: float      # implied volatility (annualized)


# ---------------------------------------------------------------------------
# Black-Scholes pricing (pure functions)
# ---------------------------------------------------------------------------

def _d1(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
) -> float:
    """Compute the d1 term of the Black-Scholes formula.

    Args:
        underlying_price: Current price of the underlying asset (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T).
        risk_free_rate: Annualized risk-free interest rate (r).
        sigma: Annualized volatility (sigma).

    Returns:
        The d1 value.
    """
    numerator = (
        math.log(underlying_price / strike)
        + (risk_free_rate + 0.5 * sigma ** 2) * dte_years
    )
    denominator = sigma * math.sqrt(dte_years)
    return numerator / denominator


def _d2(d1_value: float, sigma: float, dte_years: float) -> float:
    """Compute the d2 term of the Black-Scholes formula.

    Args:
        d1_value: Previously computed d1.
        sigma: Annualized volatility.
        dte_years: Time to expiration in years.

    Returns:
        The d2 value.
    """
    return d1_value - sigma * math.sqrt(dte_years)


def bs_price(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
    option_type: str,
) -> float:
    """Compute the Black-Scholes theoretical price for a European option.

    Args:
        underlying_price: Current price of the underlying asset (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T). Must be > 0.
        risk_free_rate: Annualized risk-free interest rate (r).
        sigma: Annualized volatility (sigma). Must be > 0.
        option_type: ``"call"`` or ``"put"``.

    Returns:
        Theoretical option price.

    Raises:
        GreeksError: If ``option_type`` is not ``"call"`` or ``"put"``.
        ValueError: If ``dte_years`` or ``sigma`` are non-positive.
    """
    if option_type not in ("call", "put"):
        raise GreeksError(f"Invalid option_type '{option_type}': expected 'call' or 'put'")
    if dte_years <= 0:
        raise ValueError("dte_years must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")

    d1_val = _d1(underlying_price, strike, dte_years, risk_free_rate, sigma)
    d2_val = _d2(d1_val, sigma, dte_years)
    discount = math.exp(-risk_free_rate * dte_years)

    if option_type == "call":
        return (
            underlying_price * norm.cdf(d1_val)
            - strike * discount * norm.cdf(d2_val)
        )
    else:
        return (
            strike * discount * norm.cdf(-d2_val)
            - underlying_price * norm.cdf(-d1_val)
        )


# ---------------------------------------------------------------------------
# Greeks computation (pure functions)
# ---------------------------------------------------------------------------

def compute_delta(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
    option_type: str,
) -> float:
    """Compute option delta.

    Args:
        underlying_price: Current price of the underlying (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T).
        risk_free_rate: Annualized risk-free rate (r).
        sigma: Annualized volatility.
        option_type: ``"call"`` or ``"put"``.

    Returns:
        Delta value. Calls: [0, 1], Puts: [-1, 0].
    """
    d1_val = _d1(underlying_price, strike, dte_years, risk_free_rate, sigma)
    if option_type == "call":
        return norm.cdf(d1_val)
    else:
        return norm.cdf(d1_val) - 1.0


def compute_gamma(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
) -> float:
    """Compute option gamma (same for calls and puts).

    Args:
        underlying_price: Current price of the underlying (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T).
        risk_free_rate: Annualized risk-free rate (r).
        sigma: Annualized volatility.

    Returns:
        Gamma value.
    """
    d1_val = _d1(underlying_price, strike, dte_years, risk_free_rate, sigma)
    return norm.pdf(d1_val) / (underlying_price * sigma * math.sqrt(dte_years))


def compute_theta(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
    option_type: str,
) -> float:
    """Compute option theta per calendar day.

    The raw Black-Scholes theta is annualized; this function divides
    by 365 to return the per-day value.

    Args:
        underlying_price: Current price of the underlying (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T).
        risk_free_rate: Annualized risk-free rate (r).
        sigma: Annualized volatility.
        option_type: ``"call"`` or ``"put"``.

    Returns:
        Theta per calendar day (typically negative for long options).
    """
    d1_val = _d1(underlying_price, strike, dte_years, risk_free_rate, sigma)
    d2_val = _d2(d1_val, sigma, dte_years)
    discount = math.exp(-risk_free_rate * dte_years)
    sqrt_t = math.sqrt(dte_years)

    common_term = -(underlying_price * norm.pdf(d1_val) * sigma) / (2.0 * sqrt_t)

    if option_type == "call":
        theta_annual = common_term - risk_free_rate * strike * discount * norm.cdf(d2_val)
    else:
        theta_annual = common_term + risk_free_rate * strike * discount * norm.cdf(-d2_val)

    # Convert from per-year to per-calendar-day
    return theta_annual / 365.0


def compute_vega(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
) -> float:
    """Compute option vega per 1% move in implied volatility.

    The raw Black-Scholes vega is per 1.0 (100%) move in IV.  This
    function scales it by 0.01 so the result represents the price
    change for a 1 percentage-point move in IV.

    Args:
        underlying_price: Current price of the underlying (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T).
        risk_free_rate: Annualized risk-free rate (r).
        sigma: Annualized volatility.

    Returns:
        Vega per 1% IV move (same for calls and puts).
    """
    d1_val = _d1(underlying_price, strike, dte_years, risk_free_rate, sigma)
    vega_raw = underlying_price * norm.pdf(d1_val) * math.sqrt(dte_years)
    # Scale to per-1%-point move
    return vega_raw * 0.01


def compute_all_greeks(
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    sigma: float,
    option_type: str,
) -> OptionGreeks:
    """Compute all Greeks for an option in a single call.

    This is more efficient than calling individual greek functions because
    it computes the shared d1/d2 terms only once.

    Args:
        underlying_price: Current price of the underlying (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T). Must be > 0.
        risk_free_rate: Annualized risk-free rate (r).
        sigma: Annualized volatility. Must be > 0.
        option_type: ``"call"`` or ``"put"``.

    Returns:
        :class:`OptionGreeks` with all computed values.

    Raises:
        GreeksError: If ``option_type`` is invalid.
        ValueError: If ``dte_years`` or ``sigma`` are non-positive.
    """
    if option_type not in ("call", "put"):
        raise GreeksError(f"Invalid option_type '{option_type}': expected 'call' or 'put'")
    if dte_years <= 0:
        raise ValueError("dte_years must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")

    d1_val = _d1(underlying_price, strike, dte_years, risk_free_rate, sigma)
    d2_val = _d2(d1_val, sigma, dte_years)
    discount = math.exp(-risk_free_rate * dte_years)
    sqrt_t = math.sqrt(dte_years)
    pdf_d1 = norm.pdf(d1_val)

    # Delta
    if option_type == "call":
        delta = norm.cdf(d1_val)
    else:
        delta = norm.cdf(d1_val) - 1.0

    # Gamma (same for calls and puts)
    gamma = pdf_d1 / (underlying_price * sigma * sqrt_t)

    # Theta (per calendar day)
    common_term = -(underlying_price * pdf_d1 * sigma) / (2.0 * sqrt_t)
    if option_type == "call":
        theta_annual = common_term - risk_free_rate * strike * discount * norm.cdf(d2_val)
    else:
        theta_annual = common_term + risk_free_rate * strike * discount * norm.cdf(-d2_val)
    theta = theta_annual / 365.0

    # Vega (per 1% IV move)
    vega = underlying_price * pdf_d1 * sqrt_t * 0.01

    return OptionGreeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        iv=sigma,
    )


# ---------------------------------------------------------------------------
# Implied volatility solver
# ---------------------------------------------------------------------------

def implied_volatility(
    market_price: float,
    underlying_price: float,
    strike: float,
    dte_years: float,
    risk_free_rate: float,
    option_type: str,
) -> float:
    """Solve for the implied volatility that matches the observed market price.

    Uses Brent's method (``scipy.optimize.brentq``) to find the volatility
    ``sigma`` such that ``bs_price(S, K, T, r, sigma, type) == market_price``.

    Args:
        market_price: Observed market price of the option.
        underlying_price: Current price of the underlying (S).
        strike: Option strike price (K).
        dte_years: Time to expiration in years (T). Must be > 0.
        risk_free_rate: Annualized risk-free rate (r).
        option_type: ``"call"`` or ``"put"``.

    Returns:
        Annualized implied volatility.  Returns ``float('nan')`` if the
        solver fails to converge (e.g. market price is below intrinsic value).
    """
    if market_price <= 0 or dte_years <= 0:
        return float("nan")

    def objective(sigma: float) -> float:
        return bs_price(underlying_price, strike, dte_years, risk_free_rate, sigma, option_type) - market_price

    try:
        iv = brentq(objective, IV_LOWER_BOUND, IV_UPPER_BOUND, xtol=1e-8, maxiter=200)
        return iv
    except (ValueError, RuntimeError):
        # Solver failed to converge -- market price is outside theoretical bounds
        return float("nan")
