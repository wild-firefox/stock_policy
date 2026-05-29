# Stock Policy

A 股量化投研工具链——行情数据采集、技术指标计算、多股回测、新闻采集。

## 环境准备

### 1. 安装 uv（Python 包管理器）

```bash
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 安装依赖

```bash
uv sync
uv run playwright install chromium
```

### 3. 配置 API Token

```bash
cp config.example.py config.py
```

编辑 `config.py`，填入你的 API Token：

```python
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "你的Tushare Token")
BOCHA_API_KEY = os.environ.get("BOCHA_API_KEY", "你的博查AI Key")
```

- **Tushare**：注册 https://tushare.pro ，获取 Token（需积分 >= 2000 才能调用 `stk_factor_pro` 接口）
- **博查 AI**：注册 https://open.bochaai.com ，获取 API Key（新闻搜索用，可选）

### 4. 导出股票列表（可选）

```bash
uv run python export_all_stocks.py   # 生成 a_shares_list.csv
uv run python export_etfs.py         # 生成 etf_list.csv
```

---

## 功能模块

### 行情数据

下载全量 A 股 / ETF / 指数日线数据（含 200+ 技术因子）：

```bash
uv run python data_manager/download_stk.py       # A 股日线
uv run python data_manager/download_etf.py       # ETF 日线
uv run python data_manager/download_idx.py       # 指数日线
```

下载完成后构建 HDF5（供回测引擎使用）：

```bash
uv run python data_manager/recalc_qfq.py          # 重算前复权
uv run python data_manager/scripts/build_hdf5.py  # 股票 CSV → HDF5
uv run python data_manager/scripts/build_etf_hdf5.py  # ETF CSV → HDF5
```

数据存储结构：

```
data_manager/data/
├── stk/raw/              # 每只 A 股一个 CSV
├── stk/raw_h5/           # HDF5 合并包
├── etf/raw/              # 每只 ETF 一个 CSV
├── etf/raw_h5/           # ETF HDF5 包
└── idx/raw/              # 指数 CSV
```

### 回测

```bash
# 多因子均值回归策略（推荐，2020-2025 超额 +127%）
uv run python data_manager/backtest/strategy/strategy_multi_factor.py

# QFQ 指标使用演示（展示如何正确使用前复权数据）
uv run python data_manager/backtest/strategy/strategy_qfq_demo.py

# 多股均线金叉/死叉策略
uv run python data_manager/backtest/run_multi_backtest.py

# 单股均线策略（平安银行 000001）
uv run python data_manager/backtest/strategy/strategy_ma.py

# 小市值轮动策略
uv run python data_manager/backtest/strategy/strategy_smallgo.py

# ETF RSRS+动量轮动策略
uv run python data_manager/backtest/strategy/etf_rotation.py
```

回测输出（每轮运行生成时间戳目录）：

```
data_manager/backtest/log/<策略名>/<时间戳>/
├── report.md              # 收益/回撤/夏普统计
├── transactions.csv       # 逐笔买卖流水
├── trades.csv             # 平仓记录
└── return_curve_with_trades.png  # 收益曲线图
```

引擎特性：
- T+1 开盘价成交，整手（100 股）取整
- 双边佣金 + 印花税（0.03%）
- 先卖后买释放现金，逐笔现金校验
- 与 JoinQuant 结果对比验证（误差 < 0.1%）

---

## 使用 AI Agent 编写与迭代策略

本项目支持通过 Claude Code（或其他兼容 AI Agent）自动编写、回测、迭代量化策略。

### 前置准备

1. 安装 [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
2. 将本项目克隆为 Claude Code 工作目录
3. Agent 会自动读取 `CLAUDE.md`（项目指令）和 `STRATEGY_MANUAL.md`（策略手册）

### 第一次使用

打开 Claude Code 后，直接说出你的策略想法，Agent 会：

1. **参考策略手册**：自动读取 `data_manager/backtest/strategy/STRATEGY_MANUAL.md`，了解引擎 API、可用数据列、指标函数、常见陷阱
2. **编写策略代码**：在 `data_manager/backtest/strategy/` 下生成策略文件
3. **运行回测**：`uv run python` 执行回测，打印收益/夏普/回撤
4. **迭代优化**：根据回测结果调整因子权重、参数、过滤条件
5. **记录经验**：错误自动写入 `.claude/memory/gotchas.md`，策略发现写入 `.claude/memory/strategy_research.md`

### 策略编写示例

```
> 帮我写一个基于低换手率 + 低PB + 小市值的多因子策略，
  持仓 20 只，每 10 天调仓，2020-2025 年回测
```

Agent 会：
- 阅读手册了解 `today_data` 可用列（`turnover_rate`, `pb`, `circ_mv`, `bias1_qfq` 等）
- 过滤股票池（PE > 0, 非跌停, 流通市值 > 30亿）
- 横截面百分位排名打分（注意：价格衍生指标必须用 `_qfq` 列）
- 生成买卖订单（卖出不在 top 缓冲区的持仓，等权买入新人选）
- 运行回测并输出报告

### 关键约束

Agent 在编写策略时自动遵守以下规则：

| 规则 | 说明 |
|------|------|
| **qfq 横截面** | 多股票排名打分必须用 `_qfq` 列（前复权），`_bfq` 列（前复权）仅用于存储 |
| **QFQ 止损** | 绝对价格阈值必须用 `engine.get_history_qfq()` |
| **无未来函数** | T+1 开盘价成交，策略只看到当日收盘数据 |
| **回测区间** | 默认 2020-2025 年中，覆盖完整牛熊周期 |
| **TDD** | 所有纯逻辑函数必须有 pytest，失败先记录 gotchas |

### 策略迭代流程

```
想法 → Agent 写策略 → uv run 回测 → 看报告
                                      ↓
                              不理想 → 分析因子 IC/IR
                                      ↓
                              Agent 调整参数/因子
                                      ↓
                              满意 → 记录到 strategy_research.md
```

详细参考：
- 策略 API：`data_manager/backtest/strategy/STRATEGY_MANUAL.md`
- AI 可读项目说明：`readme_ai.md`

---

### 新闻采集

```bash
# 韭研公社盘前纪要（工作日每日更新）
uv run python news_manager/jiuyangongshe_scraper.py --latest
uv run python news_manager/jiuyangongshe_scraper.py --date 20260526

# 同花顺个股新闻（自动抓取近 4 个交易日全文）
uv run python news_manager/stocknews_scraper.py list --code 600410
uv run python news_manager/stocknews_scraper.py list --code 600410 --fetch-days 7

# 博查 AI 搜索 + 全文抓取
uv run python news_manager/bocha_scraper.py --code 600410
uv run python news_manager/bocha_scraper.py --code 600410 --days 5
```

新闻数据存储：

```
news_manager/data/
├── jygs/pqjy/<YYYYMMDD>/       # 韭研公社文章 + 图片
├── stk/raw_ths/<code>/         # 同花顺新闻列表 + 全文
└── stk/raw_Bocha/<code>/       # 博查搜索结果 + 全文
```

---

## 项目结构

```
stock_policy/
├── config.example.py         # 配置文件模板 → 复制为 config.py 并填入 Token
├── data_manager/             # 行情数据 + 回测引擎
│   ├── download_*.py         #   数据下载器
│   ├── scripts/build_*.py    #   CSV → HDF5 构建
│   ├── backtest/             #   回测引擎 + 策略
│   │   ├── strategy/         #     策略文件 + 策略手册
│   │   │   ├── STRATEGY_MANUAL.md  # 策略编写手册（LLM 可读）
│   │   │   ├── strategy_multi_factor.py  # 多因子均值回归策略
│   │   │   └── strategy_qfq_demo.py     # QFQ 指标使用演示
│   │   └── ...
│   └── data/                 #   数据存储（gitignore）
├── news_manager/             # 新闻采集
│   ├── *_scraper.py          #   爬虫脚本
│   └── data/                 #   新闻存储（gitignore）
├── tushare_tools/            # 基本面计算工具
├── test/                     # 测试
└── readme_ai.md              # AI 可读项目详解
```

## 运行测试

```bash
uv run pytest test/ -v
```
