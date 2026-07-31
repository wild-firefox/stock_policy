import sys

import agent_predict


def _history_entry(report_date, report_time, target_date):
    return f"""### 预测时间: {report_date} {report_time}

**【标的】601138 工业富联**

| 预测内容 | 实际情况 | 回测结果 |
| --- | --- | --- |
| 【{target_date} 方向】涨 【信心】7 | 实际方向: 涨 | **[对]** |
| 【{target_date} 涨跌幅】-1% ~ 3% 【信心】6 | 实际涨跌幅: +1.5% | **[对]** |

<details>
<summary>详细逻辑与交易计划</summary>

> 【1日逻辑理由】量价同步改善，短线偏强。
> 【3日逻辑理由】这里只用于验证过滤。
> 【5日逻辑理由】这里只用于验证过滤。
> --- 实战交易计划 ---
> 【逻辑失效价位】跌破前低则失效。
> 【盘中验证信号】观察开盘量能。
> 【盈亏比与仓位】小仓位验证。
</details>

**【1日复盘归因】**

- 方向和区间均符合实际结果。

**【3日复盘归因】**

- 不应进入1日精简历史。
"""


def _write_prediction_marker(predict_root, report_date, report_time):
    marker = (
        predict_root
        / report_date.replace("-", "")
        / "stk"
        / "601138"
        / report_time.replace(":", "")
        / "601138.md"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("- **类型**: stk\n", encoding="utf-8")


def test_extract_previous_two_trade_days_from_self_contained_report(tmp_path):
    predict_root = tmp_path / "predict" / "profile"
    report_path = predict_root / "experience" / "predict_eval_history.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _history_entry("2026-07-15", "23:00:00", "2026-07-16")
        + "\n---\n\n"
        + _history_entry("2026-07-16", "23:01:00", "2026-07-17"),
        encoding="utf-8",
    )
    _write_prediction_marker(predict_root, "2026-07-15", "23:00:00")
    _write_prediction_marker(predict_root, "2026-07-16", "23:01:00")

    history = agent_predict.get_recent_prediction_reflection_history(
        asset_type="stk",
        code="601138",
        previous_trade_dates=["20260716", "20260717"],
        predict_root=str(predict_root),
        report_path=str(report_path),
    )

    assert "【2026-07-16 方向】" in history
    assert "【2026-07-16 涨跌幅】" in history
    assert "【2026-07-17 方向】" in history
    assert "【2026-07-17 涨跌幅】" in history
    assert history.count("【1日逻辑理由】") == 2
    assert history.count("--- 实战交易计划 ---") == 2
    assert history.count("**【1日复盘归因】**") == 2
    assert "【3日逻辑理由】" not in history
    assert "【5日逻辑理由】" not in history
    assert "**【3日复盘归因】**" not in history
    assert "**【5日复盘归因】**" not in history

    output_encoding = sys.stdout.encoding or "utf-8"
    safe_history = history.encode(output_encoding, errors="replace").decode(
        output_encoding
    )
    print("\n" + safe_history)
