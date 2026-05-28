"""
多因子均值回归策略 (v4 Final)
==============================
基于 A 股实证因子研究。A 股市场在短周期呈现强均值回归特征（散户过度反应创造反转溢价）。

因子构成（横截面百分位排名后加权合成）：
- 低换手 (40%): -turnover_rate — 回避散户投机性追涨（最强 alpha 因子）
- 小市值 (30%): -log(circ_mv) — 小盘股溢价（稳健基础收益）
- 短期反转 (20%): -bias1_qfq — 买入超卖、卖出超买（均值回归）
- 质量 (10%): 1/pe_ttm — 盈利收益率（避免价值陷阱）

不使用大盘择时（MA60/MA20 择时因踏空反弹而摧毁收益）。
持仓 15 只等权，卖出缓冲区 25 只，每 5 个交易日调仓。
个股止损：qfq_close < qfq_MA60 * 0.85（使用前复权价格计算）。

横截面打分必须使用 _qfq 列（前复权，各股锚定同一基准日），
不能使用 _bfq 列（后复权，各股锚定各自当前日，基准不同无法横比）。
换手率/市值/PE 等非价格衍生指标不受复权方式影响。
"""
import os
import sys
import numpy as np
import pandas as pd

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(current_dir))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(current_dir))))

from engine_multi import MultiBacktestEngine
from config import STK_RAW_H5_DIR, get_latest_h5_path


# ── 参数 ──
TOP_N = 15              # 持仓数量
BUFFER_N = 25           # 卖出缓冲区（降低换手）
STOP_LOSS_RATIO = 0.85  # qfq_close < qfq_MA60 * 0.85 → 止损
MAX_SINGLE_WEIGHT = 0.12  # 单只股票最大仓位
REBALANCE_EVERY_N = 5   # 调仓间隔（交易日）

# 因子权重（网格搜索最优化结果）
W_SIZE = 0.20
W_LOWVOL = 0.60
W_REVERSAL = 0.10
W_QUALITY = 0.10

# 股票池过滤条件
MIN_CIRC_MV = 3e5       # 30亿（300000万元）最低流通市值
MAX_PE = 200
MIN_PB = 0
MAX_PB = 30


# ── 调仓日程缓存 ──
_rebalance_cache = {}


def get_rebalance_dates(engine):
    """返回需要调仓的日期集合（每 REBALANCE_EVERY_N 个交易日）。"""
    eid = id(engine)
    if eid in _rebalance_cache:
        return _rebalance_cache[eid]
    dates = [d for d in engine.dates if d >= engine.target_start_date]
    rb = {d for i, d in enumerate(dates) if i % REBALANCE_EVERY_N == 0}
    _rebalance_cache[eid] = rb
    return rb


# ── QFQ 止损检查 ──
def check_stop_loss(engine, code, current_date):
    """
    使用前复权 (QFQ) 价格计算止损条件。
    止损因涉及绝对价格阈值，必须使用 QFQ 数据，不能用 today_data 中的 bfq 列。
    """
    qfq_df = engine.get_history_qfq(code, current_date)
    if len(qfq_df) < 60:
        return False

    qfq_close = qfq_df['qfq_close']
    qfq_ma60 = qfq_close.rolling(60, min_periods=1).mean().iloc[-1]
    last_close = qfq_close.iloc[-1]

    if pd.isna(qfq_ma60) or pd.isna(last_close) or qfq_ma60 <= 0:
        return False
    return last_close < qfq_ma60 * STOP_LOSS_RATIO


# ── 股票池过滤 ──
def filter_universe(today_data):
    """基于流动性、估值和可交易性过滤股票池。"""
    return today_data[
        (today_data['circ_mv'] > MIN_CIRC_MV) &
        (today_data['pe_ttm'] > 0) &
        (today_data['pe_ttm'] < MAX_PE) &
        (today_data['pb'] > MIN_PB) &
        (today_data['pb'] < MAX_PB) &
        (today_data['pct_chg'] > -0.095) &  # 非跌停
        (today_data['vol'] > 0) &
        (today_data['turnover_rate'] > 0)
    ]


# ── 因子打分 ──
def compute_score(valid):
    """
    计算每只股票的综合因子得分。
    横截面排名必须使用 _qfq 列（各股统一锚定最新日复权因子），
    turnover_rate / circ_mv / pe_ttm 等非价格衍生指标不受复权方式影响。
    """
    score = pd.Series(0.0, index=valid.index)

    # 小市值：流通市值越小得分越高（非价格衍生，不受复权影响）
    score += (-np.log(valid['circ_mv'].replace(0, np.nan))).rank(pct=True) * W_SIZE

    # 低换手：换手率越低越不投机（非价格衍生，不受复权影响）
    score += (-valid['turnover_rate']).rank(pct=True) * W_LOWVOL

    # 短期反转：超卖（负 bias1_qfq）得分高（价格衍生，必须用 qfq）
    score += (-valid['bias1_qfq']).rank(pct=True) * W_REVERSAL

    # 质量：盈利收益率越高越便宜（非价格衍生，不受复权影响）
    score += (1.0 / valid['pe_ttm'].replace(0, np.nan)).rank(pct=True) * W_QUALITY

    return score


# ── 主策略函数 ──
def strategy_multi_factor(engine, current_date, today_data, current_positions):
    orders = []

    if today_data.empty:
        return orders

    # 止损检查（每日执行，使用 QFQ 前复权数据）
    for code in list(current_positions.keys()):
        if check_stop_loss(engine, code, current_date):
            orders.append({'code': code, 'action': 'sell'})

    # 调仓日程
    if current_date not in get_rebalance_dates(engine):
        return orders

    # 股票池过滤
    valid = filter_universe(today_data)
    if valid.empty:
        for code in current_positions:
            if code not in {o['code'] for o in orders}:
                orders.append({'code': code, 'action': 'sell'})
        return orders

    # 因子打分与排名
    valid = valid.copy()
    valid['score'] = compute_score(valid)
    valid = valid.sort_values('score', ascending=False)

    top_buy = set(valid.head(TOP_N).index)
    top_buffer = set(valid.head(BUFFER_N).index)
    stopped = {o['code'] for o in orders if o['action'] == 'sell'}

    # 卖出：不在缓冲区的持仓
    for code in list(current_positions.keys()):
        if code in stopped:
            continue
        if code not in top_buffer:
            orders.append({'code': code, 'action': 'sell'})

    # 买入：填充空位
    planned_sells = {o['code'] for o in orders if o['action'] == 'sell'}
    keep_count = len([c for c in current_positions
                      if c in top_buy and c not in planned_sells])

    slots = TOP_N - keep_count
    if slots <= 0:
        return orders

    candidates = [c for c in top_buy
                  if c not in current_positions and c not in planned_sells]
    weight = min(1.0 / max(slots, 1), MAX_SINGLE_WEIGHT)

    for code in candidates:
        if slots <= 0:
            break
        if code not in today_data.index:
            continue
        orders.append({'code': code, 'action': 'buy', 'weight': weight})
        slots -= 1

    return orders


# ── 入口 ──
def main():
    hdf_path = get_latest_h5_path(STK_RAW_H5_DIR)
    if not os.path.exists(hdf_path):
        print(f"[X] 数据文件未找到: {hdf_path}")
        return

    print(f"[*] 数据: {hdf_path}")
    print("[*] 回测区间: 20200101 ~ 20250630")

    engine = MultiBacktestEngine(
        data_path=hdf_path,
        start_date="20200101",
        end_date="20250630",
        initial_capital=100000.0,
        commission=0.0002,
        tax=0.0005,
        warmup_days=260,
    )

    result = engine.run(strategy_multi_factor)
    engine.print_report(result)
    engine.save_report(result, strategy_name="multi_factor")

    s = result['stats']
    excess = s['total_return'] - s['bm_total_return']
    print(f"\n[*] 超额收益（相对基准）: {excess:.2%}")


if __name__ == '__main__':
    main()
