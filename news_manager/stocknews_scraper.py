"""Scraper for 10jqka stock news pages — low-token, all work in subprocess."""

import argparse
import asyncio
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import NEWS_RAW_DIR

OUTPUT_BASE = NEWS_RAW_DIR

ANNOUNCEMENT_URL_RE = re.compile(r"/(?:field/)?sn/", re.IGNORECASE)
DYNAMIC_TABLE_TITLE_RE = re.compile(r"融资|融券|两融")
LOADING_PLACEHOLDER_RE = re.compile(r"^加载中[.。…·]*$")
MODERN_TAB_CONFIG = (
    ("news", "热点新闻", "新闻"),
    ("announcement", "公司公告", "公告"),
    ("report", "相关研报", "研报"),
)

DYNAMIC_TABLE_READY_JS = r"""() => {
    const root = document.querySelector('.article-content, .news-content, article, #content');
    if (!root) return false;

    const hasVisibleDataTable = Array.from(root.querySelectorAll('table')).some(table => {
        const style = window.getComputedStyle(table);
        const visible = style.display !== 'none' && style.visibility !== 'hidden' &&
            table.getClientRects().length > 0;
        return visible && table.querySelectorAll('tr').length >= 2;
    });

    const hasLoadingPlaceholder = Array.from(root.querySelectorAll('*')).some(el =>
        /^加载中[.。…·]*$/.test((el.innerText || '').trim())
    );

    return hasVisibleDataTable && !hasLoadingPlaceholder;
}"""

LISTING_READY_JS = r"""() => {
    if (document.querySelector('a[href*="/field/"], a[href*="/sn/"], a[href*="/sr/"]')) {
        return true;
    }
    const buttonTexts = new Set(Array.from(document.querySelectorAll('button')).map(button =>
        (button.innerText || button.textContent || '').trim()
    ));
    return ['\u65b0\u95fb', '\u516c\u544a', '\u7814\u62a5'].every(label => buttonTexts.has(label));
}"""

EXTRACT_LISTING_JS = r"""(() => {
    const result = { stockName: '', stockCode: '', sections: [], totalItems: 0 };

    const h1 = document.querySelector('h1');
    const h1Text = h1 ? h1.innerText.trim() : '';
    const parts = h1Text.split(/\s+/);
    result.stockName = parts[0] || '';
    result.stockCode = parts[1] || '';

    // Find section h2s by vertical position
    const h2s = Array.from(document.querySelectorAll('h2'))
        .filter(h => {
            const t = h.innerText.trim();
            return t === '热点新闻' || t === '公司公告' || t === '相关研报';
        })
        .map(h => ({ title: h.innerText.trim(), y: h.getBoundingClientRect().y }));

    // Get all relevant links with vertical positions and parent text for date extraction
    const allLinks = Array.from(
        document.querySelectorAll('a[href*="/field/"], a[href*="/sn/"], a[href*="/sr/"]')
    ).map(a => {
        const href = a.href;
        const text = a.innerText.trim();
        const y = a.getBoundingClientRect().y;
        const parent = a.closest('li, dt, div') || a.parentElement;
        const nearbyText = parent ? parent.innerText.trim() : text;
        return { href, text, y, nearbyText };
    }).filter(l => l.text.length >= 5 && !l.text.includes('首页概览'));

    // Helper: extract date from text
    function findDate(text) {
        const m = text.match(/^(\d{2}\/\d{2})\s+/);
        if (m) return m[1];
        const m2 = text.match(/(\d{4}-\d{2}-\d{2})/);
        if (m2) {
            const parts = m2[1].split('-');
            return parts[1] + '/' + parts[2];
        }
        return '';
    }

    // Assign links to sections by position
    const seen = new Set();
    h2s.forEach((h2, i) => {
        const nextY = i + 1 < h2s.length ? h2s[i + 1].y : Infinity;
        const items = [];
        allLinks.forEach(link => {
            if (seen.has(link.href)) return;
            if (link.y >= h2.y && link.y < nextY) {
                seen.add(link.href);
                let date = findDate(link.text);
                if (!date) date = findDate(link.nearbyText);
                let title = link.text;
                const datePrefix = title.match(/^(\d{2}\/\d{2}|\d{4}-\d{2}-\d{2})\s*/);
                if (datePrefix) title = title.slice(datePrefix[0].length);
                let type = 'news';
                if (link.href.includes('/sn/')) type = 'announcement';
                else if (link.href.includes('/sr/')) type = 'report';
                items.push({ date, title, url: link.href, type });
            }
        });
        if (items.length > 0) {
            result.sections.push({ title: h2.title, items });
            result.totalItems += items.length;
        }
    });

    return result;
})()"""

MODERN_TAB_BUTTON_RECT_JS = r"""(label) => {
    const buttons = Array.from(document.querySelectorAll('button'));
    const button = buttons.find(b => (b.innerText || b.textContent || '').trim() === label);
    if (!button) return null;
    const rect = button.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    return {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        w: Math.round(rect.width),
        h: Math.round(rect.height),
        active: button.getAttribute('data-state') === 'active',
    };
}"""

MODERN_ACTIVE_TAB_LINKS_JS = r"""() => {
    const newline = String.fromCharCode(10);
    const links = Array.from(document.querySelectorAll('a'));
    return links.map(a => {
        const rect = a.getBoundingClientRect();
        const text = (a.innerText || '').trim();
        const lines = text.split(newline).map(s => s.trim()).filter(Boolean);
        return {
            href: a.href || '',
            lines,
            y: Math.round(rect.y),
            w: Math.round(rect.width),
            h: Math.round(rect.height),
        };
    }).filter(item =>
        item.href &&
        item.lines.length > 0 &&
        item.y >= 140 &&
        item.w > 0 &&
        item.h > 0 &&
        item.href.includes('10jqka.com.cn')
    );
}"""

EXTRACT_ARTICLE_JS = r"""(() => {
    const result = { title: '', time: '', source: '', paragraphs: [], stocks: [] };

    // Title
    result.title = document.title.split('_')[0].split('-')[0].trim();
    if (!result.title) result.title = document.title;

    // Time
    const timeMatch = document.body.innerText.match(/(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
    result.time = timeMatch ? timeMatch[1] : '';

    // Source
    const sourceLink = Array.from(document.querySelectorAll('a')).find(
        a => a.closest('*')?.innerText?.includes('来源')
    );
    result.source = sourceLink ? sourceLink.innerText.trim() : '';

    // Content paragraphs — exclude sidebar/complementary
    const contentPs = document.querySelectorAll('p');
    contentPs.forEach(p => {
        if (p.closest('complementary, aside, [class*="side"], [class*="right_wrap"]')) return;
        const text = p.innerText.trim();
        if (text && text.length > 5 &&
            !text.includes('免责声明') &&
            !text.includes('风险提示') &&
            !text.includes('浙江同花顺')) {
            result.paragraphs.push(text);
        }
    });

    // Mentioned stocks — find cards after "文章提及标的" label
    const allEls = Array.from(document.querySelectorAll('*'));
    const stockLabel = allEls.find(
        el => el.innerText && el.innerText.trim() === '文章提及标的'
    );
    if (stockLabel) {
        let container = stockLabel.parentElement;
        for (let i = 0; i < 3 && container; i++) {
            const cards = container.querySelectorAll('[class*="cursor-pointer"]');
            if (cards.length > 0) {
                cards.forEach(card => {
                    const text = card.innerText.trim();
                    if (text && text.length < 30) {
                        result.stocks.push(text.replace(/\n/g, ' '));
                    }
                });
                break;
            }
            container = container.parentElement;
        }
    }

    return result;
})()"""

# Generic fallback extraction — ordered tree walk with table + image support
GENERIC_EXTRACT_JS = r"""(() => {
    const result = { title: '', time: '', elements: [] };

    result.title = (document.title || '').replace(/[-_|]\s*.+$/, '').trim();
    if (!result.title || result.title.length < 5) result.title = document.title;

    // Look for better title if document.title is too short or generic
    if (result.title.length < 5 || /^(研报|公告|新闻|文章)$/.test(result.title)) {
        const h1 = document.querySelector('h1');
        if (h1 && h1.innerText.trim().length > 5) result.title = h1.innerText.trim();
        if (result.title.length < 5) {
            const heading = document.querySelector('[class*="title"], [class*="headline"], .YBcontentTit');
            if (heading && heading.innerText.trim().length > 5) result.title = heading.innerText.trim();
        }
    }

    // Find publication time: prefer near "发表时间/发布时间" labels, then YYYY-MM-DD HH:MM:SS
    const timeLabel = document.body.innerText.match(/(?:发表时间|发布时间|时间)[：:]\s*(\d{4}-\d{2}-\d{2})/);
    if (timeLabel) { result.time = timeLabel[1]; }
    if (!result.time) {
        const tm = document.body.innerText.match(/(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})/);
        if (tm) result.time = tm[1];
    }
    if (!result.time) {
        const m2 = document.body.innerText.match(/(\d{4}年\d{1,2}月\d{1,2}日)/);
        if (m2) result.time = m2[1];
    }

    const selectors = [
        '#js_content', '.rich_media_content', 'article',
        '.article-content', '.article', '.content', '#content',
        '.main-content', '[class*="article"]', '[class*="content"]',
    ];
    let root = null;
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText.trim().length > 80) { root = el; break; }
    }
    if (!root) root = document.body;

    function isDataImage(src) {
        if (!src || !src.startsWith('http')) return false;
        if (/avatar|icon|emoji|logo|qr[_-]?code|weixin|wxlogo/i.test(src)) return false;
        if (/\.(gif|svg)(\?|$)/i.test(src)) return false;
        return true;
    }

    const tableSeen = new Set();

    function walk(el, out) {
        for (const child of el.children) {
            const tag = child.tagName;
            const text = (child.innerText || '').trim();
            if (['STYLE','SCRIPT','NOSCRIPT'].includes(tag)) continue;
            const style = window.getComputedStyle(child);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const cls = (child.className || '') + ' ' + (child.id || '');
            if (/\b(nav|footer|sidebar|header|copyright|banner)\b/i.test(cls)) continue;

            if (tag === 'IMG') {
                const src = child.src || child.getAttribute('data-src') || child.getAttribute('data-original') || '';
                if (isDataImage(src)) out.push({ type: 'image', src });
            } else if (tag === 'TABLE') {
                const rows = [];
                child.querySelectorAll('tr').forEach(tr => {
                    const cells = [];
                    tr.querySelectorAll('th, td').forEach(td =>
                        cells.push(td.innerText.trim().replace(/\n/g, ' ')));
                    if (cells.length > 0) rows.push(cells);
                });
                const signature = JSON.stringify(rows);
                if (rows.length >= 2 && !tableSeen.has(signature)) {
                    tableSeen.add(signature);
                    out.push({ type: 'table', rows });
                }
            } else if (tag === 'P' && child.querySelector('table')) {
                // Some 10jqka dynamic widgets place DIV/TABLE trees inside P.
                // Preserve the table structure instead of flattening innerText.
                walk(child, out);
            } else if (tag === 'P' || /^H[1-6]$/.test(tag)) {
                // Extract images first — they can appear even without text
                child.querySelectorAll('img').forEach(img => {
                    const src = img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
                    if (isDataImage(src)) out.push({ type: 'image', src });
                });
                if (text.length > 0 && !/^加载中[.。…·]*$/.test(text)) {
                    if (/^(相关|推荐|热门|精彩)(阅读|文章|推荐|内容)/.test(text)) break;
                    out.push({ type: 'paragraph', text });
                }
            } else if (['DIV','SPAN'].includes(tag)) {
                // Leaf DIV/SPAN (no block children): treat as paragraph
                const blockTags = ['DIV','P','SECTION','UL','OL','TABLE','BLOCKQUOTE','H1','H2','H3','H4','H5','H6'];
                const hasBlockChild = Array.from(child.children).some(c => blockTags.includes(c.tagName));
                if (hasBlockChild) {
                    walk(child, out);
                } else if (text.length > 0 && !/^加载中[.。…·]*$/.test(text)) {
                    child.querySelectorAll('img').forEach(img => {
                        const src = img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
                        if (isDataImage(src)) out.push({ type: 'image', src });
                    });
                    if (/^(相关|推荐|热门|精彩)(阅读|文章|推荐|内容)/.test(text)) break;
                    out.push({ type: 'paragraph', text });
                }
            } else if (['ARTICLE','SECTION','BLOCKQUOTE','UL','OL'].includes(tag)) {
                walk(child, out);
            }
        }
    }

    const elements = [];
    walk(root, elements);

    // Dedup paragraphs, keep images and tables as-is
    const seen = new Set();
    elements.forEach(el => {
        if (el.type === 'table') {
            result.elements.push(el);
        } else if (el.type === 'image') {
            result.elements.push(el);
        } else if (el.type === 'paragraph') {
            const t = el.text;
            const key = t.slice(0, 40);
            if (seen.has(key)) return;
            seen.add(key);
            if (t.length > 5 && !t.includes('版权所有') && !t.includes('ICP') &&
                !t.includes('备案号') && !t.includes('若涉及侵权') &&
                !t.includes('不要付钱') && !/^(免费|扫码|关注|长按|点击上方)/.test(t)) {
                result.elements.push({ type: 'p', text: t });
            }
        }
    });

    // Fallback: if structured walk found nothing, split root innerText by lines
    // (handles pages like 研报 that use <br>-separated text without <p> tags)
    if (result.elements.length === 0) {
        const text = (root.innerText || '').trim();
        text.split(/\n+/).forEach(line => {
            const t = line.trim();
            if (t.length > 10 && !/^加载中[.。…·]*$/.test(t) &&
                !/版权所有|ICP|备案号|禁止发表|我有话说/.test(t)) {
                result.elements.push({ type: 'p', text: t });
            }
        });
    }

    return result;
})()"""


def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name)


def should_skip_article_detail(item):
    """Return True when a listing item should not open a detail page."""
    url = str(item.get('url', ''))
    return item.get('type') == 'announcement' or bool(ANNOUNCEMENT_URL_RE.search(url))


def needs_dynamic_table_wait(title):
    """Return True for financing/margin articles whose tables render asynchronously."""
    return bool(DYNAMIC_TABLE_TITLE_RE.search(str(title or '')))


def article_file_needs_refresh(file_path, title):
    """Return True when a cached financing article is incomplete or flattened."""
    if not needs_dynamic_table_wait(title):
        return False
    try:
        content = Path(file_path).read_text(encoding='utf-8')
    except OSError:
        return True
    return '加载中' in content or '|---|' not in content


def _is_loading_placeholder(text):
    return bool(LOADING_PLACEHOLDER_RE.fullmatch(str(text or '').strip()))


async def _wait_for_dynamic_table(page, title, timeout=30000):
    """Wait for visible financing table rows and removal of loading placeholders."""
    if not needs_dynamic_table_wait(title):
        return
    try:
        await page.wait_for_function(DYNAMIC_TABLE_READY_JS, timeout=timeout)
    except Exception:
        print(f"[scrape] Dynamic table wait timed out: {str(title)[:30]}")


def parse_mmdd_to_date(mmdd, today):
    """Parse MM/DD string into a datetime.date relative to today."""
    try:
        parts = mmdd.split('/')
        month, day = int(parts[0]), int(parts[1])
        result = datetime(today.year, month, day).date()
        if result > today.date():
            result = datetime(today.year - 1, month, day).date()
        return result
    except (ValueError, IndexError):
        return None


def business_days_cutoff(today, days):
    """Return the earliest date that covers `days` business days back from today (inclusive)."""
    current = today
    count = 0
    while count < days:
        if current.weekday() < 5:  # Mon-Fri
            count += 1
        current = current - timedelta(days=1)
    return current + timedelta(days=1)


def ensure_listing_identity(data, fallback_stock_code, page_title=""):
    """Ensure listing data has stock name/code even when the modern page has no h1."""
    result = dict(data or {})
    title_match = re.search(r"(.+?)\((\d{6})\)", page_title or "")

    if not result.get("stockCode"):
        result["stockCode"] = str(fallback_stock_code or "")
    if not result.get("stockCode") and title_match:
        result["stockCode"] = title_match.group(2)

    if not result.get("stockName") and title_match:
        result["stockName"] = title_match.group(1).strip()
    if not result.get("stockName"):
        result["stockName"] = ""

    result.setdefault("sections", [])
    result["totalItems"] = sum(len(s.get("items", [])) for s in result.get("sections", []))
    return result


def listing_has_items(data):
    """Return True when the listing contains at least one item."""
    return any(section.get("items") for section in (data or {}).get("sections", []))


async def _open_listing_page(page, url):
    """打开列表页并等待新闻内容出现，忽略页面持续存在的后台请求。"""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        print("[scrape] Listing navigation timed out; checking loaded DOM")

    try:
        await page.wait_for_function(LISTING_READY_JS, timeout=15000)
    except PlaywrightTimeoutError:
        print("[scrape] Listing content wait timed out; attempting extraction")


def _format_mmdd(month, day):
    return f"{int(month):02d}/{int(day):02d}"


def normalize_modern_listing_date(text, today=None):
    """Normalize modern 10jqka date text to MM/DD."""
    today = today or datetime.now()
    if not isinstance(today, datetime):
        today = datetime.combine(today, datetime.min.time())

    text = str(text or "").strip()
    if not text:
        return ""

    full = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if full:
        return _format_mmdd(full.group(2), full.group(3))

    short = re.search(r"(\d{1,2})-(\d{1,2})", text)
    if short:
        return _format_mmdd(short.group(1), short.group(2))

    slash = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if slash:
        return _format_mmdd(slash.group(1), slash.group(2))

    if "昨天" in text:
        target = today - timedelta(days=1)
        return target.strftime("%m/%d")

    if any(token in text for token in ("分钟前", "小时前", "刚刚")):
        return today.strftime("%m/%d")

    return ""


def _date_from_url(url):
    m = re.search(r"/(\d{4})(\d{2})(\d{2})/", str(url or ""))
    if not m:
        return ""
    return f"{m.group(2)}/{m.group(3)}"


def normalize_modern_listing_item(lines, href, item_type, today=None):
    """Convert modern tab link text lines to the legacy listing item shape."""
    clean_lines = [str(line).strip() for line in (lines or []) if str(line).strip()]
    title = clean_lines[0] if clean_lines else ""
    date = ""
    for line in reversed(clean_lines):
        date = normalize_modern_listing_date(line, today)
        if date:
            break
    if not date:
        date = _date_from_url(href)
    return {
        "date": date,
        "title": title,
        "url": href,
        "type": item_type,
    }


async def _click_modern_tab(page, label):
    rect = await page.evaluate(MODERN_TAB_BUTTON_RECT_JS, label)
    if not rect:
        return False
    if not rect.get("active"):
        await page.mouse.click(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
        await page.wait_for_timeout(1200)
    return True


def _modern_item_matches_tab(item_type, href):
    href = str(href or "")
    if item_type == "announcement":
        return "/sn/" in href
    if item_type == "report":
        return "/sr/" in href
    return "/sn/" not in href and "/sr/" not in href


async def extract_modern_listing(page, stock_code, today=None):
    """Extract the modern React tabbed 10jqka stock news page."""
    page_title = await page.title()
    data = ensure_listing_identity(
        {"stockName": "", "stockCode": stock_code, "sections": [], "totalItems": 0},
        stock_code,
        page_title,
    )
    today = today or datetime.now()

    for item_type, section_title, label in MODERN_TAB_CONFIG:
        clicked = await _click_modern_tab(page, label)
        if not clicked:
            continue

        raw_items = await page.evaluate(MODERN_ACTIVE_TAB_LINKS_JS)
        items = []
        seen = set()
        for raw in raw_items:
            href = raw.get("href", "")
            if href in seen or not _modern_item_matches_tab(item_type, href):
                continue
            item = normalize_modern_listing_item(raw.get("lines", []), href, item_type, today)
            if not item["title"]:
                continue
            seen.add(href)
            items.append(item)

        if items:
            data["sections"].append({"title": section_title, "items": items})

    data["totalItems"] = sum(len(s.get("items", [])) for s in data["sections"])
    return data


def parse_listing_to_markdown(data, local_articles=None):
    """Parse listing page data into structured markdown.

    local_articles: set of filenames (without .md) available in articles/ dir.
        e.g. {"05-27_回购公告", "05-26_融资买入"}
        When an item matches, a [本地](path) link is appended inline.
    """
    stock_name = data.get('stockName', '')
    stock_code = data.get('stockCode', '')
    sections = data.get('sections', [])

    lines = []
    lines.append(f"# {stock_name}（{stock_code}）新闻公告")
    lines.append("")
    lines.append(f"> 来源：同花顺个股页")
    lines.append("")

    for section in sections:
        title = section.get('title', '')
        items = section.get('items', [])

        lines.append("---")
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")

        for item in items:
            date_str = f"`{item['date']}` " if item['date'] else ""
            line = f"- {date_str}[{item['title']}]({item['url']})"
            if local_articles and item['date']:
                item_date = item['date'].replace('/', '-')
                expected = f"{item_date}_{sanitize_filename(item['title'])}"
                if expected in local_articles:
                    line += f" 📎[本地](../articles/{expected}.md)"
            lines.append(line)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 数据来源：同花顺财经，内容仅供参考。")
    lines.append("")

    return "\n".join(lines)


def parse_article_to_markdown(data, image_map=None):
    """Parse article detail data into markdown.

    data['elements'] is the preferred source — a unified array of
    {type: 'p', text} | {type: 'image', src} | {type: 'table', rows}.
    Falls back to legacy paragraphs/tables fields for backward compat.
    """
    title = data.get('title', 'untitled')
    time_text = data.get('time', '')
    source = data.get('source', '')
    stocks = data.get('stocks', [])

    # Build unified elements array
    elements = data.get('elements', [])
    if not elements:
        for p in data.get('paragraphs', []):
            elements.append({'type': 'p', 'text': p})
        for rows in data.get('tables', []):
            elements.append({'type': 'table', 'rows': rows})

    lines = []
    lines.append(f"# {title}")
    lines.append("")
    meta = [f"**时间**：{time_text}"]
    if source:
        meta.append(f"**来源**：{source}")
    lines.append("  \n".join(meta))
    lines.append("")

    if stocks:
        lines.append("### 文章提及标的")
        lines.append("")
        for s in stocks:
            lines.append(f"- {s}")
        lines.append("")

    lines.append("### 正文")
    lines.append("")

    rendered_tables = set()
    for el in elements:
        if el['type'] == 'p':
            if _is_loading_placeholder(el.get('text', '')):
                continue
            lines.append(el['text'])
            lines.append("")
        elif el['type'] == 'image':
            src = el['src']
            if image_map and src in image_map:
                lines.append(f"![]({image_map[src]})")
                lines.append("")
        elif el['type'] == 'table':
            rows = el.get('rows', [])
            signature = tuple(tuple(str(cell) for cell in row) for row in rows)
            if not rows or signature in rendered_tables:
                continue
            rendered_tables.add(signature)
            lines.extend(_render_table(rows))
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 内容整理自同花顺财经，仅供参考。")
    lines.append("")

    return "\n".join(lines)


def _render_table(rows):
    """Render a 2D array of rows as a markdown table."""
    if not rows:
        return []
    max_cols = max(len(r) for r in rows)
    result = []
    # Header row
    header = rows[0]
    padded = list(header) + [''] * (max_cols - len(header))
    result.append('| ' + ' | '.join(padded) + ' |')
    result.append('|' + '|'.join(['---'] * max_cols) + '|')
    # Data rows
    for row in rows[1:]:
        padded = list(row) + [''] * (max_cols - len(row))
        result.append('| ' + ' | '.join(padded) + ' |')
    return result


async def scrape_listing(stock_code, output_dir=None, fetch_articles=True, fetch_days=4):
    """Scrape news listing for a stock code, optionally fetching recent articles."""
    from playwright.async_api import async_playwright

    url = f"https://stockpage.10jqka.com.cn/{stock_code}/news/"
    today = datetime.now()
    today_ymd = today.strftime("%Y%m%d")
    ts = today.strftime("%H%M%S")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await _open_listing_page(page, url)

        data = await page.evaluate(EXTRACT_LISTING_JS)
        data = ensure_listing_identity(data, stock_code, await page.title())
        if not listing_has_items(data):
            data = await extract_modern_listing(page, stock_code, today)

        if not data or data.get("error"):
            print(f"[scrape] Error: {data.get('error', 'unknown')}")
            await browser.close()
            return None

        if not listing_has_items(data):
            print(f"[scrape] Error: no listing items found for {stock_code}")
            await browser.close()
            return None

        print(f"[scrape] 股票: {data.get('stockName')} ({data.get('stockCode')})")
        for s in data.get('sections', []):
            print(f"[scrape] {s['title']}: {len(s['items'])} 条")

        # --- Auto-fetch recent articles ---
        articles_dir = OUTPUT_BASE / stock_code / "articles"
        cutoff = business_days_cutoff(today, fetch_days)

        if fetch_articles:
            articles_dir.mkdir(parents=True, exist_ok=True)
            fetched = 0
            skipped = 0
            skipped_announcements = 0

            for section in data.get('sections', []):
                for item in section.get('items', []):
                    if not item.get('date'):
                        continue

                    item_date = parse_mmdd_to_date(item['date'], today)
                    if item_date is None or item_date < cutoff.date():
                        continue

                    # Company announcement links redirect to PDF. Skip before
                    # page.goto() so redirect timeouts never block the loop.
                    if should_skip_article_detail(item):
                        skipped_announcements += 1
                        print(f"[scrape] Skip (announcement): {item['title'][:30]}")
                        continue

                    date_prefix = item['date'].replace('/', '-')
                    filename = f"{date_prefix}_{sanitize_filename(item['title'])}.md"
                    filepath = articles_dir / filename

                    if filepath.exists():
                        if article_file_needs_refresh(filepath, item.get('title', '')):
                            print(f"[scrape] Refresh (incomplete): {item['title'][:30]}")
                        else:
                            skipped += 1
                            continue

                    try:
                        # Use domcontentloaded + short timeout — many field/ links
                        # redirect through ad/tracking hops and never reach networkidle
                        await page.goto(item['url'], wait_until="domcontentloaded", timeout=10000)
                        # Give redirects a brief moment to settle
                        await asyncio.sleep(1)
                        final_url = page.url

                        # Only skip PDF files
                        if final_url.lower().endswith('.pdf'):
                            print(f"[scrape] Skip (PDF): {item['title'][:30]}")
                            continue

                        # Financing/margin articles render their data tables
                        # asynchronously. Wait for actual visible rows instead
                        # of saving the temporary loading placeholder.
                        if needs_dynamic_table_wait(item.get('title', '')):
                            await _wait_for_dynamic_table(page, item.get('title', ''))
                        # Other 10jqka pages keep the existing shorter wait.
                        elif '10jqka.com.cn' in final_url:
                            try:
                                await page.wait_for_load_state('networkidle', timeout=15000)
                            except Exception:
                                await asyncio.sleep(3)

                        # Run both extractors: 10jqka-specific for stocks/source,
                        # generic for elements (tables + images + dedup + noise)
                        article_data = await page.evaluate(EXTRACT_ARTICLE_JS)
                        generic_data = await page.evaluate(GENERIC_EXTRACT_JS)

                        if article_data and generic_data:
                            article_data['elements'] = generic_data.get('elements', [])
                            if not article_data.get('time') and generic_data.get('time'):
                                article_data['time'] = generic_data['time']
                        elif generic_data and generic_data.get('elements'):
                            article_data = generic_data

                        if article_data and article_data.get('elements'):
                            # Download images for non-redirecting (10jqka) articles
                            image_map = {}
                            if '10jqka.com.cn' in final_url:
                                images_dir = articles_dir / "images"
                                images_dir.mkdir(parents=True, exist_ok=True)
                                img_idx = 0
                                for el in article_data['elements']:
                                    if el['type'] == 'image':
                                        src = el['src']
                                        if src in image_map:
                                            continue
                                        try:
                                            resp = await page.request.get(src, timeout=10000)
                                            if resp.ok:
                                                ct = resp.headers.get('content-type', '')
                                                ext = 'jpg'
                                                if 'png' in ct:
                                                    ext = 'png'
                                                elif 'webp' in ct:
                                                    ext = 'webp'
                                                elif 'gif' in ct:
                                                    ext = 'gif'
                                                img_name = f"{hashlib.md5(src.encode()).hexdigest()[:12]}.{ext}"
                                                img_path = images_dir / img_name
                                                img_path.write_bytes(await resp.body())
                                                image_map[src] = f"images/{img_name}"
                                                img_idx += 1
                                            else:
                                                print(f"[scrape] Image HTTP {resp.status}: {src[:60]}")
                                        except Exception as e:
                                            print(f"[scrape] Image fail: {src[:60]} — {e}")
                                if img_idx > 0:
                                    print(f"[scrape] Downloaded {img_idx} images")

                            article_md = parse_article_to_markdown(article_data, image_map)
                            filepath.write_text(article_md, encoding="utf-8")
                            fetched += 1
                            print(f"[scrape] Article: {filename}")
                        else:
                            print(f"[scrape] Skip (empty): {item['title'][:30]}")
                    except Exception as e:
                        print(f"[scrape] Skip (error): {item['title'][:30]} — {e}")

            print(
                f"[scrape] Articles: {fetched} fetched, {skipped} skipped (exists), "
                f"{skipped_announcements} skipped (announcement)"
            )

        # --- Build local articles index (after fetch, so new articles are included) ---
        local_set = set()
        if articles_dir.exists():
            for lf in articles_dir.glob("*.md"):
                m = re.match(r'(\d{2})-(\d{2})_', lf.stem)
                if m:
                    try:
                        fd = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
                    except ValueError:
                        continue
                    if fd >= cutoff.date():
                        local_set.add(lf.stem)

        # --- Save listing markdown ---
        if not output_dir:
            output_dir = OUTPUT_BASE / stock_code / today_ymd

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        md_content = parse_listing_to_markdown(data, local_set)
        md_path = output_dir / f"{data.get('stockCode') or stock_code}_新闻公告_{ts}.md"
        md_path.write_text(md_content, encoding="utf-8")
        print(f"[scrape] Listing saved to {md_path}")

        await browser.close()
        return str(md_path)


async def scrape_article(url, stock_code=None, output_dir=None):
    """Scrape a single news article."""
    from playwright.async_api import async_playwright

    if should_skip_article_detail({'url': url}):
        print("[scrape] Skip (announcement)")
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
        await asyncio.sleep(1)
        final_url = page.url

        if final_url.lower().endswith('.pdf'):
            print(f"[scrape] Skip (PDF)")
            await browser.close()
            return None

        page_title = await page.title()
        if needs_dynamic_table_wait(page_title):
            await _wait_for_dynamic_table(page, page_title)
        # Non-redirecting 10jqka pages need more time for React/image rendering
        elif '10jqka.com.cn' in final_url:
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                await asyncio.sleep(3)

        data = await page.evaluate(EXTRACT_ARTICLE_JS)
        generic_data = await page.evaluate(GENERIC_EXTRACT_JS)

        if data and generic_data:
            data['elements'] = generic_data.get('elements', [])
            if not data.get('time') and generic_data.get('time'):
                data['time'] = generic_data['time']
        elif generic_data and generic_data.get('elements'):
            data = generic_data

        if not data or not data.get('elements'):
            print("[scrape] No data returned")
            await browser.close()
            return None

        print(f"[scrape] Title: {data.get('title')}")
        print(f"[scrape] Elements: {len(data.get('elements', []))}")
        img_count = sum(1 for el in data.get('elements', []) if el.get('type') == 'image')
        print(f"[scrape] Images in elements: {img_count}")

        # Download images for non-redirecting (10jqka) articles
        image_map = {}
        if '10jqka.com.cn' in final_url:
            if not output_dir:
                if stock_code:
                    articles_dir = OUTPUT_BASE / stock_code / "articles"
                else:
                    articles_dir = OUTPUT_BASE / "articles"
            else:
                articles_dir = Path(output_dir)
            images_dir = articles_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)

            img_idx = 0
            for el in data.get('elements', []):
                if el.get('type') == 'image':
                    src = el['src']
                    if src in image_map:
                        continue
                    try:
                        resp = await page.request.get(src, timeout=10000)
                        if resp.ok:
                            ct = resp.headers.get('content-type', '')
                            ext = 'jpg'
                            if 'png' in ct:
                                ext = 'png'
                            elif 'webp' in ct:
                                ext = 'webp'
                            elif 'gif' in ct:
                                ext = 'gif'
                            img_name = f"{hashlib.md5(src.encode()).hexdigest()[:12]}.{ext}"
                            img_path = images_dir / img_name
                            img_path.write_bytes(await resp.body())
                            image_map[src] = f"images/{img_name}"
                            img_idx += 1
                        else:
                            print(f"[scrape] Image HTTP {resp.status}: {src[:60]}")
                    except Exception as e:
                        print(f"[scrape] Image fail: {src[:60]} — {e}")
            if img_idx > 0:
                print(f"[scrape] Downloaded {img_idx} images")

        md_content = parse_article_to_markdown(data, image_map)
        await browser.close()

        if not output_dir:
            if stock_code:
                output_dir = OUTPUT_BASE / stock_code / "articles"
            else:
                output_dir = OUTPUT_BASE / "articles"

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        md_path = output_dir / f"{sanitize_filename(data.get('title', 'article'))}.md"
        md_path.write_text(md_content, encoding="utf-8")
        print(f"[scrape] Markdown saved to {md_path}")

        return str(md_path)


def main():
    parser = argparse.ArgumentParser(description="Scrape 10jqka stock news")
    sub = parser.add_subparsers(dest="mode", required=True)

    list_parser = sub.add_parser("list", help="Scrape news listing for a stock")
    list_parser.add_argument("--code", required=True, help="Stock code (e.g. 600519)")
    list_parser.add_argument("--output-dir", default=None)
    list_parser.add_argument("--fetch-articles", action="store_true", default=True,
                             help="Fetch recent article detail pages (default)")
    list_parser.add_argument("--no-fetch", action="store_false", dest="fetch_articles",
                             help="Skip article fetching")
    list_parser.add_argument("--fetch-days", type=int, default=4,
                             help="Trading days to look back for article fetching (default: 4)")

    article_parser = sub.add_parser("article", help="Scrape a single news article")
    article_parser.add_argument("--url", required=True, help="Article URL")
    article_parser.add_argument("--code", default=None, help="Stock code for output grouping")
    article_parser.add_argument("--output-dir", default=None)

    args = parser.parse_args()

    if args.mode == "list":
        md_path = None
        for attempt in range(1, 4):
            try:
                md_path = asyncio.run(scrape_listing(
                    args.code, args.output_dir, args.fetch_articles, args.fetch_days
                ))
            except Exception as exc:
                print(f"[scrape] Attempt {attempt}/3 failed: {exc}")

            if md_path:
                break
            if attempt < 3:
                print(f"[scrape] Retrying listing ({attempt + 1}/3)...")
                time.sleep(2)

        if not md_path:
            print("[scrape] Failed after 3 attempts")
            raise SystemExit(1)
    else:
        md_path = asyncio.run(scrape_article(args.url, args.code, args.output_dir))

    if md_path:
        print(f"[scrape] Done -> {md_path}")


if __name__ == "__main__":
    main()
