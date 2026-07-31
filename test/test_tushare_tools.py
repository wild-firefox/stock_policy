import numpy as np
import pandas as pd

from tushare_tools import tools


class FakeFinancialPro:
    def __init__(self, indicator_data=None, cashflow_data=None):
        self.indicator_data = indicator_data
        self.cashflow_data = cashflow_data
        self.indicator_calls = []
        self.cashflow_calls = []

    def fina_indicator(self, **kwargs):
        self.indicator_calls.append(kwargs)
        return self.indicator_data.copy()

    def cashflow(self, **kwargs):
        self.cashflow_calls.append(kwargs)
        return self.cashflow_data.copy()


def test_stock_financial_trend_returns_latest_quarters_in_time_order():
    dates = pd.date_range("2023-03-31", periods=10, freq="QE").strftime("%Y%m%d")
    source = pd.DataFrame(
        {
            "ts_code": ["600519.SH"] * 10,
            "ann_date": dates,
            "end_date": dates,
            "current_ratio": np.arange(10, dtype=float),
            "grossprofit_margin": np.arange(10, dtype=float) + 50,
            "roe": np.arange(10, dtype=float) + 20,
            "debt_to_assets": np.arange(10, dtype=float) + 10,
        }
    ).sample(frac=1, random_state=1)
    pro = FakeFinancialPro(indicator_data=source)

    result = tools.get_stock_financial_trend("600519.SH", periods=8, pro=pro)

    assert result["end_date"].tolist() == list(dates[-8:])
    assert result.columns.tolist() == [
        "ts_code",
        "ann_date",
        "end_date",
        "current_ratio",
        "grossprofit_margin",
        "roe",
        "debt_to_assets",
    ]
    assert pro.indicator_calls[0]["ts_code"] == "600519.SH"


def test_stock_fcf_trend_calculates_latest_quarters_separately():
    dates = pd.date_range("2024-03-31", periods=9, freq="QE").strftime("%Y%m%d")
    source = pd.DataFrame(
        {
            "ts_code": ["600519.SH"] * 9,
            "ann_date": dates,
            "f_ann_date": dates,
            "end_date": dates,
            "n_cashflow_act": [100.0, np.nan, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0],
            "c_pay_acq_const_fiolta": [20.0] * 9,
        }
    )
    pro = FakeFinancialPro(cashflow_data=source)

    result = tools.get_stock_fcf_trend("600519.SH", periods=8, pro=pro)

    assert result["end_date"].tolist() == list(dates[-8:])
    assert result["fcf"].tolist() == [
        -20.0,
        100.0,
        110.0,
        120.0,
        130.0,
        140.0,
        150.0,
        160.0,
    ]
    assert pro.cashflow_calls[0]["ts_code"] == "600519.SH"
