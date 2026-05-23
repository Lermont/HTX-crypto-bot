# Стратегия HTX futures bot 2.0.3

Документ описывает фактическую стратегию по текущему коду проекта. Локальные `.env`, `long/.env` и `short/.env` могут переопределять часть значений, поэтому ниже указаны дефолты из `config.py` и активное поведение при этих дефолтах.

## 1. Коротко

Активная торговая стратегия: `ema_pullback`.

Идея:

- торговать альткоины в направлении старшего EMA-тренда;
- входить после отката на среднем таймфрейме и восстановления локального EMA-триггера;
- дополнительно фильтровать новый вход через относительную силу к BTC, 30-минутное движение BTC, `score`, `rs60` и `rs30`;
- если одновременно появилось много валидных сигналов, выбирать только лучшие через top-N и crowded mode;
- ограничивать скорость набора новых позиций через rate-limit;
- открывать позицию двумя limit-ордерами;
- сопровождать позицию adaptive reduce-only exit ladder с отдельным runner-хвостом в normal-режиме;
- разрешать максимум пять усреднений до breakeven, если старший EMA-сигнал не сломан;
- если позиция не закрылась за 48 часов, отменять дальнейшие доборы и переводить выход в reduce-only breakeven.

Стоп-лосс в активном маршруте не выставляется. Stop-market, stop-limit, entry expansion, frozen recovery averaging, controlled-loss и absolute force exit по умолчанию не участвуют в основном торговом цикле. Исключение: сервисная логика может закрыть пылевую позицию reduce-only market-ордером, если включён `DUST_CLOSE_ENABLED`.

## 2. Профили и рынок

Бот поддерживает два профиля:

- `long`: вход `buy`, выход reduce-only `sell`;
- `short`: вход `sell`, выход reduce-only `buy`.

Общие режимы:

- биржа: HTX USDT-M futures через `ccxt.htx`;
- рынок: linear swap contracts, quote/settle `USDT`;
- margin mode: `cross`;
- position mode: `one-way`;
- `DRY_RUN=true` по умолчанию;
- `POST_ONLY_ENABLED=true` для входных limit-ордеров;
- `REDUCE_ONLY_ENABLED=true` для выходных ордеров;
- `LEVERAGE=30` используется как внутренний sizing-множитель для бюджета и notional-лимитов.

Важно: бот не меняет плечо HTX при старте. Для live-ордеров `leverRate` берётся из `ACCOUNT_LEVERAGE`, если он задан, иначе бот пытается прочитать текущее ручное плечо из HTX account/position info. Если ручное плечо определить нельзя, live-вход блокируется.

BTC используется только как benchmark. Если BTC-перпетуал найден в списке рынков, он исключается из entry universe и не торгуется как обычный символ.

По умолчанию запускаются оба профиля:

```text
BOT_PROFILES=long,short
```

В combined-режиме профили используют общий cached market-data exchange. Live combined-режим разрешён только если у профилей одинаковый `DRY_RUN` и одинаковые HTX API credentials.

Если один профиль уже держит позицию, entry orders или exit orders по символу, второй профиль считает символ зарезервированным, не открывает встречную экспозицию и отменяет свои tracked orders по этому символу.

Дефолтный список монет для `long`:

```text
eth, sol, bnb, xrp, ada, avax, link, dot, ltc, bch,
etc, trx, ton, sui, apt, op, near, sei, inj, fil,
atom, algo, pol, tao, icp, wld, grt, tia, hbar,
kas, xlm, kaito, ssv, lpt, pendle, ena, ondo, jup,
aave, uni, ldo, ethfi, zro, zk, 1inch, crv, orca,
hype, zec, xmr, dydx, ens, cake, comp, gala, axs,
sand
```

Дефолтный список монет для `short`:

```text
eth, sol, bnb, xrp, ada, avax, link, dot, ltc, bch,
doge, etc, trx, ton, sui, apt, arb, op, near, sei,
inj, fil, atom, algo, pol, tao, icp, wld, grt, pyth,
tia, hbar, xlm, kaito, ssv, lpt, pendle, ena, jup,
uni, ldo, ethfi, zro, zk, 1inch, crv, orca, zec,
xmr, dydx, ens, cake, comp, gala, axs, cfx, sand
```

## 3. Основной цикл

Один цикл `run` делает:

1. Сбрасывает private caches.
2. Обновляет signal cache на последнюю закрытую trigger-свечу.
3. Готовит entry gate для новых позиций: quality, top-N, rate-limit, crowded mode.
4. Последовательно вызывает `step_symbol` для каждого tracked symbol.
5. Сохраняет state, если `DRY_RUN=false`.
6. Спит `POLL_INTERVAL_SEC=3`.

`step_symbol` для каждого символа:

1. Загружает position snapshot.
2. Загружает open orders.
3. Синхронизирует локальный state с биржей.
4. Проверяет пылевое закрытие.
5. Проверяет reserved-symbol gate combined-профиля.
6. Валидирует exit orders и entry orders.
7. Управляет активными entry orders.
8. Если позиция открыта, сопровождает выход и, если разрешено, усреднение.
9. Если позиции нет, пробует открыть новый initial entry.

Если position snapshot или open orders недоступны, символ пропускается fail-closed на этом цикле.

## 4. Сбор сигнала

Сигналы обновляются один раз на закрытую свечу trigger timeframe.

Дефолтные таймфреймы:

```text
EMA_TRIGGER_TIMEFRAME=1m
EMA_PULLBACK_TIMEFRAME=4h
EMA_MACRO_TIMEFRAME=1d
```

Алгоритм обновления сигналов:

1. Загружаются закрытые BTC benchmark candles на trigger timeframe.
2. Определяется timestamp последней закрытой BTC-свечи.
3. Для каждого символа загружаются trigger candles, выровненные по BTC timestamp.
4. Дополнительно загружаются macro и pullback candles на их собственных таймфреймах.
5. Строятся EMA, RS, BTC-фильтр, score и служебные volatility fields.
6. Сигнал сохраняется в `signal_cache["symbols"][symbol]`.

Если BTC benchmark недоступен, история слишком короткая или символ не выровнен с BTC по последней закрытой trigger-свече, новые входы fail-closed: `benchmark_ok=false` или символ не попадает в signal cache.

## 5. EMA-периоды

EMA-периоды задаются в минутах, затем переводятся в количество свечей выбранного таймфрейма.

| Логическое имя | Env | Дефолт минут | Дефолтный timeframe | Фактический EMA |
|---|---:|---:|---|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES` | 36000 | `1d` | EMA25 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES` | 72000 | `1d` | EMA50 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES` | 1440 | `4h` | EMA6 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES` | 2880 | `4h` | EMA12 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES` | 50 | `1m` | EMA50 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES` | 100 | `1m` | EMA100 |

Pullback recovery задаётся отдельно:

```text
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES=2880
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES=1440
EMA_PULLBACK_RECOVERY_GAP=0.001
```

На дефолтном `4h` pullback timeframe это означает: за последние 12 свечей / 48 часов EMA fast должна была быть против направления входа, а обратный cross в сторону входа должен быть не старше 6 свечей / 24 часов. `EMA_PULLBACK_RECOVERY_GAP=0.001` требует, чтобы текущая fast EMA ушла за slow EMA минимум на 0.1%, а не просто коснулась её.

EMA считаются по close закрытых свечей. Если есть кэш предыдущего EMA и пришла ровно следующая свеча, EMA обновляется инкрементально. Иначе EMA пересчитывается по доступной истории.

Минимальная история:

- trigger: максимум из EMA50, EMA100, `RS60 + 1` и 31 свечи;
- macro: максимум из macro fast/slow;
- pullback: максимум из pullback fast/slow плюс recovery lookback;
- BTC benchmark: минимум `RS60 + 1` и 31 свеча.

## 6. RS и BTC-фильтр

RS считается как относительная лог-доходность монеты к BTC:

```text
rs30 = log_return(symbol_now, symbol_30m_ago) - log_return(btc_now, btc_30m_ago)
rs60 = log_return(symbol_now, symbol_60m_ago) - log_return(btc_now, btc_60m_ago)
btc_return_30m = log_return(btc_now, btc_30m_ago)
```

Дефолтные raw RS/BTC фильтры:

```text
EMA_USE_RS_CONFIRMATION=true
EMA_LONG_MIN_RS60=0.0
EMA_SHORT_MAX_RS60=0.0
EMA_USE_BTC_RISK_FILTER=true
EMA_BTC_LONG_MIN_RETURN_30M=-0.0025
EMA_BTC_SHORT_MAX_RETURN_30M=0.0025
```

Смысл:

- long raw signal разрешён, если монета не слабее BTC на 60-минутном горизонте и BTC не падает быстрее допустимого порога;
- short raw signal разрешён, если монета не сильнее BTC на 60-минутном горизонте и BTC не растёт быстрее допустимого порога.

Это первый, мягкий RS/BTC-фильтр. Для нового входа поверх него применяется более строгий quality gate.

## 7. Raw EMA entry

Long raw entry:

```text
macro_valid    = EMA25D > EMA50D
pullback_valid = EMA1D was <= EMA2D within last 48h
                 and EMA1D crossed above EMA2D within last 24h
                 and EMA1D >= EMA2D * 1.001
trigger_valid  = EMA50 > EMA100
rs_confirm     = rs60 >= EMA_LONG_MIN_RS60
btc_filter     = btc_return_30m >= EMA_BTC_LONG_MIN_RETURN_30M

entry_valid = macro_valid
           and pullback_valid
           and trigger_valid
           and rs_confirm
           and btc_filter
```

Short raw entry:

```text
macro_valid    = EMA25D < EMA50D
pullback_valid = EMA1D was >= EMA2D within last 48h
                 and EMA1D crossed below EMA2D within last 24h
                 and EMA1D <= EMA2D * 0.999
trigger_valid  = EMA50 < EMA100
rs_confirm     = rs60 <= EMA_SHORT_MAX_RS60
btc_filter     = btc_return_30m <= EMA_BTC_SHORT_MAX_RETURN_30M

entry_valid = macro_valid
           and pullback_valid
           and trigger_valid
           and rs_confirm
           and btc_filter
```

`signal["valid"]` равен `macro_valid`. Для нового входа одного `signal.valid` недостаточно: raw entry требует одновременно `signal.valid`, `entry_valid` и `benchmark_ok`.

Для усреднений используется более мягкое условие:

```text
add_valid = macro_valid and (trigger_valid or pullback_valid)
```

Если macro-сигнал сломан, новые входы и усреднения запрещены.

## 8. Score

Сигнал сохраняет подробный контекст:

- `ema_macro_fast`, `ema_macro_slow`, aliases `ema25d`, `ema50d`;
- `ema_pullback_fast`, `ema_pullback_slow`, aliases `ema1d`, `ema2d`;
- `ema_trigger_fast`, `ema_trigger_slow`, aliases `ema50`, `ema100`;
- `rs30`, `rs60`, `btc_return_30m`;
- flags `macro_valid`, `pullback_valid`, `trigger_valid`, `rs_confirm_valid`, `btc_entry_valid`, `entry_valid`, `add_valid`;
- `score`, `rs_edge`, `trend_ema_gap`, `ema_gap`, `local_reversion`;
- volatility fields, currently neutral for active default sizing.

Score:

```text
long_score  = macro_gap + trigger_gap + pullback_depth + max(0, rs60)
short_score = macro_gap + trigger_gap + pullback_depth + max(0, -rs60)
```

Где:

- `macro_gap` отражает расстояние между macro EMA в сторону тренда;
- `trigger_gap` отражает расстояние между trigger EMA в сторону входа;
- `pullback_depth` отражает текущий recovery-gap между pullback EMA в сторону входа;
- `rs_edge` добавляет положительную относительную силу для long или слабость к BTC для short.

Score используется не только для логирования. Он является частью fresh-entry quality gate и ключом ранжирования в top-N.

### 8.1 Gold/BTC RSI macro overlay

Если `ENABLE_GOLD_BTC_RSI_OVERLAY=true`, бот отдельно считает RSI по XAUT/gold proxy и BTC на `GOLD_TIMEFRAME=4h`. Этот overlay не добавляет XAUT в торговый universe, а только меняет риск-контекст:

- `deleveraging`: BTC и gold слабые, новые входы могут быть запрещены через `PANIC_DISABLE_NEW_ENTRIES`, усреднения и recovery запрещаются, ladder становится шире;
- `crypto_underperforms_gold`: gold сильный, BTC слабый или сильно отстаёт от gold, long budget уменьшается до `RISK_OFF_LONG_BUDGET_MULTIPLIER=0.55`, short budget остаётся мягче через `RISK_OFF_SHORT_BUDGET_MULTIPLIER=0.85`, усреднения/recovery могут блокироваться;
- `crypto_risk_on` и `broad_liquidity_risk_on`: long остаётся без штрафа, short budget снижается до `0.75` или `0.85`;
- stale или недоступный macro-context логируется и используется как neutral fallback.

Macro overlay применяется к `budget_multiplier`, `ladder_multiplier`, fresh-entry block, averaging block и ускорению breakeven/time-exit через `RISK_OFF_TIME_EXIT_MULTIPLIER`.

### 8.2 External MEXC price radar

Если `EXTERNAL_PRICE_FEED_ENABLED=true`, HTX book сравнивается с MEXC book ticker:

- stale reference по умолчанию игнорируется для входа (`EXTERNAL_PRICE_IGNORE_REFERENCE_IF_STALE=true`), но может стать fail-closed через `EXTERNAL_PRICE_DISABLE_TRADING_IF_REFERENCE_STALE=true`;
- long не открывается, если HTX premium выше `EXTERNAL_PRICE_MAX_HTX_PREMIUM_FOR_LONG_BPS=15`;
- short не открывается, если HTX discount ниже `-EXTERNAL_PRICE_MAX_HTX_DISCOUNT_FOR_SHORT_BPS=15`;
- резкое расхождение HTX/MEXC за 1 минуту больше `EXTERNAL_PRICE_BLOCK_IF_DIVERGENCE_1M_BPS=50` включает cooldown на `EXTERNAL_PRICE_BLOCK_DURATION_SEC=300`;
- если MEXC ведёт HTX на 30 сек минимум на `EXTERNAL_PRICE_MEXC_LEAD_THRESHOLD_BPS_30S=5`, к fresh-entry score добавляется `EXTERNAL_PRICE_IMPULSE_SCORE_BONUS=0.02`;
- благоприятный HTX premium/discount может включить `external_tightened` exit ladder.

## 9. Fresh-entry quality gate

Для нового initial entry `_is_entry_signal_valid` требует:

1. raw entry valid:

```text
signal exists
signal.valid == true
signal.entry_valid == true
benchmark_ok == true
```

2. минимальный score:

```text
ENTRY_MIN_SCORE=0.03
raw_score + external_impulse_bonus >= ENTRY_MIN_SCORE
```

3. направленная относительная сила `rs60`:

```text
ENTRY_MIN_RS60_ABS=0.002

long:  rs60 >=  0.002
short: rs60 <= -0.002
```

4. направленная относительная сила `rs30`:

```text
ENTRY_MIN_RS30_ABS=0.001

long:  rs30 >=  0.001
short: rs30 <= -0.001
```

Эти quality-пороги применяются только к новым initial entries. Усреднения продолжают использовать `add_valid`, drawdown, interval и risk caps.

## 10. Entry gate: top-N, rate-limit, crowded mode

Перед проходом по символам бот строит `entry_gate` для текущей закрытой trigger-свечи.

### 10.1 Raw candidates

В raw candidate set попадают символы, которые:

- входят в `entry_symbols`;
- имеют raw entry valid;
- не frozen и не zombie;
- не находятся в cooldown;
- не зарезервированы другим профилем, если flat;
- уже имеют entry order или позицию с текущим signal timestamp, чтобы они продолжали занимать слот текущей свечи.

### 10.2 Crowded mode

Crowded mode включается, если выполнено хотя бы одно условие:

```text
ENTRY_CROWDED_MIN_SIGNALS=12
raw_candidates >= 12
```

или:

```text
ENTRY_CROWDED_SIGNAL_FRACTION=0.30
raw_candidates / entry_universe >= 0.30
```

В crowded mode fresh-entry quality gate становится строже:

```text
ENTRY_CROWDED_MIN_SCORE=0.04
ENTRY_CROWDED_MIN_RS60_ABS=0.003
ENTRY_CROWDED_MIN_RS30_ABS=0.0015
```

То есть:

- long: `score >= 0.04`, `rs60 >= 0.003`, `rs30 >= 0.0015`;
- short: `score >= 0.04`, `rs60 <= -0.003`, `rs30 <= -0.0015`.

### 10.3 Ranking

После quality gate кандидаты сортируются:

```text
1. score desc
2. directional rs60 desc
3. directional rs30 desc
4. trend_ema_gap desc
5. ema_gap desc
6. symbol asc
```

Для short directional RS равен `-rs`, чтобы более слабые к BTC монеты ранжировались выше.

### 10.4 Top-N

Обычный режим:

```text
ENTRY_MAX_NEW_LADDERS_PER_SIGNAL=5
```

Crowded mode:

```text
ENTRY_CROWDED_MAX_NEW_LADDERS_PER_SIGNAL=3
```

Если лимит `<= 0`, top-N ограничение считается выключенным.

### 10.5 Rate-limit

Скорость набора ограничена:

```text
ENTRY_RATE_LIMIT_LADDERS=10
ENTRY_RATE_LIMIT_WINDOW_MINUTES=60
```

В лимит считаются:

- активные позиции, открытые в пределах окна;
- активные entry orders, созданные в пределах окна;
- недавно закрытые циклы из cycle stats, если `opened_at` попадает в окно.

Если `ENTRY_RATE_LIMIT_LADDERS <= 0`, rate-limit выключен.

### 10.6 Gate result

Новый initial entry разрешён только если символ попал в `allowed_symbols` текущего `entry_gate`.

Символ может быть заблокирован с reason:

- `entry_quality_blocked`;
- `entry_score_below_min`;
- `entry_rs60_below_min`;
- `entry_rs30_below_min`;
- `entry_top_n_blocked`;
- `entry_rate_limited`;
- `entry_gate_not_ranked`.

На новой signal-свече бот логирует `entry_gate_updated` с количеством raw/quality/allowed кандидатов, crowded flag, top-N limit и rate-limit counters.

## 11. Новый вход

Новый initial entry разрешён, если:

- символ входит в `entry_symbols`;
- нет открытой позиции;
- нет активных entry orders;
- символ не frozen и не zombie;
- не активен cooldown после предыдущего закрытия;
- fresh-entry quality gate прошёл;
- symbol разрешён текущим top-N/rate-limit/crowded `entry_gate`;
- символ не зарезервирован другим профилем;
- profile health gate прошёл;
- external price gate не вернул premium/discount/divergence block;
- risk budget даёт положительный размер.

Health gate:

```text
MAX_UNHEALTHY_POSITIONS_FOR_NEW_ENTRIES=2
```

Unhealthy позиция: активная позиция без tracked exit ladder или zombie. Если число unhealthy positions в профиле `>= threshold`, новые циклы не открываются.

Reference price:

- long/buy: `bid`, затем `last`, затем `ask`;
- short/sell: `ask`, затем `last`, затем `bid`.

После отмены или постановки initial entry ladder бот не пересоздаёт новый ladder на той же самой signal timestamp.

## 12. Risk budget

Для нового входа базовый margin budget:

```text
base_margin_budget = equity * EMA_POSITION_BUDGET_FRACTION
EMA_POSITION_BUDGET_FRACTION=0.02
```

Дальше budget ограничивается:

- свободной маржей после резерва `min_quote_reserve=15 USDT`;
- лимитом активных слотов `max_active_positions=50`;
- общим notional cap:

```text
total_cap_notional = equity * LEVERAGE * EMA_MAX_TOTAL_MARGIN_FRACTION
EMA_MAX_TOTAL_MARGIN_FRACTION=0.50
```

- notional cap на символ:

```text
position_cap_notional = equity * LEVERAGE * EMA_MAX_POSITION_MARGIN_FRACTION
EMA_MAX_POSITION_MARGIN_FRACTION=0.03
```

- уже открытым notional по всем позициям и активным entry orders;
- доступной маржей после резерва;
- exchange minimum amount/min notional;
- `budget_multiplier` и `volatility_budget_multiplier`, которые в активной дефолтной стратегии равны `1.0`.

Если рассчитанный размер ниже минимума биржи и может быть поднят до минимума без нарушения caps, бот поднимает размер до минимального допустимого. Если размер всё равно недопустим, вход пропускается.

Для усреднения используется отдельный budget:

```text
desired_margin = current_position_margin * EMA_AVERAGING_POSITION_FRACTION
```

Он также ограничивается free margin, total cap, per-symbol cap и exchange minimums.

## 13. Entry ladder

Initial entry и EMA averaging используют один и тот же entry ladder:

```text
EMA_ENTRY_LADDER_FRACTIONS=0.50,0.50
EMA_ENTRY_LADDER_OFFSETS=0.0,0.01
```

Long:

- stage 1: около reference price;
- stage 2: на 1% ниже reference price.

Short:

- stage 1: около reference price;
- stage 2: на 1% выше reference price.

Цены округляются в безопасную сторону, чтобы не пересечь книгу:

- buy округляется не выше raw price;
- sell округляется не ниже raw price.

Для live-ордера:

- order type: `limit`;
- side: `ENTRY_SIDE`;
- `postOnly=POST_ONLY_ENABLED`;
- `leverRate` равен ручному HTX leverage;
- `marginMode=cross`;
- `hedged=false`.

Если HTX возвращает price-band ошибку, бот пытается переставить цену внутрь разрешённого диапазона. Если ордер всё равно отклонён или размер ниже минимума, stage пропускается.

Если не удалось поставить ни один stage:

- для flat symbol state сбрасывается, но last ladder signal timestamp сохраняется;
- для открытой позиции состояние возвращается к предыдущему active state.

В `DRY_RUN=true` реальные ордера не отправляются, но state получает dry-run order refs и логируется preview будущего exit ladder.

## 14. Entry order management

Активные entry orders отменяются, если:

- символ удалён из entry universe;
- для initial ladder больше не проходит fresh-entry quality gate;
- для averaging ladder больше не проходит `add_valid`;
- истёк `ORDER_TIMEOUT_SEC=90`;
- символ зарезервирован другим профилем;
- на бирже обнаружены неизвестные entry-side orders перед новым входом;
- breakeven активирован.

Top-N/rate-limit/crowded gate применяется к постановке нового initial ladder. Уже поставленный tracked entry ladder не отменяется только из-за того, что позже другой символ оказался выше в ranking на той же свече.

## 15. Синхронизация позиции

Каждый `step_symbol` сначала синхронизирует локальный state с биржей:

- если обнаружена позиция противоположной стороны, символ отключается и bot orders отменяются;
- если противоположная позиция принадлежит другому combined-профилю, символ считается reserved, но не отключается;
- если позиция появилась без локального state, бот принимает её в сопровождение;
- если позиция увеличилась, бот записывает entry fill, пересчитывает среднюю цену и отменяет старый exit ladder для перестройки;
- если позиция уменьшилась, бот записывает exit fill и перестраивает exit ladder;
- если позиция закрылась, бот пишет cycle stats, включает cooldown на 10 минут и сбрасывает state.

Cost basis учитывает entry/exit notional и комиссии. Если биржа не отдаёт fee details, используются дефолтные fee rates:

```text
buy_fee_rate=0.0001
sell_fee_rate=0.0001
```

## 16. Нормальный выход

После появления позиции бот обеспечивает tracked reduce-only exit ladder. В дефолтной конфигурации включён adaptive ladder:

```text
EMA_ADAPTIVE_EXIT_ENABLED=true
EMA_EXIT_NORMAL_LADDER_FRACTIONS=0.35,0.25,0.25,0.15
EMA_EXIT_NORMAL_LADDER_MARKUPS=0.008,0.016,0.030,0.050
EMA_EXIT_MEDIUM_LADDER_FRACTIONS=0.45,0.30,0.15,0.10
EMA_EXIT_MEDIUM_LADDER_MARKUPS=0.004,0.010,0.020,0.035
EMA_EXIT_HEAVY_LADDER_FRACTIONS=0.60,0.25,0.15
EMA_EXIT_HEAVY_LADDER_MARKUPS=0.003,0.008,0.015
EMA_EXIT_RUNNER_ENABLED=true
```

Выбор ladder зависит от текущего notional к initial notional:

- `normal`: ratio `<= 1.30`;
- `medium`: ratio `<= 1.80`;
- `heavy`: ratio `> 1.80`.

В `normal` ladder последняя фракция `0.15` до 6 часов не ставится обычным лимитным TP, а резервируется как runner. Runner активируется после движения в прибыль на `EMA_EXIT_RUNNER_ACTIVATION_MARKUP=0.020` и закрывается reduce-only limit при trailing pullback `0.010`, take-profit `0.050` или сломе trigger EMA против позиции.

Если HTX имеет благоприятный premium для long или discount для short, external price radar может заменить normal ladder на `external_tightened`:

```text
EXTERNAL_PRICE_TIGHTENED_LADDER_FRACTIONS=0.40,0.30,0.20,0.10
EXTERNAL_PRICE_TIGHTENED_LADDER_MARKUPS=0.005,0.010,0.020,runner
```

Если `EMA_ADAPTIVE_EXIT_ENABLED=false`, используется legacy ladder:

```text
EMA_EXIT_LADDER_FRACTIONS=1.0
EMA_TAKE_PROFIT_MARKUP=0.01
```

Exit amount ограничивается фактически доступным для закрытия количеством `position_available`. Если closeable amount недоступен или биржа сообщает, что reduce-only amount превышает доступный объём, бот не ставит дублирующий выход и помечает exit ladder как pending closeable.

Если `REDUCE_ONLY_ENABLED=false`, выходной ордер не ставится.

## 17. Контроль exit orders

Перед постановкой выхода бот проверяет открытые exit-side orders:

- tracked exit order без reduce-only отменяется;
- unknown exit orders, превышающие размер позиции, отменяются;
- если сумма tracked + unknown exit orders превышает позицию, tracked bot ladder отменяется;
- unknown reduce-only exit orders с безопасной ценой могут быть приняты в сопровождение;
- unknown non-reduce-only exit order не отменяется автоматически, но блокирует постановку дублирующего ladder;
- скрытые HTX close orders могут быть приняты, если выглядят как close/reduce-only и соответствуют замороженному объёму;
- если tracked order временно не виден в open orders, state может сохранить ref вместо немедленной перестройки.

Это защищает от открытия встречной позиции и от выхода объёмом больше текущей позиции.

## 18. Усреднение

Усреднение включено:

```text
EMA_AVERAGING_ENABLED=true
EMA_AVERAGING_DRAWDOWN_STEP=0.01
EMA_AVERAGING_POSITION_FRACTION=0.50
EMA_AVERAGING_INTERVAL_HOURS=8
EMA_MAX_AVERAGING_STAGES=5
```

Усреднение разрешено, если:

- позиция уже открыта;
- есть tracked exit ladder;
- нет активных entry orders;
- позиция не frozen и не zombie;
- breakeven ещё не активирован;
- `add_valid=true`;
- macro-сигнал не сломан;
- drawdown позиции `>= 1%`;
- с прошлого усреднения прошло минимум 8 часов;
- текущий signal timestamp ещё не использовался для усреднения;
- не достигнут лимит пяти averaging stages;
- risk caps дают положительный budget.

Drawdown:

```text
long_drawdown  = max(0, (entry_price - reference_price) / entry_price)
short_drawdown = max(0, (reference_price - entry_price) / entry_price)
```

Размер усреднения:

```text
desired_margin = current_position_margin * EMA_AVERAGING_POSITION_FRACTION
```

То есть дефолтный добор равен 50% текущей margin позиции до применения caps. Ордер добора ставится той же двухступенчатой entry ladder.

После успешной постановки averaging entry ladder увеличивается `average_stage`. Если ladder не создан, stage откатывается.

Fresh-entry top-N, rate-limit и crowded mode не применяются к усреднениям.

## 19. Breakeven после 48 часов

Breakeven включён:

```text
EMA_BREAKEVEN_ENABLED=true
EMA_BREAKEVEN_AFTER_HOURS=48
EMA_BREAKEVEN_REPRICE_MINUTES=15
EMA_BREAKEVEN_FEE_BUFFER=0.0002
EMA_BREAKEVEN_EXIT_FRACTIONS=1.0
```

Если позиция открыта дольше 48 часов:

1. Активные entry orders отменяются.
2. Позиция переводится в `frozen_no_more_buys=true`.
3. `sell_ladder_mode` становится `breakeven`.
4. Старый TP отменяется.
5. Ставится один reduce-only limit на breakeven price.
6. `breakeven_activated_at` записывается в state.
7. Новые усреднения больше не разрешаются.

Breakeven fee floor:

```text
fee_floor = buy_fee_rate + sell_fee_rate + EMA_BREAKEVEN_FEE_BUFFER
          = 0.0001 + 0.0001 + 0.0002
          = 0.0004
```

Long breakeven:

```text
price = average_entry_price * 1.0004
side = sell
```

Short breakeven:

```text
price = average_entry_price * 0.9996
side = buy
```

Если breakeven ladder старше 15 минут, бот отменяет и пересоздаёт его. Breakeven не использует market orders и не выставляет stop orders.

## 20. Поведение при слабом или отсутствующем сигнале

Для открытой позиции `signal_valid` означает:

```text
signal exists and signal.valid and benchmark_ok
```

Так как `signal.valid == macro_valid`, открытая позиция считается сигнально допустимой, пока macro-тренд не сломан и BTC benchmark доступен.

Если по открытой позиции signal missing/invalid или `benchmark_ok=false`, бот:

- замораживает дальнейшие доборы через `frozen_no_more_buys=true`;
- продолжает сопровождать существующий exit ladder;
- после 48 часов всё равно переводит позицию в breakeven;
- не запускает frozen recovery, потому что эта ветка отключена.

Плохой сигнал не закрывает позицию рынком. Он запрещает дальнейшее наращивание риска.

## 21. Сервисное закрытие пыли

Это не основной выход стратегии, но активный защитный механизм:

```text
DUST_POSITION_NOTIONAL=10.0
DUST_CLOSE_ENABLED=true
```

Если позиция имеет notional `<= 10 USDT` и `DRY_RUN=false`, бот:

- отменяет tracked entry/exit orders;
- ставит reduce-only market order на закрытие доступного пылевого объёма;
- помечает позицию как frozen/zombie до синхронизации.

Если reduce-only выключен или объём ниже минимума биржи, закрытие пыли блокируется/пропускается с логом.

## 22. Отключённые ветки и legacy-поля

В коде сохранены поля и helper-функции старой системы, но дефолтная EMA Pullback Strategy их не использует в активном маршруте.

Отключено:

- `enable_signal_size_scaling=false`;
- `enable_entry_expansion=false`, `_is_entry_expansion_signal_valid` всегда возвращает `False`;
- `enable_frozen_recovery_averaging=false`, а `_maybe_place_frozen_recovery_buy` является no-op;
- `enable_controlled_loss_exit=false`, `_maybe_apply_controlled_loss_exit` не вызывается из текущего runner;
- `enable_absolute_force_exit=false`, `_maybe_apply_absolute_force_exit` не вызывается из текущего runner;
- `hard_time_exit_after_minutes=0`;
- urgent/hard/zombie time-exit параметры равны нулю;
- `enable_volatility_adjusted_ladders=false`;
- `enable_volatility_targeted_sizing=false`;
- `enable_volatility_recovery_stages=false`;
- `enable_dynamic_time_exit_markups=false`;
- `enable_dynamic_profit_floor=false`;
- `enable_btc_risk_multiplier=false`;
- `enable_funding_aware_exit=false`.

Даже если некоторые параметры вручную включить в `.env`, controlled-loss и absolute-force ветки не станут частью активного маршрута без изменения runner. Они покрыты helper-тестами, но текущая стратегия их не вызывает.

Legacy-поля `entry_min_rs_edge`, `entry_min_ema_gap`, `entry_min_trend_ema_gap`, `entry_min_pullback`, `add_min_score` и похожие параметры сейчас не являются active gates для EMA Pullback default route.

## 23. State

`TradeState` хранит:

- позицию: `position_size`, `position_available`, `position_frozen`, `entry_price`, `position_side`;
- активные ордера: `entry_orders`, `sell_ladder_orders`, `sell_ladder_mode`, `sell_ladder_signature`;
- risk flags: `frozen_no_more_buys`, `zombie_position`, `cooldown_until`;
- lifecycle: `cycle_opened_at`, `time_exit_activated_at`, `breakeven_activated_at`;
- fill accounting: bought/sold amount, notional, fees, realized/unrealized/net PnL;
- EMA context последнего сигнала: `last_ema25d`, `last_ema50d`, `last_ema1d`, `last_ema2d`, `last_ema50`, `last_ema100`;
- EMA context входа: `entry_ema25d`, `entry_ema50d`, `entry_ema1d`, `entry_ema2d`, `entry_ema50`, `entry_ema100`;
- RS/BTC context: `last_rs30`, `last_rs60`, `entry_rs30`, `entry_rs60`, `last_btc_return_30m`, `entry_btc_return_30m`;
- averaging: `average_stage`, `last_average_at`, `last_average_signal_timestamp`;
- strategy marker: `strategy_name="ema_pullback"`.

В `DRY_RUN=true` `_save_state` не пишет state на диск, но логика в памяти работает.

## 24. Логи и CSV

Основной trade CSV содержит:

```text
ts, level, event, symbol, side, order_id, price, amount, filled,
remaining, position_size, entry_price, notional, fee_quote,
fee_currency, fill_source, rs30, rs60, ema30, ema60, reason
```

Cycle stats CSV содержит:

```text
symbol, opened_at, closed_at, leverage, margin_mode, planned_budget,
total_entry_notional, total_exit_notional, average_entry_price,
average_exit_price, buy_fees, sell_fees, realized_pnl_quote,
realized_pnl_percent_on_notional, realized_pnl_percent_on_margin,
holding_minutes, max_buy_stage, frozen_no_more_buys, close_reason,
entry_rs30, entry_rs60, entry_ema30, entry_ema60, strategy_name,
entry_ema25d, entry_ema50d, entry_ema1d, entry_ema2d, entry_ema50,
entry_ema100, entry_btc_return_30m, max_averaging_stage,
breakeven_activated
```

Ключевые события:

- `futures_setup`;
- `signal_updated`;
- `ema_signal_valid`, `ema_signal_invalid`;
- `entry_gate_updated`;
- `entry_ladder_placed`, `entry_ladder_planned`, `entry_order_canceled`;
- `exit_ladder_placed`, `exit_ladder_planned`, `exit_ladder_rebuilt`;
- `ema_average_placed`, `ema_average_skipped`;
- `ema_breakeven_activated`, `ema_breakeven_repriced`;
- `position_synced`, `position_frozen`, `cycle_closed`;
- `dust_close_order_placed`, `dust_close_failed`;
- `reduce_only_violation_prevented`;
- `state_exchange_mismatch`;
- `unexpected_long_position`, `unexpected_short_position`.

`reason` у EMA-сигнала включает EMA values, RS, BTC return, pullback recovery details, flags `macro/pullback/trigger/entry/add` и `score`.

`reason` у `entry_gate_updated` показывает:

- signal timestamp;
- crowded flag;
- per-signal top-N limit;
- recent entry count;
- rate limit;
- remaining rate slots.

## 25. Главные параметры `.env`

```text
BOT_PROFILES=long,short
DRY_RUN=true
DRY_RUN_EQUITY=1000
LEVERAGE=30
POLL_INTERVAL_SEC=3
LOG_LEVEL=INFO

EMA_STRATEGY_ENABLED=true
EMA_MACRO_TIMEFRAME=1d
EMA_PULLBACK_TIMEFRAME=4h
EMA_TRIGGER_TIMEFRAME=1m
EMA_MACRO_FAST_MINUTES=36000
EMA_MACRO_SLOW_MINUTES=72000
EMA_PULLBACK_FAST_MINUTES=1440
EMA_PULLBACK_SLOW_MINUTES=2880
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES=2880
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES=1440
EMA_PULLBACK_RECOVERY_GAP=0.001
EMA_TRIGGER_FAST_MINUTES=50
EMA_TRIGGER_SLOW_MINUTES=100

EMA_USE_RS_CONFIRMATION=true
EMA_LONG_MIN_RS60=0.0
EMA_SHORT_MAX_RS60=0.0
EMA_USE_BTC_RISK_FILTER=true
EMA_BTC_LONG_MIN_RETURN_30M=-0.0025
EMA_BTC_SHORT_MAX_RETURN_30M=0.0025

ENTRY_MIN_SCORE=0.03
ENTRY_MIN_RS60_ABS=0.002
ENTRY_MIN_RS30_ABS=0.001
ENTRY_MAX_NEW_LADDERS_PER_SIGNAL=5
ENTRY_RATE_LIMIT_LADDERS=10
ENTRY_RATE_LIMIT_WINDOW_MINUTES=60
ENTRY_CROWDED_SIGNAL_FRACTION=0.30
ENTRY_CROWDED_MIN_SIGNALS=12
ENTRY_CROWDED_MAX_NEW_LADDERS_PER_SIGNAL=3
ENTRY_CROWDED_MIN_SCORE=0.04
ENTRY_CROWDED_MIN_RS60_ABS=0.003
ENTRY_CROWDED_MIN_RS30_ABS=0.0015

EMA_POSITION_BUDGET_FRACTION=0.02
EMA_MAX_POSITION_MARGIN_FRACTION=0.03
EMA_MAX_TOTAL_MARGIN_FRACTION=0.50
EMA_ENTRY_LADDER_FRACTIONS=0.50,0.50
EMA_ENTRY_LADDER_OFFSETS=0.0,0.01

EMA_TAKE_PROFIT_MARKUP=0.01
EMA_EXIT_LADDER_FRACTIONS=1.0
EMA_AVERAGING_ENABLED=true
EMA_AVERAGING_DRAWDOWN_STEP=0.01
EMA_AVERAGING_POSITION_FRACTION=0.50
EMA_AVERAGING_INTERVAL_HOURS=8
EMA_MAX_AVERAGING_STAGES=5

EMA_BREAKEVEN_ENABLED=true
EMA_BREAKEVEN_AFTER_HOURS=48
EMA_BREAKEVEN_REPRICE_MINUTES=15
EMA_BREAKEVEN_FEE_BUFFER=0.0002
EMA_BREAKEVEN_EXIT_FRACTIONS=1.0

MAX_UNHEALTHY_POSITIONS_FOR_NEW_ENTRIES=2
ORDER_TIMEOUT_SEC=90
POST_ONLY_ENABLED=true
REDUCE_ONLY_ENABLED=true
DUST_POSITION_NOTIONAL=1.0
DUST_CLOSE_ENABLED=true
```

Профильные переопределения поддерживаются префиксами `LONG_` и `SHORT_`, например:

```text
LONG_ENTRY_MIN_SCORE=0.035
SHORT_ENTRY_MIN_SCORE=0.04
```

## 26. Проверка

Документ сверялся с текущими файлами:

- `config.py`;
- `htxbot/signal_engine.py`;
- `htxbot/strategy.py`;
- `htxbot/runner.py`;
- `htxbot/state.py`;
- `htxbot/exchange.py`;
- `htxbot/combined.py`;
- `htxbot/app.py`;
- `tests/test_unified_bot.py`.

Актуальная проверка после добавления entry gate:

```text
python -m pytest -q
43 passed
```

Синтаксическая проверка:

```text
python -m py_compile config.py htxbot\signal_engine.py htxbot\strategy.py htxbot\runner.py htxbot\combined.py tests\test_unified_bot.py
```
