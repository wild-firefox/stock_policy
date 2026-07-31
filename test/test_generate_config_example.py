from pathlib import Path

import pytest

from generate_config_example import (
    DEFAULT_OUTPUT,
    DEFAULT_SOURCE,
    PLACEHOLDERS,
    generate_config_example,
    sanitize_config,
)


def test_sanitize_config_only_replaces_private_defaults() -> None:
    source = (
        'TOKEN = os.environ.get("TUSHARE_TOKEN", "real-token")  # 保留注释\n'
        'PAGE = os.environ.get("NOTION_PAGE_ID", \'real-page\')\n'
        'TITLE = os.environ.get("THS_WINDOW_TITLE", ".*某券商.*")\n'
        'PROXY = os.environ.get("PROXY_URL", "http://127.0.0.1:7897")\n'
        'NORMAL_VALUE = "原样保留"\n'
    )

    result = sanitize_config(source)

    assert 'os.environ.get("TUSHARE_TOKEN", "API_KEY")  # 保留注释' in result
    assert "os.environ.get(\"NOTION_PAGE_ID\", 'PAGE_ID')" in result
    assert 'os.environ.get("THS_WINDOW_TITLE", ".*证券公司.*")' in result
    assert 'os.environ.get("PROXY_URL", "http://[IP_ADDRESS]")' in result
    assert 'NORMAL_VALUE = "原样保留"' in result
    assert "real-token" not in result
    assert "real-page" not in result


def test_sanitize_config_rejects_unhandled_sensitive_variable() -> None:
    source = 'NEW_KEY = os.environ.get("NEW_API_KEY", "real-secret")\n'

    with pytest.raises(ValueError, match="NEW_API_KEY"):
        sanitize_config(source)


def test_generate_config_example_supports_write_and_check(tmp_path: Path) -> None:
    source_path = tmp_path / "config.py"
    output_path = tmp_path / "config.example.py"
    source_path.write_text(
        '\n'.join(
            f'{name} = os.environ.get("{name}", "private")'
            for name in (
                "TUSHARE_TOKEN",
                "DEEPSEEK_API_KEY",
                "BOCHA_API_KEY",
                "TICKFLOW_API_KEY",
                "NOTION_TOKEN",
                "NOTION_PAGE_ID",
                "NOTION_PRIVATE_PAGE_ID",
                "AIHUBMIX_API_KEY",
                "THS_WINDOW_TITLE",
                "PROXY_URL",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert generate_config_example(source_path, output_path)
    assert generate_config_example(source_path, output_path, check_only=True)
    assert "private" not in output_path.read_text(encoding="utf-8")

    output_path.write_text("stale\n", encoding="utf-8")
    assert not generate_config_example(source_path, output_path, check_only=True)


def test_project_config_example_is_current_and_contains_only_placeholders() -> None:
    source = DEFAULT_SOURCE.read_text(encoding="utf-8-sig")
    example = DEFAULT_OUTPUT.read_text(encoding="utf-8-sig")

    assert example == sanitize_config(source)
    for placeholder in PLACEHOLDERS.values():
        assert placeholder in example
