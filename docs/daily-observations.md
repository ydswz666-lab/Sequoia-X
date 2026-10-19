# 收盘日线观察

本模块把拒绝新低、异常成交量区域与趋势延续放在同一份本地 JSON 报告中。
它输出研究观察及依据，不产生订单；全部结果为 `OBSERVE_ONLY`，报告为 `trade_eligible=false`。
无需 TradingView。现有策略仍由原有日常流程执行，海龟的 20 日突破条件在两处共用。

## 运行与数据准备

```bash
pip install -e '.[dev]'

# 如旧库没有可靠复权标记，创建新库；不要覆盖或删除旧库。
DB_PATH=data/sequoia_hfq.db FEISHU_WEBHOOK_URL='' python main.py --backfill

# 从现有本地库生成报告：不更新行情、不创建通知器、不调用飞书。
DB_PATH=data/sequoia_hfq.db python main.py --observe-only

# 截止日期包括当日，但只允许已经结束的日期。
DB_PATH=data/sequoia_hfq.db python main.py --observe-only --as-of 2026-10-16 --output reports/daily-observations.json

# 原有日常更新、策略和通知流程，同时写本地观察报告。
DB_PATH=data/sequoia_hfq.db python main.py

python -m pytest -q
```

运行日常飞书流程时使用自己的配置。请勿提交真实 Webhook URL、`.env` 或数据库。
本地观察无需 Webhook。`--output` 可指定报告路径，默认是 `reports/daily-observations.json`。
`--as-of` 是本地数据截止日期，不代表数据源已补齐该日，也不表示该日经交易日历验证。

新回填和增量统一请求 baostock 后复权数据，所有 OHLC 使用同一口径，成交量与成交额保持供应商原始单位。
`turnover` 在本项目中指成交额，单位为元，不是换手率。
新行记录 `price_adjustment='hfq'`；旧行保留未知值，不因迁移而获得未经证明的口径。
未知口径、混合口径、日期重复或倒序、非有限值、非正价格/成交量、负成交额及不一致 OHLC 会拒绝分析。
迁移只增加元数据列；回填按日期增量续传并刷新最新完整日，未重新抓取的旧历史仍为未知，
因此在旧库再次运行回填不能替代新库重建。
增量与回填都按上海收盘截止日请求数据，并过滤供应商返回的越界日期；重复运行按股票和日期更新，
不会因为一个批次只返回部分股票而删除同日其他股票的数据。
停牌或零成交量行不得作为信号日；数据引擎不保留这些行，指标窗口及区域年龄按有成交的已存日线计数。

## 信号公式

以下 `t` 表示一根已结束日线，所有“此前窗口”均不含 `t`，不使用未来确认点。
均线可以包含 `t` 的收盘价，因为整行结果只在该日收盘之后可用。
不足对应窗口时不触发该项信号；区域观察、突破及趋势可有不同预热长度。

| 信号 | 公式与解释 |
|---|---|
| `breakout_20` | `close[t] > max(high[t-20:t])`，成交额严格大于 1 亿元，且 `close[t] > open[t]`、`close[t] > close[t-1]`。直接复用 `turtle_breakout_mask`；不重复市值等海龟策略附加筛选。 |
| `failure_swing` | `low[t] < min(low[t-N:t]) < close[t]`，N 默认 20。必须严格跌破并严格收回，仅触及或收于参考线不算。 |
| `trend_continuation` | `close[t] > SMA_fast[t] > SMA_slow[t]`，两条均线均较前一根上升，且阳线收盘严格高于昨日最高价。默认 fast=20、slow=60。 |
| `demand_zone_created` | `volume[t] > ratio × mean(volume[t-V:t])`，默认 ratio=2、V=20；阳线且收盘位于当日全幅上 1/3，建立候选需求区。 |
| `supply_zone_created` | 同样的异常成交量条件；阴线且收盘位于当日全幅下 1/3，建立候选供给区。 |
| `demand_retest` | 当日价格区间与此前有效需求区重叠，阳线收盘严格高于该区上沿。 |
| `supply_rejection` | 当日价格区间与此前有效供给区重叠，阴线收盘严格低于该区下沿。 |
| `defense_failure` | 收盘严格跌破此前有效需求区下沿；该区当天失效。 |
| `supply_zone_broken` | 收盘严格突破此前有效供给区上沿；该区当天失效。 |
| `breakout_failure` | 昨日符合共享海龟突破条件，今日收盘回到昨日突破所依据的原 20 日最高价或以下。不是任意历史突破的持续追踪。 |

表中切片区间为左闭右开。区域边界采用**事件日整根日线的最低价与最高价**，
并记录创建日期、索引及成交量相对此前均量的倍数。收盘位置为 `(close-low)/(high-low)`；
零幅度日线没有阳阴方向，不建立供需区。
“需求/供给”只是按日线形态划分的研究标签，不能证明真实买卖盘、机构持仓或后续收益。
拒绝新低也不是已确认反转。

## 区域生命周期与因果性

每个交易日先处理旧区域，再创建当日区域：

1. 若 `当前索引-创建索引 > zone_max_age`，先到期，不再允许回踩或失效信号；默认最多保留 20 根后续日线。
2. 检查收盘穿越造成的失效。需求区严格跌破下沿、供给区严格突破上沿即移除；收盘恰好等于边界仍有效。
3. 对仍有效区域检查回踩或拒绝。仅有影线接触不足以触发，需满足表中的收盘和阳阴条件。
4. 检查当日成交量并建立新区。新区不能在创建日被自己回踩；超出容量时移除最早创建的区域。

`zone_events` 记录 `created`、`retest`、`rejection`、`invalidated`、`expired`、`capacity_evicted`。
同一天多个区域可产生同类事件；行级 `signals` 去重，区域级依据保留。
区域失效或到期只影响当日之后的状态，不回写过去快照，也不把未来确认信号移动到过去。
过去观察可能因供应商数据修订而变化；本次未建立逐时点供应商快照或复权因子审计。

## 参数

使用 `.env` 或环境变量覆盖；嵌套分隔符为两个下划线。例如：

```bash
DAILY_SIGNALS__ZONE_VOLUME_RATIO=3 DB_PATH=data/sequoia_hfq.db python main.py --observe-only
```

| 环境变量 | 默认 | 范围/约束 |
|---|---:|---|
| `DAILY_SIGNALS__SUPPORT_WINDOW` | 20 | 整数 2–250 |
| `DAILY_SIGNALS__VOLUME_WINDOW` | 20 | 整数 2–250 |
| `DAILY_SIGNALS__ZONE_VOLUME_RATIO` | 2.0 | 大于 1，至多 20；当日倍数必须严格超过阈值 |
| `DAILY_SIGNALS__ZONE_MAX_AGE` | 20 | 整数 1–250，按后续有效日线计数 |
| `DAILY_SIGNALS__MAX_ACTIVE_ZONES` | 16 | 整数 1–100 |
| `DAILY_SIGNALS__TREND_FAST` | 20 | 整数 2–250，必须小于慢线窗口 |
| `DAILY_SIGNALS__TREND_SLOW` | 60 | 整数 3–500 |

20 日突破窗口固定为 20，保持与原海龟条件一致。这些默认参数是研究假设，没有收益校准依据。

## 报告、收盘截止与 T+1

报告包含日期截止、可用的本地最新日线、覆盖数量、已评估数量、排除原因、观察列表及配置。
每条观察提供 `signals`、中文 `explanations`、参考 `levels`、成交量倍数、有效区域和区域事件。
`readiness` 分别说明各项指标是否已有完整预热窗口；全部未就绪时以 `INSUFFICIENT_HISTORY` 排除，
不计作一次有效的无信号评估。日期损坏的股票单独排除，不参与全库最新日期对齐。
报告只展示对齐日存在信号或仍有有效区域的股票最新快照，不是逐日历史事件明细。
后复权价位是研究坐标，不能直接当作真实委托价格。
`available_after` 标记信号日上海时间 15:00；它是日线时间边界，不代表数据供应商已即时发布。

入口按上海时区处理当前日期。15:00 前默认排除当天，15:00 起可纳入；显式未来日期、尚未收盘的当天或无效日期会报错。
该规则不连接交易所日历，因此 `calendar_verified=false`；周末、节假日或本地全库陈旧需要使用者核对。
截至截止日，以本地可用的最大日期对齐股票；落后该日期的股票作为 `STALE_SYMBOL` 排除，
数据校验失败作为 `INVALID_DATA` 排除。没有截至该日记录的未来股票不进入历史本地股票集合。
本地最大日期也可能已过时，程序不会把它描述成已验证的最新交易日。

`coverage=LOCAL_DATABASE_ONLY` 明确表示本地样本；不是历史时点的完整 A 股股票池，亦不消除幸存者偏差。
观察为空可能是没有信号，也可能是数据缺失或被排除，不能解释为全市场没有机会或应当空仓。

执行约束仅作为报告政策：

- T 日收盘信号最早在后续交易日考虑进入，`min_entry_session_offset=1`。
- 买入后的股票最早于实际买入后的下一交易日考虑卖出，`min_exit_session_offset_from_actual_entry=1`。
- 不用自然日加一来杜撰可交易日期，不假设 T 日收盘可按该收盘价成交，不允许把 T+1 解释为信号日后的次日可随意卖出。
- 停牌、涨跌停、交易资格、滑点、手续费和真实持仓未建模；观察不构成可成交保证。

## 来源与独立改编

概念参考 Heavy91 的官方 [TradingView_Indicators 仓库](https://github.com/Heavy91/TradingView_Indicators)，
核对版本为 [`2905f62b4f561f1ac1461465c8ac7a691acf1d7a`](https://github.com/Heavy91/TradingView_Indicators/commit/2905f62b4f561f1ac1461465c8ac7a691acf1d7a)：

- [Failure Swing (stop hunting) Strategy](https://github.com/Heavy91/TradingView_Indicators/blob/2905f62b4f561f1ac1461465c8ac7a691acf1d7a/Failure%20Swing%20%28stop%20hunting%29%20Strategy)：参考假突破后收回的思路。
- [Volume Supply and Demand Zones Indicator](https://github.com/Heavy91/TradingView_Indicators/blob/2905f62b4f561f1ac1461465c8ac7a691acf1d7a/Volume%20Supply%20and%20Demand%20Zones%20Indicator)：参考异常成交量附近保留价格区域的思路。

该版本未发现仓库许可证。此次实现独立编写 Python 研究规则，没有复制或逐行翻译 Pine 源码，也未将其作为获得授权的源码移植。
本项目自身的 MIT 许可证不授予 Heavy91 源码的使用权。
为适应收盘日线观察，规则明确不同于原脚本：拒绝新低采用此前窗口和严格跌破/收回，
未照搬原脚本的支撑稳定条件、EMA 距离过滤和交易退出；异常量使用正向倍量与此前均量，
区域取事件日而非前一日，并新增方向标签、失效、到期及容量限制。

## 尚未验证

单元测试和本地报告验证不能证明交易盈利。本次未验证历史收益、胜率、回撤、参数稳定性，
未完成逐时点复权数据审计、真实成交建模或实时全市场运行验收，也没有接入真实交易。
后续收益研究须使用可复现的数据快照与股票池、交易日历及 T+1 成交约束，另行报告结果。
