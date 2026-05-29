import os
import sys
import math
import pandas as pd
from functools import lru_cache
import tushare as ts

# 动态添加相对路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(current_dir)) # 添加 backtest 目录 (用于导入 engine_multi)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))) # 添加项目根目录 stock_policy (用于导入 config)

from engine_multi import MultiBacktestEngine
from config import STK_RAW_H5_DIR, TUSHARE_TOKEN,get_latest_h5_path

# 设定 Tushare API (确保 config.py 中配置了 TUSHARE_TOKEN)
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

@lru_cache(maxsize=128) # 参数一模一样 → 直接返回缓存结果，不重新跑函数,最多存128组
def get_hist_index_stocks_cached(index_code: str, year_month: str):
    """
    按月缓存获取指数成分股，避免每天重复请求 Tushare。
    """
    start_date = f"{year_month}01"
    dt = pd.to_datetime(start_date)
    end_date = (dt + pd.offsets.MonthEnd(0)).strftime('%Y%m%d')
    try:
        df = pro.index_weight(index_code=index_code, start_date=start_date, end_date=end_date)
        return [] if df.empty else df['con_code'].drop_duplicates().tolist()
    except Exception as e:
        print(f"获取 {year_month} 指数成分股失败: {e}")
        return []

## TODO 由于这个也会占内存 先不存进去

@lru_cache(maxsize=128)
def get_st_stocks_cached(trade_date: str):
    """
    获取历史某日的 ST 股票列表
    """
    try:
        # Tushare 获取 ST 股票有多种方式，这里假定使用 bak_basic 接口判断名称是否含 'ST'
        #df = pro.bak_basic(trade_date=trade_date, fields='ts_code,name')
        df = pro.stock_st(trade_date=trade_date,fields='ts_code')
        if not df.empty:
            #return df[df['name'].str.contains('ST')]['ts_code'].tolist()
            return df['ts_code'].tolist()
        return []
    except Exception as e:
        print(f"获取 {trade_date} ST 股票失败: {e}")
        return []

@lru_cache(maxsize=128) 
def get_suspended_stocks_cached(trade_date: str):
    """
    获取历史某日停牌的股票列表
    """
    try:
        df = pro.suspend_d(trade_date=trade_date, suspend_type='S')
        return [] if df.empty else df['ts_code'].tolist()
    except Exception as e:
        print(f"获取 {trade_date} 停牌股票失败: {e}")
        return []


def check_limit_status(code, current_price, pre_close, is_st=False):
    """
    按照 A 股规则，使用复权昨收精确计算涨跌停状态。
    考虑 ST 股票特殊的 5% 涨跌幅。
    """
    if pd.isna(current_price) or pd.isna(pre_close) or pre_close <= 0:
        return False, False
        
    pure_code = str(code).split('.')[0]
    # 判断涨跌幅比例
    if pure_code.startswith(('8', '4')):
        ratio = 0.30
    elif pure_code.startswith(('688', '30')):
        ratio = 0.20
    elif is_st:
        ratio = 0.05
    else:
        ratio = 0.10 

    # 严格按照四舍五入保留2位小数计算涨跌停价
    limit_up_price = math.floor(pre_close * (1 + ratio) * 100 + 0.5) / 100.0
    limit_down_price = math.floor(pre_close * (1 - ratio) * 100 + 0.5) / 100.0

    limit_up_tolerance = round(abs(pre_close * (1 + ratio) - limit_up_price), 10)
    limit_down_tolerance = round(abs(pre_close * (1 - ratio) - limit_down_price), 10)

    is_limit_up = (current_price > 0) and (abs(current_price - limit_up_price) <= limit_up_tolerance)
    is_limit_down = (current_price > 0) and (abs(current_price - limit_down_price) <= limit_down_tolerance)

    return is_limit_up, is_limit_down

def strategy_smallgo(engine, current_date, today_data, current_positions):
    """
    小市值轮动策略 (严格前复权验证涨跌停), 次日开盘调仓
    current_date 为日期
    today_data 为数据
    """
    BUY_STOCK_COUNT = 5
    INDEX_CODE = '399101.SZ'
    orders = []
    
    # 1. 获取基础股票池和过滤名单
    year_month = str(current_date)[:6] 
    index_stocks = get_hist_index_stocks_cached(INDEX_CODE, year_month)

    st_stocks_ts = get_st_stocks_cached(current_date) ###
    suspended_ts = get_suspended_stocks_cached(current_date) ###
    
    engine_symbols = today_data.index.tolist()
    is_engine_pure_number = not any(s.endswith('.SZ') or s.endswith('.SH') for s in engine_symbols[:5])
    
    # 处理 Tushare ts_code 后缀对齐
    target_universe = [s.split('.')[0] for s in index_stocks] if is_engine_pure_number else index_stocks
    st_stocks = [s.split('.')[0] for s in st_stocks_ts] if is_engine_pure_number else st_stocks_ts ###
    suspended_stocks = [s.split('.')[0] for s in suspended_ts] if is_engine_pure_number else suspended_ts ###
        
    # 2. 基础过滤：在指数内，且排除 ST、排除停牌（也有 vol > 0 的保护）
    valid_data = today_data[  ## 
        (today_data.index.isin(target_universe)) & 
        (~today_data.index.isin(st_stocks)) &  ###
        (~today_data.index.isin(suspended_stocks)) & ###
        (today_data['vol'] > 0) & 
        (today_data['open'] > 0)
    ].copy()
    
    if valid_data.empty:
        target_buy_list = []
    else:
        # 3. 按流通市值从小到大排序
        valid_data = valid_data.sort_values(by='circ_mv', ascending=True)
        
        target_buy_list = []
        # 4. 逐个验证前复权是否涨跌停，直到选齐
        for code in valid_data.index:
            qfq_df = engine.get_history_qfq(code, current_date)
            if len(qfq_df) < 2: 
                continue
                
            today_qfq = qfq_df.iloc[-1]
            qfq_close = today_qfq['qfq_close']
            qfq_pre_close = today_qfq['qfq_pre_close']
            
            is_st = code in st_stocks ###
            is_limit_up, is_limit_down = check_limit_status(code, qfq_close, qfq_pre_close , is_st) ###
            
            # 过滤涨停、跌停股票（昨日涨停、跌停）注意：判断收盘后涨停、跌停 对于第二天来说相当于是昨日涨停、跌停
            #if not is_limit_up and not is_limit_down:
            target_buy_list.append(code)
                
            if len(target_buy_list) >= BUY_STOCK_COUNT:
                break
                
    # 5. 生成卖出指令（不在目标列表中的持仓）
    sell_n = 0
    for code, shares in current_positions.items():
        if code not in target_buy_list: # 已不在目标池
            qfq_df = engine.get_history_qfq(code, current_date)
            if len(qfq_df) > 0:
                today_qfq = qfq_df.iloc[-1]
                qfq_close = today_qfq['qfq_close']
                qfq_pre_close = today_qfq['qfq_pre_close']
                
                # 判断当前持仓股是否跌停，跌停则卖不出去，静默跳过。 昨日跌停，但是第二日可能不跌停，可以不跳过，除非第二天开盘价为跌停则卖不出去。但是这里默认是不知道第二天的开盘价，所以这里可去掉判断跌停过滤
                is_st = code in st_stocks
                is_up, is_down = check_limit_status(code, qfq_close, qfq_pre_close , is_st)
                
                # 停牌也静默跳过
                if  (code in suspended_stocks):
                    continue
                    
            orders.append({'code': code, 'action': 'sell'})
            sell_n += 1
                
    # # 6. 生成买入指令
    position_count = len(current_positions) - sell_n
    
    for code in target_buy_list:
        if code not in current_positions:
            weight_per_stock = 1.0 / (BUY_STOCK_COUNT-position_count)
            orders.append({'code': code, 'action': 'buy', 'weight': weight_per_stock})

    # # 6. 生成买入指令
    # keep_count = len([c for c in current_positions.keys() if c in target_buy_list])
    
    # if BUY_STOCK_COUNT > keep_count:
    #     # 剩余需要买入的槽位
    #     buy_slots = BUY_STOCK_COUNT - keep_count
    #     # 将剩余的理论资金权重平分（保证全仓运作，不留闲余）
    #     weight_per_stock = (1.0 - (keep_count / BUY_STOCK_COUNT)) / buy_slots
        
    #     buy_added = 0
    #     for code in target_buy_list:
    #         if code not in current_positions:
    #             orders.append({'code': code, 'action': 'buy', 'weight': weight_per_stock})
    #             buy_added += 1
    #             if keep_count + buy_added >= BUY_STOCK_COUNT:
    #                 break
            
    return orders

def main():
    hdf_path = get_latest_h5_path(STK_RAW_H5_DIR)
    if not os.path.exists(hdf_path):
        print(f"找不到数据文件: {hdf_path}")
        return
        
    print(">>> Test MultiBacktestEngine (SmallGO Strategy)")
    engine = MultiBacktestEngine(
        data_path=hdf_path,
        start_date="20230101",
        end_date="20230331",
        initial_capital=100000.0,
        commission=0.0002,
        tax=0.0005
    )
    
    result = engine.run(strategy_smallgo)
    engine.print_report(result)

    # 增加保存数据及图形调用
    engine.save_report(result, strategy_name="small_go")

if __name__ == '__main__':
    main()

'''

最终结果：
全市场组合回测报告 | 20230101 ~ 20230331
============================================================
  交易天数:     60
  总交易次数:   13
  总收益率:     8.95%
  年化收益率:   42.95%
  夏普比率:     2.2811
  最大回撤:     -5.86%
  胜率:         53.85%
  平均盈利:     10.97%
  平均亏损:     -4.02%
  佣金:         0.020% (双边)
  印花税:       0.050% (卖出)
================================

聚宽结果：（启用了开盘强制撮合）
https://www.joinquant.com/algorithm/backtest/detail?backtestId=139f8d7764446e565c0b766cecfd4253

策略收益：9.03%
两者结果基本一致，且交易记录也基本一致。可以认为当前回测系统成功搭建。

'''