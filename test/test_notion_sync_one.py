from types import SimpleNamespace

import notion_sync_one


def test_split_markdown_for_two_requests_preserves_content():
    content = "前言\n\n## 第一部分\n内容一\n\n## 第二部分\n内容二"

    first, second = notion_sync_one._split_markdown_for_two_requests(content)

    assert first + second == content
    assert second.startswith("## ")


def test_split_markdown_uses_level_three_headings_near_midpoint():
    content = "报告头部\n" + "".join(
        f"### 预测时间: {index}\n" + ("内容" * 100) + "\n"
        for index in range(10)
    )

    first, second = notion_sync_one._split_markdown_for_two_requests(content)

    assert first + second == content
    assert second.startswith("### ")
    assert abs(len(first) - len(second)) < len(content) * 0.25


def test_patch_with_retry_stops_after_three_413_responses(monkeypatch):
    calls = []
    sleeps = []

    def fake_patch(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(status_code=413, text="Request body too large")

    monkeypatch.setattr(notion_sync_one.requests, "patch", fake_patch)
    monkeypatch.setattr(notion_sync_one.time, "sleep", sleeps.append)

    succeeded = notion_sync_one._patch_with_retry(
        "https://example.com",
        {"Authorization": "Bearer test"},
        {"type": "replace_content"},
        "更新正文内容",
    )

    assert succeeded is False
    assert len(calls) == 3
    assert sleeps == [3, 3]


def test_sync_sends_markdown_as_replace_then_append(tmp_path, monkeypatch):
    markdown_file = tmp_path / "report.md"
    markdown_file.write_text(
        "# 测试报告\n\n## 第一部分\n内容一\n\n## 第二部分\n内容二",
        encoding="utf-8",
    )
    calls = []

    def fake_patch(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(notion_sync_one, "NOTION_TOKEN", "test-token")
    monkeypatch.setattr(notion_sync_one.requests, "patch", fake_patch)

    result = notion_sync_one.sync_md_to_notion(markdown_file, "page-id")

    content_payloads = [payload for url, payload in calls if url.endswith("/markdown")]
    assert result is True
    assert len(content_payloads) == 2
    assert content_payloads[0]["type"] == "replace_content"
    assert content_payloads[1] == {
        "type": "insert_content",
        "insert_content": {
            "content": content_payloads[1]["insert_content"]["content"],
            "position": {"type": "end"},
        },
    }
    combined = (
        content_payloads[0]["replace_content"]["new_str"]
        + content_payloads[1]["insert_content"]["content"]
    )
    assert "## 第一部分" in combined
    assert "## 第二部分" in combined
