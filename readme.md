# HTX Futures EMA Pullback Bot

[Русский README](docs/readme.ru.md) | [简体中文](docs/readme.zh-CN.md) | [Strategy details](strategy.md)

Python bot for HTX USDT-M futures. The active trading route is an EMA Pullback strategy with separate `long` and `short` profiles that can run together in one process, share market data, and avoid opening opposite exposure on the same symbol.

The bot is a live-ordering application: startup requires explicit HTX API credentials, matching credentials for both profiles in combined mode, and an account leverage that the bot can read from HTX or that you define via `ACCOUNT_LEVERAGE`.

Trading futures is risky. This repository is software, not financial advice. Audit the code and run the unit tests plus mock/stub exchange checks before sending live orders.

## What Is Active Now

The default route is `ema_pullback`.

- Exchange: HTX linear USDT-M futures through CCXT.
- Margin/position mode: cross margin, one-way mode.
- Profiles: `long`, `short`, or both via `BOT_PROFILES=long,short`.
- Signals: closed candles only, with macro EMA trend, pullback recovery, trigger EMA, relative strength to BTC, BTC 30m filter, score ranking, and entry throttling.
- Initial entry: post-only limit ladder using `BUYING.ladder_fractions` and `BUYING.ladder_offsets`.
- Averaging: enabled by `EMA_AVERAGING_ENABLED`; requires an open position, no active entry orders, healthy signal `add_valid`, drawdown threshold, age limit, interval limit, risk caps, and optional account-PnL context.
- Exit: reduce-only adaptive exit ladder, optional trailing/runner logic, account profit unload, and breakeven after the configured holding time.
- External reference prices: MEXC book ticker is used as a reference for entry blocks, directional 1m blocks, impulse bonus, and tighter exits.
- Monitoring: CSV/JSONL logs for trades, cycle stats, macro context, external prices, signal analytics, diagnostics, and account PnL.

The default live route does not place a classic stop-loss. Controlled-loss, hard-time-exit, and absolute-force-exit helpers exist in code but are disabled by default and are not part of the conservative launch profile unless explicitly wired and configured.

## Strategy Snapshot

Default EMA map:

| Layer | Config | Timeframe | Effective EMA |
|---|---:|---:|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES=36000` | `1d` | EMA25 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES=72000` | `1d` | EMA50 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES=1440` | `4h` | EMA6 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES=2880` | `4h` | EMA12 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES=50` | `1m` | EMA50 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES=100` | `1m` | EMA100 |

Long entry requires the macro and trigger EMAs to point up, the pullback layer to recover upward after a recent pullback, optional RS confirmation, and a BTC 30m filter that is not too negative.

Short entry mirrors the same logic downward: macro and trigger EMAs point down, pullback layer recovers downward after a recent bounce, optional RS confirmation, and BTC 30m is not too positive.

Signal flags are split by use: `valid` means the symbol has a coherent directional signal, `entry_valid` means a full new-entry gate passed, and `add_valid` is the stricter health gate used for averaging an already open position.

If HTX rejects a reduce-only exit because the whole position is reported as closeable-reserved/frozen, the bot keeps a pending exit-ladder state and waits for available closeable amount, a position-size change, or visible close orders to adopt/cancel. It does not keep duplicating exit ladders on timeout alone.

For implementation-level details, see [strategy.md](strategy.md).

## Quick Start

Requirements:

- Python 3.11+
- HTX futures account for live mode
- API key/secret with the required futures permissions for live trading

Install:

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Prepare configuration:

```bash
copy .env.example .env
```

Fill at least:

```dotenv
HTX_API_KEY=
HTX_API_SECRET=
BOT_PROFILES=long,short
```

Run both profiles:

```bash
python bot.py
```

Run one profile:

```bash
python bot.py --profiles long
python bot.py --profiles short
```

Run tests:

```bash
python -m pytest -q
```

## Important Configuration

Global runtime:

```dotenv
BOT_PROFILES=long,short
POLL_INTERVAL_SEC=3
LOG_LEVEL=INFO
```

Risk and sizing:

```dotenv
LEVERAGE=30
SET_LEVERAGE_ON_START=false
ACCOUNT_LEVERAGE=50
MIN_QUOTE_RESERVE=15
MAX_ACTIVE_POSITIONS=50
EMA_MAX_POSITION_MARGIN_FRACTION=0.03
EMA_MAX_TOTAL_MARGIN_FRACTION=0.50
```

`LEVERAGE` is used for internal sizing and notional caps. In live mode the bot reads HTX account leverage before orders; set `SET_LEVERAGE_ON_START=true` only when you want startup to apply `LEVERAGE` to each tracked contract.

Signal periods:

```dotenv
EMA_MACRO_TIMEFRAME=1d
EMA_PULLBACK_TIMEFRAME=4h
EMA_TRIGGER_TIMEFRAME=1m
EMA_MACRO_FAST_MINUTES=36000
EMA_MACRO_SLOW_MINUTES=72000
EMA_PULLBACK_FAST_MINUTES=1440
EMA_PULLBACK_SLOW_MINUTES=2880
EMA_TRIGGER_FAST_MINUTES=50
EMA_TRIGGER_SLOW_MINUTES=100
```

Entry ladder:

```dotenv
EMA_POSITION_BUDGET_FRACTION=0.02
EMA_ENTRY_LADDER_FRACTIONS=0.50,0.50
EMA_ENTRY_LADDER_OFFSETS=0.0,0.01
```

Entry quality gates:

```dotenv
ENTRY_MIN_SCORE=0.03
ENTRY_MIN_RS60_ABS=0.002
ENTRY_MIN_RS30_ABS=0.001
ENTRY_MAX_NEW_LADDERS_PER_SIGNAL=5
ENTRY_RATE_LIMIT_LADDERS=10
ENTRY_RATE_LIMIT_WINDOW_MINUTES=60
```

Averaging:

```dotenv
EMA_AVERAGING_ENABLED=true
EMA_AVERAGING_DRAWDOWN_STEP=0.01
EMA_AVERAGING_BASE_FRACTION=0.50
EMA_AVERAGING_POWER=1.0
EMA_AVERAGING_INTERVAL_HOURS=8
EMA_MAX_AVERAGING_STAGES=2
```

`EMA_AVERAGING_BASE_FRACTION` is the fraction of the current open position used for each averaging stage. `EMA_MAX_AVERAGING_STAGES` is capped at 2.

Breakeven:

```dotenv
EMA_BREAKEVEN_ENABLED=true
EMA_BREAKEVEN_AFTER_HOURS=48
EMA_BREAKEVEN_REPRICE_MINUTES=15
EMA_BREAKEVEN_FEE_BUFFER=0.0002
EMA_BREAKEVEN_EXIT_FRACTIONS=1.0
```

External reference prices:

```dotenv
EXTERNAL_PRICE_FEED_ENABLED=true
EXTERNAL_PRICE_REST_POLL_INTERVAL_SEC=1
EXTERNAL_PRICE_REST_TIMEOUT_SEC=3
EXTERNAL_PRICE_MAX_PRICE_AGE_MS=3000
EXTERNAL_PRICE_STALE_AFTER_MS=3000
EXTERNAL_PRICE_ENTRY_FILTER_ENABLED=true
EXTERNAL_PRICE_MAX_HTX_PREMIUM_FOR_LONG_BPS=15
EXTERNAL_PRICE_MAX_HTX_DISCOUNT_FOR_SHORT_BPS=15
EXTERNAL_PRICE_DIRECTIONAL_1M_GATE_ENABLED=true
EXTERNAL_PRICE_DIRECTIONAL_ENTRY_1M_BLOCK_BPS=50
EXTERNAL_PRICE_DIRECTIONAL_AVERAGING_1M_BLOCK_BPS=50
EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED=false
```

Per-profile overrides are supported through `LONG_` and `SHORT_` prefixes, for example `LONG_LEVERAGE=30` or `SHORT_LEVERAGE=30`.

## Generated Files

Each profile writes state and logs into its own directory:

- `long/bot_futures_state.json`, `short/bot_futures_short_state.json`
- trade event CSV
- cycle stats CSV
- `signal_analytics.csv` and `signal_analytics.jsonl`
- `diagnostics.csv` and `diagnostics.jsonl`
- `account_pnl.csv`
- `external_price_feed.csv`
- `bot_futures_macro.csv`

These runtime CSV/JSONL artifacts are local audit output and are ignored by git. Local secrets live in `.env`, `long/.env`, or `short/.env`; these files are also ignored by git.

## Live Launch Checklist

1. Run `python -m pytest -q` and any mock/stub exchange scenario checks before starting the bot.
2. Combined mode requires identical API credentials across enabled profiles.
3. Confirm HTX account leverage or set `ACCOUNT_LEVERAGE`.
4. Keep conservative caps first: `EMA_POSITION_BUDGET_FRACTION=0.02`, `EMA_MAX_POSITION_MARGIN_FRACTION=0.03`, `EMA_MAX_TOTAL_MARGIN_FRACTION=0.50`.
5. Check `long/.env` and `short/.env` for profile-specific overrides.
6. Confirm HTX position mode is one-way and margin mode is cross.
7. Confirm runtime diagnostics/log artifacts are not staged or committed. If old signed HTX diagnostics were ever shared, rotate the affected HTX API key before live start.
8. Start with small budgets and watch the first cycles closely.

## Project Layout

```text
bot.py                  CLI entry point
config.py               profile/env configuration
htxbot/combined.py      combined long/short runner
htxbot/app.py           bot composition and file setup
htxbot/signal_engine.py signal construction
htxbot/strategy.py      entry, averaging, exit, risk gates
htxbot/exchange.py      HTX/CCXT exchange layer
htxbot/state.py         state sync and persistence
htxbot/monitoring.py    CSV/JSONL logging
tests/                  pytest coverage
```
