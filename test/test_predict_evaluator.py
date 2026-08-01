import predict_evaluator
import notion_sync_one
import sys
import types

import pandas as pd


def test_prediction_rules_blockquote_has_no_empty_quote_lines():
    markdown = predict_evaluator._format_rules_markdown("1.规则一\n2.规则二")

    assert markdown == "> 1.规则一\n> 2.规则二"
    assert "\n>\n" not in markdown


def test_legacy_reflection_task_id_is_normalized():
    assert predict_evaluator.normalize_reflection_task_id(
        "TASK_1_1D_STK_601138_20260717"
    ) == "1日任务: stk 601138 (20260717)"
    assert predict_evaluator.normalize_reflection_task_id(
        "TASK_4_3D_STK_601138_20260717"
    ) == "3日任务: stk 601138 (20260717)"


def test_parse_prediction_file_reads_reference_kb_footer(tmp_path):
    prediction_file = tmp_path / "159985.md"
    prediction_file.write_text(
        "# 159985 预测报告 (20260714 235208)\n\n"
        "- **类型**: etf\n"
        "- **状态**: success\n\n"
        "```text\n"
        "【标的】159985 华夏饲料豆粕期货ETF\n"
        "【2026-07-15 方向】涨 【信心】6\n"
        "【2026-07-15 涨跌幅】0% ~ 2% 【信心】6\n"
        "```\n\n"
        "<!-- 参考复盘知识库版本: predict_ex_20260714_235208.md -->\n",
        encoding="utf-8",
    )

    doc = predict_evaluator.parse_prediction_file(str(prediction_file))

    assert doc["reference_kb"] == "predict_ex_20260714_235208.md"


def test_parse_prediction_file_skips_missing_or_invalid_reference_kb(tmp_path):
    prediction_file = tmp_path / "601138.md"
    prediction_file.write_text(
        "# 601138 预测报告 (20260714 235208)\n\n"
        "- **类型**: stk\n"
        "- **状态**: success\n\n"
        "```text\n"
        "【标的】601138 工业富联\n"
        "【2026-07-15 方向】涨 【信心】6\n"
        "【2026-07-15 涨跌幅】0% ~ 2% 【信心】6\n"
        "```\n\n"
        "<!-- 参考复盘知识库版本: 无 -->\n",
        encoding="utf-8",
    )

    doc = predict_evaluator.parse_prediction_file(str(prediction_file))

    assert doc["reference_kb"] == ""


def test_notion_sync_removes_hidden_html_comments():
    markdown = "正文\n<!-- 参考复盘知识库版本: predict_ex_20260714_235208.md -->\n后文"

    visible_markdown = notion_sync_one._strip_html_comments(markdown)

    assert "参考复盘知识库版本" not in visible_markdown
    assert "正文" in visible_markdown
    assert "后文" in visible_markdown


def test_run_evaluation_writes_reference_kb_as_hidden_comment(
    tmp_path, monkeypatch
):
    fake_path = str(tmp_path / "20260714" / "etf" / "159985.md")
    doc = {
        "code": "159985",
        "name": "华夏饲料豆粕期货ETF",
        "atype": "etf",
        "report_date": "20260714",
        "report_time": "235208",
        "reference_kb": "predict_ex_20260714_235208.md",
        "predictions": [
            {
                "type": "pct_chg",
                "category": "1d",
                "target_start": "2026-07-15",
                "target_end": "2026-07-15",
                "value": "0% ~ 2%",
                "original_str": "【2026-07-15 涨跌幅】0% ~ 2%",
            }
        ],
        "plan_title": None,
        "plan_content": None,
    }
    monkeypatch.setattr(
        predict_evaluator.glob,
        "glob",
        lambda pattern: [fake_path] if pattern.endswith("*.md") else [],
    )
    monkeypatch.setattr(
        predict_evaluator,
        "parse_prediction_file",
        lambda path: doc.copy(),
    )
    monkeypatch.setattr(
        predict_evaluator,
        "fetch_actual_data",
        lambda ts_code, atype, start_ymd: pd.DataFrame(),
    )

    predict_evaluator.run_evaluation(str(tmp_path), page_id="")

    report = (
        tmp_path / "experience" / "predict_eval_history.md"
    ).read_text(encoding="utf-8")
    assert "### 预测时间: 2026-07-14 23:52:08" in report
    assert (
        "<!-- 参考复盘知识库版本: predict_ex_20260714_235208.md -->"
        in report
    )


def test_direction_rates_map_flat_to_up_for_win_and_down_for_loss():
    stats = predict_evaluator._new_stats()

    predict_evaluator._update_stats(
        stats, {"category": "direction", "value": "涨"}, "实际方向: 涨", "[对]"
    )
    predict_evaluator._update_stats(
        stats, {"category": "direction", "value": "平"}, "实际方向: 平", "[对]"
    )
    predict_evaluator._update_stats(
        stats, {"category": "direction", "value": "平"}, "实际方向: 涨", "[错]"
    )
    predict_evaluator._update_stats(
        stats, {"category": "direction", "value": "平"}, "实际方向: 跌", "[错]"
    )
    predict_evaluator._update_stats(
        stats, {"category": "direction", "value": "涨"}, "实际方向: 跌", "[错]"
    )

    rates = predict_evaluator._direction_rates(stats)

    assert rates["strict"] == (2, 5, 40.0)
    assert rates["flat_as_win"] == (2, 5, 40.0)
    assert rates["flat_as_loss"] == (2, 5, 40.0)


def test_recent_prediction_dates_use_latest_five_dates_present_in_records():
    dates = [
        "20260620",
        "20260618",
        "20260624",
        "20260619",
        "20260623",
        "20260622",
        "20260624",
    ]

    recent_dates = predict_evaluator._recent_prediction_dates(dates, limit=5)

    assert recent_dates == [
        "20260619",
        "20260620",
        "20260622",
        "20260623",
        "20260624",
    ]
    assert predict_evaluator._format_date_range(recent_dates) == (
        "2026-06-19 ~ 2026-06-24"
    )


def test_stats_markdown_contains_all_existing_and_new_win_rates():
    stats = predict_evaluator._new_stats()
    predict_evaluator._update_stats(
        stats,
        {"category": "direction", "value": "平"},
        "实际方向: 涨",
        "[错]",
    )
    predict_evaluator._update_stats(
        stats, {"category": "1d"}, "实际涨跌幅: +1.00%", "[对]"
    )
    predict_evaluator._update_stats(
        stats, {"category": "3d"}, "实际涨跌幅: +2.00%", "[错]"
    )
    predict_evaluator._update_stats(
        stats, {"category": "5d"}, "实际涨跌幅: +3.00%", "[对]"
    )

    markdown = predict_evaluator._format_stats_markdown(stats)

    assert "大盘/个股方向（严格匹配）" in markdown
    assert "方向胜率（平算胜）" in markdown
    assert "方向胜率（平算负）" in markdown
    assert "1日涨跌幅" in markdown
    assert "3日涨跌幅" in markdown
    assert "5日涨跌幅" in markdown


def test_run_evaluation_writes_global_and_recent_five_prediction_dates(
    tmp_path, monkeypatch
):
    report_dates = [
        "20260618",
        "20260619",
        "20260620",
        "20260622",
        "20260623",
        "20260624",
    ]
    fake_paths = [
        str(tmp_path / date / "stk" / f"{index}.md")
        for index, date in enumerate(report_dates)
    ]
    docs = {}
    for index, (path, report_date) in enumerate(zip(fake_paths, report_dates)):
        target_date = (
            f"{report_date[:4]}-{report_date[4:6]}-{report_date[6:]}"
        )
        docs[path] = {
            "code": f"600{index:03d}",
            "name": f"测试{index}",
            "atype": "stk",
            "report_date": report_date,
            "report_time": "120000",
            "predictions": [
                {
                    "type": "direction",
                    "category": "direction",
                    "target_start": target_date,
                    "target_end": target_date,
                    "value": "平",
                    "original_str": f"【{target_date} 方向】平",
                },
                {
                    "type": "pct_chg",
                    "category": "1d",
                    "target_start": target_date,
                    "target_end": target_date,
                    "value": "0% ~ 1%",
                    "original_str": f"【{target_date} 涨跌幅】0% ~ 1%",
                },
            ],
            "plan_title": None,
            "plan_content": None,
            "reference_kb": (
                "predict_ex_20260618_120000.md" if index == 0 else ""
            ),
        }

    monkeypatch.setattr(predict_evaluator.glob, "glob", lambda pattern: fake_paths)
    monkeypatch.setattr(
        predict_evaluator,
        "parse_prediction_file",
        lambda path: docs[path].copy(),
    )
    monkeypatch.setattr(
        predict_evaluator,
        "fetch_actual_data",
        lambda ts_code, atype, start_ymd: pd.DataFrame(),
    )
    monkeypatch.setattr(
        predict_evaluator,
        "evaluate_prediction",
        lambda pred, df: (
            ("实际方向: 平", "[对]")
            if pred["category"] == "direction"
            else ("实际涨跌幅: +0.50%", "[对]")
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "notion_sync_one",
        types.SimpleNamespace(
            sync_md_to_notion=lambda *args, **kwargs: None
        ),
    )

    predict_evaluator.run_evaluation(str(tmp_path))

    report = (
        tmp_path / "experience" / "predict_eval_history.md"
    ).read_text(encoding="utf-8")
    assert "预测胜率全局统计" in report
    assert "方向胜率（平算胜）**: 0.0% (0/6)" in report
    assert "方向胜率（平算负）**: 0.0% (0/6)" in report
    assert (
        "最近5个预测日胜率统计（2026-06-19 ~ 2026-06-24）"
        in report
    )
    assert "方向胜率（平算胜）**: 0.0% (0/5)" in report
    assert "按已经产生实际方向结果的一日预测目标日期" in report
    assert (
        "<!-- 参考复盘知识库版本: predict_ex_20260618_120000.md -->"
        in report
    )
