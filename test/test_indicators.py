"""
测试 data_manager/backtest/custom_indicators.py 和 data_manager/recalc_qfq.py
"""
import sys
import os
import pandas as pd
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'data_manager', 'backtest'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'data_manager'))

from custom_indicators import (
    calc_ma, calc_ema, calc_macd, calc_kdj, calc_rsi, calc_boll,
    calc_atr, calc_bbi, calc_cci, calc_bias, calc_cr, calc_roc,
    calc_trix, calc_wr, calc_dfma, calc_dpo, calc_ktn, calc_mtm,
    calc_taq, calc_xsii, calc_expma,
    INDICATOR_REGISTRY, compute_indicators, add_dynamic_indicator,
)
from recalc_qfq import (
    INVARIANT_MAP, SCALE_COLS, apply_qfq_indicators,
    recalc_all_qfq, recalc_qfq_by_formula,
)


# ── 共用测试数据 ──
@pytest.fixture
def sample_close():
    """10 个交易日的模拟收盘价"""
    return pd.Series([10.0, 10.5, 10.2, 10.8, 11.0, 10.9, 11.2, 11.5, 11.3, 11.8],
                     name='close')


@pytest.fixture
def sample_ohlc():
    """含 open/high/low/close 的模拟 DataFrame"""
    n = 50
    np.random.seed(42)
    close = pd.Series(10.0 + np.cumsum(np.random.randn(n) * 0.2), name='close')
    return pd.DataFrame({
        'open': close.shift(1).fillna(10.0),
        'high': close * 1.02,
        'low': close * 0.98,
        'close': close,
    })


# ═══════════════════════════════════════════════════════
# 单元测试：各 calc_* 函数
# ═══════════════════════════════════════════════════════

class TestCalcMa:
    def test_ma5(self, sample_close):
        result = calc_ma(sample_close, p=5)
        assert len(result) == 10
        assert not result.isna().any()  # min_periods=1

    def test_ma_values(self, sample_close):
        result = calc_ma(sample_close, p=3)
        expected_0 = sample_close.iloc[0]
        expected_1 = sample_close.iloc[:2].mean()
        assert abs(result.iloc[0] - expected_0) < 1e-10
        assert abs(result.iloc[1] - expected_1) < 1e-10


class TestCalcEma:
    def test_ema5(self, sample_close):
        result = calc_ema(sample_close, p=5)
        assert len(result) == 10
        assert not result.isna().any()


class TestCalcMacd:
    def test_macd_output(self, sample_close):
        dif, dea, macd = calc_macd(sample_close)
        assert len(dif) == 10
        assert len(dea) == 10
        assert len(macd) == 10
        assert (macd == (dif - dea) * 2).all()


class TestCalcKdj:
    def test_kdj_output(self, sample_ohlc):
        k, d, j = calc_kdj(sample_ohlc['high'], sample_ohlc['low'], sample_ohlc['close'])
        assert len(k) == 50
        assert len(d) == 50
        assert len(j) == 50
        assert (j == 3 * k - 2 * d).all()


class TestCalcRsi:
    def test_rsi_range(self, sample_close):
        result = calc_rsi(sample_close, p=6)
        assert len(result) == 10
        assert result.between(0, 100).all()


class TestCalcBoll:
    def test_boll_output(self, sample_close):
        mid, upper, lower = calc_boll(sample_close, n=5, k=2)
        assert len(mid) == 10
        assert (upper >= mid).all()
        assert (lower <= mid).all()


class TestCalcCci:
    def test_cci_output(self, sample_ohlc):
        result = calc_cci(sample_ohlc['high'], sample_ohlc['low'], sample_ohlc['close'], n=14)
        assert len(result) == 50
        assert not result.isna().all()


class TestCalcBias:
    def test_bias_output(self, sample_close):
        result = calc_bias(sample_close, n=6)
        assert len(result) == 10


class TestCalcRoc:
    def test_roc_output(self, sample_close):
        roc, maroc = calc_roc(sample_close, n=3, m=2)
        assert len(roc) == 10
        assert len(maroc) == 10


class TestCalcTrix:
    def test_trix_output(self, sample_close):
        trix, trma = calc_trix(sample_close, n=12, m=20)
        assert len(trix) == 10
        assert len(trma) == 10


class TestCalcWr:
    def test_wr_output(self, sample_ohlc):
        result = calc_wr(sample_ohlc['high'], sample_ohlc['low'], sample_ohlc['close'], n=10)
        assert len(result) == 50
        assert result.between(0, 100).all()


class TestCalcDfma:
    def test_dfma_output(self, sample_close):
        dif, difma = calc_dfma(sample_close, n1=10, n2=50, m=10)
        assert len(dif) == 10


class TestCalcDpo:
    def test_dpo_output(self, sample_close):
        dpo, madpo = calc_dpo(sample_close, n=20, m=6)
        assert len(dpo) == 10


class TestCalcKtn:
    def test_ktn_output(self, sample_ohlc):
        prev_close = sample_ohlc['close'].shift(1)
        mid, upper, lower = calc_ktn(sample_ohlc['high'], sample_ohlc['low'],
                                     sample_ohlc['close'], prev_close, n=20, m=2)
        assert len(mid) == 50
        assert (upper >= mid).all()
        assert (lower <= mid).all()


class TestCalcMtm:
    def test_mtm_output(self, sample_close):
        mtm, mtmma = calc_mtm(sample_close, n=6, m=3)
        assert len(mtm) == 10


class TestCalcTaq:
    def test_taq_output(self, sample_ohlc):
        up, down, mid = calc_taq(sample_ohlc['high'], sample_ohlc['low'], n=10)
        assert len(up) == 50
        assert (up >= down).all()


class TestCalcXsii:
    def test_xsii_output(self, sample_close):
        td1, td2, td3, td4 = calc_xsii(sample_close)
        assert len(td1) == 10
        assert (td1 >= td2).all()


class TestCalcExpma:
    def test_expma_output(self, sample_close):
        e12, e50 = calc_expma(sample_close)
        assert len(e12) == 10


class TestCalcAtr:
    def test_atr_output(self, sample_ohlc):
        prev_close = sample_ohlc['close'].shift(1)
        result = calc_atr(sample_ohlc['high'], sample_ohlc['low'], prev_close, n=14)
        assert len(result) == 50
        assert (result >= 0).all()


class TestCalcBbi:
    def test_bbi_output(self, sample_close):
        result = calc_bbi(sample_close)
        assert len(result) == 10


# ═══════════════════════════════════════════════════════
# Registry 覆盖测试
# ═══════════════════════════════════════════════════════

class TestRegistryCoverage:
    """确保 SAVE_COLS 中的每个 _qfq 指标列在 Registry 中有对应条目"""

    def test_all_qfq_cols_in_registry(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from config import SAVE_COLS

        qfq_indicator_cols = [c for c in SAVE_COLS
                             if c.endswith('_qfq') and c not in
                             ('open_qfq', 'high_qfq', 'low_qfq', 'close_qfq')]

        # 收集 Registry 所有输出列
        from custom_indicators import _resolve_output_cols
        registry_cols = set()
        for base_name, entry in INDICATOR_REGISTRY.items():
            for variant in entry['variants']:
                params = {**entry['params'], **variant}
                for col in _resolve_output_cols(entry, params):
                    registry_cols.add(col)

        missing = [c for c in qfq_indicator_cols if c not in registry_cols]
        assert missing == [], f"以下 QFQ 列未在 Registry 注册: {missing}"

    def test_all_registry_formula_have_func(self):
        for name, entry in INDICATOR_REGISTRY.items():
            if entry['category'] == 'formula':
                assert entry['func'] is not None, f"{name} 标记为 formula 但 func 为 None"
            elif entry['category'] == 'not_implemented':
                assert entry['func'] is None, f"{name} 标记为 not_implemented 但 func 不为 None"


# ═══════════════════════════════════════════════════════
# ETF 场景测试
# ═══════════════════════════════════════════════════════

class TestComputeIndicatorsEtf:
    """ETF 数据无 bfq 列，所有指标从 QFQ 价格公式计算"""

    def test_formula_indicators_computed(self, sample_ohlc):
        df = sample_ohlc.copy()
        df['qfq_close'] = df['close']
        df['qfq_high'] = df['high']
        df['qfq_low'] = df['low']

        compute_indicators(df, ['ma_5', 'macd', 'kdj'],
                          close_col='qfq_close', high_col='qfq_high', low_col='qfq_low')

        assert 'ma_qfq_5' in df.columns
        assert 'macd_dif_qfq' in df.columns
        assert 'kdj_k_qfq' in df.columns
        assert not df['ma_qfq_5'].isna().all()

    def test_not_implemented_fills_nan(self, sample_ohlc):
        df = sample_ohlc.copy()
        df['qfq_close'] = df['close']

        compute_indicators(df, ['obv', 'mfi'],
                          close_col='qfq_close', log_missing=False)

        assert 'obv_qfq' in df.columns
        assert 'mfi_qfq' in df.columns
        assert df['obv_qfq'].isna().all()
        assert df['mfi_qfq'].isna().all()


# ═══════════════════════════════════════════════════════
# 命名约定测试
# ═══════════════════════════════════════════════════════

class TestNamingNoSuffix:
    """输出列名不带参数后缀"""

    def test_kdj_no_suffix(self, sample_ohlc):
        df = sample_ohlc.copy()
        df['qfq_close'] = df['close']
        df['qfq_high'] = df['high']
        df['qfq_low'] = df['low']
        df['qfq_pre_close'] = df['close'].shift(1)

        add_dynamic_indicator(df, 'kdj_9_3_3')

        assert 'kdj_k_qfq' in df.columns
        assert 'kdj_k_qfq_9_3_3' not in df.columns

    def test_ma_no_suffix(self, sample_ohlc):
        df = sample_ohlc.copy()
        df['qfq_close'] = df['close']
        df['qfq_high'] = df['high']
        df['qfq_low'] = df['low']

        add_dynamic_indicator(df, 'ma_5')

        assert 'ma_qfq_5' in df.columns
        assert 'ma_qfq_5_5' not in df.columns


# ═══════════════════════════════════════════════════════
# apply_qfq_indicators 测试
# ═══════════════════════════════════════════════════════

class TestApplyQfqIndicators:
    """测试混合策略函数"""

    def test_invariant_bfq_copy(self):
        n = 20
        df = pd.DataFrame({
            'qfq_close': np.random.randn(n).cumsum() + 10,
            'qfq_high': np.random.randn(n).cumsum() + 11,
            'qfq_low': np.random.randn(n).cumsum() + 9,
            'kdj_k_bfq': np.random.rand(n) * 100,
            'kdj_d_bfq': np.random.rand(n) * 100,
            'kdj_bfq': np.random.rand(n) * 100,
            'rsi_bfq_12': np.random.rand(n) * 100,
            '_ratio': 1.0,
        })

        apply_qfq_indicators(df, ['kdj', 'rsi_12'])

        assert 'kdj_k_qfq' in df.columns
        assert 'rsi_qfq_12' in df.columns
        assert (df['kdj_k_qfq'] == df['kdj_k_bfq']).all()
        assert (df['rsi_qfq_12'] == df['rsi_bfq_12']).all()

    def test_scale_bfq_ratio(self):
        n = 20
        ratio = pd.Series(np.linspace(0.9, 1.1, n), name='_ratio')
        df = pd.DataFrame({
            'qfq_close': np.random.randn(n).cumsum() + 10,
            'qfq_high': np.random.randn(n).cumsum() + 11,
            'qfq_low': np.random.randn(n).cumsum() + 9,
            'atr_bfq': np.random.rand(n) * 2 + 0.5,
            '_ratio': ratio,
        })

        apply_qfq_indicators(df, ['atr'])

        assert 'atr_qfq' in df.columns
        expected = df['atr_bfq'] * ratio
        assert np.allclose(df['atr_qfq'].values, expected.values)

    def test_formula_fallback(self):
        n = 30
        df = pd.DataFrame({
            'qfq_close': np.random.randn(n).cumsum() + 10,
            'qfq_high': np.random.randn(n).cumsum() + 11,
            'qfq_low': np.random.randn(n).cumsum() + 9,
            '_ratio': 1.0,
        })
        # 没有 bfq 列，ma_5 应走公式
        apply_qfq_indicators(df, ['ma_5'])

        assert 'ma_qfq_5' in df.columns
        assert not df['ma_qfq_5'].isna().all()

    def test_mixed_indicators(self):
        n = 30
        df = pd.DataFrame({
            'qfq_close': np.random.randn(n).cumsum() + 10,
            'qfq_high': np.random.randn(n).cumsum() + 11,
            'qfq_low': np.random.randn(n).cumsum() + 9,
            'kdj_k_bfq': np.random.rand(n) * 100,
            'kdj_d_bfq': np.random.rand(n) * 100,
            'kdj_bfq': np.random.rand(n) * 100,
            'atr_bfq': np.random.rand(n) * 2 + 0.5,
            '_ratio': 1.0,
        })

        apply_qfq_indicators(df, ['kdj', 'atr', 'ma_5', 'macd'])

        assert 'kdj_k_qfq' in df.columns
        assert 'atr_qfq' in df.columns
        assert 'ma_qfq_5' in df.columns
        assert 'macd_dif_qfq' in df.columns
        assert not df['ma_qfq_5'].isna().all()
        assert not df['macd_dif_qfq'].isna().all()


# ═══════════════════════════════════════════════════════
# recalc_all_qfq / recalc_qfq_by_formula 行为测试
# ═══════════════════════════════════════════════════════

class TestRecalcQfq:
    """验证 refactored recalc 函数基本行为"""

    def _make_df(self):
        n = 50
        np.random.seed(99)
        df = pd.DataFrame({
            'trade_date': pd.date_range('2024-01-01', periods=n, freq='D').strftime('%Y%m%d'),
            'open': np.random.randn(n).cumsum() + 10,
            'high': np.random.randn(n).cumsum() + 11,
            'low': np.random.randn(n).cumsum() + 9,
            'close': np.random.randn(n).cumsum() + 10,
            'adj_factor': np.linspace(1.0, 1.5, n),
        })
        # 添加所有 qfq 和 bfq 列
        for src, dst in [('open', 'open_qfq'), ('high', 'high_qfq'),
                         ('low', 'low_qfq'), ('close', 'close_qfq')]:
            df[dst] = df[src] * df['adj_factor'] / df['adj_factor'].iloc[-1]

        # 添加 bfq 指标列（用随机值填充以测试 invariant_map/scale_cols）
        for bfq_col in set(INVARIANT_MAP.values()) | set(SCALE_COLS.keys()):
            df[bfq_col] = np.random.randn(n).cumsum() + 50

        # 添加所有 qfq 指标列
        from custom_indicators import _resolve_output_cols
        for entry in INDICATOR_REGISTRY.values():
            for variant in entry['variants']:
                params = {**entry['params'], **variant}
                for col in _resolve_output_cols(entry, params):
                    if col not in df.columns:
                        df[col] = np.nan

        return df

    def test_recalc_all_qfq_no_error(self):
        df = self._make_df()
        result = recalc_all_qfq(df, decimals=2)
        assert result is not None
        assert len(result) == 50
        assert not result['close_qfq'].isna().all()

    def test_recalc_by_formula_no_error(self):
        df = self._make_df()
        result = recalc_qfq_by_formula(df, decimals=2)
        assert result is not None
        assert len(result) == 50

    def test_invariant_columns_preserved(self):
        df = self._make_df()
        result = recalc_all_qfq(df, decimals=2)
        # KDJ qfq 应等于 bfq
        for qfq_col, bfq_col in INVARIANT_MAP.items():
            if bfq_col in df.columns and qfq_col in df.columns:
                assert (result[qfq_col] == result[bfq_col]).all(), \
                    f"{qfq_col} != {bfq_col}"

    def test_empty_df(self):
        assert recalc_all_qfq(pd.DataFrame()) is not None
        assert recalc_qfq_by_formula(pd.DataFrame()) is not None

    def test_zero_adj_factor_early_return(self):
        """adj_factor 为 0 时应提前返回原数据"""
        df = pd.DataFrame({'close': [1.0, 2.0, 3.0], 'adj_factor': [0.0, 0.0, 0.0]})
        result = recalc_all_qfq(df)
        assert len(result) == 3
        assert 'close' in result.columns
