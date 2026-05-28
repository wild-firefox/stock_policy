
import pandas as pd
import time
import os
import tushare as ts
import sys

# 修改引入根目录配置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_pro


def get_latest_current_ratio(pro, target_date: str):
    """
    获取全市场主板股票在 target_date 视角下的最新流动比率 (Current Ratio)
    """
    # 1. 获取主板股票列表
    df_basic = pro.stock_basic(list_status='L', fields='ts_code,market')
    main_board_codes = df_basic[df_basic['market'] == '主板']['ts_code'].tolist()
    
    # 2. 动态计算财报季末日期
    target_dt = pd.to_datetime(target_date)
    periods = pd.date_range(end=target_dt, periods=2, freq='QE').strftime('%Y%m%d').tolist()
    periods = sorted(periods, reverse=True) 

    bs_list = []
    print(f"正在拉取资产负债表 ({target_date} 视角)，涉猎财报期: {periods}")
    for p in periods:
        try:
            df_bs = pro.balancesheet_vip(period=p, fields='ts_code,end_date,f_ann_date,total_cur_assets,total_cur_liab')
            # 过滤掉还未公布的财报 (防止未来函数)
            if not df_bs.empty:
                df_bs = df_bs[df_bs['f_ann_date'] <= target_date]
            bs_list.append(df_bs)
        except Exception as e:
            print(f"获取 {p} 资产负债表失败: {e}")
            
    if not bs_list:
        return pd.DataFrame(), pd.DataFrame()

    df_all_bs = pd.concat(bs_list, ignore_index=True)
    
    # 3. 数据清洗与防未来函数过滤
    df_bs_view = df_all_bs.dropna(subset=['f_ann_date']).copy()
    df_bs_view = df_bs_view[df_bs_view['f_ann_date'] <= target_date]
    df_bs_view = df_bs_view[df_bs_view['ts_code'].isin(main_board_codes)]
    
    # 4. 提取最新一期数据
    df_latest = df_bs_view.sort_values(by=['ts_code', 'end_date'], ascending=[True, False]).drop_duplicates(subset=['ts_code'], keep='first')
    
    # 5. 计算 Current Ratio
    df_latest['current_ratio'] = df_latest.apply(
        lambda x: round(x['total_cur_assets'] / x['total_cur_liab'], 4) 
        if pd.notna(x['total_cur_liab']) and x['total_cur_liab'] != 0 else None, 
        axis=1
    )
    
    # 统计 end_date 的分布
    date_counts = df_latest['end_date'].value_counts().reset_index()
    date_counts.columns = ['end_date', 'stock_count']
    return date_counts, df_latest[['ts_code', 'end_date', 'f_ann_date', 'total_cur_assets', 'total_cur_liab', 'current_ratio']]


def get_latest_fcf(pro, target_date: str):
    """
    获取全市场主板股票在 target_date 视角下的最新自由现金流 (FCF)
    """
    # 1. 获取主板股票列表
    df_basic = pro.stock_basic(list_status='L', fields='ts_code,market')
    main_board_codes = df_basic[df_basic['market'] == '主板']['ts_code'].tolist()
    
    # 2. 动态计算财报季末日期
    target_dt = pd.to_datetime(target_date)
    periods = pd.date_range(end=target_dt, periods=2, freq='QE').strftime('%Y%m%d').tolist()
    periods = sorted(periods, reverse=True) 

    cf_list = []
    print(f"正在拉取现金流量表 ({target_date} 视角)，涉猎财报期: {periods}")
    for p in periods:
        try:
            df_cf = pro.cashflow_vip(period=p, fields='ts_code,end_date,f_ann_date,n_cashflow_act,c_pay_acq_const_fiolta')
            # 过滤掉还未公布的财报 (防止未来函数)
            if not df_cf.empty:
                df_cf = df_cf[df_cf['f_ann_date'] <= target_date]
            cf_list.append(df_cf)
        except Exception as e:
            print(f"获取 {p} 现金流量表失败: {e}")
            
    if not cf_list:
        return pd.DataFrame(), pd.DataFrame()

    df_all_cf = pd.concat(cf_list, ignore_index=True)
    
    # 3. 数据清洗与防未来函数过滤
    df_cf_view = df_all_cf.dropna(subset=['f_ann_date']).copy()
    df_cf_view = df_cf_view[df_cf_view['f_ann_date'] <= target_date]
    df_cf_view = df_cf_view[df_cf_view['ts_code'].isin(main_board_codes)]
    
    # 4. 提取最新一期数据
    df_latest = df_cf_view.sort_values(by=['ts_code', 'end_date'], ascending=[True, False]).drop_duplicates(subset=['ts_code'], keep='first')
    
    # 5. 计算 FCF
    # 填充空值为0以便计算，或者根据需求保留NaN
    df_latest['n_cashflow_act'] = df_latest['n_cashflow_act'].fillna(0)
    df_latest['c_pay_acq_const_fiolta'] = df_latest['c_pay_acq_const_fiolta'].fillna(0)
    df_latest['fcf'] = df_latest['n_cashflow_act'] - df_latest['c_pay_acq_const_fiolta']
    
    # 统计 end_date 的分布
    date_counts = df_latest['end_date'].value_counts().reset_index()
    date_counts.columns = ['end_date', 'stock_count']
    print(f"FCF 数据分布:\n{date_counts}")
    return date_counts, df_latest[['ts_code', 'end_date', 'f_ann_date', 'n_cashflow_act', 'c_pay_acq_const_fiolta', 'fcf']]


# ====== 测试逻辑 ======

if __name__ == "__main__":
    test_date = '20260528'
    pro = get_pro()


    # 1. 测试 Current Ratio
    print("\n--- 获取 Current Ratio ---")
    cr_counts, cr_df = get_latest_current_ratio(pro, test_date)
    if not cr_counts.empty:
        print(f"CR 覆盖股票数: {cr_counts['stock_count'].sum()}")
        print(cr_df[['ts_code', 'end_date', 'current_ratio']].head(3))

    # 2. 测试 FCF
    print("\n--- 获取 FCF ---")
    fcf_counts, fcf_df = get_latest_fcf(pro, test_date)
    if not fcf_counts.empty:
        print(f"FCF 覆盖股票数: {fcf_counts['stock_count'].sum()}")
        print(fcf_df[['ts_code', 'end_date', 'fcf']].head(3))
