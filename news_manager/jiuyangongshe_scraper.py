"""Scraper for jiuyangongshe.com articles — low-token, all work in subprocess."""

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import NEWS_JYGS_DIR

OUTPUT_BASE = NEWS_JYGS_DIR
AUTHOR_PAGE = "https://www.jiuyangongshe.com/u/4df747be1bf143a998171ef03559b517"

EXTRACT_JS = r"""(() => {
    const container = document.querySelector('.detail-container');
    if (!container) return { error: 'no .detail-container found' };

    // Title: find text node that matches "X月X日盘前纪要" pattern
    let title = '';
    const titleEl = container.querySelector('[class*="topic-title"]') ||
                    Array.from(container.querySelectorAll('*')).find(
                        el => /^\d+月\d+日/.test(el.innerText?.trim() || '')
                    );
    if (titleEl) title = titleEl.innerText.trim().split('\n')[0];
    if (!title) title = document.title.replace('-韭研公社', '').trim();

    // Time: find element containing date pattern
    let timeText = '';
    const timeMatch = container.innerText.match(/(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
    if (timeMatch) timeText = timeMatch[1];

    // Content: find the section between "每天10分钟阅读" and "本文内容基于互联网信息整理"
    const allText = container.innerText;
    const startMarker = '每天10分钟阅读';
    const endMarker = '本文内容基于互联网信息整理';
    const startIdx = allText.indexOf(startMarker);
    const endIdx = allText.indexOf(endMarker);
    const contentText = (startIdx >= 0 && endIdx >= 0)
        ? allText.substring(startIdx, endIdx + endMarker.length)
        : allText;

    // Extract paragraphs from content area using DOM
    const paragraphs = [];
    function findContentRoot(el) {
        // Find the deepest element containing the start marker
        if (el.innerText && el.innerText.includes(startMarker) && el.innerText.includes('No.1')) {
            for (const child of el.children) {
                if (child.innerText && child.innerText.includes(startMarker) &&
                    child.innerText.includes('No.1')) {
                    return findContentRoot(child);
                }
            }
            return el;
        }
        for (const child of el.children) {
            if (child.innerText && child.innerText.includes(startMarker)) {
                return findContentRoot(child);
            }
        }
        return el;
    }
    const contentRoot = findContentRoot(container);

    function walk(node) {
        for (const child of node.childNodes) {
            if (child.nodeType !== 1) continue;
            const tag = child.tagName;
            const text = (child.innerText || '').trim();
            const merchantImgs = child.querySelectorAll
                ? Array.from(child.querySelectorAll('img[src*="/merchant/"]'))
                : [];

            if (merchantImgs.length > 0 && text.length < 10) {
                merchantImgs.forEach(i =>
                    paragraphs.push({ type: 'image', src: i.src })
                );
            } else if (child.tagName === 'IMG' && (child.src || '').includes('/merchant/')) {
                paragraphs.push({ type: 'image', src: child.src });
            } else if (tag === 'P' && text) {
                paragraphs.push({ type: 'paragraph', text });
            } else if (tag === 'DIV' || tag === 'SECTION' || tag === 'SPAN') {
                walk(child);
            }
        }
    }
    walk(contentRoot);

    if (paragraphs.length === 0) {
        // Fallback 1: extract from Nuxt state (content loaded via SSR but Vue not hydrated)
        try {
            const nuxtContent = (function findNuxtContent(obj, depth) {
                if (depth > 15 || !obj || typeof obj !== 'object') return null;
                for (const key of Object.keys(obj)) {
                    const val = obj[key];
                    if (typeof val === 'string' && val.includes('No.1') && val.length > 500) return val;
                    if (typeof val === 'object' && val !== null) {
                        const r = findNuxtContent(val, depth + 1);
                        if (r) return r;
                    }
                }
                return null;
            })(window.__NUXT__ || {}, 0);

            if (nuxtContent) {
                const tmp = document.createElement('div');
                tmp.innerHTML = nuxtContent;
                // Extract paragraphs
                tmp.querySelectorAll('p').forEach(p => {
                    const text = p.innerText.trim();
                    if (text && text.length > 3) paragraphs.push({ type: 'paragraph', text });
                });
                // Extract images
                tmp.querySelectorAll('img').forEach(img => {
                    if (img.src && img.src.includes('/merchant/')) {
                        paragraphs.push({ type: 'image', src: img.src });
                    }
                });
            }
        } catch(e) {}

        // Fallback 2: split content text by double newlines
        if (paragraphs.length === 0) {
            contentText.split(/\n\n+/).forEach(t => {
                const trimmed = t.trim();
                if (trimmed) paragraphs.push({ type: 'paragraph', text: trimmed });
            });
        }
    }

    // Extract all merchant images — from container first, then from Nuxt content
    const images = Array.from(
        container.querySelectorAll('img[src*="/merchant/"]')
    )
    .filter(img => img.src.includes('/merchant/') && !img.src.includes('/avatar'))
    .map((img, i) => ({ url: img.src, order: i }));

    // If no images found in DOM, try Nuxt content
    if (images.length === 0) {
        try {
            const nuxtContent = (function findNuxtContent(obj, depth) {
                if (depth > 15 || !obj || typeof obj !== 'object') return null;
                for (const key of Object.keys(obj)) {
                    const val = obj[key];
                    if (typeof val === 'string' && val.includes('No.1') && val.length > 500) return val;
                    if (typeof val === 'object' && val !== null) {
                        const r = findNuxtContent(val, depth + 1);
                        if (r) return r;
                    }
                }
                return null;
            })(window.__NUXT__ || {}, 0);
            if (nuxtContent) {
                const tmp = document.createElement('div');
                tmp.innerHTML = nuxtContent;
                tmp.querySelectorAll('img[src*="/merchant/"]').forEach((img, i) => {
                    if (!img.src.includes('/avatar')) images.push({ url: img.src, order: i });
                });
            }
        } catch(e) {}
    }

    return { title, time: timeText, paragraphs, images, url: location.href };
})()"""


def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name)


async def find_article_url(target_date=None):
    """Find article URL from author page. target_date in YYYYMMDD format, or None for latest."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(AUTHOR_PAGE, wait_until="networkidle", timeout=30000)

        articles = await page.evaluate(r"""(() => {
            const nuxt = window.__NUXT__ || {};
            function findList(obj, depth) {
                if (depth > 15 || !obj || typeof obj !== 'object') return null;
                for (const key of Object.keys(obj)) {
                    const val = obj[key];
                    if (Array.isArray(val) && val.length > 0 &&
                        val[0].article_id && val[0].create_time) return val;
                    if (typeof val === 'object' && val !== null) {
                        const r = findList(val, depth + 1);
                        if (r) return r;
                    }
                }
                return null;
            }
            const list = findList(nuxt, 0);
            return list ? list.map(item => ({
                article_id: item.article_id,
                title: item.title || '',
                create_time: item.create_time || ''
            })) : null;
        })()""")

        await browser.close()

        if not articles:
            print("[discover] No articles found in Nuxt state")
            return None

        print(f"[discover] Found {len(articles)} articles from Nuxt state")
        for a in articles[:3]:
            print(f"[discover]   {a['create_time']} - {a['title']}")

        if target_date:
            target_prefix = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
            for a in articles:
                if a['create_time'].startswith(target_prefix):
                    url = f"https://www.jiuyangongshe.com/a/{a['article_id']}"
                    print(f"[discover] Matched: {a['title']} -> {url}")
                    return url
            print(f"[discover] No article found for date {target_date}")
            return None
        else:
            a = articles[0]
            url = f"https://www.jiuyangongshe.com/a/{a['article_id']}"
            print(f"[discover] Latest: {a['title']} -> {url}")
            return url


def parse_to_markdown(title, time_text, paragraphs, image_dir="images"):
    """Parse paragraphs into structured markdown."""
    lines = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**时间**：{time_text}")
    lines.append("")

    img_counter = [0]  # mutable counter
    list_counter = [0]  # mutable counter for numbered lists

    def next_image():
        img_counter[0] += 1
        pad = f"{img_counter[0]:02d}"
        return f"![image](./{image_dir}/img_{pad}.png)"

    def reset_list():
        list_counter[0] = 0

    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]

        if p.get('type') == 'image':
            lines.append(next_image())
            lines.append("")
            i += 1
            continue

        text = p.get('text', '')

        # Section header: No.X — reset list counter on new section
        if re.match(r'^No\.\d+', text):
            reset_list()
            lines.append(f"---")
            lines.append("")
            lines.append(f"## {text}")
            lines.append("")
            i += 1
            continue

        # Subsection: Chinese number header — reset list counter
        if re.match(r'^[一二三四五六七八九十]、', text):
            reset_list()
            lines.append(f"### {text}")
            lines.append("")
            i += 1
            continue

        # Numbered items (must be checked BEFORE stock pattern to avoid "2、xxx：yyy" confusion)
        num_match = re.match(r'^(\d+)[、\s]\s*(.+)', text)
        if num_match:
            list_counter[0] += 1
            lines.append(f"{list_counter[0]}. {num_match.group(2)}")
            lines.append("")
            i += 1
            continue

        # Event text (before stock pattern to avoid "事件：xxx" confusion)
        if text.startswith('事件'):
            lines.append(f"> {text}")
            lines.append("")
            i += 1
            continue

        # Hot stock patterns: "板块：股票1、股票2"
        stock_match = re.match(r'^(.+?)：(.+)$', text)
        if stock_match and len(text) < 80:
            key = stock_match.group(1)
            values = stock_match.group(2)
            lines.append(f"- **{key}**：{values}")
            lines.append("")
            i += 1
            continue
            lines.append(f"> {text}")
            lines.append("")
            i += 1
            continue

        # Default: regular paragraph
        lines.append(text)
        lines.append("")
        i += 1

    # Add disclaimer footer
    lines.append("---")
    lines.append("")
    lines.append("> 本文内容基于互联网信息整理，不构成投资建议；仅用于研究学习。")
    lines.append("")

    return "\n".join(lines)


async def scrape(url, output_dir=None):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Single extraction call
        data = await page.evaluate(EXTRACT_JS)

        if not data or data.get("error"):
            print(f"[scrape] Error: {data.get('error', 'unknown')}")
            await browser.close()
            return None

        title = data.get("title", "untitled")
        time_text = data.get("time", "")
        paragraphs = data.get("paragraphs", [])
        images = data.get("images", [])

        print(f"[scrape] Title: {title}")
        print(f"[scrape] Paragraphs: {len(paragraphs)}, Images: {len(images)}")

        if not paragraphs:
            print("[scrape] No paragraphs found — falling back to innerText")
            body_text = await page.evaluate("""
                () => { const c = document.querySelector('.detail-container'); return c ? c.innerText : ''; }
            """)
            paragraphs = [{"type": "paragraph", "text": body_text}]

        # Derive date from time or URL
        date_str = ""
        if time_text:
            m = re.match(r'(\d{4}-\d{2}-\d{2})', time_text)
            if m:
                date_str = m.group(1).replace("-", "")  # e.g. "20260522"

        if not output_dir:
            folder = date_str if date_str else "unknown"
            output_dir = OUTPUT_BASE / folder

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        img_dir = output_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)

        # Download images via page context (inherits cookies)
        for idx, img in enumerate(images):
            try:
                response = await page.request.get(img["url"], timeout=30000)
                if response.ok:
                    ext = ".png"
                    content_type = response.headers.get("content-type", "")
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = ".jpg"
                    filepath = img_dir / f"img_{idx + 1:02d}{ext}"
                    filepath.write_bytes(await response.body())
            except Exception as e:
                print(f"[scrape] Image {idx + 1} failed: {e}")

        print(f"[scrape] Downloaded {len(images)} images to {img_dir}")

        # Generate markdown
        md_content = parse_to_markdown(title, time_text, paragraphs)
        md_path = output_dir / f"{sanitize_filename(title)}.md"
        md_path.write_text(md_content, encoding="utf-8")
        print(f"[scrape] Markdown saved to {md_path}")

        await browser.close()
        return str(md_path)


def main():
    parser = argparse.ArgumentParser(description="Scrape jiuyangongshe.com article")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", default=None, help="Article URL")
    group.add_argument("--latest", action="store_true", help="Auto-discover and scrape latest article")
    group.add_argument("--date", default=None, help="Auto-discover and scrape article for date (YYYYMMDD)")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--raw", action="store_true", help="Output raw JSON instead of markdown")
    args = parser.parse_args()

    if args.url:
        url = args.url
    elif args.latest:
        url = asyncio.run(find_article_url(target_date=None))
    elif args.date:
        url = asyncio.run(find_article_url(target_date=args.date))
    else:
        url = None

    if not url:
        print("[scrape] No URL to scrape")
        return

    md_path = asyncio.run(scrape(url, args.output_dir))
    if md_path:
        print(f"[scrape] Done -> {md_path}")


if __name__ == "__main__":
    main()
