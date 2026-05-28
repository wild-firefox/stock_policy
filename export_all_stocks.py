#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出A股全部上市股票，附带同花顺行业和概念标签
"""
import tushare as ts
import pandas as pd
import os
import time
from config import get_pro

# 导入自定义计算函数
from tushare_tools.tools import get_latest_current_ratio, get_latest_fcf

# ====== 可选项配置 ======
FETCH_CURRENT_RATIO = True  # 是否抓取最新流动比率
FETCH_FCF = True            # 是否抓取最新自由现金流
# ========================

pro = get_pro()

# 1. 获取全部上市股票
print("📥 获取A股上市股票列表...")
stocks = pro.stock_basic(exchange='', list_status='L',
                         fields='ts_code,symbol,name,area,industry,list_date,market,list_status')
print(f"   共 {len(stocks)} 只股票")

# 2. 获取同花顺行业分类
print("📥 获取同花顺行业分类...") # exchange='A' 
ths_industry = pro.ths_index(exchange='A', type='I', fields='ts_code,name')  # type='I' 表示行业分类，'N' 表示概念分类
print(f"   共 {len(ths_industry)} 个行业")

# 3. 获取同花顺概念分类
print("📥 获取同花顺概念分类...")
ths_concept = pro.ths_index(exchange='A', type='N', fields='ts_code,name')
print(f"   共 {len(ths_concept)} 个概念")

# 4. 获取行业-股票映射
print("📥 获取行业成员映射（需分批拉取）...")
ind_members = []
for i, row in ths_industry.iterrows():
    while True:  # 增加重试循环
        try:
            df = pro.ths_member(ts_code=row['ts_code'])
            if df is not None and len(df) > 0:
                df['ind_name'] = row['name']
                ind_members.append(df)
            break  # 获取成功，跳出重试循环，继续下一个行业
        except Exception as e:
            error_msg = str(e)
            print(f"   ⚠️ 行业 {row['ts_code']} {row['name']} 获取失败: {error_msg}")
            if "频率超限" in error_msg:
                print("   ⏳ 触发频次限制，等待 15 秒后重试此行业...")
                time.sleep(15)
            else:
                print("   ⏳ 未知错误，等待 3 秒后重试此行业...")
                time.sleep(3)
                
    if (i + 1) % 50 == 0:
        print(f"   行业进度: {i+1}/{len(ths_industry)}")
        time.sleep(1)  # 增加日常休眠，减少触顶概率

if ind_members:
    ind_all = pd.concat(ind_members, ignore_index=True)
    # 每只股票可能属于多个行业，取第一个（通常是主行业）
    stock_industry = ind_all.drop_duplicates(subset='con_code', keep='first')[['con_code', 'ind_name']]
    stock_industry.columns = ['ts_code', 'ths_industry']
    print(f"   行业覆盖: {len(stock_industry)} 只股票")
else:
    stock_industry = pd.DataFrame(columns=['ts_code', 'ths_industry'])

# 5. 获取概念-股票映射
print("📥 获取概念成员映射（需分批拉取）...")
con_members = []
for i, row in ths_concept.iterrows():
    while True:  # 增加重试循环
        try:
            df = pro.ths_member(ts_code=row['ts_code'])
            if df is not None and len(df) > 0:
                df['concept_name'] = row['name']
                con_members.append(df)
            break  # 获取成功，跳出重试循环，继续下一个概念
        except Exception as e:
            error_msg = str(e)
            print(f"   ⚠️ 概念 {row['ts_code']} {row['name']} 获取失败: {error_msg}")
            if "频率超限" in error_msg:
                print("   ⏳ 触发频次限制，等待 15 秒后重试此概念...")
                time.sleep(15)
            else:
                print("   ⏳ 未知错误，等待 3 秒后重试此概念...")
                time.sleep(3)
                
    if (i + 1) % 50 == 0:
        print(f"   概念进度: {i+1}/{len(ths_concept)}")
        time.sleep(1)  # 增加日常休眠，减少触顶概率

if con_members:
    con_all = pd.concat(con_members, ignore_index=True)
    # 每只股票的概念聚合为逗号分隔字符串
    stock_concept = con_all.groupby('con_code')['concept_name'].apply(lambda x: '|'.join(x.unique())).reset_index()
    stock_concept.columns = ['ts_code', 'ths_concepts']
    print(f"   概念覆盖: {len(stock_concept)} 只股票")
else:
    stock_concept = pd.DataFrame(columns=['ts_code', 'ths_concepts'])

# 6. 获取每日指标（PE/PB/总市值等最新数据）
print("📥 获取最新估值指标...")
today = pd.Timestamp.now().strftime('%Y%m%d')
try:
    # 先找最近一个交易日
    trade_cal = pro.trade_cal(exchange='SSE', start_date=today, end_date=today, fields='cal_date,is_open')
    if trade_cal.empty or trade_cal['is_open'].iloc[0] != 1:
        # 往前找最近交易日
        for d in range(1, 15):
            check_date = (pd.Timestamp.now() - pd.Timedelta(days=d)).strftime('%Y%m%d')
            trade_cal = pro.trade_cal(exchange='SSE', start_date=check_date, end_date=check_date, fields='cal_date,is_open')
            if not trade_cal.empty and trade_cal['is_open'].iloc[0] == 1:
                today = check_date
                break
    print(f"   使用日期: {today}")
    
    # 改用 stk_factor_pro 接口，增加 turnover_rate_f 字段
    factor_data = pro.stk_factor_pro(trade_date=today,
                                     fields='ts_code,pe,pb,total_mv,circ_mv,turnover_rate,turnover_rate_f')
    if len(factor_data) == 0:
        print("   当前交易日可能未收盘或数据未更新，尝试获取前一个交易日数据...")
        # 从今天的前一天开始往前找最近的一个交易日
        today_dt = pd.to_datetime(today)
        for d in range(1, 15):
            check_date = (today_dt - pd.Timedelta(days=d)).strftime('%Y%m%d')
            trade_cal = pro.trade_cal(exchange='SSE', start_date=check_date, end_date=check_date, fields='cal_date,is_open')
            if not trade_cal.empty and trade_cal['is_open'].iloc[0] == 1:
                today = check_date
                print(f"   回退使用日期: {today}")
                factor_data = pro.stk_factor_pro(trade_date=today,
                                                 fields='ts_code,pe,pb,total_mv,circ_mv,turnover_rate,turnover_rate_f')
                break
                
    print(f"   获取 {len(factor_data)} 条估值数据")
except Exception as e:
    print(f"   ⚠️ 估值数据获取失败: {e}")
    factor_data = pd.DataFrame(columns=['ts_code', 'pe', 'pb', 'total_mv', 'circ_mv', 'turnover_rate', 'turnover_rate_f'])


# ====== 获取可选指标 ======
if FETCH_CURRENT_RATIO:
    print("📥 获取最新流动比率 (Current Ratio)...")
    cr_counts, cr_df = get_latest_current_ratio(pro, today)
    if not cr_df.empty:
        # 重命名 end_date 防混淆
        cr_df = cr_df[['ts_code', 'end_date', 'current_ratio']].rename(columns={'end_date': 'cr_end_date'})
    else:
        cr_df = pd.DataFrame(columns=['ts_code', 'cr_end_date', 'current_ratio'])

if FETCH_FCF:
    print("📥 获取最新自由现金流 (FCF)...")
    fcf_counts, fcf_df = get_latest_fcf(pro, today)
    if not fcf_df.empty:
        # 重命名 end_date 防混淆
        fcf_df = fcf_df[['ts_code', 'end_date', 'fcf']].rename(columns={'end_date': 'fcf_end_date'})
    else:
        fcf_df = pd.DataFrame(columns=['ts_code', 'fcf_end_date', 'fcf'])

# 7. 合并所有数据
print("🔗 合并数据...")
result = stocks[['ts_code', 'symbol', 'name', 'area', 'industry', 'market', 'list_date']].copy()
result = result.merge(stock_industry, on='ts_code', how='left') # 行业
result = result.merge(stock_concept, on='ts_code', how='left') # 概念
result = result.merge(factor_data[['ts_code', 'pe', 'pb', 'total_mv', 'circ_mv', 'turnover_rate', 'turnover_rate_f']], 
                       on='ts_code', how='left')

# 重新排列列顺序
cols = ['ts_code', 'symbol', 'name', 'market', 'area', 'industry', 'ths_industry', 'ths_concepts',
        'pe', 'pb', 'total_mv', 'circ_mv', 'turnover_rate', 'turnover_rate_f', 'list_date']

# 合并可选指标列
if FETCH_CURRENT_RATIO:
    result = result.merge(cr_df, on='ts_code', how='left')
    cols.extend(['cr_end_date', 'current_ratio'])

if FETCH_FCF:
    result = result.merge(fcf_df, on='ts_code', how='left')
    cols.extend(['fcf_end_date', 'fcf'])

result = result[cols]

# 8. 输出
output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'a_shares_list.csv')
result.to_csv(output_path, index=False, encoding='utf-8-sig')
print(f"\n✅ 导出完成: {output_path}")
print(f"   总行数: {len(result)}")
print(f"   列: {list(result.columns)}")
print(f"\n前5行预览:")
print(result.head().to_string(index=False))
