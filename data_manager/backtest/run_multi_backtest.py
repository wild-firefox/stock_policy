import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) # 添加项目根目录 stock_policy (用于导入 config)
from engine_multi import MultiBacktestEngine
from config import STK_RAW_H5_DIR, get_latest_h5_path


'''
engine 示例文件
'''


def multi_ma_strategy(engine, current_date, today_data, current_positions):
    """
    多股均线策略示例：真正的利用当天前复权数据 (qfq) 计算指标 (日线级别)
    这个表示 日线级别的 MA5/MA20 金叉死叉策略，持仓上限 5 只，等权分配资金。卖出条件：持仓股票的 MA5 死叉 MA20；买入条件：非持仓股票的 MA5 金叉 MA20。
    """
    orders = []
    valid_stocks = today_data[today_data['open'] > 0].index.tolist()
    
    # 1. 卖出逻辑 (判断持仓股票是否死叉)
    for code, shares in current_positions.items():
        base_df = engine.get_history_qfq(code, current_date)
        qfq = engine.calc_indicators(base_df, freq='day', indicators=['ma_5', 'ma_20'])
        
        if len(qfq) >= 20:
            ma_short = qfq['ma_qfq_5'].iloc[-1]
            ma_long = qfq['ma_qfq_20'].iloc[-1]
            if ma_short < ma_long:
                orders.append({'code': code, 'action': 'sell'})
                
    # 2. 买入逻辑 (寻找金叉)
    candidates = valid_stocks[:50] 
    buy_list = []
    for code in candidates:
        if code in current_positions:
            continue
            
        base_df = engine.get_history_qfq(code, current_date)
        qfq = engine.calc_indicators(base_df, freq='day', indicators=['ma_5', 'ma_20'])
        
        if len(qfq) >= 20:
            ma_short = qfq['ma_qfq_5']
            ma_long = qfq['ma_qfq_20']
            
            prev_s, curr_s = ma_short.iloc[-2], ma_short.iloc[-1]
            prev_l, curr_l = ma_long.iloc[-2], ma_long.iloc[-1]
            
            if prev_s <= prev_l and curr_s > curr_l:
                buy_list.append(code)
                
    # 3. 分配资金，等权买入
    free_slots = 5 - len(current_positions)
    if free_slots > 0:
        for code in buy_list[:free_slots]:
            orders.append({'code': code, 'action': 'buy', 'weight': 0.2})
        
    return orders

def multi_weekly_ma_strategy(engine, current_date, today_data, current_positions):
    """
    周线级别多股均线策略示例
    这个表示 周线级别的 MA5/MA10 金叉死叉策略，持仓上限 5 只，等权分配资金。卖出条件：持仓股票的周线 MA5 死叉 MA10；买入条件：非持仓股票的周线 MA5 金叉 MA10。
    """
    orders = []
    valid_stocks = today_data[today_data['open'] > 0].index.tolist()
    
    # 1. 卖出逻辑
    for code, shares in current_positions.items():
        base_df = engine.get_history_qfq(code, current_date)
        weekly_df = engine.calc_indicators(base_df, freq='week',indicators=['ma_5', 'ma_10'])
        
        if len(weekly_df) >= 10:
            ma_short = weekly_df['ma_qfq_5'].iloc[-1]
            ma_long = weekly_df['ma_qfq_10'].iloc[-1]
            
            if ma_short < ma_long:
                orders.append({'code': code, 'action': 'sell'})
                
    # 2. 买入逻辑
    candidates = valid_stocks[:50] 
    buy_list = []
    
    for code in candidates:
        if code in current_positions:
            continue
            
        base_df = engine.get_history_qfq(code, current_date)
        weekly_df = engine.calc_indicators(base_df, freq='week')
        
        if len(weekly_df) >= 10:
            # ma_short = weekly_df['qfq_close'].rolling(5).mean()
            # ma_long = weekly_df['qfq_close'].rolling(10).mean()
            ma_short = weekly_df['ma_qfq_5']
            ma_long = weekly_df['ma_qfq_10']
            
            prev_s, curr_s = ma_short.iloc[-2], ma_short.iloc[-1]
            prev_l, curr_l = ma_long.iloc[-2], ma_long.iloc[-1]
            
            if prev_s <= prev_l and curr_s > curr_l:
                buy_list.append(code)
                
    # 3. 分配资金
    free_slots = 5 - len(current_positions)
    if free_slots > 0:
        for code in buy_list[:free_slots]:
            orders.append({'code': code, 'action': 'buy', 'weight': 0.2})
        
    return orders

def main():
    hdf_path = get_latest_h5_path(STK_RAW_H5_DIR) #os.path.join(STK_RAW_H5_DIR, '20000104_20260416', "market_data.h5") ## 之后改
    if not os.path.exists(hdf_path):
        print(f"Data {hdf_path} not found. Please run build_hdf5.py first.")
        return
        
    print(">>> Test MultiBacktestEngine (MA Cross Strategy)")
    engine = MultiBacktestEngine(
        data_path= hdf_path, 
        start_date="20200101",
        end_date="20240131",
        initial_capital=1000000.0,
        commission=0.0002, # 万2
        tax=0.0005 # 万5
    )
    
    result = engine.run(multi_ma_strategy)
    
    engine.print_report(result)

if __name__ == '__main__':
    main()
