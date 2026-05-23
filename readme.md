Ниже ТЗ одним сообщением, в формате для LLM/агента. Стоп-лосс исключён. Управление риском — через лимиты позиции, усреднение, breakeven/time-exit и запрет бесконечного добора.

---

# ТЗ: внедрение EMA Pullback Strategy с усреднением и breakeven для HTX Futures Bot

## 1. Цель

Внедрить новую торговую стратегию для существующего HTX USDT-M futures bot.

Стратегия должна работать в двух профилях:

* `long`: открытие long-позиций, закрытие reduce-only sell-ордерами;
* `short`: открытие short-позиций, закрытие reduce-only buy-ордерами.

Текущая архитектура уже поддерживает long/short-профили, one-way mode, cross margin и combined-режим с резервированием символов между профилями, чтобы разные профили не открывали встречную экспозицию по одной монете. Это необходимо сохранить.

## 2. Основная идея стратегии

Стратегия торгует откат внутри старшего тренда.

### Long-логика

Открывать long, если:

```text
EMA 25D > EMA 50D
EMA 1D < EMA 2D
EMA 50 > EMA 100
```

Смысл:

* старший тренд вверх;
* среднесрочно есть откат;
* локально цена начинает восстанавливаться.

### Short-логика

Открывать short, если условия зеркальные:

```text
EMA 25D < EMA 50D
EMA 1D > EMA 2D
EMA 50 < EMA 100
```

Смысл:

* старший тренд вниз;
* среднесрочно есть отскок;
* локально цена снова разворачивается вниз.

## 3. Важное уточнение по EMA 2D

В исходной формулировке указано:

```text
EMA 2D = EMA2280M
```

Это математически не 2 дня.
2 дня на минутном таймфрейме = `2880M`.

Требование:

```text
EMA_2D_PERIOD_MINUTES = 2880
```

Если нужно оставить именно 2280 минут, назвать параметр не `EMA_2D`, а, например:

```text
EMA_PULLBACK_SLOW_PERIOD = 2280
```

По умолчанию использовать корректное значение `2880`.

## 4. Источник данных и таймфреймы

Текущий бот строит сигналы по закрытым `1m` свечам, синхронизирует свечи символа с BTC benchmark, считает RS, EMA, краткосрочное движение, локальный откат, волатильность и BTC-risk context. Это поведение можно частично переиспользовать.

Для новой стратегии добавить отдельный режим расчёта EMA:

### Базовый вариант для внедрения

Считать короткие EMA по закрытым `1m` свечам, а большие периоды переводить на старшие таймфреймы, если биржа/API не отдаёт готовые EMA:

```text
EMA_50 = 50
EMA_100 = 100
EMA_1D = 1440 минут -> EMA6 на 4h
EMA_2D = 2880 минут -> EMA12 на 4h
EMA_25D = 36000 минут -> EMA25 на 1d
EMA_50D = 72000 минут -> EMA50 на 1d
```

### Практическая оптимизация

Так как `EMA_50D = 72000` минутных свечей на каждый символ — тяжёлый расчёт, реализовать кэширование EMA и не пересчитывать полную историю на каждом цикле.

Использовать режим:

```text
EMA_MACRO_TIMEFRAME = 1d
EMA_PULLBACK_TIMEFRAME = 4h
EMA_TRIGGER_TIMEFRAME = 1m
```

Периоды в конфиге остаются в минутах, код переводит их в число свечей выбранного таймфрейма и считает EMA по OHLCV closes.

## 5. Новые параметры конфигурации

Добавить в `config.py` отдельный блок стратегии, например:

```text
EMA_STRATEGY_ENABLED = true

EMA_MACRO_TIMEFRAME = 1d
EMA_PULLBACK_TIMEFRAME = 4h
EMA_TRIGGER_TIMEFRAME = 1m

EMA_MACRO_FAST_MINUTES = 36000
EMA_MACRO_SLOW_MINUTES = 72000

EMA_PULLBACK_FAST_MINUTES = 1440
EMA_PULLBACK_SLOW_MINUTES = 2880
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES = 2880
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES = 1440
EMA_PULLBACK_RECOVERY_GAP = 0.001

EMA_TRIGGER_FAST_MINUTES = 50
EMA_TRIGGER_SLOW_MINUTES = 100

EMA_USE_RS_CONFIRMATION = true
EMA_LONG_MIN_RS60 = 0.0
EMA_SHORT_MAX_RS60 = 0.0

EMA_USE_BTC_RISK_FILTER = true
EMA_BTC_LONG_MIN_RETURN_30M = -0.0025
EMA_BTC_SHORT_MAX_RETURN_30M = 0.0025
```

Текущая конфигурация уже содержит параметры для averaging, time-exit, volatility ladders, BTC-risk и funding-aware exit. Новые параметры должны аккуратно встроиться в существующий `StrategySettings`, не ломая старые поля.

## 6. Расчёт сигнала

Добавить новый метод в `signal_engine.py`, например:

```text
_build_ema_pullback_signal_from_closes(...)
```

Метод должен возвращать словарь сигнала с полями:

```text
ema_macro_fast
ema_macro_slow
ema_pullback_fast
ema_pullback_slow
ema_trigger_fast
ema_trigger_slow

macro_valid
pullback_valid
trigger_valid
rs_confirm_valid
btc_entry_valid

entry_valid
add_valid
score
reason
```

Текущий `signal_engine.py` уже возвращает подробные поля сигнала, включая `entry_valid`, `add_valid`, `score`, `trend_valid`, `recent_valid`, `btc_entry_valid`, volatility и multiplier. Новую стратегию нужно встроить в тот же контракт, чтобы остальной код сопровождения позиции продолжал работать без полного переписывания.

## 7. Long-сигнал

Для long:

```text
macro_valid =
    ema_macro_fast > ema_macro_slow

pullback_valid =
    ema_pullback_fast was <= ema_pullback_slow within last 48h
    and ema_pullback_fast crossed above ema_pullback_slow within last 24h
    and ema_pullback_fast >= ema_pullback_slow * 1.001

trigger_valid =
    ema_trigger_fast > ema_trigger_slow

entry_valid =
    macro_valid
    and pullback_valid
    and trigger_valid
    and btc_entry_valid
    and rs_confirm_valid
```

Дополнительное подтверждение через RS к BTC:

```text
rs_confirm_valid =
    rs60 >= EMA_LONG_MIN_RS60
```

По умолчанию:

```text
EMA_LONG_MIN_RS60 = 0.0
```

То есть long разрешается только если монета не слабее BTC на 60-минутном горизонте. Текущая стратегия уже использует RS30/RS60 как доходность монеты минус доходность BTC, поэтому этот блок лучше сохранить — он защищает от покупки слабых альтов в общем рыночном росте.

## 8. Short-сигнал

Для short:

```text
macro_valid =
    ema_macro_fast < ema_macro_slow

pullback_valid =
    ema_pullback_fast was >= ema_pullback_slow within last 48h
    and ema_pullback_fast crossed below ema_pullback_slow within last 24h
    and ema_pullback_fast <= ema_pullback_slow * 0.999

trigger_valid =
    ema_trigger_fast < ema_trigger_slow

entry_valid =
    macro_valid
    and pullback_valid
    and trigger_valid
    and btc_entry_valid
    and rs_confirm_valid
```

RS-подтверждение:

```text
rs_confirm_valid =
    rs60 <= EMA_SHORT_MAX_RS60
```

По умолчанию:

```text
EMA_SHORT_MAX_RS60 = 0.0
```

То есть short разрешается только если монета не сильнее BTC.

## 9. BTC-risk фильтр

Для long:

```text
btc_entry_valid =
    btc_return_30m >= EMA_BTC_LONG_MIN_RETURN_30M
```

По умолчанию:

```text
EMA_BTC_LONG_MIN_RETURN_30M = -0.0025
```

Для short:

```text
btc_entry_valid =
    btc_return_30m <= EMA_BTC_SHORT_MAX_RETURN_30M
```

По умолчанию:

```text
EMA_BTC_SHORT_MAX_RETURN_30M = 0.0025
```

Смысл: не открывать long, когда BTC резко падает; не открывать short, когда BTC резко растёт.

## 10. Score сигнала

Добавить простой score для логирования и масштабирования:

### Long

```text
macro_gap = (ema_macro_fast - ema_macro_slow) / price
trigger_gap = (ema_trigger_fast - ema_trigger_slow) / price
pullback_depth = (ema_pullback_fast - ema_pullback_slow) / price

score = macro_gap + trigger_gap + pullback_depth + max(0, rs60)
```

### Short

```text
macro_gap = (ema_macro_slow - ema_macro_fast) / price
trigger_gap = (ema_trigger_slow - ema_trigger_fast) / price
pullback_depth = (ema_pullback_slow - ema_pullback_fast) / price

score = macro_gap + trigger_gap + pullback_depth + max(0, -rs60)
```

Score не должен быть жёстким условием входа на первом этапе. Использовать его для логирования и возможного будущего scaling.

## 11. Входная лестница

При появлении `entry_valid=true` и отсутствии открытой позиции/активных входных ордеров:

### Long

Поставить 2 входных limit-ордера:

```text
1-й ордер: 1% депо по цене около рынка
2-й ордер: 1% депо по цене market - 1%
```

### Short

Поставить 2 входных limit-ордера:

```text
1-й ордер: 1% депо по цене около рынка
2-й ордер: 1% депо по цене market + 1%
```

Добавить параметры:

```text
EMA_ENTRY_LADDER_FRACTIONS = 0.50, 0.50
EMA_ENTRY_LADDER_OFFSETS_LONG = 0.0, 0.01
EMA_ENTRY_LADDER_OFFSETS_SHORT = 0.0, 0.01
EMA_POSITION_BUDGET_FRACTION = 0.02
```

Трактовка:

```text
EMA_POSITION_BUDGET_FRACTION = 0.02
```

означает суммарно 2% депо на начальную позицию: 1% + 1%.

## 12. Поведение входных ордеров

Требования:

1. Входные ордера должны быть limit.
2. Для цены около рынка использовать безопасное округление к tick size.
3. Если включён `post_only`, учитывать риск отказа ордера.
4. Если ордер не исполнился за `ORDER_TIMEOUT_SEC`, отменить и пересоздать только при сохранении `entry_valid=true`.
5. Если сигнал пропал до исполнения, отменить входные ордера.
6. Не открывать новую позицию по символу, если другой профиль уже имеет позицию, входные ордера или выходную лестницу по этому символу.

Последний пункт важен для combined-режима, где символы резервируются между профилями.

## 13. Выходная логика после входа

После появления позиции бот должен выставить reduce-only выходную лестницу.

### Базовый take-profit

Для long:

```text
exit_price = average_entry_price * 1.01
```

Для short:

```text
exit_price = average_entry_price * 0.99
```

Добавить параметры:

```text
EMA_TAKE_PROFIT_MARKUP = 0.01
EMA_EXIT_LADDER_FRACTIONS = 1.0
```

На первом этапе выход всей позиции одним reduce-only ордером. Можно оставить совместимость с текущей sell ladder, но markups сделать одним уровнем `0.01`.

Текущий бот уже поддерживает reduce-only выходную лестницу, где для long выходная сторона `sell`, для short — `buy`, а цена выхода строится от average entry price и ограничивается breakeven/profit floor. Это надо переиспользовать.

## 14. Stop-loss не внедрять

Явно не добавлять стоп-лосс.

Запрещено:

```text
не создавать stop-market
не создавать stop-limit
не закрывать позицию по фиксированному -1%
не использовать controlled-loss как замену стопу в базовом режиме
```

Параметры controlled-loss, если они есть в текущей версии, должны быть выключены для этой стратегии:

```text
ENABLE_CONTROLLED_LOSS_EXIT = false
```

Риск контролируется только через:

```text
лимит размера позиции
лимит количества усреднений
лимит общей экспозиции
breakeven/time-exit
запрет новых доборов при сломе сигнала
```

## 15. Усреднение

Усреднение включить.

### Условие для long

Если позиция long находится в минусе на 1% или больше от средней цены входа:

```text
drawdown = (current_price - average_entry_price) / average_entry_price

if drawdown <= -0.01:
    можно усреднять
```

### Условие для short

Если позиция short находится в минусе на 1% или больше:

```text
drawdown = (average_entry_price - current_price) / average_entry_price

if drawdown <= -0.01:
    можно усреднять
```

Или проще считать direction-aware PnL:

```text
position_pnl_pct <= -0.01
```

## 16. Размер усреднения

Каждое усреднение:

```text
average_order_margin = current_position_margin * 0.50
```

То есть добор на 50% от текущего размера позиции.

Добавить параметры:

```text
EMA_AVERAGING_ENABLED = true
EMA_AVERAGING_DRAWDOWN_STEP = 0.01
EMA_AVERAGING_POSITION_FRACTION = 0.50
EMA_AVERAGING_INTERVAL_HOURS = 8
EMA_MAX_AVERAGING_STAGES = 2
```

Рекомендованный лимит:

```text
EMA_MAX_AVERAGING_STAGES = 2
```

Без лимита стратегия превращается в мартингейл. Формально стопа нет, но риск не должен становиться бесконечным. Бесконечное усреднение — это стоп-лосс, просто с драматургией.

## 17. Частота усреднения

Усреднять не чаще одного раза в 8 часов по каждому символу.

Проверять поля state:

```text
last_average_at
buy_stage
last_average_signal_timestamp
```

Если с последнего усреднения прошло меньше 8 часов — не усреднять.

Если позиция уже достигла максимального количества усреднений — не усреднять.

## 18. Условия запрета усреднения

Не усреднять, если:

```text
позиция уже в breakeven/time-exit режиме
превышен EMA_MAX_AVERAGING_STAGES
превышен max_position_notional_fraction
превышен max_total_notional_fraction
недостаточно free margin после min_quote_reserve
символ зарезервирован другим профилем
биржа вернула ошибку по минимальному размеру ордера
```

Также не усреднять, если полностью сломан macro-сигнал:

### Long

```text
EMA25D <= EMA50D
```

### Short

```text
EMA25D >= EMA50D
```

То есть откат можно усреднять, но разворот старшего тренда — нет.

## 19. Усреднение и сигнал

Для первого внедрения использовать мягкое правило:

```text
для усреднения не требуется полный entry_valid
но требуется macro_valid=true
```

Дополнительно желательно:

```text
trigger_valid=true или pullback_valid=true
```

Но не требовать весь entry-сигнал, иначе усреднение почти никогда не сработает в момент просадки.

## 20. Пересчёт average entry

После каждого исполнения входного или усредняющего ордера:

1. Обновить `position_size`.
2. Обновить `entry_price` как фактическую среднюю цену позиции.
3. Обновить `last_buy_price`.
4. Обновить `last_buy_amount`.
5. Увеличить `buy_stage`.
6. Пересчитать выходную reduce-only лестницу от новой средней цены.

Текущий state уже хранит `position_size`, `entry_price`, `last_buy_price`, `buy_stage`, `planned_quote_budget`, `last_average_at`, `entry_orders`, `sell_ladder_orders` и связанные поля, поэтому структуру можно расширять минимально.

## 21. Breakeven-режим

Breakeven включить.

Если позиция живёт дольше 48 часов:

```text
holding_time >= 12 hours
```

бот должен заменить обычный take-profit на breakeven-выход.

### Long breakeven price

```text
breakeven_price = average_entry_price * (1 + fee_floor)
```

### Short breakeven price

```text
breakeven_price = average_entry_price * (1 - fee_floor)
```

Где:

```text
fee_floor = buy_fee_rate + sell_fee_rate + safety_buffer
```

Рекомендация:

```text
safety_buffer = 0.0002
```

Если в текущей логике уже есть profit floor с учётом комиссий, spread и volatility floor — использовать его, но не требовать +1% после 48 часов.

## 22. Breakeven-параметры

Добавить:

```text
EMA_BREAKEVEN_ENABLED = true
EMA_BREAKEVEN_AFTER_HOURS = 48
EMA_BREAKEVEN_REPRICE_MINUTES = 15
EMA_BREAKEVEN_FEE_BUFFER = 0.0002
EMA_BREAKEVEN_EXIT_FRACTIONS = 1.0
```

После активации breakeven:

```text
state.sell_ladder_mode = "breakeven"
state.time_exit_activated_at = now
state.frozen_no_more_buys = true
```

Важно: после перехода в breakeven больше не усреднять эту позицию.

## 23. Поведение breakeven-ордера

После 48 часов:

1. Отменить старую take-profit лестницу.
2. Выставить reduce-only ордер на breakeven.
3. Если цена изменилась и ордер не исполняется, перепрайсить раз в `EMA_BREAKEVEN_REPRICE_MINUTES`.
4. Не ставить цену хуже breakeven, если controlled-loss выключен.
5. Не использовать market close.
6. Не создавать stop-ордера.

Для long:

```text
breakeven_order_side = sell
price >= average_entry_price + комиссии
```

Для short:

```text
breakeven_order_side = buy
price <= average_entry_price - комиссии
```

## 24. Сосуществование усреднения и breakeven

Правило приоритета:

```text
Первые 48 часов:
    разрешены TP + усреднение

После 48 часов:
    усреднение запрещено
    включается breakeven-выход
```

То есть бот не должен одновременно:

```text
ставить breakeven-выход
и продолжать увеличивать позицию
```

Это критично. Иначе позиция будет «выходить без убытка», но параллельно расти. Такой фокус обычно заканчивается не магией, а margin usage.

## 25. Выходная лестница после усреднения

После каждого усреднения:

1. Отменить старые reduce-only exit orders.
2. Пересчитать average entry.
3. Создать новую TP-лестницу:

Для long:

```text
average_entry_price * 1.01
```

Для short:

```text
average_entry_price * 0.99
```

Если позиция уже старше 48 часов, вместо TP сразу ставить breakeven.

## 26. Работа с frozen/recovery

Текущая версия уже имеет frozen recovery averaging для проблемных позиций: максимум recovery-добавлений, подтверждающие свечи, add-valid сигнал и drawdown-trigger.

Для новой стратегии упростить:

```text
frozen recovery averaging отключить или подчинить общей EMA_AVERAGING логике
```

Рекомендуемый вариант:

```text
ENABLE_FROZEN_RECOVERY_AVERAGING = false
```

На первом тесте не нужно две разные системы добора. Одна система усреднения проще для анализа.

## 27. Ограничения риска

Добавить или проверить следующие лимиты:

```text
EMA_MAX_ACTIVE_POSITIONS = текущий MAX_ACTIVE_POSITIONS
EMA_MAX_POSITION_MARGIN_FRACTION = 0.03
EMA_MAX_TOTAL_MARGIN_FRACTION = 0.50
EMA_MAX_AVERAGING_STAGES = 2
EMA_MIN_QUOTE_RESERVE = текущий MIN_QUOTE_RESERVE
```

Если используются notional-лимиты с плечом, оставить текущую risk-budget механику: она уже считает budget от equity/free margin, учитывает leverage, max position notional, max total notional и min contract size.

## 28. Логирование

Добавить в CSV/log reason следующие события:

```text
ema_signal_valid
ema_signal_invalid

ema_entry_ladder_placed
ema_entry_ladder_canceled

ema_average_placed
ema_average_skipped

ema_take_profit_placed
ema_take_profit_repriced

ema_breakeven_activated
ema_breakeven_placed
ema_breakeven_repriced

ema_macro_broken_no_average
ema_max_averaging_stages_reached
```

В reason писать:

```text
ema25d
ema50d
ema1d
ema2d
ema50
ema100
rs30
rs60
btc_return_30m
macro_valid
pullback_valid
trigger_valid
entry_valid
buy_stage
position_pnl_pct
holding_minutes
```

Текущая логика уже пишет подробные причины валидности/невалидности сигнала, включая score, rs, ema_gap, trend, recent, local_reversion и BTC context. Новый EMA-сигнал должен логироваться так же подробно.

## 29. CSV статистика циклов

В `bot_futures_cycle_stats.csv` добавить поля или переиспользовать существующие:

```text
strategy_name
entry_ema25d
entry_ema50d
entry_ema1d
entry_ema2d
entry_ema50
entry_ema100
entry_rs30
entry_rs60
entry_btc_return_30m
max_averaging_stage
breakeven_activated
close_reason
holding_minutes
```

Существующий файл cycle stats уже хранит opening/closing timestamps, leverage, margin mode, planned budget, entry/exit notional, fees, realized PnL, holding time, max buy stage и close reason. Это надо сохранить и расширить.

## 30. State migration

При старте после обновления:

1. Старые state-поля не удалять.
2. Для существующих позиций:

   * продолжить сопровождение;
   * не открывать новые входы по старой логике;
   * если позиция старше 48 часов — перевести в breakeven/time-exit;
   * не усреднять старую позицию, если нет EMA macro-valid.
3. Добавить новые поля с дефолтами:

```text
entry_ema25d = 0.0
entry_ema50d = 0.0
entry_ema1d = 0.0
entry_ema2d = 0.0
entry_ema50 = 0.0
entry_ema100 = 0.0
last_ema_strategy_signal_timestamp = None
breakeven_activated_at = None
```

## 31. Старую стратегию не сохранять как активный режим

Старая RS-pullback стратегия не нужна как переключаемый режим. Активный маршрут должен быть EMA Pullback only; старые механизмы могут оставаться только как неиспользуемый legacy-код или тестовые helper-методы, если они не вызываются из основного торгового цикла.

## 32. Что изменить по файлам

### `config.py`

Добавить:

```text
EMA_STRATEGY_ENABLED
EMA_* параметры таймфреймов
EMA_* параметры периодов
EMA_ENTRY_* параметры
EMA_AVERAGING_* параметры
EMA_BREAKEVEN_* параметры
EMA_TAKE_PROFIT_MARKUP
```

Расширить `StrategySettings`.

### `signal_engine.py`

Добавить расчёт EMA Pullback Strategy.

Методы:

```text
_build_ema_pullback_signal_from_closes
_is_ema_entry_signal_valid
_is_ema_add_signal_valid
```

В `_update_signal_cache_if_needed()` строить EMA Pullback-сигнал и не открывать новые входы по старой логике.

### `strategy.py`

Изменить/расширить:

```text
_maybe_place_initial_buy
_maybe_place_average_buy
_ensure_sell_ladder
_maybe_apply_time_based_exit
```

Логика должна учитывать:

```text
EMA take-profit
EMA averaging
EMA breakeven
no stop-loss
```

### `models.py`

Добавить новые поля state для EMA strategy, если нужно хранить значения входа.

### `monitoring.py`

Расширить CSV header при необходимости.

### `strategy.md`

Обновить описание текущей стратегии после внедрения.

## 33. Тесты

Добавить тесты.

### Сигнал long

Проверить:

```text
EMA25D > EMA50D
EMA1D had been <= EMA2D within 48h
EMA1D crossed above EMA2D within 24h
EMA1D >= EMA2D * 1.001
EMA50 > EMA100
rs60 >= 0
btc_return_30m >= threshold
=> entry_valid = true
```

### Сигнал short

Проверить:

```text
EMA25D < EMA50D
EMA1D had been >= EMA2D within 48h
EMA1D crossed below EMA2D within 24h
EMA1D <= EMA2D * 0.999
EMA50 < EMA100
rs60 <= 0
btc_return_30m <= threshold
=> entry_valid = true
```

### Запрет входа

Проверить:

```text
macro_valid=false
=> entry_valid=false
```

### Усреднение

Проверить:

```text
position_pnl_pct <= -1%
last_average_at older than 8h
buy_stage < max
macro_valid=true
=> average order placed
```

### Запрет усреднения

Проверить:

```text
position_pnl_pct <= -1%
but holding_time >= 48h and breakeven active
=> average order not placed
```

### Breakeven

Проверить:

```text
holding_time >= 48h
=> old TP canceled
=> reduce-only breakeven order placed
=> frozen_no_more_buys=true
```

### Stop-loss absence

Проверить:

```text
no stop-market orders
no stop-limit orders
no forced close at -1%
```

## 34. Acceptance criteria

Считать задачу выполненной, если:

1. Бот запускается с EMA Pullback как единственным активным торговым режимом.
2. Long и short профили используют зеркальную EMA-логику.
3. BTC benchmark не торгуется как обычный символ.
4. Входные ордера ставятся 2 уровнями: около рынка и на откате 1%.
5. TP выставляется на ±1% от средней цены.
6. При просадке −1% бот усредняет на 50% текущей позиции.
7. Усреднение не чаще одного раза в 8 часов.
8. Максимум усреднений ограничен.
9. После 48 часов позиция переводится в breakeven.
10. После breakeven новые усреднения запрещены.
11. Стоп-лосс не создаётся и не эмулируется.
12. Все выходные ордера reduce-only.
13. Логи содержат причины входа, отказа, усреднения и breakeven.
14. Старая стратегия не участвует в активном торговом маршруте.

## 35. Рекомендуемые стартовые параметры

```text
EMA_MACRO_TIMEFRAME = 1d
EMA_PULLBACK_TIMEFRAME = 4h
EMA_TRIGGER_TIMEFRAME = 1m

EMA_MACRO_FAST_MINUTES = 36000
EMA_MACRO_SLOW_MINUTES = 72000

EMA_PULLBACK_FAST_MINUTES = 1440
EMA_PULLBACK_SLOW_MINUTES = 2880
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES = 2880
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES = 1440
EMA_PULLBACK_RECOVERY_GAP = 0.001

EMA_TRIGGER_FAST_MINUTES = 50
EMA_TRIGGER_SLOW_MINUTES = 100

EMA_POSITION_BUDGET_FRACTION = 0.02
EMA_ENTRY_LADDER_FRACTIONS = 0.50, 0.50
EMA_ENTRY_LADDER_OFFSETS = 0.0, 0.01

EMA_TAKE_PROFIT_MARKUP = 0.01

EMA_AVERAGING_ENABLED = true
EMA_AVERAGING_DRAWDOWN_STEP = 0.01
EMA_AVERAGING_POSITION_FRACTION = 0.50
EMA_AVERAGING_INTERVAL_HOURS = 8
EMA_MAX_AVERAGING_STAGES = 2

EMA_BREAKEVEN_ENABLED = true
EMA_BREAKEVEN_AFTER_HOURS = 48
EMA_BREAKEVEN_REPRICE_MINUTES = 15
EMA_BREAKEVEN_FEE_BUFFER = 0.0002

ENABLE_CONTROLLED_LOSS_EXIT = false
ENABLE_FROZEN_RECOVERY_AVERAGING = false
```

## 36. Ключевой принцип реализации

Не надо городить ещё одну параллельную систему ордеров.

Нужно встроить новую стратегию в существующие механизмы:

```text
signal_cache
risk_budget
entry_ladder
average_buy
sell_ladder
time_exit/breakeven
state sync
reduce-only validation
CSV logging
```

То есть меняем мозг сигнала и правила сопровождения, но не ломаем скелет бота. Это дешевле, безопаснее и потом проще сравнить статистику с предыдущими версиями.
