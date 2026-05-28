"""Tests for jiuyangongshe_scraper.py — parser logic and utilities."""

from news_manager.jiuyangongshe_scraper import parse_to_markdown, sanitize_filename


class TestSanitizeFilename:
    def test_ascii_title(self):
        assert sanitize_filename("Hello World") == "Hello World"

    def test_chinese_title(self):
        result = sanitize_filename("5月22日盘前纪要")
        assert "5月22日盘前纪要" in result
        assert "/" not in result
        assert ":" not in result

    def test_special_chars(self):
        result = sanitize_filename("test:file<name>here")
        assert ":" not in result
        assert "<" not in result
        assert ">" not in result


class TestParseToMarkdown:
    def test_section_header(self):
        paragraphs = [
            {"type": "paragraph", "text": "No.1 盘前热点事件"},
            {"type": "paragraph", "text": "PCB：鹏鼎控股、宝鼎科技"},
        ]
        md = parse_to_markdown("测试标题", "2026-05-22 07:08", paragraphs)
        assert "## No.1 盘前热点事件" in md
        assert "- **PCB**" in md

    def test_subsection_header(self):
        paragraphs = [
            {"type": "paragraph", "text": "一、昨日热点"},
            {"type": "paragraph", "text": "PCB：鹏鼎控股"},
        ]
        md = parse_to_markdown("测试", "2026-05-22", paragraphs)
        assert "### 一、昨日热点" in md
        assert "- **PCB**" in md

    def test_numbered_items(self):
        paragraphs = [
            {"type": "paragraph", "text": "七、行业要闻"},
            {"type": "paragraph", "text": "1、第一项内容"},
            {"type": "paragraph", "text": "2、第二项内容"},
            {"type": "paragraph", "text": "3、第三项内容"},
        ]
        md = parse_to_markdown("测试", "2026-05-22", paragraphs)
        assert "1. 第一项内容" in md
        assert "2. 第二项内容" in md
        assert "3. 第三项内容" in md

    def test_numbering_resets_on_new_section(self):
        paragraphs = [
            {"type": "paragraph", "text": "七、行业要闻"},
            {"type": "paragraph", "text": "1、第一条"},
            {"type": "paragraph", "text": "No.2 公告精选"},
            {"type": "paragraph", "text": "一、日常公告"},
            {"type": "paragraph", "text": "1、又一个第一条"},
        ]
        md = parse_to_markdown("测试", "2026-05-22", paragraphs)
        # Count "1." occurrences should be exactly 2 (first items of each list)
        lines = md.split("\n")
        ones = [l for l in lines if l.strip().startswith("1. ")]
        assert len(ones) == 2

    def test_event_text(self):
        paragraphs = [
            {"type": "paragraph", "text": "事件：某公司公告重大事项。"},
        ]
        md = parse_to_markdown("测试", "2026-05-22", paragraphs)
        assert "> 事件" in md

    def test_image_insertion(self):
        paragraphs = [
            {"type": "paragraph", "text": "一段文字"},
            {"type": "image", "src": "https://cdn.example.com/img.png"},
            {"type": "paragraph", "text": "另一段文字"},
        ]
        md = parse_to_markdown("测试", "2026-05-22", paragraphs)
        assert "![image](./images/img_01.png)" in md

    def test_complex_stock_pattern_priority(self):
        paragraphs = [
            {"type": "paragraph", "text": "No.4 涨停事件"},
            {"type": "paragraph", "text": "1、公告涨停（7）"},
            {"type": "paragraph", "text": "3、自动驾驶（4）"},
        ]
        md = parse_to_markdown("测试", "2026-05-22", paragraphs)
        lines = md.split("\n")
        numbered = [l for l in lines if l.strip().startswith(("1. ", "2. "))]
        assert len(numbered) == 2
        assert "1. 公告涨停（7）" in md
        assert "2. 自动驾驶（4）" in md
