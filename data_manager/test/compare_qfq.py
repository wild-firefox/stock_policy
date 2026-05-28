"""
对比三种 QFQ 计算方式与 API 原始值的差异

方法: 1=API原始  2=recalc_all_qfq(bfq*ratio缩放)  3=recalc_qfq_by_formula(公式重算)
随机 3 只主板股票，对比 2 vs 1 和 3 vs 1 的误差。
"""
import sys
import os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import get_pro
from data_manager.recalc_qfq import recalc_all_qfq, recalc_qfq_by_formula


def pick_fixed_stocks():
    """固定 3 只主板股票，保证每次对比结果可复现"""
    return ['603578.SH', '600064.SH', '000520.SZ']


def fetch_raw(pro, ts_code):
    """拉取 API 原始数据（不做任何 QFQ 重算），返回按 trade_date 排序的 DataFrame"""
    df = pro.stk_factor_pro(ts_code=ts_code, start_date='20000101', end_date='20260528')
    if df is None or len(df) == 0:
        return None
    # 删掉不需要的列
    hfq_cols = [c for c in df.columns if c.endswith('_hfq')]
    df = df.drop(columns=hfq_cols + ['pre_close'], errors='ignore')
    return df.sort_values('trade_date').reset_index(drop=True)


def calc_error(df_ref: pd.DataFrame, df_test: pd.DataFrame, qfq_cols: list):
    """计算 df_test 相对 df_ref 在每列 qfq 上的 MAE / MaxAE / RelErr%"""
    results = {}
    for col in qfq_cols:
        if col not in df_ref.columns or col not in df_test.columns:
            continue
        ref = pd.to_numeric(df_ref[col], errors='coerce')
        tst = pd.to_numeric(df_test[col], errors='coerce')
        mask = ref.notna() & tst.notna()
        if mask.sum() == 0:
            continue
        diff = (tst - ref).abs()
        mae = diff[mask].mean()
        max_ae = diff[mask].max()
        ref_mean = ref[mask].abs().mean()
        rel_pct = (mae / ref_mean * 100) if ref_mean > 1e-9 else float('nan')
        results[col] = {'MAE': mae, 'MaxAE': max_ae, 'RelErr%': rel_pct}
    return results


# ── 方法4：从 QFQ 价格用标准公式重算"不变型"指标 ──
def _wilder_smooth(series, period):
    """Wilder平滑: today = prev*(n-1)/n + raw/n, 等价 ewm(alpha=1/n)"""
    return series.ewm(alpha=1 / period, adjust=False).mean()


def compute_invariant_from_qfq(df):
    """
    从 QFQ 价格用标准公式计算所有"不变型"指标。
    返回 dict: {qfq_col_name: pd.Series}
    不修改 df，仅返回计算结果。对于无法公式化的指标（OBV/ASI/MFI），返回空。
    """
    res = {}
    c = df['close_qfq']
    h = df['high_qfq']
    l = df['low_qfq']
    o = df['open_qfq']
    v = df['vol']

    # ── KDJ (9, 3, 3) ──
    low_9 = l.rolling(9, min_periods=1).min()
    high_9 = h.rolling(9, min_periods=1).max()
    denom = (high_9 - low_9).replace(0, np.nan)
    rsv = ((c - low_9) / denom * 100).clip(0, 100)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    res['kdj_k_qfq'] = k
    res['kdj_d_qfq'] = d
    res['kdj_qfq'] = 3 * k - 2 * d

    # ── RSI (6, 12, 24) — Wilder's smoothing ──
    delta = c.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    for n in [6, 12, 24]:
        avg_up = _wilder_smooth(up, n)
        avg_down = _wilder_smooth(down, n)
        rs = (avg_up / avg_down.replace(0, np.nan)).fillna(0)
        res[f'rsi_qfq_{n}'] = (100 - 100 / (1 + rs)).clip(0, 100)

    # ── CCI (14) ──
    tp = (h + l + c) / 3
    ma_tp = tp.rolling(14, min_periods=1).mean()
    md = tp.rolling(14, min_periods=1).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    res['cci_qfq'] = ((tp - ma_tp) / (0.015 * md)).replace([np.inf, -np.inf], np.nan)

    # ── WR (10) / WR1 (6) ──
    for n, col in [(10, 'wr_qfq'), (6, 'wr1_qfq')]:
        h_n = h.rolling(n, min_periods=1).max()
        l_n = l.rolling(n, min_periods=1).min()
        res[col] = ((h_n - c) / (h_n - l_n).replace(0, np.nan) * 100)

    # ── BIAS (6, 12, 24) ──
    for n, col in [(6, 'bias1_qfq'), (12, 'bias2_qfq'), (24, 'bias3_qfq')]:
        ma = c.rolling(n, min_periods=1).mean()
        res[col] = (c - ma) / ma.replace(0, np.nan) * 100

    # ── ROC (12) / MAROC (6) ──
    roc = (c - c.shift(12)) / c.shift(12).replace(0, np.nan) * 100
    res['roc_qfq'] = roc
    res['maroc_qfq'] = roc.rolling(6, min_periods=1).mean()

    # ── PSY (12) / PSYMA (6) ──
    psy = c.diff().gt(0).rolling(12, min_periods=1).sum() / 12 * 100
    res['psy_qfq'] = psy
    res['psyma_qfq'] = psy.rolling(6, min_periods=1).mean()

    # ── VR (26) ──
    uv = v.where(c.diff() > 0, 0).rolling(26, min_periods=1).sum()
    dv = v.where(c.diff() < 0, 0).rolling(26, min_periods=1).sum()
    res['vr_qfq'] = (uv / dv.replace(0, np.nan) * 100)

    # ── DMI (14, 6) — Wilder's DMI ──
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr_s = _wilder_smooth(tr, 14)

    pdm_raw = h.diff()
    mdm_raw = l.diff().mul(-1)
    pdm = pdm_raw.where((pdm_raw > 0) & (pdm_raw > mdm_raw), 0)
    mdm = mdm_raw.where((mdm_raw > 0) & (mdm_raw > pdm_raw), 0)

    pdi = _wilder_smooth(pdm, 14) / atr_s.replace(0, np.nan) * 100
    mdi = _wilder_smooth(mdm, 14) / atr_s.replace(0, np.nan) * 100
    dx = ((pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100)
    adx = _wilder_smooth(dx, 6)
    res['dmi_pdi_qfq'] = pdi
    res['dmi_mdi_qfq'] = mdi
    res['dmi_adx_qfq'] = adx
    res['dmi_adxr_qfq'] = (adx + adx.shift(6)) / 2

    # ── BRAR (26) ──
    ar_num = (h - o).rolling(26, min_periods=1).sum()
    ar_den = (o - l).rolling(26, min_periods=1).sum()
    res['brar_ar_qfq'] = (ar_num / ar_den.replace(0, np.nan) * 100)

    prev_c = c.shift(1)
    br_num = (h - prev_c).clip(lower=0).rolling(26, min_periods=1).sum()
    br_den = (prev_c - l).clip(lower=0).rolling(26, min_periods=1).sum()
    res['brar_br_qfq'] = (br_num / br_den.replace(0, np.nan) * 100)

    # ── CR (26) — MID = (H+L+O+C)/4 ──
    mid = (h + l + o + c) / 4
    prev_mid = mid.shift(1)
    cr_num = (h - prev_mid).clip(lower=0).rolling(26, min_periods=1).sum()
    cr_den = (prev_mid - l).clip(lower=0).rolling(26, min_periods=1).sum()
    res['cr_qfq'] = (cr_num / cr_den.replace(0, np.nan) * 100)

    # ── TRIX (12) / TRMA (20) ──
    tr = c.ewm(span=12, adjust=False).mean() \
          .ewm(span=12, adjust=False).mean() \
          .ewm(span=12, adjust=False).mean()
    trix = (tr - tr.shift(1)) / tr.shift(1).replace(0, np.nan) * 100
    res['trix_qfq'] = trix
    res['trma_qfq'] = trix.rolling(20, min_periods=1).mean()

    # ── MASS (25, 9) ──
    hl = h - l
    ema1 = hl.ewm(span=9, adjust=False).mean()
    ema2 = ema1.ewm(span=9, adjust=False).mean()
    ratio = (ema1 / ema2.replace(0, np.nan))
    res['mass_qfq'] = ratio.rolling(25, min_periods=1).sum()
    res['ma_mass_qfq'] = res['mass_qfq'].rolling(9, min_periods=1).mean()

    # ── EMV (14, 9) ──
    mid_hl = (h + l) / 2
    emv_raw = (mid_hl - mid_hl.shift(1)) / (v / (h - l).replace(0, np.nan)).replace(0, np.nan)
    res['emv_qfq'] = emv_raw.rolling(14, min_periods=1).mean()
    res['maemv_qfq'] = res['emv_qfq'].rolling(9, min_periods=1).mean()

    # ── MFI (14) ──
    tp_mfi = (h + l + c) / 3
    mf = tp_mfi * v
    pos_mf = mf.where(tp_mfi.diff() > 0, 0).rolling(14, min_periods=1).sum()
    neg_mf = mf.where(tp_mfi.diff() < 0, 0).rolling(14, min_periods=1).sum()
    mr = (pos_mf / neg_mf.replace(0, np.nan))
    res['mfi_qfq'] = (100 - 100 / (1 + mr))

    # OBV / ASI / ASIT 无法从 QFQ 价格简单重算（需完整历史序列），不覆盖
    return res


def apply_qfq_compute(df, computed):
    """将 computed dict 的结果写入 df（仅覆盖存在的列）"""
    for col, series in computed.items():
        if col in df.columns:
            df[col] = series
    return df


def main():
    print("=" * 70)
    print("QFQ 四种方式对比 — 固定 3 只主板股票")
    print("方法: 1=API原始  2=混和策略  3=全公式重算  4=从QFQ价格算不变指标")
    print("=" * 70)

    pro = get_pro()
    stocks = pick_fixed_stocks()
    print(f"\n选中股票: {stocks}\n")

    qfq_cols_all = [
        'open_qfq', 'high_qfq', 'low_qfq', 'close_qfq',
        'ma_qfq_5', 'ma_qfq_10', 'ma_qfq_20', 'ma_qfq_30',
        'ma_qfq_60', 'ma_qfq_90', 'ma_qfq_250',
        'ema_qfq_5', 'ema_qfq_10', 'ema_qfq_20', 'ema_qfq_30',
        'ema_qfq_60', 'ema_qfq_90', 'ema_qfq_250',
        'macd_dif_qfq', 'macd_dea_qfq', 'macd_qfq',
        'kdj_k_qfq', 'kdj_d_qfq', 'kdj_qfq',
        'rsi_qfq_6', 'rsi_qfq_12', 'rsi_qfq_24',
        'boll_upper_qfq', 'boll_mid_qfq', 'boll_lower_qfq',
        'cci_qfq', 'asi_qfq', 'asit_qfq', 'atr_qfq', 'bbi_qfq',
        'bias1_qfq', 'bias2_qfq', 'bias3_qfq',
        'brar_ar_qfq', 'brar_br_qfq', 'cr_qfq',
        'dfma_dif_qfq', 'dfma_difma_qfq',
        'dmi_adx_qfq', 'dmi_adxr_qfq', 'dmi_mdi_qfq', 'dmi_pdi_qfq',
        'dpo_qfq', 'madpo_qfq', 'emv_qfq', 'maemv_qfq',
        'expma_12_qfq', 'expma_50_qfq',
        'ktn_upper_qfq', 'ktn_mid_qfq', 'ktn_down_qfq',
        'mass_qfq', 'ma_mass_qfq', 'mfi_qfq',
        'mtm_qfq', 'mtmma_qfq', 'obv_qfq',
        'psy_qfq', 'psyma_qfq', 'roc_qfq', 'maroc_qfq',
        'taq_up_qfq', 'taq_mid_qfq', 'taq_down_qfq',
        'trix_qfq', 'trma_qfq', 'vr_qfq', 'wr_qfq', 'wr1_qfq',
        'xsii_td1_qfq', 'xsii_td2_qfq', 'xsii_td3_qfq', 'xsii_td4_qfq',
    ]

    # 收集三只股票的结果
    all_results = []  # list of (ts_code, n_rows, {col: {MAE, MaxAE, RelErr%}})

    for ts_code in stocks:
        print(f"[*] 拉取 {ts_code} ...")
        df_raw = fetch_raw(pro, ts_code)
        if df_raw is None or len(df_raw) == 0:
            print(f"    [!] {ts_code} 无数据，跳过")
            continue

        n_rows = len(df_raw)
        print(f"    获取 {n_rows} 行数据")

        # 方法①: API 原始
        df1 = df_raw.copy()

        # 方法②: recalc_all_qfq
        df2 = df_raw.copy()
        df2 = recalc_all_qfq(df2)

        # 方法③: recalc_qfq_by_formula
        df3 = df_raw.copy()
        df3 = recalc_qfq_by_formula(df3)

        # 方法④: 从 QFQ 价格用标准公式算"不变型"指标
        df4 = recalc_all_qfq(df_raw.copy())  # 先用混和策略打好基础
        computed = compute_invariant_from_qfq(df4)
        df4 = apply_qfq_compute(df4, computed)

        # 找实际存在的 qfq 列
        available_cols = [c for c in qfq_cols_all if c in df1.columns]

        # 对比
        err_2v1 = calc_error(df1, df2, available_cols)
        err_3v1 = calc_error(df1, df3, available_cols)
        err_4v1 = calc_error(df1, df4, available_cols)

        all_results.append((ts_code, n_rows, err_2v1, err_3v1, err_4v1))

        # 逐只股票打印摘要
        print(f"\n{'─' * 80}")
        print(f"  {ts_code} ({n_rows} 行)")
        print(f"  {'指标':<25s} {'方法2 MAE':>10s} {'方法3 MAE':>10s} {'方法4 MAE':>10s} {'最佳':>6s}")
        print(f"  {'─' * 80}")

        for col in available_cols:
            e2 = err_2v1.get(col, {})
            e3 = err_3v1.get(col, {})
            e4 = err_4v1.get(col, {})
            mae2 = e2.get('MAE', 0)
            mae3 = e3.get('MAE', 0)
            mae4 = e4.get('MAE', 0)

            best = min(mae2, mae3, mae4)
            best_label = ('2' if best == mae2 else ('3' if best == mae3 else '4'))
            improvements = []
            if mae4 < mae2 * 0.95 and mae2 > 0.01:
                improvements.append(f"4比2好 {mae2/mae4:.1f}x")
            if mae4 < mae3 * 0.95 and mae3 > 0.01:
                improvements.append(f"4比3好 {mae3/mae4:.1f}x")
            note = ' ' + '; '.join(improvements) if improvements else ''

            print(f"  {col:<25s} {mae2:>10.6f} {mae3:>10.6f} {mae4:>10.6f} {best_label:>6s}{note}")

    # ── 汇总 ──
    print(f"\n{'=' * 80}")
    print("汇总对比")
    print(f"{'=' * 80}")

    # 按列汇总平均误差
    summary = {}
    for _, _, err2, err3, err4 in all_results:
        for col, e in err2.items():
            if col not in summary:
                summary[col] = {'mae2': [], 'mae3': [], 'mae4': []}
            summary[col]['mae2'].append(e['MAE'])
        for col, e in err3.items():
            if col not in summary:
                summary[col] = {'mae2': [], 'mae3': [], 'mae4': []}
            summary[col]['mae3'].append(e['MAE'])
        for col, e in err4.items():
            if col not in summary:
                summary[col] = {'mae2': [], 'mae3': [], 'mae4': []}
            summary[col]['mae4'].append(e['MAE'])

    print(f"  {'指标':<25s} {'方法2均值':>10s} {'方法3均值':>10s} {'方法4均值':>10s} {'最佳':>6s} {'备注':>20s}")
    print(f"  {'─' * 90}")
    for col in qfq_cols_all:
        if col not in summary:
            continue
        s = summary[col]
        avg2 = np.mean(s['mae2'])
        avg3 = np.mean(s['mae3'])
        avg4 = np.mean(s['mae4'])
        best = min(avg2, avg3, avg4)
        best_label = ('2' if best == avg2 else ('3' if best == avg3 else '4'))

        note = ''
        if avg4 < avg2 * 0.9 and avg2 > 0.01:
            note = f'4优于2 ({avg2/avg4:.1f}x)'
        elif avg2 < avg4 * 0.9 and avg4 > 0.01:
            note = f'2优于4 ({avg4/avg2:.1f}x)'

        print(f"  {col:<25s} {avg2:>10.6f} {avg3:>10.6f} {avg4:>10.6f} {best_label:>6s} {note:>20s}")

    print(f"\n[*] 方法②=混和策略, 方法③=全公式重算, 方法④=从QFQ价格算不变指标")
    print(f"[*] 方法④仅影响不可缩放型指标（KDJ/RSI/CCI/WR/DMI等），对MA/EMA/MACD/BOLL等无影响")

    # 写入 Markdown 报告
    report_path = os.path.join(os.path.dirname(__file__), 'qfq_compare_report.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# QFQ 四种方式对比报告\n\n")
        f.write(f"股票: {', '.join(stocks)}\n\n")
        f.write("## 方法说明\n\n")
        f.write("| 编号 | 方式 | 说明 |\n")
        f.write("|------|------|------|\n")
        f.write("| 1 | API原始 | `stk_factor_pro` 返回的 `_qfq` 列，不做任何处理 |\n")
        f.write("| 2 | 混和策略 | `recalc_all_qfq()` — MA/EMA/MACD用公式重算，ATR/KTN用bfq*ratio缩放，不变指标bfq复制 |\n")
        f.write("| 3 | 全公式重算 | `recalc_qfq_by_formula()` — 所有可从QFQ价格推导的指标都用公式重算 |\n")
        f.write("| 4 | QFQ价算不变指标 | 方法2基础上，将所有不变指标（KDJ/RSI/CCI等）从QFQ价格用标准公式重算 |\n\n")

        f.write("## 汇总（3只股票平均MAE）\n\n")
        f.write(f"| 指标 | 方法2 | 方法3 | 方法4 | 最佳 |\n")
        f.write(f"|------|------|------|------|------|\n")
        for col in qfq_cols_all:
            if col not in summary:
                continue
            s = summary[col]
            avg2 = np.mean(s['mae2'])
            avg3 = np.mean(s['mae3'])
            avg4 = np.mean(s['mae4'])
            best = min(avg2, avg3, avg4)
            best_label = ('2' if best == avg2 else ('3' if best == avg3 else '4'))
            f.write(f"| {col} | {avg2:.6f} | {avg3:.6f} | {avg4:.6f} | {best_label} |\n")

        # 每只股票详细
        for ts_code, n_rows, err2, err3, err4 in all_results:
            f.write(f"\n## {ts_code} ({n_rows} 行)\n\n")
            f.write(f"| 指标 | 方法2 MAE | 方法3 MAE | 方法4 MAE | 最佳 |\n")
            f.write(f"|------|----------|----------|----------|------|\n")
            for col in qfq_cols_all:
                e2 = err2.get(col, {})
                e3 = err3.get(col, {})
                e4 = err4.get(col, {})
                if not e2 and not e3 and not e4:
                    continue
                mae2 = e2.get('MAE', 0)
                mae3 = e3.get('MAE', 0)
                mae4 = e4.get('MAE', 0)
                best = min(mae2, mae3, mae4)
                best_label = ('2' if best == mae2 else ('3' if best == mae3 else '4'))
                f.write(f"| {col} | {mae2:.6f} | {mae3:.6f} | {mae4:.6f} | {best_label} |\n")

    print(f"\n[*] Markdown 报告已写入: {report_path}")


if __name__ == '__main__':
    main()
