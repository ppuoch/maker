# Loaf R2 Patient Maker

Production-ready market-making bot for **League of Loaf Round 2**.

Built on the **official Loaf Python SDK**. Implements the architecture agreed by Grok / Claude / Kimi:

- **OrderManager** — sole owner of all `place` / `cancel` network calls
- **PatientMaker** — sizeable resting liquidity, low replace frequency, strong inventory skew
- **Local Accounting** — realised + unrealised PnL, volume, maker %, requests per dollar
- **RiskManager** — hard account drawdown kill-switch + inventory time-stop
- Official SDK handles nonce lifecycle, typed errors, rate-limit headers, no auto-retry of places

## Quick start

```bash
# 1. Clone / unpack this repo
cd loaf_r2_bot

# 2. Create venv
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install official Loaf SDK
pip install "git+https://github.com/Loaf-Markets/loaf-python-api-bot-template.git"

# 4. Install this bot
pip install -e .

# 5. Configure
cp .env.example .env
# Edit .env and set LOAF_API_KEY=...
# Optionally set LOAF_TARGET_TOKEN=eiffel  and LOAF_USER_ID=...

# 6. Run
python -m loaf_bot.main
# or: loaf-bot
```

Stop with Ctrl-C. Open orders are cancelled on shutdown.

## Configuration (`.env`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `LOAF_API_KEY` | required | API key from beta.loafmarkets.com/api |
| `LOAF_API_BASE_URL` | `https://api.loafmarkets.com/api` | REST base |
| `LOAF_TARGET_TOKEN` | `eiffel` | Token to trade (comma-separated later) |
| `LOAF_USER_ID` | empty | Numeric user id for private portfolio WS |
| `LOAF_TICK_INTERVAL_S` | `8.0` | Main loop period |
| `LOAF_MAX_DRAWDOWN_PCT` | `3.0` | Hard kill if equity falls this % from peak |
| `LOAF_MAX_POSITION_NOTIONAL` | `25000` | Max notional per token |

### Strategy defaults (edit in `config.py` or subclass)

```text
inside_offset_ticks        = 3      # start conservative; A/B 1 vs 3 vs 5
base_size                  = 4.0    # start small; scale after stability
min_refresh_interval_s     = 8.0
price_move_threshold_ticks = 3      # only replace when mid moved this much
max_inventory              = 40.0
inventory_time_stop_s      = 1200   # force reduce after 20 min
```

## Architecture

```
main.py / BotRuntime
├── official LoafClient + WS
├── Accounting          (local realised / unrealised / volume / maker %)
├── RiskManager         (drawdown kill-switch, time-stop, notional limits)
├── OrderManager        (ensure / cancel — only place where network is touched)
└── PatientMaker        (emits DesiredQuote only)
```

Strategy never calls `orders.create` or `orders.cancel` directly.

## Success metrics (local accounting)

Watch the periodic log lines:

- `equity` / `drawdown_pct` — capital health
- `realised_pnl` — closed-leg result (UI does not show this reliably)
- `total_volume` / `maker_ratio` — competition score quality
- `req_per_dollar` — rate-limit efficiency
- `open_age_s` — inventory persistence

Targets before calling it R2-ready:

| Metric | Target |
|--------|--------|
| Maker share | ≥ 85 % |
| Drawdown | < 1 % / hour |
| Avg resting time | > 60 s |
| Balance errors | 0 |
| Ctrl-C shutdown | < 5 s |

## A/B testing offset

```bash
# Edit config.py (or subclass) and run 20-min segments:
# inside_offset_ticks = 1 / 3 / 5
# Compare fill rate × realised spread from the accounting logs.
```

## Important platform rules encoded

- Long-only (cannot sell more than held)
- Min notional ~10 USDL (we guard at 12)
- Nonce never reused (SDK owns it)
- 503 → reconcile before retry
- No string-matching on HTTP 202
- Hard rate budget (~1.2 req/s safety net; smart-replace is primary throttle)

## What this deliberately does *not* do

- Aggressive 1-second top-of-book improvement
- Classic per-order trailing take-profit
- Multi-account / wash patterns
- Blind retries of order placement

## License / disclaimer

For the Loaf Markets simulated competition only. No warranty. You are solely responsible for the orders this bot places and any outcomes.
