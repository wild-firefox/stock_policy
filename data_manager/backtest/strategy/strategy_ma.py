import os
import sys

# 动态添加相对路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(current_dir)) # 添加 backtest 目录 (用于导入 engine_multi)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))) # 添加项目根目录 stock_policy (用于导入 config)

from engine_multi import MultiBacktestEngine
from config import STK_RAW_H5_DIR,get_latest_h5_path



def strategy_ma(engine, current_date, today_data, current_positions):
    """
    单股均线策略 (Ping An Bank: 000001.SZ)
    如果 T 日收盘价高出 5 日平均价 1%, 则 T+1 开盘全仓买入
    如果 T 日收盘价低于 5 日平均价, 则 T+1 开盘空仓卖出
    """
    orders = []
    target_code = '000001' # 来代表 000001.SZ 平安银行，后续可以改成参数化的形式
    
    # 判断该股当天是否有数据（可能停牌）
    if target_code not in today_data.index:
        return orders
        
    # 获取包含今天的历史前复权数据
    qfq_df = engine.get_history_qfq(target_code, current_date)
    
    qfq = engine.calc_indicators(qfq_df, freq='day', indicators=['ma_5'])
        
    
    # 需要至少 5 天的数据来计算 MA5
    if len(qfq_df) < 5:
        return orders
        
    # 获取过去 5 天的收盘价
    close_data = qfq_df['qfq_close'].tail(5)
    
    #### 两者没有差距，使用算好的会快一点点。
    ma5 = close_data.mean()
    #ma5= qfq['ma_qfq_5'].iloc[-1]

    current_price = close_data.iloc[-1]

    #print(f"{current_date} - {current_price} - MA5: {ma5:.2f}")
    
    is_holding = target_code in current_positions

    # 买入条件：上一时间点价格高出五天平均价 1%，且当前未持仓（模拟 cash > 0）
    if current_price > 1.01 * ma5:
        if not is_holding:
            # 记录买入指令，使用全仓 (weight: 1.0)
            orders.append({'code': target_code, 'action': 'buy', 'weight': 1.0})
            
    # 卖出条件：上一时间点价格低于五天平均价，且当前持仓
    elif current_price < ma5:
        if is_holding:
            # 记录卖出指令
            orders.append({'code': target_code, 'action': 'sell'})

    return orders

def main():
    hdf_path = get_latest_h5_path(STK_RAW_H5_DIR)
    if not os.path.exists(hdf_path):
        print(f"找不到数据文件: {hdf_path}")
        return
        
    print(">>> Test MultiBacktestEngine (MA Strategy on 000001.SZ)")
    engine = MultiBacktestEngine(
        data_path=hdf_path,
        start_date="20230901",
        end_date="20231231",
        initial_capital=100000.0,
        commission=0.0002,     # 买卖双边万二
        tax=0.0005,            # 卖出千分之0.5印花税
        warmup_days=10         # 增加预热期以确保开局有足够的 MA5 数据
    )
    
    result = engine.run(strategy_ma)
    engine.print_report(result)

    # 保存报告及收益曲线图
    engine.save_report(result, strategy_name="ma_000001")

if __name__ == '__main__':
    main()

'''


    

聚宽回测：（强制撮合： `set_option("match_by_signal", True)`）
https://www.joinquant.com/algorithm/backtest/detail?backtestId=ec8505e20dc44ce58d53fb5d29dd043a

策略收益：-4.21%

'''