# Diff `.env` -> ТЗ v2 (строго по docs/TZ_EMA_Pullback_Only_v2.md)

Парсер `.env` (config.py:44) — **первое вхождение ключа побеждает**, строки обрезаются (indented дубликаты тоже парсятся). Поэтому безопасный способ применить v2 — **один блок-оверрайд в начало `.env`** (перебивает все дубликаты ниже, тривиально откатывается удалением блока).

Сводка: **60 изменить, 27 добавить, 44 уже совпадают** (всего v2-ключей 131). Остальные ~163 ключей `.env` не входят в v2 и не трогаются.

> ⚠️ Дубликаты среди v2-ключей в `.env` (актив = первое вхождение): ENTRY_MIN_RS30_ABS×2, ENTRY_MIN_SCORE×2, ENTRY_MAX_NEW_LADDERS_PER_SIGNAL×2, ENTRY_RATE_LIMIT_LADDERS×2, ENTRY_RATE_LIMIT_WINDOW_MINUTES×2, EMA_ENTRY_LADDER_OFFSETS×2, EMA_MAX_AVERAGING_STAGES×2, EMA_AVERAGING_DRAWDOWN_STEP×2, EMA_BREAKEVEN_AFTER_HOURS×2. Блок-оверрайд сверху делает их неважными; при правке in-place нужно менять именно первое вхождение.


## Изменения (old -> new), по разделам ТЗ


**1. Общие гейты**

| key | .env | v2 |
|-----|------|----|
| `FACTOR_ENTRY_ENABLED` | `true` | `false` |

**2. EMA-слои (структура сигнала)**

| key | .env | v2 |
|-----|------|----|
| `EMA_TRIGGER_TIMEFRAME` | `1m` | `5m` |
| `EMA_TRIGGER_FAST_MINUTES` | `50` | `60` |
| `EMA_TRIGGER_SLOW_MINUTES` | `100` | `120` |

**3. Pullback recovery (жёсткий гейт)**

| key | .env | v2 |
|-----|------|----|
| `EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES` | `360` | `120` |

**4. RS монеты к BTC**

| key | .env | v2 |
|-----|------|----|
| `EMA_LONG_MIN_RS60` | `0.0` | `0.003` |
| `EMA_SHORT_MAX_RS60` | `0.0` | `-0.003` |
| `ENTRY_MIN_RS60_ABS` | `0.002` | `0.003` |
| `ENTRY_MIN_RS30_ABS` | `0.0005` | `0.0` |

**5. BTC 30m risk filter — выключен**

| key | .env | v2 |
|-----|------|----|
| `EMA_USE_BTC_RISK_FILTER` | `true` | `false` |
| `EMA_BTC_LONG_MIN_RETURN_30M` | `-0.0025` | `-1.0` |
| `EMA_BTC_SHORT_MAX_RETURN_30M` | `0.0025` | `1.0` |

**6. Macro RSI BTC/XAUt overlay**

| key | .env | v2 |
|-----|------|----|
| `RISK_OFF_LONG_BUDGET_MULTIPLIER` | `0.55` | `0.0` |
| `RISK_OFF_SHORT_BUDGET_MULTIPLIER` | `0.85` | `1.0` |

**7. Дополнительные фильтры и скоринг — жёсткие гейты**

| key | .env | v2 |
|-----|------|----|
| `EMA_CHOP_FILTER_ENABLED` | `true` | `false` |
| `EMA_VOLUME_CONFIRMATION_ENABLED` | `true` | `false` |
| `EMA_VOLUME_SPIKE_FILTER_ENABLED` | `true` | `false` |
| `EMA_VOLUME_PROFILE_FILTER_ENABLED` | `true` | `false` |
| `ENTRY_MIN_SCORE` | `0.02` | `0.0` |
| `ENTRY_MACRO_INVALID_PENALTY` | `0.009` | `999.0` |
| `ENTRY_PULLBACK_INVALID_PENALTY` | `0.003` | `999.0` |
| `ENTRY_TRIGGER_INVALID_PENALTY` | `0.009` | `999.0` |
| `ENTRY_BTC_INVALID_PENALTY` | `0.006` | `0.0` |
| `ENTRY_MARKET_STRUCTURE_INVALID_PENALTY` | `0.010` | `0.0` |
| `ENTRY_VOLUME_INVALID_PENALTY` | `0.006` | `0.0` |
| `ENTRY_CHOP_INVALID_PENALTY` | `0.006` | `0.0` |
| `ENTRY_QUALITY_BUDGET_REFERENCE` | `0.03` | `1.0` |

**8. Ограничение частоты входов**

| key | .env | v2 |
|-----|------|----|
| `ENTRY_RATE_LIMIT_LADDERS` | `3` | `6` |
| `ENTRY_RATE_LIMIT_WINDOW_MINUTES` | `30` | `60` |
| `ENTRY_CROWDED_SIGNAL_FRACTION` | `0.30` | `1.0` |
| `ENTRY_CROWDED_MIN_SIGNALS` | `12` | `999` |
| `ENTRY_CROWDED_MIN_SCORE` | `0.02` | `0.0` |
| `ENTRY_CROWDED_MIN_RS60_ABS` | `0.003` | `0.0` |
| `ENTRY_CROWDED_MIN_RS30_ABS` | `0.0015` | `0.0` |

**9. Entry ladder**

| key | .env | v2 |
|-----|------|----|
| `EMA_ENTRY_LADDER_OFFSETS` | `0.0,0.01` | `0.0,0.008` |

**10. Усреднение**

| key | .env | v2 |
|-----|------|----|
| `EMA_MAX_AVERAGING_STAGES` | `2` | `1` |
| `EMA_AVERAGING_INTERVAL_HOURS` | `8` | `6.0` |
| `EMA_AVERAGING_MIN_DAILY_VOLATILITY_FRACTION` | `0.18` | `0.0` |

**11. Take Profit — верхний триаж-барьер**

| key | .env | v2 |
|-----|------|----|
| `EMA_TAKE_PROFIT_MARKUP` | `0.01` | `0.03` |
| `EMA_ADAPTIVE_EXIT_ENABLED` | `true` | `false` |
| `EMA_EXIT_NORMAL_LADDER_FRACTIONS` | `0.25,0.25,0.25,0.15` | `1.0` |
| `EMA_EXIT_NORMAL_LADDER_MARKUPS` | `0.012,0.020,0.032,0.050` | `0.03` |
| `EMA_EXIT_MEDIUM_LADDER_FRACTIONS` | `0.45,0.30,0.15,0.10` | `1.0` |
| `EMA_EXIT_MEDIUM_LADDER_MARKUPS` | `0.004,0.010,0.020,0.035` | `0.03` |
| `EMA_EXIT_HEAVY_LADDER_FRACTIONS` | `0.60,0.25,0.15` | `1.0` |
| `EMA_EXIT_HEAVY_LADDER_MARKUPS` | `0.003,0.008,0.015` | `0.03` |

**12. SL — нижний триаж-барьер + soft defensive exit**

| key | .env | v2 |
|-----|------|----|
| `HARD_STOP_LOSS_PCT` | `0.04` | `0.03` |

**13. Отключённые runner / trailing / account exits**

| key | .env | v2 |
|-----|------|----|
| `EMA_EXIT_RUNNER_ENABLED` | `true` | `false` |
| `EMA_EXIT_TRAILING_ENABLED` | `true` | `false` |
| `ACCOUNT_PROFIT_UNLOAD_ENABLED` | `true` | `false` |
| `ACCOUNT_AVERAGING_ENABLED` | `true` | `false` |

**14. Time exit — вертикальный барьер 48ч**

| key | .env | v2 |
|-----|------|----|
| `EMA_BREAKEVEN_ENABLED` | `true` | `false` |
| `EMA_BREAKEVEN_AFTER_HOURS` | `12` | `48` |
| `EMA_BREAKEVEN_FEE_BUFFER` | `0.0002` | `0.0003` |
| `HARD_TIME_EXIT_STEP_MINUTES` | `360` | `720` |

**15. Отключённые controlled loss / absolute force exit**

| key | .env | v2 |
|-----|------|----|
| `ENABLE_CONTROLLED_LOSS_EXIT` | `true` | `false` |

**16. External price feed**

| key | .env | v2 |
|-----|------|----|
| `EXTERNAL_PRICE_ENTRY_FILTER_ENABLED` | `true` | `false` |
| `EXTERNAL_PRICE_DIRECTIONAL_1M_GATE_ENABLED` | `true` | `false` |
| `EXTERNAL_PRICE_IMPULSE_CONFIRMATION_ENABLED` | `true` | `false` |
| `EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED` | `true` | `false` |

## Добавить (нет в `.env`)


**1. Общие гейты**

- `BOT_PROFILES=long,short`

**6. Macro RSI BTC/XAUt overlay**

- `ENABLE_GOLD_DIRECTIONAL_BIAS=false`

**7. Дополнительные фильтры и скоринг — жёсткие гейты**

- `ENTRY_BTC_RETURN_PENALTY_MULTIPLIER=0.0`
- `ENTRY_RS60_SHORTFALL_PENALTY_MULTIPLIER=0.0`
- `ENTRY_RS30_SHORTFALL_PENALTY_MULTIPLIER=0.0`
- `ENTRY_QUALITY_BUDGET_MIN_MULTIPLIER=1.0`

**10. Усреднение**

- `MAX_BUY_STAGES=2`
- `AVERAGING_BUDGET_FRACTION_1=1.0`
- `AVERAGING_DRAWDOWN_STEP_1=0.01`

**12. SL — нижний триаж-барьер + soft defensive exit**

- `HARD_STOP_LOSS_MIN_EMERGENCY_PCT=0.0`
- `SOFT_DEFENSIVE_EXIT_ENABLED=true`
- `SOFT_DEFENSIVE_EXIT_MIN_DRAWDOWN=0.03`
- `SOFT_DEFENSIVE_EXIT_BTC_AGAINST_RETURN=0.0`
- `SOFT_DEFENSIVE_EXIT_CONFIRMATIONS=2`
- `SOFT_DEFENSIVE_EXIT_INITIAL_FRACTION=1.0`
- `SOFT_DEFENSIVE_EXIT_STEP_FRACTION=0.0`
- `SOFT_DEFENSIVE_EXIT_MAX_FRACTION=1.0`
- `SOFT_DEFENSIVE_EXIT_REPRICE_MINUTES=3.0`

**13. Отключённые runner / trailing / account exits**

- `EMA_EXIT_RUNNER_PROFIT_LOCK_ENABLED=false`
- `EMA_EXIT_RUNNER_USE_AGGRESSIVE_LIMIT=false`

**14. Time exit — вертикальный барьер 48ч**

- `URGENT_TIME_EXIT_AFTER_MINUTES=2880`
- `HARD_TIME_EXIT_AFTER_HOURS=48`
- `HARD_TIME_EXIT_CLOSE_FRACTION=1.0`
- `HARD_TIME_EXIT_FRACTION_STEP=1.0`
- `HARD_TIME_EXIT_MAX_LOSS_ON_NOTIONAL=0.03`
- `HARD_TIME_EXIT_BYPASS_PROFIT_BANK=true`

**15. Отключённые controlled loss / absolute force exit**

- `ENABLE_ABSOLUTE_FORCE_EXIT=false`

## Готовый блок-оверрайд (вставить в НАЧАЛО `.env`)

```
# ===== ТЗ v2 override block (prepend; first-occurrence-wins) =====
# --- 1. Общие гейты ---
BOT_PROFILES=long,short
FACTOR_ENTRY_ENABLED=false
# --- 2. EMA-слои (структура сигнала) ---
EMA_TRIGGER_TIMEFRAME=5m
EMA_TRIGGER_FAST_MINUTES=60
EMA_TRIGGER_SLOW_MINUTES=120
# --- 3. Pullback recovery (жёсткий гейт) ---
EMA_PULLBACK_RECOVERY_MAX_CROSS_AGE_MINUTES=120
# --- 4. RS монеты к BTC ---
EMA_LONG_MIN_RS60=0.003
EMA_SHORT_MAX_RS60=-0.003
ENTRY_MIN_RS60_ABS=0.003
ENTRY_MIN_RS30_ABS=0.0
# --- 5. BTC 30m risk filter — выключен ---
EMA_USE_BTC_RISK_FILTER=false
EMA_BTC_LONG_MIN_RETURN_30M=-1.0
EMA_BTC_SHORT_MAX_RETURN_30M=1.0
# --- 6. Macro RSI BTC/XAUt overlay ---
RISK_OFF_LONG_BUDGET_MULTIPLIER=0.0
RISK_OFF_SHORT_BUDGET_MULTIPLIER=1.0
ENABLE_GOLD_DIRECTIONAL_BIAS=false
# --- 7. Дополнительные фильтры и скоринг — жёсткие гейты ---
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
# --- 8. Ограничение частоты входов ---
ENTRY_RATE_LIMIT_LADDERS=6
ENTRY_RATE_LIMIT_WINDOW_MINUTES=60
ENTRY_CROWDED_SIGNAL_FRACTION=1.0
ENTRY_CROWDED_MIN_SIGNALS=999
ENTRY_CROWDED_MIN_SCORE=0.0
ENTRY_CROWDED_MIN_RS60_ABS=0.0
ENTRY_CROWDED_MIN_RS30_ABS=0.0
# --- 9. Entry ladder ---
EMA_ENTRY_LADDER_OFFSETS=0.0,0.008
# --- 10. Усреднение ---
EMA_MAX_AVERAGING_STAGES=1
MAX_BUY_STAGES=2
EMA_AVERAGING_INTERVAL_HOURS=6.0
EMA_AVERAGING_MIN_DAILY_VOLATILITY_FRACTION=0.0
AVERAGING_BUDGET_FRACTION_1=1.0
AVERAGING_DRAWDOWN_STEP_1=0.01
# --- 11. Take Profit — верхний триаж-барьер ---
EMA_TAKE_PROFIT_MARKUP=0.03
EMA_ADAPTIVE_EXIT_ENABLED=false
EMA_EXIT_NORMAL_LADDER_FRACTIONS=1.0
EMA_EXIT_NORMAL_LADDER_MARKUPS=0.03
EMA_EXIT_MEDIUM_LADDER_FRACTIONS=1.0
EMA_EXIT_MEDIUM_LADDER_MARKUPS=0.03
EMA_EXIT_HEAVY_LADDER_FRACTIONS=1.0
EMA_EXIT_HEAVY_LADDER_MARKUPS=0.03
# --- 12. SL — нижний триаж-барьер + soft defensive exit ---
HARD_STOP_LOSS_PCT=0.03
HARD_STOP_LOSS_MIN_EMERGENCY_PCT=0.0
SOFT_DEFENSIVE_EXIT_ENABLED=true
SOFT_DEFENSIVE_EXIT_MIN_DRAWDOWN=0.03
SOFT_DEFENSIVE_EXIT_BTC_AGAINST_RETURN=0.0
SOFT_DEFENSIVE_EXIT_CONFIRMATIONS=2
SOFT_DEFENSIVE_EXIT_INITIAL_FRACTION=1.0
SOFT_DEFENSIVE_EXIT_STEP_FRACTION=0.0
SOFT_DEFENSIVE_EXIT_MAX_FRACTION=1.0
SOFT_DEFENSIVE_EXIT_REPRICE_MINUTES=3.0
# --- 13. Отключённые runner / trailing / account exits ---
EMA_EXIT_RUNNER_ENABLED=false
EMA_EXIT_TRAILING_ENABLED=false
EMA_EXIT_RUNNER_PROFIT_LOCK_ENABLED=false
EMA_EXIT_RUNNER_USE_AGGRESSIVE_LIMIT=false
ACCOUNT_PROFIT_UNLOAD_ENABLED=false
ACCOUNT_AVERAGING_ENABLED=false
# --- 14. Time exit — вертикальный барьер 48ч ---
EMA_BREAKEVEN_ENABLED=false
EMA_BREAKEVEN_AFTER_HOURS=48
EMA_BREAKEVEN_FEE_BUFFER=0.0003
URGENT_TIME_EXIT_AFTER_MINUTES=2880
HARD_TIME_EXIT_AFTER_HOURS=48
HARD_TIME_EXIT_CLOSE_FRACTION=1.0
HARD_TIME_EXIT_STEP_MINUTES=720
HARD_TIME_EXIT_FRACTION_STEP=1.0
HARD_TIME_EXIT_MAX_LOSS_ON_NOTIONAL=0.03
HARD_TIME_EXIT_BYPASS_PROFIT_BANK=true
# --- 15. Отключённые controlled loss / absolute force exit ---
ENABLE_CONTROLLED_LOSS_EXIT=false
ENABLE_ABSOLUTE_FORCE_EXIT=false
# --- 16. External price feed ---
EXTERNAL_PRICE_ENTRY_FILTER_ENABLED=false
EXTERNAL_PRICE_DIRECTIONAL_1M_GATE_ENABLED=false
EXTERNAL_PRICE_IMPULSE_CONFIRMATION_ENABLED=false
EXTERNAL_PRICE_EXIT_ADJUSTMENT_ENABLED=false
# ===== end ТЗ v2 override block =====
```
