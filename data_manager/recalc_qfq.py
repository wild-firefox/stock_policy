"""
从 qfq 价格重算所有 _qfq 技术指标

增量更新后，adj_factor 变化会导致 API 返回的 qfq 指标与旧数据不一致。
解决方案：合并数据后，用最新 adj_factor 重算 qfq 价格，再基于 qfq 价格重算指标。

四类指标处理策略（经实证对比，按最小 MAE 选择）：
  1. 公式重算型（MA/EMA/MACD/BOLL/CCI/BIAS/ROC/TRIX 等）：从 qfq 价格用 rolling/ewm 公式重算
     — 精度远高于 bfq*ratio 缩放（MA_250 MAE: 0.001 vs 0.279，差 200x）
     — CCI/BIAS/ROC/TRIX 从 QFQ 价算优于 bfq 复制（1.4~20x）
  2. 价格不变型（KDJ/RSI/DMI/CR/BRAR/PSY/VR/WR/MASS/EMV/OBV/MFI）：bfq 复制
     — 公式重算因参数/方法差异反而误差更大
  3. bfq*ratio 缩放型（ATR/KTN/XSII/ASI/BBI）：公式重算不如线性缩放精准

用法：
    from recalc_qfq import recalc_all_qfq
    df = recalc_all_qfq(df, decimals=2)
"""
import numpy as np
import pandas as pd

from data_manager.backtest.custom_indicators import (
    calc_ma, calc_ema, calc_macd, calc_boll, calc_cci, calc_bias,
    calc_roc, calc_trix, calc_dpo, calc_taq, calc_dfma,
    calc_expma, calc_mtm, calc_bbi, calc_atr, calc_ktn, calc_xsii,
)

# ═══════════════════════════════════════════════════════════
# 模块级常量：指标分类（供 recalc_all_qfq 和 apply_qfq_indicators 共用）
# ═══════════════════════════════════════════════════════════

# 价格不变型：bfq 值直接复制到 qfq（qfq_col → bfq_col）
INVARIANT_MAP = {
    'kdj_k_qfq': 'kdj_k_bfq', 'kdj_d_qfq': 'kdj_d_bfq', 'kdj_qfq': 'kdj_bfq',
    'rsi_qfq_6': 'rsi_bfq_6', 'rsi_qfq_12': 'rsi_bfq_12', 'rsi_qfq_24': 'rsi_bfq_24',
    'brar_ar_qfq': 'brar_ar_bfq', 'brar_br_qfq': 'brar_br_bfq',
    'cr_qfq': 'cr_bfq',
    'psy_qfq': 'psy_bfq', 'psyma_qfq': 'psyma_bfq',
    'dmi_adx_qfq': 'dmi_adx_bfq', 'dmi_adxr_qfq': 'dmi_adxr_bfq',
    'dmi_mdi_qfq': 'dmi_mdi_bfq', 'dmi_pdi_qfq': 'dmi_pdi_bfq',
    'vr_qfq': 'vr_bfq',
    'mass_qfq': 'mass_bfq', 'ma_mass_qfq': 'ma_mass_bfq',
    'wr_qfq': 'wr_bfq', 'wr1_qfq': 'wr1_bfq',
    'emv_qfq': 'emv_bfq', 'maemv_qfq': 'maemv_bfq',
    'obv_qfq': 'obv_bfq', 'mfi_qfq': 'mfi_bfq',
}

# bfq*ratio 缩放型：bfq 列 × ratio → qfq 列（bfq_col → qfq_col）
SCALE_COLS = {
    'atr_bfq': 'atr_qfq',
    'ktn_mid_bfq': 'ktn_mid_qfq',
    'ktn_upper_bfq': 'ktn_upper_qfq',
    'ktn_down_bfq': 'ktn_down_qfq',
    'xsii_td1_bfq': 'xsii_td1_qfq',
    'xsii_td2_bfq': 'xsii_td2_qfq',
    'xsii_td3_bfq': 'xsii_td3_qfq',
    'xsii_td4_bfq': 'xsii_td4_qfq',
    'asi_bfq': 'asi_qfq',
    'asit_bfq': 'asit_qfq',
    'bbi_bfq': 'bbi_qfq',
}

# 反向索引：qfq_col → bfq_col（用于按需查找）
_SCALE_REVERSE = {v: k for k, v in SCALE_COLS.items()}
_INVARIANT_SET = set(INVARIANT_MAP.keys())
_SCALE_SET = set(_SCALE_REVERSE.keys())


def recalc_all_qfq(df: pd.DataFrame, decimals: int = 2) -> pd.DataFrame:
    """
    给定含 open/high/low/close/adj_factor 的 DataFrame，
    重算所有 *_qfq 列。采用混合策略，每类指标选误差最小的方式。

    要求 df 已按 trade_date 升序排列。
    指标文档：https://tushare.pro/document/2?doc_id=328
    """
    if df is None or len(df) == 0:
        return df

    latest_factor = df['adj_factor'].iloc[-1]
    if pd.isna(latest_factor) or latest_factor == 0:
        return df

    ratio = df['adj_factor'] / latest_factor

    # ── 1. 重算 4 个 qfq 价格 ──
    for src, dst in [('open', 'open_qfq'), ('high', 'high_qfq'),
                     ('low', 'low_qfq'), ('close', 'close_qfq')]:
        if src in df.columns and dst in df.columns:
            df[dst] = pd.to_numeric(df[src] * ratio, errors='coerce').round(decimals)

    # ── 2. 价格不变型指标：直接用 bfq 值 ──
    for qfq_col, bfq_col in INVARIANT_MAP.items():
        if qfq_col in df.columns and bfq_col in df.columns:
            df[qfq_col] = df[bfq_col]

    # ── 3. 从 QFQ 价格用公式重算（委托 custom_indicators 唯一公式来源）──
    close_qfq = df['close_qfq']
    high_qfq = df['high_qfq']
    low_qfq = df['low_qfq']

    # MA (SMA)
    for p in [5, 10, 20, 30, 60, 90, 250]:
        col = f'ma_qfq_{p}'
        if col in df.columns:
            df[col] = calc_ma(close_qfq, p=p)

    # EMA
    for p in [5, 10, 20, 30, 60, 90, 250]:
        col = f'ema_qfq_{p}'
        if col in df.columns:
            df[col] = calc_ema(close_qfq, p=p)

    # MACD (12, 26, 9)
    if 'macd_dif_qfq' in df.columns:
        dif, dea, macd = calc_macd(close_qfq, fast=12, slow=26, signal=9)
        df['macd_dif_qfq'] = dif
        df['macd_dea_qfq'] = dea
        df['macd_qfq'] = macd

    # DMA (10, 50, 10)
    if 'dfma_dif_qfq' in df.columns:
        dif, difma = calc_dfma(close_qfq, n1=10, n2=50, m=10)
        df['dfma_dif_qfq'] = dif
        df['dfma_difma_qfq'] = difma

    # BOLL (20, 2)
    if 'boll_mid_qfq' in df.columns:
        mid, upper, lower = calc_boll(close_qfq, n=20, k=2)
        df['boll_mid_qfq'] = mid
        df['boll_upper_qfq'] = upper
        df['boll_lower_qfq'] = lower

    # EXPMA (12, 50)
    if 'expma_12_qfq' in df.columns:
        e12, e50 = calc_expma(close_qfq, p1=12, p2=50)
        df['expma_12_qfq'] = e12
        df['expma_50_qfq'] = e50

    # MTM (12, 6)
    if 'mtm_qfq' in df.columns:
        mtm, mtmma = calc_mtm(close_qfq, n=12, m=6)
        df['mtm_qfq'] = mtm
        df['mtmma_qfq'] = mtmma

    # DPO (20, 6)
    if 'dpo_qfq' in df.columns:
        dpo, madpo = calc_dpo(close_qfq, n=20, m=6)
        df['dpo_qfq'] = dpo
        if 'madpo_qfq' in df.columns:
            df['madpo_qfq'] = madpo

    # TAQ (Donchian Channel, 20)
    if 'taq_mid_qfq' in df.columns:
        up, down, mid = calc_taq(high_qfq, low_qfq, n=20)
        df['taq_up_qfq'] = up
        df['taq_down_qfq'] = down
        df['taq_mid_qfq'] = mid

    # CCI (14)
    if 'cci_qfq' in df.columns:
        df['cci_qfq'] = calc_cci(high_qfq, low_qfq, close_qfq, n=14)

    # BIAS (6, 12, 24)
    for n, col in [(6, 'bias1_qfq'), (12, 'bias2_qfq'), (24, 'bias3_qfq')]:
        if col in df.columns:
            df[col] = calc_bias(close_qfq, n=n)

    # ROC (12) / MAROC (6)
    if 'roc_qfq' in df.columns:
        roc, maroc = calc_roc(close_qfq, n=12, m=6)
        df['roc_qfq'] = roc
        if 'maroc_qfq' in df.columns:
            df['maroc_qfq'] = maroc

    # TRIX (12) / TRMA (20)
    if 'trix_qfq' in df.columns:
        trix, trma = calc_trix(close_qfq, n=12, m=20)
        df['trix_qfq'] = trix
        if 'trma_qfq' in df.columns:
            df['trma_qfq'] = trma

    # ── 4. bfq * ratio 缩放 ──
    for bfq_col, qfq_col in SCALE_COLS.items():
        if bfq_col in df.columns and qfq_col in df.columns:
            df[qfq_col] = df[bfq_col] * ratio

    return df


def recalc_qfq_by_formula(df: pd.DataFrame, decimals: int = 2) -> pd.DataFrame:
    """
    [仅用于精度对比] 全部线性指标从 QFQ 价格用公式重算。
    实际生产请使用 recalc_all_qfq()（混合策略，误差更小）。
    """
    if df is None or len(df) == 0:
        return df

    latest_factor = df['adj_factor'].iloc[-1]
    if pd.isna(latest_factor) or latest_factor == 0:
        return df

    ratio = df['adj_factor'] / latest_factor

    for src, dst in [('open', 'open_qfq'), ('high', 'high_qfq'),
                     ('low', 'low_qfq'), ('close', 'close_qfq')]:
        if src in df.columns and dst in df.columns:
            df[dst] = pd.to_numeric(df[src] * ratio, errors='coerce').round(decimals)

    for qfq_col, bfq_col in INVARIANT_MAP.items():
        if qfq_col in df.columns and bfq_col in df.columns:
            df[qfq_col] = df[bfq_col]

    close_qfq = df['close_qfq']
    high_qfq = df['high_qfq']
    low_qfq = df['low_qfq']

    # ── MACD (12, 26, 9) ──
    if 'macd_dif_qfq' in df.columns:
        dif, dea, macd = calc_macd(close_qfq, fast=12, slow=26, signal=9)
        df['macd_dif_qfq'] = dif
        df['macd_dea_qfq'] = dea
        df['macd_qfq'] = macd

    # ── DMA (10, 50, 10) ──
    if 'dfma_dif_qfq' in df.columns:
        dif, difma = calc_dfma(close_qfq, n1=10, n2=50, m=10)
        df['dfma_dif_qfq'] = dif
        df['dfma_difma_qfq'] = difma

    # ── MA (SMA) ──
    for p in [5, 10, 20, 30, 60, 90, 250]:
        col = f'ma_qfq_{p}'
        if col in df.columns:
            df[col] = calc_ma(close_qfq, p=p)

    # ── EMA ──
    for p in [5, 10, 20, 30, 60, 90, 250]:
        col = f'ema_qfq_{p}'
        if col in df.columns:
            df[col] = calc_ema(close_qfq, p=p)

    # ── BOLL (20, 2) ──
    if 'boll_mid_qfq' in df.columns:
        mid, upper, lower = calc_boll(close_qfq, n=20, k=2)
        df['boll_mid_qfq'] = mid
        df['boll_upper_qfq'] = upper
        df['boll_lower_qfq'] = lower

    # ── ATR (14) ──
    if 'atr_qfq' in df.columns:
        df['atr_qfq'] = calc_atr(high_qfq, low_qfq, close_qfq.shift(1), n=14)

    # ── BBI (3, 6, 12, 24) ──
    if 'bbi_qfq' in df.columns:
        df['bbi_qfq'] = calc_bbi(close_qfq, m1=3, m2=6, m3=12, m4=24)

    # ── DPO (20, 6) ──
    if 'dpo_qfq' in df.columns:
        dpo, madpo = calc_dpo(close_qfq, n=20, m=6)
        df['dpo_qfq'] = dpo
        if 'madpo_qfq' in df.columns:
            df['madpo_qfq'] = madpo

    # ── EXPMA (12, 50) ──
    if 'expma_12_qfq' in df.columns:
        e12, e50 = calc_expma(close_qfq, p1=12, p2=50)
        df['expma_12_qfq'] = e12
        df['expma_50_qfq'] = e50

    # ── KTN (20, 2) ──
    if 'ktn_mid_qfq' in df.columns:
        mid, upper, lower = calc_ktn(high_qfq, low_qfq, close_qfq, close_qfq.shift(1), n=20, m=2)
        df['ktn_mid_qfq'] = mid
        df['ktn_upper_qfq'] = upper
        df['ktn_down_qfq'] = lower

    # ── MTM (12, 6) ──
    if 'mtm_qfq' in df.columns:
        mtm, mtmma = calc_mtm(close_qfq, n=12, m=6)
        df['mtm_qfq'] = mtm
        df['mtmma_qfq'] = mtmma

    # ── TAQ (20) ──
    if 'taq_mid_qfq' in df.columns:
        up, down, mid = calc_taq(high_qfq, low_qfq, n=20)
        df['taq_up_qfq'] = up
        df['taq_down_qfq'] = down
        df['taq_mid_qfq'] = mid

    # ── CCI (14) ──
    if 'cci_qfq' in df.columns:
        df['cci_qfq'] = calc_cci(high_qfq, low_qfq, close_qfq, n=14)

    # ── BIAS (6, 12, 24) ──
    for n, col in [(6, 'bias1_qfq'), (12, 'bias2_qfq'), (24, 'bias3_qfq')]:
        if col in df.columns:
            df[col] = calc_bias(close_qfq, n=n)

    # ── ROC (12) / MAROC (6) ──
    if 'roc_qfq' in df.columns:
        roc, maroc = calc_roc(close_qfq, n=12, m=6)
        df['roc_qfq'] = roc
        if 'maroc_qfq' in df.columns:
            df['maroc_qfq'] = maroc

    # ── TRIX (12) / TRMA (20) ──
    if 'trix_qfq' in df.columns:
        trix, trma = calc_trix(close_qfq, n=12, m=20)
        df['trix_qfq'] = trix
        if 'trma_qfq' in df.columns:
            df['trma_qfq'] = trma

    # ── XSII (102, 7%, 12, 3%) ──
    if 'xsii_td1_qfq' in df.columns:
        td1, td2, td3, td4 = calc_xsii(close_qfq, n1=102, m1_pct=7, n2=12, m2_pct=3)
        df['xsii_td1_qfq'] = td1
        df['xsii_td2_qfq'] = td2
        df['xsii_td3_qfq'] = td3
        df['xsii_td4_qfq'] = td4

    # ── ASI：公式不如 bfq*ratio 精准，此处仅用于对比，仍用 ratio 缩放 ──
    for col in ['asi_qfq', 'asit_qfq']:
        bfq_col = col.replace('_qfq', '_bfq')
        if col in df.columns and bfq_col in df.columns:
            df[col] = df[bfq_col] * ratio

    return df


def apply_qfq_indicators(df: pd.DataFrame, indicators: list, ratio=None) -> pd.DataFrame:
    """
    混合策略：对指定指标列表选择最优计算方式（供 engine_multi 使用）。

    分类依据 INVARIANT_MAP / SCALE_COLS 模块级常量，其余委托
    custom_indicators.compute_indicators() 用公式计算。

    参数:
        df: 含 bfq 列 + qfq_close/qfq_high/qfq_low 的 DataFrame
        indicators: 指标基名列表，如 ['ma_5', 'kdj', 'macd', 'rsi_12']
        ratio: adj_factor 比例列，None 则从 df['_ratio'] 取

    返回:
        df（原地修改并返回）
    """
    from data_manager.backtest.custom_indicators import (
        compute_indicators, INDICATOR_REGISTRY, _resolve_output_cols, _resolve_ind_name,
    )


    if df is None or len(df) == 0:
        return df

    if ratio is None:
        ratio = df['_ratio'] if '_ratio' in df.columns else pd.Series(1.0, index=df.index)

    invariant_done = set()
    scale_done = set()
    formula_inds = []

    for ind_name in indicators:
        ind_lower = ind_name.lower()

        # 解析 base_name 和参数
        parts = ind_lower.split('_')
        base_parts = []
        user_params = []
        in_params = False
        for p in parts:
            if p.lstrip('-').isdigit():
                in_params = True
            if in_params:
                user_params.append(int(p) if p.lstrip('-').isdigit() else p)
            else:
                base_parts.append(p)
        base_name = _resolve_ind_name('_'.join(base_parts))

        entry = INDICATOR_REGISTRY.get(base_name)
        if entry is None:
            formula_inds.append(ind_lower)
            continue

        # 匹配用户参数到 variant，未匹配时使用默认参数
        if user_params:
            matched_variant = None
            for v in entry['variants']:
                if list(v.values()) == user_params:
                    matched_variant = v
                    break
            active_params = {**entry['params'], **matched_variant} if matched_variant else entry['params']
        else:
            active_params = entry['params']

        output_cols = _resolve_output_cols(entry, active_params)

        # 判断该指标的所有输出列分别属于哪类
        all_invariant = all(c in _INVARIANT_SET for c in output_cols)
        all_scale = all(c in _SCALE_SET for c in output_cols)

        if all_invariant and entry['category'] != 'not_implemented':
            # bfq 快速路径：直接复制
            for qfq_col in output_cols:
                bfq_col = INVARIANT_MAP.get(qfq_col)
                if bfq_col and bfq_col in df.columns:
                    df[qfq_col] = df[bfq_col]
                    invariant_done.add(qfq_col)
        elif all_scale:
            # bfq*ratio 快速路径
            for qfq_col in output_cols:
                bfq_col = _SCALE_REVERSE.get(qfq_col)
                if bfq_col and bfq_col in df.columns:
                    df[qfq_col] = df[bfq_col] * ratio
                    scale_done.add(qfq_col)
        else:
            formula_inds.append(ind_lower)

    # 公式计算：统一委托给 compute_indicators
    if formula_inds:
        compute_indicators(df, formula_inds,
                          close_col='qfq_close' if 'qfq_close' in df.columns else 'close',
                          high_col='qfq_high' if 'qfq_high' in df.columns else 'high',
                          low_col='qfq_low' if 'qfq_low' in df.columns else 'low')

    return df
