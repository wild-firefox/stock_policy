#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出全市场 ETF 列表，过滤掉场外基金，仅保留 SH 和 SZ 结尾的场内 ETF
"""
import pandas as pd
import os
from config import get_pro, PROJECT_ROOT

def export_etfs():
    pro = get_pro()
    print("📥 获取全市场 ETF 列表...")
    
    try:
        # 获取上市状态的不同 ETF
        df_l = pro.etf_basic(list_status='L')  # 上市
        
        # 仅保留 SH 和 SZ 结尾的场内 ETF，并且长度为 9 (如: 510300.SH)
        df_filtered = df_l[
            (df_l['ts_code'].str.endswith(('.SH', '.SZ'))) & 
            (df_l['ts_code'].str.len() == 9)
        ].copy()
        
        # 重新排序并可选择性保留重要的列
        cols = ['ts_code', 'csname', 'extname', 'cname', 'index_code', 'index_name','list_date','exchange']
        # 确保列存在
        cols = [c for c in cols if c in df_filtered.columns]
        df_filtered = df_filtered[cols]

        print(f"   共获取到 {len(df_filtered)} 只场内 ETF")
        
        # 导出为 csv
        export_path = PROJECT_ROOT / 'etf_list.csv'
        df_filtered.to_csv(export_path, index=False, encoding='utf-8-sig')
        
        print(f"✅ 导出完成: {export_path}")
        print(f"   前 5 行预览:\n{df_filtered.head().to_string(index=False)}")
        
    except Exception as e:
        print(f"⚠️ 导出 ETF 列表失败: {e}")

if __name__ == "__main__":
    export_etfs()
