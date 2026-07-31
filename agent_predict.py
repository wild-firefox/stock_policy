import argparse
import difflib
import glob
import json
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
import requests
from openai import OpenAI

# Ensure project-local imports work when the script is launched directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import predict_evaluator
from config import DEEPSEEK_API_KEY, PREDICTION_OUTPUT_RULES, TUSHARE_TOKEN, get_pro
import config
NOTION_PAGE_ID = getattr(config, "NOTION_PAGE_ID", "")
from tushare_tools.high_freq_data import (
    get_daily_qfq_with_indicators,
    get_etf_daily_with_indicators,
    get_idx_daily_with_indicators,
    get_money_flow_data,
    get_stock_basic_info,
)


# =============================================================================
# Configuration and shared constants
# =============================================================================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# 9:00-12:00 和 14:00-18:00（北京时间），在此期间调用 API 的价格会翻倍
# 预测默认先通过 Claude CLI/CC Switch 调用；CLI 失败后使用 DeepSeek API。
# 两个模型相互独立，可以配置为不同模型。
MODEL_PREDICT_CLI_NAME = "deepseek-v4-pro"
MODEL_PREDICT_API_NAME = "deepseek-v4-pro"

# 预测档案名称只控制 predict/<档案名称>/ 下的历史、复盘知识库和输出目录，
# 与实际调用的 CLI/API 模型相互独立。
# - 更换模型但继续沿用原历史：只修改上面的模型名称，保持这里不变。
# - 为新模型建立独立目录：把这里改成新的档案名称，例如 "grok-4.5"。
PREDICT_PROFILE_NAME = "deepseek-v4-pro"

# 新档案首次运行时，可以从指定旧档案继承最新的 predict_ex_*.md。
# 这里只复制知识库，不复制旧预测文件、predict_eval_history.md 或统计结果。
# 留空表示新档案从空知识库开始；目标档案已有知识库时也不会再次复制。
INITIAL_KB_PROFILE_NAME = ""

# 复盘模型和助手模型仅通过 Claude CLI/CC Switch 调用。
MODEL_REFLECT_NAME = "deepseek-v4-pro"
MODEL_ASSISTANT_NAME = 'deepseek-v4-flash'   #"deepseek-v4-pro" 'mimo-v2.5-pro' #'gpt-5.5-free'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_BASE_DIR = os.path.join(BASE_DIR, "news_manager", "data", "stk", "raw_ths")
PREDICT_DIR = os.path.join(BASE_DIR, "predict")

TRADING_CALENDAR_DAYS = 30
MARKET_OPEN_MINUTE = 9 * 60 + 30
MARKET_CLOSE_MINUTE = 15 * 60
DAILY_DATA_READY_HOUR = 19
MAX_AGENT_STEPS = 5
MAX_AGENT_WORKERS = 3


# =============================================================================
# Asset metadata and trading-calendar helpers
# =============================================================================

def _get_ts_code(code: str, atype: str) -> str:
    if '.' in code:
        return code
    if atype == 'stk':
        if code.startswith('6'):
            return f"{code}.SH"
        if code.startswith('8') or code.startswith('4'):
            return f"{code}.BJ"
        return f"{code}.SZ"
    if atype == 'etf':
        return f"{code}.SH" if code.startswith('5') else f"{code}.SZ"
    if atype == 'idx':
        return f"{code}.SZ" if code.startswith('3') else f"{code}.SH"
    return f"{code}.SZ"


def _get_tencent_symbol(code: str, asset_type: str) -> str:
    """把标的代码统一转换为腾讯行情接口使用的市场前缀格式。"""
    normalized = code.strip().lower()
    if '.' in normalized:
        pure_code, suffix = normalized.split('.', 1)
        return f"{suffix}{pure_code}"
    if asset_type == 'stk':
        if normalized.startswith('6'):
            return f"sh{normalized}"
        if normalized.startswith(('0', '3')):
            return f"sz{normalized}"
        if normalized.startswith(('4', '8')):
            return f"bj{normalized}"
        return f"sh{normalized}"
    if asset_type == 'etf':
        return f"sh{normalized}" if normalized.startswith('5') else f"sz{normalized}"
    if asset_type == 'idx':
        return f"sz{normalized}" if normalized.startswith('3') else f"sh{normalized}"
    return f"sh{normalized}"


def _is_market_open(now: datetime, is_trading_day: bool) -> bool:
    """按当前既有口径判断是否处于交易时段。"""
    current_minutes = now.hour * 60 + now.minute
    return is_trading_day and MARKET_OPEN_MINUTE <= current_minutes <= MARKET_CLOSE_MINUTE


def get_target_name(code: str, asset_type: str) -> str:
    """使用 Tushare 根据资产类型获取中文简称"""

    pro = get_pro()
    name = ""
    ts_code = _get_ts_code(code, asset_type)

    try:
        if asset_type == 'stk':
            df = pro.stock_basic(ts_code=ts_code, fields='ts_code,name')
        elif asset_type == 'etf':
            df = pro.etf_basic(ts_code=ts_code, fields='ts_code,extname')
        elif asset_type == 'idx':
            df = pro.index_basic(ts_code=ts_code, fields='ts_code,name')
        else:
            df = None

        if df is not None and not df.empty:
            name = df.iloc[0]['extname'] if asset_type == 'etf' else df.iloc[0]['name']
    except Exception as e:
        print(f"获取 {code} 名称失败: {e}")

    return name


def _find_claude() -> str:
    """查找 claude 可执行文件路径（跨平台：Windows / Linux / macOS）"""
    # 1. PATH 中查找
    for name in ["claude", "claude.cmd"]:
        path = shutil.which(name)
        if path:
            return path

    # 2. 常见安装位置回退
    candidates = []
    home = os.path.expanduser("~")
    if os.name == "nt":
        candidates = [
            os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
            os.path.join(home, "AppData", "Roaming", "npm", "claude.cmd"),
        ]
    else:
        candidates = [
            os.path.join(home, "npm", "bin", "claude"),
            os.path.join(home, ".npm-global", "bin", "claude"),
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    return "claude"  # 最后兜底


CLAUDE_EXE = _find_claude()


def _format_prediction_cli_context(messages: list[dict]) -> str:
    """把 API 消息历史整理为 Claude CLI 可通过 stdin 接收的完整上下文。"""
    role_names = {
        "system": "SYSTEM",
        "user": "USER",
        "assistant": "ASSISTANT",
    }
    context_parts = [
        "以下是股票预测任务的完整会话上下文。"
        "请严格遵守 SYSTEM 内容，并只输出下一条 ASSISTANT 回复，"
        "不要解释这些标签，也不要重复已有回复。"
    ]
    for message in messages:
        raw_role = message.get("role", "")
        role = role_names.get(raw_role, raw_role.upper() or "UNKNOWN")
        content = message.get("content", "")
        context_parts.append(f"<{role}>\n{content}\n</{role}>")
    context_parts.append("<ASSISTANT_NEXT>")
    return "\n\n".join(context_parts)


class _PredictionCliReply(str):
    """携带 Claude SessionID 的预测文本，仍兼容普通字符串用法。"""

    def __new__(cls, text: str, session_id: str):
        instance = super().__new__(cls, text)
        instance.session_id = session_id
        return instance


def _run_prediction_claude_cli(
    messages: list[dict],
    model_name: str,
    session_id: str = "",
) -> _PredictionCliReply:
    """通过 Claude CLI 获取一轮预测，并在后续轮次续接同一会话。"""
    env = os.environ.copy()
    env["TUSHARE_TOKEN"] = TUSHARE_TOKEN
    env["ANTHROPIC_MODEL"] = model_name

    cmd = [
        CLAUDE_EXE,
        "-p",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.extend([
        "--verbose",
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--model",
        model_name,
    ])

    if session_id:
        if not messages or messages[-1].get("role") != "user":
            raise RuntimeError("续接预测会话时缺少最新一轮用户消息")
        input_text = str(messages[-1].get("content", ""))
    else:
        input_text = _format_prediction_cli_context(messages)

    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=BASE_DIR,
    )
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Claude CLI 执行失败 (Exit Code {result.returncode}): {error_text}"
        )

    reply, _, returned_session_id = _parse_claude_stream(result.stdout or "")
    reply = reply.strip()
    if not reply:
        raise RuntimeError("Claude CLI 未返回任何预测内容")
    return _PredictionCliReply(
        reply,
        returned_session_id or session_id,
    )


# 复盘会话在接近模型上限前提前压缩，给摘要生成和下一轮请求保留空间。
REFLECTION_AUTO_COMPACT_WINDOW = 800000
REFLECTION_COMPACT_COMMAND = (
    "/compact "
    "仅保留股票预测复盘的执行规则、工作文件路径、JSON字段约束、"
    "知识库修改约束和必要的长期结论。"
    "知识库正文以工作目录中的最新 predict_ex_*.md 文件为准。"
    "丢弃历史新闻全文、CSV原始内容、工具调用输出、已经完成的旧复盘任务、"
    "重复提示词和重复分析过程。"
)


class ClaudeCompactionError(RuntimeError):
    """Claude 会话压缩失败，当前复盘必须立即终止。"""


def _next_n_trading_days(trading_days, base_date, count):
    """Return the first `count` trading days strictly after base_date."""
    result = []
    for date in trading_days:
        if date > base_date:
            result.append(date)
            if len(result) >= count:
                break
    return result


def _format_ymd(ymd):
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _format_date_range(dates):
    if len(dates) >= 2:
        return f"{_format_ymd(dates[0])}~{_format_ymd(dates[-1])}"
    if len(dates) == 1:
        return _format_ymd(dates[0])
    return "?"


def get_predict_target_dates(now: datetime = None):
    """
    根据当前时间 + Tushare 交易日历计算预测目标日期。
    返回 dict:
      - label_1d:  "2026-06-05"         单日标签
      - label_3d:  "2026-06-05~2026-06-09"  3日范围
      - label_5d:  "2026-06-05~2026-06-11"  5日范围
      - ts_dates:  {"1d_start":"20260605", "1d_end":"20260605",
                     "3d_start":"20260605", "3d_end":"20260609", ...}
      - write_date: "20260605"           写入的日期文件夹
      - suffix: "" | "_nowind"           目录后缀
    """
    if now is None:
        now = datetime.now()

    pro = get_pro()
    today_str = now.strftime("%Y%m%d")
    hour = now.hour
    minute = now.minute
    current_time = hour * 60 + minute  # 分钟数

    # 获取交易日历（前后多取几天缓冲）
    cal_df = pro.trade_cal(
        exchange="SSE",
        start_date=(now - timedelta(days=TRADING_CALENDAR_DAYS)).strftime("%Y%m%d"),
        end_date=(now + timedelta(days=TRADING_CALENDAR_DAYS)).strftime("%Y%m%d"),
    )
    trading_days = sorted(cal_df[cal_df["is_open"] == 1]["cal_date"].tolist())

    is_trading_day = today_str in trading_days

    # 找最近的有效收盘数据交易日 (data_td)
    if is_trading_day and current_time >= MARKET_CLOSE_MINUTE:
        data_td = today_str
    else:
        past_td_today = [d for d in trading_days if d < today_str]
        data_td = past_td_today[-1] if past_td_today else today_str

    # 前一个交易日应为 data_td 的前一个交易日
    past_td_data = [d for d in trading_days if d < data_td]
    prev_td = past_td_data[-1] if past_td_data else data_td

    # 判断窗口类型
    suffix = "_nowind" if (
        is_trading_day
        and MARKET_CLOSE_MINUTE <= current_time < DAILY_DATA_READY_HOUR * 60
    ) else ""

    # 无论是盘中、盘后还是非交易日，预测目标的基准**永远**是“最新收盘数据对应的交易日(data_td)”
    next_tds = _next_n_trading_days(trading_days, data_td, 5)

    one_day = next_tds[:1]
    three_days = next_tds[:3]
    five_days = next_tds[:5]

    # 找今天之后的交易日 (next_td)
    future_td = [d for d in trading_days if d > today_str]
    next_td = future_td[0] if future_td else today_str

    return {
        "label_1d": _format_ymd(one_day[0]) if one_day else "?",
        "label_3d": _format_date_range(three_days),
        "label_5d": _format_date_range(five_days),
        "ts_dates": {
            "1d_start": one_day[0] if one_day else "?",
            "1d_end": one_day[0] if one_day else "?",
            "3d_start": three_days[0] if three_days else "?",
            "3d_end": three_days[-1] if three_days else "?",
            "5d_start": five_days[0] if five_days else "?",
            "5d_end": five_days[-1] if five_days else "?",
            "prev_td": _format_ymd(prev_td),
            "data_td": _format_ymd(data_td),
            "next_td": _format_ymd(next_td)
        },
        "write_date": today_str,
        "suffix": suffix,
        "is_trading_day":is_trading_day,
    }


# =============================================================================
# Realtime quote providers
# =============================================================================

def fetch_realtime_15min_kline(code: str, atype: str, num_bars: int = 80) -> str:
    """获取标的近期的 15分钟 K线数据"""
    symbol = _get_tencent_symbol(code, atype)

    url = f'https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},m15,,{num_bars}&_var=m15_today'
    try:
        res = requests.get(url, timeout=5).text
        json_str = res.split('=', 1)[1]
        data = json.loads(json_str)
        m15_kline = data['data'][symbol]['m15']

        header = "| 时间 | 开盘 | 收盘 | 最高 | 最低 | 成交量(手) |"
        sep = "|---|---|---|---|---|---|"
        lines = ["=== 近期 15分钟 K线数据 ===", header, sep]
        for item in m15_kline:
            t = item[0]
            formatted_time = f"{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
            lines.append(f"| {formatted_time} | {item[1]} | {item[2]} | {item[3]} | {item[4]} | {item[5]} |")

        return "\n".join(lines)
    except Exception as e:
        return f"=== 近期 15分钟 K线数据 ===\n(获取失败: {e})"

def fetch_realtime_quotes(tasks: list, method="tick") -> dict:
    """
    批量获取实时行情。
    tasks: list of (code, atype)
    method: "tick" (使用 tickflow, 限频, 支持分批) 或 "tencent" (腾讯财经)
    返回 { "000001": "=== 实时/最新盘面数据 ===\n..." } 的字典
    """
    results = {}
    if not tasks:
        return results

    warnings.filterwarnings("ignore", category=ResourceWarning)

    if method == "tick":
        '''https://docs.tickflow.org/'''
        try:
            from tickflow import TickFlow
            try:
                from config import TICKFLOW_API_KEY
                if TICKFLOW_API_KEY and "TICKFLOW_API_KEY" not in os.environ:
                    os.environ["TICKFLOW_API_KEY"] = TICKFLOW_API_KEY
            except ImportError:
                pass
            tf = TickFlow()
        except Exception as e:
            print(f"初始化 tickflow 失败 ({e})，降级使用 tencent 接口")
            method = "tencent"

    if method == "tick":
        batch_size = 5
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i+batch_size]
            ts_codes = []
            ts_code_map = {}
            for code, atype in batch:
                ts_c = _get_ts_code(code, atype)
                ts_codes.append(ts_c)
                ts_code_map[ts_c] = (code, atype)

            retry_count = 0
            while retry_count < 3:
                try:
                    quotes = tf.quotes.get(symbols=ts_codes)
                    for q in quotes:
                        sym = q.get('symbol', '')
                        ext = q.get('ext', {})
                        name = ext.get('name', '')
                        latest_price = q.get('last_price')
                        change_pct = ext.get('change_pct', 0) * 100
                        change_amount = ext.get('change_amount', 0)
                        amplitude = ext.get('amplitude', 0) * 100
                        turnover_rate = ext.get('turnover_rate', 0) * 100
                        open_p = q.get('open')
                        prev_close = q.get('prev_close')
                        high = q.get('high')
                        low = q.get('low')
                        volume = q.get('volume')
                        amount = q.get('amount')
                        ts = q.get('timestamp')
                        if ts:
                            dt = datetime.fromtimestamp(ts/1000)
                            update_time = dt.strftime("%Y%m%d%H%M%S")
                        else:
                            update_time = ""

                        res_str = (
                            f"=== 实时/最新盘面数据 ===\n"
                            f"代码: {sym}, 名称: {name}\n"
                            f"最新价: {latest_price}, 涨跌幅: {change_pct:.2f}%, 涨跌额: {change_amount:.2f}\n"
                            f"今开: {open_p}, 昨收: {prev_close}\n"
                            f"最高: {high}, 最低: {low}, 振幅: {amplitude:.2f}%, 换手率: {turnover_rate:.2f}%\n"
                            f"成交量: {volume}手, 成交额: {amount/1000:.2f}千元\n"
                            f"更新时间: {update_time}"
                        )

                        # 精准匹配，避免纯数字代码（如000001股票与指数）冲突
                        original_task = ts_code_map.get(sym)
                        if original_task:
                            results[original_task] = res_str

                    break  # 成功获取，跳出重试循环

                except Exception as e:
                    err_msg = str(e)
                    match = re.search(r"请\s*(\d+)ms\s*后重试", err_msg)
                    if match:
                        wait_ms = int(match.group(1))
                        wait_sec = wait_ms / 1000.0 + 0.1 # 多等 0.1 秒保底
                        print(f"Tickflow 触发限频，等待 {wait_sec:.2f} 秒后第 {retry_count + 1} 次重试...")
                        time.sleep(wait_sec)
                        retry_count += 1
                    else:
                        print(f"Tickflow 获取失败: {err_msg}")
                        break

    elif method == "tencent":
        batch_size = 100
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i+batch_size]
            symbols = []
            sym_map = {}
            for code, atype in batch:
                symbol = _get_tencent_symbol(code, atype)
                symbols.append(symbol)
                sym_map[symbol] = (code, atype)

            url = f"http://qt.gtimg.cn/q={','.join(symbols)}"

            retry_count = 0
            while retry_count < 3:
                try:
                    resp = requests.get(url, headers={'Referer': 'http://finance.qq.com'}, timeout=10)
                    resp.encoding = 'gbk'
                    if resp.status_code == 200:
                        lines = resp.text.split(';')
                        for line in lines:
                            if not line.strip(): continue
                            if '="' not in line: continue
                            prefix, data_str = line.split('="')
                            q_symbol = prefix.strip().replace("v_", "")
                            data_str = data_str.strip('"')
                            fields = data_str.split('~')
                            if len(fields) >= 45:
                                name = fields[1]
                                pure_code = fields[2]
                                latest_price = fields[3]
                                prev_close = fields[4]
                                open_p = fields[5]
                                change_amount = fields[31]
                                change_pct = fields[32]
                                high = fields[33]
                                low = fields[34]
                                try: amplitude = f"{float(fields[43]):.2f}"
                                except: amplitude = fields[43]

                                try: turnover_rate = f"{float(fields[38]):.2f}"
                                except: turnover_rate = fields[38]

                                try: amount = f"{float(fields[37]) * 10:.2f}"
                                except: amount = fields[37]

                                try: volume = f"{int(fields[36])}"
                                except: volume = fields[36]

                                update_time = fields[30]

                                res_str = (
                                    f"=== 实时/最新盘面数据 ===\n"
                                    f"代码: {pure_code}, 名称: {name}\n"
                                    f"最新价: {latest_price}, 涨跌幅: {change_pct}%, 涨跌额: {change_amount}\n"
                                    f"今开: {open_p}, 昨收: {prev_close}\n"
                                    f"最高: {high}, 最低: {low}, 振幅: {amplitude}%, 换手率: {turnover_rate}%\n"
                                    f"成交量: {volume}手, 成交额: {amount}千元\n"
                                    f"更新时间: {update_time}"
                                )

                                original_task = sym_map.get(q_symbol)
                                if original_task:
                                    results[original_task] = res_str
                        break  # 成功，跳出重试循环
                    else:
                        print(f"腾讯行情获取失败: HTTP {resp.status_code}")
                        retry_count += 1
                        time.sleep(1)
                except Exception as e:
                    print(f"腾讯行情获取失败 (重试 {retry_count+1}/3): {e}")
                    retry_count += 1
                    time.sleep(1)

    return results

# =============================================================================
# Historical prediction reflection
# =============================================================================


def create_news_sandbox(
    work_dir: str,
    news_files: list,
) -> tuple[list, dict]:
    """把已收集的新闻、文章和图片复制到沙盒，并返回新旧路径映射。"""
    sandbox_dir = os.path.abspath(os.path.join(work_dir, "news_sandbox"))
    os.makedirs(sandbox_dir, exist_ok=True)

    source_files = []
    seen = set()
    for source_path in news_files:
        source_abs = os.path.abspath(source_path)
        source_key = os.path.normcase(source_abs)
        if source_key in seen or not os.path.isfile(source_abs):
            continue
        seen.add(source_key)
        source_files.append(source_abs)

    if not source_files:
        return [], {}

    source_root = os.path.commonpath(source_files)
    if os.path.isfile(source_root):
        source_root = os.path.dirname(source_root)

    copied_files = []
    path_map = {}
    try:
        for source_abs in source_files:
            relative_path = os.path.relpath(source_abs, source_root)
            destination = os.path.abspath(os.path.join(sandbox_dir, relative_path))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source_abs, destination)
            copied_files.append(destination)
            path_map[source_abs] = destination
    except Exception:
        shutil.rmtree(sandbox_dir, ignore_errors=True)
        raise

    return copied_files, path_map

def _replace_reflected_marker(content: str, reflected_dates: list) -> str:
    clean_content = re.sub(
        r'<!--\s*reflected:[^>]*-->',
        '',
        content,
        flags=re.IGNORECASE,
    ).strip()
    marker = f"<!-- reflected: {','.join(reflected_dates)} -->"
    return f"{clean_content}\n\n{marker}\n"


def _parse_reflection_analyses(response_text, tasks_metadata):
    """按系统任务顺序解析复盘 XML，任务 ID 不依赖模型输出。"""
    analysis_blocks = re.findall(
        r'<task_analysis>(.*?)</task_analysis>',
        response_text,
        re.DOTALL,
    )
    if len(analysis_blocks) != len(tasks_metadata):
        raise ValueError(
            "大模型返回的复盘任务数量不一致："
            f"期望 {len(tasks_metadata)} 个，实际 {len(analysis_blocks)} 个"
        )

    parsed_analyses = {}
    for task, block in zip(tasks_metadata, analysis_blocks):
        task_id = task["id"]
        ref_m = re.search(r'<reflection>(.*?)</reflection>', block, re.DOTALL)
        p1_m = re.search(r'<plan_1>(.*?)</plan_1>', block, re.DOTALL)
        p2_m = re.search(r'<plan_2>(.*?)</plan_2>', block, re.DOTALL)
        p3_m = re.search(r'<plan_3>(.*?)</plan_3>', block, re.DOTALL)
        parsed_analyses[task_id] = {
            'reflection': (
                ref_m.group(1).strip()
                if ref_m else "未提取到复盘分析"
            ),
            'plan_1': p1_m.group(1).strip() if p1_m else "",
            'plan_2': p2_m.group(1).strip() if p2_m else "",
            'plan_3': p3_m.group(1).strip() if p3_m else "",
        }

    return parsed_analyses


def _build_reflection_analysis_skeleton(
    tasks_metadata,
    reference_kb_filename,
    new_kb_filename,
):
    """由代码生成固定任务ID和字段，模型只负责填写字段内容。"""
    payload = {
        "reference_kb": reference_kb_filename,
        "new_kb": new_kb_filename,
        "knowledge_base_change": {},
    }
    for task in tasks_metadata:
        if task["type"] == "1d":
            payload[task["id"]] = {
                "reflection": "",
                "plan_1": "",
                "plan_2": "",
                "plan_3": "",
            }
        else:
            payload[task["id"]] = {
                "reflection": "",
                "plan_1": "",
            }
    return payload


def _validate_reflection_work_files(
    analysis_json_path,
    knowledge_base_path,
    tasks_metadata,
    reference_kb_filename,
    new_kb_filename,
):
    """严格校验模型直接编辑的复盘JSON和完整知识库工作文件。"""
    errors = []
    try:
        with open(analysis_json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as error:
        raise ValueError(f"复盘JSON无法解析: {error}") from error

    if not isinstance(payload, dict):
        raise ValueError("复盘JSON顶层必须是对象")

    expected_task_ids = [task["id"] for task in tasks_metadata]
    metadata_keys = {
        "reference_kb",
        "new_kb",
        "knowledge_base_change",
    }
    actual_task_ids = [
        key for key in payload
        if key not in metadata_keys
    ]
    missing_task_ids = [
        task_id for task_id in expected_task_ids
        if task_id not in actual_task_ids
    ]
    extra_task_ids = [
        task_id for task_id in actual_task_ids
        if task_id not in expected_task_ids
    ]
    if missing_task_ids:
        errors.append("缺少任务ID: " + "；".join(missing_task_ids))
    if extra_task_ids:
        errors.append("出现未知任务ID: " + "；".join(extra_task_ids))

    if payload.get("reference_kb") != reference_kb_filename:
        errors.append("reference_kb 被修改")
    if payload.get("new_kb") != new_kb_filename:
        errors.append("new_kb 被修改")
    if payload.get("knowledge_base_change") != {}:
        errors.append("knowledge_base_change 必须保留为空对象，由代码生成Diff")
    if (
        not missing_task_ids
        and not extra_task_ids
        and actual_task_ids != expected_task_ids
    ):
        errors.append("任务顺序被修改")

    placeholder_fragments = (
        "未提取到",
        "同上",
        "待补充",
        "占位",
    )
    placeholder_values = {
        "已分析",
        "已完成",
        "已完成修改",
    }
    parsed_analyses = {}
    for task in tasks_metadata:
        task_id = task["id"]
        analysis = payload.get(task_id)
        if not isinstance(analysis, dict):
            errors.append(f"{task_id}: 任务内容必须是对象")
            continue

        expected_fields = (
            {"reflection", "plan_1", "plan_2", "plan_3"}
            if task["type"] == "1d"
            else {"reflection", "plan_1"}
        )
        actual_fields = set(analysis)
        missing_fields = sorted(expected_fields - actual_fields)
        extra_fields = sorted(actual_fields - expected_fields)
        if missing_fields:
            errors.append(
                f"{task_id}: 缺少字段 {', '.join(missing_fields)}"
            )
        if extra_fields:
            errors.append(
                f"{task_id}: 不允许出现字段 {', '.join(extra_fields)}"
            )

        for field_name in sorted(expected_fields):
            value = analysis.get(field_name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{task_id}: {field_name} 必须填写非空正文")
                continue
            clean_value = value.strip()
            if (
                clean_value in placeholder_values
                or any(
                    word in clean_value
                    for word in placeholder_fragments
                )
            ):
                errors.append(
                    f"{task_id}: {field_name} 包含占位内容"
                )

        if not missing_fields and not extra_fields:
            parsed_analyses[task_id] = {
                field_name: analysis[field_name].strip()
                for field_name in analysis
            }

    if not os.path.isfile(knowledge_base_path):
        errors.append("未找到知识库Markdown工作文件")
        edited_knowledge = ""
    else:
        with open(knowledge_base_path, "r", encoding="utf-8") as f:
            edited_knowledge = f.read()
        clean_knowledge = re.sub(
            r'<!--\s*reflected:[^>]*-->',
            '',
            edited_knowledge,
            flags=re.IGNORECASE,
        ).strip()
        if len(clean_knowledge) < 100:
            errors.append("知识库Markdown正文过短或为空")
        if len(clean_knowledge) > 50000:
            errors.append(
                f"知识库Markdown超过50000字符，当前为{len(clean_knowledge)}字符"
            )

    if errors:
        raise ValueError("\n".join(f"- {error}" for error in errors))
    return parsed_analyses, edited_knowledge


def _build_knowledge_base_diff(
    old_knowledge,
    new_knowledge,
    reference_kb_filename,
    new_kb_filename,
):
    """忽略复盘日期标记，机械生成新旧知识库的统一Diff。"""
    marker_pattern = r'<!--\s*reflected:[^>]*-->'
    old_clean = re.sub(
        marker_pattern,
        '',
        old_knowledge or "",
        flags=re.IGNORECASE,
    ).strip()
    new_clean = re.sub(
        marker_pattern,
        '',
        new_knowledge or "",
        flags=re.IGNORECASE,
    ).strip()
    diff_lines = list(
        difflib.unified_diff(
            old_clean.splitlines(),
            new_clean.splitlines(),
            fromfile=reference_kb_filename or "空知识库",
            tofile=new_kb_filename,
            lineterm="",
        )
    )
    if not diff_lines:
        return "知识库正文无变化。"
    return "```diff\n" + "\n".join(diff_lines) + "\n```"


def _initialize_profile_knowledge_base(experience_dir: str) -> str:
    """在新预测档案中仅继承一次旧档案的最新复盘知识库。"""
    current_files = glob.glob(
        os.path.join(experience_dir, "predict_ex_*.md")
    )
    if current_files:
        return sorted(current_files)[-1]

    source_profile = INITIAL_KB_PROFILE_NAME.strip()
    if not source_profile or source_profile == PREDICT_PROFILE_NAME:
        return ""

    source_experience_dir = os.path.join(
        PREDICT_DIR,
        source_profile,
        "experience",
    )
    source_files = glob.glob(
        os.path.join(source_experience_dir, "predict_ex_*.md")
    )
    if not source_files:
        print(
            "[!] 未找到可继承的复盘知识库："
            f"predict/{source_profile}/experience/predict_ex_*.md"
        )
        return ""

    source_file = sorted(source_files)[-1]
    destination = os.path.join(
        experience_dir,
        os.path.basename(source_file),
    )
    shutil.copy2(source_file, destination)
    print(
        "[+] 新预测档案已继承复盘知识库："
        f"{source_profile} -> {PREDICT_PROFILE_NAME}/"
        f"{os.path.basename(destination)}"
    )
    return destination


def _run_reflection(run_date: str, run_time: str) -> tuple[str, str]:
    """
    复盘模块：从上次已复盘交易日之后开始，补齐截至最新交易日 T 的历史预测。
    - 单日期 D: D 在窗口内 → 纳入复盘
    - 范围 D~E: E == T → 纳入复盘（区间预测完整到期）
    - _nowind/ 目录的预测不进入复盘
    """
    pro = get_pro()

    @lru_cache(maxsize=128)
    def get_suspended_stocks_cached(trade_date: str):
        """获取历史某日停牌的股票列表。"""
        try:
            df = pro.suspend_d(trade_date=trade_date, suspend_type='S')
            return [] if df is None or df.empty else df['ts_code'].tolist()
        except Exception as e:
            print(f"获取 {trade_date} 停牌股票失败: {e}")
            return []

    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y%m%d")
    model_dir = os.path.join(PREDICT_DIR, PREDICT_PROFILE_NAME)
    experience_dir = os.path.join(model_dir, "experience")
    os.makedirs(experience_dir, exist_ok=True)

    old_ex = ""
    latest_ex_filename = "无"

    # 1. 先读取最新知识库标记，用它决定交易日历需要回溯多远。
    _initialize_profile_knowledge_base(experience_dir)
    ex_files = glob.glob(os.path.join(experience_dir, "predict_ex_*.md"))
    latest_ex_file = sorted(ex_files)[-1] if ex_files else None
    is_first_reflection = latest_ex_file is None
    if latest_ex_file:
        latest_ex_filename = os.path.basename(latest_ex_file)

    last_reflected_T = ""
    if latest_ex_file:
        with open(latest_ex_file, 'r', encoding='utf-8') as f:
            old_ex = f.read()
        marker_match = re.search(r'<!--\s*reflected:\s*([\d,]+)\s*-->', old_ex)
        if marker_match:
            last_reflected_T = marker_match.group(1).split(",")[-1].strip()
            old_ex = re.sub(r'<!--\s*reflected:.*?-->', '', old_ex).strip()

    prediction_date_dirs = sorted(
        entry
        for entry in os.listdir(model_dir)
        if entry.isdigit() and os.path.isdir(os.path.join(model_dir, entry))
    )
    earliest_prediction_date = prediction_date_dirs[0] if prediction_date_dirs else ""
    calendar_anchor = last_reflected_T or earliest_prediction_date or today_str
    try:
        calendar_anchor_dt = datetime.strptime(calendar_anchor, "%Y%m%d")
    except ValueError:
        calendar_anchor_dt = now_dt
        last_reflected_T = ""

    calendar_start = (
        calendar_anchor_dt - timedelta(days=TRADING_CALENDAR_DAYS)
    ).strftime("%Y%m%d")

    # 2. 获取从历史锚点到今天的完整交易日历，并确定最新可验证交易日 T。
    cal_df = pro.trade_cal(
        exchange="SSE",
        start_date=calendar_start,
        end_date=today_str,
    )
    trading_days = sorted(cal_df[cal_df["is_open"] == 1]["cal_date"].tolist())
    # T = 最近一个有收盘数据的交易日
    T = ""
    can_check_today = now_dt.hour >= DAILY_DATA_READY_HOUR
    for d in reversed(trading_days):
        if d > today_str:
            continue
        if d == today_str and not can_check_today:
            continue
        probe = pro.index_daily(ts_code="000001.SH", trade_date=d)
        if probe is not None and not probe.empty:
            T = d
            break
    if not T:
        print("[*] 找不到可验证的交易日收盘数据，跳过复盘")
        return old_ex, "无"

    # 3. 生成全部未处理交易日。首次复盘则从最早预测目录开始。
    if last_reflected_T:
        pending_reflect_Ts = [d for d in trading_days if last_reflected_T < d <= T]
    elif earliest_prediction_date:
        pending_reflect_Ts = [d for d in trading_days if earliest_prediction_date <= d <= T]
    else:
        pending_reflect_Ts = [T]
    pending_reflect_set = set(pending_reflect_Ts)

    if not pending_reflect_Ts:
        print(f"[*] 当前可验证最新交易日(T={T})已复盘过(盘中/周末无新数据)，跳过复盘")
        return old_ex, latest_ex_filename

    # 复盘窗口: [T-4, T]（5 个交易日）
    t_idx = trading_days.index(T)
    first_pending_idx = trading_days.index(pending_reflect_Ts[0])
    review_window = trading_days[max(0, first_pending_idx - 4): t_idx + 1]

    has_printed_review_header = False
    review_context = ""

    # 4. 收集所有候选预测（按 标的 + 目标预测日 分组，同日取最新）
    candidates = {}  # key=(atype, code, predict_ymd) -> (file_dt, date_folder, ts_entry, content)
    # 5日的收盘数据则需要再往前1日的预测数据
    earliest_dir_idx = max(0, first_pending_idx - 6)
    data_dir = [
        _
        for _ in sorted(os.listdir(model_dir), reverse=True)
        if _.isdigit() and trading_days[earliest_dir_idx] <= _ <= trading_days[t_idx]
    ]
    for date_folder in data_dir:


        date_dir_path = os.path.join(model_dir, date_folder)

        for atype in ("stk", "etf", "idx"):
            type_dir = os.path.join(date_dir_path, atype)
            if not os.path.isdir(type_dir):
                continue
            for code in os.listdir(type_dir):
                code_dir = os.path.join(type_dir, code)
                if not os.path.isdir(code_dir):
                    continue

                # 遍历该标的下的所有时间戳文件夹
                for ts_entry in os.listdir(code_dir):
                    # 排除 15:00-19:00 产出的 _nowind 文件
                    if ts_entry.endswith("_nowind"):
                        continue

                    ts_dir = os.path.join(code_dir, ts_entry)
                    if not os.path.isdir(ts_dir):
                        continue

                    try:
                        # 解析预测文件的实际生成时间
                        file_dt = datetime.strptime(f"{date_folder}{ts_entry[:6]}", "%Y%m%d%H%M%S")
                    except ValueError:
                        continue


                    md_files = glob.glob(os.path.join(ts_dir, f"{code}.md"))
                    if not md_files:
                        continue
                    with open(md_files[0], 'r', encoding='utf-8') as f:
                        content = f.read()

                    # 排除未成功完成预测（如 timeout, error）的文件
                    if "- **状态**: success" not in content:
                        continue

                    # 提取该预测的目标日期 (predict_ymd)
                    r1_m = re.search(r'【(\d{4}-\d{2}-\d{2})\s*涨跌幅】', content)
                    if not r1_m:
                        raise ValueError(f"预测文件解析失败, 缺少涨跌幅区间关键格式: {md_files[0]}")
                    predict_ymd = r1_m.group(1).replace("-", "")
                    if predict_ymd not in review_window:
                        continue

                    # 相同目标日只取最新一条
                    key = (atype, code, predict_ymd)

                    if key not in candidates or file_dt > candidates[key][0]:
                        candidates[key] = (file_dt, date_folder, ts_entry, content)

    # 5. 逐个处理候选
    printed_windows = set()
    reflection_news_files = []
    num_reflection_tags = 0
    tasks_metadata = []
    for (atype, code, predict_ymd), (_, date_folder, ts_entry, content) in candidates.items():
        dir_m = re.search(r'【(\d{4}-\d{2}-\d{2})\s*方向】\s*([涨跌平]+)(?:.*?【信心】\s*(\d+))?', content)
        r1_m = re.search(r'【(\d{4}-\d{2}-\d{2})\s*涨跌幅】\s*([^【\n]+)(?:.*?【信心】\s*(\d+))?', content)
        ranges = re.findall(r'【(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})\s*涨跌幅】\s*([^【\n]+)(?:.*?【信心】\s*(\d+))?', content)
        class MockMatch:
            def __init__(self, m): self.m = m
            def group(self, i): return self.m[i-1]
        r3_m = MockMatch(ranges[0]) if len(ranges) >= 1 else None
        r5_m = MockMatch(ranges[1]) if len(ranges) >= 2 else None

        # 判断是否纳入复盘：只有当 1日、3日 或 5日 的结束日期刚好等于 T 时，才在今天触发复盘
        should_review = False
        end_ymd_3d = r3_m.group(2).replace("-", "") if r3_m else ""
        end_ymd_5d = r5_m.group(2).replace("-", "") if r5_m else ""

        if predict_ymd in pending_reflect_set:
            should_review = True
        if end_ymd_3d in pending_reflect_set:
            should_review = True
        if end_ymd_5d in pending_reflect_set:
            should_review = True

        if not should_review:
            continue

        if not dir_m:
            raise ValueError(f"预测文件解析失败, 缺少方向字段: {date_folder}/{ts_entry}")

        if not has_printed_review_header:
            print(
                f"\n[*] 开始复盘: T={_format_ymd(T)}, "
                f"待补={list(_format_ymd(d) for d in pending_reflect_Ts)}, "
                f"窗口={list(_format_ymd(d) for d in sorted(review_window))}"
            )
            has_printed_review_header = True

        if predict_ymd not in printed_windows:
            print(f"[*] 锁定目标预测日: {predict_ymd}")
            printed_windows.add(predict_ymd)

        print(f"    -> 提取标的 {code} 的预测文件 ({date_folder}/{ts_entry})")

        direction = dir_m.group(2).strip()
        conf_dir = dir_m.group(3) if dir_m.group(3) else "未填"

        # 用预测日期查 tushare 获取实际行情
        full_ts_code = _get_ts_code(code, atype)

        if atype == 'etf':
            df = pro.fund_daily(ts_code=full_ts_code, start_date=predict_ymd, end_date=today_str)
        elif atype == 'idx':
            df = pro.index_daily(ts_code=full_ts_code, start_date=predict_ymd, end_date=today_str)
        else:
            df = pro.daily(ts_code=full_ts_code, start_date=predict_ymd, end_date=today_str)

        actual_rows = {}
        base_price = None
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').reset_index(drop=True)
            actual_rows = {
                str(row['trade_date']): row
                for _, row in df.iterrows()
            }
            # 首个实际交易日的 pre_close 是整个预测区间的比较基准。
            base_price = df.iloc[0].get('pre_close')

        try:
            base_price = float(base_price)
            base_price_valid = base_price == base_price and base_price > 0
        except (TypeError, ValueError):
            base_price_valid = False

        expected_dates = set()
        if predict_ymd <= T:
            expected_dates.add(predict_ymd)
        for range_match in (r3_m, r5_m):
            if not range_match:
                continue
            range_start = range_match.group(1).replace("-", "")
            range_end = range_match.group(2).replace("-", "")
            expected_dates.update(
                date for date in trading_days
                if range_start <= date <= min(range_end, T)
            )

        missing_dates = sorted(expected_dates - set(actual_rows))
        suspended_dates = []
        missing_data_dates = []
        for missing_date in missing_dates:
            if (
                atype == 'stk'
                and full_ts_code in get_suspended_stocks_cached(missing_date)
            ):
                suspended_dates.append(missing_date)
            else:
                missing_data_dates.append(missing_date)

        def missing_date_result(target_date):
            if not target_date or target_date > T:
                return "时间未到"
            if target_date in suspended_dates:
                return "停牌（无当日收盘数据）"
            if target_date in missing_data_dates:
                return "行情数据缺失（未查询到停牌记录）"
            return "基准价格缺失"

        def get_return_by_date(target_date):
            row = actual_rows.get(target_date)
            if row is None or not base_price_valid:
                return missing_date_result(target_date)
            try:
                close_price = float(row['close'])
                if close_price != close_price:
                    return "收盘价格缺失"
                return f"{(close_price / base_price - 1) * 100:+.2f}%"
            except (KeyError, TypeError, ValueError):
                return "收盘价格缺失"

        act_1d = get_return_by_date(predict_ymd)
        act_3d = get_return_by_date(end_ymd_3d)
        act_5d = get_return_by_date(end_ymd_5d)

        first_day_row = actual_rows.get(predict_ymd)
        if first_day_row is None or not base_price_valid:
            correct = f"[?]{missing_date_result(predict_ymd)}"
        else:
            try:
                first_day_close = float(first_day_row['close'])
                if first_day_close != first_day_close:
                    raise ValueError("close is NaN")
                actual_pct_chg = (first_day_close / base_price - 1) * 100
                actual_dir = "涨" if actual_pct_chg > 0 else ("跌" if actual_pct_chg < 0 else "平")
                correct = "[OK]" if actual_dir in direction else "[X]方向"
            except (KeyError, TypeError, ValueError):
                correct = "[?]收盘价格缺失"

        period_status_parts = []
        if suspended_dates:
            period_status_parts.append(
                "真实停牌日期: " + ",".join(_format_ymd(date) for date in suspended_dates)
            )
        if missing_data_dates:
            period_status_parts.append(
                "行情缺失但未查到停牌记录: "
                + ",".join(_format_ymd(date) for date in missing_data_dates)
            )
        period_data_status = "；".join(period_status_parts) if period_status_parts else "无"

        p_1d = r1_m.group(2).strip() if r1_m else "未填"
        p_3d = r3_m.group(3).strip() if r3_m else "未填"
        p_5d = r5_m.group(3).strip() if r5_m else "未填"
        conf_1d = r1_m.group(3) if r1_m and r1_m.group(3) else "未填"
        conf_3d = r3_m.group(4) if r3_m and r3_m.group(4) else "未填"
        conf_5d = r5_m.group(4) if r5_m and r5_m.group(4) else "未填"

        tools_m = re.search(r'- \*\*调用工具记录\*\*: (.*?)(?=- \*\*可读取新闻路径\*\*|$)', content, re.DOTALL)
        news_m = re.search(r'- \*\*可读取新闻路径\*\*: (.*?)(?=### AI 模型结论：|$)', content, re.DOTALL)
        tools_str = tools_m.group(1).strip() if tools_m else "无"
        news_str = news_m.group(1).strip() if news_m else "无"

        formatted_news_str = "无"
        if news_str != "无":
            paths = news_str.split(" | ") if " | " in news_str else news_str.split("、")
            valid_paths = []
            for p in paths:
                if p.strip():
                    abs_p = os.path.abspath(os.path.join(BASE_DIR, p.strip()))
                    if abs_p not in reflection_news_files:
                        reflection_news_files.append(abs_p)
                    valid_paths.append(abs_p)
            if valid_paths:
                lines = "\n".join(f"    {i}. {p}" for i, p in enumerate(valid_paths, 1))
                formatted_news_str = f"\n{lines}"

        label_1d = r1_m.group(1) if r1_m else "?"
        label_3d = f"{r3_m.group(1)}~{r3_m.group(2)}" if r3_m else "?"
        label_5d = f"{r5_m.group(1)}~{r5_m.group(2)}" if r5_m else "?"

        ai_conclusion_m = re.search(r'### AI 模型结论：\n```text\n(.*?)```', content, re.DOTALL)
        ai_reasoning = ai_conclusion_m.group(1).strip() if ai_conclusion_m else "未提取到推演逻辑"

        logic_1d = re.search(r'【1日逻辑理由】(.*?)(?=【3日逻辑理由】|【5日逻辑理由】|--- (?:实战交易计划|宏观/大盘应对参考) ---|</result>|$)', ai_reasoning, re.DOTALL)
        logic_1d_str = logic_1d.group(1).strip() if logic_1d else "未提取到"

        logic_3d = re.search(r'【3日逻辑理由】(.*?)(?=【5日逻辑理由】|--- (?:实战交易计划|宏观/大盘应对参考) ---|</result>|$)', ai_reasoning, re.DOTALL)
        logic_3d_str = logic_3d.group(1).strip() if logic_3d else "未提取到"

        logic_5d = re.search(r'【5日逻辑理由】(.*?)(?=--- (?:实战交易计划|宏观/大盘应对参考) ---|</result>|$)', ai_reasoning, re.DOTALL)
        logic_5d_str = logic_5d.group(1).strip() if logic_5d else "未提取到"

        plan_match = re.search(r'(--- (?:实战交易计划|宏观/大盘应对参考) ---.*)', ai_reasoning, re.DOTALL)
        plan_str = plan_match.group(1).strip() if plan_match else "未提取到"

        fail_cond = re.search(r'【(逻辑失效价位|核心支撑/阻力)】(.*?)(?=【|$)', plan_str, re.DOTALL)
        fail_cond_str = f"【{fail_cond.group(1)}】{fail_cond.group(2).strip()}" if fail_cond else "未提取到共享失效条件"

        # For 1-day task
        if predict_ymd in pending_reflect_set:
            num_reflection_tags += 1
            task_id = f"1日任务: {atype} {code} ({predict_ymd})"
            task_ctx = f"==== {task_id} ====\n"
            task_ctx += f"  类型: {atype}\n"
            task_ctx += f"  预测时间: {date_folder} {ts_entry}\n"
            task_ctx += f"  调用工具记录: {tools_str}\n"
            task_ctx += f"  使用的新闻路径: {formatted_news_str}\n"
            task_ctx += f"  预测区间停牌/数据缺失核验: {period_data_status}\n"
            task_ctx += f"  ==== 预测结果对照 ====\n"
            task_ctx += f"  {label_1d} 方向: {direction} (信心 {conf_dir}) | 实际: {correct}\n"
            task_ctx += f"  {label_1d} 涨跌幅: {p_1d} (信心 {conf_1d}) | 实际: {act_1d}\n"
            task_ctx += f"  ==== AI 原始推演逻辑 ====\n"
            task_ctx += f"  【1日逻辑理由】 {logic_1d_str}\n\n"
            task_ctx += f"  {plan_str}\n\n"
            review_context += task_ctx
            tasks_metadata.append({"id": task_id, "type": "1d", "atype": atype, "ctx": task_ctx})

        # For 3-day task
        if end_ymd_3d and end_ymd_3d in pending_reflect_set:
            num_reflection_tags += 1
            task_id = f"3日任务: {atype} {code} ({end_ymd_3d})"
            task_ctx = f"==== {task_id} ====\n"
            task_ctx += f"  类型: {atype}\n"
            task_ctx += f"  预测时间: {date_folder} {ts_entry}\n"
            task_ctx += f"  调用工具记录: {tools_str}\n"
            task_ctx += f"  使用的新闻路径: {formatted_news_str}\n"
            task_ctx += f"  预测区间停牌/数据缺失核验: {period_data_status}\n"
            task_ctx += f"  ==== 预测结果对照 ====\n"
            task_ctx += f"  {label_3d} 涨跌幅: {p_3d} (信心 {conf_3d}) | 实际: {act_3d}\n"
            task_ctx += f"  ==== AI 原始推演逻辑 ====\n"
            task_ctx += f"  【3日逻辑理由】 {logic_3d_str}\n"
            task_ctx += f"  {fail_cond_str}\n\n"
            review_context += task_ctx
            tasks_metadata.append({"id": task_id, "type": "3d", "atype": atype, "ctx": task_ctx})

        # For 5-day task
        if end_ymd_5d and end_ymd_5d in pending_reflect_set:
            num_reflection_tags += 1
            task_id = f"5日任务: {atype} {code} ({end_ymd_5d})"
            task_ctx = f"==== {task_id} ====\n"
            task_ctx += f"  类型: {atype}\n"
            task_ctx += f"  预测时间: {date_folder} {ts_entry}\n"
            task_ctx += f"  调用工具记录: {tools_str}\n"
            task_ctx += f"  使用的新闻路径: {formatted_news_str}\n"
            task_ctx += f"  预测区间停牌/数据缺失核验: {period_data_status}\n"
            task_ctx += f"  ==== 预测结果对照 ====\n"
            task_ctx += f"  {label_5d} 涨跌幅: {p_5d} (信心 {conf_5d}) | 实际: {act_5d}\n"
            task_ctx += f"  ==== AI 原始推演逻辑 ====\n"
            task_ctx += f"  【5日逻辑理由】 {logic_5d_str}\n"
            task_ctx += f"  {fail_cond_str}\n\n"
            review_context += task_ctx
            tasks_metadata.append({"id": task_id, "type": "5d", "atype": atype, "ctx": task_ctx})

    if not review_context:
        print("未发现需要复盘的历史开仓记录。")
        return old_ex, latest_ex_filename

    # 调用 Claude 进行持久化复盘
    print("[*] 触发知识沉淀，Claude 正在深度复盘...")

    claude_session_file = os.path.join(experience_dir, "claude_reflection.md")
    claude_session_id = ""
    if os.path.exists(claude_session_file):
        with open(claude_session_file, 'r', encoding='utf-8') as f:
            match = re.search(r'SessionID:\s*(.+)', f.read())
            if match:
                claude_session_id = match.group(1).strip()

    len_old_ex = len(old_ex)
    if is_first_reflection:
        compression_rule = "本次为首次复盘，不存在上一版知识库；必须从零生成结构完整、可独立使用的新知识库。"
    elif len_old_ex < 25000:
        compression_rule = f"当前上一版知识库字数为 {len_old_ex} 字符 (< 25000)，直接完整抄写即可，不需要压缩。"
    elif len_old_ex <= 50000:
        compression_rule = f"当前上一版知识库字数为 {len_old_ex} 字符 (介于 25000 到 50000 之间)，无需高度精炼压缩，请尽量保留原始细节。"
    else:
        compression_rule = f"当前上一版知识库字数为 {len_old_ex} 字符 (> 50000)，请务必进行高度精炼压缩，主动删减冗余长篇大论，严格控制全文规模。"

    # 为本次复盘建立独立的可审计工作区。
    reflection_name = f"predict_ex_{run_date}_{run_time}"
    reflection_run_dir = os.path.join(experience_dir, reflection_name)
    reflection_tools_dir = os.path.join(reflection_run_dir, "tools")
    os.makedirs(reflection_tools_dir, exist_ok=True)

    working_ex_filename = f"{reflection_name}.md"
    working_ex_path = os.path.join(reflection_tools_dir, working_ex_filename)
    reference_kb_filename = (
        "" if is_first_reflection else latest_ex_filename
    )
    if latest_ex_file and os.path.exists(latest_ex_file):
        shutil.copy2(latest_ex_file, working_ex_path)
    else:
        with open(working_ex_path, "w", encoding="utf-8") as f:
            f.write("# 股票短期预测 复盘知识库\n")

    working_analysis_path = os.path.join(
        reflection_tools_dir,
        "predict_analysis.json",
    )
    analysis_skeleton = _build_reflection_analysis_skeleton(
        tasks_metadata,
        reference_kb_filename,
        working_ex_filename,
    )
    with open(working_analysis_path, "w", encoding="utf-8") as f:
        json.dump(
            analysis_skeleton,
            f,
            ensure_ascii=False,
            indent=2,
        )

    sandbox_news_files, sandbox_path_map = create_news_sandbox(
        reflection_tools_dir,
        reflection_news_files,
    )
    for original_path, sandbox_path in sandbox_path_map.items():
        review_context = review_context.replace(original_path, sandbox_path)

    if is_first_reflection:
        knowledge_operation = (
            "这是首次复盘。请从零把结构完整、可以独立使用的知识库全文"
            "写入知识库Markdown工作文件。"
        )
    else:
        knowledge_operation = (
            "知识库Markdown工作文件已经是上一版完整副本。请直接在该文件"
            "上增删改规则；保留仍有效规则的完整正文，禁止只写摘要或Diff，"
            "禁止重新排版或改写未变化段落。"
        )

    field_guidance_parts = []
    for task in tasks_metadata:
        task_id = task["id"]
        task_type = task["type"]
        asset_type = task["atype"]
        plan_1_name = (
            "核心支撑/阻力"
            if asset_type == "idx"
            else "逻辑失效价位"
        )
        if task_type == "1d":
            plan_2_name = (
                "盘面验证信号"
                if asset_type == "idx"
                else "盘中验证信号"
            )
            plan_3_name = (
                "系统性风险评估"
                if asset_type == "idx"
                else "盈亏比与仓位"
            )
            field_guidance_parts.append(
                f"""- {task_id}
  - reflection：填写【1日方向、涨跌幅的复盘分析】。必须对照实际结果，
    说明预测正确或错误的根因、数据证据和规则教训。
  - plan_1：填写原交易计划中【{plan_1_name}的复盘分析】。只分析该条件
    是否在实际盘面触发、何时触发、设置是否合理，以及触发后应采取什么动作。
  - plan_2：填写原交易计划中【{plan_2_name}的复盘分析】。逐项核对原计划
    的验证信号实际是否出现、是否及时确认或否定预测，以及执行是否合理。
  - plan_3：填写原交易计划中【{plan_3_name}的复盘分析】。结合实际最大
    有利/不利波动，判断原盈亏比、仓位、止损和风险控制是否合理并给出改进。"""
            )
        else:
            duration_name = task_type.replace("d", "日")
            field_guidance_parts.append(
                f"""- {task_id}
  - reflection：填写【{duration_name}涨跌幅的复盘分析】。必须对照完整窗口
    的实际涨跌幅和路径，说明区间正确或错误的根因、数据证据和规则教训。
  - plan_1：填写原交易计划中【{plan_1_name}的复盘分析】。只分析该条件
    是否在{duration_name}窗口内触发、触发后的影响，以及原设置是否合理。"""
            )
    field_guidance = "\n".join(field_guidance_parts)

    reflect_prompt = f"""以下是系统最近的预测、实盘交易计划与实际盘面结果复盘：
{review_context}

本次共有 {num_reflection_tags} 个复盘任务。你必须逐项调用 Tushare MCP、
Tushare skills 获取对应时点可用的资金面数据，或使用 Read 工具阅读对应新闻原文。

本次唯一正式交付物是下面两个工作文件：

1. 复盘分析JSON：
{working_analysis_path}

2. 完整复盘知识库Markdown：
{working_ex_path}

【复盘分析JSON要求】
1. JSON中的任务ID和字段已经由代码生成，禁止增加、删除、改名、合并或重排任务。
2. 只填写各任务现有字段的字符串值，不得修改 reference_kb、new_kb、
   knowledge_base_change。
3. 1日任务必须完整填写 reflection、plan_1、plan_2、plan_3。
4. 3日和5日任务只存在 reflection、plan_1，必须全部填写；禁止自行添加
   plan_2、plan_3。
5. 字段正文必须给出具体归因，禁止使用“未提取到”“同上”“已分析”
   “已完成”“待补充”等占位表述。
6. 字段正文不要重复“1日方向、涨跌幅”“逻辑失效价位”等外层标题。
7. plan_1、plan_2、plan_3是对原交易计划三个组成部分的事后核验，
   绝对不是三套新的预测方案。禁止在这些字段中重新给出新的方向、
   涨跌幅区间或信心分数。
8. 必须保持JSON语法有效。

【各任务字段的准确中文含义】
{field_guidance}

【完整知识库Markdown要求】
1. {knowledge_operation}
2. 知识库必须包含核心避雷法则、成功应对经验和必要错误记录。
3. 引用规则时禁止只写规则编号，必须同时写出规则的完整核心释义，例如
   【避雷30: 业绩预增次日高开兑现】。
4. 未变化规则禁止重新排版或改写，避免制造大面积无意义Diff。
5. {compression_rule}
6. 文件正文不得超过50000字符。
7. 不要把以下预测输出格式规则写进复盘知识库：
{PREDICTION_OUTPUT_RULES}

【历史数据边界】
复盘历史预测时，只能使用该预测当时能够获取的数据。例如复盘
20260606190000的预测，只能获取end_date不晚于该时点的数据。

【允许修改范围】
上面两个工作文件是唯一正式交付物。为了获取复盘数据，可以在当前tools
目录生成CSV、_query_meta.json和临时查询脚本，但不得把它们当作复盘结果。
新闻沙盒和
{os.path.join(BASE_DIR, ".claude", "memory", "strategy_research")}
目录只能读取，禁止写入；禁止修改当前tools目录以外的其他正式文件。

完成后必须重新读取两个工作文件并自行检查字段完整性和文件内容。
不要在终端输出任务分析、知识库正文、XML标签或Diff。
文件修改完成后只回复：FILES_UPDATED
"""

    max_retries = 3
    new_session_id = None
    tool_log_parts = []
    active_session_id = claude_session_id
    current_prompt = reflect_prompt
    parsed_analyses = {}
    edited_knowledge = ""
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[*] 开始进行知识库反思... (第 {attempt}/{max_retries} 次尝试)")
            response_part, tool_log_part, returned_session_id = _run_reflection_claude_with_context_recovery(
                prompt=current_prompt,
                work_dir=reflection_tools_dir,
                news_files=sandbox_news_files,
                session_id=active_session_id,
                model_name=MODEL_REFLECT_NAME,
            )

            if tool_log_part.strip():
                tool_log_parts.append(tool_log_part.strip())
            if returned_session_id:
                new_session_id = returned_session_id
                active_session_id = returned_session_id

            tool_log = "\n\n".join(tool_log_parts)
            with open(os.path.join(reflection_run_dir, "tools.log"), "w", encoding="utf-8") as f:
                f.write(tool_log.rstrip() + "\n")

            try:
                parsed_analyses, edited_knowledge = (
                    _validate_reflection_work_files(
                        working_analysis_path,
                        working_ex_path,
                        tasks_metadata,
                        reference_kb_filename,
                        working_ex_filename,
                    )
                )
                break
            except ValueError as validation_error:
                print(
                    "[!] 复盘工作文件校验失败，正在同会话定点修复：\n"
                    f"{validation_error}"
                )
                if attempt >= max_retries:
                    raise
                current_prompt = f"""上一轮已经执行完复盘，但工作文件校验失败：

{validation_error}

只修复下面两个工作文件中指出的问题：
{working_analysis_path}
{working_ex_path}

不要重新分析已经完整的任务，不要修改任务ID或字段结构，不要输出JSON、
知识库正文、XML标签或Diff。修复后重新读取文件自检，并且只回复：
FILES_UPDATED

注意：reflection是对应预测周期的结果归因；plan_1、plan_2、plan_3
分别是对原交易计划中失效条件、验证信号、盈亏比与仓位（指数任务使用
对应的核心支撑/阻力、盘面验证、系统性风险名称）的事后核验，
不是新的预测方案，禁止重新给出方向、涨跌幅区间或信心分数。
"""
                time.sleep(3)
        except ClaudeCompactionError as e:
            print(f"[X] Claude 复盘会话压缩失败，本次复盘立即终止: {e}")
            raise
        except Exception as e:
            err_msg = str(e)
            print(f"❌ 反思过程报错或解析失败 (第 {attempt} 次尝试): {err_msg}")
            if attempt < max_retries:
                time.sleep(3)
            else:
                return old_ex, latest_ex_filename  # fallback

    try:
        final_content = _replace_reflected_marker(
            edited_knowledge,
            pending_reflect_Ts,
        )
        new_knowledge = re.sub(
            r'<!--\s*reflected:[^>]*-->',
            '',
            final_content,
            flags=re.IGNORECASE,
        ).strip()
        kb_change_content = _build_knowledge_base_diff(
            old_ex,
            new_knowledge,
            reference_kb_filename,
            working_ex_filename,
        )
        kb_log_title = "知识库版本差异"

        log_content = "# 预测复盘与计划追踪报告\n\n"
        for task in tasks_metadata:
            tid = task['id']
            ttype = task['type']
            atype = task['atype']

            log_content += task['ctx']

            analysis = parsed_analyses.get(tid, {})
            if not analysis:
                log_content += "> [!WARNING]\n> 模型未按格式返回该任务的复盘数据。\n\n"
                continue

            plan_1_label =  "核心支撑/阻力" if atype == 'idx' else "逻辑失效价位"
            plan_2_label =  "盘面验证信号" if atype == 'idx' else "盘中验证信号"
            plan_3_label = "系统性风险评估" if atype == 'idx' else "盈亏比与仓位"

            if ttype == '1d':
                log_content += f"【1日方向、涨跌幅的复盘分析】{analysis.get('reflection', '')}\n\n"
                log_content += f"【{plan_1_label}的复盘分析】{analysis.get('plan_1', '')}\n\n"
                log_content += f"【{plan_2_label}的复盘分析】{analysis.get('plan_2', '')}\n\n"
                log_content += f"【{plan_3_label}的复盘分析】{analysis.get('plan_3', '')}\n\n"
            else:
                log_content += f"【{ttype.replace('d', '日')}涨跌幅的复盘分析】{analysis.get('reflection', '')}\n\n"
                log_content += f"【{plan_1_label}的复盘分析】{analysis.get('plan_1', '')}\n\n"

            log_content += "---\n\n"

        log_content += f"# {kb_log_title}\n\n{kb_change_content}\n"

        with open(os.path.join(reflection_run_dir, "predict_analysis.md"), "w", encoding="utf-8") as f:
            f.write(log_content.rstrip() + "\n")

        # 版本Diff由代码生成，不再依赖模型输出标签或修改摘要。
        analysis_payload = {
            "reference_kb": reference_kb_filename,
            "new_kb": working_ex_filename,
            "knowledge_base_change": {
                "type": "full" if is_first_reflection else "diff",
                "title": kb_log_title,
                "content": kb_change_content,
            },
            **parsed_analyses,
        }
        with open(working_analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis_payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(reflection_run_dir, "predict_analysis.json"), "w", encoding="utf-8") as f:
            json.dump(analysis_payload, f, ensure_ascii=False, indent=2)

        # 工作副本、运行目录正式产物及 experience 根目录兼容副本保持一致。
        with open(working_ex_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        new_ex_filename = working_ex_filename
        run_ex_path = os.path.join(reflection_run_dir, new_ex_filename)
        with open(run_ex_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        shutil.copy2(run_ex_path, os.path.join(experience_dir, new_ex_filename))

        if new_session_id:
            with open(claude_session_file, 'w', encoding='utf-8') as f:
                f.write(f"# Claude 专属复盘会话\nSessionID: {new_session_id}\n\n最近一次更新: {datetime.now().strftime('%Y%m%d %H:%M:%S')}\n已复盘的最新的交易日: {T}\n")

        print(f"[+] 复盘知识库更新完毕！保存为 {run_ex_path}")
        return new_knowledge, new_ex_filename
    except Exception as e:
        print(f"❌ 反思后续处理报错: {e}")
        return old_ex, latest_ex_filename  # fallback


def run_reflection(run_date: str, run_time: str) -> tuple[str, str]:
    """执行复盘，并保证本次复盘新闻沙盒最终被清理。"""
    sandbox_dir = os.path.join(
        PREDICT_DIR,
        PREDICT_PROFILE_NAME,
        "experience",
        f"predict_ex_{run_date}_{run_time}",
        "tools",
        "news_sandbox",
    )
    try:
        return _run_reflection(run_date, run_time)
    finally:
        if os.path.exists(sandbox_dir):
            shutil.rmtree(sandbox_dir, ignore_errors=True)


# =============================================================================
# Claude CLI bridge
# =============================================================================


def _parse_claude_stream(stdout):
    """Parse Claude CLI stream-json output without changing its log format."""
    captured_session_id = ""
    tool_log_parts = []
    text_parts = []
    current_tool_name = None
    current_tool_input_parts = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        inner = event.get("event", event)
        event_type = inner.get("type", event.get("type", ""))

        if event_type == "system" and inner.get("subtype") == "init":
            captured_session_id = inner.get("session_id", "")

        if event_type == "content_block_start":
            content_block = inner.get("content_block", {})
            if content_block.get("type") == "tool_use":
                current_tool_name = content_block.get("name", "unknown")
                current_tool_input_parts = []

        elif event_type == "content_block_delta":
            delta = inner.get("delta", {})
            if delta.get("type") == "input_json_delta":
                current_tool_input_parts.append(delta.get("partial_json", ""))
            elif delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))

        elif event_type == "content_block_stop":
            if current_tool_name:
                input_str = "".join(current_tool_input_parts)
                if input_str:
                    try:
                        input_obj = json.loads(input_str)
                        if current_tool_name in ("Bash", "PowerShell"):
                            command = input_obj.get("command", "")
                            tool_log_parts.append(
                                f">>> {current_tool_name}\n    command:\n{command}"
                            )
                        elif current_tool_name == "Write":
                            file_path = input_obj.get("file_path", "?")
                            content = input_obj.get("content", "")
                            tool_log_parts.append(
                                f">>> Write({file_path})\n    content:\n{content}"
                            )
                        else:
                            args = [f"{key}={value}" for key, value in input_obj.items()]
                            tool_log_parts.append(
                                f">>> {current_tool_name}({', '.join(args)})"
                            )
                    except:
                        tool_log_parts.append(f">>> {current_tool_name}(...)")
                current_tool_name = None
                current_tool_input_parts = []

        elif event_type == "result":
            if inner.get("subtype", "") == "success":
                result_text = inner.get("result", "")
                if result_text and not text_parts:
                    text_parts.append(str(result_text))

    tool_log = "\n".join(tool_log_parts) if tool_log_parts else "(无工具调用记录)"
    assistant_text = "".join(text_parts)
    if not assistant_text.strip():
        assistant_text = stdout

    return assistant_text, tool_log, captured_session_id


def _is_claude_context_overflow(error) -> bool:
    """判断 Claude CLI 错误是否属于上下文长度超限。"""
    error_text = str(error).lower()
    overflow_markers = (
        "maximum context length",
        "prompt is too long",
        "conversation too long",
        "context_length_exceeded",
        "context window exceeded",
    )
    return any(marker in error_text for marker in overflow_markers)


def _compact_claude_session(
    session_id: str,
    work_dir: str = "",
    model_name: str = MODEL_REFLECT_NAME,
) -> None:
    """向已有 Claude 会话发送原生 `/compact` 命令。"""
    if not session_id:
        raise ValueError("压缩 Claude 会话时缺少 SessionID")

    cwd_dir = os.path.abspath(work_dir) if work_dir else BASE_DIR
    if work_dir:
        os.makedirs(cwd_dir, exist_ok=True)

    env = os.environ.copy()
    env["TUSHARE_TOKEN"] = TUSHARE_TOKEN
    env["ANTHROPIC_MODEL"] = model_name
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(REFLECTION_AUTO_COMPACT_WINDOW)

    cmd = [
        CLAUDE_EXE,
        "-p",
        "--resume", session_id,
        "--verbose",
        "--input-format", "text",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--model", model_name,
    ]

    result = subprocess.run(
        cmd,
        input=REFLECTION_COMPACT_COMMAND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd_dir,
    )
    stdout_text = result.stdout or ""
    stderr_text = result.stderr or ""
    combined_output = "\n".join(
        part.strip() for part in (stderr_text, stdout_text) if part.strip()
    )
    if result.returncode != 0:
        raise ClaudeCompactionError(
            f"Claude CLI 退出码为 {result.returncode}:\n{combined_output[:2000]}"
        )

    compact_boundary_found = False
    failure_details = []
    for raw_line in stdout_text.splitlines():
        raw_lower = raw_line.lower()
        if "local-command-stderr" in raw_lower or "error during compaction" in raw_lower:
            failure_details.append(raw_line)

        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue

        inner = event.get("event", event)
        if event.get("is_error") is True or inner.get("is_error") is True:
            failure_details.append(
                str(inner.get("result") or inner.get("content") or "is_error=true")
            )
        if inner.get("type") == "system" and inner.get("subtype") == "compact_boundary":
            compact_boundary_found = True

    if failure_details:
        detail = "\n".join(failure_details)
        raise ClaudeCompactionError(f"Claude 返回压缩失败事件:\n{detail[:2000]}")
    if not compact_boundary_found:
        detail = combined_output[:2000] if combined_output else "Claude CLI 未返回任何压缩事件"
        raise ClaudeCompactionError(
            "Claude CLI 未返回 compact_boundary，不能判定为压缩成功。"
            f"\n{detail}"
        )


def _run_reflection_claude_with_context_recovery(
    prompt: str,
    work_dir: str,
    news_files: list,
    session_id: str,
    model_name: str,
) -> tuple[str, str, str]:
    """执行复盘；上下文超限时先压缩旧会话，失败则使用完整提示词新开会话。"""
    call_kwargs = {
        "prompt": prompt,
        "work_dir": work_dir,
        "news_files": news_files,
        "session_id": session_id,
        "model_name": model_name,
        "is_reflection": True,
    }
    try:
        return run_claude_code(**call_kwargs)
    except Exception as error:
        if not _is_claude_context_overflow(error):
            raise
        if not session_id:
            raise ClaudeCompactionError(
                f"Claude 上下文超限，但当前没有可压缩的 SessionID: {error}"
            ) from error

        print(f"[!] Claude 复盘会话上下文超限，正在压缩会话 {session_id}...")
        try:
            _compact_claude_session(
                session_id,
                work_dir=work_dir,
                model_name=model_name,
            )
        except Exception as compact_error:
            raise ClaudeCompactionError(str(compact_error)) from compact_error

        print("[+] Claude 复盘会话压缩完成，正在重试当前任务。")
        try:
            return run_claude_code(**call_kwargs)
        except Exception as retry_error:
            if _is_claude_context_overflow(retry_error):
                raise ClaudeCompactionError(
                    f"Claude 会话压缩后重试仍然上下文超限: {retry_error}"
                ) from retry_error
            raise

def run_claude_code(
    prompt: str,
    work_dir: str = "",
    news_files: list = None,
    session_id: str = "",
    model_name: str = MODEL_ASSISTANT_NAME,
    is_reflection: bool = False,
) -> tuple[str, str, str]:
    """
    Claude CLI 公共执行器，由助手与反思入口复用。

    参数:
        prompt:     发给 Claude 的指令
        work_dir:   脚本工作目录（Claude 写 Python 脚本到这里）
        news_files: 可读取的新闻文件精确路径列表（仅限 main_md + links）
        session_id: 续接的会话 ID（非空时用 --resume 续接,此时不需要再配置一遍）
        model_name: 指定使用的模型
        is_reflection: 是否启用反思专属权限与提示
    返回: (发给DeepSeek的数据文本, 工具调用日志, session_id)
    """
    if news_files is None:
        news_files = []

    print(f"\n[*] 内部唤起 Claude: {prompt[:80]}...")
    tmp_settings = None
    cwd_dir = os.path.abspath(work_dir) if work_dir else BASE_DIR
    try:
        # 构建权限白名单：放行必要操作
        settings_permissions = [
            "Bash(uv run python *)",
            "Bash(uv run *)",
            "Bash(mkdir *)",
            "Bash(ls *)",
            "Bash(cd *)",
            "Bash(cat *)",
            "Bash(type *)",
            "PowerShell(ls *)",
            "PowerShell(cd *)",
            "PowerShell(cat *)",
            "PowerShell(uv run python *)",
            "PowerShell(uv run *)",
            "PowerShell(New-Item *)",
            "PowerShell(Out-File *)",
            "PowerShell(type *)",
            "mcp__tushareMcp__*",
        ]

        # 工作目录读取权限
        if work_dir:
            work_dir_abs = os.path.abspath(work_dir)
            drive, path_no_drive = os.path.splitdrive(work_dir_abs)

            p_unix = path_no_drive.replace('\\', '/')
            drive_lower = drive.lower()
            drive_upper = drive.upper()

            # 添加allow 权限
            settings_permissions.extend([
                f"Write({drive_lower}{p_unix}/*)", f"Write({drive_lower}{p_unix}/**)", f"Write({drive_lower}{p_unix})",
                f"Write({drive_upper}{p_unix}/*)", f"Write({drive_upper}{p_unix}/**)", f"Write({drive_upper}{p_unix})",
                f"Edit({drive_lower}{p_unix}/**)", f"Edit({drive_upper}{p_unix}/**)",
                f"MultiEdit({drive_lower}{p_unix}/**)", f"MultiEdit({drive_upper}{p_unix}/**)",
                f"Write(**{p_unix}/*)", f"Write(**{p_unix}/**)"
            ])

        # 复盘模式只追加策略研究记忆目录的读取权限
        if is_reflection:
            mem_abs = os.path.abspath(
                os.path.join(BASE_DIR, ".claude", "memory", "strategy_research")
            )
            # 公开仓库首次运行时该可选目录可能尚不存在；空目录也可安全传给 CLI。
            os.makedirs(mem_abs, exist_ok=True)
            mem_drive, mem_no_drive = os.path.splitdrive(mem_abs)
            mem_unix = mem_no_drive.replace('\\', '/')
            mem_drive_l = mem_drive.lower()
            mem_drive_u = mem_drive.upper()
            settings_permissions.extend([
                f"Read({mem_drive_l}{mem_unix}/*)", f"Read({mem_drive_l}{mem_unix}/**)", f"Read({mem_drive_l}{mem_unix})",
                f"Read({mem_drive_u}{mem_unix}/*)", f"Read({mem_drive_u}{mem_unix}/**)", f"Read({mem_drive_u}{mem_unix})",
                f"Read(**{mem_unix}/*)", f"Read(**{mem_unix}/**)",
            ])

        # 新闻文件读取权限
        for fpath in news_files:
            fpath_abs = os.path.abspath(fpath)
            drive, path_no_drive = os.path.splitdrive(fpath_abs)

            p_unix = path_no_drive.replace('\\', '/')
            drive_lower = drive.lower()
            drive_upper = drive.upper()

            # 添加allow 权限
            settings_permissions.extend([
                f"Read({drive_lower}{p_unix})", f"Read({drive_upper}{p_unix})", f"Read(**{p_unix})"
            ])

        tmp_settings = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({
            "permissionMode": "bypassPermissions",
            "permissions": {"allow": settings_permissions},
        }, tmp_settings)
        tmp_settings.close()

        # 增减环境变量
        env = os.environ.copy()
        env["TUSHARE_TOKEN"] = TUSHARE_TOKEN
        # 强制覆盖当前环境变量，确保 Claude Code 读取到指定的模型
        env["ANTHROPIC_MODEL"] = model_name
        if is_reflection:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(REFLECTION_AUTO_COMPACT_WINDOW)


        ## 对于复盘的会话id 需要进行下面这种设置因为新闻文件在变， 如果是助手的会话id 可能 "--settings" "--add-dir" "--model" 不需要重复设置
        cmd = [
            CLAUDE_EXE,
            "-p",
            "--verbose",
            "--input-format", "text",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--settings", tmp_settings.name,
            "--model", model_name,
        ]

        # 使用已有会话 ID 续接会话
        if session_id:
            cmd.insert(2, "--resume")
            cmd.insert(3, session_id)

        # 工作权限+目录
        if work_dir:
            os.makedirs(work_dir, exist_ok=True)
            cmd.extend(["--add-dir", work_dir])

        # 新闻权限+目录（助手时 为tools/news_sandbox 文件夹，复盘时 多个不同的新闻路径）
        for fpath in news_files:
            d = os.path.dirname(fpath)
            if d and d not in cmd:
                cmd.extend(["--add-dir", d])

        # 反思模式下，记忆库权限+目录
        if is_reflection:
            if mem_abs not in cmd:
                cmd.extend(["--add-dir", mem_abs])

        if not session_id:
            # 首次会话：发送完整的能力清单和纪律规则
            capability_prompt = f"""
=== Claude Runtime Capability ===
你当前运行于受控 Agent 环境。已经自动为你授予了以下能力，请放心使用，**绝对不要再次申请权限或询问用户**：

[当前运行目录] {cwd_dir}

[已完全放行的能力]
✓ 使用 Tushare MCP / Tushare Skills
✓ 使用 Write / Edit / MultiEdit 修改或创建代码
✓ 在工作目录内自由创建 CSV / JSON / Python 文件
✓ 使用 Bash / PowerShell 运行脚本 (如 `uv run python script.py`)

[工作目录]
{"写入/修改目录:" + work_dir if work_dir else "无写入/修改目录权限"}

"""

            if work_dir:
                capability_prompt += f"""
【写文件极简法则（仅限 Windows 环境）】
优先使用自带的 `Write` 工具写文件。
如果需要用命令行，请使用 `PowerShell` 工具并搭配 `Out-File`。
**严禁使用 Bash 里的 `echo` 或 `>` 进行多行文件写入（例如禁止 `echo "代码" > file.py`）**，Windows 的引号和多行字符会直接导致校验错误！

【⚠️致命拦截红线：脚本执行安全限制（绝对服从）⚠️】
1. **绝对禁止**在 PowerShell 或 Bash 工具中使用 `cd` 命令切换目录！这会触发底层安全沙箱拦截并导致程序永远卡死等待用户手动授权！
2. **绝对禁止**在一个工具调用里组合多个操作（禁止使用 `&&`、`;` 或 `|` 组合执行逻辑），例如 `cd xxx && python script.py` 是**致命错误**！这会被判断为 Compound command 并立刻导致运行挂起！
3. 正确且唯一的做法：用一条最干净的命令，通过 `uv run python` 和**绝对路径**运行 Python 脚本！
   范例：`PowerShell(uv run python "{work_dir}/your_script.py")`
   错误范例：`PowerShell(python {work_dir}/your_script.py)` 或 `PowerShell(cd {work_dir} && python your_script.py)`

{f"""【数据桥接规则】
无论数据时间跨度长短、数据量大小，只要你调用工具获取或计算了用于判断的数据，必须在工作目录（{work_dir}）下生成 CSV 文件，并同时生成(或更新) `_query_meta.json`。禁止只在终端文字中返回数据而不落 CSV。
`_query_meta.json` 格式如下：
{{
  "file":[
     "a.csv",
     "b.csv"
  ]
}}
**严重警告**：编写的 Python 脚本必须使用 `os.path.dirname(os.path.abspath(__file__))` 来拼接保存路径，**绝对禁止使用 `os.getcwd()` 或相对路径保存**！因为你没有使用 cd，当前路径其实是在根目录！
如果有多个数据集，请生成多个 CSV 并全部登记到 `_query_meta.json` 的 `file` 列表中。""" if not is_reflection else ""}


【工作目录自检（必做）】
开始核心任务前，请先用 Python 验证写权限。例如：
```python
import os
print('WORKDIR_OK', os.getcwd())
open('permission_test.txt', 'w').write('ok')
os.remove('permission_test.txt')
print('WRITE_OK')
```
运行此脚本，成功再继续。失败立即报错。
"""

            if not is_reflection:
                 # === 构建更智能的 Prompt (给助手用) 反思的直接在prompt里===
                allowed_summary = ["可读取新闻路径:",]
                for f in news_files:
                    allowed_summary.append(f"{f}|")
                capability_prompt += f"""
{chr(10).join(allowed_summary) if len(allowed_summary) > 1 else "无可读取新闻路径"}
"""
            full_prompt = capability_prompt + "\n\n=== 原始任务 ===\n\n" + prompt

        else:
            # 续接会话：模型已记住规则，极大精简，仅发送任务和可能更新的文件列表
            full_prompt = f"=== 续接任务 [当前运行目录] {cwd_dir} ===\n\n{prompt}"

        if is_reflection:
            full_prompt += (
                f"\n\n【复盘专属提醒】\n"
                f"你可以随时使用 Read 工具读取 `{mem_abs}` 目录下的所有记忆文件。"
                "但不要在此目录下写入任何文件。"
            )


        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=cwd_dir,  ## 这个才是运行的当前目录 默认会读取.gitignore 里的忽略规则
        )

        if result.returncode != 0:
            err_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            raise RuntimeError(f"Claude CLI 执行失败 (Exit Code {result.returncode}):\n{err_msg}")

        return _parse_claude_stream(result.stdout)

    finally:
        if tmp_settings:
            try:
                os.unlink(tmp_settings.name)
            except OSError:
                pass

# =============================================================================
# News collection and context assembly
# =============================================================================


def run_scraper(code: str):
    """
    第一步：调用 scraper 脚本抓取最新新闻
    """
    print(f"[*] 正在启动爬虫，抓取股票 {code} 的最新新闻...")
    try:
        cmd = ["uv", "run", "--no-sync", "python", "news_manager/stocknews_scraper.py", "list", "--code", code]
        subprocess.run(cmd, check=True)
        print("[+] 爬虫抓取完毕！")
        return True
    except subprocess.CalledProcessError as e:
        print(f"⚠️ 爬虫执行异常: {e}")
        return False


def _is_useful_news_listing(md_path: str, stock_code: str) -> bool:
    """Return True when a main listing markdown has usable news content."""
    basename = os.path.basename(md_path)
    if not basename.startswith(f"{stock_code}_"):
        return False
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return False
    return "[本地]" in content or ("## " in content and "- " in content)


def _news_context_failed(news_content: str) -> bool:
    """Return True when local news context indicates scraping/listing failure."""
    if not news_content:
        return True
    failure_markers = (
        "未找到股票",
        "未找到日期文件夹",
        "未能在此日期找到主新闻 Markdown 文件",
    )
    return any(marker in news_content for marker in failure_markers)


def get_latest_news_content(stock_code: str) -> tuple[str, list[str], list[str]]:
    """
    第二步：查找主 Markdown 文件及解析本地附件，拼接为完整上下文
    返回 tuple: (完整上下文文本, 提取到的 links 列表, 新闻文件绝对路径列表[main_md + articles])
    """
    stock_dir = os.path.join(NEWS_BASE_DIR, str(stock_code))
    if not os.path.exists(stock_dir):
        return f"未找到股票 {stock_code} 的新闻目录", [], []

    # 按名称降序排列，取最新的日期文件夹 (如 20260527)
    date_dirs = sorted([d for d in os.listdir(stock_dir) if d.isdigit()], reverse=True)
    if not date_dirs:
        return "未找到日期文件夹", [], []

    latest_date_dir = os.path.join(stock_dir, date_dirs[0])

    # 查找主控 markdown，如 600519_新闻公告_225523.md
    md_files = glob.glob(os.path.join(latest_date_dir, "*_新闻公告_*.md"))
    md_files = [p for p in md_files if _is_useful_news_listing(p, str(stock_code))]
    if not md_files:
        return "未能在此日期找到主新闻 Markdown 文件", [], []

    md_files.sort(reverse=True)
    main_md_path = md_files[0]

    # 1. 读取主文件内容
    with open(main_md_path, 'r', encoding='utf-8') as f:
        main_content = f.read()

    full_context = f"=== 核心公告摘要: {os.path.basename(main_md_path)} ===\n{main_content}\n\n"

    # 2. 正则匹配出所有带 "本地" 的附件链接
    links = re.findall(r'\[本地\]\((.*?\.md)\)', main_content)
    news_file_paths = [main_md_path]  # 精确路径列表，用于授权 Claude Read

    for link in links:
        # 将 "../articles/xxx.md" 拼接成绝对路径
        article_path = os.path.normpath(os.path.join(os.path.dirname(main_md_path), link))
        news_file_paths.append(article_path)
        if os.path.exists(article_path):
            try:
                with open(article_path, 'r', encoding='utf-8') as af:
                    content = af.read()
                    full_context += f"=== 新闻正文: {os.path.basename(article_path)} ===\n{content}\n\n"

                    # 提取正文中的本地图片并加入 news_file_paths
                    img_links = re.findall(r'!\[.*?\]\((.*?)\)', content)
                    for img in img_links:
                        if img.startswith("http"): continue
                        img_path = os.path.normpath(os.path.join(os.path.dirname(article_path), img))
                        if os.path.exists(img_path) and img_path not in news_file_paths:
                            news_file_paths.append(img_path)
            except Exception as e:
                full_context += f"=== 新闻正文 (读取失败): {link} ===\n\n"

    return full_context, links, news_file_paths


# =============================================================================
# Per-asset prediction orchestration
# =============================================================================


def _build_trading_plan_text(asset_type):
    if asset_type == 'idx':
        return (
            "--- 宏观/大盘应对参考 ---\n"
            "【核心支撑/阻力】大盘关键点位或均线，跌破/突破则上述趋势判断失效。\n"
            "【盘面验证信号】明日重点观察的领涨跌板块、量能变化或外部催化剂。\n"
            "【系统性风险评估】结合整体环境，给出当前市场情绪及整体仓位建议（如：满仓做多/半仓轮动/防守观望/空仓）。\n"
        )
    return (
        "--- 实战交易计划 ---\n"
        "【逻辑失效价位】具体价格或关键均线，跌破则上述理由完全失效，必须止损。\n"
        "【盘中验证信号】明日开盘需重点观察的量价特征或外部催化剂。\n"
        "【盈亏比与仓位】结合胜率和赔率，给出买卖评级（强烈看多/谨慎试错/观望/清仓）及建议仓位比例。\n"
    )


def _strip_report_quote_prefix(markdown):
    """去掉评估报告详情块中的 Markdown 引用前缀。"""
    return "\n".join(
        re.sub(r"^> ?", "", line)
        for line in markdown.strip().splitlines()
    ).strip()


def _report_entry_matches_asset_type(
    report_date,
    report_time,
    asset_type,
    code,
    predict_root,
):
    """通过原预测文件目录确认报告记录的资产类型。"""
    date_dir = os.path.join(
        predict_root,
        report_date.replace("-", ""),
        asset_type,
        code,
    )
    compact_time = report_time.replace(":", "")
    prediction_pattern = os.path.join(
        date_dir,
        f"{compact_time}*",
        f"{code}.md",
    )
    for prediction_file in glob.glob(prediction_pattern):
        with open(prediction_file, "r", encoding="utf-8") as f:
            content = f.read()
        if f"- **类型**: {asset_type}" in content:
            return True
    return False


def _extract_complete_one_day_history(entry_body, target_date):
    """提取完整的1日预测和复盘记录；字段不全时返回空字符串。"""
    target_label = (
        f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
    )
    table_rows = re.findall(
        rf"^\| 【{re.escape(target_label)} (?:方向|涨跌幅)】.*?\|$",
        entry_body,
        re.MULTILINE,
    )
    if len(table_rows) != 2 or any(
        "暂无结果" in row for row in table_rows
    ):
        return ""

    logic_match = re.search(
        r"^> 【1日逻辑理由】.*?"
        r"(?=^> 【(?:3|5)日逻辑理由】|^> --- 实战交易计划 ---)",
        entry_body,
        re.MULTILINE | re.DOTALL,
    )
    plan_match = re.search(
        r"^> --- 实战交易计划 ---.*?(?=^</details>)",
        entry_body,
        re.MULTILINE | re.DOTALL,
    )
    reflection_match = re.search(
        r"^\*\*【1日复盘归因】\*\*.*?"
        r"(?=^\*\*【(?:3|5)日复盘归因】\*\*|^</details>)",
        entry_body,
        re.MULTILINE | re.DOTALL,
    )
    if not logic_match or not plan_match or not reflection_match:
        return ""

    table = "\n".join(
        [
            "| 预测内容 | 实际情况 | 回测结果 |",
            "| --- | --- | --- |",
            *table_rows,
        ]
    )
    return "\n\n".join(
        [
            f"### {target_label}",
            table,
            _strip_report_quote_prefix(logic_match.group(0)),
            _strip_report_quote_prefix(plan_match.group(0)),
            reflection_match.group(0).strip(),
        ]
    )


def get_recent_prediction_reflection_history(
    asset_type,
    code,
    previous_trade_dates,
    predict_root=None,
    report_path=None,
):
    """读取同一标的前两个交易日的完整1日预测和复盘记录。"""
    predict_root = predict_root or os.path.join(
        PREDICT_DIR,
        PREDICT_PROFILE_NAME,
    )
    report_path = report_path or os.path.join(
        predict_root,
        "experience",
        "predict_eval_history.md",
    )
    if not os.path.isfile(report_path):
        return ""

    with open(report_path, "r", encoding="utf-8") as f:
        report_markdown = f.read()

    requested_dates = set(previous_trade_dates)
    records_by_date = {}
    entry_pattern = re.compile(
        r"^### 预测时间: "
        r"(?P<report_date>\d{4}-\d{2}-\d{2}) "
        r"(?P<report_time>\d{2}:\d{2}:\d{2})\n"
        r"(?P<body>.*?)(?=^---\s*$|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    for entry_match in entry_pattern.finditer(report_markdown):
        entry_body = entry_match.group("body")
        code_match = re.search(
            r"^\*\*【标的】([A-Za-z0-9_]+)(?:\s+.*?)?\*\*$",
            entry_body,
            re.MULTILINE,
        )
        if not code_match or code_match.group(1) != code:
            continue
        if not _report_entry_matches_asset_type(
            entry_match.group("report_date"),
            entry_match.group("report_time"),
            asset_type,
            code,
            predict_root,
        ):
            continue

        for target_date in requested_dates:
            record = _extract_complete_one_day_history(
                entry_body,
                target_date,
            )
            if record:
                records_by_date[target_date] = record

    ordered_records = [
        records_by_date[trade_date]
        for trade_date in previous_trade_dates
        if trade_date in records_by_date
    ]
    if not ordered_records:
        return ""
    return (
        "<recent_prediction_reflection_history>\n"
        + "\n\n".join(ordered_records)
        + "\n</recent_prediction_reflection_history>"
    )


def _build_prediction_user_prompt(
    asset_type,
    type_label,
    stock_display,
    dates_info,
    trading_plan_str,
    use_preload=True,
    has_recent_history=False,
):
    use_asset_preload = use_preload and asset_type in {"stk", "etf", "idx"}
    preload_notice = ""
    if use_asset_preload and asset_type == "stk":
        preload_notice = (
            "系统已经在提示词下方分别使用 <technical_data>, <money_flow_data>, <basic_info> 标签为你默认提供了"
            "该标的最近 120 个交易日的**前复权日线及核心技术指标数据**，以及最近 30 个交易日的"
            "**资金流向数据**和**初步股票基本面信息**。请注意查阅！\n"
        )
    elif use_asset_preload:
        preload_notice = (
            "系统已经在提示词下方使用 <technical_data> 标签为你默认提供了"
            f"该{type_label}最近 120 个交易日的**原始价格日线及核心技术指标数据**。请注意查阅！\n"
        )

    recent_history_notice = ""
    if has_recent_history:
        recent_history_notice = (
            "系统已经在提示词下方使用 "
            "<recent_prediction_reflection_history> 标签提供了当前标的"
            "前两个交易日的1日预测、实际结果、1日逻辑、交易计划和复盘归因。"
            "请对照历史判断是否存在连续误判、逻辑失效或可复用经验，"
            "但不得机械照搬历史方向。\n"
        )

    provided_data_description = "系统默认提供的数据" if use_asset_preload else "当前上下文中已有的数据"

    return f"""运用你所有的金融知识，分析以下行情摘要{'与新闻' if asset_type == 'stk' else ''}，推测{type_label} {stock_display} 未来的走势。
{preload_notice}
{recent_history_notice}

【深度数据自主挖掘要求】
{provided_data_description}仅供基础扫视。作为顶级量化交易员，你必须具备极强的数据探索嗅觉。
要求你基于当前盘面的蛛丝马迹（如：异动、关键位置、特殊日历事件等），**自主思考并决定还需要哪些深度的量化数据**（例如：更高阶的资金面、深度财务指标、各类估值模型、市场情绪或特定业务数据等）来印证你的假设。
请高度发散思维，通过 `<claude>指令</claude>` 调用外部助手（内置 Tushare MCP 及海量量化 Skills）去主动挖掘隐藏线索。
切勿仅依赖基础数据就仓促下结论！你有最多 5 次请求数据的机会，请务必建立严密的多维数据交叉验证体系。

当前时间为{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}，
最近的交易日情况如下（按时间顺序）：
- {dates_info['ts_dates']['prev_td']} (最近交易日的前一个交易日)
- {dates_info['ts_dates']['data_td']} (最近的交易日)
- {dates_info['ts_dates']['next_td']} (后一个交易日)
请务必使用能获取到的最新的数据进行分析。

规则：
1. 请求数据用：<claude>指令</claude>
2. 必须使用qfq的数据
3. 系统已在底层拦截并强制限定了数据获取的 end_date，你只需专心说明需要提取多长窗口的数据即可，无需再手动强调 end_date。
4. 若基础数据不足以支撑高信心决策，必须继续请求深度数据（如估值、筹码、融资融券等），直到有多维信息交叉验证为止。严禁凭空推测数据（例如：在分析“筹码分布”或“市盈率”时，绝对禁止凭空捏造，必须通过<claude>获取真实数据），必要时利用claude执行python脚本来精确计算。
5. 【数据通信规则】系统会自动要求 Claude 输出 CSV 文件到工作目录，你只需在 `<claude>指令</claude>` 中说明需要什么数据（如：龙虎榜、筹码分布等），无需手动写明生成 CSV 和 _query_meta.json 的长篇要求，系统底层会自动帮你附加此格式指令。系统仅会传输首次生成或后续发生变化的文件内容。
6. 你最多有 5 次请求数据的机会。
7. 预测时间段参考（你必须使用这些精确日期标签，禁止使用"明日/未来"等模糊词）：例如
   - 1日目标日期: {dates_info['label_1d']}
   - 3日目标范围: {dates_info['label_3d']}
   - 5日目标范围: {dates_info['label_5d']}
8. 最终你必须输出包含分析加上明确结果的标签（例子如下所示），且注意输出有以下不变规则：
{PREDICTION_OUTPUT_RULES}

<result>
【标的】{stock_display}
【{dates_info['label_1d']} 方向】涨 (或者 跌/平) 【信心】y
【{dates_info['label_1d']} 涨跌幅】x% ~ x% 【信心】y
【{dates_info['label_3d']} 涨跌幅】x% ~ x% 【信心】y
【{dates_info['label_5d']} 涨跌幅】x% ~ x% 【信心】y
【1日逻辑理由】z
【3日逻辑理由】z
【5日逻辑理由】z
{trading_plan_str.strip()}
</result>
9.每次输出先分析再请求1个<claude>指令</claude>或1个<result>结果</result>,不能多个。
10.【复盘知识库规则引用完整性】引用复盘知识库规则时，禁止仅输出规则编号（例如仅写“触发【避雷 30】”），必须同时附带该规则的完整核心释义。规范格式示例：触发【避雷 30: 业绩预增次日高开兑现】。

"""

def agent_worker(
    stock_code: str,
    asset_type: str,
    predict_ex_knowledge: str,
    predict_ex_filename: str,
    run_date: str,
    run_time: str,
    pre_fetched_tick: str = None,
    dates_info: dict = None,
    use_news_scraper: bool = True,
    use_preload: bool = True,
    preloaded_basic_df=None,
    preloaded_money_df=None,
    preloaded_recent_history="",
) -> dict:
    """
    独立的子进程工作函数，负责单个标的的分析全流程。
    asset_type: 'stk' | 'etf' | 'idx'
    """
    if not dates_info:
        dates_info = get_predict_target_dates()

    suffix = dates_info["suffix"]  # "" 或 "_nowind"

    # 建立日志目录: predict/{model}/{date}/{type}/{code}/{HHMMSS}/ 或 .../_nowind/
    run_dir = os.path.join(
        PREDICT_DIR,
        PREDICT_PROFILE_NAME,
        run_date,
        asset_type,
        stock_code,
        run_time + suffix,
    )
    work_dir = os.path.join(run_dir, "tools")
    log_file = os.path.join(run_dir, f"{stock_code}.log")
    log_tool_file = os.path.join(run_dir, f"{stock_code}_tools.log")

    tools_used = []

    def write_log(txt_line: str):
        os.makedirs(run_dir, exist_ok=True)
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(txt_line + "\n")

    def write_tool_log(tool_log: str):
        os.makedirs(run_dir, exist_ok=True)
        with open(log_tool_file, 'a', encoding='utf-8') as f:
            f.write(tool_log + "\n")


    try:
        # 仅 stk 抓取新闻
        news_content = ""
        news_links = []
        news_file_paths = []
        original_news_paths = []
        if asset_type == "stk" and use_news_scraper:
            scrape_ok = False
            try:
                scrape_ok = bool(run_scraper(stock_code))
            except Exception as e:
                write_log(f"⚠️ 爬虫执行异常: {e}")

            if not scrape_ok:
                msg = f"新闻抓取失败：股票 {stock_code} 的同花顺新闻抓取未成功，已停止本次预测。"
                write_log(msg)
                return {
                    "stock": stock_code,
                    "asset_type": asset_type,
                    "status": "fail",
                    "prediction": msg,
                    "tools": tools_used,
                    "news_paths": [],
                }

            news_content, news_links, news_file_paths = get_latest_news_content(stock_code)
            original_news_paths = news_file_paths.copy()

            # ======== ✨ 沙盒拦截启动 ✨ ========
            if news_file_paths:
                news_file_paths, _ = create_news_sandbox(work_dir, news_file_paths)
            # ======== ✨ 沙盒拦截结束 ✨ ========

            if _news_context_failed(news_content):
                msg = f"新闻抓取失败：股票 {stock_code} 未获取到有效新闻文件，已停止本次预测。"
                write_log(msg)
                return {
                    "stock": stock_code,
                    "asset_type": asset_type,
                    "status": "fail",
                    "prediction": msg,
                    "tools": tools_used,
                    "news_paths": [],
                }

        # 判断当前是否在开盘时间 (9:30 到 15:00 之间，且是交易日)
        now = datetime.now()
        is_market_open = _is_market_open(now, dates_info.get('is_trading_day', True))
        # 仅在盘中才获取实时数据
        if is_market_open:
            if pre_fetched_tick:
                tick_content = pre_fetched_tick
            else:
                tick_dict = fetch_realtime_quotes([(stock_code, asset_type)], method="tencent")
                tick_content = tick_dict.get((stock_code, asset_type), "")
                if not tick_content:
                    tick_content = "=== 实时/最新盘面数据 ===\n(获取失败)"
        else:
            tick_content = "=== 实时盘面数据 ===\n(当前为非交易时段，无需获取盘中快照，请直接以K线历史数据为准)"

        m15_kline_text = fetch_realtime_15min_kline(stock_code, asset_type, num_bars=80)
        tick_content = tick_content + "\n\n" + m15_kline_text


        # Claude 会话持久化
        claude_session_id = ""

        # 融合上下文
        type_label = {"stk": "股票", "etf": "ETF", "idx": "指数"}.get(asset_type, "标的")
        target_name = get_target_name(stock_code, asset_type)
        stock_display = f"{stock_code} {target_name}".strip()

        # === 默认获取高频行情的 Markdown 数据 ===
        daily_content = ""
        money_content = ""
        basic_content = ""
        pre_fetched = []

        if preloaded_recent_history:
            pre_fetched.append("前两个交易日精简预测/复盘")

        if is_market_open and "获取失败" not in tick_content:
            pre_fetched.append("实时盘面")
        if "获取失败" not in m15_kline_text:
            pre_fetched.append("15分钟K线")

        if use_preload:
            data_td = dates_info['ts_dates']['data_td']
            ts_c = _get_ts_code(stock_code, asset_type)

            if asset_type == "stk":
                # 1. 基本面优先使用主进程批量预取结果；直接调用 worker 时保留单股获取能力。
                try:
                    basic_df = preloaded_basic_df
                    if basic_df is None:
                        basic_df = get_stock_basic_info(ts_c)
                    if not basic_df.empty:
                        basic_content = f"=== 股票基本面与行业特征信息 ===\n{basic_df.to_markdown(index=False)}\n\n"
                        pre_fetched.append("基本面")
                except Exception as e:
                    basic_content = f"=== 默认基本面数据获取异常 ===\n(异常信息: {e})\n\n"
                    write_log(f"⚠️ 预加载基本面数据时发生异常: {e}")

            # 2. 根据资产类型获取近120个交易日的日线及指标。
            try:
                if asset_type == "stk":
                    daily_df = get_daily_qfq_with_indicators(ts_code=ts_c, end_date=data_td, trade_days=120)
                    price_label = "前复权"
                    price_description = "前复权开高低收价格"
                    volume_description = "- vol: 成交量(手), amount: 成交额(千元), turnover_rate_f: 自由流通换手率(%)\n"
                elif asset_type == "etf":
                    daily_df = get_etf_daily_with_indicators(ts_code=ts_c, end_date=data_td, trade_days=120)
                    price_label = "原始价格"
                    price_description = "ETF原始开高低收价格（为统一结构保留_qfq后缀）"
                    volume_description = "- vol: 成交量(手), amount: 成交额(千元)\n"
                else:
                    daily_df = get_idx_daily_with_indicators(ts_code=ts_c, end_date=data_td, trade_days=120)
                    price_label = "原始价格"
                    price_description = "指数原始开高低收价格（为统一结构保留_qfq后缀）"
                    volume_description = "- vol: 成交量(手), amount: 成交额(千元)\n"

                if not daily_df.empty:
                    col_explanations = (
                        "【日线及指标列名含义说明】:\n"
                        "- trade_date: 交易日期\n"
                        f"- open_qfq, high_qfq, low_qfq, close_qfq: {price_description}\n"
                        f"{volume_description}"
                        f"- ma_qfq_X: 基于{price_label}的 X 日简单移动平均线 (包含 5, 10, 20, 30, 60, 90, 250)\n"
                        f"- macd_dif_qfq, macd_dea_qfq, macd_qfq: 基于{price_label}的 MACD 指标 (DIF, DEA, MACD柱)\n"
                        f"- rsi_qfq_X: 基于{price_label}的 X 日 RSI 相对强弱指标 (包含 6, 12, 24)\n"
                        f"- boll_upper_qfq, boll_mid_qfq, boll_lower_qfq: 基于{price_label}的 BOLL 布林带 (上、中、下轨)\n"
                        f"- kdj_k_qfq, kdj_d_qfq, kdj_qfq: 基于{price_label}的 KDJ 随机指标 (K, D, J)\n\n"
                    )
                    daily_md = daily_df.round(3).to_markdown(index=False)
                    daily_content = f"=== 默认提供的基础日线与技术指标数据 (近 {len(daily_df)} 个交易日) ===\n{col_explanations}{daily_md}\n\n"
                    pre_fetched.append("基础日线")
                else:
                    daily_content = "=== 默认基础日线数据 ===\n(获取为空，如有需要请自行尝试通过 <claude> 工具获取)\n\n"
            except Exception as e:
                daily_content = f"=== 默认基础日线数据获取异常 ===\n(异常信息: {e})\n\n"
                write_log(f"⚠️ 预加载基础日线数据时发生异常: {e}")

            if asset_type == "stk":
                # 3. 资金流向优先使用主进程批量预取结果。
                try:
                    money_df = preloaded_money_df
                    if money_df is None:
                        money_df = get_money_flow_data(ts_code=ts_c, end_date=data_td, trade_days=30)
                    if not money_df.empty:
                        money_explanations = (
                            "【资金流向列名含义说明】:\n"
                            "- buy_sm_vol/amount: 小单买入量(手)/额(万元)\n"
                            "- buy_md_vol/amount: 中单买入量(手)/额(万元)\n"
                            "- buy_lg_vol/amount: 大单买入量(手)/额(万元)\n"
                            "- buy_elg_vol/amount: 特大单买入量(手)/额(万元)\n"
                            "- net_mf_vol/amount: 主力净流入量(手)/额(万元)\n\n"
                        )
                        money_md = money_df.round(3).to_markdown(index=False)
                        money_content = f"=== 默认提供的资金流向数据 (近 {len(money_df)} 个交易日) ===\n{money_explanations}{money_md}\n\n"
                        pre_fetched.append("资金流")
                except Exception as e:
                    money_content = f"=== 默认资金流向数据获取异常 ===\n(异常信息: {e})\n\n"
                    write_log(f"⚠️ 预加载资金流向数据时发生异常: {e}")

        trading_plan_str = _build_trading_plan_text(asset_type)
        user_initial_prompt = _build_prediction_user_prompt(
            asset_type,
            type_label,
            stock_display,
            dates_info,
            trading_plan_str,
            use_preload=use_preload,
            has_recent_history=bool(preloaded_recent_history),
        )

        # 重新组织 prompt 内容，用明确的 XML 标签区分
        content_parts = [user_initial_prompt]
        if basic_content:
            content_parts.append(f"<basic_info>\n{basic_content}</basic_info>\n")
        if daily_content:
            content_parts.append(f"<technical_data>\n{daily_content}</technical_data>\n")
        if money_content:
            content_parts.append(f"<money_flow_data>\n{money_content}</money_flow_data>\n")
        if tick_content:
            content_parts.append(f"<realtime_tick_data>\n{tick_content}</realtime_tick_data>\n")
        if news_content:
            content_parts.append(f"<news_articles>\n{news_content}</news_articles>\n")
        if preloaded_recent_history:
            content_parts.append(preloaded_recent_history)

        # ===== 注入私域知识库 =====
        private_knowledge = []
        try:
            from private_data.reader import read_local_markdown, read_notion_page
            # 1. 扫描 private_data/data 下的 md 文件
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "private_data", "data")
            if os.path.exists(data_dir):
                for md_file in glob.glob(os.path.join(data_dir, "*.md")):
                    file_name = os.path.basename(md_file)
                    try:
                        md_text = read_local_markdown(file_name)
                        private_knowledge.append(f"--- 本地私有知识库 ({file_name}) ---\n{md_text}")
                    except Exception as e:
                        print(f"[-] 读取私有知识库文件 {file_name} 失败: {e}")

            # 2. 检查 NOTION_PRIVATE_PAGE_ID
            notion_private_page_id = getattr(config, "NOTION_PRIVATE_PAGE_ID", "")
            if notion_private_page_id != '':
                try:
                    notion_text = read_notion_page(notion_private_page_id)
                    private_knowledge.append(f"--- Notion 私有知识库 ---\n{notion_text}")
                except Exception as e:
                    print(f"[-] 读取 Notion 私有知识库失败: {e}")
        except Exception as e:
            pass

        if private_knowledge:
            all_private_str = "\n\n".join(private_knowledge)
            content_parts.append(f"<private_knowledge>\n{all_private_str}\n</private_knowledge>\n")
            write_log(f"\n[此部分进入模型上下文] 👉 成功提取私域知识库 (共 {len(private_knowledge)} 个数据源)，已作为参考上下文注入。")

        full_message_content = "\n".join(content_parts)

        if pre_fetched:
            pre_fetched_str = f"系统已预加载 [{' / '.join(pre_fetched)}] 数据"
        else:
            pre_fetched_str = "系统未预加载基础数据"

        write_log(f"=== {asset_type.upper()} {stock_code} 分析进程启动 ===\n{pre_fetched_str}")

        api_client = None
        prediction_backend = "claude_cli"
        prediction_session_id = ""

        system_prompt = f"你是一个顶级的量化事件驱动交易员，具有极强的文本分析与指标融合能力。当前分析标的类型: {type_label}。"
        if predict_ex_knowledge:
            system_prompt += f"\n\n请**严格参考**并运用以下系统复盘知识库中的避雷与盈利法则：\n{predict_ex_knowledge}"
            write_log(f"\n[此部分进入模型上下文] 👉 (已挂载历史复盘知识库文件: {predict_ex_filename})")

        if news_links:
            write_log(f"[此部分进入模型上下文] 👉 提取到的本地新闻附件链接:\n  - " + "\n  - ".join(news_links))

        # 因为上下文很长，不在 log 中写完整 user prompt，只给大模型：
        write_log(f"\n[此部分进入模型上下文] sysem prompt:\n{system_prompt}\n\n[此部分进入模型上下文] user prompt (数据已用标签分割附在末尾):\n{user_initial_prompt}...\n")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_message_content}
        ]

        sent_files = {}
        for step in range(1, MAX_AGENT_STEPS + 1):
            write_log(
                f"\n[*] [ 思考中... 第 {step}/{MAX_AGENT_STEPS} 步]"
            )

            if prediction_backend == "claude_cli":
                try:
                    if prediction_session_id:
                        cli_reply = _run_prediction_claude_cli(
                            messages,
                            MODEL_PREDICT_CLI_NAME,
                            prediction_session_id,
                        )
                    else:
                        cli_reply = _run_prediction_claude_cli(
                            messages,
                            MODEL_PREDICT_CLI_NAME,
                        )
                    ds_reply = str(cli_reply)
                    returned_prediction_session_id = getattr(
                        cli_reply,
                        "session_id",
                        "",
                    )
                    if returned_prediction_session_id:
                        if not prediction_session_id:
                            write_log(
                                "\n[*] 已创建当前标的的 Claude 预测会话: "
                                f"{returned_prediction_session_id}"
                            )
                        prediction_session_id = returned_prediction_session_id
                    elif not re.search(
                        r"<result>.*?</result>",
                        ds_reply,
                        re.DOTALL,
                    ):
                        raise RuntimeError(
                            "Claude CLI 未返回预测 SessionID，"
                            "无法保证后续步骤续接同一会话"
                        )
                    reasoning = ""
                    output_label = "Claude CLI"
                except Exception as cli_error:
                    write_log(
                        "\n[!] Claude CLI 预测调用失败，后续改用 DeepSeek API: "
                        f"{cli_error}"
                    )
                    prediction_backend = "deepseek_api"

            if prediction_backend == "deepseek_api":
                if api_client is None:
                    api_client = OpenAI(
                        api_key=DEEPSEEK_API_KEY,
                        base_url=DEEPSEEK_BASE_URL,
                    )
                response = api_client.chat.completions.create(
                    model=MODEL_PREDICT_API_NAME,
                    messages=messages,
                    temperature=0.7,
                )
                ds_reply = response.choices[0].message.content or ""
                reasoning = getattr(
                    response.choices[0].message,
                    "reasoning_content",
                    "",
                )
                output_label = "DeepSeek"

            log_text = f"[{output_label} 输出]:\n"
            if reasoning:
                log_text += f"<think>\n{reasoning}\n</think>\n"
            log_text += ds_reply

            write_log(log_text)
            messages.append({"role": "assistant", "content": ds_reply})

            # 优先处理 <claude>：DeepSeek 可能一次输出 <claude> + <result>，
            # 必须先执行工具调用，避免跳过 Claude 直接收幻觉生成的 result
            claude_match = re.search(r'<claude>(.*?)</claude>', ds_reply, re.DOTALL)
            # 部分 DeepSeek 响应只返回 reasoning_content，content 为空。
            # 此时再从日志所显示的 <think> 内容中提取 Claude 指令。
            if not claude_match and not ds_reply.strip() and reasoning:
                claude_match = re.search(r'<claude>(.*?)</claude>', reasoning, re.DOTALL)

            # output 和 think 中都没有工具指令时，直接请求下一轮输出。
            if not ds_reply.strip() and not claude_match:
                if step < MAX_AGENT_STEPS:
                    messages.append({"role": "user", "content": "请遵守输出格式，需要数据给 <claude>指令</claude>，预测请给 <result>预测内容格式</result>"})
                continue

            if claude_match:
                claude_instruction = claude_match.group(1).strip()

                # 强行注入“必须落盘 CSV 和限定 end_date” 的系统指令，防范大模型遗忘
                enforced_end_date = dates_info['ts_dates']['data_td'].replace('-', '')
                enhanced_instruction = (
                    f"你好，请使用 TushareMCP/Tushare Skills :\n'{claude_instruction}'\n\n"
                    "【系统强制附加指令】\n"
                    "1. 请务必将获取到的所有数据整理成 CSV 格式并保存在工作目录下。\n"
                    "2. 必须生成或更新 `_query_meta.json` 文件，使用 `file` 作为键名记录所有生成的 CSV 文件名数组。\n"
                    f"3. 绝对严格遵守时间约束：所有数据获取请求的 `end_date` 参数强制设定为 {enforced_end_date}。\n"
                    "4. 你已获得工作目录内所需权限。绝对不要申请权限、等待用户批准或询问用户；如果某条命令受限，必须立即改用已允许的方式执行，禁止把权限审批请求作为回复。\n"
                    "5. 执行 Python 脚本时，只允许使用 `uv run python \"<脚本绝对路径>\"`，严禁使用 `python ...`。"
                )

                data_text, tool_log, new_session_id = run_claude_code(
                    prompt=enhanced_instruction,
                    work_dir=work_dir,
                    news_files=news_file_paths,
                    session_id=claude_session_id,
                )
                if new_session_id:
                    claude_session_id = new_session_id
                tools_used.append(f"\n第{step}次数据查询结果\n{claude_instruction}")
                data_text_raw = data_text
                # 检查并自动桥接 _query_meta.json
                meta_file = os.path.join(work_dir, "_query_meta.json")
                if os.path.exists(meta_file):
                    try:
                        import pandas as pd
                        with open(meta_file, 'r', encoding='utf-8') as mf:
                            meta_data = json.load(mf)

                        csv_contents = []
                        def extract_files(obj):
                            if isinstance(obj, dict):
                                for v in obj.values():
                                    extract_files(v)
                            elif isinstance(obj, list):
                                for item in obj:
                                    extract_files(item)
                            elif isinstance(obj, str) and obj.lower().endswith('.csv'):
                                fpath = obj if os.path.isabs(obj) else os.path.join(work_dir, obj)
                                if os.path.exists(fpath):
                                    mtime = os.path.getmtime(fpath)
                                    if fpath not in sent_files or sent_files[fpath] != mtime:
                                        sent_files[fpath] = mtime
                                        try:
                                            df = pd.read_csv(fpath)
                                            # 大模型防错位优化：转为 Markdown 或纯 CSV，并保留 3 位小数降噪
                                            try:
                                                content_str = df.round(3).to_markdown(index=False)
                                            except ImportError:
                                                content_str = df.round(3).to_csv(index=False)
                                            csv_contents.append(f"==== 文件: {os.path.basename(fpath)} ====\n{content_str}\n")
                                        except Exception as e:
                                            csv_contents.append(f"==== 读取 {obj} 失败 ====\n{e}\n")

                        extract_files(meta_data)
                        if csv_contents:
                            data_text += "\n\n[系统自动附加: 根据 _query_meta.json 自动提取的数据]\n" + "\n".join(csv_contents)
                            write_log(f"\n[数据桥接成功] 自动提取了 {len(csv_contents)} 个数据文件内容并附加到返回结果。")
                    except Exception as e:
                        write_log(f"\n⚠️ 解析 _query_meta.json 或读取文件失败: {e}")

                feedback_prompt = f"[第{step}次数据查询结果]\n{data_text}\n\n请继续逻辑(可继续请求<claude>或输出<result>结论)"
                # 工具调用日志只写 log 文件，不进 DeepSeek 上下文
                write_tool_log(f"\n[第{step}次工具调用日志]\n{tool_log}\n")
                write_log(f"\n[Claude 结果 ({len(data_text)} 字符返回)]:{data_text_raw}\n")
                messages.append({"role": "user", "content": feedback_prompt})
                continue

            result_match = re.search(r'<result>(.*?)</result>', ds_reply, re.DOTALL)
            if result_match:
                write_log(f"\n🎉 [达成最终预测]")
                rel_news = [os.path.relpath(p, BASE_DIR) for p in original_news_paths]

                # 检查工作目录下是否生成了任何 CSV 数据文件
                csv_files = glob.glob(os.path.join(work_dir, "*.csv"))
                prediction_content = result_match.group(1).strip()
                status = "success"

                # 检查是否成功获取到了预加载数据
                has_pre_fetched_data = bool(daily_content and "默认提供的基础日线" in daily_content)

                if not csv_files and not has_pre_fetched_data:
                    status = "fail"
                    prediction_content += "\n\n⚠️ 【系统强制标记为无效】: 预测过程中模型未能成功调用量化工具拉取并生成任何 CSV 数据，且系统预拉取数据为空，本次预测被认定为空想，不可信！"
                    write_log(f"\n⚠️ [判定失效]: 核心目录 {work_dir} 未发现任何生成的 CSV 文件，且预拉取数据为空，强置为 fail。")

                return {"stock": stock_code, "asset_type": asset_type, "status": status, "prediction": prediction_content, "tools": tools_used, "news_paths": rel_news, "pre_fetched": pre_fetched}

            if step < MAX_AGENT_STEPS:
                messages.append({"role": "user", "content": "请遵守输出格式，需要数据给 <claude>指令</claude>，预测请给 <result>预测内容格式</result>"})

        rel_news = [os.path.relpath(p, BASE_DIR) for p in original_news_paths]
        return {"stock": stock_code, "asset_type": asset_type, "status": "timeout", "prediction": "未能生成标准判断。", "tools": tools_used, "news_paths": rel_news, "pre_fetched": pre_fetched}

    except Exception as e:
        err_msg = f"进程异常导致失败: {e}"
        write_log(err_msg)
        return {"stock": stock_code, "asset_type": asset_type, "status": "error", "prediction": err_msg, "tools": tools_used, "news_paths": [], "pre_fetched": []}
    finally:
        # ======== ✨ 预测结束打扫战场 ✨ ========
        sandbox_dir = os.path.join(run_dir, "tools", "news_sandbox")
        if os.path.exists(sandbox_dir):
            shutil.rmtree(sandbox_dir, ignore_errors=True)

# =============================================================================
# CLI orchestration and report output
# =============================================================================


def _deduplicate_codes(codes) -> list:
    """规范化并按原输入顺序去除重复标的代码。"""
    unique_codes = []
    seen = set()
    for code in codes or []:
        normalized = str(code).strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_codes.append(normalized)
    return unique_codes


def _split_batch_stock_data(df) -> dict:
    """把批量股票接口结果按 ts_code 拆成可直接传给各 worker 的 DataFrame。"""
    if df is None or df.empty or "ts_code" not in df.columns:
        return {}
    return {
        str(ts_code): group.reset_index(drop=True)
        for ts_code, group in df.groupby("ts_code", sort=False)
    }


def _prefetch_batch_stock_data(stk_codes, data_td: str) -> tuple[dict, dict]:
    """在主进程并行批量获取全部股票的基本面和近30日资金流。"""
    if not stk_codes:
        return {}, {}

    batch_ts_code = ",".join(_get_ts_code(code, "stk") for code in stk_codes)
    print(f"[*] 批量预加载 {len(stk_codes)} 只股票的基本面和资金流...")

    basic_df = pd.DataFrame()
    money_df = pd.DataFrame()
    with ThreadPoolExecutor(max_workers=2) as executor:
        basic_future = executor.submit(get_stock_basic_info, batch_ts_code)
        money_future = executor.submit(
            get_money_flow_data,
            batch_ts_code,
            None,
            data_td,
            30,
        )

        try:
            basic_df = basic_future.result()
        except Exception as exc:
            print(f"[!] 批量预加载股票基本面失败: {exc}")

        try:
            money_df = money_future.result()
        except Exception as exc:
            print(f"[!] 批量预加载股票资金流失败: {exc}")

    return _split_batch_stock_data(basic_df), _split_batch_stock_data(money_df)


def _update_prediction_evaluation(model_root, sync_web=True):
    """更新本地预测评估报告，并按配置同步网页。"""
    try:
        predict_evaluator.run_evaluation(
            predict_dir=model_root,
            page_id=NOTION_PAGE_ID if sync_web else "",
        )
        eval_md_path = os.path.join(
            model_root,
            "experience",
            "predict_eval_history.md",
        )
        print(f"✅ 本地回测评估报告已更新！请查看文件: {eval_md_path}")
        return True
    except Exception as e:
        print(f"⚠️ 自动历史回测失败: {e}")
        return False


def main(stk_codes, etf_codes, idx_codes, reflect_only=False, use_news_scraper=True, use_preload=True):
    """
    入口函数，控制全局复盘、并发协程分发和主报表生成
    stk_codes/etf_codes/idx_codes: 各自的代码列表
    """
    # 同一资产类型内按输入顺序去重，避免多个 worker 竞争同一个运行目录。
    stk_codes = _deduplicate_codes(stk_codes)
    etf_codes = _deduplicate_codes(etf_codes)
    idx_codes = _deduplicate_codes(idx_codes)

    # 收集所有任务: (code, asset_type)
    all_tasks = (
        [(code, "stk") for code in stk_codes]
        + [(code, "etf") for code in etf_codes]
        + [(code, "idx") for code in idx_codes]
    )

    if not all_tasks and not reflect_only:
        print("[X] 请至少指定 --stk / --etf / --idx 中的一个")
        return

    type_names = []
    if reflect_only: type_names.append("仅复盘模式")
    if stk_codes: type_names.append(f"{len(stk_codes)}只股票")
    if etf_codes: type_names.append(f"{len(etf_codes)}只ETF")
    if idx_codes: type_names.append(f"{len(idx_codes)}只指数")
    print(f"🚀 系统启动！当前队列: {', '.join(type_names)}")

    dates_info = get_predict_target_dates()
    run_date = dates_info["write_date"]
    run_time = datetime.now().strftime("%H%M%S")
    suffix = dates_info["suffix"]

    # 第1步：处理复盘知识库
    predict_ex_knowledge, latest_ex_filename = run_reflection(run_date, run_time)
    model_root = os.path.join(PREDICT_DIR, PREDICT_PROFILE_NAME)

    if reflect_only:
        _update_prediction_evaluation(model_root)
        print("\n✅ 仅复盘模式执行完毕。没有任何预测任务被触发。")
        return

    # 复盘后先仅刷新本地报告，让本次预测能读取最新的同标的历史。
    report_refreshed = _update_prediction_evaluation(
        model_root,
        sync_web=False,
    )
    global_recent_history = {}
    if report_refreshed:
        previous_trade_dates = [
            dates_info["ts_dates"]["prev_td"],
            dates_info["ts_dates"]["data_td"],
        ]
        for code, atype in all_tasks:
            history = get_recent_prediction_reflection_history(
                asset_type=atype,
                code=code,
                previous_trade_dates=previous_trade_dates,
                predict_root=model_root,
            )
            if history:
                global_recent_history[(code, atype)] = history

    # 第2步：开启多进程执行预测
    print("\n================ 进入多进程智脑分析循环 ================")
    results = []

    # === 在这里预先获取全部实时行情（如果开盘） ===
    now = datetime.now()
    is_market_open = _is_market_open(now, dates_info.get('is_trading_day', True))

    global_realtime_data = {}
    if is_market_open:
        print(f"\n[*] 市场交易中，预先获取批量实时行情 (共 {len(all_tasks)} 个标的)...")
        # 预先在主进程使用 tickflow(tf.quotes.get) 批量获取
        if len(all_tasks) <= 50:
            global_realtime_data = fetch_realtime_quotes(all_tasks, method="tick")
        else:
            global_realtime_data = fetch_realtime_quotes(all_tasks, method="tencent")

    # 股票基本面和资金流接口支持逗号分隔的批量股票代码，在主进程一次获取并拆分。
    global_basic_data = {}
    global_money_data = {}
    if use_preload and stk_codes:
        global_basic_data, global_money_data = _prefetch_batch_stock_data(
            stk_codes,
            dates_info['ts_dates']['data_td'],
        )

    max_workers = min(len(all_tasks), multiprocessing.cpu_count(), MAX_AGENT_WORKERS)

    # Windows spawn 模式下 Ctrl+C 会传给子进程干扰 import，先屏蔽
    original_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with multiprocessing.Pool(processes=max_workers) as pool:
            signal.signal(signal.SIGINT, original_sigint)  # 主进程恢复 SIGINT
            tasks = []
            for code, atype in all_tasks:
                ts_code = _get_ts_code(code, atype)
                basic_df = global_basic_data.get(ts_code, pd.DataFrame()) if atype == "stk" else None
                money_df = global_money_data.get(ts_code, pd.DataFrame()) if atype == "stk" else None
                tasks.append((
                    code,
                    atype,
                    predict_ex_knowledge,
                    latest_ex_filename,
                    run_date,
                    run_time,
                    global_realtime_data.get((code, atype)),
                    dates_info,
                    use_news_scraper,
                    use_preload,
                    basic_df,
                    money_df,
                    global_recent_history.get((code, atype), ""),
                ))
            try:
                pool_results = pool.starmap(agent_worker, tasks)
                results.extend(pool_results)
            except KeyboardInterrupt:
                print("\n[X] 用户中断，正在终止子进程...")
                pool.terminate()
                pool.join()
                return
    finally:
        signal.signal(signal.SIGINT, original_sigint)


    # 第3步：聚合输出——每个标的独立 md 到各自目录
    print("================ 全部进程处理完毕，正在保存主报表 ================")
    for res in results:
        code = res["stock"]
        atype = res.get("asset_type", "stk")
        out_dir = os.path.join(model_root, run_date, atype, code, run_time + suffix)
        os.makedirs(out_dir, exist_ok=True)
        md_path = os.path.join(out_dir, f"{code}.md")

        with open(md_path, "w", encoding="utf-8") as rf:
            rf.write(f"# {code} 预测报告 ({run_date} {run_time})\n\n")
            rf.write(f"- **类型**: {atype}\n")
            rf.write(f"- **状态**: {res['status']}\n")
            pre_fetched = res.get('pre_fetched', [])
            if pre_fetched:
                pre_fetched_str = f"系统已预加载 [{' / '.join(pre_fetched)}] 数据"
            else:
                pre_fetched_str = "系统未预加载基础数据"

            if res['tools']:
                tool_str = f"{pre_fetched_str}。另外，模型动态调用了外部工具：\n" + "\n".join(res['tools'])
            else:
                tool_str = f"{pre_fetched_str}，模型未额外调用工具。"

            rf.write(f"- **调用工具记录**: {tool_str}\n")
            news_str = " | ".join(res.get('news_paths', [])) if res.get('news_paths') else "无"
            rf.write(f"- **可读取新闻路径**: {news_str}\n\n")
            rf.write("### AI 模型结论：\n")
            rf.write(f"```text\n{res['prediction']}\n```\n")
            rf.write(f"\n注意：不变规则：\n{PREDICTION_OUTPUT_RULES}\n")
            rf.write("---\n\n")
            rf.write(f"<!-- 参考复盘知识库版本: {latest_ex_filename} -->\n")

    print(f"✅ 执行结束！输出目录: {os.path.join(model_root, run_date)}")

    # 触发自动历史回测与评估
    _update_prediction_evaluation(model_root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="综合智能投研 Agent 多标的打分系统")
    parser.add_argument("--stk", type=str, nargs="*", default=None, help="股票代码列表")
    parser.add_argument("--etf", type=str, nargs="*", default=None, help="ETF代码列表")
    parser.add_argument("--idx", type=str, nargs="*", default=None, help="指数代码列表")
    parser.add_argument("--reflect-only", action="store_true", help="仅执行复盘并更新知识库，不进行后续预测")
    parser.add_argument("--no-news-scraper", action="store_true", help="股票预测时跳过新闻抓取")
    parser.add_argument("--no-preload", action="store_true", help="股票预测时跳过系统默认的日线/资金流/基本面数据预加载")
    args = parser.parse_args()
    main(args.stk, args.etf, args.idx, args.reflect_only, use_news_scraper=not args.no_news_scraper, use_preload=not args.no_preload)
