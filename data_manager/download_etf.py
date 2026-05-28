import os
import sys
import time
import json
import argparse
import pandas as pd
from datetime import datetime, timedelta

# 修改引入根目录配置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    get_pro, setup_logger, ETF_RAW_DIR,
    API_SLEEP, RATE_LIMIT_SLEEP, MAX_RETRY, SAVE_ETF_COLS
)

log = setup_logger("download_etf")

def get_etf_list(pro):
    """获取所有 ETF 列表 (包含上市和退市)"""
    try:
        df_d = pro.etf_basic(list_status='D') 
        df_l = pro.etf_basic(list_status='L')
        df_all = pd.concat([df_d, df_l], ignore_index=True)
        # 仅保留 SH 和 SZ 结尾的场内 ETF，并且符号部分为严格的6位数字
        df_filtered = df_all[ 
            (df_all['ts_code'].str.endswith(('.SH', '.SZ'))) & 
            (df_all['ts_code'].str.len() == 9) &
            (df_all['ts_code'].str[:6].str.isdigit())
        ].copy()
        return df_filtered
    except Exception as e:
        log.error(f"获取 ETF 列表失败: {e}")
        return pd.DataFrame()

def fetch_data(pro, ts_code, start_date, end_date, retry=0):
    """同时拉取 fund_daily 和 fund_adj，并进行合并"""
    try:
        # 1. 获取日线不复权数据
        df_daily = pro.fund_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df_daily is None or len(df_daily) == 0:
            return pd.DataFrame()
            
        # 筛选所需列
        daily_cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'change', 'pct_chg', 'vol', 'amount']
        df_daily = df_daily[[c for c in daily_cols if c in df_daily.columns]]

        # 2. 获取每日复权因子
        time.sleep(API_SLEEP) # 防止超频
        df_adj = pro.fund_adj(ts_code=ts_code, start_date=start_date, end_date=end_date)
        
        # 3. 合并数据
        if df_adj is not None and not df_adj.empty:
            df_adj = df_adj[['ts_code', 'trade_date', 'adj_factor']]
            df = pd.merge(df_daily, df_adj, on=['ts_code', 'trade_date'], how='left')
            
            # 将 adj_factor 列缺失的地方均补上第一个有 adj_factor 值的日期的 adj_factor 值
            first_valid_val = df['adj_factor'].dropna().iloc[0] if not df['adj_factor'].dropna().empty else 1.0
            df['adj_factor'] = df['adj_factor'].fillna(first_valid_val)
        else:
            # 如果没有查到复权因子，默认给 1.0 (某些刚上市或无因子的基金)
            df = df_daily.copy()
            df['adj_factor'] = 1.0
            
        return df.sort_values('trade_date').reset_index(drop=True)
        
    except Exception as e:
        err = str(e)
        if '频率' in err or 'limit' in err.lower() or 'freq' in err.lower() or '次数' in err:
            if retry < MAX_RETRY:
                log.warning("%s 触发频率限制，第 %d 次重试，等待 %ds ...", ts_code, retry + 1, RATE_LIMIT_SLEEP)
                time.sleep(RATE_LIMIT_SLEEP)
                return fetch_data(pro, ts_code, start_date, end_date, retry + 1)
            else:
                log.error("%s 重试 %d 次仍失败: %s", ts_code, MAX_RETRY, err)
        else:
            log.error("%s 异常: %s", ts_code, err)
        return None

def save_csv(df, ts_code):
    if df is None or len(df) == 0:
        return False
    cols = [c for c in SAVE_ETF_COLS if c in df.columns]
    out = df[cols].copy()
    symbol = ts_code.split('.')[0]
    out.to_csv(os.path.join(ETF_RAW_DIR, f"{symbol}.csv"), index=False)
    return True

def save_incremental(df_new, ts_code):
    symbol = ts_code.split('.')[0]
    filepath = os.path.join(ETF_RAW_DIR, f"{symbol}.csv")
    
    if os.path.exists(filepath):
        df_old = pd.read_csv(filepath, dtype={'trade_date': str})
        if df_new is not None and len(df_new) > 0:
            df_new['trade_date'] = df_new['trade_date'].astype(str)
            df_all = pd.concat([df_old, df_new], ignore_index=True)
            df_all = df_all.drop_duplicates(subset=['trade_date'], keep='last')
            df_all = df_all.sort_values('trade_date').reset_index(drop=True)
        else:
            df_all = df_old
    else:
        df_all = df_new
        
    save_csv(df_all, ts_code)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', type=str, default='20000101')
    parser.add_argument('--end', type=str, default=None)
    parser.add_argument('--codes', nargs='+', default=None)
    parser.add_argument('--sleep', type=float, default=None)
    args = parser.parse_args()

    sleep_time = args.sleep if args.sleep is not None else API_SLEEP
    pro = get_pro()
    end_date = args.end or datetime.now().strftime('%Y%m%d')

    log.info("=" * 60)
    log.info("开始下载 ETF | start=%s end=%s", args.start, end_date)

    if args.codes:
        ts_codes = args.codes
    else:
        df_etf = get_etf_list(pro)
        ts_codes = df_etf['ts_code'].tolist()
        
    log.info("拟处理 ETF 数量: %d", len(ts_codes))
    
    success, skip, fail, warn = 0, 0, 0, 0
    total = len(ts_codes)
    t0 = time.time()

    end_dt = datetime.strptime(end_date, '%Y%m%d')
    fresh_threshold = (end_dt - timedelta(days=3)).strftime('%Y%m%d')

    for idx, ts_code in enumerate(ts_codes):
        symbol = ts_code.split('.')[0]
        filepath = os.path.join(ETF_RAW_DIR, f"{symbol}.csv")

        # 增量判断
        if os.path.exists(filepath):
            df_existing = pd.read_csv(filepath, dtype={'trade_date': str})
            if len(df_existing) > 0:
                max_date = df_existing['trade_date'].max()
                if max_date >= fresh_threshold:
                    skip += 1
                    continue
                
                fetch_start = (datetime.strptime(max_date, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
                log.info("[%d/%d] %s 增量更新 %s→%s ...", idx + 1, total, ts_code, fetch_start, end_date)
                df_new = fetch_data(pro, ts_code, fetch_start, end_date)
                
                if df_new is not None and not df_new.empty:
                    save_incremental(df_new, ts_code)
                    log.info("[%d/%d] %s 增量 %d 条", idx + 1, total, ts_code, len(df_new))
                    success += 1
                else:
                    skip += 1
                continue

        log.info("[%d/%d] %s 全量下载中 ...", idx + 1, total, ts_code)
        df = fetch_data(pro, ts_code, args.start, end_date)

        if df is not None and not df.empty:
            if save_csv(df, ts_code):
                log.info("[%d/%d] %s OK (%d 条)", idx + 1, total, ts_code, len(df))
                success += 1
            else:
                log.error("[%d/%d] %s 保存失败", idx + 1, total, ts_code)
                fail += 1
        else:
            log.warning("[%d/%d] %s 无数据", idx + 1, total, ts_code)
            warn += 1

        done = success + skip + fail + warn
        if done % 50 == 0:
            elapsed = time.time() - t0
            speed = done / elapsed if elapsed > 0 else 0
            remain = (total - done) / speed if speed > 0 else 0
            log.info("进度: %d/%d (%.1f%%) | 成功 %d 跳过 %d 警告 %d 失败 %d | 剩余约 %.0f 秒",
                     done, total, (done / total) * 100, success, skip, warn, fail, remain)

        time.sleep(sleep_time)

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("ETF 下载完成！成功: %d, 跳过: %d, 警告(无数据): %d, 失败: %d, 耗时: %.0f 秒", success, skip, warn, fail, elapsed)

if __name__ == '__main__':
    main()