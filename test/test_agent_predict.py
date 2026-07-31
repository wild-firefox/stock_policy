"""Characterization tests for agent_predict.py refactoring."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import agent_predict


ORIGINAL_PREDICTION_CLI_RUNNER = getattr(
    agent_predict,
    "_run_prediction_claude_cli",
    None,
)


@pytest.fixture(autouse=True)
def disable_real_prediction_cli(monkeypatch):
    """现有 worker 测试默认让 CLI 失败，继续覆盖原有 API 行为。"""

    def fail_cli(*args, **kwargs):
        raise RuntimeError("测试中禁用真实 Claude CLI")

    monkeypatch.setattr(
        agent_predict,
        "_run_prediction_claude_cli",
        fail_cli,
        raising=False,
    )


def test_prediction_cli_receives_model_environment_and_full_context(monkeypatch):
    assert ORIGINAL_PREDICTION_CLI_RUNNER is not None
    monkeypatch.setattr(
        agent_predict,
        "_run_prediction_claude_cli",
        ORIGINAL_PREDICTION_CLI_RUNNER,
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="<result>CLI预测</result>", stderr="")

    monkeypatch.setattr(agent_predict.subprocess, "run", fake_run)

    result = agent_predict._run_prediction_claude_cli(
        [
            {"role": "system", "content": "系统规则"},
            {"role": "user", "content": "初始数据"},
            {"role": "assistant", "content": "<claude>获取数据</claude>"},
            {"role": "user", "content": "工具返回数据"},
        ],
        model_name="deepseek-v4-pro",
    )

    assert result == "<result>CLI预测</result>"
    assert captured["cmd"] == [
        agent_predict.CLAUDE_EXE,
        "-p",
        "--verbose",
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--model",
        "deepseek-v4-pro",
    ]
    assert captured["kwargs"]["env"]["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert captured["kwargs"]["env"]["TUSHARE_TOKEN"] == agent_predict.TUSHARE_TOKEN
    assert captured["kwargs"]["cwd"] == agent_predict.BASE_DIR
    assert "系统规则" in captured["kwargs"]["input"]
    assert "初始数据" in captured["kwargs"]["input"]
    assert "<claude>获取数据</claude>" in captured["kwargs"]["input"]
    assert "工具返回数据" in captured["kwargs"]["input"]
    assert "timeout" not in captured["kwargs"]


def test_local_prediction_evaluation_refresh_disables_web_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(
        agent_predict.predict_evaluator,
        "run_evaluation",
        lambda **kwargs: calls.append(kwargs),
    )

    refreshed = agent_predict._update_prediction_evaluation(
        "model-root",
        sync_web=False,
    )

    assert refreshed is True
    assert calls == [{"predict_dir": "model-root", "page_id": ""}]


def test_prediction_prompt_mentions_recent_history_when_preloaded():
    dates_info = {
        "label_1d": "2026-07-20",
        "label_3d": "2026-07-20~2026-07-22",
        "label_5d": "2026-07-20~2026-07-24",
        "ts_dates": {
            "prev_td": "20260716",
            "data_td": "20260717",
            "next_td": "20260720",
        },
    }

    prompt = agent_predict._build_prediction_user_prompt(
        "stk",
        "股票",
        "601138 工业富联",
        dates_info,
        agent_predict._build_trading_plan_text("stk"),
        has_recent_history=True,
    )

    assert "<recent_prediction_reflection_history>" in prompt
    assert "前两个交易日" in prompt


def test_worker_injects_recent_history_as_pre_fetched_module(
    tmp_path, monkeypatch
):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(
                content="<result>结构化预测</result>",
                reasoning_content="",
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    dates_info = {
        "label_1d": "2026-07-20",
        "label_3d": "2026-07-20~2026-07-22",
        "label_5d": "2026-07-20~2026-07-24",
        "ts_dates": {
            "prev_td": "20260716",
            "data_td": "20260717",
            "next_td": "20260720",
        },
        "suffix": "",
        "is_trading_day": False,
    }
    recent_history = (
        "<recent_prediction_reflection_history>\n"
        "### 2026-07-17\n历史记录\n"
        "</recent_prediction_reflection_history>"
    )
    monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
    monkeypatch.setattr(agent_predict, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        agent_predict,
        "get_target_name",
        lambda code, asset_type: "沪深300",
    )
    monkeypatch.setattr(
        agent_predict,
        "fetch_realtime_15min_kline",
        lambda *args, **kwargs: "(获取失败)",
    )

    result = agent_predict.agent_worker(
        "000300",
        "idx",
        "",
        "无",
        "20260719",
        "120000",
        dates_info=dates_info,
        use_preload=False,
        preloaded_recent_history=recent_history,
    )

    assert recent_history in captured["messages"][1]["content"]
    assert "前两个交易日精简预测/复盘" in result["pre_fetched"]


def test_reflection_analysis_uses_system_owned_task_id_by_output_order():
    response_text = """
<task_analysis>
<reflection>方向分析</reflection>
<plan_1>失效条件分析</plan_1>
<plan_2>盘中信号分析</plan_2>
<plan_3>仓位分析</plan_3>
</task_analysis>
"""
    tasks_metadata = [
        {
            "id": "1日任务: stk 601138 (20260717)",
            "type": "1d",
            "atype": "stk",
            "ctx": "",
        }
    ]

    parsed = agent_predict._parse_reflection_analyses(
        response_text,
        tasks_metadata,
    )

    assert list(parsed) == ["1日任务: stk 601138 (20260717)"]
    assert parsed["1日任务: stk 601138 (20260717)"]["reflection"] == "方向分析"


def test_reflection_analysis_rejects_missing_output_block():
    tasks_metadata = [
        {"id": "1日任务: stk 601138 (20260717)"},
        {"id": "3日任务: stk 601138 (20260717)"},
    ]

    with pytest.raises(ValueError, match="期望 2 个，实际 1 个"):
        agent_predict._parse_reflection_analyses(
            "<task_analysis><reflection>只有一个</reflection></task_analysis>",
            tasks_metadata,
        )


def test_reflect_only_updates_prediction_evaluation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        agent_predict,
        "get_predict_target_dates",
        lambda: {"write_date": "20260718", "suffix": ""},
    )
    monkeypatch.setattr(
        agent_predict,
        "run_reflection",
        lambda run_date, run_time: ("复盘知识库", "predict_ex_20260718_120000.md"),
    )
    monkeypatch.setattr(
        agent_predict.predict_evaluator,
        "run_evaluation",
        lambda **kwargs: calls.append(kwargs),
    )

    agent_predict.main([], [], [], reflect_only=True)

    assert calls == [
        {
            "predict_dir": agent_predict.os.path.join(
                agent_predict.PREDICT_DIR, agent_predict.MODEL_PREDICT_CLI_NAME
            ),
            "page_id": agent_predict.NOTION_PAGE_ID,
        }
    ]


class FakeCalendarPro:
    def trade_cal(self, **kwargs):
        return pd.DataFrame(
            {
                "cal_date": [
                    "20260619",
                    "20260622",
                    "20260623",
                    "20260624",
                    "20260625",
                    "20260626",
                    "20260629",
                    "20260630",
                ],
                "is_open": [1] * 8,
            }
        )


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 25, 20, 0, 0)


class FakeReflectionPro:
    trading_days = [
        "20260615",
        "20260616",
        "20260617",
        "20260618",
        "20260619",
        "20260622",
        "20260623",
        "20260624",
        "20260625",
    ]

    def trade_cal(self, **kwargs):
        return pd.DataFrame({"cal_date": self.trading_days, "is_open": [1] * len(self.trading_days)})

    def index_daily(self, **kwargs):
        trade_date = kwargs.get("trade_date")
        if trade_date:
            return pd.DataFrame({"trade_date": [trade_date], "close": [100.0]})
        return pd.DataFrame({"trade_date": self.trading_days, "close": [100.0] * len(self.trading_days)})

    def daily(self, **kwargs):
        start_date = kwargs.get("start_date")
        days = [d for d in self.trading_days if d >= start_date]
        return pd.DataFrame(
            {
                "trade_date": days,
                "pre_close": [100.0] * len(days),
                "close": [101.0] * len(days),
            }
        )


class TestTsCode:
    def test_stock_exchange_suffixes(self):
        assert agent_predict._get_ts_code("600519", "stk") == "600519.SH"
        assert agent_predict._get_ts_code("000001", "stk") == "000001.SZ"
        assert agent_predict._get_ts_code("830000", "stk") == "830000.BJ"

    def test_etf_index_and_existing_suffix(self):
        assert agent_predict._get_ts_code("510300", "etf") == "510300.SH"
        assert agent_predict._get_ts_code("159915", "etf") == "159915.SZ"
        assert agent_predict._get_ts_code("399001", "idx") == "399001.SZ"
        assert agent_predict._get_ts_code("000300.SH", "idx") == "000300.SH"


class TestPredictTargetDates:
    def test_intraday_uses_previous_close(self, monkeypatch):
        monkeypatch.setattr(agent_predict, "get_pro", lambda: FakeCalendarPro())

        result = agent_predict.get_predict_target_dates(datetime(2026, 6, 23, 14, 0))

        assert result["label_1d"] == "2026-06-23"
        assert result["label_3d"] == "2026-06-23~2026-06-25"
        assert result["label_5d"] == "2026-06-23~2026-06-29"
        assert result["ts_dates"]["prev_td"] == "2026-06-19"
        assert result["ts_dates"]["data_td"] == "2026-06-22"
        assert result["suffix"] == ""

    def test_after_close_before_data_ready_marks_nowind(self, monkeypatch):
        monkeypatch.setattr(agent_predict, "get_pro", lambda: FakeCalendarPro())

        result = agent_predict.get_predict_target_dates(datetime(2026, 6, 23, 16, 0))

        assert result["label_1d"] == "2026-06-24"
        assert result["label_3d"] == "2026-06-24~2026-06-26"
        assert result["label_5d"] == "2026-06-24~2026-06-30"
        assert result["ts_dates"]["data_td"] == "2026-06-23"
        assert result["suffix"] == "_nowind"


class TestNewsContext:
    def test_latest_listing_and_local_articles_are_combined(self, tmp_path, monkeypatch):
        stock_dir = tmp_path / "600519"
        date_dir = stock_dir / "20260623"
        articles_dir = stock_dir / "articles"
        images_dir = articles_dir / "images"
        date_dir.mkdir(parents=True)
        images_dir.mkdir(parents=True)

        article_path = articles_dir / "news.md"
        image_path = images_dir / "chart.png"
        image_path.write_bytes(b"png")
        article_path.write_text("正文\n\n![](images/chart.png)\n", encoding="utf-8")
        listing_path = date_dir / "600519_新闻公告_120000.md"
        listing_path.write_text("[本地](../articles/news.md)", encoding="utf-8")

        monkeypatch.setattr(agent_predict, "NEWS_BASE_DIR", str(tmp_path))
        context, links, paths = agent_predict.get_latest_news_content("600519")

        assert "核心公告摘要: 600519_新闻公告_120000.md" in context
        assert "新闻正文: news.md" in context
        assert links == ["../articles/news.md"]
        assert paths == [str(listing_path), str(article_path), str(image_path)]

    def test_empty_underscore_listing_is_skipped_for_same_day(self, tmp_path, monkeypatch):
        stock_dir = tmp_path / "601138"
        date_dir = stock_dir / "20260625"
        date_dir.mkdir(parents=True)

        empty_latest = date_dir / "_新闻公告_235959.md"
        valid_listing = date_dir / "601138_新闻公告_120000.md"
        empty_latest.write_text("# （）新闻公告\n\n---\n", encoding="utf-8")
        valid_listing.write_text(
            "# 工业富联（601138）新闻公告\n\n"
            "---\n\n"
            "## 热点新闻\n\n"
            "- `06/25` [工业富联：6月24日获融资买入15.27亿元](https://news.10jqka.com.cn/20260625/c677694777.shtml)\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(agent_predict, "NEWS_BASE_DIR", str(tmp_path))
        context, links, paths = agent_predict.get_latest_news_content("601138")

        assert "核心公告摘要: 601138_新闻公告_120000.md" in context
        assert "工业富联：6月24日获融资买入15.27亿元" in context
        assert paths == [str(valid_listing)]


class TestClaudeRunner:
    def test_stream_json_parsing_and_subprocess_contract(self, tmp_path, monkeypatch):
        events = [
            {"type": "system", "subtype": "init", "session_id": "session-1"},
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Bash"},
            },
            {
                "type": "content_block_delta",
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps({"command": "uv run python query.py"}),
                },
            },
            {"type": "content_block_stop"},
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "查询完成"},
            },
        ]
        stdout = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(agent_predict.subprocess, "run", fake_run)

        text, tool_log, session_id = agent_predict.run_claude_code(
            "查询行情",
            work_dir=str(tmp_path),
        )

        assert text == "查询完成"
        assert "uv run python query.py" in tool_log
        assert session_id == "session-1"
        assert captured["kwargs"]["cwd"] == str(tmp_path)
        assert captured["kwargs"]["input"].endswith("=== 原始任务 ===\n\n查询行情")
        assert "无论数据时间跨度长短" in captured["kwargs"]["input"]
        assert "否则直接回答即可" not in captured["kwargs"]["input"]
        assert 'PowerShell(uv run python "' in captured["kwargs"]["input"]
        assert "错误范例" in captured["kwargs"]["input"]
        assert "PowerShell(python " in captured["kwargs"]["input"]
        assert "--settings" in captured["cmd"]

    def test_reflection_runner_sets_early_auto_compact_window(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(agent_predict.subprocess, "run", fake_run)

        agent_predict.run_claude_code(
            "执行复盘",
            work_dir=str(tmp_path),
            is_reflection=True,
        )

        assert captured["kwargs"]["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "800000"
        expected_memory_dir = agent_predict.os.path.abspath(
            agent_predict.os.path.join(
                agent_predict.BASE_DIR,
                ".claude",
                "memory",
                "strategy_research",
            )
        )
        assert expected_memory_dir in captured["cmd"]
        assert expected_memory_dir in captured["kwargs"]["input"]

    def test_reflection_runner_creates_missing_strategy_memory_dir(self, tmp_path, monkeypatch):
        captured = {}
        project_dir = tmp_path / "project"
        work_dir = tmp_path / "work"

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(agent_predict, "BASE_DIR", str(project_dir))
        monkeypatch.setattr(agent_predict.subprocess, "run", fake_run)

        agent_predict.run_claude_code(
            "执行复盘",
            work_dir=str(work_dir),
            is_reflection=True,
        )

        expected_memory_dir = project_dir / ".claude" / "memory" / "strategy_research"
        assert expected_memory_dir.is_dir()
        assert str(expected_memory_dir) in captured["cmd"]

    def test_compact_reflection_session_sends_raw_slash_command(self, tmp_path, monkeypatch):
        captured = {}
        stdout = json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"preTokens": 1000, "postTokens": 100},
            }
        )

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(agent_predict.subprocess, "run", fake_run)

        agent_predict._compact_claude_session(
            "session-old",
            work_dir=str(tmp_path),
            model_name="deepseek-v4-pro",
        )

        assert captured["kwargs"]["input"] == agent_predict.REFLECTION_COMPACT_COMMAND
        assert captured["kwargs"]["input"].startswith("/compact ")
        assert "--resume" in captured["cmd"]
        assert "session-old" in captured["cmd"]
        assert captured["kwargs"]["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "800000"

    @pytest.mark.parametrize(
        "stdout",
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "local_command",
                    "content": "<local-command-stderr>Error during compaction: Prompt is too long</local-command-stderr>",
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "Error during compaction",
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "is_error": False}),
        ],
        ids=["local-command-stderr", "is-error", "missing-boundary"],
    )
    def test_compact_reflection_session_rejects_false_success(self, stdout, tmp_path, monkeypatch):
        monkeypatch.setattr(
            agent_predict.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
        )

        with pytest.raises(agent_predict.ClaudeCompactionError):
            agent_predict._compact_claude_session(
                "session-old",
                work_dir=str(tmp_path),
                model_name="deepseek-v4-pro",
            )

    def test_context_overflow_compacts_and_retries_same_session(self, monkeypatch):
        calls = []
        compacted = []

        def fake_run_claude_code(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("maximum context length exceeded")
            return "完成", "日志", "session-old"

        monkeypatch.setattr(agent_predict, "run_claude_code", fake_run_claude_code)
        monkeypatch.setattr(
            agent_predict,
            "_compact_claude_session",
            lambda session_id, **kwargs: compacted.append(session_id),
        )

        result = agent_predict._run_reflection_claude_with_context_recovery(
            prompt="当前补充任务",
            work_dir="tools",
            news_files=[],
            session_id="session-old",
            model_name="deepseek-v4-pro",
        )

        assert result == ("完成", "日志", "session-old")
        assert compacted == ["session-old"]
        assert [call["session_id"] for call in calls] == ["session-old", "session-old"]
        assert calls[1]["prompt"] == "当前补充任务"

    def test_failed_compaction_stops_without_retrying_or_starting_new_session(self, monkeypatch):
        calls = []

        def fake_run_claude_code(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("Prompt is too long")
            return "完成", "日志", "session-new"

        def fail_compaction(*args, **kwargs):
            raise RuntimeError("Error during compaction: Conversation too long")

        monkeypatch.setattr(agent_predict, "run_claude_code", fake_run_claude_code)
        monkeypatch.setattr(agent_predict, "_compact_claude_session", fail_compaction)

        with pytest.raises(agent_predict.ClaudeCompactionError):
            agent_predict._run_reflection_claude_with_context_recovery(
                prompt="只补 knowledge_base 标签",
                work_dir="tools",
                news_files=[],
                session_id="session-old",
                model_name="deepseek-v4-pro",
            )

        assert len(calls) == 1


class TestPredictionPrompt:
    def test_prompt_requires_csv_for_all_data_requests(self):
        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
        }

        prompt = agent_predict._build_prediction_user_prompt(
            "stk",
            "股票",
            "601138 工业富联",
            dates_info,
            "--- 交易计划 ---",
        )

        assert "系统会自动要求 Claude 输出 CSV 文件到工作目录" in prompt
        assert "_query_meta.json" in prompt
        assert "超过20日" not in prompt


class TestReflectionBackfill:
    def test_reflection_backfills_all_unprocessed_trading_days(self, tmp_path, monkeypatch):
        model_root = tmp_path / agent_predict.PREDICT_PROFILE_NAME
        experience_dir = model_root / "experience"
        experience_dir.mkdir(parents=True)
        (experience_dir / "predict_ex_20260623_200000.md").write_text(
            "old knowledge\n\n<!-- reflected: 20260623 -->\n",
            encoding="utf-8",
        )

        def write_prediction(date_folder, ts_entry, target_label):
            out_dir = model_root / date_folder / "stk" / "000001" / ts_entry
            out_dir.mkdir(parents=True)
            out_dir.joinpath("000001.md").write_text(
                f"""# 000001 预测报告

- **类型**: stk
- **状态**: success
- **调用工具记录**: generated csv
- **可读取新闻路径**: 无

### AI 模型结论：
```text
<result>
【标的】000001
【{target_label} 方向】涨 【信心】8
【{target_label} 涨跌幅】1% ~ 2% 【信心】8
【{target_label}~2026-06-26 涨跌幅】1% ~ 3% 【信心】7
【{target_label}~2026-06-30 涨跌幅】1% ~ 5% 【信心】6
【1日逻辑理由】fixture
</result>
```
""",
                encoding="utf-8",
            )

        write_prediction("20260623", "200000", "2026-06-24")
        write_prediction("20260624", "200000", "2026-06-25")

        captured = {}
        new_knowledge = "# 更新后知识库\n\n" + "完整规则和错误归因。" * 20

        def fake_run_claude_code(**kwargs):
            captured["prompt"] = kwargs["prompt"]
            working_copy = next(
                Path(kwargs["work_dir"]).glob("predict_ex_*.md")
            )
            working_copy.write_text(new_knowledge, encoding="utf-8")
            analysis_path = Path(kwargs["work_dir"]) / "predict_analysis.json"
            payload = json.loads(analysis_path.read_text(encoding="utf-8"))
            for task_id, analysis in payload.items():
                if not isinstance(analysis, dict) or task_id in {
                    "knowledge_base_change",
                }:
                    continue
                for field_name in analysis:
                    analysis[field_name] = f"{task_id} {field_name} 完整复盘正文"
            analysis_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return "FILES_UPDATED", "", "session-1"

        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(agent_predict, "get_pro", lambda: FakeReflectionPro())
        monkeypatch.setattr(agent_predict, "datetime", FrozenDateTime)
        monkeypatch.setattr(agent_predict, "run_claude_code", fake_run_claude_code)

        knowledge, filename = agent_predict.run_reflection("20260625", "210000")

        assert knowledge == new_knowledge
        assert filename == "predict_ex_20260625_210000.md"
        assert "2026-06-24 方向" in captured["prompt"]
        assert "2026-06-25 方向" in captured["prompt"]
        marker = (experience_dir / filename).read_text(encoding="utf-8")
        assert "<!-- reflected: 20260624,20260625 -->" in marker


class TestAgentWorker:
    def test_prediction_prefers_claude_cli_without_initializing_api(
        self, tmp_path, monkeypatch
    ):
        class ShouldNotCallOpenAI:
            def __init__(self, **kwargs):
                raise AssertionError("Claude CLI 成功时不应初始化 DeepSeek API")

        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
            "suffix": "",
            "is_trading_day": False,
        }
        calls = []
        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(
            agent_predict,
            "MODEL_PREDICT_CLI_NAME",
            "cli-prediction-model",
        )
        monkeypatch.setattr(
            agent_predict,
            "MODEL_PREDICT_API_NAME",
            "api-fallback-model",
        )
        monkeypatch.setattr(
            agent_predict,
            "PREDICT_PROFILE_NAME",
            "test-profile",
        )
        monkeypatch.setattr(agent_predict, "OpenAI", ShouldNotCallOpenAI)
        monkeypatch.setattr(
            agent_predict,
            "_run_prediction_claude_cli",
            lambda messages, model_name: calls.append((messages, model_name))
            or "<result>CLI结构化预测</result>",
        )
        monkeypatch.setattr(
            agent_predict,
            "get_target_name",
            lambda code, asset_type: "沪深300",
        )

        result = agent_predict.agent_worker(
            "000300",
            "idx",
            "",
            "无",
            "20260623",
            "120000",
            dates_info=dates_info,
            use_preload=False,
        )

        assert result["status"] == "fail"
        assert result["prediction"].startswith("CLI结构化预测")
        assert len(calls) == 1
        assert calls[0][1] == "cli-prediction-model"

    def test_stock_news_scrape_failure_exits_before_model(self, tmp_path, monkeypatch):
        class ShouldNotCallOpenAI:
            def __init__(self, **kwargs):
                raise AssertionError("OpenAI should not be called when news scraping fails")

        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
            "suffix": "",
            "is_trading_day": False,
        }
        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(agent_predict, "OpenAI", ShouldNotCallOpenAI)
        monkeypatch.setattr(agent_predict, "run_scraper", lambda code: False)
        monkeypatch.setattr(
            agent_predict,
            "get_latest_news_content",
            lambda code: (_ for _ in ()).throw(AssertionError("should not read stale news")),
        )

        result = agent_predict.agent_worker(
            "601138",
            "stk",
            "",
            "无",
            "20260623",
            "120000",
            dates_info=dates_info,
        )

        assert result["status"] == "fail"
        assert "新闻抓取失败" in result["prediction"]

    def test_stock_can_skip_news_scraper_with_parameter(self, tmp_path, monkeypatch):
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                message = SimpleNamespace(
                    content="<result>结构化预测</result>",
                    reasoning_content="推理过程",
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
            "suffix": "",
            "is_trading_day": False,
        }
        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(agent_predict, "OpenAI", FakeOpenAI)
        monkeypatch.setattr(agent_predict, "get_target_name", lambda code, asset_type: "工业富联")
        monkeypatch.setattr(
            agent_predict,
            "run_scraper",
            lambda code: (_ for _ in ()).throw(AssertionError("scraper should be skipped")),
        )
        monkeypatch.setattr(
            agent_predict,
            "get_latest_news_content",
            lambda code: (_ for _ in ()).throw(AssertionError("news context should be skipped")),
        )

        result = agent_predict.agent_worker(
            "601138",
            "stk",
            "",
            "无",
            "20260623",
            "120000",
            dates_info=dates_info,
            use_news_scraper=False,
            use_preload=False,
        )

        assert result["status"] == "fail"
        assert captured["messages"][1]["content"]

    def test_direct_result_keeps_prompt_and_result_contract(self, tmp_path, monkeypatch):
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                message = SimpleNamespace(
                    content="<result>结构化预测</result>",
                    reasoning_content="推理过程",
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
            "suffix": "",
            "is_trading_day": False,
        }
        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(
            agent_predict,
            "MODEL_PREDICT_CLI_NAME",
            "cli-prediction-model",
        )
        monkeypatch.setattr(
            agent_predict,
            "MODEL_PREDICT_API_NAME",
            "api-fallback-model",
        )
        monkeypatch.setattr(agent_predict, "OpenAI", FakeOpenAI)
        monkeypatch.setattr(agent_predict, "get_target_name", lambda code, asset_type: "沪深300")

        result = agent_predict.agent_worker(
            "000300",
            "idx",
            "",
            "无",
            "20260623",
            "120000",
            dates_info=dates_info,
            use_preload=False,
        )

        assert result["stock"] == "000300"
        assert result["asset_type"] == "idx"
        assert result["status"] == "fail"
        assert "结构化预测" in result["prediction"]
        assert captured["model"] == "api-fallback-model"
        assert captured["temperature"] == 0.7
        user_message = captured["messages"][1]["content"]
        assert "指数 000300 沪深300" in user_message
        assert "1日目标日期: 2026-06-24" in user_message
        assert "--- 宏观/大盘应对参考 ---" in user_message

        log_path = (
            tmp_path
            / agent_predict.PREDICT_PROFILE_NAME
            / "20260623"
            / "idx"
            / "000300"
            / "120000"
            / "000300.log"
        )
        log_content = log_path.read_text(encoding="utf-8")
        assert "=== IDX 000300 分析进程启动 ===" in log_content
        assert "[DeepSeek 输出]:" in log_content
        assert "Claude CLI 预测调用失败，后续改用 DeepSeek API" in log_content
        assert "[判定失效]" in log_content

    def test_claude_instruction_falls_back_to_reasoning_when_output_is_empty(self, tmp_path, monkeypatch):
        call_count = 0
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    message = SimpleNamespace(
                        content="",
                        reasoning_content="分析过程\n<claude>获取豆粕期货数据</claude>",
                    )
                else:
                    message = SimpleNamespace(
                        content="<result>结构化预测</result>",
                        reasoning_content="",
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        def fake_run_claude_code(**kwargs):
            captured["prompt"] = kwargs["prompt"]
            return "豆粕数据", "工具日志", "session-1"

        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
            "suffix": "",
            "is_trading_day": False,
        }
        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(agent_predict, "OpenAI", FakeOpenAI)
        monkeypatch.setattr(agent_predict, "get_target_name", lambda code, asset_type: "豆粕ETF")
        monkeypatch.setattr(agent_predict, "run_claude_code", fake_run_claude_code)
        monkeypatch.setattr(
            agent_predict,
            "get_etf_daily_with_indicators",
            lambda **kwargs: pd.DataFrame(
                {"trade_date": ["20260623"], "close_qfq": [2.1]}
            ),
        )

        result = agent_predict.agent_worker(
            "159985", "etf", "", "无", "20260623", "120000", dates_info=dates_info
        )

        assert call_count == 2
        assert "获取豆粕期货数据" in captured["prompt"]
        assert "绝对不要申请权限、等待用户批准或询问用户" in captured["prompt"]
        assert 'uv run python "<脚本绝对路径>"' in captured["prompt"]
        assert "严禁使用 `python ...`" in captured["prompt"]
        assert result["prediction"] == "结构化预测"

    def test_empty_output_without_claude_in_reasoning_continues(self, tmp_path, monkeypatch):
        call_count = 0

        class FakeCompletions:
            def create(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    message = SimpleNamespace(content="", reasoning_content="只有思考，没有工具指令")
                else:
                    message = SimpleNamespace(content="<result>结构化预测</result>", reasoning_content="")
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        dates_info = {
            "label_1d": "2026-06-24",
            "label_3d": "2026-06-24~2026-06-26",
            "label_5d": "2026-06-24~2026-06-30",
            "ts_dates": {
                "prev_td": "2026-06-22",
                "data_td": "2026-06-23",
                "next_td": "2026-06-24",
            },
            "suffix": "",
            "is_trading_day": False,
        }
        monkeypatch.setattr(agent_predict, "PREDICT_DIR", str(tmp_path))
        monkeypatch.setattr(agent_predict, "OpenAI", FakeOpenAI)
        monkeypatch.setattr(agent_predict, "get_target_name", lambda code, asset_type: "豆粕ETF")
        monkeypatch.setattr(
            agent_predict,
            "get_etf_daily_with_indicators",
            lambda **kwargs: pd.DataFrame(
                {"trade_date": ["20260623"], "close_qfq": [2.1]}
            ),
        )
        monkeypatch.setattr(
            agent_predict,
            "run_claude_code",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("不应调用 Claude")),
        )

        result = agent_predict.agent_worker(
            "159985", "etf", "", "无", "20260623", "120001", dates_info=dates_info
        )

        assert call_count == 2
        assert result["prediction"] == "结构化预测"
