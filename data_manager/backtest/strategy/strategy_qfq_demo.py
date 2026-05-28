"""
QFQ 指标使用演示策略
=====================
演示如何正确使用引擎的 qfq 计算管线。策略逻辑从简，重点展示 API 用法。

两种 qfq 使用场景：
  1. 横截面排名打分 → 直接用 today_data 中的 _qfq 列（已预计算，高效）
  2. 个股深度分析 → engine.get_history_qfq() + engine.calc_indicators()
     - 计算非标准参数指标（如 MA_15、周线 MACD）
     - 止损/涨跌停等涉及绝对价格阈值的判断

非价格衍生指标（换手率、市值、PE）不受复权方式影响，原名使用。
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
TOP_N = 15
BUFFER_N = 25
REBALANCE_EVERY_N = 5
STOP_LOSS_RATIO = 0.85
MAX_SINGLE_WEIGHT = 0.12

MIN_CIRC_MV = 3e5
MAX_PE = 200
MIN_PB = 0
MAX_PB = 30

_rebalance_cache = {}


def get_rebalance_dates(engine):
    eid = id(engine)
    if eid in _rebalance_cache:
        return _rebalance_cache[eid]
    dates = [d for d in engine.dates if d >= engine.target_start_date]
    rb = {d for i, d in enumerate(dates) if i % REBALANCE_EVERY_N == 0}
    _rebalance_cache[eid] = rb
    return rb


# ═══════════════════════════════════════════════════════════════
# 场景 A: 横截面排名 — 直接使用 today_data 的 _qfq 列
# ═══════════════════════════════════════════════════════════════

def cross_sectional_score(today_data):
    """
    多因子横截面打分。
    qfq 列已预计算在 today_data 中，直接用于百分位排名。
    换手率/市值/PE 等非价格衍生指标不受复权方式影响，原名使用。
    """
    valid = today_data[
        (today_data['circ_mv'] > MIN_CIRC_MV) &
        (today_data['pe_ttm'] > 0) &
        (today_data['pe_ttm'] < MAX_PE) &
        (today_data['pb'] > MIN_PB) &
        (today_data['pb'] < MAX_PB) &
        (today_data['pct_chg'] > -0.095) &
        (today_data['vol'] > 0) &
        (today_data['turnover_rate'] > 0)
    ].copy()

    if valid.empty:
        return valid

    score = pd.Series(0.0, index=valid.index)

    # 低换手 (50%): 非价格衍生指标，原名使用
    score += (-valid['turnover_rate']).rank(pct=True) * 0.50
    # 小市值 (20%): 非价格衍生指标
    score += (-np.log(valid['circ_mv'].replace(0, np.nan))).rank(pct=True) * 0.20
    # 短期反转 (20%): 价格衍生 → 必须用 _qfq 列
    score += (-valid['bias1_qfq']).rank(pct=True) * 0.20
    # 低 RSI (10%): invariant 指标（bfq=qfq），但统一用 _qfq 列
    score += (-valid['rsi_qfq_6']).rank(pct=True) * 0.10

    valid['score'] = score
    return valid.sort_values('score', ascending=False)


# ═══════════════════════════════════════════════════════════════
# 场景 B: 个股深度分析 — get_history_qfq + calc_indicators
# ═══════════════════════════════════════════════════════════════

def check_stop_loss_qfq(engine, code, current_date):
    """
    [场景 B-1] 止损判断：涉及绝对价格阈值，必须用 QFQ 数据。
    工作流：
      1. get_history_qfq() → 获取单只股票的前复权价格历史
      2. 手动计算 qfq_MA60，判断 close < MA60 * 0.85
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


def check_weekly_trend(engine, code, current_date):
    """
    [场景 B-2] 周线趋势确认：用 calc_indicators 计算非标准频率指标。
    工作流：
      1. get_history_qfq() → 获取 qfq 价格历史（含 bfq 列加速日线计算）
      2. calc_indicators(freq='week', indicators=['macd']) → 周线 MACD
      3. 判断周线 MACD 方向（dif > dea → 上升趋势）

    这个指标不在 today_data 中（today_data 只有日线预计算指标），
    需要按需计算。
    """
    qfq_df = engine.get_history_qfq(code, current_date)
    if len(qfq_df) < 26 * 5:  # 至少 5 个月数据用于周线
        return False

    weekly = engine.calc_indicators(qfq_df, freq='week', indicators=['macd'])
    if len(weekly) < 2:
        return False

    last = weekly.iloc[-1]
    prev = weekly.iloc[-2]

    if pd.isna(last['macd_dif_qfq']) or pd.isna(last['macd_dea_qfq']):
        return False

    # 周线 MACD 金叉且 dif > 0 → 中期上升趋势
    return (last['macd_dif_qfq'] > last['macd_dea_qfq'] and
            prev['macd_dif_qfq'] <= prev['macd_dea_qfq'] and
            last['macd_dif_qfq'] > 0)


def check_volume_surge(engine, code, current_date):
    """
    [场景 B-3] 非标准参数日线指标：用 calc_indicators 自定义 MA 周期。
    工作流：
      1. get_history_qfq() → 获取 qfq 价格历史
      2. calc_indicators(freq='day', indicators=['ma_15']) → MA_15
         (today_data 只有标准 MA_5/10/20/30/60/90/250，没有 MA_15)
      3. 判断成交量是否放大（当日 vol > MA_15 * 1.5）

    注意：日线走 apply_qfq_indicators() 快路径（bfq 复制+公式兜底），
    性能远优于周线（全部公式计算）。
    """
    qfq_df = engine.get_history_qfq(code, current_date)
    if len(qfq_df) < 20:
        return False

    # 日线 + 非标准参数 → bfq 快路径加速
    daily = engine.calc_indicators(qfq_df, freq='day', indicators=['ma_15'])
    if daily.empty:
        return False

    last = daily.iloc[-1]
    if 'vol' not in daily.columns or pd.isna(last['vol']):
        return False

    ma15_vol = daily['vol'].rolling(15, min_periods=1).mean().iloc[-1]
    if pd.isna(ma15_vol) or ma15_vol <= 0:
        return False

    return last['vol'] > ma15_vol * 1.5


# ═══════════════════════════════════════════════════════════════
# 主策略
# ═══════════════════════════════════════════════════════════════

def strategy_qfq_demo(engine, current_date, today_data, current_positions):
    orders = []

    if today_data.empty:
        return orders

    # 止损检查（场景 B-1：每只持仓股调用 get_history_qfq）
    for code in list(current_positions.keys()):
        if check_stop_loss_qfq(engine, code, current_date):
            orders.append({'code': code, 'action': 'sell'})

    # 调仓日程
    if current_date not in get_rebalance_dates(engine):
        return orders

    # 横截面排名（场景 A：直接用 today_data 的 _qfq 列，高效批量）
    ranked = cross_sectional_score(today_data)
    if ranked.empty:
        for code in current_positions:
            if code not in {o['code'] for o in orders}:
                orders.append({'code': code, 'action': 'sell'})
        return orders

    top_buy = set(ranked.head(TOP_N).index)
    top_buffer = set(ranked.head(BUFFER_N).index)
    stopped = {o['code'] for o in orders if o['action'] == 'sell'}

    # 卖出
    for code in list(current_positions.keys()):
        if code in stopped:
            continue
        if code not in top_buffer:
            orders.append({'code': code, 'action': 'sell'})

    # 买入（可选叠加周线趋势过滤，场景 B-2）
    planned_sells = {o['code'] for o in orders if o['action'] == 'sell'}
    keep_count = len([c for c in current_positions
                      if c in top_buy and c not in planned_sells])
    slots = TOP_N - keep_count

    if slots <= 0:
        return orders

    candidates = [c for c in ranked.index
                  if c not in current_positions and c not in planned_sells]
    weight = min(1.0 / max(slots, 1), MAX_SINGLE_WEIGHT)

    for code in candidates:
        if slots <= 0:
            break
        if code not in today_data.index:
            continue

        # 可选：周线趋势确认（场景 B-2，默认关闭以避免过度过滤）
        # if not check_weekly_trend(engine, code, current_date):
        #     continue

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

    result = engine.run(strategy_qfq_demo)
    engine.print_report(result)
    engine.save_report(result, strategy_name="qfq_demo")

    s = result['stats']
    excess = s['total_return'] - s['bm_total_return']
    print(f"\n[*] 超额收益（相对基准）: {excess:.2%}")


if __name__ == '__main__':
    main()
