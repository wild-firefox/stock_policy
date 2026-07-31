import argparse
import pandas as pd
import os
import sys
import time

# 引入根目录配置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SAVE_COLS, get_pro
from data_manager.recalc_qfq import recalc_all_qfq
from data_manager.backtest.custom_indicators import (
    calc_boll,
    calc_kdj,
    calc_ma,
    calc_macd,
    calc_rsi,
)


DAILY_QFQ_COLS = [
    "trade_date",
    "open_qfq", "high_qfq", "low_qfq", "close_qfq",
    "vol", "amount", "turnover_rate_f",
    "ma_qfq_5", "ma_qfq_10", "ma_qfq_20", "ma_qfq_30",
    "ma_qfq_60", "ma_qfq_90", "ma_qfq_250",
    "macd_dif_qfq", "macd_dea_qfq", "macd_qfq",
    "rsi_qfq_6", "rsi_qfq_12", "rsi_qfq_24",
    "boll_upper_qfq", "boll_mid_qfq", "boll_lower_qfq",
    "kdj_k_qfq", "kdj_d_qfq", "kdj_qfq",
]

# 只允许返回项目统一存储字段，防止接口字段命名变化混入结果。
DAILY_QFQ_COLS = [column for column in DAILY_QFQ_COLS if column in SAVE_COLS]
ETF_IDX_DAILY_COLS = [column for column in DAILY_QFQ_COLS if column != "turnover_rate_f"]

def _fetch_dataframe_with_retry(fetcher, retry_timeout: float = 60.0, retry_interval: float = 2.0):
    """接口异常或暂时返回空表时持续重试，达到时间上限后退出。"""
    if retry_timeout < 0:
        raise ValueError("retry_timeout must be greater than or equal to 0")
    if retry_interval <= 0:
        raise ValueError("retry_interval must be greater than 0")

    deadline = time.monotonic() + retry_timeout
    last_error = None

    while True:
        try:
            df = fetcher()
            last_error = None
            if df is not None and not df.empty:
                return df
        except Exception as exc:
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error is not None:
                raise last_error
            return pd.DataFrame()

        time.sleep(min(retry_interval, remaining))


def _fetch_start_date(end_date: str, trade_days: int, warmup_rows: int = 0) -> str:
    """按交易日数量估算一个留有节假日余量的自然日起点。"""
    if trade_days <= 0:
        raise ValueError("trade_days must be greater than 0")

    required_rows = trade_days + warmup_rows
    calendar_days = (required_rows * 7 + 4) // 5 + 60
    return (pd.to_datetime(end_date) - pd.Timedelta(days=calendar_days)).strftime("%Y%m%d")


def _prepare_original_price_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """将ETF/指数原价统一映射为 qfq 列名，并补齐统一技术指标。"""
    df = df.copy()

    # ETF和指数在此工具中不做复权，统一列名仅用于和股票输出结构保持一致。
    for source, target in (
        ("open", "open_qfq"),
        ("high", "high_qfq"),
        ("low", "low_qfq"),
        ("close", "close_qfq"),
    ):
        if source in df.columns:
            df[target] = pd.to_numeric(df[source], errors="coerce")

    # idx_factor_pro 的原价指标通常使用 bfq 后缀，直接复制为统一的 qfq 列名。
    for target in ETF_IDX_DAILY_COLS:
        if "_qfq" not in target or target in df.columns:
            continue
        source = target.replace("_qfq", "_bfq")
        if source in df.columns:
            df[target] = df[source]

    close = df["close_qfq"]
    high = df["high_qfq"]
    low = df["low_qfq"]

    for period in (5, 10, 20, 30, 60, 90, 250):
        column = f"ma_qfq_{period}"
        if column not in df.columns:
            df[column] = calc_ma(close, p=period)

    macd_columns = ("macd_dif_qfq", "macd_dea_qfq", "macd_qfq")
    if not all(column in df.columns for column in macd_columns):
        dif, dea, macd = calc_macd(close, fast=12, slow=26, signal=9)
        df["macd_dif_qfq"] = dif
        df["macd_dea_qfq"] = dea
        df["macd_qfq"] = macd

    for period in (6, 12, 24):
        column = f"rsi_qfq_{period}"
        if column not in df.columns:
            df[column] = calc_rsi(close, p=period)

    boll_columns = ("boll_upper_qfq", "boll_mid_qfq", "boll_lower_qfq")
    if not all(column in df.columns for column in boll_columns):
        boll_mid, boll_upper, boll_lower = calc_boll(close, n=20, k=2)
        df["boll_upper_qfq"] = boll_upper
        df["boll_mid_qfq"] = boll_mid
        df["boll_lower_qfq"] = boll_lower

    kdj_columns = ("kdj_k_qfq", "kdj_d_qfq", "kdj_qfq")
    if not all(column in df.columns for column in kdj_columns):
        kdj_k, kdj_d, kdj_j = calc_kdj(high, low, close, n=9, m1=3, m2=3)
        df["kdj_k_qfq"] = kdj_k
        df["kdj_d_qfq"] = kdj_d
        df["kdj_qfq"] = kdj_j

    for column in ETF_IDX_DAILY_COLS:
        if column not in df.columns:
            df[column] = pd.NA

    return df[ETF_IDX_DAILY_COLS]


def _finalize_daily_window(
    df: pd.DataFrame,
    requested_start: str | None,
    fetch_end: str,
    trade_days: int,
) -> pd.DataFrame:
    """过滤日期、计算原价指标并按看盘习惯返回降序数据。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=ETF_IDX_DAILY_COLS)

    df = df[df["trade_date"] <= fetch_end]
    if df.empty:
        return pd.DataFrame(columns=ETF_IDX_DAILY_COLS)

    df = df.sort_values("trade_date").reset_index(drop=True)
    df = _prepare_original_price_indicators(df)
    if requested_start:
        df = df[df["trade_date"] >= requested_start]
    else:
        df = df.tail(trade_days)

    return df.sort_values("trade_date", ascending=False).reset_index(drop=True)


def get_daily_qfq_with_indicators(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    trade_days: int = 120,
    retry_timeout: float = 60.0,
    retry_interval: float = 2.0,
):
    """
    高频数据工具1: 整合了超过 90% 需求频次的 日线行情、均线、技术指标（MACD/RSI/BOLL）
    start_date 优先；未指定 start_date 时，必须通过 trade_days 指定最近交易日数量。
    内部额外获取指标预热数据，保证 recalc_all_qfq 的计算口径一致。
    """
    if end_date is None:
        raise ValueError("end_date is required")
    if start_date is None and trade_days is None:
        raise ValueError("start_date or trade_days is required")

    fetch_end = end_date.replace('-', '')
    requested_start = start_date.replace('-', '') if start_date else None
    if requested_start:
        fetch_start = _fetch_start_date(start_date, 1, warmup_rows=250)
    else:
        fetch_start = _fetch_start_date(end_date, trade_days, warmup_rows=250)

    # 使用 stk_factor_pro 接口获取核心指标（自带 MACD、RSI、KDJ、BOLL 等）
    df = _fetch_dataframe_with_retry(
        lambda: get_pro().stk_factor_pro(
            ts_code=ts_code,
            start_date=fetch_start,
            end_date=fetch_end,
        ),
        retry_timeout=retry_timeout,
        retry_interval=retry_interval,
    )

    if df is None or df.empty:
        return pd.DataFrame()

    # 防御性排除 end_date 之后的数据，再按升序统一 qfq 口径。
    df = df[df['trade_date'] <= fetch_end]
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values('trade_date').reset_index(drop=True)
    df = recalc_all_qfq(df, decimals=2)
    if requested_start:
        df = df[df['trade_date'] >= requested_start]
    else:
        df = df.tail(trade_days)

    # stk_factor_pro 已包含 SAVE_COLS 中的 qfq 数据，仅截取当前工具需要的字段。
    final_cols = [column for column in DAILY_QFQ_COLS if column in df.columns]
    df = df[final_cols]

    # 将时间降序排回，符合看盘习惯
    df = df.sort_values('trade_date', ascending=False).reset_index(drop=True)

    return df


def get_etf_daily_with_indicators(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    trade_days: int = 120,
    retry_timeout: float = 60.0,
    retry_interval: float = 2.0,
):
    """获取ETF原价日线并统一输出技术指标，不包含 turnover_rate_f。"""
    if end_date is None:
        raise ValueError("end_date is required")
    if start_date is None and trade_days is None:
        raise ValueError("start_date or trade_days is required")

    fetch_end = end_date.replace("-", "")
    requested_start = start_date.replace("-", "") if start_date else None
    fetch_start = (
        _fetch_start_date(start_date, 1, warmup_rows=250)
        if requested_start
        else _fetch_start_date(end_date, trade_days, warmup_rows=250)
    )

    df = _fetch_dataframe_with_retry(
        lambda: get_pro().fund_daily(
            ts_code=ts_code,
            start_date=fetch_start,
            end_date=fetch_end,
        ),
        retry_timeout=retry_timeout,
        retry_interval=retry_interval,
    )
    return _finalize_daily_window(df, requested_start, fetch_end, trade_days)


def get_idx_daily_with_indicators(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    trade_days: int = 120,
    retry_timeout: float = 60.0,
    retry_interval: float = 2.0,
):
    """获取指数 idx_factor_pro 日线并统一输出指标，不包含 turnover_rate_f。"""
    if end_date is None:
        raise ValueError("end_date is required")
    if start_date is None and trade_days is None:
        raise ValueError("start_date or trade_days is required")

    fetch_end = end_date.replace("-", "")
    requested_start = start_date.replace("-", "") if start_date else None
    fetch_start = (
        _fetch_start_date(start_date, 1, warmup_rows=250)
        if requested_start
        else _fetch_start_date(end_date, trade_days, warmup_rows=250)
    )

    df = _fetch_dataframe_with_retry(
        lambda: get_pro().idx_factor_pro(
            ts_code=ts_code,
            start_date=fetch_start,
            end_date=fetch_end,
        ),
        retry_timeout=retry_timeout,
        retry_interval=retry_interval,
    )
    return _finalize_daily_window(df, requested_start, fetch_end, trade_days)


def get_money_flow_data(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    trade_days: int = 30,
    retry_timeout: float = 60.0,
    retry_interval: float = 2.0,
):
    """
    高频数据工具2: 资金流向数据 (出现率 87.2%)
    获取单只股票的每日大单、中单、小单及主力净流入等资金层面数据，
    start_date 优先；未指定 start_date 时，必须通过 trade_days 指定最近交易日数量。
    """
    if end_date is None:
        raise ValueError("end_date is required")
    if start_date is None and trade_days is None:
        raise ValueError("start_date or trade_days is required")

    end_str = end_date.replace('-', '')
    requested_start = start_date.replace('-', '') if start_date else None
    start_str = requested_start or _fetch_start_date(end_date, trade_days)

    df = _fetch_dataframe_with_retry(
        lambda: get_pro().moneyflow(
            ts_code=ts_code,
            start_date=start_str,
            end_date=end_str,
        ),
        retry_timeout=retry_timeout,
        retry_interval=retry_interval,
    )

    if not df.empty:
        df = df[df['trade_date'] <= end_str]
        if requested_start:
            df = df[df['trade_date'] >= requested_start]
            df = df.sort_values('trade_date', ascending=False).reset_index(drop=True)
        else:
            df = df.sort_values('trade_date', ascending=False)
            if 'ts_code' in df.columns:
                df = df.groupby('ts_code', sort=False, group_keys=False).head(trade_days)
            else:
                df = df.head(trade_days)
            df = df.reset_index(drop=True)
    return df


def get_stock_basic_info(
    ts_code: str,
    retry_timeout: float = 60.0,
    retry_interval: float = 2.0,
):
    """
    高频数据工具3: 股票基本面与行业信息 (出现率 58.1%)
    返回股票名称、行业、上市时间、所属市场等基础特征信息。
    """
    return _fetch_dataframe_with_retry(
        lambda: get_pro().stock_basic(
            ts_code=ts_code,
            fields='ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,curr_type,list_status,list_date,delist_date,is_hs',
        ),
        retry_timeout=retry_timeout,
        retry_interval=retry_interval,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview high-frequency Tushare data")
    parser.add_argument("--code", default="600519.SH", help="Tushare stock code")
    parser.add_argument("--etf-code", default="159985.SZ", help="Tushare ETF code")
    parser.add_argument("--idx-code", default="000300.SH", help="Tushare index code")
    parser.add_argument(
        "--end-date",
        default=pd.Timestamp.today().strftime("%Y%m%d"),
        help="End date in YYYYMMDD or YYYY-MM-DD format",
    )
    parser.add_argument("--start-date", default=None, help="Optional start date")
    parser.add_argument("--trade-days", type=int, default=60, help="Rows used when start-date is omitted")
    args = parser.parse_args()

    # print(f"[*] Daily qfq data: {args.code}")
    # daily = get_daily_qfq_with_indicators(
    #     args.code,
    #     start_date=args.start_date,
    #     end_date=args.end_date,
    #     trade_days=args.trade_days,
    # )
    # print(daily.to_markdown(index=False))

    # print(f"\n[*] Money flow data: {args.code}")
    # money_flow = get_money_flow_data(
    #     args.code,
    #     start_date=args.start_date,
    #     end_date=args.end_date,
    #     trade_days=args.trade_days,
    # )
    # print(money_flow.to_markdown(index=False))

    # print(get_stock_basic_info(args.code).to_markdown(index=False))

    print(f"\n[*] ETF original-price data with indicators: {args.etf_code}")
    etf_daily = get_etf_daily_with_indicators(
        args.etf_code,
        start_date=args.start_date,
        end_date=args.end_date,
        trade_days=args.trade_days,
    )
    print(etf_daily.to_markdown(index=False))

    # print(f"\n[*] Index data with indicators: {args.idx_code}")
    # idx_daily = get_idx_daily_with_indicators(
    #     args.idx_code,
    #     start_date=args.start_date,
    #     end_date=args.end_date,
    #     trade_days=args.trade_days,
    # )
    # print(idx_daily.to_markdown(index=False))
