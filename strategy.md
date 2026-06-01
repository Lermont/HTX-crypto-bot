# Стратегия HTX Futures EMA Pullback

Документ описывает текущую фактическую стратегию проекта после аудита конфигурации. Активный маршрут по умолчанию — `ema_pullback` для HTX USDT-M futures, с двумя профилями: `long` и `short`.

## 1. Режим Работы

Бот запускается через `python bot.py`. По умолчанию `BOT_PROFILES=long,short`, поэтому `CombinedHtxFuturesBot` поднимает оба профиля в одном процессе.

Combined mode делает три важные вещи:

- использует один общий exchange-wrapper с кэшем market data;
- разделяет account-PnL runtime между профилями;
- помечает символ занятым для второго профиля, если у первого уже есть позиция, entry orders или exit orders;
- после рестарта учитывает не только локальный state, но и видимые биржевые позиции/ордера другого профиля, чтобы пустой state не создавал ложный `unexpected_*` для противоположной стороны.

Combined mode требует одинаковые HTX API credentials для всех включенных профилей.

## 2. Торгуемый Рынок

Активная реализация ориентирована на HTX linear USDT-M futures.

- `default_type=swap`;
- quote currency: `USDT`;
- margin mode: `cross`;
- position mode: `one-way`;
- entry side: `buy` для long, `sell` для short;
- exit side: `sell` для long, `buy` для short.

Плечо в конфиге разделено по смыслу:

- `LEVERAGE` — внутренний множитель sizing/notional caps;
- `ACCOUNT_LEVERAGE` — ручное live-плечо аккаунта, если его нужно явно подсказать боту.

Бот не исходит из того, что может безопасно менять плечо на бирже при старте. В live он использует leverage, прочитанный с HTX, либо `ACCOUNT_LEVERAGE`.

## 3. Карта EMA

Периоды задаются в минутах, затем конвертируются в количество свечей выбранного timeframe.

| Слой | Параметры | Timeframe | По умолчанию |
|---|---:|---:|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES=36000` | `1d` | EMA25 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES=72000` | `1d` | EMA50 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES=1440` | `4h` | EMA6 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES=2880` | `4h` | EMA12 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES=50` | `1m` | EMA50 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES=100` | `1m` | EMA100 |

Используются только закрытые свечи. Текущая незакрытая свеча не участвует в расчете сигнала.

## 4. Long-Сигнал

Long-сигнал считается пригодным для нового входа, когда одновременно выполняется базовая логика:

```text
EMA25D > EMA50D
EMA1D восстановилась выше EMA2D после недавнего отката
EMA50 > EMA100
rs60 >= EMA_LONG_MIN_RS60, если EMA_USE_RS_CONFIRMATION=true
btc_return_30m >= EMA_BTC_LONG_MIN_RETURN_30M, если EMA_USE_BTC_RISK_FILTER=true
```

Pullback recovery проверяет не просто факт `EMA1D > EMA2D`, а недавнее восстановление после состояния против направления сделки. Возраст recovery-cross ограничен `EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES`, а история поиска — `EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES`.

## 5. Short-Сигнал

Short-сигнал зеркален:

```text
EMA25D < EMA50D
EMA1D восстановилась ниже EMA2D после недавнего отскока
EMA50 < EMA100
rs60 <= EMA_SHORT_MAX_RS60, если EMA_USE_RS_CONFIRMATION=true
btc_return_30m <= EMA_BTC_SHORT_MAX_RETURN_30M, если EMA_USE_BTC_RISK_FILTER=true
```

Для short относительная сила и EMA-разрывы приводятся к направлению позиции через directional value, чтобы quality gates работали симметрично.

## 6. Score И Entry Gate

Сигнал содержит `score`, `rs30`, `rs60`, EMA-значения, BTC context, volatility context, macro context и external-price context.

Флаги сигнала разделены по назначению:

- `valid` — направленный сигнал собран и не противоречит базовой структуре стратегии;
- `entry_valid` — пройдены условия именно для нового входа;
- `add_valid` — сигнал достаточно здоров для добора уже открытой позиции, даже если полный new-entry gate сейчас не проходит.

Для нового входа дополнительно проверяются:

- `ENTRY_MIN_SCORE`;
- `ENTRY_MIN_RS60_ABS`;
- `ENTRY_MIN_RS30_ABS`;
- `ENTRY_MAX_NEW_LADDERS_PER_SIGNAL`;
- `ENTRY_RATE_LIMIT_LADDERS` за `ENTRY_RATE_LIMIT_WINDOW_MINUTES`;
- crowded-market правила: `ENTRY_CROWDED_SIGNAL_FRACTION`, `ENTRY_CROWDED_MIN_SIGNALS`, `ENTRY_CROWDED_*`.

Когда рынок “crowded”, бот ужесточает пороги и уменьшает число новых ladder entries на один сигнал. Это защищает от массового открытия однотипных позиций при синхронном движении альтов.

## 7. Внешний Reference Price

`ExternalPriceFeed` использует MEXC book ticker как reference для HTX.

Активные проверки:

- валидность HTX/MEXC bid-ask и минимальный notional стакана;
- freshness через `EXTERNAL_PRICE_MAX_PRICE_AGE_MS` и `EXTERNAL_PRICE_STALE_AFTER_MS`;
- premium/discount HTX относительно MEXC;
- divergence за 1 минуту между HTX и MEXC;
- directional 1m gate для entry и averaging;
- impulse score bonus при движении MEXC в сторону сделки;
- tighten exit ladder при неблагоприятном HTX premium/discount.

Если reference устарел, поведение задается парой:

```text
EXTERNAL_PRICE_DISABLE_TRADING_IF_REFERENCE_STALE
EXTERNAL_PRICE_IGNORE_REFERENCE_IF_STALE
```

По умолчанию stale reference не обязан полностью блокировать торговлю, но невалидный context записывается в `external_price_feed.csv` и `signal_analytics`.

## 8. Initial Entry

Начальный вход ставится только если:

- символ входит в `entry_symbols`;
- нет открытой позиции;
- нет активных entry orders;
- позиция не frozen и не zombie;
- нет cooldown;
- сигнал прошел quality gate;
- macro overlay не запретил новые входы;
- combined mode не зарезервировал символ другим профилем;
- risk caps и external entry checks разрешили сделку.

Бюджет:

```text
base_margin_budget = account_equity * EMA_POSITION_BUDGET_FRACTION
```

По умолчанию:

```text
EMA_POSITION_BUDGET_FRACTION=0.02
EMA_ENTRY_LADDER_FRACTIONS=0.50,0.50
EMA_ENTRY_LADDER_OFFSETS=0.0,0.01
```

Фактический margin budget дополнительно ограничивается:

- `MIN_QUOTE_RESERVE`;
- `MAX_ACTIVE_POSITIONS`;
- `EMA_MAX_POSITION_MARGIN_FRACTION`;
- `EMA_MAX_TOTAL_MARGIN_FRACTION`;
- exchange minimum amount/notional;
- доступной free margin;
- macro budget multiplier;
- volatility/account multipliers, если включены.

В live entry orders отправляются как limit/post-only, если `POST_ONLY_ENABLED=true`.

## 9. Averaging

Активный механизм добора — EMA averaging. Он управляется параметрами `EMA_AVERAGING_*`.

Добор возможен только если:

- `EMA_AVERAGING_ENABLED=true`;
- позиция открыта и имеет entry price;
- breakeven еще не активирован;
- позиция не старше `EMA_BREAKEVEN_AFTER_HOURS` в части добора;
- нет активных entry orders;
- позиция не frozen/zombie;
- у позиции есть exit ladder;
- сигнал валиден и `add_valid=true`;
- macro context не запретил averaging;
- BTC/external directional gates не блокируют добор;
- выдержан `EMA_AVERAGING_INTERVAL_HOURS`;
- не превышен `EMA_MAX_AVERAGING_STAGES`.
- `pullback_valid=true`, если `EMA_AVERAGING_REQUIRE_PULLBACK_RECOVERY=true`.

Порог drawdown:

```text
stage_1 = max(AVERAGING_DRAWDOWN_STEP_1, EMA_AVERAGING_MIN_DRAWDOWN_STEP)
stage_2 = max(AVERAGING_DRAWDOWN_STEP_2, EMA_AVERAGING_MIN_DRAWDOWN_STEP * 2)
...
```

Effective threshold дополнительно расширяется, если сигнал содержит ATR или daily volatility:

```text
atr_floor = atr_rate * EMA_AVERAGING_MIN_ATR_MULTIPLIER * stage
daily_vol_floor = daily_volatility * EMA_AVERAGING_MIN_DAILY_VOLATILITY_FRACTION * stage
effective_threshold = max(configured_stage, min_stage_floor, atr_floor, daily_vol_floor)
```

По умолчанию:

```text
EMA_AVERAGING_DRAWDOWN_STEP=0.01
EMA_AVERAGING_MIN_DRAWDOWN_STEP=0.01
EMA_AVERAGING_MIN_ATR_MULTIPLIER=1.0
EMA_AVERAGING_MIN_DAILY_VOLATILITY_FRACTION=0.18
EMA_AVERAGING_REQUIRE_PULLBACK_RECOVERY=true
EMA_MAX_AVERAGING_STAGES=2
```

Размер добора:

```text
base_notional = initial_entry_notional
ratio = max(current_position_notional / base_notional, 1)
desired_notional = current_position_notional * EMA_AVERAGING_BASE_FRACTION
```

Если добор разрешен account-PnL context, применяется `ACCOUNT_AVERAGING_BUDGET_SCALE`.
`EMA_AVERAGING_BASE_FRACTION` также читает legacy alias `EMA_AVERAGING_POSITION_FRACTION`; это доля от текущей позиции, а не от начального входа. `EMA_AVERAGING_POWER` сохраняется как legacy-настройка для совместимости логов, но размер добора не может превышать явно заданную долю текущей позиции.

По умолчанию:

```text
EMA_AVERAGING_BASE_FRACTION=0.50
EMA_AVERAGING_POWER=1.0
ACCOUNT_AVERAGING_BUDGET_SCALE=0.50
```

## 10. Account-PnL Guard

Бот ведет общий account-PnL runtime для combined profiles и пишет `account_pnl.csv`.

Две opt-in функции:

- `account_profit_unload`: частично закрывает прибыльные позиции, когда общий account PnL находится в верхнем диапазоне;
- `account_averaging`: разрешает/масштабирует доборы только около account-PnL trough и после прекращения падения PnL.

Ключевые параметры:

```text
ACCOUNT_PNL_ENABLED=true
ACCOUNT_PROFIT_UNLOAD_ENABLED=false
ACCOUNT_AVERAGING_ENABLED=false
ACCOUNT_PNL_WINDOW_MINUTES=360
ACCOUNT_PNL_SAMPLE_INTERVAL_SEC=30
```

## 11. Exit Ladder

Выходы ставятся reduce-only. Активный default — adaptive exit ladder.

Режим выбирается по отношению текущего notional позиции к initial notional:

- normal: до `EMA_EXIT_MEDIUM_POSITION_RATIO`;
- medium: до `EMA_EXIT_HEAVY_POSITION_RATIO`;
- heavy: выше heavy threshold.

Параметры по умолчанию:

```text
EMA_ADAPTIVE_EXIT_ENABLED=true
EMA_EXIT_NORMAL_LADDER_FRACTIONS=0.35,0.25,0.25,0.15
EMA_EXIT_NORMAL_LADDER_MARKUPS=0.008,0.016,0.030,0.050
EMA_EXIT_MEDIUM_LADDER_FRACTIONS=0.45,0.30,0.15,0.10
EMA_EXIT_MEDIUM_LADDER_MARKUPS=0.004,0.010,0.020,0.035
EMA_EXIT_HEAVY_LADDER_FRACTIONS=0.60,0.25,0.15
EMA_EXIT_HEAVY_LADDER_MARKUPS=0.003,0.008,0.015
```

Normal mode по умолчанию использует только fixed reduce-only ladder. Runner/trailing остаётся opt-in через `EMA_EXIT_RUNNER_ENABLED=true` и `EMA_EXIT_TRAILING_ENABLED=true`; тогда остаток может закрываться при pullback от лучшей цены или при достижении take-profit markup.

Profit floor учитывает комиссии:

```text
profit_floor >= (buy_fee_rate + sell_fee_rate) * min_profit_fee_multiplier
```

Если HTX отклоняет reduce-only ladder с причиной, что closeable amount уже зарезервирован существующими close orders, бот переводит ladder в pending mode (`pending_closeable:*`) и не повторяет постановку только из-за истечения таймаута, пока snapshot показывает `position_available=0` и `position_frozen>0`. Retry возобновляется, когда появляется closeable amount, меняется размер позиции или видимые close orders можно принять/отменить.

После `HARD_TIME_EXIT_AFTER_HOURS=96` включается bounded-loss маршрут: бот может постепенно перестраивать reduce-only выход с ограничением `HARD_TIME_EXIT_MAX_LOSS_ON_NOTIONAL=0.03`, начиная с `HARD_TIME_EXIT_CLOSE_FRACTION=0.25` и увеличивая долю каждые `HARD_TIME_EXIT_STEP_MINUTES`.

## 12. Breakeven

Breakeven заменяет обычный exit ladder после заданного времени удержания:

```text
EMA_BREAKEVEN_ENABLED=true
EMA_BREAKEVEN_AFTER_HOURS=48
EMA_BREAKEVEN_REPRICE_MINUTES=15
EMA_BREAKEVEN_FEE_BUFFER=0.0002
EMA_BREAKEVEN_EXIT_FRACTIONS=1.0
```

При активации:

- новые entry orders отменяются;
- `frozen_no_more_buys=true`;
- sell ladder переводится в mode `breakeven`;
- reduce-only exit ставится около entry price с fee buffer;
- stale breakeven ladder периодически переставляется.

## 13. Dust И Tiny Cleanup

В live режиме есть защитные cleanup-ветки:

- `DUST_CLOSE_ENABLED` закрывает слишком маленькую позицию reduce-only market;
- `TINY_ENTRY_CLOSE_ENABLED` закрывает микроскопический частичный entry, если он слишком мал относительно planned budget.

Эти cleanup-действия отправляют реальные reduce-only market orders, поэтому они покрываются unit-тестами через mock/stub exchange.

## 14. Macro Overlay

Macro overlay сравнивает XAUT/BTC context через RSI и может:

- снижать budget multiplier;
- расширять ladder multiplier;
- запрещать новые входы в panic/risk-off;
- запрещать averaging;
- ускорять breakeven через time-exit multiplier.

По умолчанию XAUT используется только как macro/reference input и не добавляется в список торгуемых монет.

## 15. Состояние И Логи

Профили пишут отдельные state/log файлы:

- `long/bot_futures_state.json`;
- `short/bot_futures_short_state.json`;
- trade event CSV;
- cycle stats CSV;
- `signal_analytics.csv`;
- `signal_analytics.jsonl`;
- `diagnostics.csv`;
- `diagnostics.jsonl`;
- `account_pnl.csv`;
- `external_price_feed.csv`;
- `bot_futures_macro.csv`.

`signal_analytics.csv` содержит текущую EMA-схему: `ema50`, `ema100`, `ema1d`, `ema2d`, `ema25d`, `ema50d`, а также компоненты `macro_gap`, `trigger_gap`, `pullback_depth`.

Все diagnostics/signal analytics/runtime CSV/JSONL файлы являются локальными артефактами аудита и не должны попадать в git. Если старые diagnostics со signed HTX URL уже были опубликованы или отправлены третьим лицам, API key нужно ротировать до live-старта.

## 16. Что Удалено Из Конфига

После аудита из `config.py` удалены параметры, которые не имели реального runtime-потребителя:

- дубли EMA/ladder полей в `SignalSettings`, `SellSettings` и `StrategySettings`;
- legacy entry-expansion thresholds и multipliers;
- неиспользуемые time-exit/reprice/dynamic-time-exit поля;
- старые неиспользуемые controlled-loss ladder поля;
- неиспользуемые external-price поля `use_existing_trading_universe`, `only_usdt_pairs`, `reconnect_on_stale_ms`, `tighten_ladder_factor`;
- неиспользуемый monitoring TTL.

Оставлены выключенные по умолчанию, но реально подключенные механики: volatility sizing/recovery, BTC risk multiplier, funding-aware exit, dynamic profit floor, hard/controlled/absolute force exit helpers.
Controlled-loss exit при активации двигает цену закрытия от `CONTROLLED_LOSS_MIN_MOVE_FRACTION` к reference price за `CONTROLLED_LOSS_RAMP_MINUTES`; скорость ramp ускоряется при отрицательном directional `trend_ema_gap`/`macro_gap` и неблагоприятном macro overlay, а stale ladder перестраивается через `CONTROLLED_LOSS_REPRICE_MINUTES`.

## 17. Live-Готовность

Перед live-запуском:

1. Прогнать `python -m pytest -q`.
2. Проверить ключевые runtime-сценарии через mock/stub exchange без подключения к live-аккаунту.
3. Проверить `long/.env` и `short/.env`: там не должно быть противоречий с `.env`.
4. Убедиться, что `EMA_POSITION_BUDGET_FRACTION`, `EMA_MAX_POSITION_MARGIN_FRACTION`, `EMA_MAX_TOTAL_MARGIN_FRACTION` остаются консервативными.
5. Проверить HTX account leverage или задать `ACCOUNT_LEVERAGE`.
6. Проверить, что runtime diagnostics/signal analytics файлы не staged и не tracked git.
7. После этого запускать бот с минимальными бюджетами и внимательно наблюдать первые циклы.
