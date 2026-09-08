# Stock Policy

A 股量化投研工具链，覆盖行情数据、前复权指标、多股回测、新闻采集、AI 预测、到期复盘与历史评估。

## 在线预测展示

> 备用网址：[查看 Stock Policy 在线预测、复盘与历史统计](https://wild-firefox.notion.site/wild-firefox-stock_policy-382899f001bd800f8bf4e2e55a628e7b)

> 本项目仅用于数据研究和工程实验，不构成投资建议。AI 输出、回测收益和历史胜率都不代表未来表现。

## 本次 public 版本相对上一版的主要变化

1. **预测模型与档案目录解耦**
   - 预测默认先通过 Claude CLI / CC Switch 调用 `MODEL_PREDICT_CLI_NAME`。
   - CLI 失败后回退到 `MODEL_PREDICT_API_NAME` 对应的 API。
   - `PREDICT_PROFILE_NAME` 只决定预测、复盘和评估文件的存储目录，可以换模型但继续使用原历史，也可以为新模型建立独立档案。
   - 支持从其他档案仅继承最新复盘知识库。

2. **复盘链路改为可追溯的文件驱动流程**
   - 会逐个回放上次之后未处理的到期交易日，避免长时间未运行后漏复盘。
   - 每个复盘任务的 JSON 结构由代码先生成，模型只填写对应字段。
   - 预测文件记录当时使用的知识库版本，复盘记录 `reference_kb`、`new_kb` 和版本差异。
   - 1日任务分别复盘方向/涨跌幅、逻辑失效价位、盘中验证信号、盈亏比与仓位；3日/5日任务只复盘对应窗口和逻辑失效条件。
   - 会话超限时可调用 compact，仅在确认返回 `compact_boundary` 后才继续。

3. **数据预加载和多标的并发**
   - 股票默认预加载120个交易日前复权日线指标、30个交易日资金流和基本面。
   - ETF 和指数默认预加载120个交易日日线及指标。
   - 股票基本信息、资金流支持主进程批量获取；单股 `stk_factor_pro` 仍独立获取。
   - 外部数据请求支持60秒时间窗重试，不再因一次短暂空数据立即失败。

4. **新闻沙盒和同花顺新页面适配**
   - 支持新版“新闻 / 公告 / 研报”页签。
   - PDF 公告链接直接标记并跳过，不再等待跳转超时。
   - 融资类新闻允许更长的加载时间，表格会输出为 Markdown 表格。
   - 预测与复盘都把新闻 Markdown 和关联图片复制到独立沙盒，任务结束后删除沙盒。

5. **预测评估与 Notion 发布**
   - 生成全局和最近5个预测日的方向胜率、区间胜率和“平算胜/平算负”方向胜率。
   - 根据预测目标日期精确回填1日/3日/5日复盘，同一任务只插入一次。
   - Notion 同步默认只携带最近20个预测日的详细内容，并把正文分段更新，降低 HTTP 413 概率。

6. **新增的辅助工具**
   - 新增单股近8个季度财务指标和自由现金流趋势函数。
   - 新增 Windows 同花顺远航版持仓 OCR/视觉识别工具（可选、实验性）。
   - 提供经过脱敏的 `config.example.py`；本地配置和生成脚本不进入公开分支。

---

## 1. 安装

### 1.1 获取 public 分支

```bash
git clone --branch public https://github.com/wild-firefox/stock_policy.git
cd stock_policy
```

### 1.2 安装 uv 和项目依赖

```powershell
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
uv sync
uv run playwright install chromium
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run playwright install chromium
```

项目使用 Python 3.12。`position_manager.py` 依赖 Windows GUI、同花顺远航版和 DirectML，其他数据、新闻、预测与回测模块不依赖该 GUI。

## 2. 配置与隐私

### 2.1 创建本地配置

Windows：

```powershell
Copy-Item config.example.py config.py
```

macOS / Linux：

```bash
cp config.example.py config.py
```

然后编辑 `config.py`，或使用环境变量覆盖默认值。

| 环境变量 | 用途 | 必需性 |
| --- | --- | --- |
| `TUSHARE_TOKEN` | A股、ETF、指数、财务和资金流数据 | 核心数据功能必需 |
| `DEEPSEEK_API_KEY` | 预测 CLI 失败后的 API 回退、文本解析 | 可选但建议 |
| `BOCHA_API_KEY` | 博查 AI 新闻搜索 | 可选 |
| `TICKFLOW_API_KEY` | 实时行情 | 可选 |
| `NOTION_TOKEN` / `NOTION_PAGE_ID` | 评估报告同步到 Notion | 可选 |
| `AIHUBMIX_API_KEY` | 持仓截图视觉识别 | 可选，Windows |
| `THS_WINDOW_TITLE` | 本地券商交易窗口标题 | 可选，Windows |
| `PROXY_URL` | Notion 等外部请求代理 | 可选 |

`config.py`、预测档案、行情数据、新闻数据、持仓 JSON 和回测日志均已在 `.gitignore` 中排除。不要把真实密钥填回 `config.example.py`。

## 3. 数据下载与前复权处理

```bash
# 导出标的列表
uv run python export_all_stocks.py
uv run python export_etfs.py

# 下载日线与指标
uv run python data_manager/download_stk.py
uv run python data_manager/download_etf.py
uv run python data_manager/download_idx.py

# 重算前复权指标并构建 HDF5 回测数据
uv run python data_manager/recalc_qfq.py
uv run python data_manager/scripts/build_hdf5.py
uv run python data_manager/scripts/build_etf_hdf5.py
```

数据默认存放在：

```text
data_manager/data/
├── stk/raw/       # 单股 CSV
├── stk/raw_h5/    # 股票 HDF5
├── etf/raw/       # ETF CSV
├── etf/raw_h5/    # ETF HDF5
└── idx/raw/       # 指数 CSV
```

### 高频使用的 Tushare 函数

`tushare_tools/high_freq_data.py` 提供：

- `get_daily_qfq_with_indicators(...)`：单股前复权日线及指标，默认120个交易日。
- `get_money_flow_data(...)`：单股资金流，默认30个交易日。
- `get_etf_daily_with_indicators(...)`：ETF 原价日线及指标。
- `get_idx_daily_with_indicators(...)`：指数日线及指标。
- `get_stock_basic_info(...)`：股票基本信息。

`start_date` 优先；如果 `start_date=None`，则按 `trade_days` 截取最近交易日。

`tushare_tools/tools.py` 额外提供：

- `get_stock_financial_trend(ts_code, periods=8)`
- `get_stock_fcf_trend(ts_code, periods=8)`

## 4. 新闻采集

```bash
# 韭研公社盘前纪要
uv run python news_manager/jiuyangongshe_scraper.py --latest
uv run python news_manager/jiuyangongshe_scraper.py --date 20260730

# 同花顺个股新闻/公告/研报，默认近4个交易日
uv run python news_manager/stocknews_scraper.py list --code 600519
uv run python news_manager/stocknews_scraper.py list --code 600519 --fetch-days 7
uv run python news_manager/stocknews_scraper.py list --code 600519 --no-fetch

# 单篇同花顺文章
uv run python news_manager/stocknews_scraper.py article --code 600519 --url <URL>

# 博查 AI 搜索
uv run python news_manager/bocha_scraper.py --code 600519 --days 3
```

公告 PDF 会记录为跳过，不抓取 PDF 正文。同花顺站内文章的图片会下载到文章图片目录。

## 5. AI 预测与复盘

### 5.1 额外前置条件

1. 安装并配置可在终端运行的 `claude` CLI。
2. 如果通过 CC Switch 或兼容网关转发到其他模型，需保证该网关兼容 Claude Code 实际发送的协议与工具结构。
3. 为 Claude CLI 配置可用的 Tushare MCP/Skill，或确保它可以在项目工作目录中运行本地 Tushare 工具。
4. 根据供应商能力修改 `agent_predict.py` 顶部的模型名称。

```python
MODEL_PREDICT_CLI_NAME = "deepseek-v4-pro"
MODEL_PREDICT_API_NAME = "deepseek-v4-pro"
MODEL_REFLECT_NAME = "deepseek-v4-pro"
MODEL_ASSISTANT_NAME = "deepseek-v4-flash"

# 只决定 predict/<profile>/ 档案目录
PREDICT_PROFILE_NAME = "deepseek-v4-pro"

# 新档案可选继承某个旧档案的最新知识库
INITIAL_KB_PROFILE_NAME = ""
```

### 5.2 运行命令

```bash
# 同时预测股票、ETF和指数
uv run python agent_predict.py --stk 600519 601138 --etf 159915 --idx 000300

# 只复盘已到期的历史预测
uv run python agent_predict.py --reflect-only

# 跳过股票新闻爬虫
uv run python agent_predict.py --stk 600519 --no-news-scraper

# 跳过默认行情/资金流/基本面预加载
uv run python agent_predict.py --stk 600519 --no-preload
```

同一资产类型中的重复代码会在启动 worker 前去重，避免多进程竞争同一输出目录。

### 5.3 默认执行顺序

```text
回放所有未复盘到期日
        ↓
更新复盘知识库与 predict_analysis.json
        ↓
仅在本地刷新 predict_eval_history.md
        ↓
提取同标的前两个交易日1日预测/复盘摘要
        ↓
并发执行本次预测
        ↓
再次更新评估报告，按配置同步 Notion
```

预测模型可在一个标的内复用同一 CLI SessionID 执行最多5轮数据查询，不会每一步都新建会话。

### 5.4 输出结构

```text
predict/<PREDICT_PROFILE_NAME>/
├── <YYYYMMDD>/
│   ├── stk/<code>/<HHMMSS>/
│   ├── etf/<code>/<HHMMSS>/
│   └── idx/<code>/<HHMMSS>/
└── experience/
    ├── predict_eval_history.md
    ├── claude_reflection.md
    ├── predict_ex_<YYYYMMDD>_<HHMMSS>.md
    └── predict_ex_<YYYYMMDD>_<HHMMSS>/
        ├── predict_analysis.json
        ├── predict_analysis.md
        └── tools/
```

新闻沙盒只是任务期间的临时副本，原新闻和图片仍保留在 `news_manager/data/`。

## 6. 历史评估与 Notion

`agent_predict.py` 会自动调用评估器。也可以单独运行：

```bash
uv run python predict_evaluator.py
```

单独运行时，入口当前默认评估 `predict/deepseek-v4-pro`。如果使用了其他 `PREDICT_PROFILE_NAME`，推荐从 `agent_predict.py` 触发，或在调用 `run_evaluation()` 时显式传入档案目录。

未配置 `NOTION_PAGE_ID` 时只生成本地 `predict_eval_history.md`，不会发起网页更新。

## 7. 多股回测

```bash
# 多因子均值回归
uv run python data_manager/backtest/strategy/strategy_multi_factor.py

# 前复权指标口径演示
uv run python data_manager/backtest/strategy/strategy_qfq_demo.py

# 多股均线策略
uv run python data_manager/backtest/run_multi_backtest.py

# 其他示例
uv run python data_manager/backtest/strategy/strategy_ma.py
uv run python data_manager/backtest/strategy/strategy_smallgo.py
uv run python data_manager/backtest/strategy/etf_rotation.py
```

公共回测引擎的主要边界：

- T 日收盘后决策，T+1 开盘成交。
- A 股按100股整手取整。
- 先卖后买，按实际可用现金检查订单。
- 佣金和印花税由策略创建引擎时显式配置。
- 价格派生指标和跨期阈值必须使用统一的前复权口径。

每次回测会生成：

```text
data_manager/backtest/log/<strategy>/<timestamp>/
├── report.md
├── transactions.csv
├── trades.csv
└── return_curve_with_trades.png
```

## 8. Windows 持仓识别与条件交易计划（实验性）

`position_manager.py` 用于识别同花顺远航版持仓页面：

1. 连接 `happ.exe` 并前置交易窗口。
2. RapidOCR 定位资金区和持仓表格。
3. 优先使用视觉模型解析截图。
4. 视觉解析失败时，先调用 Claude CLI 文本模型，再回退 DeepSeek API。
5. 校验“资金余额 + 股票市值 ≈ 总资产”。

`agent_trader_plan.py` 会读取持仓、最新预测和复盘规则，生成条件交易计划 JSON。该模块仍属于实验性工具，运行前请先检查其中的档案目录和模型配置，不要把生成的 JSON 直接用于自动下单。

## 9. 测试

```bash
uv run pytest
```

爬虫的端到端测试会访问真实网站，并可能需要浏览器、网络和对应 API 配置。

## 10. 项目结构

```text
stock_policy/
├── agent_predict.py                 # 预测、复盘和并发调度
├── predict_evaluator.py             # 历史预测评估
├── notion_sync_one.py                # Notion 分段同步
├── position_manager.py               # Windows 持仓 OCR/视觉识别
├── agent_trader_plan.py              # 实验性条件交易计划
├── config.example.py                 # 脱敏配置模板
├── news_manager/                     # 新闻爬虫
├── tushare_tools/                    # 高频数据与财务工具
├── data_manager/
│   ├── download_*.py                 # 行情下载
│   ├── recalc_qfq.py                # 前复权重算
│   └── backtest/                     # 回测引擎和策略
├── private_data/                    # 可选私域文本读取（data/已忽略）
└── test/                            # pytest
```

## 11. 开发时必须遵守的数据边界

- 回测、复盘和预测都必须明确交易日和数据可用时间。
- 不得用预测时点之后的新闻、资金流或行情辅助历史复盘。
- 前复权数据、原始价格和 `pct_chg` 的口径不可混用。
- 历史评估只统计已产生完整实际结果的预测，“暂无结果”不进入胜率分母。
- 任何私有新闻、持仓、预测档案和 API 密钥都不应进入 public 分支。
