import os
import requests
import sys

# 将上级目录加入 sys.path 以便导入 config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from config import NOTION_TOKEN, PROXY_URL
except ImportError:
    NOTION_TOKEN = ""
    PROXY_URL = ""

def read_local_markdown(file_name: str) -> str:
    """读取 private_data/data 目录下的指定 md 文件内容"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "data", file_name)

    # 自动补充 .md 后缀
    if not file_path.endswith('.md'):
        file_path += '.md'

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"未找到本地知识库文件: {file_path}")

    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()

def _parse_rich_text(rich_text_array):
    """提取 Notion rich_text 数组中的纯文本"""
    if not rich_text_array:
        return ""
    return "".join([t.get("plain_text", "") for t in rich_text_array])

def _fetch_blocks(block_id: str, headers: dict) -> list:
    """递归获取区块的所有子区块"""
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"读取 Notion Block 失败: HTTP {response.status_code} - {response.text}")

    data = response.json()
    return data.get("results", [])

def _parse_blocks_to_markdown(blocks: list, headers: dict, indent_level: int = 0) -> str:
    """将区块列表解析为 Markdown 字符串"""
    markdown_lines = []
    indent = "    " * indent_level

    for block in blocks:
        b_type = block.get("type")
        has_children = block.get("has_children", False)

        if not b_type or b_type not in block:
            continue

        b_content = block[b_type]

        # 处理代码块
        if b_type == "code":
            text = _parse_rich_text(b_content.get("rich_text", []))
            language = b_content.get("language", "")
            markdown_lines.append(f"{indent}```{language}\n{text}\n{indent}```")
            continue

        # 处理分隔线
        if b_type == "divider":
            markdown_lines.append(f"{indent}---")
            continue

        # 处理书签
        if b_type == "bookmark":
            url_link = b_content.get("url", "")
            markdown_lines.append(f"{indent}[Bookmark/Link]({url_link})")
            continue

        # 尝试提取普通富文本段落
        text = ""
        if "rich_text" in b_content:
            text = _parse_rich_text(b_content["rich_text"])

        # 根据类型映射为 Markdown
        if b_type == "paragraph":
            if text:
                markdown_lines.append(f"{indent}{text}")
            else:
                markdown_lines.append("")
        elif b_type == "heading_1":
            markdown_lines.append(f"{indent}# {text}")
        elif b_type == "heading_2":
            markdown_lines.append(f"{indent}## {text}")
        elif b_type == "heading_3":
            markdown_lines.append(f"{indent}### {text}")
        elif b_type == "bulleted_list_item":
            markdown_lines.append(f"{indent}- {text}")
        elif b_type == "numbered_list_item":
            markdown_lines.append(f"{indent}1. {text}")
        elif b_type == "quote":
            markdown_lines.append(f"{indent}> {text}")
        elif b_type == "callout":
            markdown_lines.append(f"{indent}> 💡 {text}")
        elif b_type == "toggle":
            markdown_lines.append(f"{indent}<details><summary>{text}</summary>\n")
        else:
            if text:
                markdown_lines.append(f"{indent}{text}")

        # 递归处理子块
        if has_children:
            child_blocks = _fetch_blocks(block["id"], headers)
            # Toggle 折叠块的子内容不需要缩进，但被包含在 HTML 标签内
            if b_type == "toggle":
                child_md = _parse_blocks_to_markdown(child_blocks, headers, indent_level)
                markdown_lines.append(child_md)
                markdown_lines.append(f"{indent}</details>")
            else:
                child_md = _parse_blocks_to_markdown(child_blocks, headers, indent_level + 1)
                markdown_lines.append(child_md)

    return "\n\n".join(markdown_lines)

def read_notion_page(page_id: str) -> str:
    """读取指定 Notion Page 的纯文本内容并格式化为 Markdown（支持段落、标题、列表、代码块、折叠列表及嵌套块等基本类型）"""
    if not NOTION_TOKEN:
        raise ValueError("请在 config.py 中配置 NOTION_TOKEN，或传递有效的 Token")

    if PROXY_URL:
        os.environ["http_proxy"] = PROXY_URL
        os.environ["https_proxy"] = PROXY_URL

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28"
    }

    blocks = _fetch_blocks(page_id, headers)
    return _parse_blocks_to_markdown(blocks, headers, 0)

# 简单测试入口
if __name__ == "__main__":
    print("支持的方法:")
    print("1. read_local_markdown('example.md') -> 读取 private_data/data 里的文件")
    print("2. read_notion_page('page_id') -> 读取并解析 Notion 页面为 Markdown")
