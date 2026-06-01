# HTX Futures EMA Pullback Bot

[English](../readme.md) | [简体中文](readme.zh-CN.md) | [Подробная стратегия](../strategy.md)

HTX Futures EMA Pullback Bot — Python-бот для автоматической торговли криптовалютными USDT-M фьючерсами на HTX. Он запускает long и short профили, сканирует настраиваемый список альткоинов, строит EMA pullback сигналы по закрытым свечам и сопровождает позиции через входные лимитные ордера, усреднение, reduce-only выходы и breakeven-режим.

Проект рассчитан на трейдеров и разработчиков, которым нужен crypto trading bot с простым стартом, гибкой настройкой через `.env` и уже проработанной EMA Pullback strategy из коробки. Бот является live-ordering приложением: перед запуском нужны HTX API credentials и проверка сценариев через тесты или mock/stub exchange.

> Торговля фьючерсами рискованна. Это программное обеспечение, а не финансовая рекомендация. Перед live-режимом проверьте код, протестируйте настройки и поймите логику каждого параметра.

## Почему Этот Бот

- **Быстрый первый запуск**: установить зависимости, скопировать `.env.example`, выполнить `python bot.py`.
- **Long и short профили**: можно запускать `long`, `short` или оба профиля вместе.
- **Готовая EMA Pullback стратегия**: macro trend, pullback recovery, trigger EMA, RS к BTC, BTC risk filter, score, top-N выбор и throttling при crowded market.
- **Гибкая настройка через `.env`**: периоды EMA, entry gates, risk budget, averaging, breakeven, внешние reference prices и macro overlay настраиваются без правки кода.
- **USDT-M futures workflow**: HTX linear swap markets, cross margin, one-way position mode, post-only входы и reduce-only выходы.
- **Биржевой слой на CCXT**: активная реализация нацелена на HTX, но CCXT упрощает перенос market data, precision и order workflow на другую поддерживаемую биржу после адаптации exchange-слоя.
- **Операционные CSV-логи**: события сделок, статистика циклов, macro context и диагностика внешних цен.
- **Проверенное ядро**: pytest-suite покрывает сигналы, entry gates, усреднение, breakeven, профили и safety-ветки.

## Обзор Стратегии

Активная стратегия по умолчанию — `ema_pullback`: бот торгует альткоины в направлении старшего EMA-тренда после отката на среднем таймфрейме и подтверждения локального восстановления.

Дефолтная EMA-карта:

| Слой | Параметр | Таймфрейм | Фактическая EMA |
|---|---:|---:|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES=36000` | `1d` | EMA25 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES=72000` | `1d` | EMA50 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES=1440` | `4h` | EMA6 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES=2880` | `4h` | EMA12 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES=50` | `1m` | EMA50 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES=100` | `1m` | EMA100 |

Long-вход требует:

```text
EMA25D > EMA50D
EMA1D восстановилась выше EMA2D после недавнего отката
EMA50 > EMA100
rs60 >= EMA_LONG_MIN_RS60
btc_return_30m >= EMA_BTC_LONG_MIN_RETURN_30M
```

Short-вход использует зеркальную логику:

```text
EMA25D < EMA50D
EMA1D восстановилась ниже EMA2D после недавнего отскока
EMA50 < EMA100
rs60 <= EMA_SHORT_MAX_RS60
btc_return_30m <= EMA_BTC_SHORT_MAX_RETURN_30M
```

Сигнал не сводится к одному пересечению. Он объединяет направление старшего тренда, возраст recovery-cross, recovery gap, локальный trigger, относительную силу к BTC, краткосрочный BTC-risk и score для ранжирования кандидатов. Полное техническое описание находится в [strategy.md](../strategy.md).

## Управление Риском И Позицией

В активном маршруте по умолчанию нет классического stop-loss. Риск ограничивается лимитами позиции, staged entries, ограничениями усреднения, валидацией сигнала, reduce-only выходами и breakeven/time-exit поведением.

Ключевые дефолты:

- `BOT_PROFILES=long,short`.
- Вход по умолчанию делится на два limit-ордера.
- Новые входы проходят quality gates: score, RS60, RS30, top-N, rate-limit и crowded-market rules.
- Усреднение ограничено числом стадий, интервалом, hard-floor drawdown, ATR/daily-volatility floors и pullback recovery.
- Breakeven активируется после заданного времени удержания, запрещает дальнейшие доборы и переставляет reduce-only выход.
- Combined mode не даёт long и short профилям открыть встречную экспозицию по одной монете.

Для live-торговли нужны HTX API credentials и известное плечо аккаунта. `LEVERAGE` используется ботом для sizing и notional-лимитов; он не меняет плечо на бирже автоматически при старте.

## Быстрый Старт

Требования:

- Локально проверен Python 3.14; рекомендуется Python 3.11+.
- HTX account для live-режима: [открыть HTX с invite code `6hc25223`](https://www.htx.com/invite/en-us/1f?invite_code=6hc25223).
- Опциональный MEXC account для анализа reference market: [открыть MEXC по referral link](https://promote.mexc.com/r/lxcLKaZgvh). Текущий MEXC radar использует публичные рыночные данные и не требует MEXC API keys.
- Доступ к USDT-M futures.

Установка и локальная проверка:

```powershell
python -m pip install -r requirements.txt
copy .env.example .env
python -m pytest -q
```

Запуск одного профиля:

```powershell
python bot.py --profiles long
python bot.py --profiles short
```

Явный запуск обоих профилей:

```powershell
python bot.py --profiles long,short
```

Запуск `python bot.py` подключается к HTX и может отправлять реальные заявки, поэтому сначала проверьте `.env`, состояние аккаунта, risk limits и результаты тестов.

## Конфигурация

Скопируйте `.env.example` в `.env` и переопределите только нужные значения. Для совместимости также поддерживаются `long/.env` и `short/.env`.

Минимальные live-связанные значения:

```dotenv
HTX_API_KEY=
HTX_API_SECRET=
BOT_PROFILES=long,short
```

Полезные profile overrides:

```dotenv
LONG_LEVERAGE=30
SHORT_LEVERAGE=30
```

Примеры настройки стратегии:

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

В боте есть macro overlay, который сравнивает proxy золота, обычно XAUT, с BTC через RSI и относительное движение gold/BTC. Это не отдельная стратегия входа, а слой определения рыночного режима: он может уменьшать риск, запрещать averaging или делать выходы более консервативными, когда крипта проигрывает защитному активу.

Дефолтные macro-настройки:

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

Overlay классифицирует режимы:

- `crypto_underperforms_gold`: золото сильное, а BTC слабый или заметно отстаёт. Long budget уменьшается, short-направление может оставаться мягче, averaging может быть отключен, time-exit ускоряется.
- `crypto_risk_on`: BTC сильный, золото отстаёт. Бот сохраняет штатное long-поведение и может снизить агрессивность short.
- `broad_liquidity_risk_on`: BTC и золото одновременно сильные. Это конструктивный режим, но он не повышает плечо автоматически.
- `deleveraging`: BTC и золото одновременно слабые. Новые входы могут блокироваться, averaging отключается, выход ускоряется.
- `neutral` или `macro_unavailable`: overlay не добавляет сильного directional-фильтра.

Macro context кэшируется, пишется в CSV и используется в решениях по new-entry block, averaging block, ladder multiplier и time-exit multiplier. Точная логика режимов описана в [strategy.md](../strategy.md).

## MEXC Reference Price Signals

External price radar сравнивает mid-price стакана HTX с публичным MEXC book-ticker. Его задача — видеть cross-exchange premium, discount, stale reference data и краткосрочный impulse до открытия или сопровождения позиции.

Основные MEXC-параметры по умолчанию:

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

На практике:

- Long-вход может быть заблокирован, если HTX слишком дорогой относительно MEXC.
- Short-вход может быть заблокирован, если HTX слишком дешёвый относительно MEXC.
- Большое 1-минутное расхождение HTX/MEXC может отправить символ в cooldown.
- Если MEXC двигается раньше HTX в ту же сторону, кандидат может получить impulse score bonus.
- Exit ladder может быть затянут, если premium/discount на HTX подсказывает более осторожный take-profit.
- Если reference data устарели, дефолтное поведение — игнорировать reference, а не отключать всю торговлю; это можно ужесточить через `.env`.

Radar мапит HTX symbols в MEXC spot-style `BASEUSDT` symbols и пишет bid, ask, mid, spread, z-score, age и short-window changes в external price CSV.

## Перенос На Другие Биржи

Торговая биржа создаётся через CCXT. Это хорошая база для переносимости: загрузка рынков, precision formatting, timeframes, tick sizes и стандартные order calls уже идут через общий exchange abstraction.

Текущий релиз всё равно HTX-специфичен. Live trading использует `ccxt.htx`, conventions HTX USDT-M futures, contract hostnames HTX, проверки one-way mode, чтение ручного leverage, reduce-only/order parameters и обработку HTX price-band. Перенос на другую CCXT-supported exchange должен быть заметно проще, чем переписывание бота с нуля, но это именно adapter task: нужно реализовать exchange factory, credentials, market filters, leverage/position-mode handling, order flags и тесты для выбранной биржи до live-использования.

## Мониторинг

Runtime CSV-файлы создаются локально и игнорируются Git:

- trade events;
- cycle statistics;
- macro overlay context;
- external reference-price diagnostics;
- markets cache и state-файлы.

Они помогают понять, почему сигнал был принят или отклонён, как рассчитан размер позиции, когда заблокировано усреднение и как переставлены выходы.

## Тесты

Запуск тестов:

```powershell
python -m pytest -q
```

Текущий локальный baseline:

```text
82 passed, 4 subtests passed
```

## Структура Проекта

```text
bot.py                 CLI entrypoint
config.py              dataclass-конфигурация и профили
htxbot/                Движок, биржа, сигналы, стратегия, state, monitoring
tests/                 Единый pytest-suite
.env.example           Безопасный шаблон конфигурации
strategy.md            Полное описание стратегии и поведения
docs/                  Переводы релизной документации
```

## Безопасность

- Не коммитьте `.env`, `long/.env` и `short/.env`.
- Ограничивайте права API-ключей и меняйте их при малейшем подозрении на утечку.
- Перед запуском прогоняйте тесты и mock/stub exchange сценарии.
- До осознанного live-запуска используйте минимальные разрешения API.

## Дисклеймер

Автоматическая торговля фьючерсами может быстро привести к убыткам, особенно при плече, гэпах ликвидности, сбоях биржи, ошибках конфигурации или изменении рыночного режима. Репозиторий предоставляет код и документацию. Ответственность за тестирование, deployment, настройки биржи, риск-лимиты и торговые решения несёте вы.

## Лицензия

MIT License. См. [LICENSE](../LICENSE).
