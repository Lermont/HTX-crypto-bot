# ТЗ: Macro-overlay по RSI XAULT/USDT для HTX futures bot

## 1. Цель

Добавить в бота внешний macro-фильтр на основе поведения токенизированного золота **XAULT/USDT** относительно **BTC/USDT**.

Фильтр должен использовать XAULT как индикатор risk-on / risk-off режима:

- если золото сильнее BTC — снижать риск long-позиций по альтам;
- если BTC сильнее золота — разрешать обычный risk-on режим;
- если оба актива слабые — блокировать новые входы;
- если оба актива сильные — работать в neutral/risk-on, но без автоматического увеличения плеча.

XAULT используется только для аналитики и не должен торговаться ботом как обычная монета.

---

## 2. Важное ограничение

**XAULT не должен участвовать в торговой вселенной.**

Не добавлять XAULT в `LONG_COINS`, `SHORT_COINS` или `COINS`, если эти списки используются для открытия entry orders.

Нужно добавить отдельный список macro-инструментов:

```python
MACRO_GOLD_COINS = ("xault",)
```

Если на бирже фактический тикер называется иначе, сделать настройку через env:

```python
MACRO_GOLD_COINS = _env_csv("MACRO_GOLD_COINS", ("xault",), profile=name)
```

Тикер должен быть настраиваемым, потому что в API/HTX может использоваться `XAULT`, `XAUT` или другой market id.

---

## 3. Нужно ли добавлять XAULT/BTC

На первом этапе прямая пара **XAULT/BTC не обязательна**.

Достаточно использовать пары:

```text
XAULT/USDT
BTC/USDT
```

Отношение XAULT/BTC можно считать синтетически:

```text
synthetic_XAULT_BTC = XAULT_USDT_close / BTC_USDT_close
```

Прямая пара `XAULT/BTC` может быть добавлена в API опционально, но бот должен работать и без неё.

Логика:

```text
1. Если доступна прямая пара XAULT/BTC и включено USE_DIRECT_GOLD_BTC_PAIR=true:
   использовать её для ratio momentum / RSI spread.

2. Если прямой пары нет:
   считать synthetic_XAULT_BTC = XAULT_USDT_close / BTC_USDT_close.

3. Отсутствие XAULT/BTC не должно ломать macro-overlay.
```

---

## 4. Новые параметры конфигурации

Добавить в `config.py` отдельный dataclass, например `MacroSettings`.

```python
@dataclass(frozen=True)
class MacroSettings:
    enable_gold_btc_rsi_overlay: bool
    gold_coins: Tuple[str, ...]
    gold_timeframe: str
    gold_rsi_period: int
    gold_min_candles: int
    gold_cache_ttl_sec: int
    use_direct_gold_btc_pair: bool
    direct_gold_btc_symbol: str

    gold_strong_rsi: float
    gold_weak_rsi: float
    btc_strong_rsi: float
    btc_weak_rsi: float
    rsi_spread_threshold: float

    risk_off_long_budget_multiplier: float
    risk_off_short_budget_multiplier: float
    risk_off_ladder_multiplier: float
    risk_off_disable_averaging: bool
    risk_off_disable_recovery: bool
    risk_off_time_exit_multiplier: float

    panic_disable_new_entries: bool
    stale_macro_max_age_sec: int
```

Рекомендуемые значения по умолчанию:

```python
enable_gold_btc_rsi_overlay = True
gold_coins = ("xault",)
gold_timeframe = "4h"
gold_rsi_period = 14
gold_min_candles = 80
gold_cache_ttl_sec = 900
use_direct_gold_btc_pair = False
direct_gold_btc_symbol = ""

gold_strong_rsi = 60.0
gold_weak_rsi = 40.0
btc_strong_rsi = 60.0
btc_weak_rsi = 40.0
rsi_spread_threshold = 15.0

risk_off_long_budget_multiplier = 0.55
risk_off_short_budget_multiplier = 0.85
risk_off_ladder_multiplier = 1.25
risk_off_disable_averaging = True
risk_off_disable_recovery = True
risk_off_time_exit_multiplier = 0.75

panic_disable_new_entries = True
stale_macro_max_age_sec = 3600
```

---

## 5. Поиск macro-symbols

Добавить методы:

```python
_find_macro_gold_symbol()
_find_direct_gold_btc_symbol()
```

Требования:

- использовать существующий `exchange.load_markets()`;
- искать `XAULT/USDT:USDT`, `XAULT/USDT`, `XAUT/USDT:USDT`, `XAUT/USDT` — в зависимости от настроек;
- если futures-пара недоступна, разрешить использовать spot OHLCV, если `ccxt.htx` отдаёт свечи;
- если gold-symbol не найден, macro-overlay должен отключиться и записать warning в лог;
- отсутствие XAULT не должно останавливать торгового бота.

---

## 6. Расчёт RSI

Добавить функцию в `indicators.py`:

```python
def calculate_rsi(closes: Sequence[float], period: int) -> float:
    ...
```

Требования:

- не использовать pandas;
- можно использовать numpy, если он уже подключён;
- если свечей недостаточно — вернуть `0.0`;
- RSI считать по закрытым свечам;
- использовать Wilder smoothing либо простой средний gain/loss;
- главное требование — стабильный и воспроизводимый расчёт.

Минимальная интерпретация:

```text
RSI > 60 = сильный momentum
RSI < 40 = слабый momentum
40–60 = нейтральная зона
```

---

## 7. Расчёт gold/BTC context

Добавить метод:

```python
_gold_btc_rsi_context(self) -> dict
```

Он должен вернуть структуру:

```python
{
    "ok": True,
    "ts": 1770000000,
    "gold_symbol": "XAULT/USDT:USDT",
    "btc_symbol": "BTC/USDT:USDT",
    "timeframe": "4h",
    "gold_rsi": 63.2,
    "btc_rsi": 42.7,
    "rsi_spread": -20.5,
    "gold_btc_ratio_return": 0.018,
    "regime": "crypto_underperforms_gold",
    "long_budget_multiplier": 0.55,
    "short_budget_multiplier": 0.85,
    "ladder_multiplier": 1.25,
    "disable_new_entries": False,
    "disable_averaging": True,
    "disable_recovery": True,
    "time_exit_multiplier": 0.75,
    "reason": "gold_strong_btc_weak"
}
```

Если данных нет:

```python
{
    "ok": False,
    "regime": "macro_unavailable",
    "reason": "gold_symbol_not_found"
}
```

---

## 8. Режимы macro-overlay

### 8.1. `crypto_underperforms_gold`

Условие:

```text
gold_rsi >= 60
btc_rsi <= 45
```

или:

```text
gold_rsi - btc_rsi >= 15
```

Действия:

```text
long_budget_multiplier *= 0.55
short_budget_multiplier *= 0.85
ladder_multiplier *= 1.25
disable_averaging = true
disable_recovery = true
time_exit_after_minutes *= 0.75
```

Смысл: золото сильное, BTC слабый — рынок уходит в защиту, alt-long лучше резать.

---

### 8.2. `crypto_risk_on`

Условие:

```text
btc_rsi >= 60
gold_rsi < 55
btc_rsi - gold_rsi >= 10
```

Действия:

```text
long_budget_multiplier *= 1.0
short_budget_multiplier *= 0.75
ladder_multiplier *= 1.0
disable_averaging = false
disable_recovery = false
```

Важно: не увеличивать long-budget выше текущего signal multiplier. Macro-overlay должен быть защитным слоем, а не кнопкой агрессивного увеличения риска.

---

### 8.3. `broad_liquidity_risk_on`

Условие:

```text
btc_rsi >= 60
gold_rsi >= 60
```

Действия:

```text
long_budget_multiplier *= 1.0
short_budget_multiplier *= 0.85
ladder_multiplier *= 1.0
disable_averaging = false
```

Смысл: оба актива сильные, можно работать штатно, но не повышать риск автоматически.

---

### 8.4. `deleveraging`

Условие:

```text
btc_rsi <= 40
gold_rsi <= 40
```

Действия:

```text
disable_new_entries = true
disable_averaging = true
disable_recovery = true
ladder_multiplier *= 1.4
time_exit_after_minutes *= 0.65
```

Смысл: падают и BTC, и золото — это не rotation, а общий сброс риска/ликвидности.

---

### 8.5. `neutral`

Все остальные случаи.

Действия:

```text
multipliers = 1.0
disable flags = false
```

---

## 9. Интеграция в signal cache

В `signal_cache` добавить ключ:

```python
"macro": {
    "gold_btc_rsi": {...}
}
```

Обновлять macro-context не на каждой 1m-свече, а по TTL:

```text
gold_cache_ttl_sec = 900
```

То есть максимум раз в 15 минут.

---

## 10. Интеграция в budget multiplier

Сейчас сигнал уже должен формировать или использовать:

```text
signal_budget_multiplier
btc_budget_multiplier
budget_multiplier
ladder_multiplier
```

Нужно добавить macro-множители после BTC-risk:

```python
budget_multiplier = (
    signal_budget_multiplier
    * btc_risk_budget_multiplier
    * macro_budget_multiplier
)

ladder_multiplier = (
    volatility_multiplier
    * btc_risk_ladder_multiplier
    * macro_ladder_multiplier
)
```

Для long-профиля использовать:

```python
macro_budget_multiplier = long_budget_multiplier
```

Для short-профиля использовать:

```python
macro_budget_multiplier = short_budget_multiplier
```

---

## 11. Блокировка новых входов

В `_maybe_place_initial_buy()` или аналогичном методе перед размещением entry ladder добавить проверку:

```python
if macro_context.get("disable_new_entries"):
    log event="macro_entry_blocked"
    return
```

Лог должен включать:

```text
symbol
profile
macro_regime
gold_rsi
btc_rsi
rsi_spread
reason
```

---

## 12. Блокировка averaging и recovery

Перед обычным averaging:

```python
if macro_context.get("disable_averaging"):
    log event="macro_averaging_blocked"
    return
```

Перед frozen recovery:

```python
if macro_context.get("disable_recovery"):
    log event="macro_recovery_blocked"
    return
```

Важно: при режиме `crypto_underperforms_gold` нельзя докупать просадку по альтам только потому, что локальный RS-сигнал выглядит нормально. В плохом macro-режиме красивый локальный сигнал часто является ловушкой.

---

## 13. Time-exit

Для time-exit добавить ускорение через multiplier:

```python
effective_time_exit_after_minutes = (
    config.STRATEGY.time_exit_after_minutes
    * macro_context.get("time_exit_multiplier", 1.0)
)
```

Аналогично для urgent-time-exit:

```python
effective_urgent_time_exit_after_minutes = max(
    15,
    config.STRATEGY.urgent_time_exit_after_minutes
    * macro_context.get("time_exit_multiplier", 1.0)
)
```

Минимальное значение нужно, чтобы бот не начал слишком агрессивно закрывать позиции из-за одного macro-сигнала.

---

## 14. Логи

Добавить события:

```text
macro_context_updated
macro_context_unavailable
macro_context_stale
macro_entry_blocked
macro_averaging_blocked
macro_recovery_blocked
macro_budget_scaled
```

Желательно не ломать существующий CSV торговых событий. Для macro добавить отдельный файл:

```text
bot_futures_macro.csv
```

Колонки:

```text
ts
profile
regime
gold_symbol
btc_symbol
gold_rsi
btc_rsi
rsi_spread
gold_btc_ratio_return
long_budget_multiplier
short_budget_multiplier
ladder_multiplier
disable_new_entries
disable_averaging
disable_recovery
reason
```

---

## 15. Защита от stale macro

Если macro-context старше `stale_macro_max_age_sec`, то:

```text
regime = neutral
macro multipliers = 1.0
disable flags = false
log warning macro_context_stale
```

Не использовать старый risk-off бесконечно. Одна плохая свеча XAULT не должна душить бота весь день.

---

## 16. Fallback-логика

Если XAULT/USDT недоступен:

```text
1. Логировать warning.
2. Не падать.
3. Работать как сейчас.
4. regime = macro_unavailable.
5. multiplier = 1.0.
```

Если BTC/USDT недоступен, текущая benchmark-логика уже должна блокировать сигналы. Дополнительно ничего ломать не нужно.

---

## 17. Тесты

Добавить unit-тесты.

### 17.1. `calculate_rsi()`

Проверить:

```text
растущий ряд даёт RSI > 50
падающий ряд даёт RSI < 50
flat ряд не падает с ошибкой
недостаточно свечей возвращает 0.0
```

### 17.2. `_gold_btc_rsi_context()`

Проверить:

```text
gold_rsi=65, btc_rsi=42 -> crypto_underperforms_gold
btc_rsi=65, gold_rsi=48 -> crypto_risk_on
gold_rsi=35, btc_rsi=35 -> deleveraging
gold_rsi=65, btc_rsi=65 -> broad_liquidity_risk_on
нет XAULT -> macro_unavailable
```

### 17.3. Entry block

Проверить:

```text
при disable_new_entries=True бот не ставит entry ladder
```

### 17.4. Averaging block

Проверить:

```text
при disable_averaging=True бот не делает average buy/sell
```

### 17.5. XAULT не торгуется

Проверить:

```text
если MACRO_GOLD_COINS=("xault",), XAULT не должен попасть в entry_symbols
по XAULT не должно создаваться entry/sell ladder orders
```

---

## 18. Приёмка

Изменение считается готовым, если:

```text
1. Бот стартует в live/dry-run без ошибок.
2. XAULT используется только для fetch_ohlcv.
3. XAULT отсутствует в торговой вселенной entry_symbols.
4. При gold_rsi >> btc_rsi long-budget уменьшается.
5. При deleveraging новые входы блокируются.
6. При недоступном XAULT бот продолжает работать как раньше.
7. В логах виден macro regime и причина.
8. Есть тесты на основные режимы.
```

---

## 19. Рекомендуемый порядок внедрения

```text
1. Добавить MacroSettings в config.py.
2. Добавить RSI в indicators.py.
3. Добавить поиск macro-symbol отдельно от торговых symbols.
4. Добавить _gold_btc_rsi_context().
5. Добавить macro_context в signal_cache.
6. Подмешать macro multipliers в budget_multiplier и ladder_multiplier.
7. Добавить блокировку initial entries / averaging / recovery.
8. Добавить macro CSV/log.
9. Добавить тесты.
```

---

## 20. Итоговая логика

```text
XAULT сильнее BTC по RSI -> crypto risk-off -> режем long, запрещаем доборы, быстрее выходим.
BTC сильнее XAULT по RSI -> crypto risk-on -> работаем штатно.
Оба слабые -> не открываем новое.
Оба сильные -> neutral/risk-on без повышения плеча.
```

Прямую пару XAULT/BTC на первом этапе добавлять не обязательно. Синтетического отношения `XAULT/USDT / BTC/USDT` достаточно. Прямую пару стоит подключать только если она реально есть, ликвидна и API стабильно отдаёт свечи.
