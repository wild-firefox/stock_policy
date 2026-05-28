# 策略编写手册（供大模型参考）

在 MultiBacktestEngine 上编写日频 A 股策略的简明参考。

## 引擎 API

### 构造参数

```python
MultiBacktestEngine(
    data_path,          # str: HDF5 文件路径或 CSV 目录
    start_date,         # str: "YYYYMMDD"
    end_date,           # str: "YYYYMMDD"
    initial_capital=100000.0,
    commission=0.0002,  # 万二
    tax=0.0005,         # 万五（仅卖出）
    exclude_boards=['gem','star','bj'],  # 或 None 表示全市场
    warmup_days=260,    # 在 start_date 前预加载 N 个交易日
    benchmark='000300', # 基准指数代码（沪深300）
    is_etf=False,
)
```

### 策略函数签名

```python
def strategy(engine, current_date, today_data, current_positions) -> list[dict]:
```

| 参数 | 类型 | 说明 |
|-----------|------|-------------|
| `engine` | MultiBacktestEngine | 可访问 `.benchmark_df`、`.get_history_qfq()`、`.dates` |
| `current_date` | str | "YYYYMMDD"，当天收盘日期（收盘后决策） |
| `today_data` | DataFrame | 按 ts_code 索引，约 160 列（全部 BACKTEST_BASE 字段，含 bfq 和 qfq 指标） |
| `current_positions` | dict | `{ts_code: shares}` — 当前持仓 |

### 订单格式

```python
# 全部卖出某只股票
{'code': '000001.SZ', 'action': 'sell'}

# 卖出指定股数
{'code': '000001.SZ', 'action': 'sell', 'shares': 500}

# 按权重买入（总资产百分比）
{'code': '000002.SZ', 'action': 'buy', 'weight': 0.2}

# 买入精确股数（必须为 100 的整数倍）
{'code': '000002.SZ', 'action': 'buy', 'shares': 1000}
```

### T+1 执行模型

1. T 日收盘：`strategy()` 基于 T 日收盘数据运行
2. T+1 日开盘：订单以开盘价执行
3. 卖出订单优先执行（释放资金），再执行买入订单
4. 未成交订单（停牌/跌停）顺延至下一日

**无未来函数泄露**：策略仅看到 T 日数据。订单在 T+1 开盘成交。

## `today_data` 中的可用数据列

`today_data` 是按 ts_code 索引的 DataFrame，包含以下全部列：

### 价格与成交量（日频）
`open`, `high`, `low`, `close`, `change`, `pct_chg`, `vol`, `amount`

### 估值与基本面
`pe`, `pe_ttm`, `pb`, `ps`, `ps_ttm`, `dv_ratio`, `dv_ttm`

### 市值与股本
`total_share`, `float_share`, `free_share`, `total_mv`, `circ_mv`

### 流动性
`turnover_rate`, `turnover_rate_f`, `volume_ratio`

### 其他
`adj_factor`, `updays`, `downdays`, `topdays`, `lowdays`

### 技术指标（qfq — 前复权，用于横截面排名打分）

**横截面打分（选股排名）必须使用 qfq 列**。qfq（前复权）以统一的最新日为锚点调整所有股票价格，各股指标在同一基准下可横比。

**趋势类 (MA/EMA)**: `ma_qfq_5`, `ma_qfq_10`, `ma_qfq_20`, `ma_qfq_30`, `ma_qfq_60`, `ma_qfq_90`, `ma_qfq_250`, `ema_qfq_5` .. `ema_qfq_250`（相同周期）

**MACD**: `macd_dif_qfq`, `macd_dea_qfq`, `macd_qfq`

**KDJ**: `kdj_k_qfq`, `kdj_d_qfq`, `kdj_qfq`

**RSI**: `rsi_qfq_6`, `rsi_qfq_12`, `rsi_qfq_24`

**布林带**: `boll_upper_qfq`, `boll_mid_qfq`, `boll_lower_qfq`

**单值指标**: `cci_qfq`, `atr_qfq`, `bbi_qfq`, `bias1_qfq`, `bias2_qfq`, `bias3_qfq`, `cr_qfq`, `wr_qfq`, `wr1_qfq`, `obv_qfq`, `mfi_qfq`, `vr_qfq`, `psy_qfq`, `psyma_qfq`

**多输出指标**: `asi_qfq`/`asit_qfq`, `brar_ar_qfq`/`brar_br_qfq`, `dmi_adx_qfq`/`dmi_adxr_qfq`/`dmi_mdi_qfq`/`dmi_pdi_qfq`, `dfma_dif_qfq`/`dfma_difma_qfq`, `dpo_qfq`/`madpo_qfq`, `emv_qfq`/`maemv_qfq`, `expma_12_qfq`/`expma_50_qfq`, `ktn_upper_qfq`/`ktn_mid_qfq`/`ktn_down_qfq`, `mass_qfq`/`ma_mass_qfq`, `mtm_qfq`/`mtmma_qfq`, `roc_qfq`/`maroc_qfq`, `taq_up_qfq`/`taq_mid_qfq`/`taq_down_qfq`, `trix_qfq`/`trma_qfq`, `xsii_td1_qfq`~`xsii_td4_qfq`

### 技术指标（bfq — 不复权，仅限单只股票时间序列分析）

**bfq 列严禁用于横截面对比（排名、打分、选股）**。bfq（不复权）以各股自身当前日为锚点做价格调整，每只股票的复权基准不同，因此 bfq 指标值在横截面上不可直接比较。bfq 唯一合法用途：单只股票的时间序列分析（如判断个股自身趋势转向）。

**趋势类 (MA/EMA)**: `ma_bfq_5`, `ma_bfq_10`, `ma_bfq_20`, `ma_bfq_30`, `ma_bfq_60`, `ma_bfq_90`, `ma_bfq_250`, `ema_bfq_5` .. `ema_bfq_250`（相同周期）

**MACD**: `macd_dif_bfq`, `macd_dea_bfq`, `macd_bfq`

**KDJ**: `kdj_k_bfq`, `kdj_d_bfq`, `kdj_bfq`

**RSI**: `rsi_bfq_6`, `rsi_bfq_12`, `rsi_bfq_24`

**布林带**: `boll_upper_bfq`, `boll_mid_bfq`, `boll_lower_bfq`

**单值指标**: `cci_bfq`, `atr_bfq`, `bbi_bfq`, `bias1_bfq`, `bias2_bfq`, `bias3_bfq`, `cr_bfq`, `wr_bfq`, `wr1_bfq`, `obv_bfq`, `mfi_bfq`, `vr_bfq`, `psy_bfq`, `psyma_bfq`

**多输出指标**: `asi_bfq`/`asit_bfq`, `brar_ar_bfq`/`brar_br_bfq`, `dmi_adx_bfq`/`dmi_adxr_bfq`/`dmi_mdi_bfq`/`dmi_pdi_bfq`, `dfma_dif_bfq`/`dfma_difma_bfq`, `dpo_bfq`/`madpo_bfq`, `emv_bfq`/`maemv_bfq`, `expma_12_bfq`/`expma_50_bfq`, `ktn_upper_bfq`/`ktn_mid_bfq`/`ktn_down_bfq`, `mass_bfq`/`ma_mass_bfq`, `mtm_bfq`/`mtmma_bfq`, `roc_bfq`/`maroc_bfq`, `taq_up_bfq`/`taq_mid_bfq`/`taq_down_bfq`, `trix_bfq`/`trma_bfq`, `xsii_td1_bfq`~`xsii_td4_bfq`

> **要点**: 换手率、市值、PE 等非价格衍生指标不受复权方式影响，原名使用。价格衍生指标（bias1、MA、RSI、MACD 等）的横截面对比必须用 qfq 列。止损/涨跌停等涉及绝对价格阈值的判断必须使用 `engine.get_history_qfq()`。

## ETF 与股票指标可用性

| 类别 | 股票 | ETF |
|----------|-------|-----|
| 公式型指标 (21个) | 全覆盖 | 全覆盖（从 qfq 价格公式计算） |
| 未实现指标 (9个) | bfq 列可用 | 填 NaN |

**9 个未实现（ETF 不可用）**: OBV, ASI, MFI, DMI, BRAR, PSY, VR, MASS, EMV

## 辅助方法

### QFQ 指标计算管线（核心概念）

引擎提供两层 qfq 指标获取方式，策略应根据场景选择：

**场景 A — 横截面排名打分（批量高效）**：直接用 `today_data` 中的 `_qfq` 列。
所有 qfq 指标已预计算在 BACKTEST_BASE 中，`today_data` 直接可用。适合因子横截面对比（百分位排名），一次访问所有股票，无需逐只调 API。

**场景 B — 个股深度分析（按需计算）**：`get_history_qfq()` + `calc_indicators()`。
适合：(1) 计算非标准参数指标（如 MA_15、周线 MACD，不在 today_data 预计算列表中）；(2) 止损/涨跌停等涉及绝对价格阈值的判断；(3) 组合多个指标综合研判单只股票。每只股票一次调用，适合持仓股级别的分析。

非价格衍生指标（换手率、市值、PE）不受复权方式影响，原名使用。

### `engine.get_history_qfq(symbol, anchor_date) -> DataFrame`

获取单只股票以 anchor_date 为锚点的前复权价格历史，是整个 qfq 计算管线的**入口**。

返回的 DataFrame 包含（按 trade_date 升序）：
- `qfq_open`, `qfq_high`, `qfq_low`, `qfq_close` — 前复权 OHLC（close × adj_factor / anchor_adj_factor）
- `qfq_pre_close` — 前复权前收盘价（用于涨跌停判断）
- `_ratio` — 复权比例列（adj_factor / anchor_adj_factor），内部使用，calc_indicators 后自动删除
- 全部 bfq 列（从引擎数据中携带，用于日线指标的 bfq 快路径加速）
- `vol`, `amount`, `trade_date` 等非价格列

```python
# 获取某只股票的 qfq 价格历史
qfq_df = engine.get_history_qfq('000001', '20250630')
# 得到 20+ 年的日线数据，包含 qfq 价格 + bfq 指标 + _ratio
```

### `engine.calc_indicators(data, freq, indicators) -> DataFrame`

在 `get_history_qfq()` 返回的数据上计算技术指标。**必须先用 get_history_qfq 获取数据，再传入此方法。**

| 参数 | 说明 |
|------|------|
| `data` | `get_history_qfq()` 返回的 DataFrame |
| `freq` | `'day'` 日线（推荐）或 `'week'` 周线 |
| `indicators` | 指标名列表，如 `['ma_15', 'macd', 'bias']` |

**日线 (`freq='day'`)**：走 `recalc_qfq.apply_qfq_indicators()` 混合策略，性能高：
- 价格不变型指标（KDJ/RSI/DMI/CR/BRAR/PSY/VR/WR/MASS/EMV/OBV/MFI）：直接从 bfq 列复制（bfq = qfq）
- bfq*ratio 缩放型（ATR/KTN/XSII/ASI/BBI）：bfq 值 × ratio
- 公式重算型（MA/EMA/MACD/BOLL/CCI/BIAS/ROC/TRIX 等）：从 qfq 价格用 rolling/ewm 公式重算

**周线 (`freq='week'`)**：先 resample 到周五，再全部用 `compute_indicators()` 公式计算。无 bfq 快路径。需要 ≥5 个月数据。

**可用的 indicator 名称**（变体参数见 INDICATOR_REGISTRY）：
`ma_N`, `ema_N`, `macd`, `kdj`, `rsi_N`, `boll`, `bias_N`, `cci`, `atr`, `bbi`, `roc`, `wr_N`, `dfma`, `dpo`, `ktn`, `mtm`, `taq`, `xsii`, `expma`, `trix`, `cr`

```python
# 示例 1: 日线 MA_15（非标准周期，today_data 里没有）
data = engine.get_history_qfq('000001', current_date)
daily = engine.calc_indicators(data, freq='day', indicators=['ma_15'])
ma15_val = daily['ma_qfq_15'].iloc[-1]

# 示例 2: 周线 MACD（跨周期分析，today_data 只有日线指标）
weekly = engine.calc_indicators(data, freq='week', indicators=['macd'])
weekly_dif = weekly['macd_dif_qfq'].iloc[-1]

# 示例 3: 同时计算多个指标
daily = engine.calc_indicators(data, freq='day',
    indicators=['ma_15', 'bias', 'rsi_14', 'macd'])
```

### `engine.benchmark_df`

按 trade_date 索引的 DataFrame，含基准 OHLCV 及全部技术指标。用于大盘择时：
```python
bm_close = engine.benchmark_df.loc[current_date, 'close']
```

## 策略模式

### 模式 1：横截面排名（最常用）

```python
def strategy(engine, date, today_data, positions):
    # 1. 过滤股票池
    valid = today_data[
        (today_data['circ_mv'] > 3e5) &  # 30亿最低流通市值
        (today_data['pe_ttm'] > 0) &
        (today_data['vol'] > 0)
    ].copy()

    # 2. 打分（必须用 qfq 列做截面排名）
    valid['score'] = -valid['bias1_qfq']  # 反转因子

    # 3. 选前 N 名
    top = valid.nlargest(10, 'score')

    # 4. 生成订单
    orders = []
    for code in positions:
        if code not in top.index:
            orders.append({'code': code, 'action': 'sell'})

    n_hold = len([c for c in positions if c in top.index])
    for code in top.index:
        if code not in positions:
            w = 1.0 / (10 - n_hold)
            orders.append({'code': code, 'action': 'buy', 'weight': w})

    return orders
```

### 模式 2：大盘择时叠加

```python
def strategy(engine, date, today_data, positions):
    # 熊市：全部清仓
    bm = engine.benchmark_df
    if date in bm.index:
        ma20 = bm['close'].loc[:date].rolling(20).mean().iloc[-1]
        if bm.loc[date, 'close'] < ma20:
            return [{'code': c, 'action': 'sell'} for c in positions]
    # ... 正常选股逻辑 ...
```

### 模式 3：预计算信号（外部数据驱动）

```python
def make_strategy(signals_df):  # signals_df: date × ts_code → weight
    def strategy(engine, date, today_data, positions):
        if date not in signals_df.index:
            return []
        weights = signals_df.loc[date]
        orders = []
        # ... 将 weight 转换为买卖订单 ...
        return orders
    return strategy
```

### 模式 4：QFQ 管线 — 个股深度分析（get_history_qfq + calc_indicators）

```python
def strategy(engine, date, today_data, positions):
    orders = []

    # 场景 B-1: 止损 — 涉及绝对价格阈值，必须用 QFQ 数据
    for code in list(positions.keys()):
        qfq_df = engine.get_history_qfq(code, date)
        if len(qfq_df) >= 60:
            qfq_close = qfq_df['qfq_close']
            qfq_ma60 = qfq_close.rolling(60).mean().iloc[-1]
            if qfq_close.iloc[-1] < qfq_ma60 * 0.85:
                orders.append({'code': code, 'action': 'sell'})

    # 场景 B-2: 周线趋势过滤 — 非标准频率，不在 today_data 中
    def weekly_macd_golden_cross(engine, code, date):
        qfq_df = engine.get_history_qfq(code, date)
        weekly = engine.calc_indicators(qfq_df, freq='week', indicators=['macd'])
        if len(weekly) < 2:
            return False
        last, prev = weekly.iloc[-1], weekly.iloc[-2]
        return (last['macd_dif_qfq'] > last['macd_dea_qfq'] and
                prev['macd_dif_qfq'] <= prev['macd_dea_qfq'])

    # 场景 B-3: 非标准参数日线指标 — MA_15 不在 today_data 预计算列表中
    def ma15_support(engine, code, date):
        qfq_df = engine.get_history_qfq(code, date)
        daily = engine.calc_indicators(qfq_df, freq='day', indicators=['ma_15'])
        if daily.empty:
            return False
        return daily['qfq_close'].iloc[-1] > daily['ma_qfq_15'].iloc[-1]

    # ... 选股 + 叠加过滤 ...
    return orders
```
        return orders
    return strategy
```

## 常见陷阱

1. **ts_code 格式**: 引擎可能使用纯数字（"000001"）或完整代码（"000001.SZ"）。检查 `today_data.index`——如果首个条目无后缀，则需从所有 Tushare 结果中去除 `.SZ`/`.SH`。

2. **横截面打分必须用 QFQ 列**: `today_data` 中的 `_qfq` 列（前复权）以统一的最新日为锚点，各股指标在同一基准下可横比。`_bfq` 列（不复权）以各股自身当前日为锚点，复权基准不同，**严禁用于横截面对比**（排名、打分、选股）。止损/涨跌停等涉及绝对价格阈值的判断必须使用 `engine.get_history_qfq()`。换手率、市值、PE 等非价格衍生指标不受复权方式影响。

3. **涨停/跌停判断**: 必须使用 `engine.get_history_qfq()` 获取准确的 qfq_pre_close 来计算涨跌停价。`today_data` 有 `pct_chg` 但涨跌停价的计算精度不同。

4. **停牌股票**: `vol == 0` 或 `open` 为 NaN → 跳过。也可通过 `pro.suspend_d()` 查询。

5. **ST 股票**: 通过 `pro.stock_st()` 或 `pro.bak_basic()` 过滤。ST 股涨跌幅为 5%。

6. **预热天数**: 设置 `warmup_days >= 260`，确保 MA_250 等长周期指标从首日即有效。

7. **手续费**: `commission=0.0002`（万二），`tax=0.0005`（万五，仅卖出）。此为 A 股实际费率。

8. **板块排除**: 默认排除 300（创业板）、688（科创板）、92/43/8x（北交所）。设 `exclude_boards=None` 包含全部。

9. **A 股因子特征**: A 股散户主导，短周期呈均值回归特征。反转因子（-bias1、低 RSI）和低换手因子产生正 alpha；趋势/动量因子（正 bias1、MA 交叉）产生负 alpha。

## Tushare API 速查

```python
from config import get_pro
pro = get_pro()

# 股票列表
pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry')

# 日频因子（PE、PB、MA、MACD、KDJ、RSI 等）
pro.stk_factor_pro(ts_code='000001.SZ', start_date='20200101', end_date='20251231')

# 指数成分权重
pro.index_weight(index_code='399101.SZ', start_date='20200101', end_date='20200131')

# ST 列表
pro.stock_st(trade_date='20250630', fields='ts_code')

# 停牌股票
pro.suspend_d(trade_date='20250630', suspend_type='S')

# 利润表
pro.income(ts_code='000001.SZ', start_date='20200101', end_date='20251231')

# 资产负债表
pro.balancesheet(ts_code='000001.SZ', start_date='20200101', end_date='20251231')
```

调用 `pro.*` 的函数使用 `@lru_cache(maxsize=128)` 避免重复请求 API。
