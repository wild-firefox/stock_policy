import os
import sys
import pandas as pd
import glob
import gc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) # 添加项目根目录 stock_policy (用于导入 config)
from config import STK_RAW_DIR, STK_RAW_H5_DIR, BACKTEST_BASE, is_excluded



EXCLUDE_BOARDS = ['gem', 'star', 'bj']


def build():
    print(f"开始分批写入 {STK_RAW_DIR} 下的 csv 文件到 HDF5...")
    csv_files = glob.glob(os.path.join(STK_RAW_DIR, "*.csv"))

    if not csv_files:
        print("未找到任何 CSV 文件！请确保 STK_RAW_DIR 路径正确且包含数据。")
        return

    # 1. 找到筛选后的第一个文件，提取最早和最晚的交易日期
    first_valid_file = None
    for f in csv_files:
        symbol = os.path.basename(f).replace('.csv', '')
        if not is_excluded(symbol, EXCLUDE_BOARDS):
            first_valid_file = f
            break
            
    if not first_valid_file:
        print("未找到任何符合筛选条件的 CSV 文件！")
        return

    print(f"正在读取第一个筛选后文件 {first_valid_file} 以确定数据日期范围...")
    df_sample = pd.read_csv(first_valid_file, usecols=['trade_date'], dtype={'trade_date': str})
    if df_sample.empty:
        print(f"文件 {first_valid_file} 中未找到有效的 trade_date 列！")
        return
        
    df_sample['trade_date'] = df_sample['trade_date'].str.replace('-', '')
    min_date = df_sample['trade_date'].min()
    max_date = df_sample['trade_date'].max()
    print(f"数据日期范围: {min_date} ~ {max_date}")
    
    data_time = f"{max_date}" # 拿最新日期当文件名

    # 2. 生成带日期时间范围的 HDF5 保存路径
    OUTPUT_HDF = os.path.join(STK_RAW_H5_DIR, data_time, "market_data.h5")
    
    os.makedirs(os.path.dirname(OUTPUT_HDF), exist_ok=True)
    if os.path.exists(OUTPUT_HDF):
        os.remove(OUTPUT_HDF)
        
    total_files = len(csv_files)
    total_excluded = 0
    
    batch_size = 500
    for i in range(0, len(csv_files), batch_size):
        batch = csv_files[i:i+batch_size]
        df_list = []
        batch_excluded = 0
        for f in batch:
            symbol = os.path.basename(f).replace('.csv', '')
            
            # 板块过滤
            if is_excluded(symbol,EXCLUDE_BOARDS):
                batch_excluded += 1
                total_excluded += 1
                continue
                
            try:
                # 动态匹配 CSV 里实际含有的列，防报错
                with open(f, 'r', encoding='utf-8') as file_obj:
                    header = [c.strip('"') for c in file_obj.readline().strip().split(',')]
                valid_cols = [c for c in header if c in BACKTEST_BASE]
                
                if not valid_cols:
                    continue
                    
                df = pd.read_csv(f, usecols=valid_cols, dtype={'trade_date': str})
                if df.empty: 
                    continue
                # 使用 .copy() 去除 pandas 读取大量列时产生的内存碎片
                df = df.copy()
                df['ts_code'] = symbol
                df_list.append(df)
            except Exception as e:
                print(f"读取 {symbol} 失败: {e}")
            
        print(f"合并第 {i} 到 {min(i+len(batch), total_files)} 个文件... (本批过虑: {batch_excluded} 个)")
        if not df_list: 
            continue
            
        # 合并并使用 .copy() 防止产生内存碎片
        all_data = pd.concat(df_list, ignore_index=True).copy()
        
        # 强制回收小表
        del df_list
        gc.collect()
        
        # 去除横杠转换为 YYYYMMDD 格式
        all_data['trade_date'] = all_data['trade_date'].str.replace('-', '')
        
        all_data.set_index(['trade_date', 'ts_code'], inplace=True)
        
        # 统一将不需要精度的 float64 转换为 float32 降维处理，节约 HDF5 容量
        float_cols = all_data.select_dtypes(include=['float64']).columns
        all_data[float_cols] = all_data[float_cols].astype('float32')
        
        # 防止碎片化重组
        all_data = all_data.copy()
        
        all_data.to_hdf(OUTPUT_HDF, key='data', mode='a', format='table', append=True)
        
    print("-" * 50)
    print(f"数据 HDF5 文件构建成功！")
    print(f"原文件总数: {total_files}")
    print(f"总计过滤数: {total_excluded}")
    print(f"实际加载数: {total_files - total_excluded}")
    print("-" * 50)

if __name__ == '__main__':
    build()