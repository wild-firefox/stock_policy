"""Tests for stocknews_scraper.py — parser logic and utilities."""

import asyncio
from datetime import datetime

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from news_manager.stocknews_scraper import (
    _open_listing_page,
    GENERIC_EXTRACT_JS,
    article_file_needs_refresh,
    ensure_listing_identity,
    normalize_modern_listing_item,
    needs_dynamic_table_wait,
    parse_article_to_markdown,
    parse_listing_to_markdown,
    sanitize_filename,
    should_skip_article_detail,
)


class TestSanitizeFilename:
    def test_ascii_title(self):
        assert sanitize_filename("Hello World") == "Hello World"

    def test_stock_code(self):
        result = sanitize_filename("600519")
        assert ":" not in result


class TestArticleFetchPolicy:
    def test_announcement_type_skips_detail_navigation(self):
        item = {
            "type": "announcement",
            "url": "http://news.10jqka.com.cn/field/sn/20260622/58598955.shtml?ts=3&qs=3",
        }
        assert should_skip_article_detail(item) is True

    def test_announcement_url_skips_even_without_type(self):
        item = {
            "type": "news",
            "url": "http://news.10jqka.com.cn/field/sn/20260622/58598955.shtml",
        }
        assert should_skip_article_detail(item) is True

    def test_regular_news_still_fetches(self):
        item = {
            "type": "news",
            "url": "http://news.10jqka.com.cn/field/20260623/677629993.shtml",
        }
        assert should_skip_article_detail(item) is False

    def test_margin_financing_title_waits_for_dynamic_table(self):
        assert needs_dynamic_table_wait("贵州茅台：6月22日获融资买入6.16亿元") is True
        assert needs_dynamic_table_wait("贵州茅台发布年度报告") is False

    def test_generic_extractor_recurses_into_sections(self):
        assert "['ARTICLE','SECTION','BLOCKQUOTE','UL','OL']" in GENERIC_EXTRACT_JS
        assert "tag === 'P' || /^H[1-6]$/.test(tag) || tag === 'SECTION'" not in GENERIC_EXTRACT_JS

    def test_generic_extractor_recurses_into_paragraphs_that_wrap_tables(self):
        assert "tag === 'P' && child.querySelector('table')" in GENERIC_EXTRACT_JS

    def test_incomplete_financing_article_is_refetched(self, tmp_path):
        article = tmp_path / "融资文章.md"
        article.write_text("正文\n\n加载中...\n", encoding="utf-8")
        assert article_file_needs_refresh(article, "贵州茅台获融资买入6.16亿元") is True

        article.write_text("正文已有数据但没有表格", encoding="utf-8")
        assert article_file_needs_refresh(article, "贵州茅台获融资买入6.16亿元") is True

        article.write_text("| 交易日期 | 融资买入额 |\n|---|---|", encoding="utf-8")
        assert article_file_needs_refresh(article, "贵州茅台获融资买入6.16亿元") is False

    def test_regular_existing_article_is_not_refetched(self, tmp_path):
        article = tmp_path / "普通文章.md"
        article.write_text("普通新闻正文", encoding="utf-8")
        assert article_file_needs_refresh(article, "贵州茅台发布年度报告") is False


class TestParseListingToMarkdown:
    def test_modern_news_tab_item_keeps_legacy_listing_template(self):
        item = normalize_modern_listing_item(
            [
                "工业富联：6月24日获融资买入15.27亿元",
                "同花顺iNews",
                "3小时前",
            ],
            "https://news.10jqka.com.cn/20260625/c677694777.shtml",
            "news",
            today=datetime(2026, 6, 25),
        )
        data = ensure_listing_identity(
            {
                "stockName": "工业富联",
                "stockCode": "601138",
                "sections": [{"title": "热点新闻", "items": [item]}],
            },
            "601138",
            "工业富联(601138)个股资讯查询_个股行情_同花顺财经",
        )

        md = parse_listing_to_markdown(data)

        assert "# 工业富联（601138）新闻公告" in md
        assert "## 热点新闻" in md
        assert "`06/25` [工业富联：6月24日获融资买入15.27亿元]" in md
        assert "https://news.10jqka.com.cn/20260625/c677694777.shtml" in md

    def test_modern_announcement_and_report_dates_are_normalized(self):
        announcement = normalize_modern_listing_item(
            [
                "工业富联：富士康工业互联网股份有限公司董事会决议公告",
                "证券代码：601138 富士康工业互联网股份有限公司",
                "2026-06-19",
            ],
            "http://news.10jqka.com.cn/field/sn/20260619/58578285.shtml",
            "announcement",
            today=datetime(2026, 6, 25),
        )
        report = normalize_modern_listing_item(
            ["AI服务器销售占比提升", "金元证券", "2026-06-15"],
            "http://news.10jqka.com.cn/field/sr/20260617/58566465.shtml",
            "report",
            today=datetime(2026, 6, 25),
        )

        assert announcement == {
            "date": "06/19",
            "title": "工业富联：富士康工业互联网股份有限公司董事会决议公告",
            "url": "http://news.10jqka.com.cn/field/sn/20260619/58578285.shtml",
            "type": "announcement",
        }
        assert report == {
            "date": "06/15",
            "title": "AI服务器销售占比提升",
            "url": "http://news.10jqka.com.cn/field/sr/20260617/58566465.shtml",
            "type": "report",
        }

    def test_empty_stock_code_falls_back_to_requested_code_and_title(self):
        data = ensure_listing_identity(
            {"stockName": "", "stockCode": "", "sections": []},
            "601138",
            "工业富联(601138)个股资讯查询_个股行情_同花顺财经",
        )

        assert data["stockName"] == "工业富联"
        assert data["stockCode"] == "601138"

    def test_empty_data(self):
        data = {"stockName": "测试", "stockCode": "000001", "sections": []}
        md = parse_listing_to_markdown(data)
        assert "测试（000001）" in md
        assert "同花顺个股页" in md

    def test_single_section(self):
        data = {
            "stockName": "测试股",
            "stockCode": "000001",
            "sections": [
                {
                    "title": "热点新闻",
                    "items": [
                        {"date": "05/25", "title": "某重大利好公告", "url": "http://example.com/1", "type": "news"},
                        {"date": "05/24", "title": "行业政策解读", "url": "http://example.com/2", "type": "news"},
                    ]
                }
            ]
        }
        md = parse_listing_to_markdown(data)
        assert "## 热点新闻" in md
        assert "[某重大利好公告](http://example.com/1)" in md
        assert "[行业政策解读](http://example.com/2)" in md
        assert "`05/25`" in md

    def test_multiple_sections(self):
        data = {
            "stockName": "测试股",
            "stockCode": "000001",
            "sections": [
                {"title": "热点新闻", "items": [
                    {"date": "05/25", "title": "新闻1", "url": "http://x.com/1", "type": "news"},
                ]},
                {"title": "公司公告", "items": [
                    {"date": "05/22", "title": "公告1", "url": "http://x.com/2", "type": "announcement"},
                ]},
                {"title": "相关研报", "items": [
                    {"date": "05/20", "title": "研报1", "url": "http://x.com/3", "type": "report"},
                ]},
            ]
        }
        md = parse_listing_to_markdown(data)
        assert "## 热点新闻" in md
        assert "## 公司公告" in md
        assert "## 相关研报" in md

    def test_item_without_date(self):
        data = {
            "stockName": "测试", "stockCode": "000001",
            "sections": [{"title": "热点新闻", "items": [
                {"date": "", "title": "无日期新闻", "url": "http://x.com/1", "type": "news"},
            ]}]
        }
        md = parse_listing_to_markdown(data)
        assert "[无日期新闻](http://x.com/1)" in md
        assert "``" not in md  # empty date should not render code span


class TestParseArticleToMarkdown:
    def test_minimal_article(self):
        data = {"title": "测试文章", "time": "2026-05-25 10:30:00", "source": "", "paragraphs": [], "stocks": []}
        md = parse_article_to_markdown(data)
        assert "# 测试文章" in md
        assert "2026-05-25 10:30:00" in md

    def test_article_with_paragraphs(self):
        data = {
            "title": "测试文章",
            "time": "2026-05-25 10:30:00",
            "source": "上海证券报",
            "paragraphs": ["第一段内容", "第二段内容"],
            "stocks": []
        }
        md = parse_article_to_markdown(data)
        assert "上海证券报" in md
        assert "第一段内容" in md
        assert "第二段内容" in md

    def test_article_with_stocks(self):
        data = {
            "title": "测试", "time": "2026-05-25", "source": "",
            "paragraphs": [], "stocks": ["金徽酒 +10.03%", "贵州茅台 -0.32%"]
        }
        md = parse_article_to_markdown(data)
        assert "文章提及标的" in md
        assert "金徽酒 +10.03%" in md

    def test_article_elements_format(self):
        data = {
            "title": "测试", "time": "", "source": "", "stocks": [],
            "elements": [
                {"type": "p", "text": "第一段"},
                {"type": "table", "rows": [["A", "B"], ["1", "2"]]},
                {"type": "p", "text": "第二段"},
            ]
        }
        md = parse_article_to_markdown(data)
        assert "第一段" in md
        assert "第二段" in md
        assert "| A | B |" in md
        assert "| 1 | 2 |" in md

    def test_loading_placeholder_removed_and_table_kept(self):
        data = {
            "title": "融资买入",
            "time": "",
            "source": "",
            "stocks": [],
            "elements": [
                {"type": "p", "text": "加载中..."},
                {
                    "type": "table",
                    "rows": [
                        ["交易日期", "融资买入额", "融资余额"],
                        ["2026-06-22", "6.16亿", "199.19亿"],
                    ],
                },
            ],
        }
        md = parse_article_to_markdown(data)
        assert "加载中" not in md
        assert "| 交易日期 | 融资买入额 | 融资余额 |" in md
        assert "| 2026-06-22 | 6.16亿 | 199.19亿 |" in md

    def test_duplicate_dynamic_tables_render_once(self):
        rows = [
            ["交易日期", "融资买入额"],
            ["2026-06-22", "6.16亿"],
        ]
        data = {
            "title": "融资买入",
            "time": "",
            "source": "",
            "stocks": [],
            "elements": [
                {"type": "table", "rows": rows},
                {"type": "table", "rows": rows},
            ],
        }
        md = parse_article_to_markdown(data)
        assert md.count("| 交易日期 | 融资买入额 |") == 1

    def test_article_with_images(self):
        data = {
            "title": "测试", "time": "", "source": "", "stocks": [],
            "elements": [
                {"type": "p", "text": "文字"},
                {"type": "image", "src": "https://example.com/img.jpg"},
                {"type": "p", "text": "结尾"},
            ]
        }
        image_map = {"https://example.com/img.jpg": "images/01.jpg"}
        md = parse_article_to_markdown(data, image_map)
        assert "文字" in md
        assert "![](images/01.jpg)" in md
        assert "结尾" in md

    def test_article_image_no_map(self):
        data = {
            "title": "测试", "time": "", "source": "", "stocks": [],
            "elements": [
                {"type": "image", "src": "https://example.com/img.jpg"},
            ]
        }
        md = parse_article_to_markdown(data)
        # Images without image_map entry are skipped (external hotlinks)
        assert "![](" not in md

    def test_article_skip_nonlocal_image(self):
        data = {
            "title": "测试", "time": "", "source": "", "stocks": [],
            "elements": [
                {"type": "image", "src": "https://mmbiz.qpic.cn/photo.jpg"},
                {"type": "image", "src": "https://e.thsi.cn/img/abc123"},
            ]
        }
        image_map = {"https://e.thsi.cn/img/abc123": "images/local.jpg"}
        md = parse_article_to_markdown(data, image_map)
        # Only the downloaded image shows, external hotlink is skipped
        assert "![](images/local.jpg)" in md
        assert "mmbiz.qpic.cn" not in md


class FakeListingPage:
    def __init__(self, goto_error=None, ready_error=None):
        self.goto_error = goto_error
        self.ready_error = ready_error
        self.goto_calls = []
        self.ready_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error

    async def wait_for_function(self, expression, **kwargs):
        self.ready_calls.append((expression, kwargs))
        if self.ready_error:
            raise self.ready_error


class TestListingNavigation:
    def test_waits_for_dom_and_listing_content(self):
        page = FakeListingPage()

        asyncio.run(_open_listing_page(page, "https://example.com/news/"))

        assert page.goto_calls == [(
            "https://example.com/news/",
            {"wait_until": "domcontentloaded", "timeout": 30000},
        )]
        assert len(page.ready_calls) == 1
        assert "querySelectorAll('button')" in page.ready_calls[0][0]
        assert page.ready_calls[0][1] == {"timeout": 15000}

    def test_continues_when_navigation_timeout_has_loaded_dom(self):
        page = FakeListingPage(
            goto_error=PlaywrightTimeoutError("navigation timed out")
        )

        asyncio.run(_open_listing_page(page, "https://example.com/news/"))

        assert len(page.ready_calls) == 1
