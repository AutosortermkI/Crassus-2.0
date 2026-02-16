# Crassus 2.0

Azure Function that receives TradingView webhook alerts and places **bracket orders** (stocks) and **risk-sized options orders** on Alpaca.

## Architecture

```
function_app/
├── function_app.py          # HTTP trigger entry point (POST /api/trade)
├── parser.py                # Webhook content parsing
├── strategy.py              # Strategy config + TP/SL/stop-limit computation
├── stock_orders.py          # Alpaca stock bracket order submission
├── options_screener.py      # Options contract screening (Yahoo-first, Alpaca fallback)
├── options_orders.py        # Options order submission + exit management
├── risk.py                  # Risk sizing (fixed dollar; % equity planned)
├── greeks.py                # Black-Scholes Greeks (Delta/Gamma/Theta/Vega) + IV solver
├── yahoo_client.py          # Yahoo Finance crumb/cookie client for market data
├── utils.py                 # Correlation ID, structured logging, rounding
├── host.json                # Azure Functions host config
├── requirements.txt         # Python dependencies
└── local.settings.json      # Env var template (gitignored)

tests/
├── conftest.py              # Adds function_app/ to sys.path
├── test_parser.py           # 26 tests: parsing, edge cases, example payloads
├── test_strategy.py         # 13 tests: bracket math, strategy lookup
├── test_risk.py             # 9 tests:  options qty sizing, edge cases
├── test_greeks.py           # 47 tests: BS pricing, Greeks, IV solver, edge cases
└── test_yahoo_client.py     # 26 tests: Yahoo client, option chains, error handling
```

## Request flow

```
TradingView webhook
        │
        ▼
  POST /api/trade
  Header: X-Webhook-Token
  Body: { "content": "..." }
        │
        ├─ 401  invalid / missing token
        │
        ▼
  Parse content string
        │
        ├─ 400  bad payload / missing fields
        │
        ▼
  Look up strategy
        │
        ├─ 400  unknown strategy
        │
        ▼
  Route by mode
        │
        ├─ mode=stock ──────► Stock bracket order (Alpaca BRACKET)
        │                         TP / SL / stop-limit computed from strategy %
        │
        └─ mode=options ───► Fetch option chain (Yahoo Finance)
                              ► Compute Greeks via Black-Scholes
                              ► Screen by delta, volume, spread, OI
                              ► Risk-size qty from MAX_DOLLAR_RISK
                              ► Submit limit entry order (DAY)
                              ► Log TP/SL targets for external monitoring
```

## Supported strategies

| Strategy | Stock TP % | Stock SL % | Options TP % (premium) | Options SL % (premium) |
|---|---|---|---|---|
| `bollinger_mean_reversion` | 0.2 % | 0.1 % | 20 % | 10 % |
| `lorentzian_classification` | 1.0 % | 0.8 % | 50 % | 40 % |

All percentages are configurable via environment variables (see below).

## Webhook payload format

TradingView sends JSON with a `content` multi-line string:

```json
{
  "content": "**New Buy Signal:**\nAAPL 5 Min Candle\nStrategy: bollinger_mean_reversion\nMode: stock\nVolume: 2500000\nPrice: 189.50\nTime: 2024-06-15T14:30:00Z"
}
```

The webhook **must** include an `X-Webhook-Token` header matching the configured secret.

### Parsed fields

| Field | Required | Example |
|---|---|---|
| Side | Yes (from header line) | `"buy"` / `"sell"` |
| Ticker | Yes (first word after header) | `"AAPL"` |
| Strategy | Yes | `"bollinger_mean_reversion"` |
| Price | Yes | `189.50` |
| Mode | No (default `"stock"`) | `"stock"` / `"options"` |
| Volume | No | `2500000` |
| Time | No | `"2024-06-15T14:30:00Z"` |

## Options: design decisions

### Data source vs execution venue

Yahoo Finance and Alpaca serve different roles:

| Concern | Source | Why |
|---|---|---|
| **Market data** (bid/ask/IV/volume/OI) | Yahoo Finance | Richer options data than Alpaca's trading API |
| **Greeks computation** | `greeks.py` (local) | Black-Scholes from Yahoo's IV or solved from market prices |
| **Contract screening** | `options_screener.py` | Uses Yahoo data + Greeks for delta-based selection |
| **Order execution** | Alpaca | Execution venue; contract symbols map directly from Yahoo OCC format |

### Why no bracket orders for options?

This is an **Alpaca execution-side constraint** that Yahoo integration does not change. Alpaca does not support bracket orders (`BRACKET` / `OCO` / `OTO`) for options contracts -- the API returns an error if you try. Therefore:

- **Entry:** Simple limit order with `TimeInForce.DAY`.
- **Exit monitoring:** TP / SL target prices are logged with the correlation ID. A future **Timer Trigger** Azure Function will poll open positions (using Yahoo market data snapshots for current prices) and submit exit orders when targets are hit. See `options_orders.py::monitor_options_exits()` for the implementation outline.

### Risk sizing

```
qty = max_dollar_risk / (stop_distance × 100)
```

Where `stop_distance = (stop_loss_pct / 100) × premium_price` and `× 100` is the options multiplier.

### Contract selection

When Yahoo Finance is enabled (default), the screener uses Yahoo for richer market data and computes real Black-Scholes Greeks:

1. **Fetch** option chains from Yahoo Finance (provides bid/ask/IV/volume/OI)
2. **Compute** Greeks via `greeks.py` using Yahoo's IV data (or solve for IV from market prices)
3. **Filter** candidates by:
   - **DTE window:** 14–45 days (configurable)
   - **Delta range:** 0.30–0.70 absolute delta (real Greeks, not moneyness proxy)
   - **Volume:** minimum daily volume
   - **Bid-ask spread:** max spread as % of mid price
   - **Open interest:** minimum OI threshold
   - **Price:** within configured min/max premium range
4. **Score** using composite ranking: delta proximity (40%), OI (30%), spread tightness (20%), IV (10%)
5. **Map** selected contract symbol to Alpaca for order submission

**Fallback:** If Yahoo is unavailable or disabled (`YAHOO_ENABLED=false`), the screener falls back to Alpaca-only data with IV solved from close prices.

### Greeks computation

`greeks.py` implements the Black-Scholes model for European-style options:

- **Pricing:** Call and put theoretical prices
- **Delta:** Rate of change of price w.r.t. underlying
- **Gamma:** Rate of change of delta w.r.t. underlying
- **Theta:** Time decay per calendar day
- **Vega:** Price sensitivity per 1% IV move
- **IV solver:** Brent's method (`scipy.optimize.brentq`) to solve for implied volatility from observed market prices

### Yahoo Finance integration

`yahoo_client.py` provides authenticated access to Yahoo Finance's options API:

- Cookie/crumb authentication with automatic refresh on 401
- Exponential backoff on rate limiting (429) and transient errors (5xx)
- Returns Crassus-compatible dataclasses (`YahooOptionContract`, `YahooOptionChain`)
- Intelligent expiration selection: prefers 0DTE, falls back to nearest future

## Environment variables

### Required

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `WEBHOOK_AUTH_TOKEN` | Shared secret for `X-Webhook-Token` header |

### Optional (with defaults)

| Variable | Default | Description |
|---|---|---|
| `ALPACA_PAPER` | `true` | `true` = paper trading, `false` = live |
| `DEFAULT_STOCK_QTY` | `1` | Shares per stock trade |

### Strategy: bollinger_mean_reversion (prefix `BMR_`)

| Variable | Default | Description |
|---|---|---|
| `BMR_STOCK_TP_PCT` | `0.2` | Stock take-profit % |
| `BMR_STOCK_SL_PCT` | `0.1` | Stock stop-loss % |
| `BMR_STOCK_STOP_LIMIT_PCT` | `0.15` | Stock stop-limit % |
| `BMR_OPTIONS_TP_PCT` | `20.0` | Options TP as % of premium |
| `BMR_OPTIONS_SL_PCT` | `10.0` | Options SL as % of premium |

### Strategy: lorentzian_classification (prefix `LC_`)

| Variable | Default | Description |
|---|---|---|
| `LC_STOCK_TP_PCT` | `1.0` | Stock take-profit % |
| `LC_STOCK_SL_PCT` | `0.8` | Stock stop-loss % |
| `LC_STOCK_STOP_LIMIT_PCT` | `0.9` | Stock stop-limit % |
| `LC_OPTIONS_TP_PCT` | `50.0` | Options TP as % of premium |
| `LC_OPTIONS_SL_PCT` | `40.0` | Options SL as % of premium |

### Options screening

| Variable | Default | Description |
|---|---|---|
| `OPTIONS_DTE_MIN` | `14` | Min days to expiration |
| `OPTIONS_DTE_MAX` | `45` | Max days to expiration |
| `OPTIONS_DELTA_MIN` | `0.30` | Min absolute delta (real Greeks via `greeks.py`) |
| `OPTIONS_DELTA_MAX` | `0.70` | Max absolute delta (real Greeks via `greeks.py`) |
| `OPTIONS_MIN_OI` | `100` | Min open interest |
| `OPTIONS_MIN_VOLUME` | `10` | Min daily volume |
| `OPTIONS_MAX_SPREAD_PCT` | `5.0` | Max bid-ask spread as % of mid |
| `OPTIONS_MIN_PRICE` | `0.50` | Min option premium ($) |
| `OPTIONS_MAX_PRICE` | `50.0` | Max option premium ($) |

### Greeks computation

| Variable | Default | Description |
|---|---|---|
| `RISK_FREE_RATE` | `0.05` | Annualized risk-free rate for Black-Scholes (5%) |

### Yahoo Finance data source

| Variable | Default | Description |
|---|---|---|
| `YAHOO_ENABLED` | `true` | Toggle Yahoo Finance as market data source |
| `YAHOO_RETRY_COUNT` | `5` | Max retries for Yahoo API requests |
| `YAHOO_BACKOFF_BASE` | `2` | Exponential backoff base (seconds) |

### Risk sizing

| Variable | Default | Description |
|---|---|---|
| `MAX_DOLLAR_RISK` | `50.0` | Max $ risk per options trade |
| `RISK_PCT_OF_EQUITY` | *(not set)* | Future: % of account equity |

## Dependencies

| Package | Purpose |
|---|---|
| `azure-functions` | Azure Functions runtime |
| `alpaca-py` | Alpaca Trading API (stocks + options execution) |
| `scipy` | `norm.cdf`/`norm.pdf` (Greeks), `brentq` (IV solver) |
| `numpy` | Numerical computation |
| `requests` | Yahoo Finance HTTP client (direct API, not `yfinance`) |

> **Note:** Do NOT add `yfinance`. The direct API approach via `requests` + `YahooCrumbClient` is more reliable and avoids the heavy `yfinance` dependency tree.

## Setup

1. **Clone and install**
   ```bash
   git clone <repo-url>
   cd Crassus-2.0
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r function_app/requirements.txt
   ```

2. **Configure credentials**
   - Copy `.env.example` to `.env` and fill in real values.
   - Copy `function_app/local.settings.json` and set the `Values` section.

3. **Run locally**
   ```bash
   cd function_app
   func start
   ```
   Endpoint: `http://localhost:7071/api/trade` (POST)

4. **Run tests**
   ```bash
   pip install pytest
   python -m pytest tests/ -v    # 121 tests across 5 modules
   ```

## Example curl

```bash
# Stock buy signal (bollinger_mean_reversion)
curl -X POST http://localhost:7071/api/trade \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: your-secret-token" \
  -d '{
    "content": "**New Buy Signal:**\nAAPL 5 Min Candle\nStrategy: bollinger_mean_reversion\nMode: stock\nVolume: 2500000\nPrice: 189.50\nTime: 2024-06-15T14:30:00Z"
  }'

# Options sell signal (lorentzian_classification)
curl -X POST http://localhost:7071/api/trade \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: your-secret-token" \
  -d '{
    "content": "**New Sell Signal:**\nQQQ 5 Min Candle\nStrategy: lorentzian_classification\nMode: options\nPrice: 460.75"
  }'
```

## HTTP responses

| Code | Meaning |
|---|---|
| **200** | Order placed successfully (JSON with order details) |
| **400** | Bad payload, missing fields, unknown strategy, no contract found |
| **401** | Missing or invalid `X-Webhook-Token` header |
| **502** | Alpaca API error |
| **500** | Internal / unexpected error |

All responses include a `correlation_id` for log tracing.

## Deployment

Deploy to Azure via VS Code Azure Functions extension or:

```bash
func azure functionapp publish <AppName>
```

Set all environment variables as **Application Settings** in the Function App.

## License

Use and modify as needed for your own trading setup.
