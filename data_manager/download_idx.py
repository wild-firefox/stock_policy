"""
全量下载指数数据 — 使用 idx_factor_pro 接口

用法:
    python download_idx.py
    python download_idx.py --start 20000101
    python download_idx.py --codes 000300.SH 000001.SH
"""
import os
import sys
import time
import argparse
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    get_pro, init_dirs, setup_logger,
    IDX_RAW_DIR, SAVE_COLS,
    API_SLEEP, RATE_LIMIT_SLEEP, MAX_RETRY,
)

log = setup_logger("download_idx")

def get_all_indices(pro):
    """
    按照用户要求，默认通过某天（如2018-10-18）的 index_dailybasic 截面获取大盘常用指数列表
    如果需要全市场所有指数，可以使用 pro.index_basic()
    """
    try:
        # 取一天固定截面获取主要的指数列表
        df = pro.index_dailybasic(trade_date='20181018', fields='ts_code,trade_date,turnover_rate,pe')
        if df is not None and not df.empty:
            return pd.DataFrame({'ts_code': df['ts_code'].unique()})
    except Exception as e:
        log.error("获取指数列表失败: %s", e)
        
    # 如果接口失败，兜底返回常见宽基指数
    fallback_codes = [
        '000001.SH', '000300.SH', '000905.SH', '399001.SZ', 
        '399005.SZ', '399006.SZ', '399016.SZ', '399300.SZ', 
        '000005.SH', '000006.SH', '000016.SH', '399905.SZ'
    ]
    return pd.DataFrame({'ts_code': fallback_codes})

def fetch_data(pro, ts_code, start_date, end_date, retry=0):
    """拉取 idx_factor_pro，带频率限制重试"""
    try:
        df = pro.idx_factor_pro(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or len(df) == 0:
            return pd.DataFrame()
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
    # 过滤字段，如果没有配置特别的保存列则全量保存
    cols = [c for c in SAVE_COLS if c in df.columns] if SAVE_COLS else df.columns.tolist()
    out = df[cols].copy()
    symbol = ts_code.split('.')[0]
    out.to_csv(os.path.join(IDX_RAW_DIR, f"idx_{symbol}.csv"), index=False)
    return True

def save_incremental(df_new, ts_code):
    """
    指数没有复权概念，直接合并去重即可
    """
    symbol = ts_code.split('.')[0]
    filepath = os.path.join(IDX_RAW_DIR, f"idx_{symbol}.csv")
    
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
    
    init_dirs()
    # 必须确保指数数据存储目录存在
    os.makedirs(IDX_RAW_DIR, exist_ok=True)
    
    pro = get_pro()
    end_date = args.end or datetime.now().strftime('%Y%m%d')

    log.info("=" * 60)
    log.info("开始下载指数数据 | start=%s end=%s", args.start, end_date)

    if args.codes:
        idx_list = pd.DataFrame({'ts_code': args.codes})
    else:
        idx_list = get_all_indices(pro)
        
    log.info("需处理指数数量: %d", len(idx_list))

    success, skip, fail, warn = 0, 0, 0, 0
    total = len(idx_list)
    t0 = time.time()

    end_dt = datetime.strptime(end_date, '%Y%m%d')
    fresh_threshold = (end_dt - timedelta(days=3)).strftime('%Y%m%d')

    for idx, row in idx_list.iterrows():
        ts_code = row['ts_code']
        symbol = ts_code.split('.')[0]
        filepath = os.path.join(IDX_RAW_DIR, f"idx_{symbol}.csv")

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
                
                if df_new is not None and len(df_new) > 0:
                    save_incremental(df_new, ts_code)
                    log.info("[%d/%d] %s 增量 %d 条", idx + 1, total, ts_code, len(df_new))
                    success += 1
                else:
                    skip += 1
                continue

        log.info("[%d/%d] %s 全量下载中 ...", idx + 1, total, ts_code)
        df = fetch_data(pro, ts_code, args.start, end_date)

        if df is not None and len(df) > 0:
            if save_csv(df, ts_code):
                log.info("[%d/%d] %s OK (%d 条)", idx + 1, total, ts_code, len(df))
                success += 1
            else:
                log.error("[%d/%d] %s 保存失败", idx + 1, total, ts_code)
                fail += 1
        else:
            log.warning("[%d/%d] %s 无数据", idx + 1, total, ts_code)
            warn += 1

        time.sleep(sleep_time)

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("指数下载完成！成功: %d, 跳过: %d, 警告(无数据): %d, 失败: %d, 耗时: %.0f 秒", success, skip, warn, fail, elapsed)

if __name__ == '__main__':
    main()