import os
import requests
import json
import sys
import re
import time

'''
此文件为同步Notion网站使用
'''

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
NOTION_TOKEN = getattr(config, "NOTION_TOKEN", "")
NOTION_PAGE_ID = getattr(config, "NOTION_PAGE_ID", "")
PROXY_URL = getattr(config, "PROXY_URL", "")


def _strip_html_comments(markdown_text):
    """移除仅供本地 Markdown 保存的隐藏元数据，避免同步到网页。"""
    return re.sub(r'<!--.*?-->', '', markdown_text, flags=re.DOTALL)


def _limit_prediction_details(markdown_text, max_prediction_dates):
    """保留报告头部，以及最近若干个一日预测目标日期的详细记录。"""
    if not max_prediction_dates or max_prediction_dates < 1:
        return markdown_text, []

    first_section = re.search(r"(?m)^### 预测时间:", markdown_text)
    if not first_section:
        return markdown_text, []

    header = markdown_text[:first_section.start()]
    sections = re.split(
        r"(?m)(?=^### 预测时间:)",
        markdown_text[first_section.start():],
    )
    section_dates = []
    for section in sections:
        match = re.search(
            r"\|\s*【(\d{4}-\d{2}-\d{2})\s+方向】",
            section,
        )
        section_dates.append(match.group(1) if match else "")

    recent_dates = sorted({date for date in section_dates if date})[-max_prediction_dates:]
    if not recent_dates:
        return markdown_text, []

    recent_date_set = set(recent_dates)
    kept_sections = [
        section
        for section, target_date in zip(sections, section_dates)
        if target_date in recent_date_set
    ]
    notice = (
        f"> Notion 网页仅展示最近 {len(recent_dates)} 个一日预测目标日期的详细记录"
        f"（{recent_dates[0]} ~ {recent_dates[-1]}）；本地 Markdown 保留完整历史。\n\n"
    )
    return header.rstrip() + "\n\n" + notice + "".join(kept_sections), recent_dates


def _split_markdown_for_two_requests(markdown_text):
    """尽量在二级标题或空行处把 Markdown 平分为两段。"""
    if len(markdown_text) < 2:
        return markdown_text, ""

    midpoint = len(markdown_text) // 2
    heading_positions = [
        match.start()
        for match in re.finditer(r"(?m)^#{2,4} ", markdown_text)
        if 0 < match.start() < len(markdown_text)
    ]
    if heading_positions:
        split_at = min(heading_positions, key=lambda position: abs(position - midpoint))
    else:
        next_blank = markdown_text.find("\n\n", midpoint)
        prev_blank = markdown_text.rfind("\n\n", 0, midpoint)
        candidates = [position + 2 for position in (prev_blank, next_blank) if position >= 0]
        split_at = min(candidates, key=lambda position: abs(position - midpoint)) if candidates else midpoint

    return markdown_text[:split_at], markdown_text[split_at:]


def _patch_with_retry(url, headers, payload, action_name, max_attempts=3):
    """调用 Notion PATCH 接口，失败最多尝试三次。"""
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[{attempt}/{max_attempts}] 正在调用 Notion API {action_name}...")
            res = requests.patch(url, headers=headers, json=payload, timeout=30)
            if res.status_code == 200:
                print(f"[+] {action_name}成功")
                return True
            print(f"[-] {action_name}失败，HTTP 状态码: {res.status_code}\n{res.text}")
        except Exception as exc:
            print(f"[-] {action_name}发生异常: {exc}")

        if attempt < max_attempts:
            print("等待 3 秒后自动重试...")
            time.sleep(3)

    print(f"[-] {action_name}连续 {max_attempts} 次失败，停止同步")
    return False

def sync_md_to_notion(file_path, page_id=None, max_prediction_dates=None):
    file_path = os.fspath(file_path)
    if page_id is None:
        page_id = NOTION_PAGE_ID

    if not NOTION_TOKEN or not page_id:
        print("提示: 未配置 NOTION_TOKEN 或 NOTION_PAGE_ID，跳过 Notion 同步。")
        return

    if PROXY_URL:
        os.environ["http_proxy"] = PROXY_URL
        os.environ["https_proxy"] = PROXY_URL

    if not os.path.exists(file_path):
        print(f"❌ 找不到文件: {file_path}")
        return

    print("正在读取本地 Markdown 文件...")
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    if not lines:
        print("⚠️ 提醒: 文件为空。")
        return

    markdown_text, synced_dates = _limit_prediction_details(
        "".join(lines),
        max_prediction_dates,
    )
    if synced_dates:
        print(
            f"Notion 仅同步最近 {len(synced_dates)} 个预测目标日期的详细记录: "
            f"{synced_dates[0]} ~ {synced_dates[-1]}"
        )
    lines = markdown_text.splitlines(keepends=True)

    first_line = lines[0].strip()
    # 检查第一行是不是以 '# ' 开头的标题
    if not first_line.startswith("# "):
        print("⚠️ 提醒: Markdown 文件的第一行不是以 '# ' 开头的标题。")
        page_title = "未命名同步页面"
        rest_content = "".join(lines).strip()
    else:
        # 提取纯文本标题 (去掉前面的 '# ')
        page_title = first_line.replace("# ", "", 1).strip()
        print(f"提取到完美标题: 【{page_title}】")
        # 剩下的正文，跳过第一行
        rest_content = "".join(lines[1:]).strip()

    # HTML 注释只用于本地文件保存元数据，不同步到 Notion 网页。
    rest_content = _strip_html_comments(rest_content)

    # 在正文顶部补充项目地址
    project_link_line = "👉 项目地址：[wild-firefox/stock_policy](https://github.com/wild-firefox/stock_policy)\n\n"
    rest_content = project_link_line + rest_content

    if file_path.endswith('.ipynb'):
        rest_content = f"```json\n{rest_content}\n```"

    # Notion API 必须的请求头
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json"
    }

    # 解析 Markdown 格式的链接放入 Notion title 的 rich_text 数组
    def parse_markdown_to_rich_text(text):
        pattern = r'\[([^\]]+)\]\(([^\)]+)\)'
        rich_text = []
        last_idx = 0
        for match in re.finditer(pattern, text):
            if match.start() > last_idx:
                rich_text.append({"text": {"content": text[last_idx:match.start()]}})
            rich_text.append({
                "text": {
                    "content": match.group(1),
                    "link": {"url": match.group(2)}
                }
            })
            last_idx = match.end()
        if last_idx < len(text):
            rich_text.append({"text": {"content": text[last_idx:]}})
        return rich_text if rich_text else [{"text": {"content": text}}]

    title_rich_text = parse_markdown_to_rich_text(page_title)

    # ================= 动作一：更新 Notion 的页面标题 =================
    url_title = f"https://api.notion.com/v1/pages/{page_id}"
    payload_title = {
        "properties": {
            "title": {
                "title": title_rich_text
            }
        }
    }
    if not _patch_with_retry(url_title, headers, payload_title, "更新页面标题"):
        return False

    # ================= 动作二：分两次更新正文 =================
    url_content = f"https://api.notion.com/v1/pages/{page_id}/markdown"
    first_content, second_content = _split_markdown_for_two_requests(rest_content)
    replace_payload = {
        "type": "replace_content",
        "replace_content": {
            "new_str": first_content
        }
    }
    if not _patch_with_retry(url_content, headers, replace_payload, "更新正文内容（第 1/2 段）"):
        return False

    if second_content:
        append_payload = {
            "type": "insert_content",
            "insert_content": {
                "content": second_content,
                "position": {"type": "end"},
            },
        }
        if not _patch_with_retry(url_content, headers, append_payload, "更新正文内容（第 2/2 段）"):
            return False

    return True

if __name__ == "__main__":
    sync_md_to_notion("predict/deepseek-v4-pro/experience/predict_eval_history.md")
