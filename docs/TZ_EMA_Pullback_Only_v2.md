# ТЗ для LLM: параметры стратегии EMA Pullback Only — v2

> Ревизия исходного ТЗ «EMA Pullback Only» по итогам разбора статистики (40 long / 71 short
> закрытых сделок + 580 факторных снапшотов, окно 14–16.06.2026). Базовая логика стратегии
> не меняется: вход — три EMA-слоя (глобальный тренд, откат, начало восстановления), RS к BTC
> и macro RSI BTC/XAUt как фильтры, усреднения при живом сигнале, равносторонние TP/SL
> (триаж-барьеры) + time-exit как вертикальный барьер. Меняются параметры, перечисленные ниже.

## Чейнджлог v2 (что изменилось относительно исходного ТЗ и почему)

| # | Раздел | Было (исходное ТЗ) | Стало (v2) | Основание |
|---|--------|--------------------|------------|-----------|
| A | §1, §7 | факторный отбор top-K по сути управлял входом (live `FACTOR_ENTRY_ENABLED=true`, штрафы 0.009/0.003) | вход только по **жёстким EMA-гейтам**, `FACTOR_ENTRY_ENABLED=false`, штрафы macro/pullback/trigger = **999** | Fama-MacBeth (`analyze_factors.py`): composite top-3 даёт SR/сделку −0.14…−0.18, win <50%, per-period t от −2.0 до −3.2 на всех горизонтах и сторонах → факторный отбор значимо **анти-предиктивен** |
| B | §1 | — | измерение факторов и снапшоты **остаются включёнными**, но отвязаны от входа | сохраняем исследовательский харнесс для оффлайн-валидации факторов на нормальной выборке без влияния на live-вход |
| C | §12 | `HARD_STOP_LOSS_ENABLED=false`, опора только на soft-exit (1% + слом сигнала) | сохранить **широкий аварийный стоп 4%** как бэкстоп поверх soft-exit | реализованные убытки (`hard_stop_loss_market_close`, ср. −2.71) уезжали хуже −1%, т.к. сигнал формально «не ломался»; широкий стоп валидирован по винрейту в прошлом анализе и обрезает хвост |
| D | §4 | `EMA_LONG_MIN_RS60=0.0`, `ENTRY_MIN_RS60_ABS=0.0` (фильтр по знаку RS) | требовать **\|RS60\| ≥ 0.003** | RS60 в данных лежит в диапазоне ±0.005; порог 0 = фильтр по знаку шума, RS60 в FM незначим → ужесточаем, чтобы RS отсеивал, а не подбрасывал монету |
| E | §10 | `EMA_MAX_AVERAGING_STAGES=2` | сузить до **1 ступени** усреднения | stage-2 в выборке нетто-отрицателен (short −0.98 на 4 сделках; long −3.96 — это контаминировано аномалией сайзинга SUI), сигнал тонкий → консервативно ограничиваем одной до-докупкой, обратимо |
| F | §2 | таймфреймы 1h / 5m / 5m (исходное ТЗ) либо 4h / 1h / 1m (live) | **гибрид: macro 4h + pullback 1h + trigger 5m** | у live (B) медленный 4-8-дневный глобальный тренд и 6-12ч откат — это и есть эдж; единственное слабое звено B — шумный 1m-триггер → заменён на 5m-триггер от исходного ТЗ (A) с тем же ~1ч горизонтом, но без 1m-шума/комиссий |
| G | §11, §12, §14 | равносторонние TP/SL = 1% + breakeven-24h | **триаж-барьеры: TP=SL=3% (симметрично) + вертикальный барьер 48ч** | медианный 1m-ATR ≈0.09% → 1% барьер ≈1.5-2ч дрейфа, т.е. **внутри шума** (меряет мин-реверс, а не альфу). 3% ≈ 1× суточный ATR — вне ≤4ч шума, достижим за 24-48ч реальным ходом; time-exit высвобождает капитал при отсутствии хода. Метки TP/SL/timeout становятся чистыми → `P(TP)−P(SL)` = прямая оценка альфы |
| — | §разное | — | прочие разделы — **без изменений** относительно исходного ТЗ | подтверждены данными или не пересматривались |

> **Решение по таймфреймам (F) принято:** гибрид B + триггер A — macro **4h** (EMA25/50,
> глобальный тренд 4д/8д), pullback **1h** (EMA6/12, откат 6ч/12ч), trigger **5m** (EMA12/24,
> тайминг ~1ч без 1m-шума). См. §2.
>
> **Фаза замера vs эксплуатации (G):** симметричные 3% барьеры + 48ч вертикальный барьер —
> это **фаза замера**: барьеры намеренно широкие и равносторонние, чтобы чисто отделить альфу
> от шума. Когда направление/размер альфы известны → **фаза эксплуатации**: возвращаем
> асимметрию (шире TP / уже SL, трейлинг, ранний срез по слому) и breakeven-24h.

---

## 1. Общие гейты

```
EMA_STRATEGY_ENABLED=true
BOT_PROFILES=long,short

# v2: вход — по EMA-гейтам, НЕ по факторному top-K отбору
FACTOR_ENTRY_ENABLED=false
# Измерение/снапшоты факторов остаются включёнными (отвязаны от входа) — для оффлайн-анализа
```

- Единый RS-pullback движок на оба профиля, общий BTC-benchmark.
- long: вход на стороне восходящего EMA-тренда монеты; short: на стороне нисходящего.
- Факторный скоринг продолжает писать снапшоты (`factor_snapshots.jsonl`), но **не отбирает
  и не гейтит** входы. Решение о входе принимают только EMA-гейты §2–§3 + фильтры §4–§6.

## 2. EMA-слои (структура сигнала)  *(ИЗМЕНЕНО — F: гибрид B + триггер A)*

```
EMA_MACRO_TIMEFRAME=4h          # B: глобальный тренд
EMA_PULLBACK_TIMEFRAME=1h       # B: крупный откат
EMA_TRIGGER_TIMEFRAME=5m        # A: тайминг без 1m-шума
EMA_MACRO_FAST_MINUTES=6000     # 100h  -> EMA25 на 4h
EMA_MACRO_SLOW_MINUTES=12000    # 200h  -> EMA50 на 4h
EMA_PULLBACK_FAST_MINUTES=360   # 6h    -> EMA6  на 1h
EMA_PULLBACK_SLOW_MINUTES=720   # 12h   -> EMA12 на 1h
EMA_TRIGGER_FAST_MINUTES=60     # 1h    -> EMA12 на 5m
EMA_TRIGGER_SLOW_MINUTES=120    # 2h    -> EMA24 на 5m
```

- Macro trend: EMA25/EMA50 на 4h (глобальный тренд 4д против 8д).
- Pullback layer: EMA6/EMA12 на 1h (откат 6ч против 12ч).
- Trigger layer: EMA12/EMA24 на 5m (тайминг ~1ч против 2ч, семплинг на 5m — без 1m-шума).

Условия long:
- `EMA25_4h > EMA50_4h`
- `EMA6_1h` был `<= EMA12_1h` в окне отката
- `EMA6_1h` снова выше `EMA12_1h`
- `EMA12_5m > EMA24_5m`

Условия short — зеркально.

> Обоснование выбора (F): медленный 4-8-дневный macro — «настоящий» глобальный тренд (быстрый
> 2-5-дневный переворачивается на нормальных качелях и даёт whipsaw macro-гейта); 6-12ч pullback
> ловит торгуемые откаты. Единственное слабое звено live-конфига — 1m-триггер (шум, комиссии,
> в Fama-MacBeth незначим) — заменён на 5m-триггер того же ~1ч горизонта.

## 3. Pullback recovery (жёсткий гейт)

```
EMA_ENTRY_REQUIRE_PULLBACK_RECOVERY=true
EMA_PULLBACK_RECOVERY_LOOKBACK_MINUTES=720
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES=120
EMA_PULLBACK_RECOVERY_GAP=0.001
```

- Откат должен был произойти за последние 12 часов.
- Обратный кросс — не старше 2 часов.
- Минимальный EMA-gap 0.1%.
- Это **hard gate**, а не quality penalty: нет восстановления отката → нет входа.

> Согласовать с §2: эти окна заданы в абсолютных минутах и были рассчитаны под 5m-pullback.
> Pullback теперь на 1h → `MAX_CROSS_AGE=120` мин = всего 2 часовых бара (жёстко). Если входов
> станет слишком мало, расширить `MAX_CROSS_AGE` до 180–360 и при необходимости `LOOKBACK`.

## 4. RS монеты к BTC  *(ИЗМЕНЕНО — D)*

```
EMA_USE_RS_CONFIRMATION=true
EMA_LONG_MIN_RS60=0.003       # было 0.0
EMA_SHORT_MAX_RS60=-0.003     # было 0.0
ENTRY_MIN_RS60_ABS=0.003      # было 0.0  — абсолютный порог |RS60|
ENTRY_MIN_RS30_ABS=0.0
```

- long допускается, только если монета сильнее BTC по RS60 **с запасом ≥0.003**;
- short — если слабее BTC по RS60 с тем же запасом;
- RS30 не гейтит, используется только для crowded-режима;
- RS остаётся **подтверждающим** фильтром, не входит в скоринг.

## 5. BTC 30m risk filter — выключен

```
EMA_USE_BTC_RISK_FILTER=false
EMA_BTC_LONG_MIN_RETURN_30M=-1.0
EMA_BTC_SHORT_MAX_RETURN_30M=1.0
```

BTC 30m return не гейтит вход; за макро-режим отвечает overlay BTC/XAUt (§6).

## 6. Macro RSI BTC/XAUt overlay

```
ENABLE_GOLD_BTC_RSI_OVERLAY=true
MACRO_GOLD_COINS=xaut
GOLD_TIMEFRAME=4h
GOLD_RSI_PERIOD=14
GOLD_MIN_CANDLES=80
GOLD_CACHE_TTL_SEC=900
GOLD_STRONG_RSI=60
GOLD_WEAK_RSI=40
BTC_STRONG_RSI=60
BTC_WEAK_RSI=40
RSI_SPREAD_THRESHOLD=15
PANIC_DISABLE_NEW_ENTRIES=true
RISK_OFF_LONG_BUDGET_MULTIPLIER=0.0
RISK_OFF_SHORT_BUDGET_MULTIPLIER=1.0
RISK_OFF_DISABLE_AVERAGING=true
RISK_OFF_TIME_EXIT_MULTIPLIER=0.75
ENABLE_GOLD_DIRECTIONAL_BIAS=false
```

- XAUt сильнее BTC → новые long отключаются;
- short не запрещаются, но усиливаются в risk-off;
- directional bias оставлен выключенным.

> Наблюдение к мониторингу (не зашито): в разобранном окне open-book был в минусе
> одновременно по long и short. Если двусторонняя просадка в чопе повторяется,
> рассмотреть `ENABLE_GOLD_DIRECTIONAL_BIAS=true` для более жёсткого выбора стороны.

## 7. Дополнительные фильтры и скоринг — жёсткие гейты  *(ИЗМЕНЕНО — A)*

```
EMA_CHOP_FILTER_ENABLED=false
EMA_VOLUME_CONFIRMATION_ENABLED=false
EMA_VOLUME_SPIKE_FILTER_ENABLED=false
EMA_VOLUME_PROFILE_FILTER_ENABLED=false

ENTRY_MIN_SCORE=0.0
ENTRY_MACRO_INVALID_PENALTY=999.0
ENTRY_PULLBACK_INVALID_PENALTY=999.0
ENTRY_TRIGGER_INVALID_PENALTY=999.0
ENTRY_BTC_INVALID_PENALTY=0.0
ENTRY_BTC_RETURN_PENALTY_MULTIPLIER=0.0
ENTRY_MARKET_STRUCTURE_INVALID_PENALTY=0.0
ENTRY_VOLUME_INVALID_PENALTY=0.0
ENTRY_CHOP_INVALID_PENALTY=0.0
ENTRY_RS60_SHORTFALL_PENALTY_MULTIPLIER=0.0
ENTRY_RS30_SHORTFALL_PENALTY_MULTIPLIER=0.0
ENTRY_QUALITY_BUDGET_MIN_MULTIPLIER=1.0
ENTRY_QUALITY_BUDGET_REFERENCE=1.0
```

- macro / pullback / trigger обязаны быть валидны (штраф 999 = hard gate);
- volume / chop / BTC30m / score не гейтят и не масштабируют бюджет;
- любой невалидный EMA-слой → входа нет.

## 8. Ограничение частоты входов

```
ENTRY_MAX_NEW_LADDERS_PER_SIGNAL=3
ENTRY_RATE_LIMIT_LADDERS=6
ENTRY_RATE_LIMIT_WINDOW_MINUTES=60
ENTRY_CROWDED_SIGNAL_FRACTION=1.0
ENTRY_CROWDED_MIN_SIGNALS=999
ENTRY_CROWDED_MAX_NEW_LADDERS_PER_SIGNAL=3
ENTRY_CROWDED_MIN_SCORE=0.0
ENTRY_CROWDED_MIN_RS60_ABS=0.0
ENTRY_CROWDED_MIN_RS30_ABS=0.0
```

## 9. Entry ladder

```
EMA_POSITION_BUDGET_FRACTION=0.02
EMA_ENTRY_LADDER_FRACTIONS=0.50,0.50
EMA_ENTRY_LADDER_OFFSETS=0.0,0.008
```

- 50% входа по рынку, 50% лимиткой на −0.8% (post-only).

## 10. Усреднение  *(ИЗМЕНЕНО — E)*

```
EMA_AVERAGING_ENABLED=true
EMA_MAX_AVERAGING_STAGES=1          # было 2
MAX_BUY_STAGES=2                     # согласовано с 1 ступенью усреднения (initial + 1 add)
EMA_AVERAGING_DRAWDOWN_STEP=0.01
EMA_AVERAGING_MIN_DRAWDOWN_STEP=0.01
EMA_AVERAGING_BASE_FRACTION=0.50
EMA_AVERAGING_POWER=1.0
EMA_AVERAGING_INTERVAL_HOURS=6.0
EMA_AVERAGING_REQUIRE_PULLBACK_RECOVERY=true
EMA_AVERAGING_ATR_ENABLED=false
EMA_AVERAGING_MIN_DAILY_VOLATILITY_FRACTION=0.0
AVERAGING_BUDGET_FRACTION_1=1.0     # единственная ступень
AVERAGING_DRAWDOWN_STEP_1=0.01
```

- одна докупка при просадке ≥1% от средней цены;
- докупка только если EMA macro + pullback recovery + trigger всё ещё валидны;
- интервал между докупками ≥6 часов;
- учитываются time-exit / breakeven, как и раньше.
- Вторая ступень (`AVERAGING_*_2`) отключена. Решение обратимо: вернуть
  `EMA_MAX_AVERAGING_STAGES=2`, если на более чистой выборке stage-2 покажет плюс.

> Согласовать с §11–§12: докупка триггерится при −1% просадки, но барьеры теперь ±3%, т.е.
> −1% — это ещё шум внутри барьера. Чтобы добавляться на осмысленном откате, а не на шуме,
> поднять `EMA_AVERAGING_DRAWDOWN_STEP`/`AVERAGING_DRAWDOWN_STEP_1` до ~0.015–0.02.

## 11. Take Profit — верхний триаж-барьер  *(ИЗМЕНЕНО — G)*

```
EMA_TAKE_PROFIT_MARKUP=0.03         # было 0.01 — верхний барьер ≈ 1× суточный ATR
EMA_EXIT_LADDER_FRACTIONS=1.0
EMA_ADAPTIVE_EXIT_ENABLED=false
EMA_EXIT_NORMAL_LADDER_FRACTIONS=1.0
EMA_EXIT_NORMAL_LADDER_MARKUPS=0.03
EMA_EXIT_MEDIUM_LADDER_FRACTIONS=1.0
EMA_EXIT_MEDIUM_LADDER_MARKUPS=0.03
EMA_EXIT_HEAVY_LADDER_FRACTIONS=1.0
EMA_EXIT_HEAVY_LADDER_MARKUPS=0.03
```

- long TP: средняя цена **+3%**; short TP: средняя цена **−3%**; всё reduce-only.
- adaptive/runner/trailing TP выключены.
- Симметрично нижнему барьеру SL (§12) = равносторонний TP/SL.

> Обоснование (G): 1% барьер ≈ 1.5-2ч типичного дрейфа (медианный 1m-ATR ≈0.09%) — он внутри
> шума и меряет мин-реверс, а не альфу. 3% ≈ 1× суточный ATR: вне ≤4ч шума (~1.5%), достижим
> реальным ходом за 24-48ч (типичный суточный ход ~3.6%, хвосты 10-25%). Не достиг ни одного
> барьера — высвобождает капитал вертикальный барьер (§14).
>
> Опционально лучше плоских 3%: **ATR-масштаб per-symbol** (R = 1.0× суточный ATR, пол 2% /
> потолок 5%), т.к. ATR гуляет 2-5.5% между монетами. Если движок умеет ATR-TP/SL — включить;
> иначе плоские 3% как v1 фазы замера.

## 12. SL — нижний триаж-барьер + soft defensive exit  *(ИЗМЕНЕНО — C, G)*

```
# Нижний барьер = СИММЕТРИЧЕН TP (§11). Было false/0.01 → включён, 3%
HARD_STOP_LOSS_ENABLED=true
HARD_STOP_LOSS_PCT=0.03
HARD_STOP_LOSS_ATR_ENABLED=false
HARD_STOP_LOSS_MIN_EMERGENCY_PCT=0.0

# Soft-exit: только реальный слом сигнала, НЕ шум. Порог поднят 0.01 → 0.03
SOFT_DEFENSIVE_EXIT_ENABLED=true
SOFT_DEFENSIVE_EXIT_MIN_DRAWDOWN=0.03
SOFT_DEFENSIVE_EXIT_BTC_AGAINST_RETURN=0.0
SOFT_DEFENSIVE_EXIT_CONFIRMATIONS=2
SOFT_DEFENSIVE_EXIT_INITIAL_FRACTION=1.0
SOFT_DEFENSIVE_EXIT_STEP_FRACTION=0.0
SOFT_DEFENSIVE_EXIT_MAX_FRACTION=1.0
SOFT_DEFENSIVE_EXIT_REPRICE_MINUTES=3.0
```

Нижний барьер из трёх:

1. **Нижний триаж-барьер (hard stop 3%)** — симметричен TP (§11), основной SL фазы замера.
   Закрытие при `drawdown ≥ 3%`. Равносторонне с TP=+3% → метки TP/SL чистые.

2. **Soft defensive exit (слом сигнала)** — досрочный выход, если EMA-тезис явно сломался
   *и* просадка уже ≥3% (порог поднят с 1%, чтобы не резать на шуме внутри барьера).
   Сигнал считается сломанным, если хотя бы одно:
   - macro trend ушёл против позиции;
   - trigger EMA ушёл против позиции;
   - signal data invalid.

> Равносторонность TP/SL соблюдена: +3% / −3%. Soft-exit на сломе сигнала оставлен как
> информационный (тезис исчез), но с порогом 3%, чтобы не вносить асимметрию внутри барьера и
> не загрязнять метки замера. В фазе эксплуатации (см. чейнджлог G) порог soft-exit и сам SL
> можно вернуть к более раннему/асимметричному срезу.

## 13. Отключённые runner / trailing / account exits

```
EMA_EXIT_RUNNER_ENABLED=false
EMA_EXIT_TRAILING_ENABLED=false
EMA_EXIT_RUNNER_PROFIT_LOCK_ENABLED=false
EMA_EXIT_RUNNER_USE_AGGRESSIVE_LIMIT=false
ACCOUNT_PROFIT_UNLOAD_ENABLED=false
ACCOUNT_PNL_TRAILING_ENABLED=false
ACCOUNT_AVERAGING_ENABLED=false
```

## 14. Time exit — вертикальный барьер 48ч  *(ИЗМЕНЕНО — G)*

```
# Фаза замера: breakeven ВЫКЛЮЧЕН, чтобы не обрезать медленных победителей до выхода на +3%
EMA_BREAKEVEN_ENABLED=false         # было true/24h (вернуть в фазе эксплуатации)
EMA_BREAKEVEN_AFTER_HOURS=48        # неактивно при ENABLED=false; на будущее
EMA_BREAKEVEN_REPRICE_MINUTES=15
EMA_BREAKEVEN_FEE_BUFFER=0.0003
EMA_BREAKEVEN_EXIT_FRACTIONS=1.0
URGENT_TIME_EXIT_AFTER_MINUTES=2880     # 48ч = ВЕРТИКАЛЬНЫЙ БАРЬЕР: полный reduce-only выход по рынку
HARD_TIME_EXIT_AFTER_HOURS=48           # было 72 — совмещён с вертикальным барьером (бэкстоп к urgent)
HARD_TIME_EXIT_CLOSE_FRACTION=1.0       # было 0.25 — полное закрытие (ступенчатость убрана для чистоты замера)
HARD_TIME_EXIT_STEP_MINUTES=720
HARD_TIME_EXIT_FRACTION_STEP=1.0
HARD_TIME_EXIT_MAX_LOSS_ON_NOTIONAL=0.03  # было 0.01 — согласовано с 3% барьером, чтобы релиз срабатывал
HARD_TIME_EXIT_BYPASS_PROFIT_BANK=true
```

- **Вертикальный барьер = 48ч**: если ни +3% (§11), ни −3% (§12) не задеты — позиция полностью
  закрывается reduce-only по рынку (urgent time exit). Это «нет хода → высвобождаем капитал».
- Breakeven-24h **выключен** на фазе замера — иначе он подтягивает TP к нулю и обрезает медленных
  победителей до того, как они дойдут до +3% (загрязнил бы эксперимент). Возвращается в фазе
  эксплуатации (чейнджлог G).
- Закрытие через TP/SL отменяет вертикальный барьер.

## 15. Отключённые controlled loss / absolute force exit

```
ENABLE_CONTROLLED_LOSS_EXIT=false
ENABLE_ABSOLUTE_FORCE_EXIT=false
```

## 16. External price feed

```
EXTERNAL_PRICE_ENTRY_FILTER_ENABLED=false
EXTERNAL_PRICE_DIRECTIONAL_1M_GATE_ENABLED=false
EXTERNAL_PRICE_IMPULSE_CONFIRMATION_ENABLED=false
EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED=false
EXTERNAL_PRICE_DISABLE_TRADING_IF_REFERENCE_STALE=false
EXTERNAL_PRICE_IGNORE_REFERENCE_IF_STALE=true
```

External price используется только как референс цены, не как фильтр.

## 17. Acceptance criteria (v2)

1. Вход открывается **только** при валидных macro trend + pullback recovery + trigger EMA.
2. `FACTOR_ENTRY_ENABLED=false`: факторный top-K **не** управляет входом; снапшоты факторов
   продолжают писаться для оффлайн-анализа.
3. Long и short профили активны.
4. RS к BTC — подтверждающий фильтр с порогом **|RS60| ≥ 0.003**, не часть скоринга.
5. BTC 30m return, volume, chop, volume profile, external impulse, adaptive scoring выключены и
   не гейтят вход.
6. XAUt/BTC RSI overlay переключает long/risk-off режим, но не отбирает отдельные сделки.
7. Усреднение — **максимум 1 ступень**, только при живом EMA-сигнале.
8. Таймфреймы: macro **4h** (EMA25/50), pullback **1h** (EMA6/12), trigger **5m** (EMA12/24).
9. **Триаж-барьеры равносторонние: TP = +3%, SL = −3%** от средней цены, reduce-only.
10. **Вертикальный барьер = 48ч**: при недостижении ни одного барьера — полный reduce-only выход
    по рынку. Breakeven-24h **выключен** на фазе замера.
11. Soft defensive exit срабатывает только при сломе EMA-сигнала **и** просадке ≥3% (не на шуме).
12. Все выходы reduce-only.
13. BTC benchmark единый для обоих профилей.
14. Снапшоты факторов и аналитика собираются, но не работают как active gates на входе.
15. Текущая конфигурация = **фаза замера** (широкие симметричные барьеры для чистого отделения
    альфы от шума). Критерий перехода в фазу эксплуатации: накоплено достаточно закрытий, чтобы
    оценить `P(TP) − P(SL)` и дрейф таймаутов.
