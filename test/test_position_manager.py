import sys
from types import ModuleType
from types import SimpleNamespace


cv2_stub = ModuleType("cv2")
numpy_stub = ModuleType("numpy")
pil_stub = ModuleType("PIL")
pil_stub.ImageGrab = SimpleNamespace()
pywinauto_stub = ModuleType("pywinauto")
pywinauto_stub.Application = object
rapidocr_stub = ModuleType("rapidocr_onnxruntime")
rapidocr_stub.RapidOCR = lambda **kwargs: SimpleNamespace()

sys.modules.setdefault("cv2", cv2_stub)
sys.modules.setdefault("numpy", numpy_stub)
sys.modules.setdefault("PIL", pil_stub)
sys.modules.setdefault("pywinauto", pywinauto_stub)
sys.modules.setdefault("rapidocr_onnxruntime", rapidocr_stub)

import position_manager


SAMPLE_RESULT = {
    "account_summary": {"资金余额": "10213.81"},
    "positions": [{"证券代码": "601138"}],
}


def _sample_rows():
    top_rows = [[{"text": "资金余额"}, {"text": "10213.81"}]]
    table_rows = [[{"text": "证券代码"}, {"text": "601138"}]]
    return top_rows, table_rows


def test_run_claude_text_parser_sets_model_and_required_environment(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout='{"account_summary": {}, "positions": []}',
            stderr="",
        )

    monkeypatch.setattr(position_manager.subprocess, "run", fake_run)
    monkeypatch.setattr(position_manager.config, "TUSHARE_TOKEN", "test-token")

    result_text = position_manager._run_claude_text_parser(
        "解析持仓",
        model_name="deepseek-v4-flash",
    )

    assert result_text.startswith("{")
    assert "--model" in captured["cmd"]
    assert "deepseek-v4-flash" in captured["cmd"]
    assert captured["kwargs"]["env"]["TUSHARE_TOKEN"] == "test-token"
    assert (
        captured["kwargs"]["env"]["ANTHROPIC_MODEL"]
        == "deepseek-v4-flash"
    )
    assert captured["kwargs"]["input"] == "解析持仓"


def test_call_text_ai_uses_claude_first_without_calling_api(monkeypatch):
    top_rows, table_rows = _sample_rows()
    calls = []
    monkeypatch.setattr(
        position_manager,
        "_run_claude_text_parser",
        lambda prompt, model_name: '{"account_summary": {"资金余额": "10213.81"}, "positions": [{"证券代码": "601138"}]}',
    )
    monkeypatch.setattr(
        position_manager,
        "_run_deepseek_text_parser",
        lambda prompt, model_name: calls.append("api"),
    )

    result = position_manager.call_text_ai_to_parse(top_rows, table_rows)

    assert result == SAMPLE_RESULT
    assert calls == []


def test_position_models_are_configured_independently():
    assert position_manager.POSITION_TEXT_PARSE_API_MODEL == "deepseek-v4-flash"
    assert position_manager.POSITION_TEXT_PARSE_CLI_MODEL == "deepseek-v4-flash"
    assert (
        position_manager.POSITION_VISION_MODEL
        == "gemini-3.1-flash-image-preview-free"
    )


def test_call_text_ai_uses_separate_cli_and_api_models(monkeypatch):
    top_rows, table_rows = _sample_rows()
    captured = {}

    def fake_cli(prompt, model_name):
        captured["cli_model"] = model_name
        return "Claude 没有返回 JSON"

    monkeypatch.setattr(
        position_manager,
        "_run_claude_text_parser",
        fake_cli,
    )

    def fake_api(prompt, model_name):
        captured["api_model"] = model_name
        return '```json\n{"account_summary": {"资金余额": "10213.81"}, "positions": [{"证券代码": "601138"}]}\n```'

    monkeypatch.setattr(
        position_manager,
        "_run_deepseek_text_parser",
        fake_api,
    )

    result = position_manager.call_text_ai_to_parse(
        top_rows,
        table_rows,
        cli_model="custom-cli-model",
        api_model="custom-api-model",
    )

    assert result == SAMPLE_RESULT
    assert captured == {
        "cli_model": "custom-cli-model",
        "api_model": "custom-api-model",
    }
