# Crassus 2.0

Azure Function that receives TradingView webhook alerts and places **bracket orders** (stocks) and **risk-sized options orders** on Alpaca.

## Architecture

```
function_app/
├── function_app.py          # HTTP trigger entry point (POST /api/trade)
├── parser.py                # Webhook content parsing
├── strategy.py              # Strategy config + TP/SL/stop-limit computation
├── stock_orders.py          # Alpaca stock bracket order submission
├── options_screener.py      # Options contract query + selection logic
├── options_orders.py        # Options order submission + exit management
├── risk.py                  # Risk sizing (fixed dollar; % equity planned)
├── utils.py                 # Correlation ID, structured logging, rounding
├── host.json                # Azure Functions host config
├── requirements.txt         # Python dependencies
└── local.settings.json      # Env var template (gitignored)

tests/
├── test_parser.py           # 26 tests: parsing, edge cases, example payloads
├── test_strategy.py         # 13 tests: bracket math, strategy lookup
└── test_risk.py             # 9 tests:  options qty sizing, edge cases
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
        └─ mode=options ───► Screen contracts (DTE, moneyness, OI)
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

### Why no bracket orders for options?

Alpaca does **not** support bracket orders (`BRACKET` / `OCO` / `OTO` order class) for options contracts. The API returns an error if you try. Therefore:

- **Entry:** Simple limit order with `TimeInForce.DAY`.
- **Exits:** TP / SL target prices are logged with the correlation ID. A future **Timer Trigger** Azure Function will poll open positions and submit exit orders when targets are hit. See `options_orders.py::monitor_options_exits()` for the implementation outline.

### Risk sizing

```
qty = max_dollar_risk / (stop_distance × 100)
```

Where `stop_distance = (stop_loss_pct / 100) × premium_price` and `× 100` is the options multiplier.

### Contract selection

The screener queries Alpaca for contracts matching:
- **DTE window:** 14–45 days (configurable)
- **Strike range:** ±10 % of underlying price (proxy for delta range)
- **Liquidity:** minimum open interest, volume, bid-ask spread
- **Price:** within configured min/max premium range

Ranked by: closest to ATM, then highest open interest.

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
| `OPTIONS_DELTA_MIN` | `0.30` | Min absolute delta (moneyness proxy) |
| `OPTIONS_DELTA_MAX` | `0.70` | Max absolute delta (moneyness proxy) |
| `OPTIONS_MIN_OI` | `100` | Min open interest |
| `OPTIONS_MIN_VOLUME` | `10` | Min daily volume |
| `OPTIONS_MAX_SPREAD_PCT` | `5.0` | Max bid-ask spread as % of mid |
| `OPTIONS_MIN_PRICE` | `0.50` | Min option premium ($) |
| `OPTIONS_MAX_PRICE` | `50.0` | Max option premium ($) |

### Risk sizing

| Variable | Default | Description |
|---|---|---|
| `MAX_DOLLAR_RISK` | `50.0` | Max $ risk per options trade |
| `RISK_PCT_OF_EQUITY` | *(not set)* | Future: % of account equity |

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
   python -m pytest tests/ -v
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
