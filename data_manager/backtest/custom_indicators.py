import pandas as pd
import numpy as np

# ==========================================
# 独立指标计算函数 (供策略直接调用)
# ==========================================

def calc_kdj(high, low, close, n=9, m1=3, m2=3):
    low_list = low.rolling(n, min_periods=1).min()
    high_list = high.rolling(n, min_periods=1).max()
    rsv = (close - low_list) / (high_list - low_list + 1e-10) * 100
    k = rsv.ewm(alpha=1/m1, adjust=False).mean()
    d = k.ewm(alpha=1/m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd = (dif - dea) * 2
    return dif, dea, macd

def calc_ma(close, p=5):
    return close.rolling(p, min_periods=1).mean()

def calc_ema(close, p=5):
    return close.ewm(span=p, adjust=False).mean()

def calc_expma(close, p1=12, p2=50):
    return close.ewm(span=p1, adjust=False).mean(), close.ewm(span=p2, adjust=False).mean()

def calc_rsi(close, p=6):
    delta = close.diff()
    up = np.where(delta > 0, delta, 0)
    down = np.where(delta < 0, -delta, 0)
    ma_up = pd.Series(up, index=close.index).rolling(p, min_periods=1).mean()
    ma_down = pd.Series(down, index=close.index).rolling(p, min_periods=1).mean()
    rs = ma_up / ma_down.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def calc_boll(close, n=20, k=2):
    mid = close.rolling(n, min_periods=1).mean()
    std = close.rolling(n, min_periods=1).std(ddof=0)
    return mid, mid + k * std, mid - k * std

def calc_atr(high, low, prev_close, n=14):
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def calc_bbi(close, m1=3, m2=6, m3=12, m4=20):
    return (close.rolling(m1, min_periods=1).mean() +
            close.rolling(m2, min_periods=1).mean() +
            close.rolling(m3, min_periods=1).mean() +
            close.rolling(m4, min_periods=1).mean()) / 4

def calc_cci(high, low, close, n=14):
    tp = (high + low + close) / 3
    ma_tp = tp.rolling(n, min_periods=1).mean()
    md = tp.rolling(n, min_periods=1).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma_tp) / (0.015 * md.replace(0, 1e-10))

def calc_bias(close, n=6):
    ma_n = close.rolling(n, min_periods=1).mean()
    return (close - ma_n) / ma_n * 100

def calc_cr(high, low, close, prev_high, prev_low, prev_close, n=26):
    typ = (prev_high + prev_low + prev_close) / 3
    p1 = pd.Series(np.where(high > typ, high - typ, 0), index=close.index)
    p2 = pd.Series(np.where(typ > low, typ - low, 0), index=close.index)
    return (p1.rolling(n, min_periods=1).sum() / p2.rolling(n, min_periods=1).sum().replace(0, 1e-10)) * 100

def calc_roc(close, n=12, m=6):
    roc = (close - close.shift(n)) / close.shift(n) * 100
    return roc, roc.rolling(m, min_periods=1).mean()

def calc_trix(close, n=12, m=20):
    """TRIX (12, 20) — 三重指数平滑变化率"""
    tr = close.ewm(span=n, adjust=False).mean() \
              .ewm(span=n, adjust=False).mean() \
              .ewm(span=n, adjust=False).mean()
    trix = (tr - tr.shift(1)) / tr.shift(1).replace(0, np.nan) * 100
    trma = trix.rolling(m, min_periods=1).mean()
    return trix, trma

def calc_wr(high, low, close, n=10):
    hh = high.rolling(n, min_periods=1).max()
    ll = low.rolling(n, min_periods=1).min()
    return (hh - close) / (hh - ll + 1e-10) * 100

def calc_dfma(close, n1=10, n2=50, m=10):
    dif = close.rolling(n1, min_periods=1).mean() - close.rolling(n2, min_periods=1).mean()
    return dif, dif.rolling(m, min_periods=1).mean()

def calc_dpo(close, n=20, m=6):
    ma_n = close.rolling(n, min_periods=1).mean()
    shift_period = int(n / 2 + 1)
    dpo = close - ma_n.shift(shift_period)
    return dpo, dpo.rolling(m, min_periods=1).mean()

def calc_ktn(high, low, close, prev_close, n=20, m=2):
    mid = close.ewm(span=n, adjust=False).mean()
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=n, adjust=False).mean()
    return mid, mid + m * atr, mid - m * atr

def calc_mtm(close, n=12, m=6):
    mtm = close - close.shift(n)
    return mtm, mtm.rolling(m, min_periods=1).mean()

def calc_taq(high, low, n=20):
    up = high.rolling(n, min_periods=1).max()
    down = low.rolling(n, min_periods=1).min()
    return up, down, (up + down) / 2

def calc_xsii(close, n1=102, m1_pct=7, n2=12, m2_pct=3):
    ma_big = close.rolling(n1, min_periods=1).mean()
    ema_small = close.ewm(span=n2, adjust=False).mean()
    return (ma_big * (1 + m1_pct / 100.0), ma_big * (1 - m1_pct / 100.0),
            ema_small * (1 + m2_pct / 100.0), ema_small * (1 - m2_pct / 100.0))

# ==========================================
# 指标注册表 — 每个指标公式的唯一声明点
# ==========================================
# category: 'formula' = 有公式实现, 'not_implemented' = ETF/公式场景下填 NaN

INDICATOR_REGISTRY = {
    'ma': {
        'func': calc_ma, 'params': {'p': 5},
        'variants': [{'p': v} for v in [5, 10, 20, 30, 60, 90, 250]],
        'output': lambda p: f'ma_qfq_{p["p"]}',
        'category': 'formula',
    },
    'ema': {
        'func': calc_ema, 'params': {'p': 5},
        'variants': [{'p': v} for v in [5, 10, 20, 30, 60, 90, 250]],
        'output': lambda p: f'ema_qfq_{p["p"]}',
        'category': 'formula',
    },
    'macd': {
        'func': calc_macd, 'params': {'fast': 12, 'slow': 26, 'signal': 9},
        'variants': [{'fast': 12, 'slow': 26, 'signal': 9}],
        'output': ['macd_dif_qfq', 'macd_dea_qfq', 'macd_qfq'],
        'category': 'formula',
    },
    'kdj': {
        'func': calc_kdj, 'params': {'n': 9, 'm1': 3, 'm2': 3},
        'variants': [{'n': 9, 'm1': 3, 'm2': 3}],
        'output': ['kdj_k_qfq', 'kdj_d_qfq', 'kdj_qfq'],
        'category': 'formula',
    },
    'rsi': {
        'func': calc_rsi, 'params': {'p': 6},
        'variants': [{'p': v} for v in [6, 12, 24]],
        'output': lambda p: f'rsi_qfq_{p["p"]}',
        'category': 'formula',
    },
    'boll': {
        'func': calc_boll, 'params': {'n': 20, 'k': 2},
        'variants': [{'n': 20, 'k': 2}],
        'output': ['boll_mid_qfq', 'boll_upper_qfq', 'boll_lower_qfq'],
        'category': 'formula',
    },
    'atr': {
        'func': calc_atr, 'params': {'n': 14},
        'variants': [{'n': 14}],
        'output': ['atr_qfq'],
        'category': 'formula',
    },
    'bbi': {
        'func': calc_bbi, 'params': {'m1': 3, 'm2': 6, 'm3': 12, 'm4': 24},
        'variants': [{'m1': 3, 'm2': 6, 'm3': 12, 'm4': 24}],
        'output': ['bbi_qfq'],
        'category': 'formula',
    },
    'cci': {
        'func': calc_cci, 'params': {'n': 14},
        'variants': [{'n': 14}],
        'output': ['cci_qfq'],
        'category': 'formula',
    },
    'bias': {
        'func': calc_bias, 'params': {'n': 6},
        'variants': [{'n': v} for v in [6, 12, 24]],
        'output': lambda p: {6: 'bias1_qfq', 12: 'bias2_qfq', 24: 'bias3_qfq'}[p['n']],
        'category': 'formula',
    },
    'cr': {
        'func': calc_cr, 'params': {'n': 26},
        'variants': [{'n': 26}],
        'output': ['cr_qfq'],
        'category': 'formula',
    },
    'roc': {
        'func': calc_roc, 'params': {'n': 12, 'm': 6},
        'variants': [{'n': 12, 'm': 6}],
        'output': ['roc_qfq', 'maroc_qfq'],
        'category': 'formula',
    },
    'wr': {
        'func': calc_wr, 'params': {'n': 10},
        'variants': [{'n': 10}, {'n': 6}],
        'output': lambda p: {10: 'wr_qfq', 6: 'wr1_qfq'}[p['n']],
        'category': 'formula',
    },
    'dfma': {
        'func': calc_dfma, 'params': {'n1': 10, 'n2': 50, 'm': 10},
        'variants': [{'n1': 10, 'n2': 50, 'm': 10}],
        'output': ['dfma_dif_qfq', 'dfma_difma_qfq'],
        'category': 'formula',
    },
    'dpo': {
        'func': calc_dpo, 'params': {'n': 20, 'm': 6},
        'variants': [{'n': 20, 'm': 6}],
        'output': ['dpo_qfq', 'madpo_qfq'],
        'category': 'formula',
    },
    'ktn': {
        'func': calc_ktn, 'params': {'n': 20, 'm': 2},
        'variants': [{'n': 20, 'm': 2}],
        'output': ['ktn_mid_qfq', 'ktn_upper_qfq', 'ktn_down_qfq'],
        'category': 'formula',
    },
    'mtm': {
        'func': calc_mtm, 'params': {'n': 12, 'm': 6},
        'variants': [{'n': 12, 'm': 6}],
        'output': ['mtm_qfq', 'mtmma_qfq'],
        'category': 'formula',
    },
    'taq': {
        'func': calc_taq, 'params': {'n': 20},
        'variants': [{'n': 20}],
        'output': ['taq_up_qfq', 'taq_down_qfq', 'taq_mid_qfq'],
        'category': 'formula',
    },
    'xsii': {
        'func': calc_xsii, 'params': {'n1': 102, 'm1_pct': 7, 'n2': 12, 'm2_pct': 3},
        'variants': [{'n1': 102, 'm1_pct': 7, 'n2': 12, 'm2_pct': 3}],
        'output': ['xsii_td1_qfq', 'xsii_td2_qfq', 'xsii_td3_qfq', 'xsii_td4_qfq'],
        'category': 'formula',
    },
    'expma': {
        'func': calc_expma, 'params': {'p1': 12, 'p2': 50},
        'variants': [{'p1': 12, 'p2': 50}],
        'output': ['expma_12_qfq', 'expma_50_qfq'],
        'category': 'formula',
    },
    'trix': {
        'func': calc_trix, 'params': {'n': 12, 'm': 20},
        'variants': [{'n': 12, 'm': 20}],
        'output': ['trix_qfq', 'trma_qfq'],
        'category': 'formula',
    },

    # ── 尚无公式实现的指标（ETF 使用时填 NaN）──
    'brar': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['brar_ar_qfq', 'brar_br_qfq'],
        'category': 'not_implemented',
    },
    'psy': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['psy_qfq', 'psyma_qfq'],
        'category': 'not_implemented',
    },
    'dmi': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['dmi_adx_qfq', 'dmi_adxr_qfq', 'dmi_mdi_qfq', 'dmi_pdi_qfq'],
        'category': 'not_implemented',
    },
    'vr': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['vr_qfq'],
        'category': 'not_implemented',
    },
    'mass': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['mass_qfq', 'ma_mass_qfq'],
        'category': 'not_implemented',
    },
    'emv': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['emv_qfq', 'maemv_qfq'],
        'category': 'not_implemented',
    },
    'obv': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['obv_qfq'],
        'category': 'not_implemented',
    },
    'mfi': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['mfi_qfq'],
        'category': 'not_implemented',
    },
    'asi': {
        'func': None, 'params': {}, 'variants': [{}],
        'output': ['asi_qfq', 'asit_qfq'],
        'category': 'not_implemented',
    },
}


def _resolve_output_cols(entry, params):
    """解析 Registry 条目的输出列名"""
    out = entry['output']
    if callable(out):
        result = out(params)
    else:
        result = out
    if isinstance(result, str):
        return [result]
    return list(result)


def _resolve_ind_name(ind_name):
    """将列名变体映射回 Registry 的 base_name"""
    name = ind_name.lower()
    aliases = {
        'kdj_k': 'kdj', 'kdj_d': 'kdj', 'kdj_j': 'kdj',
        'macd_dif': 'macd', 'macd_dea': 'macd',
        'boll_mid': 'boll', 'boll_upper': 'boll', 'boll_lower': 'boll',
        'taq_up': 'taq', 'taq_down': 'taq', 'taq_mid': 'taq',
        'dfma_dif': 'dfma', 'dfma_difma': 'dfma',
        'roc': 'roc', 'maroc': 'roc',
        'bias1': 'bias', 'bias2': 'bias', 'bias3': 'bias',
        'wr1': 'wr',
    }
    return aliases.get(name, name)


def compute_indicators(df, ind_names, close_col='qfq_close', high_col='qfq_high',
                       low_col='qfq_low', prev_close_col=None, log_missing=True):
    """
    统一入口：根据指标名列表，从 Registry 查找公式并计算。

    参数:
        df: 含价格列的 DataFrame
        ind_names: 指标名列表，如 ['ma_5', 'kdj', 'rsi_12']
        close_col/high_col/low_col: df 中价格列的名称
        prev_close_col: 前收盘列名，None 则用 close_col.shift(1)
        log_missing: 是否对无公式的指标打 warning

    返回:
        df（原地修改，同时返回）
    """
    import logging
    logger = logging.getLogger(__name__)

    close = df[close_col]
    high = df[high_col] if high_col in df.columns else None
    low = df[low_col] if low_col in df.columns else None
    prev_close = df[prev_close_col] if (prev_close_col and prev_close_col in df.columns) else close.shift(1)
    prev_high = high.shift(1) if high is not None else None
    prev_low = low.shift(1) if low is not None else None

    for ind_name in ind_names:
        parts = ind_name.lower().split('_')
        base_parts = []
        params = []
        for p in parts:
            if p.lstrip('-').isdigit():
                params.append(int(p))
            else:
                base_parts.append(p)
        base_name = _resolve_ind_name('_'.join(base_parts))

        entry = INDICATOR_REGISTRY.get(base_name)
        if entry is None:
            if log_missing:
                logger.warning("未知指标: %s", ind_name)
            continue

        output_cols = _resolve_output_cols(entry, entry['params'])

        if entry['func'] is None:
            if log_missing:
                logger.warning("指标 %s 尚无公式实现，填 NaN: %s", base_name, output_cols)
            for col in output_cols:
                df[col] = np.nan
            continue

        # 从 params 覆盖用户传入的参数
        kwargs = entry['params'].copy()
        param_names = list(entry['params'].keys())
        for i, key in enumerate(param_names):
            if i < len(params):
                kwargs[key] = params[i]

        # 构建函数调用
        func = entry['func']
        func_kwargs = {}
        for k, v in kwargs.items():
            func_kwargs[k] = v

        # 传入价格数据
        if 'close' in func.__code__.co_varnames:
            func_kwargs['close'] = close
        if 'high' in func.__code__.co_varnames:
            func_kwargs['high'] = high
        if 'low' in func.__code__.co_varnames:
            func_kwargs['low'] = low
        if 'prev_close' in func.__code__.co_varnames:
            func_kwargs['prev_close'] = prev_close
        if 'prev_high' in func.__code__.co_varnames:
            func_kwargs['prev_high'] = prev_high
        if 'prev_low' in func.__code__.co_varnames:
            func_kwargs['prev_low'] = prev_low

        result = func(**func_kwargs)
        if not isinstance(result, tuple):
            result = (result,)
        for col, val in zip(output_cols, result):
            df[col] = val

    return df


def add_dynamic_indicator(df, ind_name):
    """
    [向后兼容] 计算单个指标，输出列名不带参数后缀。
    新代码建议直接使用 compute_indicators()。
    """
    # 解析参数用于确定 variants
    parts = ind_name.lower().split('_')
    base_parts = []
    for p in parts:
        if p.lstrip('-').isdigit():
            break
        base_parts.append(p)
    base_name = _resolve_ind_name('_'.join(base_parts))

    entry = INDICATOR_REGISTRY.get(base_name)
    if entry is None or entry['func'] is None:
        return compute_indicators(df, [ind_name])

    # 直接用 calc_* 函数（绕过 Registry 的参数匹配，保留旧行为）
    close = df['qfq_close']
    high = df['qfq_high']
    low = df['qfq_low']
    prev_close = df['qfq_pre_close'] if 'qfq_pre_close' in df.columns else close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    params = [int(p) for p in parts if p.lstrip('-').isdigit()]
    output_cols = _resolve_output_cols(entry, entry['params'])

    # 直接调函数（精确匹配旧接口的参数传递方式）
    if base_name == 'kdj' and len(params) == 3:
        k, d, j = calc_kdj(high, low, close, *params)
        df[output_cols[0]], df[output_cols[1]], df[output_cols[2]] = k, d, j
    elif base_name == 'macd' and len(params) == 3:
        dif, dea, macd = calc_macd(close, *params)
        df[output_cols[0]], df[output_cols[1]], df[output_cols[2]] = dif, dea, macd
    elif base_name == 'ma' and len(params) == 1:
        df[output_cols[0]] = calc_ma(close, *params)
    elif base_name == 'ema' and len(params) == 1:
        df[output_cols[0]] = calc_ema(close, *params)
    elif base_name == 'expma' and len(params) == 2:
        e1, e2 = calc_expma(close, *params)
        df[output_cols[0]], df[output_cols[1]] = e1, e2
    elif base_name == 'rsi' and len(params) == 1:
        df[output_cols[0]] = calc_rsi(close, *params)
    elif base_name == 'boll' and len(params) == 2:
        mid, up, dn = calc_boll(close, *params)
        df[output_cols[0]], df[output_cols[1]], df[output_cols[2]] = mid, up, dn
    elif base_name == 'atr' and len(params) == 1:
        df[output_cols[0]] = calc_atr(high, low, prev_close, *params)
    elif base_name == 'bbi' and len(params) == 4:
        df[output_cols[0]] = calc_bbi(close, *params)
    elif base_name == 'cci' and len(params) == 1:
        df[output_cols[0]] = calc_cci(high, low, close, *params)
    elif base_name == 'bias' and len(params) == 1:
        df[output_cols[0]] = calc_bias(close, *params)
    elif base_name == 'cr' and len(params) == 1:
        df[output_cols[0]] = calc_cr(high, low, close, prev_high, prev_low, prev_close, *params)
    elif base_name == 'roc' and len(params) == 2:
        r, m = calc_roc(close, *params)
        df[output_cols[0]], df[output_cols[1]] = r, m
    elif base_name == 'wr' and len(params) == 1:
        df[output_cols[0]] = calc_wr(high, low, close, *params)
    elif base_name == 'dfma' and len(params) == 3:
        d, dm = calc_dfma(close, *params)
        df[output_cols[0]], df[output_cols[1]] = d, dm
    elif base_name == 'dpo' and len(params) == 2:
        d, dm = calc_dpo(close, *params)
        df[output_cols[0]], df[output_cols[1]] = d, dm
    elif base_name == 'ktn' and len(params) == 2:
        mid, up, dn = calc_ktn(high, low, close, prev_close, *params)
        df[output_cols[0]], df[output_cols[1]], df[output_cols[2]] = mid, up, dn
    elif base_name == 'mtm' and len(params) == 2:
        m, mm = calc_mtm(close, *params)
        df[output_cols[0]], df[output_cols[1]] = m, mm
    elif base_name == 'taq' and len(params) == 1:
        up, dn, mid = calc_taq(high, low, *params)
        df[output_cols[0]], df[output_cols[1]], df[output_cols[2]] = up, dn, mid
    elif base_name == 'xsii' and len(params) == 4:
        t1, t2, t3, t4 = calc_xsii(close, *params)
        df[output_cols[0]], df[output_cols[1]], df[output_cols[2]], df[output_cols[3]] = t1, t2, t3, t4
    elif base_name == 'trix' and len(params) == 2:
        tx, tm = calc_trix(close, *params)
        df[output_cols[0]], df[output_cols[1]] = tx, tm

    return df
