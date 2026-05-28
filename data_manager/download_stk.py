"""
全量下载 — 仅使用 stk_factor_pro 接口

用法:
    python download_all.py
    python download_all.py --start 20200101
    python download_all.py --codes 000001.SZ

增量更新也可以用此功能，直接全量更新。
"""
import os
import sys
import time
import json
import argparse
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    get_pro, init_dirs, setup_logger,
    STK_RAW_DIR, SAVE_COLS,
    API_SLEEP, RATE_LIMIT_SLEEP, MAX_RETRY,
)

log = setup_logger("download")


def get_all_stocks(pro):
    df_l = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,market,list_date')
    ## 回测中退市的也要保存,防止未来函数。
    df_d = pro.stock_basic(exchange='', list_status='D', fields='ts_code,symbol,name,market,list_date,delist_date') # 退市
    
    df = pd.concat([df_l, df_d], ignore_index=True)
    
    # 过滤指定板块，并且强制要求 ts_code 前6位是纯数字
    df = df[df['market'].isin(['主板', '创业板', '科创板', '北交所'])].copy()
    df = df[df['ts_code'].str[:6].str.isdigit()].copy()
    
    return df


def fetch_data(pro, ts_code, start_date, end_date, retry=0):
    """拉取 stk_factor_pro，带频率限制重试"""
    try:
        df = pro.stk_factor_pro(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or len(df) == 0:
            return pd.DataFrame()  # 【修改】正常查结果但没数据，返回空 DataFrame 而不是 None
        hfq_cols = [c for c in df.columns if c.endswith('_hfq')]
        df = df.drop(columns=hfq_cols, errors='ignore')
        df = df.drop(columns=['pre_close'], errors='ignore')
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
    cols = [c for c in SAVE_COLS if c in df.columns]
    out = df[cols].copy()
    symbol = ts_code.split('.')[0]
    out.to_csv(os.path.join(STK_RAW_DIR, f"{symbol}.csv"), index=False)
    return True


from recalc_qfq import recalc_all_qfq as _recalc_all_qfq


def save_incremental(df_new, ts_code, do_recalc=False):
    symbol = ts_code.split('.')[0]
    filepath = os.path.join(STK_RAW_DIR, f"{symbol}.csv")
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
    # 合并后重算所有 qfq 列（锚点 = 合并后的最新 adj_factor）
    if do_recalc:
        decimals = 2
        df_all = _recalc_all_qfq(df_all, decimals)
    save_csv(df_all, ts_code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', type=str, default='20000101')
    parser.add_argument('--end', type=str, default=None)
    parser.add_argument('--codes', nargs='+', default=None) # nargs='+': 接受一个或多个参数，结果是一个列表；default=None: 如果不提供参数，默认为 None
    parser.add_argument('--sleep', type=float, default=None)
    parser.add_argument('--recalc', action='store_true', default=False,
                       help='启用 QFQ 指标重算（默认关闭以加速增量更新）')
    args = parser.parse_args()

    sleep_time = args.sleep if args.sleep is not None else API_SLEEP
    init_dirs()
    pro = get_pro()
    end_date = args.end or datetime.now().strftime('%Y%m%d')

    log.info("=" * 60)
    log.info("开始下载 | start=%s end=%s", args.start, end_date)

    if args.codes:
        stock_list = pd.DataFrame({'ts_code': args.codes})
    else:
        stock_list = get_all_stocks(pro)
    log.info("股票数量: %d", len(stock_list))

    success, skip, fail, warn = 0, 0, 0, 0
    total = len(stock_list)
    t0 = time.time()

    # 跳过阈值：最近 3 天内有数据就算新鲜（覆盖周末/节假日）
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    fresh_threshold = (end_dt - timedelta(days=3)).strftime('%Y%m%d')

    for idx, row in stock_list.iterrows():
        ts_code = row['ts_code']
        symbol = ts_code.split('.')[0]
        filepath = os.path.join(STK_RAW_DIR, f"{symbol}.csv")

        if os.path.exists(filepath):
            df_existing = pd.read_csv(filepath, dtype={'trade_date': str})
            if len(df_existing) > 0:
                max_date = df_existing['trade_date'].max()
                # 数据足够新鲜，跳过
                if max_date >= fresh_threshold:
                    skip += 1
                    continue
                # 增量：只下载缺失的日期
                fetch_start = (datetime.strptime(max_date, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
                log.info("[%d/%d] %s 增量更新 %s→%s ...", idx + 1, total, ts_code, fetch_start, end_date)
                df_new = fetch_data(pro, ts_code, fetch_start, end_date)
                if df_new is not None and len(df_new) > 0:
                    save_incremental(df_new, ts_code, do_recalc=args.recalc)
                    log.info("[%d/%d] %s 增量 %d 条", idx + 1, total, ts_code, len(df_new))
                    success += 1
                else:
                    skip += 1
                continue

        log.info("[%d/%d] %s 下载中 ...", idx + 1, total, ts_code)
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

        # 每 100 只打印进度
        done = success + skip + fail + warn
        if done % 100 == 0:
            elapsed = time.time() - t0
            speed = done / elapsed if elapsed > 0 else 0
            remain = (total - done) / speed if speed > 0 else 0
            log.info("进度: %d/%d (%.1f%%) | 成功 %d 跳过 %d 警告 %d 失败 %d | 剩余约 %.0f 秒",
                     done, total, done / total * 100, success, skip, warn, fail, remain)

        time.sleep(sleep_time)

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("下载完成！成功: %d, 跳过: %d, 警告(无数据): %d, 失败: %d, 耗时: %.0f 秒", success, skip, warn, fail, elapsed)


if __name__ == '__main__':
    main()
