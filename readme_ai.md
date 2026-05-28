# Stock Policy — AI 可读项目说明

## 项目定位

A 股量化投研工具链：**数据采集 → 存储 → 技术指标计算 → 多股回测 → 新闻采集**。

## 目录结构

```
stock_policy/
├── config.py                     # 全局配置（路径/API Key/回测列定义/板块过滤）
├── pyproject.toml                # uv 项目定义 + pytest 配置
├── a_shares_list.csv             # 全量 A 股列表（含行业/概念/PE/PB/FCF）
├── etf_list.csv                  # 场内 ETF 列表
│
├── data_manager/                 # [模块A] 行情数据 + 回测引擎
│   ├── download_stk.py           #   下载全量 A 股日线（Tushare stk_factor_pro）
│   ├── download_etf.py           #   下载全量 ETF 日线
│   ├── download_idx.py           #   下载指数日线
│   ├── recalc_qfq.py             #   前复权因子重算（增量合并时缩放 qfq 列）
│   ├── scripts/
│   │   ├── build_hdf5.py         #   股票 CSV → HDF5 合并（过滤创业板/科创板/北交所，float32）
│   │   └── build_etf_hdf5.py     #   ETF CSV → HDF5 合并
│   ├── backtest/
│   │   ├── engine_multi.py       #   多股回测引擎核心类 MultiBacktestEngine
│   │   ├── run_multi_backtest.py #   引擎使用示例（均线金叉/死叉策略）
│   │   ├── custom_indicators.py  #   技术指标计算（KDJ/MACD/RSI/布林带/CCI/ATR等）
│   │   └── strategy/             #   独立策略文件
│   │       ├── STRATEGY_MANUAL.md #    策略编写手册（引擎 API、数据列、QFQ 管线、常见陷阱）
│   │       ├── strategy_ma.py    #     单股均线策略
│   │       ├── strategy_smallgo.py #   小市值轮动策略
│   │       ├── strategy_multi_factor.py  # 多因子均值回归策略 (v4，超额 +127%)
│   │       ├── strategy_qfq_demo.py     # QFQ 指标使用演示（get_history_qfq + calc_indicators）
│   │       └── etf_rotation.py   #     ETF RSRS+动量轮动策略
│   └── data/                     #   行情数据存储（gitignore）
│       ├── stk/raw/              #     每只 A 股一个 CSV
│       ├── stk/raw_h5/           #     HDF5 包，供回测引擎加载
│       ├── etf/raw/              #     每只 ETF 一个 CSV
│       ├── etf/raw_h5/           #     ETF HDF5 包
│       └── idx/raw/              #     指数 CSV
│
├── news_manager/                 # [模块B] 新闻采集
│   ├── jiuyangongshe_scraper.py  #   韭研公社盘前纪要（Playwright + Nuxt SSR 提取）
│   ├── stocknews_scraper.py      #   同花顺个股新闻（列表+文章+图片）
│   ├── bocha_scraper.py          #   博查AI搜索 + 全文抓取 + 反爬 stealth
│   ├── news_bocha.py             #   旧版博查（仅摘要，保留兼容）
│   └── data/                     #   新闻数据存储（gitignore）
│       ├── jygs/pqjy/<YYYYMMDD>/ #     韭研公社文章 + images/
│       ├── stk/raw_ths/<code>/   #     同花顺新闻列表 + articles/
│       └── stk/raw_Bocha/<code>/ #     博查搜索结果 + articles/
│
├── tushare_tools/                # [模块C] 基本面工具
│   └── tools.py                  #   流动比率/自由现金流计算
│
├── test/                         # 顶层测试
│   ├── test_jiuyangongshe_scraper.py
│   ├── test_stocknews_scraper.py
│   └── test_bocha_scraper.py
│
├── CLAUDE.md                     # Claude Code agent 行为指令
├── .claude/memory/               # Harness 闭环经验记录
│   ├── MEMORY.md                 #   索引
│   └── gotchas.md                #   经验教训
└── .gitignore                    # 排除 data/、uv.lock、.venv 等
```

## 数据流

### 行情 → 回测

```
Tushare API
  │
  ├─→ download_stk.py ─→ stk/raw/*.csv ─→ recalc_qfq.py ─→ build_hdf5.py ─→ stk/raw_h5/*.h5 ─┐
  ├─→ download_etf.py ─→ etf/raw/*.csv ────────────────────→ build_etf_hdf5.py ─→ etf/raw_h5/*.h5 ─┤
  └─→ download_idx.py ─→ idx/raw/*.csv ────────────────────────────────────────────────────────────┤
                                                                                                    │
                                                                                                    v
                                                                           MultiBacktestEngine.run()
                                                                             │
                                                                             ├─ 加载 HDF5 或 CSV 目录
                                                                             ├─ 用 pandas_market_calendars(SSE) 计算预热日
                                                                             ├─ 每日循环: 执行挂单(先卖后买,T+1开盘价) → 盯市 → 生成信号
                                                                             ├─ 整手(100股)取整 + 手续费校验(双边佣金)
                                                                             └─ 输出: report.md / transactions.csv / trades.csv / 收益曲线.png
```

### 新闻采集

```
韭研公社                        同花顺 10jqka                    博查 AI
  │                                │                                │
  │ --latest / --date              │ list --code <code>             │ --code <code> --days N
  │ --url <URL>                    │ article --url <URL>            │
  v                                v                                v
Nuxt SSR 状态提取               h2 垂直位置分区                  REST API 搜索
→ 文章内容 + 图片               → 新闻列表 + 自动抓取文章       → 多 query + 去重
→ Markdown + images/            → Markdown + images/             → 客户端相关度过滤
                                                                → Playwright stealth 全文抓取
                                                                → Markdown + images/
```

## 各模块关键文件说明

### config.py — 全局配置入口

- `TUSHARE_TOKEN` / `BOCHA_API_KEY`：环境变量读取，有硬编码备用值
- `PROJECT_ROOT`：脚本所在目录的绝对路径
- 路径常量：`STK_RAW_DIR`, `STK_RAW_H5_DIR`, `ETF_RAW_DIR`, `ETF_RAW_H5_DIR`, `IDX_RAW_DIR`, `NEWS_RAW_DIR`, `NEWS_BOCHA_DIR`, `NEWS_JYGS_DIR`, `LOG_DIR`
- `SAVE_COLS`：下载时保留的列（~200+ 技术因子列）
- `BACKTEST_BASE`：HDF5 构建时的列过滤
- `is_excluded(ts_code)`：过滤创业板(30xxxx)、科创板(688xxx)、北交所(8xxxxx)
- `get_pro()`：Tushare API 连接工厂
- `setup_logger(name)`：按模块隔离的日志记录器

### download_stk.py / download_etf.py / download_idx.py

全量下载器，共同特征：
- 增量更新：检查已有 CSV 的最新日期，只下载缺失部分
- 频率限制：`API_SLEEP=0.3s`，触发限流后 `RATE_LIMIT_SLEEP=30s`，最多 `MAX_RETRY=3` 次
- 输出 CSV 到对应的 `raw/` 目录

### build_hdf5.py / build_etf_hdf5.py

CSV → HDF5 合并脚本：
- 按日期范围分批（避免单文件过大）
- float32 降维节省内存
- MultiIndex: `(trade_date, ts_code)`
- 写入模式: `table` 格式，`append` 追加

### engine_multi.py — 回测引擎核心

类 `MultiBacktestEngine`：
- **初始化**：加载 HDF5 或 CSV 目录，用 `pandas_market_calendars` 获取 SSE 交易日历
- **预热**：在 start_date 前回退 warmup_days 个交易日以计算初始指标
- **每日循环**：
  1. 执行前一日挂单（先卖后买，T+1 开盘价成交）
  2. 手续费校验（双边佣金），整手(100股)取整
  3. 盯市计算当日净值
  4. 调用策略函数生成新信号
- **QFQ 计算管线**：
  - `get_history_qfq(code, date)` — 获取单只股票前复权价格历史（入口）
  - `calc_indicators(data, freq, indicators)` — 在 qfq 数据上计算技术指标
  - 日线走 bfq 快路径（KDJ/RSI 等不变指标直接复制），周线全部公式计算
  - 策略编写参考 `STRATEGY_MANUAL.md` 的"QFQ 指标计算管线"章节
- **输出**：`report.md`（统计表格）、`transactions.csv`（逐笔流水）、`trades.csv`（平仓记录）、`return_curve_with_trades.png`（收益曲线+交易量图）
- **精度验证**：各策略注释中标注了与 JoinQuant 的对比结果（误差 < 0.1%）

### custom_indicators.py

动态指标计算，供回测引擎 `calc_indicators()` 调用：
- KDJ、MACD、RSI、布林带(BOLL)、CCI、唐奇安通道(Donchian)、薛斯通道(XS)、ATR

### 三个爬虫的对比

| 特性 | jiuyangongshe | stocknews | bocha |
|------|:---:|:---:|:---:|
| 数据源 | 韭研公社作者页 | 同花顺个股页 | 博查 AI API |
| 发现方式 | `--latest` / `--date` / `--url` | `list --code <code>` | `--code <code> --days N` |
| 内容提取 | Nuxt SSR 状态 + DOM 回退 | h2 垂直分区 + 通用提取器 | REST 搜索 + Playwright 全文抓取 |
| 反爬 | 无 | 无 | Stealth UA + webdriver 屏蔽 + 资源拦截 + 随机延迟 |
| 输出 | 单篇文章 markdown | 新闻列表 + 每篇文章独立 markdown | 搜索列表 + 每篇文章独立 markdown |
| 文章增量 | 每次覆盖 | `articles/<MM-DD>_<title>.md` 不覆盖 | `articles/<MM-DD>_<title>.md` 不覆盖 |

## CLI 命令参考

### 行情数据

```bash
uv run python data_manager/download_stk.py       # 下载全量 A 股日线
uv run python data_manager/download_etf.py       # 下载全量 ETF 日线
uv run python data_manager/download_idx.py       # 下载指数日线
uv run python data_manager/recalc_qfq.py          # 重算前复权
uv run python data_manager/scripts/build_hdf5.py  # 构建股票 HDF5
uv run python data_manager/scripts/build_etf_hdf5.py  # 构建 ETF HDF5
```

### 回测

```bash
uv run python data_manager/backtest/strategy/strategy_multi_factor.py  # 多因子均值回归（推荐）
uv run python data_manager/backtest/strategy/strategy_qfq_demo.py      # QFQ 指标使用演示
uv run python data_manager/backtest/run_multi_backtest.py       # 多股均线策略
uv run python data_manager/backtest/strategy/strategy_ma.py     # 单股均线策略
uv run python data_manager/backtest/strategy/strategy_smallgo.py # 小市值轮动
uv run python data_manager/backtest/strategy/etf_rotation.py    # ETF 轮动
```

### 新闻采集

```bash
# 韭研公社盘前纪要
uv run python news_manager/jiuyangongshe_scraper.py --latest
uv run python news_manager/jiuyangongshe_scraper.py --date 20260526
uv run python news_manager/jiuyangongshe_scraper.py --url https://www.jiuyangongshe.com/a/xxx

# 同花顺个股新闻
uv run python news_manager/stocknews_scraper.py list --code 600410
uv run python news_manager/stocknews_scraper.py list --code 600410 --no-fetch
uv run python news_manager/stocknews_scraper.py list --code 600410 --fetch-days 5
uv run python news_manager/stocknews_scraper.py article --url <URL> --code 600410

# 博查 AI 搜索 + 全文抓取
uv run python news_manager/bocha_scraper.py --code 600410
uv run python news_manager/bocha_scraper.py --code 600410 --days 5
uv run python news_manager/bocha_scraper.py --code 600410 --no-fetch
```

## 外部依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `pandas` | >=3.0.2 | 数据处理、CSV/HDF5 读写、回测计算 |
| `playwright` | >=1.58.0 | 无头浏览器爬虫 |
| `requests` | >=2.33.1 | HTTP API 调用（博查搜索） |
| `beautifulsoup4` | >=4.14.3 | HTML 解析（部分场景） |
| `tushare` | (未在 pyproject.toml) | A 股行情数据 API |
| `pandas-market-calendars` | (未在 pyproject.toml) | SSE 交易日历 |

API 密钥：
- `TUSHARE_TOKEN`：环境变量或 config.py 硬编码
- `BOCHA_API_KEY`：环境变量或 config.py 硬编码

## 开发约束

这些约束来自 `CLAUDE.md`，AI agent 在处理本项目时必须遵守：

1. **环境**：`uv add <包>` 安装依赖，`uv run <命令>` 执行一切
2. **TDD**：所有纯逻辑函数必须有 pytest；爬虫必须有端到端测试
3. **Harness 闭环**：测试失败 → 先写 `.claude/memory/gotchas.md` 记录根因 → 再修复代码
4. **Windows GBK**：`print()` 中禁用 emoji，用 `[+]` `[*]` `[!]` `[X]` 等 ASCII 标记
5. **Git**：提交格式 `[Harness] <描述>`，作者 `Claude Code <claude.bot@localhost>`
6. **测试路径**：`test/` 目录，pytest 配置 `pythonpath = ["."]`
