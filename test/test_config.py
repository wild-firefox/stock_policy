import sys
from types import SimpleNamespace

import config


def test_get_pro_passes_token_without_writing_shared_token_file(monkeypatch):
    calls = []
    expected_client = object()

    def fail_set_token(_token):
        raise AssertionError("get_pro 不应写入共享的 Tushare Token 文件")

    def fake_pro_api(token):
        calls.append(token)
        return expected_client

    fake_tushare = SimpleNamespace(
        set_token=fail_set_token,
        pro_api=fake_pro_api,
    )
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)

    client = config.get_pro()

    assert client is expected_client
    assert calls == [config.TUSHARE_TOKEN]
