"""从 config.py 生成不包含私密默认值的 config.example.py。

生成结果保留源文件的代码、注释、引号和换行，仅替换
``os.environ.get(环境变量, 默认值)`` 中需要脱敏的默认值。
"""

from __future__ import annotations

import argparse
import codecs
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = PROJECT_ROOT / "config.py"
DEFAULT_OUTPUT = PROJECT_ROOT / "config.example.py"

# 这些字段的示例值不是真实配置，只用于说明应填入的内容。
PLACEHOLDERS = {
    "TUSHARE_TOKEN": "API_KEY",
    "DEEPSEEK_API_KEY": "API_KEY",
    "BOCHA_API_KEY": "API_KEY",
    "TICKFLOW_API_KEY": "API_KEY",
    "NOTION_TOKEN": "API_KEY",
    "NOTION_PAGE_ID": "PAGE_ID",
    "NOTION_PRIVATE_PAGE_ID": "PAGE_ID",
    "AIHUBMIX_API_KEY": "API_KEY",
    "THS_WINDOW_TITLE": ".*证券公司.*",
    "PROXY_URL": "http://[IP_ADDRESS]",
}

SENSITIVE_NAME_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PAGE_ID",
    "PROXY_URL",
    "WINDOW_TITLE",
)

ENV_GET_PATTERN = re.compile(
    r"os\.environ\.get\(\s*"
    r"(?P<name_quote>['\"])(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=name_quote)\s*,\s*"
    r"(?P<value_quote>['\"])(?P<value>.*?)(?P=value_quote)\s*\)"
)


def sanitize_config(source_text: str) -> str:
    """替换配置文本中的私密默认值，其余内容保持不变。"""
    matches = list(ENV_GET_PATTERN.finditer(source_text))
    found_names = {match.group("name") for match in matches}

    unknown_sensitive = sorted(
        name
        for name in found_names
        if any(marker in name.upper() for marker in SENSITIVE_NAME_MARKERS)
        and name not in PLACEHOLDERS
    )
    if unknown_sensitive:
        raise ValueError(
            "发现未配置脱敏占位符的敏感环境变量: "
            + ", ".join(unknown_sensitive)
        )

    parts: list[str] = []
    previous_end = 0
    for match in matches:
        name = match.group("name")
        if name not in PLACEHOLDERS:
            continue
        value_start, value_end = match.span("value")
        parts.append(source_text[previous_end:value_start])
        parts.append(PLACEHOLDERS[name])
        previous_end = value_end

    parts.append(source_text[previous_end:])
    return "".join(parts)


def generate_config_example(
    source_path: Path = DEFAULT_SOURCE,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    check_only: bool = False,
) -> bool:
    """生成或检查示例配置。

    返回 True 表示输出已与应生成内容一致；检查模式下不写文件。
    """
    raw_source = source_path.read_bytes()
    has_utf8_bom = raw_source.startswith(codecs.BOM_UTF8)
    source_text = raw_source.decode("utf-8-sig")
    sanitized_text = sanitize_config(source_text)
    output_bytes = (
        (codecs.BOM_UTF8 if has_utf8_bom else b"")
        + sanitized_text.encode("utf-8")
    )

    is_current = output_path.exists() and output_path.read_bytes() == output_bytes
    if check_only or is_current:
        return is_current

    output_path.write_bytes(output_bytes)
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 config.py 生成脱敏后的 config.example.py"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 config.example.py 是否与当前 config.py 同步",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    is_current = generate_config_example(
        args.source,
        args.output,
        check_only=args.check,
    )
    if args.check and not is_current:
        print(f"[X] 示例配置需要更新: {args.output}")
        return 1
    action = "检查通过" if args.check else "生成完成"
    print(f"[+] {action}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
