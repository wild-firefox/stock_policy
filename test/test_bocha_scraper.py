"""Unit tests for bocha_scraper.py — pure logic functions only."""

import pytest
from news_manager.bocha_scraper import is_noise_url, is_relevant, parse_bocha_date


class TestIsNoiseUrl:
    def test_sohu_stock_quote(self):
        assert is_noise_url("https://q.stock.sohu.com/cn/600410/index.shtml?date=2026-05-27") is True

    def test_sohu_stock_noise_pattern(self):
        assert is_noise_url("https://stock.sohu.com/cn/600410/index") is True

    def test_legitimate_news_url(self):
        assert is_noise_url("https://stock.cfi.cn/p20260527001475.html") is False

    def test_legitimate_epaper_url(self):
        assert is_noise_url("https://epaper.stcn.com/con/202605/27/content_2931425.html") is False

    def test_sina_realtime_quote(self):
        assert is_noise_url("https://finance.sina.com.cn/realstock/company/sh600410/nc.shtml") is True

    def test_eastmoney_quote(self):
        assert is_noise_url("https://quote.eastmoney.com/sh600410.html") is True


class TestIsRelevant:
    def test_title_contains_code(self):
        assert is_relevant(
            "华胜天成(600410)最新公告", "some content", "600410", "华胜天成"
        ) is True

    def test_title_contains_name(self):
        assert is_relevant(
            "华胜天成发布重大资产重组公告", "some content", "600410", "华胜天成"
        ) is True

    def test_content_contains_code(self):
        assert is_relevant(
            "A股异动公告", "华胜天成(600410)股价异常波动", "600410", "华胜天成"
        ) is True

    def test_irrelevant_stock(self):
        assert is_relevant(
            "华升股份(600156)交易异常波动公告", "华升股份...", "600410", "华胜天成"
        ) is False

    def test_no_match(self):
        assert is_relevant(
            "市场综述：A股三大指数收跌", "今日A股市场...", "600410", "华胜天成"
        ) is False

    def test_short_code_match(self):
        assert is_relevant(
            "600410 股票公告", "content", "600410", ""
        ) is True


class TestParseBochaDate:
    def test_standard_utc_date(self):
        exact, short = parse_bocha_date("2026-05-27T08:00:00Z")
        assert "2026-05-27" in exact
        assert "05/27" == short

    def test_empty_date(self):
        exact, short = parse_bocha_date("")
        assert exact == "unknown"
        assert short == "00/00"

    def test_none_date(self):
        exact, short = parse_bocha_date(None)
        assert exact == "unknown"
        assert short == "00/00"
