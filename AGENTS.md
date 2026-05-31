Контекст проекта:
- Это Python-бот для торговли USDT-M futures на HTX через ccxt.
- Есть long/short профили, combined-режим, shared exchange/cache, runtime lock, persisted state, CSV-логи сделок и cycle stats.
- Критичные зоны: config.py, bot.py, htxbot/app.py, htxbot/combined.py, htxbot/runner.py, htxbot/strategy.py, htxbot/signal_engine.py, htxbot/state.py, htxbot/exchange.py, htxbot/monitoring.py, models.py, requirements.txt.
- Особое внимание: state_exchange_mismatch, step_error, reduce-only exit orders, hidden/unknown close orders, frozen/zombie/time-exit состояния, long/short симметрия, восстановление после рестарта, корректность sell ladder и ограничение убытка.
- Бот может работать в live-режиме. Не совершай реальных торговых действий. Не запускай live-loop с настоящими API-ключами. Не печатай и не меняй секреты.

Режим работы:
1. Сначала составь краткий план диагностики и только потом вноси изменения.
2. Проверь весь проект, а не только один файл.
3. Исправляй root cause, а не маскируй симптомы.
4. Делай минимальные, но системные правки.
5. Сохраняй текущую архитектуру, если нет прямой необходимости менять её.
6. Не удаляй торговые инварианты ради “простого фикса”.

Что проверить обязательно:

1. Запуск и структура
- Найди фактическую точку входа.
- Проверь, как создаются long/short профили.
- Проверь combined-режим: shared exchange, reset private caches, reserved symbols, poll_interval, save_state.
- Проверь, что runtime lock не мешает тестам и корректно освобождается.

2. Конфигурация
- Проверь config.py и profile resolution.
- Убедись, что long/short профили получают свои env/config значения.
- Проверь, нет ли рассинхрона между config.POSITION_SIDE, ENTRY_SIDE, EXIT_SIDE, RISK.leverage, margin_mode.
- Не меняй API-ключи и не выводи их в отчёт.

3. State и миграции
- Проверь TradeState: все поля сериализуются/десериализуются корректно.
- Проверь загрузку старого state с отсутствующими/лишними полями.
- Проверь normalize order refs.
- Проверь пересчёт PnL, remaining_entry_quote, fees, net_open_pnl.
- Проверь сценарии:
  - позиция есть на бирже, но state пустой;
  - state есть, но позиции уже нет;
  - частичные fill;
  - frozen/zombie/time_exit после рестарта;
  - sell_ladder_signature заблокировал перестройку ladder.

4. Exchange и ордера
- Проверь price/amount precision для HTX futures.
- Проверь contractSize, min contracts, min notional.
- Проверь обработку price band ошибок HTX.
- Проверь reduce-only флаг.
- Проверь, что exit orders никогда не превышают position_size.
- Проверь, что бот не ставит дублирующую exit ladder поверх уже существующих close orders.
- Проверь adoption/cancel hidden/unknown close orders.
- Проверь, что на flat symbol tracked exit orders отменяются.

5. Strategy / Runner
- Проследи полный цикл step_symbol().
- Проверь порядок:
  - fetch position snapshot;
  - fetch open orders;
  - sync state;
  - validate exits;
  - validate entries;
  - manage entries;
  - ensure sell ladder;
  - averaging/recovery;
  - initial entry.
- Проверь, что позиция не блокируется навечно без возможности выхода.
- Проверь, что frozen recovery не открывает бесконтрольные доборы.
- Проверь, что time-based exit/urgent_time_exit действительно перестраивает ladder, а не зависает.
- Проверь, что long и short логика зеркальна там, где должна быть зеркальна.

6. Signals
- Проверь расчёт RS против BTC.
- BTC должен быть benchmark, но не обычным торговым символом.
- Проверь closed candle alignment.
- Проверь valid/entry_valid/expanded_entry_valid/add_valid.
- Проверь, что signal_cache не устаревает и не приводит к ложным блокировкам входов.

7. Monitoring / CSV
- Проверь запись CSV, headers, rotation.
- Проверь, что ошибки логируются с достаточным reason.
- Найди в bot_futures_trades.csv повторяющиеся ERROR/state_exchange_mismatch/step_error и установи их вероятную причину.
- Если error message сейчас теряется, исправь логирование так, чтобы в CSV попадал полезный exception/reason.

8. Тесты
Добавь или обнови тесты. Минимум:
- compile/import всех модулей;
- загрузка state со старыми/неполными полями;
- сериализация TradeState;
- price/amount precision через mock exchange;
- sell ladder не превышает position_size;
- reduce-only exit order validation;
- unknown/hidden close orders: adopt/cancel/wait сценарии;
- flat symbol с exit orders;
- combined long/short reserved symbols;
- short/long direction invariants;
- regression test на причину найденных step_error/state_exchange_mismatch.

Не требуй реального HTX API для unit-тестов. Используй mock/stub exchange.

9. Проверки после правок
Выполни:
- python -m compileall .
- pytest, если тесты есть или ты их добавил
- статическую проверку импортов
- сухую проверку ключевых сценариев через mock/stub exchange, без live-торговли

Если pytest не настроен — добавь минимальную конфигурацию и тесты, но не тащи тяжёлые зависимости без необходимости.

Ограничения безопасности:
- Не запускай бесконечный live-loop.
- Не отправляй реальные ордера.
- Не меняй .env с API-ключами.
- Не коммить secrets.
- Если нужно проверить runtime, используй mock/stub exchange.
- Любые потенциально опасные действия сначала замени безопасной имитацией.

Формат результата:
1. Кратко опиши найденные проблемы.
2. Укажи, какие файлы изменены.
3. Для каждой правки объясни:
   - причина;
   - что было сломано;
   - как исправлено;
   - какой тест это покрывает.
4. Укажи команды, которые были выполнены, и результат.
5. Отдельно перечисли оставшиеся риски или места, которые требуют проверки на live-аккаунте.
6. Не ограничивайся косметикой. Главная цель — чтобы бот устойчиво работал после рестарта, не зависал в state mismatch, не создавал опасные exit orders и не ломал long/short combined-режим.
