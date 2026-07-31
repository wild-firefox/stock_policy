import inspect

import pandas as pd
import pytest

from tushare_tools import high_freq_data


class FakePro:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def stk_factor_pro(self, **kwargs):
        self.calls.append(kwargs)
        return self.data.copy()

    def moneyflow(self, **kwargs):
        self.calls.append(kwargs)
        return self.data.copy()


def test_daily_qfq_reuses_project_recalc_and_selects_save_cols(monkeypatch):
    source = pd.DataFrame(
        {
            "trade_date": ["20260627", "20260625", "20260626"],
            "open": [11.0, 9.0, 10.0],
            "high": [12.0, 10.0, 11.0],
            "low": [10.0, 8.0, 9.0],
            "close": [11.5, 9.5, 10.5],
            "adj_factor": [2.0, 1.0, 1.5],
            "open_qfq": [110.0, 90.0, 100.0],
            "high_qfq": [120.0, 100.0, 110.0],
            "low_qfq": [100.0, 80.0, 90.0],
            "close_qfq": [115.0, 95.0, 105.0],
            "ma_qfq_5": [114.0, 94.0, 104.0],
            "macd_dif_qfq": [1.1, 0.9, 1.0],
            "rsi_qfq_6": [61.0, 59.0, 60.0],
            "boll_upper_qfq": [121.0, 101.0, 111.0],
            "kdj_k_qfq": [71.0, 69.0, 70.0],
            "vol": [300.0, 100.0, 200.0],
            "amount": [3000.0, 1000.0, 2000.0],
            "turnover_rate_f": [3.0, 1.0, 2.0],
            "not_in_save_cols": ["drop", "drop", "drop"],
        }
    )
    fake_pro = FakePro(source)
    recalc_inputs = []

    def fake_recalc(df, decimals=2):
        recalc_inputs.append(df.copy())
        result = df.copy()
        result["close_qfq"] = result["close_qfq"] + 0.01
        return result

    monkeypatch.setattr(high_freq_data, "get_pro", lambda: fake_pro)
    monkeypatch.setattr(high_freq_data, "recalc_all_qfq", fake_recalc, raising=False)

    result = high_freq_data.get_daily_qfq_with_indicators(
        "600519.SH", end_date="2026-06-26", trade_days=2
    )

    assert len(recalc_inputs) == 1
    assert recalc_inputs[0]["trade_date"].tolist() == [
        "20260625",
        "20260626",
    ]
    assert result["trade_date"].tolist() == ["20260626", "20260625"]
    assert result["close_qfq"].tolist() == [105.01, 95.01]
    assert fake_pro.calls[0]["end_date"] == "20260626"
    assert fake_pro.calls[0]["start_date"] < "20260625"
    assert "ma_qfq_5" in result.columns
    assert "macd_dif_qfq" in result.columns
    assert "rsi_qfq_6" in result.columns
    assert "boll_upper_qfq" in result.columns
    assert "kdj_k_qfq" in result.columns
    assert "not_in_save_cols" not in result.columns
    assert set(result.columns).issubset(set(high_freq_data.SAVE_COLS))


def test_daily_qfq_returns_empty_frame_when_api_has_no_data(monkeypatch):
    monkeypatch.setattr(high_freq_data, "get_pro", lambda: FakePro(pd.DataFrame()))

    result = high_freq_data.get_daily_qfq_with_indicators(
        "600519.SH", end_date="2026-06-26", trade_days=120
    )

    assert result.empty


def test_daily_qfq_defaults_to_latest_120_trading_rows(monkeypatch):
    dates = pd.bdate_range(end="2026-06-26", periods=130).strftime("%Y%m%d")
    source = pd.DataFrame(
        {
            "trade_date": dates,
            "open": range(130),
            "high": range(130),
            "low": range(130),
            "close": range(130),
            "adj_factor": [1.0] * 130,
            "open_qfq": range(130),
            "high_qfq": range(130),
            "low_qfq": range(130),
            "close_qfq": range(130),
        }
    )
    monkeypatch.setattr(high_freq_data, "get_pro", lambda: FakePro(source))

    result = high_freq_data.get_daily_qfq_with_indicators(
        "600519.SH", end_date="2026-06-26", trade_days=120
    )

    assert len(result) == 120
    assert result.iloc[0]["trade_date"] == "20260626"
    assert result.iloc[-1]["trade_date"] == dates[-120]


def test_money_flow_defaults_to_latest_30_trading_rows(monkeypatch):
    dates = pd.bdate_range(end="2026-06-26", periods=40).strftime("%Y%m%d")
    fake_pro = FakePro(
        pd.DataFrame(
            {
                "trade_date": dates,
                "net_mf_amount": range(40),
            }
        )
    )
    monkeypatch.setattr(high_freq_data, "get_pro", lambda: fake_pro)

    result = high_freq_data.get_money_flow_data(
        "600519.SH", end_date="2026-06-26", trade_days=30
    )

    assert len(result) == 30
    assert result.iloc[0]["trade_date"] == "20260626"
    assert result.iloc[-1]["trade_date"] == dates[-30]
    assert fake_pro.calls[0]["end_date"] == "20260626"
    assert fake_pro.calls[0]["start_date"] < dates[-30]


def test_daily_qfq_start_date_takes_priority_over_trade_days(monkeypatch):
    dates = ["20260624", "20260625", "20260626"]
    source = pd.DataFrame(
        {
            "trade_date": dates,
            "open": [1.0, 2.0, 3.0],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [1.0, 2.0, 3.0],
            "adj_factor": [1.0, 1.0, 1.0],
            "open_qfq": [1.0, 2.0, 3.0],
            "high_qfq": [1.0, 2.0, 3.0],
            "low_qfq": [1.0, 2.0, 3.0],
            "close_qfq": [1.0, 2.0, 3.0],
        }
    )
    fake_pro = FakePro(source)
    monkeypatch.setattr(high_freq_data, "get_pro", lambda: fake_pro)

    result = high_freq_data.get_daily_qfq_with_indicators(
        "600519.SH",
        start_date="2026-06-25",
        end_date="2026-06-26",
        trade_days=1,
    )

    assert result["trade_date"].tolist() == ["20260626", "20260625"]
    assert fake_pro.calls[0]["start_date"] < "20260625"


def test_money_flow_start_date_takes_priority_over_trade_days(monkeypatch):
    fake_pro = FakePro(
        pd.DataFrame(
            {
                "trade_date": ["20260624", "20260625", "20260626"],
                "net_mf_amount": [1.0, 2.0, 3.0],
            }
        )
    )
    monkeypatch.setattr(high_freq_data, "get_pro", lambda: fake_pro)

    result = high_freq_data.get_money_flow_data(
        "600519.SH",
        start_date="2026-06-25",
        end_date="2026-06-26",
        trade_days=1,
    )

    assert result["trade_date"].tolist() == ["20260626", "20260625"]
    assert fake_pro.calls[0]["start_date"] == "20260625"


@pytest.mark.parametrize(
    ("func", "default_trade_days"),
    [
        (high_freq_data.get_daily_qfq_with_indicators, 120),
        (high_freq_data.get_money_flow_data, 30),
    ],
)
def test_date_range_uses_default_trade_days_and_rejects_explicit_none(
    func,
    default_trade_days,
):
    assert (
        inspect.signature(func).parameters["trade_days"].default
        == default_trade_days
    )

    with pytest.raises(ValueError, match="start_date or trade_days"):
        func("600519.SH", end_date="2026-06-26", trade_days=None)
