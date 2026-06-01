# HTX Futures EMA Pullback Bot

[English](../readme.md) | [Русский](readme.ru.md) | [策略详情](../strategy.md)

HTX Futures EMA Pullback Bot 是一个用于 HTX USDT-M 合约的 Python crypto trading bot。它可以运行 long 和 short 两个交易配置，扫描可配置的山寨币列表，基于已收盘 K 线生成 EMA pullback strategy 信号，并在一个进程中管理限价入场、补仓、reduce-only 出场和 breakeven 行为。

这个项目适合希望快速启动、通过 `.env` 灵活配置，并直接使用一套完整 EMA Pullback strategy 的交易者和开发者。机器人是 live-ordering 应用：启动前需要 HTX API credentials，并应先通过测试或 mock/stub exchange 场景验证关键流程。

> 合约交易风险很高。本项目只是软件，不是投资建议。启用真实交易前，请先审计代码、纸面交易、回测，并理解每一个配置项。

## 项目亮点

- **启动简单**：安装依赖，复制 `.env.example`，运行 `python bot.py`。
- **Long 和 short 配置**：可以只运行 `long`、只运行 `short`，也可以两个同时运行。
- **开箱即用的 EMA Pullback strategy**：包含 macro trend、pullback recovery、trigger EMA、相对 BTC 强弱、BTC risk filter、score、top-N 选择和拥挤行情限速。
- **灵活的 `.env` 配置**：EMA 周期、entry gates、risk budget、averaging、breakeven、外部参考价格和 macro overlay 都可以不改代码直接调整。
- **USDT-M futures workflow**：支持 HTX linear swap markets、cross margin、one-way position mode、post-only 入场和 reduce-only 出场。
- **基于 CCXT 的交易所层**：当前实现面向 HTX，但 CCXT 让 market data、precision 和 order workflow 更容易迁移到其他受支持交易所，前提是补齐对应适配层。
- **运营级 CSV 日志**：记录交易事件、周期统计、macro context 和外部价格诊断。
- **核心行为有测试覆盖**：pytest 覆盖信号生成、entry gates、补仓、breakeven、profile 处理和安全分支。

## 策略概览

默认启用的策略是 `ema_pullback`：在更高周期 EMA 趋势方向上交易山寨币，等待中周期回调恢复，并由短周期 EMA 触发确认。

默认 EMA 映射：

| 层级 | 配置 | 默认周期 | 实际 EMA |
|---|---:|---:|---:|
| Macro fast | `EMA_MACRO_FAST_MINUTES=36000` | `1d` | EMA25 |
| Macro slow | `EMA_MACRO_SLOW_MINUTES=72000` | `1d` | EMA50 |
| Pullback fast | `EMA_PULLBACK_FAST_MINUTES=1440` | `4h` | EMA6 |
| Pullback slow | `EMA_PULLBACK_SLOW_MINUTES=2880` | `4h` | EMA12 |
| Trigger fast | `EMA_TRIGGER_FAST_MINUTES=50` | `1m` | EMA50 |
| Trigger slow | `EMA_TRIGGER_SLOW_MINUTES=100` | `1m` | EMA100 |

Long 入场条件：

```text
EMA25D > EMA50D
EMA1D 在近期回调后重新站上 EMA2D
EMA50 > EMA100
rs60 >= EMA_LONG_MIN_RS60
btc_return_30m >= EMA_BTC_LONG_MIN_RETURN_30M
```

Short 入场使用镜像逻辑：

```text
EMA25D < EMA50D
EMA1D 在近期反弹后重新跌破 EMA2D
EMA50 < EMA100
rs60 <= EMA_SHORT_MAX_RS60
btc_return_30m <= EMA_BTC_SHORT_MAX_RETURN_30M
```

信号不是单一均线交叉。它结合了高周期趋势方向、recovery-cross 年龄、recovery gap、短周期触发、相对 BTC 强弱、短期 BTC 风险和用于候选排序的 score。完整实现级说明见 [strategy.md](../strategy.md)。

## 风险与仓位管理

默认主流程会从入场价放置交易所侧 reduce-only hard stop-loss：`HARD_STOP_LOSS_PCT=0.02`。如果信号包含已收盘 K 线 ATR，止损可按 `HARD_STOP_LOSS_ATR_MULTIPLIER=2.0` 放宽，但不超过 `HARD_STOP_LOSS_ATR_MAX_PCT=0.03`。重启后 ATR 暂不可用时，固定止损仍作为 fallback。

关键默认值：

- `BOT_PROFILES=long,short`。
- 默认用两个限价订单完成初始入场。
- 新入场需要通过 quality gates：score、RS60、RS30、top-N、rate-limit 和 crowded-market rules。
- 补仓受阶段数、时间间隔、drawdown step 和信号健康状态限制。
- Stop-loss 使用 exchange-side TPSL/reduce-only market close，数量不会超过当前 `position_size`。
- Breakeven 会在配置的持仓时间后激活，停止继续补仓，并重新设置 reduce-only 出场。
- Combined mode 会阻止 long 和 short 配置在同一币种上开出相反方向风险敞口。

真实交易需要 HTX API credentials，并且账户杠杆必须可确定。`LEVERAGE` 用于机器人内部 sizing 和 notional 限制；启动时不会自动修改交易所杠杆。

## 快速开始

要求：

- 本地已在 Python 3.14 验证；建议 Python 3.11+。
- 真实交易需要 HTX account：[使用 invite code `6hc25223` 开通 HTX](https://www.htx.com/invite/en-us/1f?invite_code=6hc25223)。
- 可选的 MEXC account 可用于参考市场研究：[通过此 referral link 开通 MEXC](https://promote.mexc.com/r/lxcLKaZgvh)。当前 MEXC radar 使用公开市场数据，不需要 MEXC API keys。
- 需要 USDT-M futures 权限。

安装并进行本地检查：

```powershell
python -m pip install -r requirements.txt
copy .env.example .env
python -m pytest -q
```

只运行一个 profile：

```powershell
python bot.py --profiles long
python bot.py --profiles short
```

明确运行两个 profile：

```powershell
python bot.py --profiles long,short
```

运行 `python bot.py` 会连接 HTX，并可能发送真实订单；启动前请确认 `.env`、账户状态、risk limits 和测试结果。

## 配置

将 `.env.example` 复制为 `.env`，只覆盖你需要修改的值。为了兼容，也支持 `long/.env` 和 `short/.env`。

与真实交易相关的最小配置：

```dotenv
HTX_API_KEY=
HTX_API_SECRET=
BOT_PROFILES=long,short
```

常用 profile 覆盖：

```dotenv
LONG_LEVERAGE=30
SHORT_LEVERAGE=30
```

策略调参示例：

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

机器人包含一个 macro overlay，用 RSI 和 gold/BTC 相对变化比较黄金代理资产，通常是 XAUT，与 BTC。它不是独立入场策略，而是市场状态层：当加密资产相对防御资产走弱时，它可以降低风险、阻止 averaging，或让出场更保守。

默认 macro 配置：

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

Overlay 会识别这些状态：

- `crypto_underperforms_gold`：黄金强，而 BTC 弱或明显落后。Long budget 会降低，short 方向可以更宽松，averaging 可被禁用，time-exit 会更快。
- `crypto_risk_on`：BTC 强而黄金落后。机器人保持正常 long 行为，并可降低 short 激进度。
- `broad_liquidity_risk_on`：BTC 和黄金都强。它被视为建设性状态，但不会自动提高杠杆。
- `deleveraging`：BTC 和黄金都弱。新入场可被阻止，averaging 禁用，出场加速。
- `neutral` 或 `macro_unavailable`：overlay 不增加强方向过滤。

Macro context 会被缓存、写入 CSV，并用于 new-entry block、averaging block、ladder multiplier 和 time-exit multiplier 等决策。精确状态逻辑见 [strategy.md](../strategy.md)。

## MEXC Reference Price Signals

External price radar 会比较 HTX order-book mid price 与公开 MEXC book-ticker 数据。它用于在开仓或管理仓位前发现 cross-exchange premium、discount、stale reference data 和短期 impulse 差异。

默认 MEXC 相关控制：

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

实际行为：

- 当 HTX 相对 MEXC 太贵时，long 入场可被阻止。
- 当 HTX 相对 MEXC 太便宜时，short 入场可被阻止。
- 1 分钟 HTX/MEXC 大幅背离可让该 symbol 进入 cooldown。
- 如果 MEXC 在同方向上领先 HTX，候选信号可获得 impulse score bonus。
- 当 HTX premium/discount 提示更谨慎的 take-profit 时，exit ladder 可被收紧。
- 如果 reference data 过期，默认行为是忽略参考源，而不是停止全部交易；也可以在 `.env` 中改成更严格。

Radar 会把 HTX symbols 映射成 MEXC spot-style `BASEUSDT` symbols，并把 bid、ask、mid、spread、z-score、age 和 short-window changes 写入 external price CSV。

## 迁移到其他交易所

交易所客户端通过 CCXT 创建，这是可迁移性的良好基础：市场加载、precision formatting、timeframes、tick sizes 和标准 order calls 已经走统一 exchange abstraction。

当前版本仍然是 HTX-specific。真实交易使用 `ccxt.htx`、HTX USDT-M futures 约定、HTX contract hostnames、one-way mode 检查、手动 leverage 读取、reduce-only/order parameters 和 HTX price-band 处理。迁移到其他 CCXT-supported exchange 应该比从零重写容易得多，但仍应视为 adapter task：在真实使用前，需要实现对应 exchange factory、credentials、market filters、leverage/position-mode handling、order flags 和测试。

## 监控输出

运行时 CSV 文件会在本地生成，并被 Git 忽略：

- trade events；
- cycle statistics；
- macro overlay context；
- external reference-price diagnostics；
- markets cache 和 bot state files。

这些文件可以帮助分析信号为什么被接受或拒绝、仓位如何计算、补仓何时被阻止，以及出场订单如何重新定价。

## 测试

运行测试：

```powershell
python -m pytest -q
```

当前本地 baseline：

```text
82 passed, 4 subtests passed
```

## 项目结构

```text
bot.py                 CLI entrypoint
config.py              基于 dataclass 的配置和 profile 解析
htxbot/                Bot engine、exchange、signals、strategy、state、monitoring
tests/                 统一 pytest suite
.env.example           安全配置模板
strategy.md            完整策略和行为说明
docs/                  多语言发布文档
```

## 安全

- 不要提交 `.env`、`long/.env` 或 `short/.env`。
- 限制 API key 权限；如果怀疑泄露，请立即轮换。
- 启动前运行测试，并用 mock/stub exchange 验证关键场景。
- 在明确启用真实订单前，尽量使用最小 API 权限。

## 免责声明

自动化合约交易可能快速产生亏损，尤其是在使用杠杆、流动性缺口、交易所故障、配置错误或市场状态变化时。本仓库只提供代码和文档。测试、部署、交易所设置、风险限制和交易决策均由使用者自行负责。

## License

MIT License。见 [LICENSE](../LICENSE)。
