# HTX Futures EMA Pullback Bot

[Русский](docs/readme.ru.md) | [简体中文](docs/readme.zh-CN.md) | [Strategy Details](strategy.md)

HTX Futures EMA Pullback Bot is a Python crypto trading bot for HTX USDT-M futures. It runs long and short profiles, scans a configurable altcoin universe, builds EMA pullback signals on closed candles, and manages entries/exits with limit orders, averaging, reduce-only exits, and breakeven behavior.

The project is designed for traders and developers who want a practical crypto trading bot with quick setup, flexible `.env` configuration, and a production-ready EMA pullback strategy out of the box. By default, `DRY_RUN=true`, so your first run validates config, market loading, signal generation, logs, and planned orders without placing real trades.

> Futures trading is risky. This repository is software, not financial advice. Audit the code, test your config, and understand each parameter before going live.

## Why This Bot

- **Fast first start**: install dependencies, copy `.env.example`, run `python bot.py`.
- **Dry-run by default**: `DRY_RUN=true` protects your first run from accidental live orders.
- **Long + short profiles**: run `long`, `short`, or both together.
- **Built-in EMA pullback strategy**: macro trend, pullback recovery, trigger EMA, BTC-relative strength, BTC risk filter, score-based ranking, top-N selection, and crowded-market throttling.
- **Flexible `.env` tuning**: EMA windows, entry gates, risk budget, averaging, breakeven, external reference prices, and macro overlay are configurable without code edits.
- **USDT-M futures workflow**: HTX linear swap markets, cross margin, one-way mode assumptions, post-only entries, and reduce-only exits.
- **CCXT exchange layer**: current implementation targets HTX, while CCXT abstractions make future exchange adaptation easier.
- **Operational CSV logs**: trade events, cycle stats, macro context, and external-price diagnostics.
- **Tested core behavior**: pytest suite covers signals, entry gates, averaging, breakeven, profiles, and safety branches.

## Strategy Overview

The default active strategy is `ema_pullback`: trade altcoins in the direction of the higher-timeframe EMA trend after a medium-timeframe pullback and local recovery confirmation.

Default EMA map:

| Layer | Parameter | Timeframe | Effective EMA |
|---|---:|---:|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES=36000` | `1d` | EMA25 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES=72000` | `1d` | EMA50 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES=1440` | `4h` | EMA6 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES=2880` | `4h` | EMA12 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES=50` | `1m` | EMA50 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES=100` | `1m` | EMA100 |

Long entry requires:

```text
EMA25D > EMA50D
EMA1D recovered above EMA2D after a recent pullback
EMA50 > EMA100
rs60 >= EMA_LONG_MIN_RS60
btc_return_30m >= EMA_BTC_LONG_MIN_RETURN_30M
```

Short entry uses mirrored logic:

```text
EMA25D < EMA50D
EMA1D recovered below EMA2D after a recent bounce
EMA50 < EMA100
rs60 <= EMA_SHORT_MAX_RS60
btc_return_30m <= EMA_BTC_SHORT_MAX_RETURN_30M
```

Signals are not just one crossover. They combine macro trend direction, recovery-cross age, recovery gap, local trigger alignment, relative strength vs BTC, short-term BTC risk, and a ranking score. Full technical details are in [strategy.md](strategy.md).

## Risk and Position Management

The default route does not use a classic stop-loss order. Risk is constrained by position limits, staged entries, averaging caps, signal-health checks, reduce-only exits, and breakeven/time-exit behavior.

Key defaults:

- `DRY_RUN=true`
- `BOT_PROFILES=long,short`
- Initial entry is split into two limit orders by default
- New entries pass quality gates: score, RS60, RS30, top-N, rate-limit, and crowded-market rules
- Averaging is constrained by stage count, spacing, drawdown step, and signal health
- Breakeven activates after configured hold time, blocks further adds, and re-prices reduce-only exits
- Combined mode prevents long and short profiles from opening opposite exposure on the same symbol

For live trading, you need HTX API credentials and known account leverage settings. `LEVERAGE` is used for sizing and notional limits; the bot does not auto-change leverage on exchange at startup.

## Quick Start

Requirements:

- Locally validated with Python 3.14; Python 3.11+ recommended
- HTX account for live mode: [open HTX with invite code `6hc25223`](https://www.htx.com/invite/en-us/1f?invite_code=6hc25223)
- Optional MEXC account for reference-market analysis: [open MEXC via referral link](https://promote.mexc.com/r/lxcLKaZgvh)
- USDT-M futures permission when `DRY_RUN=false`

Install and run dry-run:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Run a single profile:

```bash
python bot.py --profiles long
python bot.py --profiles short
```

Run both profiles explicitly:

```bash
python bot.py --profiles long,short
```

On the first dry-run, you should see market loading, BTC benchmark selection, signal updates, entry-gate decisions, and planned order behavior without real execution.

## Configuration

Copy `.env.example` to `.env` and override only what you need. For compatibility, `long/.env` and `short/.env` are also supported.

Minimum live-related values:

```dotenv
HTX_API_KEY=
HTX_API_SECRET=
DRY_RUN=true
BOT_PROFILES=long,short
```

Useful profile overrides:

```dotenv
LONG_DRY_RUN=true
SHORT_DRY_RUN=true
LONG_LEVERAGE=30
SHORT_LEVERAGE=30
```

Strategy tuning examples:

```dotenv
EMA_MACRO_TIMEFRAME=1d
EMA_PULLBACK_TIMEFRAME=4h
EMA_TRIGGER_TIMEFRAME=1m
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES=2880
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES=1440
EMA_PULLBACK_RECOVERY_GAP=0.001
ENTRY_MIN_SCORE=0.03
ENTRY_RATE_LIMIT_LADDERS=10
```

## Macro Gold/BTC RSI Overlay

The bot includes a macro overlay that compares a gold proxy (usually XAUT) versus BTC using RSI and relative gold/BTC movement. It is not a standalone entry strategy; it is a market-regime layer that can reduce risk, block recovery logic, or make exits more conservative when crypto underperforms defensive assets.

Default macro settings:

```dotenv
ENABLE_GOLD_BTC_RSI_OVERLAY=true
MACRO_GOLD_COINS=xaut
GOLD_TIMEFRAME=4h
GOLD_RSI_PERIOD=14
GOLD_CACHE_TTL_SEC=900
GOLD_STRONG_RSI=60
GOLD_WEAK_RSI=40
BTC_STRONG_RSI=60
BTC_WEAK_RSI=40
RSI_SPREAD_THRESHOLD=15
```

Regimes:

- `crypto_underperforms_gold`
- `crypto_risk_on`
- `broad_liquidity_risk_on`
- `deleveraging`
- `neutral` / `macro_unavailable`

Macro context is cached, logged to CSV, and used in decisions such as new-entry block, averaging block, recovery block, ladder multiplier, and time-exit multiplier. See [strategy.md](strategy.md) for exact state logic.

## MEXC Reference Price Signals

External price radar compares HTX order-book mid price with public MEXC book-ticker data to detect cross-exchange premium/discount, stale references, and short-term impulse before entries and during position management.

Key defaults:

```dotenv
EXTERNAL_PRICE_FEED_ENABLED=true
EXTERNAL_PRICE_REFERENCE_EXCHANGES=mexc
EXTERNAL_PRICE_REST_POLL_INTERVAL_SEC=1
EXTERNAL_PRICE_MAX_HTX_PREMIUM_FOR_LONG_BPS=15
EXTERNAL_PRICE_MAX_HTX_DISCOUNT_FOR_SHORT_BPS=15
EXTERNAL_PRICE_BLOCK_IF_DIVERGENCE_1M_BPS=50
EXTERNAL_PRICE_MEXC_LEAD_THRESHOLD_BPS_30S=5
EXTERNAL_PRICE_IMPULSE_SCORE_BONUS=1.0
EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED=true
```

## Porting to Other Exchanges

The exchange client is built on CCXT abstractions (markets, precision, timeframes, order primitives). This improves portability, but the current release remains HTX-specific for live trading details (contract conventions, one-way checks, leverage reads, reduce-only parameters, and price-band handling).

Migration to another CCXT-supported exchange is an adapter task: implement exchange factory/credentials/market filters/position-mode handling/order flags and add exchange-specific tests before live use.

## Monitoring

Runtime CSV files are produced locally and ignored by Git:

- trade events
- cycle statistics
- macro overlay context
- external reference-price diagnostics
- markets cache and state files

These artifacts explain why signals were accepted/rejected, how sizing was computed, when averaging was blocked, and how exits were adjusted.

## Tests

Run tests:

```bash
python -m pytest -q
```

Current local baseline in documentation:

```text
90 passed, 4 subtests passed
```

## Project Structure

```text
bot.py                 CLI entrypoint
config.py              Pydantic configuration and profile loading
htxbot/                Engine, exchange, signals, strategy, state, monitoring
tests/                 Unified pytest suite
.env.example           Safe configuration template
strategy.md            Full strategy behavior details
docs/                  Localized release documentation
```

## Security

- Never commit `.env`, `long/.env`, or `short/.env`
- Scope API key permissions tightly and rotate on any suspicion
- Start with `DRY_RUN=true`
- Use minimum API permissions before intentional live deployment

## Disclaimer

Automated futures trading can produce rapid losses, especially with leverage, liquidity gaps, exchange incidents, configuration mistakes, or changing market regimes. This repository provides code and documentation; you are responsible for testing, deployment, exchange settings, risk limits, and all trading decisions.

## License

MIT License. See [LICENSE](LICENSE).
