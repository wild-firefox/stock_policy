"""
多股回测引擎 (Multi-Stock Backtest Engine)
支持全市场横截面回测，基于预处理好的 HDF5 或批量载入的数据。
"""
import os
import sys
import pandas as pd
import numpy as np
from typing import Callable, List, Dict, Optional
import math
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import BACKTEST_BASE, BACKTEST_ETF_BASE, is_excluded, IDX_RAW_DIR
from custom_indicators import compute_indicators
from recalc_qfq import apply_qfq_indicators

class MultiBacktestEngine:
    def __init__(self, data_path: str, start_date: str, end_date: str,
                 initial_capital: float = 100000.0,
                 commission: float = 0.001, tax: float = 0.001,
                 exclude_boards: list = ['gem', 'star', 'bj'],
                 warmup_days: int = 1,
                 benchmark: str = '000300',
                 is_etf: bool = False,
                 ):
        """
        参数:
            data_path:      已处理好的全量 HDF5 数据路径 (或包含所有 CSV 的目录)
            start_date:     回测起始日 YYYYMMDD
            end_date:       回测截止日 YYYYMMDD
            initial_capital:初始资金
            exclude_boards: 排除板块，如 ['gem', 'star', 'bj']
            warmup_days:    自适应预热天数，在 start_date 之前多加载 N 个交易日的历史用来算指标
            benchmark:      基准指数代码 (默认 '000300')，将自动查找 IDX_RAW_DIR 下的对应文件

            ## 注意 ！！ 使用开盘价来买卖的 有时可能开盘价并不能成交都是开盘的买卖1价成交

        """
        self.data_path = data_path
        self.target_start_date = start_date
        
        # 预推 start_date 以涵盖足够的预热期
        if warmup_days > 0:
            try:
                import pandas_market_calendars as mcal
                offset_days = int(warmup_days * 1.5) + 15  # 保守估算自然日跨度
                dt_end = pd.to_datetime(start_date)
                dt_start_fetch = dt_end - pd.Timedelta(days=offset_days)
                
                # 获取上交所交易日历
                sse = mcal.get_calendar('SSE')
                schedule = sse.schedule(start_date=dt_start_fetch, end_date=dt_end)
                
                # 获取格式化后的交易日列表
                cal_dates = schedule.index.strftime('%Y%m%d').tolist()
                
                # 往前推 warmup_days + 1 (+1 是为了确保肯定能包住目标日期的前一个交易日)
                if len(cal_dates) > warmup_days:
                    self.start_date = cal_dates[-(warmup_days + 1)]
                else:
                    self.start_date = cal_dates[0]
                print(f"启用预热机制: 匹配 SSE 交易日历，加载起点从 {start_date} 精确推前到 {self.start_date}。")
            except Exception as e:
                print(f"获取交易日历失败，回退到自然日估算: {e}")
                offset_days = int(warmup_days * 1.5) + 15  # 保守估算自然日跨度
                dt_start = pd.to_datetime(start_date) - pd.Timedelta(days=offset_days)
                self.start_date = dt_start.strftime('%Y%m%d')
                print(f"启用预热机制: 估算起点从 {start_date} 推前到 {self.start_date}。")
        else:
            self.start_date = start_date
            print("未启用预热机制，可决策时的日期可能滞后直观的 start_date，建议默认启用 warmup_days 以获得更准确的指标计算。")

        self.end_date = end_date
        self.initial_capital = initial_capital
        self.commission = commission
        self.tax = tax
        self.exclude_boards = exclude_boards if exclude_boards is not None else ['gem', 'star', 'bj']
        self.is_etf = is_etf
        
        # 自动拼接基准数据文件路径
        if benchmark:
            self.benchmark_code = benchmark
            symbol = benchmark.split('.')[0]  # 兼容 '000300.SH' 这种带后缀的输入
            self.benchmark_path = os.path.join(IDX_RAW_DIR, f"idx_{symbol}.csv")
        else:
            self.benchmark_path = None
            
        self.benchmark_df = None
        
        # 加载数据 (假定是 MultiIndex: [trade_date, ts_code])
        print(f"正在加载全市场数据 ({self.start_date} ~ {end_date}) ...")
        self._load_data()
        
        # 获取全局交易日历（升序）
        self.dates = sorted(self.df.index.get_level_values('trade_date').unique().tolist())
        
        if self.benchmark_path and os.path.exists(self.benchmark_path):
            self._load_benchmark()

    def _load_benchmark(self):
        try:
            df_bm = pd.read_csv(self.benchmark_path, dtype={'trade_date': str})
            df_bm['trade_date'] = df_bm['trade_date'].str.replace('-', '')
            df_bm = df_bm.set_index('trade_date').sort_index()
            self.benchmark_df = df_bm
            print(f"基准数据加载成功: {self.benchmark_path}")
        except Exception as e:
            print(f"基准数据加载失败: {e}")
        
    def _load_data(self):
        """
        加载数据并过滤日期。
        支持 HDF5 或 包含 CSV 文件的目录读取。
        """
        import time
        import concurrent.futures
        t_start = time.time()

        if self.data_path.endswith('.h5') or self.data_path.endswith('.hdf5'):
            self.df = pd.read_hdf(self.data_path, key='data')
            self.df = self.df.sort_index()
            idx = pd.IndexSlice
            self.df = self.df.loc[idx[self.start_date:self.end_date, :], :]
        else:
            # 目录模式按需加载减轻内存
            if not os.path.isdir(self.data_path):
                raise ValueError(f"{self.data_path} 不是有效的目录或 .h5 文件")

            # 1. 先收集所有符合条件的文件路径
            target_files = []
            for file_name in os.listdir(self.data_path):
                if not file_name.endswith('.csv'):
                    continue
                ts_code = file_name.replace('.csv', '')
                
                if not self.is_etf and is_excluded(ts_code, self.exclude_boards):
                    continue
                    
                target_files.append((os.path.join(self.data_path, file_name), ts_code))

            # 2. 定义处理单个文件的纯函数
            def process_file(args):
                file_path, ts_code = args
                try:
                    # 原生极速读取第一行获取 header
                    with open(file_path, 'r', encoding='utf-8') as f:
                        header_line = f.readline().strip()
                    header = [c.strip('"') for c in header_line.split(',')]
                    target_base = BACKTEST_ETF_BASE if self.is_etf else BACKTEST_BASE
                    valid_cols = [c for c in header if c in target_base]
                    
                    if not valid_cols:
                        return None
                    
                    # 尝试用 pyarrow 引擎提速，不支持则回退为默认 c 引擎
                    try:
                        df_part = pd.read_csv(file_path, usecols=valid_cols, dtype={'trade_date': str}, engine='pyarrow')
                    except Exception:
                        df_part = pd.read_csv(file_path, usecols=valid_cols, dtype={'trade_date': str}, engine='c')
                        
                    df_part['trade_date'] = df_part['trade_date'].str.replace('-', '')
                    df_part = df_part[(df_part['trade_date'] >= self.start_date) & (df_part['trade_date'] <= self.end_date)].copy()
                    
                    if df_part.empty:
                        return None
                        
                    if 'ts_code' not in df_part.columns:
                        df_part['ts_code'] = ts_code
                        
                    return df_part
                except Exception as e:
                    # print(f"读取 {ts_code} 失败: {e}")
                    return None

            # 3. 使用线程池并发读取
            all_dfs = []
            max_workers = os.cpu_count() or 4
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                for result in executor.map(process_file, target_files):
                    if result is not None:
                        all_dfs.append(result)

            if not all_dfs:
                raise ValueError("未读取到任何符合条件的数据")

            # 1. 合并数据
            self.df = pd.concat(all_dfs, ignore_index=True)
            
            # 取消引用并强制回收合并前的碎片内存
            del all_dfs
            import gc
            gc.collect()

            # 2. 极致降维：将 trade_date 和 ts_code 转为 category；float64 降维 float32
            self.df['trade_date'] = self.df['trade_date'].astype('category')
            self.df['ts_code'] = self.df['ts_code'].astype('category')
            
            float_cols = self.df.select_dtypes(include=['float64']).columns
            self.df[float_cols] = self.df[float_cols].astype('float32')

            # 3. 设置索引及去碎片化
            self.df = self.df.set_index(['trade_date', 'ts_code']).sort_index().copy()
            gc.collect() # 再次强制回收中间副本体积
        
        t_elapsed = time.time() - t_start
        mem_usage = self.df.memory_usage(deep=True).sum() / (1024 ** 2)
        print(f"数据加载完成: 共加载 {len(self.df)} 条记录, 耗时 {t_elapsed:.2f} 秒, 占用内存 {mem_usage:.2f} MB")


    def get_history_qfq(self, symbol: str, anchor_date: str) -> pd.DataFrame:
        """
        获取单个股票以 anchor_date 的 adj_factor 为锚点的基础前复权数据。
        仅包含基础 qfq 价格，不计算指标。
        """
        idx = pd.IndexSlice
        try:
            data = self.df.loc[idx[:anchor_date, symbol], :].copy()
        except KeyError:
            return pd.DataFrame()
            
        if data.empty:
            return pd.DataFrame()
            
        data = data.reset_index()
        
        anchor_rows = data[data['trade_date'] == anchor_date]
        if len(anchor_rows) == 0:
            anchor_rows = data.iloc[[-1]]
            
        anchor_factor = anchor_rows['adj_factor'].iloc[0]
        if anchor_factor == 0 or pd.isna(anchor_factor):
            anchor_factor = 1.0

        d = 2  if self.is_etf == False else 3 # a股 为2位 ,etf 3位
        ratio = data['adj_factor'] / anchor_factor
        
        data['qfq_open'] = (data['open'] * ratio).round(d)
        data['qfq_high'] = (data['high'] * ratio).round(d)
        data['qfq_low'] = (data['low'] * ratio).round(d)
        data['qfq_close'] = (data['close'] * ratio).round(d)
        data['qfq_pre_close'] = (data['close'].shift(1) * data['adj_factor'].shift(1) / anchor_factor).round(d)
        
        # 缓存 ratio 用于日线指标的快速缩放
        data['_ratio'] = ratio 
        return data

    def calc_indicators(self, data: pd.DataFrame, freq: str = 'day', indicators: Optional[List[str]] = None) -> pd.DataFrame:
        """
        基于 get_history_qfq 返回的基础 qfq 数据，计算技术指标。

        日线：股票走 recalc_qfq.apply_qfq_indicators()（bfq 快路径 + 公式兜底），
              ETF 同样走该函数（无 bfq 列时自动全部公式计算）。
        周线：resample 后用 compute_indicators() 全部公式计算。
        """
        if data.empty:
            return pd.DataFrame()

        if freq == 'day':
            if indicators:
                apply_qfq_indicators(data, indicators)
            if '_ratio' in data.columns:
                data = data.drop(columns=['_ratio'])
            return data.copy()

        elif freq == 'week':
            df = data.copy()
            df['trade_date_dt'] = pd.to_datetime(df['trade_date'])
            df.set_index('trade_date_dt', inplace=True)

            agg_dict = {
                'qfq_open': 'first',
                'qfq_high': 'max',
                'qfq_low': 'min',
                'qfq_close': 'last',
                'trade_date': 'last'
            }
            if 'vol' in df.columns: agg_dict['vol'] = 'sum'
            if 'amount' in df.columns: agg_dict['amount'] = 'sum'

            weekly_df = df.resample('W-FRI').agg(agg_dict).dropna(subset=['qfq_close']).reset_index(drop=True)

            if indicators:
                compute_indicators(weekly_df, indicators,
                                  close_col='qfq_close', high_col='qfq_high', low_col='qfq_low')
            return weekly_df.copy()
                   
    def run(self, strategy_fn: Callable) -> dict:
        """
        每日横截面循环
        
        参数:
            strategy_fn: 统一策略信号函数
                         签名: fn(engine: MultiBacktestEngine, current_date: str, today_data: pd.DataFrame, current_positions: Dict[str, int]) -> List[Dict]
                         返回订单列表, 例如:
                         [
                            {'code': '000001.SZ', 'action': 'sell', 'shares': 500}, # 卖出 500 股
                            {'code': '000001.SZ', 'action': 'sell'},                # 未指定 shares 表示全部卖出
                            {'code': '000002.SZ', 'action': 'buy', 'shares': 1000}, # 明确指定买入 1000 股
                            {'code': '000005.SZ', 'action': 'buy', 'weight': 0.2},  # 使用 20% 总资金买入（会计算成具体股数）
                         ]
        """
        trades = []
        daily_returns = []
        transaction_records = []  # <--- 新增流水记录
        
        capital_cash = self.initial_capital
        
        # 记录持仓: { ts_code: shares }
        positions: Dict[str, int] = {}
        # 记录开仓价: { ts_code: entry_price }
        entry_prices: Dict[str, float] = {}
        # 记录开仓日期: { ts_code: entry_date }
        entry_dates: Dict[str, str] = {}
        
        # 昨日产生的订单
        pending_orders = []
        
        print("开始回测...")
        start_time = time.time()

        # 找到 target_start_date 在实际交易日历中的位置
        target_idx = 0
        for i, d in enumerate(self.dates):
            if d >= self.target_start_date:
                target_idx = i
                break
                
        # 过滤出真正需要跑策略的交易日：额外多拿 target_start_date 的「前一个交易日」
        # 保证在这个前手日日终决策，次日 (target_start_date) 开盘刚好调仓交易
        actual_start_idx = max(0, target_idx - 1)
        trade_dates = self.dates[actual_start_idx:]
        
        print(f"实际策略计算区间: {trade_dates[0]} ~ {trade_dates[-1]} (首日作为发单预备日)")


        sum_days = len(trade_dates)
        
        for i, date in enumerate(trade_dates):
            if (i + 1) % 100 == 0:
                print(f"进度: {i + 1} / {sum_days} 天 ({date})")
                
            try:
                # （因为锚点就是当前日，当天的复权价等于当天的实际价）
                today_data = self.df.loc[date].copy() 
            except KeyError:
                continue

            if today_data.empty:
                continue
                
            # # ----------------------------------------------------
            # # 1. 估算当前总净值 (用于基于 weight 的资金分配)
            # # ----------------------------------------------------
            # market_value = 0.0
            # for code, shares in positions.items():
            #     if code in today_data.index:
            #         market_value += shares * today_data.loc[code, 'close']
            #     else:
            #         market_value += shares * entry_prices[code]
            # current_pv = capital_cash + market_value

            # ----------------------------------------------------
            # 处理昨日产生的挂单 (在今日开盘时执行)
            # 先执行卖出订单以释放资金
            # ----------------------------------------------------
            remaining_orders = []
            
            # --- 卖出队列 ---
            for order in pending_orders:
                if order['action'] != 'sell':
                    continue
                    
                code = order['code']
                
                # 如果没开盘，或者股票不在持仓中，跳过
                if code not in today_data.index or code not in positions:
                    remaining_orders.append(order)
                    continue
                    
                today_open = today_data.loc[code, 'open']
                if pd.isna(today_open) or today_open <= 0:
                    remaining_orders.append(order)
                    continue

                # 决定卖出数量，默认全部卖出
                shares_to_sell = order.get('shares', positions[code])
                shares_to_sell = min(shares_to_sell, positions[code])
                
                if shares_to_sell <= 0:
                    continue

                sell_amount = shares_to_sell * today_open
                fee = sell_amount * (self.commission + self.tax)
                sell_income = sell_amount - fee
                
                buy_cost_total = shares_to_sell * entry_prices[code] * (1 + self.commission)
                closed_pnl = sell_income - buy_cost_total
                ret = closed_pnl / buy_cost_total
                
                capital_cash += sell_income
                positions[code] -= shares_to_sell
                

                # 记录单笔交易流水
                transaction_records.append({
                    '日期': pd.to_datetime(date).strftime('%Y-%m-%d'),
                    '委托时间': '09:30:00',
                    '标的': code,
                    '交易类型': '卖',
                    '成交数量': f"{shares_to_sell}股",
                    '成交价': round(today_open, 3),
                    '成交额': round(sell_amount, 2),
                    '平仓盈亏': f'{round(closed_pnl, 2):.2f}',
                    '手续费': round(fee, 2)
                })
                
                trades.append({
                    'ts_code': code,
                    'entry_date': entry_dates[code],
                    'exit_date': date,
                    'entry_price': round(entry_prices[code], 3),
                    'exit_price': round(today_open, 3),
                    'shares': shares_to_sell,
                    'return': round(ret, 6),
                })
                
                if positions[code] == 0:
                    del positions[code]
                    del entry_prices[code]
                    del entry_dates[code]

            # ----------------------------------------------------
            # 估算当前总净值 (将此部分移动到这里！用当天的开盘价估算未卖出的持仓)
            # 这样算出的 current_pv = 刚刚卖出得到的真实现金 + 剩下未动持仓的早盘市值
            # ----------------------------------------------------
            current_pv = capital_cash
            for cd, shs in positions.items():
                if cd in today_data.index and not pd.isna(today_data.loc[cd, 'open']):
                    current_pv += shs * today_data.loc[cd, 'open']
                else:
                    current_pv += shs * entry_prices[cd]

            # --- 买入队列 ---
            for order in pending_orders:
                if order['action'] != 'buy':
                    continue
                    
                code = order['code']
                if code not in today_data.index:
                    remaining_orders.append(order)
                    continue
                    
                today_open = today_data.loc[code, 'open']
                if pd.isna(today_open) or today_open <= 0:
                    remaining_orders.append(order)
                    continue

                # 确定要买入的股数
                target_shares = 0
                if 'shares' in order:
                    target_shares = order['shares']
                elif 'weight' in order:
                    target_cash = current_pv * order['weight']
                    # 引擎级保护：如果要买的金额超过可用现金，就最多只全仓买入
                    if target_cash > capital_cash:
                        target_cash = capital_cash
                        
                    # 1. 先按不含手续费的纯股价算出理论最大整手股数
                    max_shares = int(target_cash / today_open)
                    target_shares = (max_shares // 100) * 100
                    
                    # 2. 发起极限试算：如果加上真实的双边手续费后超出了账户极限现金，才往下倒扣 100 股
                    while target_shares > 0:
                        test_cost = target_shares * today_open * (1 + self.commission)
                        if test_cost <= capital_cash:
                            break  # 探测到安全线，跳出
                        target_shares -= 100
                else:
                    # 未指定则跳过 (为了安全起见, 不做默认满仓等权, 策略需明确指定资金比例或数额)
                    continue
                    
                if target_shares > 0:
                    fee = target_shares * today_open * self.commission
                    cost = target_shares * today_open + fee
                    # 检查现金是否足够
                    if cost <= capital_cash:
                        capital_cash -= cost
                        
                        # 记录单笔买入流水
                        transaction_records.append({
                            '日期': pd.to_datetime(date).strftime('%Y-%m-%d'),
                            '委托时间': '09:30:00',
                            '标的': code,
                            '交易类型': '买',
                            '成交数量': f"{target_shares}股",
                            '成交价': round(today_open, 3),
                            '成交额': round(target_shares * today_open, 2),
                            '平仓盈亏': 0.00,
                            '手续费': round(fee, 2)
                        })
                        # 合并加仓 (这里简单将开仓价均价，或者记录最后一笔开仓价，通常取平均)
                        if code in positions:
                            old_cost = positions[code] * entry_prices[code]
                            new_cost = target_shares * today_open
                            entry_prices[code] = (old_cost + new_cost) / (positions[code] + target_shares)
                            positions[code] += target_shares
                        else:
                            positions[code] = target_shares
                            entry_prices[code] = today_open
                            entry_dates[code] = date
                    else:
                        print(f"[{date}] 警告: {code} 买入 {target_shares} 股失败，资金不足 (可用={capital_cash:.2f}, 需要={cost:.2f})")

            # 清理昨天的订单，保留未成交的
            pending_orders = [o for o in remaining_orders if o['code'] not in today_data.index or today_data.loc[o['code'], 'open'] <= 0]
            
            # ----------------------------------------------------
            # 3. 每日净值重新结算 (盯市 Mark-to-Market 以今日收盘价)
            # ----------------------------------------------------
            market_value_close = 0.0
            for code, shares in positions.items():
                if code in today_data.index:
                    market_value_close += shares * today_data.loc[code, 'close']
                else:
                    market_value_close += shares * entry_prices[code]
                    
            pv = capital_cash + market_value_close
            
            # 记录当天基准收盘价
            bm_close = np.nan
            if self.benchmark_df is not None and date in self.benchmark_df.index:
                val = self.benchmark_df.loc[date, 'close']
                bm_close = float(val.iloc[0]) if isinstance(val, pd.Series) else float(val)

            daily_returns.append({
                'trade_date': date,
                'pv': pv,
                'position_count': len(positions),
                'benchmark_close': bm_close
            })

            # ----------------------------------------------------
            # 4. 生成新信号 (收盘后，根据今天的数据(今天已收盘)及当前持仓)
            # ----------------------------------------------------
            # 策略统一接管：需自己决定抛出哪些票，选入哪些票。
            # date 为当天交易日期，today_data 为当天交易日期的数据
            new_orders = strategy_fn(self, date, today_data, positions.copy())
            if new_orders:
                pending_orders.extend(new_orders)
        
        # 强制平仓收尾 (简化处理，按照最后一日收盘价)
        last_date = self.dates[-1]
        try:
            last_data = self.df.loc[last_date]
        except KeyError:
            last_data = pd.DataFrame()
            
        for code, shares in positions.items():
            close_p = last_data.loc[code, 'close'] if (not last_data.empty and code in last_data.index) else entry_prices[code]
            sell_amount = shares * close_p
            sell_income = sell_amount * (1 - self.commission - self.tax)
            buy_cost_total = shares * entry_prices[code] * (1 + self.commission)
            ret = (sell_income - buy_cost_total) / buy_cost_total
            capital_cash += sell_income
            trades.append({
                'ts_code': code, 'entry_date': entry_dates[code], 'exit_date': last_date,
                'entry_price': round(entry_prices[code], 3), 'exit_price': round(close_p, 3),
                'shares': shares, 'return': round(ret, 6),
            })
            
        if daily_returns:
            daily_returns[-1]['pv'] = capital_cash
            
        end_time = time.time()
        print(f"回测结束，计算耗时: {end_time - start_time:.2f} 秒")
            
        df_daily = pd.DataFrame(daily_returns)
        df_trades = pd.DataFrame(trades)
        df_transactions = pd.DataFrame(transaction_records) # 流水表
        
        total_return_value = capital_cash / self.initial_capital
        stats = self._calc_stats(df_daily, df_trades, total_return_value)
            
        return {
            'daily': df_daily,
            'trades': df_trades,
            'transactions': df_transactions,
            'stats': stats,
        }

    def _calc_stats(self, df_daily, df_trades, total_return_value):
        """计算统计指标"""
        n_days = len(df_daily)
        if n_days <= 1:
            return self._empty_stats()
            
        # 转换为 NumPy 数组以便计算
        pv_array = df_daily['pv'].values
        
        # 用绝对净值计算每日真实收益率 (长为 n_days - 1)
        daily_rets = np.diff(pv_array) / pv_array[:-1]
        
        # 将每日收益回填到 DataFrame（首日记为 0）
        df_daily['daily_return'] = np.insert(daily_rets, 0, 0.0)
        # 【修改】使用初始资金来计算正确的累计收益率
        df_daily['cum_return'] = (df_daily['pv'] / self.initial_capital) - 1.0

        # 当前累计收益率
        current_rate = total_return_value - 1.0
        
        # 计算年化收益率
        # (1 + R_p) = (1 + r)^(250 / day_num)
        annual_return = (1.0 + current_rate) ** (250 / n_days) - 1.0
        
        # 夏普比率（按用户要求公式）
        rf = 0.04
        sigma_p = np.std(daily_rets, ddof=1) * np.sqrt(250) if len(daily_rets) > 1 else 0.0
        sharpe = (annual_return - rf) / sigma_p if sigma_p > 0 else 0.0

        # 计算基准收益率
        bm_total_return = 0.0
        bm_annual_return = 0.0
        if 'benchmark_close' in df_daily.columns and not df_daily['benchmark_close'].isna().all():
            first_bm = df_daily['benchmark_close'].bfill().iloc[0]
            last_bm = df_daily['benchmark_close'].ffill().iloc[-1]
            if first_bm and first_bm > 0:
                bm_total_return = (last_bm - first_bm) / first_bm
                bm_annual_return = (1.0 + bm_total_return) ** (250 / n_days) - 1.0
        
        
        # 最大回撤
        cum = df_daily['pv']
        peak = cum.cummax()
        drawdown = (cum - peak) / peak
        max_drawdown = drawdown.min()

        # 胜率
        if len(df_trades) > 0:
            win_rate = len(df_trades[df_trades['return'] > 0]) / len(df_trades)
            avg_win = df_trades[df_trades['return'] > 0]['return'].mean() if len(df_trades[df_trades['return'] > 0]) > 0 else 0
            avg_loss = df_trades[df_trades['return'] <= 0]['return'].mean() if len(df_trades[df_trades['return'] <= 0]) > 0 else 0
        else:
            win_rate = avg_win = avg_loss = 0

        return {
            'period': f"{self.target_start_date} ~ {self.end_date}",
            'total_days': n_days,
            'total_trades': len(df_trades),
            'total_return': round(current_rate, 6),
            'annual_return': round(annual_return, 6),
            'bm_total_return': round(bm_total_return, 6),
            'bm_annual_return': round(bm_annual_return, 6),
            #'alpha_annual': round(annual_return - bm_annual_return, 6),
            'sharpe': round(sharpe, 4),
            'max_drawdown': round(max_drawdown, 6),
            'win_rate': round(win_rate, 4),
            'avg_win': round(avg_win, 6),
            'avg_loss': round(avg_loss, 6),
            'commission': self.commission,
            'tax': self.tax,
        }

    def _empty_stats(self):
        return {
            'period': f"{self.target_start_date} ~ {self.end_date}",
            'total_days': 0, 'total_trades': 0, 'total_return': 0.0,
            'annual_return': 0.0, 'bm_total_return': 0.0, 'bm_annual_return': 0.0, 'alpha_annual': 0.0,
            'sharpe': 0.0, 'max_drawdown': 0.0,
            'win_rate': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'commission': self.commission, 'tax': self.tax,
        }

    def print_report(self, result: dict):
        """打印回测报告"""
        s = result['stats']
        print(f"\n{'='*60}")
        print(f"全市场组合回测报告 | {s['period']} | 基准: {self.benchmark_code if self.benchmark_code else '无'}")
        print(f"{'='*60}")
        print(f"  初始资金:     {self.initial_capital:,.2f}")
        print(f"  交易天数:     {s['total_days']}")
        print(f"  总交易次数:   {s['total_trades']}")
        print(f"  策略总收益:   {s['total_return']:.2%}  |  基准: {s['bm_total_return']:.2%}")
        print(f"  策略年化:     {s['annual_return']:.2%}  |  基准: {s['bm_annual_return']:.2%}" ) #|  Alpha: {s['alpha_annual']:.2%}")
        print(f"  夏普比率:     {s['sharpe']:.4f}")
        print(f"  最大回撤:     {s['max_drawdown']:.2%}")
        print(f"  胜率:         {s['win_rate']:.2%}")
        print(f"  平均盈利:     {s['avg_win']:.2%}")
        print(f"  平均亏损:     {s['avg_loss']:.2%}")
        print(f"  佣金:         {s['commission']:.3%} (双边)")
        print(f"  印花税:       {s['tax']:.3%} (卖出)")
        print(f"{'='*60}")

        if len(result['trades']) > 0:
            print(f"\n前 10 笔交易:")
            print(result['trades'].head(10).to_string(index=False))

    def save_report(self, result: dict, strategy_name: str):
        """保存 CSV 交易流水、回测报告、收益曲线图到 log 文件夹"""
        import datetime
        import matplotlib.pyplot as plt
        import matplotlib
        import matplotlib.ticker as ticker
        import matplotlib.dates as mdates
        import numpy as np
        
        # 兼容系统的中文字体显示
        matplotlib.rcParams['axes.unicode_minus'] = False
        
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(os.path.dirname(__file__), 'log', strategy_name, now_str)
        os.makedirs(log_dir, exist_ok=True)
        
        # 1. 保存交易流水 CSV
        df_trans = result['transactions']
        if not df_trans.empty:
            df_trans.to_csv(os.path.join(log_dir, 'transactions.csv'), index=False, encoding='utf-8-sig') #, float_format='%.3f')

        # 2. 保存平仓交易记录 CSV (trades)
        df_trades = result['trades']
        if not df_trades.empty:
            df_trades.to_csv(os.path.join(log_dir, 'trades.csv'), index=False, encoding='utf-8-sig')

        # 3. 生成并保存 Markdown 报告
        s = result['stats']
        # | **年化超额(Alpha)**| {s['alpha_annual']:.2%} | 暂不计算 TODO
        md_content = f"""# 全市场组合回测报告 | {s['period']}

| 指标 | 数值 |
|---|---|
| **基准** | {self.benchmark_code if self.benchmark_code else '无'} |
| **初始资金** | {self.initial_capital:,.2f} |
| **交易天数** | {s['total_days']} |
| **总交易次数** | {s['total_trades']} |
| **策略总收益** | {s['total_return']:.2%} |
| **基准总收益** | {s['bm_total_return']:.2%} |
| **策略年化收益** | {s['annual_return']:.2%} |
| **基准年化收益** | {s['bm_annual_return']:.2%} |
| **夏普比率** | {s['sharpe']:.4f} |
| **最大回撤** | {s['max_drawdown']:.2%} |
| **胜率** | {s['win_rate']:.2%} |
| **平均盈利** | {s['avg_win']:.2%} |
| **平均亏损** | {s['avg_loss']:.2%} |
| **佣金** | {s['commission']:.3%} (双边) |
| **印花税** | {s['tax']:.3%} (卖出) |
"""
        with open(os.path.join(log_dir, 'report.md'), 'w', encoding='utf-8') as f:
            f.write(md_content)
            
        # 4. 回测收益曲线图 PNG（上：收益曲线；下：每日买/卖柱状图）
        df_daily = result['daily'].copy()
        if not df_daily.empty:
            df_daily['trade_date_dt'] = pd.to_datetime(df_daily['trade_date'])
            x_dates = df_daily['trade_date_dt']
            # y = (df_daily['cum_return'] - self.initial_capital)/self.initial_capital * 100 if 'cum_return' in df_daily.columns and df_daily['cum_return'].max() > 10 else df_daily['cum_return'] * 100
            # 【修改】强制转换为 float 并乘以 100 得到百分比
            y = df_daily['cum_return'].astype(float) * 100

            fig, (ax1, ax3) = plt.subplots(2, 1, sharex=True, figsize=(14, 9), gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.08})
            ax1.set_facecolor('#ffffff')
            ax3.set_facecolor('#ffffff')

            ax1.plot(x_dates, y, label='Strategy Return (%)', color='#d62728', linewidth=2)

            # 用于计算 Y 轴范围（包含基准曲线数据）
            y_for_range = y.copy()

            # 画基准曲线
            if 'benchmark_close' in df_daily.columns and not df_daily['benchmark_close'].isna().all():
                bm_series = df_daily['benchmark_close'].interpolate().bfill()
                first_bm_val = bm_series.iloc[0]
                if first_bm_val > 0:
                    # 【修改】强制转换为 float
                    bm_y = (bm_series.astype(float) / first_bm_val - 1.0) * 100
                    y_for_range = pd.concat([y_for_range, bm_y])
                    ax1.plot(x_dates, bm_y, label='Benchmark Return (%)', color='#1f77b4', linewidth=1.5, linestyle='-')

            cum_pv = df_daily['pv']
            peak_series = cum_pv.cummax()
            drawdown_series = (cum_pv - peak_series) / peak_series
            
            if len(drawdown_series) > 0 and drawdown_series.min() < 0:
                end_idx = drawdown_series.idxmin()
                start_idx = cum_pv[:end_idx+1].idxmax() 
                
                start_x, start_y = x_dates.iloc[start_idx], y.iloc[start_idx]
                end_x, end_y = x_dates.iloc[end_idx], y.iloc[end_idx]
                
                ax1.plot([start_x, end_x], [start_y, end_y], 'o', color='black', markersize=8, zorder=5)
                
                ax1.annotate(f"{start_x.strftime('%Y-%m-%d')}", xy=(start_x, start_y), xytext=(5, 10),
                             textcoords='offset points', weight='bold', color='black', fontsize=10)
                ax1.annotate(f"{end_x.strftime('%Y-%m-%d')}", xy=(end_x, end_y), xytext=(5, -15),
                             textcoords='offset points', weight='bold', color='black', fontsize=10)
                ax1.fill_between(x_dates.iloc[start_idx:end_idx+1], y.iloc[start_idx:end_idx+1], 
                                 y.iloc[start_idx], color='gray', alpha=0.15, label='Max Drawdown')

            ax1.set_title(f"Strategy: {strategy_name} Cumulative Return", fontsize=14, fontweight='bold', pad=8)
            
            # 【修改】动态计算 Y 轴间隔，纳入基准曲线范围，防止基准超出刻度
            y_min = np.floor(min(0, np.nanmin(y_for_range)))
            y_max = np.ceil(np.nanmax(y_for_range))
            y_range = y_max - y_min
            step = max(2, int(y_range / 10)) # 动态确保最多十几个刻度
            yticks = np.arange(start=np.floor(y_min / step) * step, stop=(np.ceil(y_max / step) + 1) * step, step=step)
            ax1.set_yticks(yticks)
            ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda val, pos: f"{int(val)}%"))
            
            ax1.grid(True, linestyle='--', alpha=0.6, color='#cbd5e1')
            ax1.legend(frameon=True, loc='best', fontsize=11)
            
            # --- 下图：买卖金额柱状图 ---
            buy_series = pd.Series(0.0, index=x_dates)
            sell_series = pd.Series(0.0, index=x_dates)
            
            if not df_trans.empty:
                df_trans['日期_dt'] = pd.to_datetime(df_trans['日期'])
                buys = df_trans[df_trans['交易类型'] == '买'].groupby('日期_dt')['成交额'].sum()
                sells = df_trans[df_trans['交易类型'] == '卖'].groupby('日期_dt')['成交额'].sum()
                
                buy_series = x_dates.map(buys).fillna(0)
                sell_series = x_dates.map(sells).fillna(0)

            positions_num = mdates.date2num(x_dates.dt.to_pydatetime())
            bar_width = 0.9   
            
            # ax3.bar(positions_num - bar_width/2, buy_series, width=bar_width, color='#2ca02c', align='center', label='Buy Amount')
            # ax3.bar(positions_num + bar_width/2, -sell_series, width=bar_width, color='#d62728', align='center', label='Sell Amount')
            # 【修改点2】：因为买入是往上画(正)，卖出是往下画(负)，它们不会重叠遮挡。
            # 所以直接画在同一个中间点 (positions_num) 即可，不需要做左右位移，视觉上会更粗壮对齐
            ax3.bar(positions_num, buy_series, width=bar_width, color='#d62728', align='center', label='Buy Amount', alpha=0.85)
            ax3.bar(positions_num, -sell_series, width=bar_width, color='#2ca02c', align='center', label='Sell Amount', alpha=0.85)


            ax3.axhline(0, color='black', linewidth=0.6)
            # 改用英文标签，避免 Linux 缺少中文字体导致方块警告
            ax3.set_ylabel("Amount", fontsize=10)
            ax3.legend(loc='best', fontsize=9)
            ax3.grid(True, linestyle='--', alpha=0.3, axis='y')

            ax3.xaxis_date()
            ax3.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            fig.autofmt_xdate(rotation=30)

            # 移除 plt.tight_layout()，依赖 savefig 的 bbox_inches 即可避免兼容性警告
            plt.savefig(os.path.join(log_dir, 'return_curve_with_trades.png'), dpi=200, bbox_inches='tight')
            plt.close(fig)
            
        print(f"\n>>> 回测报告及图表数据已保存至: {log_dir}")