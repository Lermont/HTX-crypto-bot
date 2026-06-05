# HTX Futures EMA Pullback Bot

[Русский README](docs/readme.ru.md) | [简体中文](docs/readme.zh-CN.md) | [Strategy details](strategy.md)

Python bot for HTX USDT-M futures. The active trading route is an EMA Pullback strategy with separate `long` and `short` profiles that can run together in one process, share market data, and avoid opening opposite exposure on the same symbol.

The bot is a live-ordering application: startup requires explicit HTX API credentials, matching API-account routing for both profiles in combined mode, and an account leverage that the bot can read from HTX or that you define via `ACCOUNT_LEVERAGE`.

Trading futures is risky. This repository is software, not financial advice. Audit the code and run the unit tests plus mock/stub exchange checks before sending live orders.

## What Is Active Now

The default route is `ema_pullback`.

- Exchange: HTX linear USDT-M futures through CCXT.
- Margin/position mode: cross margin, one-way mode.
- Profiles: `long`, `short`, or both via `BOT_PROFILES=long,short`.
- Signals: closed candles only, with macro EMA trend, pullback recovery, trigger EMA, relative strength to BTC, BTC 30m filter, score ranking, and entry throttling.
- Initial entry: post-only limit ladder using `BUYING.ladder_fractions` and `BUYING.ladder_offsets`.
- Averaging: enabled by `EMA_AVERAGING_ENABLED`; requires an open position, no active entry orders, healthy signal `add_valid`, drawdown threshold, age limit, interval limit, risk caps, and optional account-PnL context.
- Exit: exchange-side reduce-only hard stop-loss, adaptive reduce-only exit ladder, optional trailing/runner logic, account profit unload, and breakeven after the configured holding time.
- External reference prices: MEXC book ticker is used as a reference for entry blocks, directional 1m blocks, impulse bonus, and tighter exits.
- Monitoring: CSV/JSONL logs for trades, cycle stats, macro context, external prices, signal analytics, diagnostics, and account PnL.

The default live route places an exchange-side reduce-only hard stop-loss from entry (`HARD_STOP_LOSS_PCT=0.02`) and can widen it from closed-candle ATR (`HARD_STOP_LOSS_ATR_MULTIPLIER=2.0`, capped by `HARD_STOP_LOSS_ATR_MAX_PCT=0.03`). The fixed stop is the fallback when ATR is unavailable after restart.

## Strategy Snapshot

Default EMA map:

| Layer | Config | Timeframe | Effective EMA |
|---|---:|---:|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES=2880` | `1h` | EMA48 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES=7200` | `1h` | EMA120 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES=120` | `5m` | EMA24 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES=360` | `5m` | EMA72 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES=120` | `5m` | EMA24 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES=360` | `5m` | EMA72 |

Long entry requires the macro and trigger EMAs to point up, optional RS confirmation, and a BTC 30m filter that is not too negative. Pullback recovery is a quality penalty by default; set `EMA_ENTRY_REQUIRE_PULLBACK_RECOVERY=true` to make it a hard entry gate again.

Short entry mirrors the same logic downward: macro and trigger EMAs point down, optional RS confirmation, and BTC 30m is not too positive. The same pullback-recovery switch applies symmetrically.

Signal flags are split by use: `valid` means the symbol has a coherent directional signal, `entry_valid` means a full new-entry gate passed, and `add_valid` is the stricter health gate used for averaging an already open position. The default EMA entry gate also requires non-choppy trigger candles and recent volume confirmation, so a late EMA cross alone no longer opens or averages a position. Signal scoring logic uses a multiplicative hybrid model where negative indicators apply penalty multipliers.

If HTX rejects a reduce-only exit because the whole position is reported as closeable-reserved/frozen, the bot keeps a pending exit-ladder state and waits for available closeable amount, a position-size change, or visible close orders to adopt/cancel. It does not keep duplicating exit ladders on timeout alone.

For implementation-level details, see [strategy.md](strategy.md).

## Quick Start

Requirements:

- Python 3.12 verified locally; Python 3.11+ recommended.
- HTX futures account for live mode: [open HTX with invite code `6hc25223`](https://www.htx.com/invite/en-us/1f?invite_code=6hc25223).
- Optional MEXC account for reference market analysis: [open MEXC via referral link](https://promote.mexc.com/r/lxcLKaZgvh). The current MEXC radar uses public market data and does not require MEXC API keys.
- API key/secret with the required futures permissions for live trading.

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
COINS=aave,ada,...
# Optional second key for symbols enabled on another HTX API key:
HTX_API_KEY_2=
HTX_API_SECRET_2=
COINS_2=1inch,aixbt,...
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
EMA_MACRO_TIMEFRAME=1h
EMA_PULLBACK_TIMEFRAME=5m
EMA_TRIGGER_TIMEFRAME=5m
EMA_MACRO_FAST_MINUTES=2880
EMA_MACRO_SLOW_MINUTES=7200
EMA_PULLBACK_FAST_MINUTES=120
EMA_PULLBACK_SLOW_MINUTES=360
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES=720
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES=180
EMA_ENTRY_REQUIRE_PULLBACK_RECOVERY=false
EMA_TRIGGER_FAST_MINUTES=120
EMA_TRIGGER_SLOW_MINUTES=360
```

Entry ladder:

```dotenv
EMA_POSITION_BUDGET_FRACTION=0.02
EMA_ENTRY_LADDER_FRACTIONS=0.50,0.50
EMA_ENTRY_LADDER_OFFSETS=0.0,0.01
```

Entry quality gates:

```dotenv
EMA_CHOP_FILTER_ENABLED=true
EMA_CHOP_PERIOD=14
EMA_CHOP_MAX=61.8
EMA_VOLUME_CONFIRMATION_ENABLED=true
EMA_VOLUME_SHORT_WINDOW=5
EMA_VOLUME_LONG_WINDOW=20
EMA_VOLUME_MIN_RATIO=1.05
EMA_VOLUME_MIN_DIRECTIONAL_FRACTION=0.0
EMA_VOLUME_SPIKE_FILTER_ENABLED=true
EMA_VOLUME_SPIKE_WINDOW=5
EMA_VOLUME_SPIKE_MIN_RATIO=1.80
EMA_VOLUME_ADVERSE_SPIKE_MIN_RATIO=2.00
EMA_VOLUME_PROFILE_FILTER_ENABLED=true
EMA_VOLUME_PROFILE_WINDOW=60
EMA_VOLUME_PROFILE_BINS=12
EMA_VOLUME_PROFILE_VALUE_AREA=0.70
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
EMA_AVERAGING_MIN_DRAWDOWN_STEP=0.01
EMA_AVERAGING_BASE_FRACTION=0.50
EMA_AVERAGING_POWER=1.0
EMA_AVERAGING_INTERVAL_HOURS=8
EMA_AVERAGING_MIN_ATR_MULTIPLIER=1.0
EMA_AVERAGING_MIN_DAILY_VOLATILITY_FRACTION=0.18
EMA_AVERAGING_REQUIRE_PULLBACK_RECOVERY=true
EMA_MAX_AVERAGING_STAGES=2
```

`EMA_AVERAGING_BASE_FRACTION` is the fraction of the current open position used for each averaging stage. `EMA_MAX_AVERAGING_STAGES` is capped at 2. The effective averaging drawdown threshold is never below `EMA_AVERAGING_MIN_DRAWDOWN_STEP * stage`, and live signals can widen it with ATR and daily-volatility floors. Averaging also requires pullback recovery by default, so a valid trigger alone cannot average into a no-rebound trend.

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

## Macro Gold/BTC RSI Overlay

The bot features a macro overlay that compares a gold proxy (usually XAUT) against BTC using RSI and relative gold/BTC movement. This is not an independent entry strategy, but a market regime detection layer: it can reduce risk, disable averaging, or make exits more conservative when crypto underperforms a defensive asset.

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

The overlay classifies regimes:

- `crypto_underperforms_gold`: gold is strong, while BTC is weak or significantly lagging. Long budget is reduced, short side can remain softer, averaging may be disabled, and time-exit accelerates.
- `crypto_risk_on`: BTC is strong, gold is lagging. The bot maintains normal long behavior and may reduce short aggressiveness.
- `broad_liquidity_risk_on`: BTC and gold are both strong. This is a constructive regime, but does not automatically increase leverage.
- `deleveraging`: BTC and gold are both weak. New entries may be blocked, averaging is disabled, and exits are accelerated.
- `neutral` or `macro_unavailable`: overlay does not add strong directional filters.

Macro context is cached, written to CSV, and used in decisions for new-entry blocks, averaging blocks, ladder multipliers, and time-exit multipliers. Exact regime logic is described in [strategy.md](strategy.md).

## MEXC Reference Price Signals

The external price radar compares the HTX order-book mid-price with the public MEXC book-ticker. Its purpose is to detect cross-exchange premium, discount, stale reference data, and short-term impulses before opening or managing a position.

Key default MEXC settings:

```dotenv
EXTERNAL_PRICE_FEED_ENABLED=true
EXTERNAL_PRICE_REFERENCE_EXCHANGES=mexc
EXTERNAL_PRICE_REST_POLL_INTERVAL_SEC=1
EXTERNAL_PRICE_MAX_HTX_PREMIUM_FOR_LONG_BPS=15
EXTERNAL_PRICE_MAX_HTX_DISCOUNT_FOR_SHORT_BPS=15
EXTERNAL_PRICE_BLOCK_IF_DIVERGENCE_1M_BPS=50
EXTERNAL_PRICE_MEXC_LEAD_THRESHOLD_BPS_30S=5
EXTERNAL_PRICE_IMPULSE_SCORE_BONUS=0.02
EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED=false
```

In practice:

- Long entry may be blocked if HTX is too expensive relative to MEXC.
- Short entry may be blocked if HTX is too cheap relative to MEXC.
- A large 1-minute HTX/MEXC divergence can send the symbol into cooldown.
- If MEXC moves ahead of HTX in the same direction, the candidate may receive an impulse score bonus.
- The exit ladder may be widened if the HTX premium/discount suggests a more cautious take-profit.
- If reference data is stale, the default behavior is to ignore the reference rather than shut down trading; this can be made stricter via `.env`.

The radar maps HTX symbols to MEXC spot-style `BASEUSDT` symbols and writes bid, ask, mid, spread, z-score, age, and short-window changes to the external price CSV.

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

These runtime CSV/JSONL artifacts are local audit output and are ignored by git. Local secrets live in the root `.env`; this file is also ignored by git.

## Live Launch Checklist

1. Run `python -m pytest -q` and any mock/stub exchange scenario checks before starting the bot.
2. Combined mode requires identical API credentials across enabled profiles.
3. Confirm HTX account leverage or set `ACCOUNT_LEVERAGE`.
4. Keep conservative caps first: `EMA_POSITION_BUDGET_FRACTION=0.02`, `EMA_MAX_POSITION_MARGIN_FRACTION=0.03`, `EMA_MAX_TOTAL_MARGIN_FRACTION=0.50`.
5. Check the root `.env` for profile-specific overrides.
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
