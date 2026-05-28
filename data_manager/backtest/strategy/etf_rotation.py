import os
import sys
import numpy as np
import pandas as pd

# 动态添加相对路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(current_dir)) # 添加 backtest 目录 (用于导入 engine_multi)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))) # 添加项目根目录 stock_policy (用于导入 config)
from engine_multi import MultiBacktestEngine
from config import ETF_RAW_H5_DIR, get_latest_h5_path

# ----------------- 策略参数 -----------------
ETF_POOL = [
    '518880',  # 黄金ETF
    '159915',  # 创业板ETF
    '513500',  # 标普500ETF
    '159985',  # 豆粕ETF
]
MOMENTUM_DAY = 25
RSRS_N = 18
RSRS_M = 600
RSRS_THRESHOLD = 0.7
MA_DAY = 20
MA_DIFF = 3
STOP_LOSS_PCT = -0.2

# ----------------- 信号预处理 -----------------
def precalc_benchmark_signal(engine: MultiBacktestEngine):
    """
    向量化预计算沪深300的 RSRS(标准分化) 和 MA 择时信号
    """
    df = engine.benchmark_df.copy()
    
    # 1. 计算 MA 信号
    df['ma_20'] = df['close'].rolling(window=MA_DAY).mean()
    df['ma_20_before'] = df['ma_20'].shift(MA_DIFF)
    df['ma_cond'] = df['ma_20'] > df['ma_20_before']
    
    # 2. 计算 RSRS
    # Slope = Cov(X, Y) / Var(X)  (X=low, Y=high)
    cov_hl = df['high'].rolling(window=RSRS_N).cov(df['low'])
    var_l = df['low'].rolling(window=RSRS_N).var()
    df['slope'] = cov_hl / var_l
    
    # R2 = (Corr(X, Y))^2
    df['r2'] = df['high'].rolling(window=RSRS_N).corr(df['low']) ** 2
    
    # z-score
    slope_mean = df['slope'].rolling(window=RSRS_M).mean()
    slope_std = df['slope'].rolling(window=RSRS_M).std(ddof=0) 
    df['zscore'] = (df['slope'] - slope_mean) / slope_std
    
    # 积分修正 (zscore * r2)
    df['rsrs_score'] = df['zscore'] * df['r2']
    
    # 3. 综合判断信号
    # 默认保持 KEEP，1 为 BUY，-1 为 SELL
    df['signal'] = 0
    buy_cond = (df['rsrs_score'] > RSRS_THRESHOLD) & (df['ma_20'] > df['ma_20_before'])
    sell_cond = (df['rsrs_score'] < -RSRS_THRESHOLD) & (df['ma_20'] < df['ma_20_before'])
    df.loc[buy_cond, 'signal'] = 1
    df.loc[sell_cond, 'signal'] = -1
    
    engine.benchmark_signal = df['signal']

# ----------------- 每日调仓逻辑 -----------------
def etf_rotation_strategy(engine: MultiBacktestEngine, current_date: str, today_data: pd.DataFrame, positions: dict) -> list:
    orders = []
    
    # 1. 获取基准择时信号 (0: KEEP, 1: BUY, -1: SELL)
    # 注意避免未来函数，当天决策用当天收盘后的信号算次日交易，所以当前 date 直接拿前一日（或者视当天收盘价而定） 
    # 在引擎框架中，today_data 是可拿到的最新数据(已收盘)
    signal = 0
    if hasattr(engine, 'benchmark_signal') and current_date in engine.benchmark_signal.index:
        signal = engine.benchmark_signal.loc[current_date]
        
    # # 2. 硬止损检查 (-20%)
    # for code, shares in list(positions.items()):
    #     if code in today_data.index:
    #         entry_price = engine.df.loc[current_date, code]['close'] # 近似处理
    #         # 简化起见，此处使用当日收盘作浮亏预估
    #         ret = today_data.loc[code, 'close'] / entry_price - 1
    #         if ret <= STOP_LOSS_PCT:
    #             orders.append({'code': code, 'action': 'sell'})
    #             print(f"[{current_date}] {code} 触发止损: 收益率 {ret:.2%}")
    #             del positions[code] # 假装已剔除，防止后续逻辑干扰
                
    # 3. 如果宏观择时为空头，全部清仓
    if signal == -1:
        for code in positions.keys():
            orders.append({'code': code, 'action': 'sell'})
        return orders
        
    # 4. 如果择时持多 (BUY 或是 KEEP)，寻找动量最强的 ETF 一只全仓
    # 获取历史切片计算动量
    #current_idx = engine.dates.index(current_date)
    #start_idx = max(0, current_idx - MOMENTUM_DAY + 1)
    #period_dates = engine.dates[start_idx:current_idx + 1]
    
    scores = {}
    for code in ETF_POOL:
        # 【删除】这下面两行，无论今天停牌还是缺失数据，都去获取它历史最新的切片来算动量
        # if code not in today_data.index:
        #     continue
        try:
            # 使用引擎内置函数获取以 today(current_date) 为复权锚点的前复权数据
            df_qfq = engine.get_history_qfq(code, current_date)
            
            if df_qfq.empty or len(df_qfq) < MOMENTUM_DAY * 0.8:
                continue
                
            # 截取最近 MOMENTUM_DAY 天的前复权收盘价
            hist_close = df_qfq['qfq_close'].tail(MOMENTUM_DAY).values
                
            y = np.log(hist_close)
            x = np.arange(len(y))
            
            slope, intercept = np.polyfit(x, y, 1)
            annualized_returns = np.power(np.exp(slope), 250) - 1
            
            y_pred = slope * x + intercept
            ss_res = np.sum((y - y_pred)**2)
            ss_tot = np.sum((y - np.mean(y))**2)
            r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
            # r_squared = 1 - (sum((y - (slope * x + intercept))**2) / ((len(y) - 1) * np.var(y, ddof=1)))
            #r_squared = 1 - (ss_res / ((len(y) - 1) * np.var(y, ddof=1))) if ss_tot != 0 else 0
            
            scores[code] = annualized_returns * r_squared
        except Exception:
            continue
            
    if not scores:
        return orders
        
    #print(scores)
    # 选取得分排第一的标的
    top_etf = sorted(scores.items(), key=lambda x: x[1], reverse=True)[0][0]
    
    # 调仓：不在此标的上的仓位全部卖出，然后将资金全仓买入 top_etf
    hold_top = False
    for code in positions.keys():
        if code != top_etf:
            orders.append({'code': code, 'action': 'sell'})
        else:
            hold_top = True
            
    if not hold_top:
        orders.append({'code': top_etf, 'action': 'buy', 'weight': 1.0})
        
    return orders

# ----------------- 启动回测 -----------------
if __name__ == '__main__':
    ETF_H5_PATH = get_latest_h5_path(ETF_RAW_H5_DIR)
    
    # 初始化引擎，启用 ETF 模式 (手续费万0.5，无印花税)
    engine = MultiBacktestEngine(
        data_path=ETF_H5_PATH,
        start_date='20190101', # 可以往后调，等待RSRS_M (600天) 窗口走满
        end_date='20251216',
        initial_capital=10000.0,
        commission=0.00005,
        tax=0.0,
        is_etf=True,
        warmup_days=RSRS_M + RSRS_N + 5, # 给足 620 天用来算初始斜率标准分
    )
    
    # 预计算 benchmark 信号
    precalc_benchmark_signal(engine)
    
    # 开始运行
    result = engine.run(etf_rotation_strategy)
    
    # 保存和打印
    engine.print_report(result)
    engine.save_report(result, "ETF_RSRS_MOMENTUM")

'''

============================================================
全市场组合回测报告 | 20190101 ~ 20251216 | 基准: 000300
============================================================
  初始资金:     10,000.00
  交易天数:     1689
  总交易次数:   122
  策略总收益:   911.45%  |  基准: 49.39%
  策略年化:     40.85%  |  基准: 6.12%
  夏普比率:     1.5092
  最大回撤:     -27.18%
  胜率:         64.75%
  平均盈利:     4.42%
  平均亏损:     -2.19%
  佣金:         0.005% (双边)
  印花税:       0.000% (卖出)
============================================================

效果与 https://www.joinquant.com/algorithm/backtest/detail?backtestId=6c8efc8b3a47b074823fdd409b0fb3b3 基本一致
聚宽总收益：911.50%

注：打开了强制撮合开关：限价单无条件直接成交，不做任何价格、成交量检查
    set_option("match_by_signal", True)
'''