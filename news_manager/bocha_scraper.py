"""Scraper using Bocha AI search + Playwright full-article fetch — stocknews-style output."""

import argparse
import asyncio
import hashlib
import os
import random
import re
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BOCHA_API_KEY, NEWS_BOCHA_DIR, A_SHARES_LIST_CSV
from news_manager.stocknews_scraper import (
    GENERIC_EXTRACT_JS,
    parse_article_to_markdown,
    sanitize_filename,
)

OUTPUT_BASE = NEWS_BOCHA_DIR

# Financial news sources (Bocha `include` whitelist)
NEWS_SITES = [
    "stcn.com",              # 证券时报
    "cnstock.com",           # 上海证券报
    "cs.com.cn",             # 中证网
    "10jqka.com.cn",         # 同花顺
    "eastmoney.com",         # 东方财富
    "cfi.cn",                # 中财网
    "cls.cn",                # 财联社
    "yicai.com",             # 第一财经
    "21jingji.com",          # 21世纪经济报道
    "finance.sina.com.cn",   # 新浪财经
    "finance.qq.com",        # 腾讯财经
    "cninfo.com.cn",         # 巨潮资讯
    "jrj.com.cn",            # 金融界
    "hexun.com",             # 和讯
    "163.com/money",         # 网易财经
    "news.qq.com",           # 腾讯新闻
    "ofweek.com",            # OFweek科技
    "sohu.com/a/",           # 搜狐新闻文章
    "sina.com.cn",           # 新浪
    "163.com",               # 网易
]

# Domains to exclude from Bocha results
EXCLUDE_DOMAINS = [
    "q.stock.sohu.com",      # Sohu stock quote
    "stock.sohu.com",        # Sohu stock detail
    "finance.sina.com.cn/realstock",  # Sina realtime quote
    "quote.eastmoney.com",   # Eastmoney quote
    "sohu.com/zs/",          # Sohu index pages
]

# Domains/URL patterns that are not news articles (post-fetch filter)
NOISE_PATTERNS = [
    r"q\.stock\.sohu\.com",
    r"/zs/\d+/index",
    r"stock\.sohu\.com/cn/\d+/index",
    r"finance\.sina\.com\.cn/realstock",
    r"quote\.eastmoney\.com",
    r"stockpage\.10jqka\.com\.cn/\d+/",
]


async def create_stealth_context(browser):
    """Create a browser context with anti-detection measures."""
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


async def setup_resource_blocking(page):
    """Block unnecessary resources to reduce fingerprint and speed up loading."""
    await page.route("**/*", lambda route: (
        route.abort()
        if route.request.resource_type in ("font", "media", "websocket")
        or any(k in route.request.url for k in [
            "google-analytics", "googletagmanager", "doubleclick",
            "baidu.com/hm", "cnzz.com", "analytics",
        ])
        else route.continue_()
    ))



def is_noise_url(url):
    """Check if URL is a known noise/non-article page."""
    for pattern in NOISE_PATTERNS:
        if re.search(pattern, url):
            return True
    return False


def is_relevant(title, content, stock_code, stock_name):
    """Check if a search result is relevant to the target stock."""
    targets = [stock_code]
    if stock_name:
        targets.append(stock_name)
    # Also try short code (without exchange prefix)
    if len(stock_code) == 6:
        targets.append(stock_code)
    text = f"{title} {content[:200]}"
    for t in targets:
        if t in text:
            return True
    return False


def bocha_search(query, freshness=None, count=50, include=None, exclude=None):
    """Call Bocha AI web search API."""
    headers = {
        "Authorization": f"Bearer {BOCHA_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "summary": True, "count": count}
    if freshness:
        payload["freshness"] = freshness
    if include:
        payload["include"] = "|".join(include)
    if exclude:
        payload["exclude"] = "|".join(exclude)

    try:
        resp = requests.post(
            "https://api.bochaai.com/v1/web-search",
            headers=headers, json=payload, timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[bocha] API request failed: {e}")
        return None


def get_recent_trade_dates(days):
    """Return the last N trading days in YYYY-MM-DD format."""
    today = pd.Timestamp.today()
    b_days = pd.bdate_range(end=today, periods=days + 5)[::-1]
    return [d.strftime("%Y-%m-%d") for d in b_days][:days]


def parse_bocha_date(date_str):
    """Parse Bocha UTC date string to (exact_time_utc8, short_date_mm/dd)."""
    if not date_str:
        return "unknown", "00/00"
    try:
        date_str = date_str.replace("Z", "")
        dt = datetime.fromisoformat(date_str) + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M:%S"), dt.strftime("%m/%d")
    except Exception:
        return str(date_str), "00/00"


async def fetch_article(context, url):
    """Fetch and extract a single article using stealth context. Returns (data, page) or (None, page)."""
    page = await context.new_page()
    try:
        await setup_resource_blocking(page)
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        final_url = page.url

        if final_url.lower().endswith(".pdf"):
            print(f"  [skip] PDF: {url[:60]}")
            return None, page

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            await asyncio.sleep(2)

        data = await page.evaluate(GENERIC_EXTRACT_JS)
        if not data or not data.get("elements"):
            print(f"  [skip] No content: {url[:60]}")
            return None, page

        data["url"] = final_url
        return data, page
    except Exception as e:
        print(f"  [error] {url[:60]} — {e}")
        return None, page


async def scrape_stock(stock_code, days=3, fetch_articles=True):
    """Search Bocha for stock news and optionally fetch full articles."""
    from playwright.async_api import async_playwright

    stock_name = ""
    if A_SHARES_LIST_CSV.exists():
        try:
            df = pd.read_csv(A_SHARES_LIST_CSV, dtype={"symbol": str})
            match = df[df["symbol"] == stock_code]
            if not match.empty:
                stock_name = match.iloc[0]["name"]
        except Exception as e:
            print(f"[bocha] Failed to read stock list: {e}")

    display = f"{stock_name}({stock_code})" if stock_name else stock_code
    today = datetime.now()
    today_ymd = today.strftime("%Y%m%d")
    ts = today.strftime("%H%M%S")

    # 1. Multi-query Bocha search
    target_dates = get_recent_trade_dates(days)
    freshness = f"{target_dates[-1]}..{target_dates[0]}"

    queries = [
        f"{stock_name} {stock_code}",
        f"{stock_name} 公告 新闻 研报",
    ]

    seen_urls = set()
    all_results = []

    for qi, query in enumerate(queries):
        print(f"[bocha] Query {qi + 1}/{len(queries)}: {query}")
        result = bocha_search(
            query,
            freshness=freshness,
            count=50,
            exclude=EXCLUDE_DOMAINS,
        )
        if not result or "data" not in result or "webPages" not in result["data"]:
            continue

        web_pages = result["data"]["webPages"].get("value", [])
        print(f"[bocha]   -> {len(web_pages)} results")

        for item in web_pages:
            url = item.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = item.get("name", "untitled")
            content = item.get("summary") or item.get("snippet", "")
            raw_date = item.get("datePublished") or item.get("dateLastCrawled") or ""
            exact_time, short_date = parse_bocha_date(raw_date)

            if is_noise_url(url):
                continue
            if not is_relevant(title, content, stock_code, stock_name):
                continue

            all_results.append({
                "title": title,
                "url": url,
                "date": short_date,
                "exact_time": exact_time,
                "content": content,
                "fetched": False,
                "article_file": "",
            })

    print(f"[bocha] Total after dedup + filter: {len(all_results)} relevant results")

    # 2. Fetch full articles
    articles_dir = OUTPUT_BASE / stock_code / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    if fetch_articles and all_results:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await create_stealth_context(browser)

            fetched = 0
            for idx, entry in enumerate(all_results):
                date_prefix = entry["date"].replace("/", "-")
                filename = f"{date_prefix}_{sanitize_filename(entry['title'])}.md"
                filepath = articles_dir / filename

                if filepath.exists():
                    print(f"  [{idx + 1}/{len(all_results)}] exists: {filename}")
                    entry["fetched"] = True
                    entry["article_file"] = filename
                    fetched += 1
                    continue

                print(f"  [{idx + 1}/{len(all_results)}] fetch: {entry['title'][:50]}")
                article_data, page = await fetch_article(context, entry["url"])

                if article_data:
                    # Download images via the article page
                    image_map = {}
                    images_dir = articles_dir / "images"
                    images_dir.mkdir(parents=True, exist_ok=True)
                    for el in article_data.get("elements", []):
                        if el.get("type") != "image":
                            continue
                        src = el["src"]
                        if src in image_map:
                            continue
                        try:
                            resp = await page.request.get(src, timeout=10000)
                            if resp.ok:
                                ct = resp.headers.get("content-type", "")
                                ext = "jpg"
                                if "png" in ct:
                                    ext = "png"
                                elif "webp" in ct:
                                    ext = "webp"
                                elif "gif" in ct:
                                    ext = "gif"
                                img_name = f"{hashlib.md5(src.encode()).hexdigest()[:12]}.{ext}"
                                img_path = images_dir / img_name
                                img_path.write_bytes(await resp.body())
                                image_map[src] = f"images/{img_name}"
                        except Exception as e:
                            print(f"    [img fail] {src[:60]} — {e}")

                    if article_data.get("title") and len(article_data["title"]) > 5:
                        entry["title"] = article_data["title"]
                        filename = f"{date_prefix}_{sanitize_filename(article_data['title'])}.md"
                        filepath = articles_dir / filename

                    md_content = parse_article_to_markdown(article_data, image_map)
                    filepath.write_text(md_content, encoding="utf-8")
                    entry["fetched"] = True
                    entry["article_file"] = filename
                    fetched += 1
                    print(f"    [+] {filename}")
                else:
                    # Save snippet as fallback
                    fallback = f"# {entry['title']}\n\n"
                    fallback += f"**时间**：{entry['exact_time']}\n\n"
                    fallback += f"**链接**：[{entry['url']}]({entry['url']})\n\n"
                    fallback += entry["content"]
                    fallback += "\n\n> 仅搜索摘要，全文抓取失败。\n"
                    filepath.write_text(fallback, encoding="utf-8")

                await page.close()

                # Random delay between fetches
                delay = random.uniform(2.0, 5.0)
                print(f"    [wait] {delay:.1f}s")
                await asyncio.sleep(delay)

            await context.close()
            await browser.close()
            print(f"[bocha] Articles fetched: {fetched}/{len(all_results)}")

    # 3. Save listing markdown
    output_dir = OUTPUT_BASE / stock_code / today_ymd
    output_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append(f"# {display} - 博查AI搜索")
    lines.append("")
    lines.append(f"> 搜索时间：{today.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 搜索查询：{', '.join(queries)}")
    lines.append(f"> 时间范围：{freshness}")
    lines.append(f"> 结果数量：{len(all_results)} 条")
    lines.append("")

    for i, entry in enumerate(all_results, 1):
        lines.append("---")
        lines.append("")
        lines.append(f"## {i}. {entry['title']}")
        lines.append("")
        lines.append(f"- **时间**：{entry['exact_time']}")
        lines.append(f"- **链接**：[{entry['url']}]({entry['url']})")
        if entry["fetched"] and entry["article_file"]:
            lines.append(f"- **本地**：[查看全文](../articles/{entry['article_file']})")
        lines.append("")
        lines.append(entry["content"][:500])
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 数据来源：博查AI搜索引擎 + 原文抓取，内容仅供参考。")

    md_path = output_dir / f"{stock_code}_博查AI_{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[bocha] Listing saved to {md_path}")

    return str(md_path)


def main():
    parser = argparse.ArgumentParser(description="Bocha AI search + full article scrape")
    parser.add_argument("--code", required=True, help="Stock code (e.g. 600410)")
    parser.add_argument("--days", type=int, default=3, help="Trading days to search (default: 3)")
    parser.add_argument("--no-fetch", action="store_true", help="Skip full article fetching")
    args = parser.parse_args()

    md_path = asyncio.run(scrape_stock(args.code, args.days, fetch_articles=not args.no_fetch))
    if md_path:
        print(f"[bocha] Done -> {md_path}")


if __name__ == "__main__":
    main()
