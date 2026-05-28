"""Tests for stocknews_scraper.py — parser logic and utilities."""

from news_manager.stocknews_scraper import parse_listing_to_markdown, parse_article_to_markdown, sanitize_filename


class TestSanitizeFilename:
    def test_ascii_title(self):
        assert sanitize_filename("Hello World") == "Hello World"

    def test_stock_code(self):
        result = sanitize_filename("600519")
        assert ":" not in result


class TestParseListingToMarkdown:
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
