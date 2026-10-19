# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 运行模式

```bash
python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
python main.py --backfill     # 回填模式：全市场历史K线一次性灌入（约12分钟）
python main.py --observe-only # 本地观察模式：只读已有行情并生成日线信号 JSON，不联网更新或推送
```

日常模式保留原有策略及飞书通知，并额外生成 `reports/daily-observations.json`。
运行时间与数据覆盖取决于数据源和本地库，以上历史估计不构成当前实测结果。

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |

## 新增：收盘日线观察

无需 TradingView。Python 观察模块把拒绝新低、异常成交量区域和趋势延续组合到同一份可解释报告中：

- **拒绝新低**：最低价严格跌破此前 N 根日线低点，收盘重新站回；默认 N=20。
- **供需区域观察**：成交量严格超过此前 20 根均量的 2 倍，结合阳阴线和收盘位置形成候选区域；后续记录回踩、拒绝、失效和到期。
- **趋势延续及纠错**：检查上升的 20/60 日均线、收盘越过昨日高点，以及前一日突破后的失效。
- **20 日新高**：直接复用现有海龟策略的突破条件，不另写一套重复选股策略。

全部为 `OBSERVE_ONLY` 研究观察，报告的 `trade_eligible` 为 `false`。信号在 T 日收盘后才可用；
最早于后续交易日考虑进入，新买入股票最早于实际买入后的下一交易日考虑退出。
代码不生成订单，也不模拟停牌、涨跌停或实际成交。

先在已回填的后复权库上运行：

```bash
DB_PATH=data/sequoia_hfq.db python main.py --observe-only
DB_PATH=data/sequoia_hfq.db python main.py --observe-only --as-of 2026-10-16 --output reports/daily-observations.json
```

本地观察模式无需配置飞书地址。报告仅覆盖本地数据库，包含排除原因；没有候选不代表全市场空仓。
旧数据库复权口径不明时会被拒绝分析，应按下文创建新库。
完整公式、参数、来源与限制见 [日线观察说明](docs/daily-observations.md)。

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 运行原有飞书推送时，填写自己的 Webhook URL；只做本地观察无需填写
```

### 3. 首次回填历史数据

本次升级为新行情写入 `price_adjustment='hfq'`。旧数据没有可证明的复权口径，
程序不会把旧行直接标记为后复权。请新建一个数据库，保留原库：

```bash
DB_PATH=data/sequoia_hfq.db FEISHU_WEBHOOK_URL='' python main.py --backfill
```

完成后，在 `.env` 中设置 `DB_PATH=data/sequoia_hfq.db`，后续更新与观察使用同一数据库。
回填需要网络和数据源可用，不保证固定耗时或全市场覆盖。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── daily_signals.py     # 因果日线观察及共享海龟条件
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   └── rps_breakout.py      # RPS 突破策略
│   ├── research/
│   │   └── daily_report.py     # 本地数据覆盖、截止日和 JSON 报告
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
├── docs/daily-observations.md   # 信号公式、数据边界与来源说明
├── reports/                    # 本地观察报告（运行时生成，不入 git）
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）
- **复权方式**：新增回填与增量数据统一向 baostock 请求后复权（hfq），同时记录口径；未知或混合口径不得进入新观察模块。供应商历史修订及逐时点复权因子尚未审计。
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：8 进程并行通过 baostock 拉取，2~3 分钟完成全市场更新

## 验证与边界

```bash
pip install -e '.[dev]'
python -m pytest -q
```

测试覆盖历史前缀一致性、未来行不影响既往信号、区域生命周期、复权数据校验和本地报告流程。
默认仅使用上海时区 15:00 后已结束的日期；显式未来或未收盘日期会报错。
系统尚未接入交易日历校验，报告会标明 `calendar_verified=false`。
本次升级没有验证策略收益、胜率、收益回撤或实时全市场覆盖，测试通过只表明所测规则与流程符合预期。

---

## 许可证 | License

MIT
