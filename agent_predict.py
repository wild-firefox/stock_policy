import os
import re
import glob
import subprocess
import tempfile
import shutil
import argparse
import time
import random
import json
from datetime import datetime
from datetime import timedelta
import multiprocessing
import signal
import efinance as ef

# 修改引入根目录配置用于获取 tushare
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_pro,TUSHARE_TOKEN,DEEPSEEK_API_KEY

from openai import OpenAI

# ================= 配置区 =================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1" 
MODEL_NAME = "deepseek-v4-pro" # 运行使用的模型名

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_BASE_DIR = os.path.join(BASE_DIR, "news_manager", "data", "stk", "raw_ths")
PREDICT_DIR = os.path.join(BASE_DIR, "predict")


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
    cal_df = pro.trade_cal(exchange="SSE", start_date=(now - timedelta(days=30)).strftime("%Y%m%d"),
                           end_date=(now + timedelta(days=30)).strftime("%Y%m%d"))
    trading_days = sorted(cal_df[cal_df["is_open"] == 1]["cal_date"].tolist())

    if today_str in trading_days:
        is_trading_day = True
    else:
        is_trading_day = False

    # 找最近的有效收盘数据交易日 (data_td)
    if is_trading_day and current_time >= 15 * 60:
        # 如果今天是交易日且已经收盘，那么最新的收盘数据就是今天的
        data_td = today_str
    else:
        # 否则（盘中、盘前、非交易日），最新收盘数据只能是上一个交易日的
        past_td_today = [d for d in trading_days if d < today_str]
        data_td = past_td_today[-1] if past_td_today else today_str

    # 前一个交易日应为 data_td 的前一个交易日
    past_td_data = [d for d in trading_days if d < data_td]
    prev_td = past_td_data[-1] if past_td_data else data_td

    # 往前找 target_idx 之后的交易日
    def next_n_trading_days(base_date, n):
        """从 base_date 之后取 n 个交易日（不含 base_date 本身）"""
        result = []
        for d in trading_days:
            if d > base_date:
                result.append(d)
                if len(result) >= n:
                    break
        return result

    def format_date(ymd):
        return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"

    def format_range(dates):
        if len(dates) >= 2:
            return f"{format_date(dates[0])}~{format_date(dates[-1])}"
        elif len(dates) == 1:
            return format_date(dates[0])
        return "?"

    # 判断窗口类型
    suffix = ""

    if is_trading_day:
        if current_time >= 19 * 60 or current_time < 9 * 60 + 30:
            pass
        elif 9 * 60 + 30 <= current_time < 15 * 60:
            pass
        else:
            suffix = "_nowind"

    # 无论是盘中、盘后还是非交易日，预测目标的基准**永远**是“最新收盘数据对应的交易日(data_td)”
    next_tds = next_n_trading_days(data_td, 5)

    one_day = next_tds[:1]
    three_days = next_tds[:3]
    five_days = next_tds[:5]

    # 找今天之后的交易日 (next_td)
    future_td = [d for d in trading_days if d > today_str]
    next_td = future_td[0] if future_td else today_str

    return {
        "label_1d": format_date(one_day[0]) if one_day else "?",
        "label_3d": format_range(three_days),
        "label_5d": format_range(five_days),
        "ts_dates": {
            "1d_start": one_day[0] if one_day else "?",
            "1d_end": one_day[0] if one_day else "?",
            "3d_start": three_days[0] if three_days else "?",
            "3d_end": three_days[-1] if three_days else "?",
            "5d_start": five_days[0] if five_days else "?",
            "5d_end": five_days[-1] if five_days else "?",
            "prev_td": format_date(prev_td),
            "data_td": format_date(data_td),
            "next_td": format_date(next_td)
        },
        "write_date": today_str,
        "suffix": suffix,
    }


# ==========================================

def fetch_realtime_quote_with_retry(stock_code: str) -> str:
    """
    获取实时 tick 数据（模仿人类延迟，加入重试逻辑）
    """
    print(f"[{stock_code}] 正在获取 efinance 实时/最新收盘快照...")
    pure_code = stock_code.split('.')[0] if '.' in stock_code else stock_code
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            # 模仿人类延迟
            delay = random.uniform(2.0, 5.0)
            time.sleep(delay)
            
            quote_df = ef.stock.get_latest_quote([pure_code])
            if not quote_df.empty:
                latest_info = quote_df.iloc[0].to_dict()
                result = f"=== 实时/最新盘面数据 ===\n" \
                         f"代码: {latest_info.get('代码')}, 名称: {latest_info.get('名称')}\n" \
                         f"最新价: {latest_info.get('最新价')}, 涨跌幅: {latest_info.get('涨跌幅')}%\n" \
                         f"最高: {latest_info.get('最高')}, 最低: {latest_info.get('最低')}\n" \
                         f"成交量: {latest_info.get('成交量')}, 成交额: {latest_info.get('成交额')}\n" \
                         f"更新时间: {latest_info.get('更新时间')}\n" \
                         f"最新交易日: {latest_info.get('最新交易日')}"
                return result
        except Exception as e:
            print(f"[{stock_code}] efinance 获取失败 (第 {attempt+1} 次): {type(e).__name__}")
            
    return f"=== 实时/最新盘面数据{datetime.now().strftime("%Y%m%d")} ===\n(获取失败)"

def run_reflection(run_date: str, run_time: str) -> tuple[str, str]:
    """
    复盘模块：用最新交易日 T 往前 5 个交易日窗口，验证历史预测。
    - 单日期 D: D 在窗口内 → 纳入复盘
    - 范围 D~E: E == T → 纳入复盘（区间预测完整到期）
    - _nowind/ 目录的预测不进入复盘
    """
    pro = get_pro()
    
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y%m%d")
    model_dir = os.path.join(PREDICT_DIR, MODEL_NAME)
    experience_dir = os.path.join(model_dir, "experience")
    os.makedirs(experience_dir, exist_ok=True)

    old_ex = "这是第一次复盘，暂无知识库记录。"
    latest_ex_filename = "无"

    # 1. 获取最新可验证交易日 T（有收盘数据的最近交易日）
    cal_df = pro.trade_cal(exchange="SSE", start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
                           end_date=today_str)
    trading_days = sorted(cal_df[cal_df["is_open"] == 1]["cal_date"].tolist())
    # T = 最近一个有收盘数据的交易日
    T = ""
    can_check_today = (now_dt.hour >= 19)
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

    # 2. 获取本地最新的 predict_ex 并提取复盘标记
    ex_files = glob.glob(os.path.join(experience_dir, "predict_ex_*.md"))
    latest_ex_file = sorted(ex_files)[-1] if ex_files else None

    if latest_ex_file:
        latest_ex_filename = os.path.basename(latest_ex_file)
    last_reflected_T = ""
    if latest_ex_file:
        with open(latest_ex_file, 'r', encoding='utf-8') as f:
            old_ex = f.read()
        m = re.search(r'<!--\s*reflected:\s*([\d,]+)\s*-->', old_ex)
        if m:
            last_reflected_T = m.group(1).split(",")[-1].strip()
            # 从上下文中剔除旧标记
            old_ex = re.sub(r'<!--\s*reflected:.*?-->', '', old_ex).strip()

    # 防重复：如果在周末/盘中等没有新交易日数据产生时执行，只要 T 没变，就不需要重复复盘
    if T == last_reflected_T:
        print(f"[*] 当前可验证最新交易日(T={T})已复盘过(盘中/周末无新数据)，跳过复盘")
        return old_ex, latest_ex_filename

    if not os.path.exists(model_dir):
        return old_ex, latest_ex_filename

    # 复盘窗口: [T-4, T]（5 个交易日）
    t_idx = trading_days.index(T) if T in trading_days else -1
    review_window = trading_days[max(0, t_idx - 4): t_idx + 1] if t_idx >= 0 else {T}

    def fmt_label(d):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    has_printed_review_header = False
    review_context = ""

    # 3. 收集所有候选预测（按 标的 + 目标预测日 分组，同日取最新）
    candidates = {}  # key=(atype, code, predict_ymd) -> (file_dt, date_folder, ts_entry, content)
    # 5日的收盘数据则需要再往前1日的预测数据
    data_dir = [ _ for _ in sorted(os.listdir(model_dir),reverse=True) if _.isdigit() and trading_days[t_idx - 6] <= _ <= trading_days[t_idx]]
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
                        continue
                    predict_ymd = r1_m.group(1).replace("-", "")
                    if predict_ymd not in review_window:
                        continue
                        
                    # 相同目标日只取最新一条
                    key = (atype, code, predict_ymd)
                    
                    if key not in candidates or file_dt > candidates[key][0]:
                        candidates[key] = (file_dt, date_folder, ts_entry, content)

    # 4. 逐个处理候选
    printed_windows = set()
    reflection_news_files = []
    for (atype, code, predict_ymd), (file_dt, date_folder, ts_entry, content) in candidates.items():
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
        
        if predict_ymd == T:
            should_review = True
        if end_ymd_3d == T:
            should_review = True
        if end_ymd_5d == T:
            should_review = True

        if not should_review:
            continue

        if not dir_m:
            continue

        if not has_printed_review_header:
            print(f"\n[*] 开始复盘: T={fmt_label(T)}, 窗口={list(fmt_label(d) for d in sorted(review_window))}")
            has_printed_review_header = True

        if predict_ymd not in printed_windows:
            print(f"[*] 锁定目标预测日: {predict_ymd}")
            printed_windows.add(predict_ymd)
            
        print(f"    -> 提取标的 {code} 的预测文件 ({date_folder}/{ts_entry})")

        direction = dir_m.group(2).strip()
        conf_dir = dir_m.group(3) if dir_m.group(3) else "未填"

        # 用预测日期查 tushare 获取实际行情
        start_ymd = predict_ymd if predict_ymd else date_folder
        
        def _get_ts_code(c, t):
            if t == 'stk':
                if c.startswith('6'): return f"{c}.SH"
                if c.startswith('8') or c.startswith('4'): return f"{c}.BJ"
                return f"{c}.SZ"
            if t == 'etf':
                return f"{c}.SH" if c.startswith('5') else f"{c}.SZ"
            if t == 'idx':
                return f"{c}.SZ" if c.startswith('3') else f"{c}.SH"
            return f"{c}.SZ"
            
        full_ts_code = _get_ts_code(code, atype)
        query_end = today_str
        
        if atype == 'etf':
            df = pro.fund_daily(ts_code=full_ts_code, start_date=start_ymd, end_date=query_end)
        elif atype == 'idx':
            df = pro.index_daily(ts_code=full_ts_code, start_date=start_ymd, end_date=query_end)
        else:
            df = pro.daily(ts_code=full_ts_code, start_date=start_ymd, end_date=query_end)

        if df is None or df.empty:
            continue

        df = df.sort_values('trade_date').reset_index(drop=True)
        
        # 既然是纯粹预测市场趋势涨跌幅，基准应当是前一天的收盘价（也就是当天的 pre_close）
        base_price = df.iloc[0].get('pre_close')
        if base_price is None or (isinstance(base_price, float) and base_price != base_price) or base_price <= 0:
            continue

        def get_nday_ret(days):
            idx = days - 1
            if len(df) > idx:
                return f"{(df.iloc[idx]['close'] / base_price - 1) * 100:+.2f}%"
            return "时间未到"

        act_1d = get_nday_ret(1)
        act_3d = get_nday_ret(3)
        act_5d = get_nday_ret(5)

        if len(df) > 0:
            actual_pct_chg = (df.iloc[0]['close'] / base_price - 1) * 100
            actual_dir = "涨" if actual_pct_chg > 0 else ("跌" if actual_pct_chg < 0 else "平")
            correct = "[OK]" if actual_dir in direction else "[X]方向"
        else:
            correct = "[?]"

        p_1d = r1_m.group(2).strip() if r1_m else "未填"
        p_3d = r3_m.group(3).strip() if r3_m else "未填"
        p_5d = r5_m.group(3).strip() if r5_m else "未填"
        conf_1d = r1_m.group(3) if r1_m and r1_m.group(3) else "未填"
        conf_3d = r3_m.group(4) if r3_m and r3_m.group(4) else "未填"
        conf_5d = r5_m.group(4) if r5_m and r5_m.group(4) else "未填"

        tools_m = re.search(r'- \*\*调用工具记录\*\*: (.*)', content)
        news_m = re.search(r'- \*\*可读取新闻路径\*\*: (.*)', content)
        tools_str = tools_m.group(1).strip() if tools_m else "无"
        news_str = news_m.group(1).strip() if news_m else "无"
        
        if news_str != "无":
            for p in news_str.split("、"):
                if p.strip():
                    abs_p = os.path.abspath(os.path.join(BASE_DIR, p.strip()))
                    if abs_p not in reflection_news_files:
                        reflection_news_files.append(abs_p)

        label_1d = r1_m.group(1) if r1_m else "?"
        label_3d = f"{r3_m.group(1)}~{r3_m.group(2)}" if r3_m else "?"
        label_5d = f"{r5_m.group(1)}~{r5_m.group(2)}" if r5_m else "?"
        
        ai_conclusion_m = re.search(r'### AI 模型结论：\n```text\n(.*?)```', content, re.DOTALL)
        ai_reasoning = ai_conclusion_m.group(1).strip() if ai_conclusion_m else "未提取到推演逻辑"

        review_context += (f"==== 标的: {code} ====\n"
                           f"  预测执行时间 (警告: 分析当时逻辑时，仅能使用此时间之前的数据): {date_folder} {ts_entry}\n"
                           f"  调用工具记录: {tools_str}\n"
                           f"  使用的新闻路径: {news_str}\n"
                           f"  ==== 预测结果对照 ====\n")
                           
        if predict_ymd <= T:
            review_context += f"  {label_1d} 方向: {direction} (信心 {conf_dir}) | 实际: {correct}\n"
            review_context += f"  {label_1d} 涨跌幅: {p_1d} (信心 {conf_1d}) | 实际: {act_1d}\n"
        if end_ymd_3d and end_ymd_3d <= T:
            review_context += f"  {label_3d} 涨跌幅: {p_3d} (信心 {conf_3d}) | 实际: {act_3d}\n"
        if end_ymd_5d and end_ymd_5d <= T:
            review_context += f"  {label_5d} 涨跌幅: {p_5d} (信心 {conf_5d}) | 实际: {act_5d}\n"
            
        review_context += (f"  ==== AI 原始推演逻辑 ====\n"
                           f"  {ai_reasoning}\n\n")

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

    reflect_prompt = f"以下是系统最近的预测、实盘交易计划与实际盘面结果复盘：\n{review_context}\n\n" \
             f"作为对照，这是上一版提炼的复盘知识库（当前字数：{len(old_ex)}字符）：\n{old_ex}\n\n" \
             f'''请结合你记忆中的历史复盘经验、上一版知识库以及本次验证结果，进行深度反思。
             你可以使用内置的工具(Tushare skills,Tushare MCP,如果有必要）或查阅上面列出的“使用的新闻路径”原文，来探究误判的原因。
             最后，请提炼并输出一份完整的、供后续分析系统使用的【全新复盘知识库】。
             要求：
             1. 包含核心的避雷法则与成功应对经验，注意预测内容主要为应对的辅助信息，重点应该在于避雷法则和成功应对经验。
             2. 剔除无效或错误的旧经验，保留有效经验，整合相似规则，但也不要过度总结经验。
             3. 核心细节可保留，但遇到长篇大论的历史复盘请主动高度精炼压缩。
             4. 严格只输出知识库内容文本，不输出废话。全文总字符数【绝对不可以超过 50000 字符】！
             5. 复盘时对于历史的当下严格按照对应当时能获取的end_data来获取qfq数据,即在复盘的时候,20260606190000的预测,使用的应该是20260606190000之前的数据。
             6. 不要输出已知且不变的系统规则，如下：
                【a.【xx方向】涨 (或者 跌/平)只允许这三个
                b.涨跌幅区间：【禁止伪精度】除非涨跌幅由明确公式计算得到，否则所有预测涨跌幅只能使用整数%或0.5%步长(如-5%,-4.5%,4.5%,5%)。禁止出现任何其它小数形式(如1.3%,1.7%,2.8%,4.2%全部禁止)。
                c.信心打分(0-10,整数)
                d.理由为你的核心逻辑，理由要明确主谓宾
                】'''

    try:
        data_text, tool_log, new_session_id = run_claude_code(
            prompt=reflect_prompt,
            work_dir="",
            news_files=reflection_news_files,
            session_id=claude_session_id
        )
        
        if new_session_id:
            with open(claude_session_file, 'w', encoding='utf-8') as f:
                f.write(f"# Claude 专属复盘会话\nSessionID: {new_session_id}\n\n最近一次更新: {datetime.now().strftime('%Y%m%d %H:%M:%S')}\n已复盘的最新的交易日: {T}\n")
                
        new_knowledge = data_text.strip()

        # 标记当前已复盘的最新交易日 T
        marker = f"\n\n<!-- reflected: {T} -->\n"

        # 写入带有新时间戳的 predict_ex
        new_ex_filename = f"predict_ex_{run_date}_{run_time}.md"
        new_ex_path = os.path.join(experience_dir, new_ex_filename)
        with open(new_ex_path, 'w', encoding='utf-8') as f:
            f.write(new_knowledge + marker)
            
        print(f"[+] 复盘知识库更新完毕！保存为 {new_ex_filename}")
        return new_knowledge, new_ex_filename
    except Exception as e:
        print(f"反思过程报错: {e}")
        return old_ex, latest_ex_filename  # fallback

def run_claude_code(
    prompt: str,
    work_dir: str = "",
    news_files: list = None,
    session_id: str = "",
) -> tuple[str, str, str]:
    """
    供 DeepSeek 调用的外部工具：唤起本地 Claude MCP 执行 Tushare 查询。

    参数:
        prompt:     发给 Claude 的指令
        work_dir:   脚本工作目录（Claude 写 Python 脚本到这里）
        news_files: 可读取的新闻文件精确路径列表（仅限 main_md + links）
        session_id: 续接的会话 ID（非空时用 --resume 续接）

    返回: (发给DeepSeek的数据文本, 工具调用日志, session_id)
    """
    if news_files is None:
        news_files = []

    print(f"\n[*] 内部唤起 Claude: {prompt[:80]}...")
    tmp_settings = None
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

        if work_dir:
            work_dir_abs = os.path.abspath(work_dir)
            drive, path_no_drive = os.path.splitdrive(work_dir_abs)
            
            p_unix = path_no_drive.replace('\\', '/')
            drive_lower = drive.lower()
            drive_upper = drive.upper()
            
            settings_permissions.extend([
                f"Write({drive_lower}{p_unix}/*)", f"Write({drive_lower}{p_unix}/**)", f"Write({drive_lower}{p_unix})",
                f"Write({drive_upper}{p_unix}/*)", f"Write({drive_upper}{p_unix}/**)", f"Write({drive_upper}{p_unix})",
                f"Edit({drive_lower}{p_unix}/**)", f"Edit({drive_upper}{p_unix}/**)",
                f"MultiEdit({drive_lower}{p_unix}/**)", f"MultiEdit({drive_upper}{p_unix}/**)",
                f"Write(**{p_unix}/*)", f"Write(**{p_unix}/**)"
            ])

        is_reflection = (work_dir == "")

        # 为复盘模式专属追加记忆库读取权限
        if is_reflection:
            mem_abs = os.path.abspath(os.path.join(BASE_DIR, ".claude", "memory"))
            mem_drive, mem_no_drive = os.path.splitdrive(mem_abs)
            mem_unix = mem_no_drive.replace('\\', '/')
            mem_drive_l = mem_drive.lower()
            mem_drive_u = mem_drive.upper()
            settings_permissions.extend([
                f"Read({mem_drive_l}{mem_unix}/*)", f"Read({mem_drive_l}{mem_unix}/**)", f"Read({mem_drive_l}{mem_unix})",
                f"Read({mem_drive_u}{mem_unix}/*)", f"Read({mem_drive_u}{mem_unix}/**)", f"Read({mem_drive_u}{mem_unix})",
                f"Read(**{mem_unix}/*)", f"Read(**{mem_unix}/**)",
            ])

        for fpath in news_files:
            fpath_abs = os.path.abspath(fpath)
            drive, path_no_drive = os.path.splitdrive(fpath_abs)
            
            p_unix = path_no_drive.replace('\\', '/')
            drive_lower = drive.lower()
            drive_upper = drive.upper()
            
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

        env = os.environ.copy()
        env["TUSHARE_TOKEN"] = TUSHARE_TOKEN

        cmd = [
            CLAUDE_EXE,
            "-p",
            "--verbose",
            "--input-format", "text",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--settings", tmp_settings.name,
            "--add-dir", BASE_DIR,
        ]
        
        if session_id:
            cmd.insert(2, "--resume")
            cmd.insert(3, session_id)
            
        if work_dir:
            os.makedirs(work_dir, exist_ok=True)
            cmd.extend(["--add-dir", work_dir])
            
        for fpath in news_files:
            d = os.path.dirname(fpath)
            if d and d not in cmd:
                cmd.extend(["--add-dir", d])

        # === 构建更智能的 Prompt ===
        allowed_summary = []
        if work_dir:
            allowed_summary.append(f"写入/修改目录: {work_dir}")

        for f in news_files:
            allowed_summary.append(f"可读取文件: {f}")

        if not session_id:
            # 首次会话：发送完整的能力清单和纪律规则
            capability_prompt = f"""
=== Claude Runtime Capability ===
你当前运行于受控 Agent 环境。已经自动为你授予了以下能力，请放心使用，**绝对不要再次申请权限或询问用户**：

[已完全放行的能力]
✓ 使用 Tushare MCP / Tushare Skills
✓ 使用 Write / Edit / MultiEdit 修改或创建代码
✓ 在工作目录内自由创建 CSV / JSON / Python 文件
✓ 使用 Bash / PowerShell 运行脚本 (如 `uv run python script.py`)

[工作目录]
{work_dir if work_dir else "无写入/修改目录权限"}

"""

            if work_dir:
                capability_prompt += f"""
【写文件极简法则（仅限 Windows 环境）】
优先使用自带的 `Write` 工具写文件。
如果需要用命令行，请使用 `PowerShell` 工具并搭配 `Out-File`。
**严禁使用 Bash 里的 `echo` 或 `>` 进行多行文件写入（例如禁止 `echo "代码" > file.py`）**，Windows 的引号和多行字符会直接导致校验错误！

【数据桥接规则】
如果生成了需要传递的 csv，必须在工作目录下同时生成(或更新) `_query_meta.json` 格式如下：
{{
  "file":[
     "a.csv",
     "b.csv"
  ]
}}
否则直接回答即可。


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

            if is_reflection:
                capability_prompt += f"""
"\n\n【复盘专属提醒】\n你可以随时使用 Read 工具读取 `.claude/memory/` 目录下的所有记忆文件。但不要写入任何文件。"
"""
            if is_reflection:
                capability_prompt += f"""
=== 本次复盘可查阅的新闻文件路径清单 ===
{chr(10).join(allowed_summary) if allowed_summary else "无"}
"""             
            else:
                capability_prompt += f"""
=== 当前已生效的权限范围清单 ===
{chr(10).join(allowed_summary) if allowed_summary else "无"}
"""

            full_prompt = capability_prompt + "\n\n=== 原始任务 ===\n\n" + prompt

            if news_files:
                lines = "\n".join(f"  {i}. {p}" for i, p in enumerate(news_files, 1))
                full_prompt += f"\n\n[可读取的新闻文件（必须从此列表查阅）]\n{lines}\n如需查阅原文，请用 Read 工具。"
                
        else:
            # 续接会话：模型已记住规则，极大精简，仅发送任务和可能更新的文件列表
            full_prompt = f"=== 续接任务 ===\n\n{prompt}"
            
            if is_reflection:
                full_prompt += "\n\n【复盘专属提醒】\n你依然可以随时使用 Read 工具读取 `.claude/memory/` 目录下的所有记忆文件。"
                if news_files:
                    lines = "\n".join(f"  {i}. {p}" for i, p in enumerate(news_files, 1))
                    full_prompt += f"\n\n[本次复盘可查阅的新闻文件路径清单]\n{lines}"

        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=BASE_DIR,
        )

        captured_session_id = ""
        tool_log_parts = []
        text_parts = []
        current_tool_name = None
        current_tool_input_parts = []

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            inner = ev.get("event", ev)
            ev_type = inner.get("type", ev.get("type", ""))

            if ev_type == "system" and inner.get("subtype") == "init":
                captured_session_id = inner.get("session_id", "")

            if ev_type == "content_block_start":
                cb = inner.get("content_block", {})
                if cb.get("type") == "tool_use":
                    current_tool_name = cb.get("name", "unknown")
                    current_tool_input_parts = []

            elif ev_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "input_json_delta":
                    current_tool_input_parts.append(delta.get("partial_json", ""))
                elif delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))

            elif ev_type == "content_block_stop":
                if current_tool_name:
                    input_str = "".join(current_tool_input_parts)
                    if input_str:
                        try:
                            input_obj = json.loads(input_str)
                            if current_tool_name in ("Bash", "PowerShell"):
                                cmd_str = input_obj.get("command", "")
                                tool_log_parts.append(f">>> {current_tool_name}\n    command:\n{cmd_str}")
                            elif current_tool_name == "Write":
                                fpath = input_obj.get("file_path", "?")
                                content = input_obj.get("content", "")
                                tool_log_parts.append(f">>> Write({fpath})\n    content:\n{content}")
                            else:
                                args = [f"{k}={v}" for k, v in input_obj.items()]
                                tool_log_parts.append(f">>> {current_tool_name}({', '.join(args)})")
                        except:
                            tool_log_parts.append(f">>> {current_tool_name}(...)")
                    current_tool_name = None
                    current_tool_input_parts = []

            elif ev_type == "result":
                sub = inner.get("subtype", "")
                if sub == "success":
                    result_text = inner.get("result", "")
                    if result_text and not text_parts:
                        text_parts.append(str(result_text))

        tool_log = "\n".join(tool_log_parts) if tool_log_parts else "(无工具调用记录)"
        assistant_text = "".join(text_parts)

        # 兜底：如果解析失败，回退用原始 stdout
        if not assistant_text.strip():
            assistant_text = result.stdout

        return assistant_text, tool_log, captured_session_id

    except subprocess.CalledProcessError as e:
        return f"Claude 执行失败: {e.stderr}", f"[错误] {e.stderr[:500]}", ""
    except subprocess.TimeoutExpired:
        return "Claude 执行超时 (300s)", "[错误] TimeoutExpired", ""
    finally:
        if tmp_settings:
            try:
                os.unlink(tmp_settings.name)
            except OSError:
                pass

def run_scraper(code: str):
    """
    第一步：调用 scraper 脚本抓取最新新闻
    """
    print(f"[*] 正在启动爬虫，抓取股票 {code} 的最新新闻...")
    try:
        cmd = ["uv", "run", "python", "news_manager/stocknews_scraper.py", "list", "--code", code]
        subprocess.run(cmd, check=True)
        print("[+] 爬虫抓取完毕！")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ 爬虫执行异常: {e}")


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
    parsed_links = []
    news_file_paths = [main_md_path]  # 精确路径列表，用于授权 Claude Read

    for link in links:
        parsed_links.append(link)
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

    return full_context, parsed_links, news_file_paths

def agent_worker(stock_code: str, asset_type: str, predict_ex_knowledge: str, predict_ex_filename: str, run_date: str, run_time: str) -> dict:
    """
    独立的子进程工作函数，负责单个标的的分析全流程。
    asset_type: 'stk' | 'etf' | 'idx'
    """
    # 计算预测目标日期
    dates_info = get_predict_target_dates()
    suffix = dates_info["suffix"]  # "" 或 "_nowind"

    # 建立日志目录: predict/{model}/{date}/{type}/{code}/{HHMMSS}/ 或 .../_nowind/
    run_dir = os.path.join(PREDICT_DIR, MODEL_NAME, run_date, asset_type, stock_code, run_time + suffix)
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
        if asset_type == "stk":
            try:
                run_scraper(stock_code)
            except Exception as e:
                write_log(f"⚠️ 爬虫执行异常: {e}")
            news_content, news_links, news_file_paths = get_latest_news_content(stock_code)

        tick_content = fetch_realtime_quote_with_retry(stock_code)

        # 构建工作目录: predict/{model}/{date}/{type}/{code}/{HHMMSS}/tools/
        work_dir = os.path.join(run_dir, "tools")
        # Claude 会话持久化
        claude_session_id = ""

        # 融合上下文
        type_label = {"stk": "股票", "etf": "ETF", "idx": "指数"}.get(asset_type, "标的")
        full_context = f"{news_content}\n\n{tick_content}" if news_content else tick_content

        write_log(f"=== {asset_type.upper()} {stock_code} 分析进程启动 ===\n{tick_content}")

        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

        system_prompt = f"你是一个顶级的量化事件驱动交易员，具有极强的文本分析与指标融合能力。当前分析标的类型: {type_label}。"
        if predict_ex_knowledge:
            system_prompt += f"\n\n请**严格参考**并运用以下系统复盘知识库中的避雷与盈利法则：\n{predict_ex_knowledge}"
            write_log(f"\n[此部分进入模型上下文] 👉 (已挂载历史复盘知识库文件: {predict_ex_filename})")

        if news_links:
            write_log(f"[此部分进入模型上下文] 👉 提取到的本地新闻附件链接:\n  - " + "\n  - ".join(news_links))
        
        if asset_type == 'idx':
            trading_plan_str = (
                "--- 宏观/大盘应对参考 ---\n"
                "【核心支撑/阻力】大盘关键点位或均线，跌破/突破则上述趋势判断失效。\n"
                "【盘面验证信号】明日重点观察的领涨跌板块、量能变化或外部催化剂。\n"
                "【系统性风险评估】结合整体环境，给出当前市场情绪及整体仓位建议（如：满仓做多/半仓轮动/防守观望/空仓）。\n"
            )
        else:
            trading_plan_str = (
                "--- 实战交易计划 ---\n"
                "【逻辑失效价位】具体价格或关键均线，跌破则上述理由完全失效，必须止损。\n"
                "【盘中验证信号】明日开盘需重点观察的量价特征或外部催化剂。\n"
                "【盈亏比与仓位】结合胜率和赔率，给出买卖评级（强烈看多/谨慎试错/观望/清仓）及建议仓位比例。\n"
            )

        user_initial_prompt = f"""运用你所有的金融知识，分析以下行情摘要{'与新闻' if asset_type == 'stk' else ''}，推测{type_label} {stock_code} 未来的走势。
你可以调用外部助手 Claude 来获取你想补充的行情、财务、资金等数据,（它内置了 Tushare MCP,也可以读取Tushare Skills）。
当前时间为{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}，
最近的交易日情况如下（按时间顺序）：
- {dates_info['ts_dates']['prev_td']} (最近交易日的前一个交易日)
- {dates_info['ts_dates']['data_td']} (最近的交易日)
- {dates_info['ts_dates']['next_td']} (后一个交易日)
请务必使用能获取到的最新的数据进行分析。

规则：
1. 请求数据用：<claude>指令</claude>
2. 必须使用qfq的数据
3. 使用数据时有时间参数时要带上end_date,保证end_date日期为历史可以获取到的日期
4. 若获取到的数据不够分析，可以继续请求数据，直到你认为有足够的信息做出判断为止，严禁推测数据和使用约等不精确数据的说法，必要时利用claude 使用python脚本来精确计算
5. 【数据通信规则】你只能通过终端文本看到 Claude 的回复(不能通过其他方式获取到他的回复，如.md等)以及通过csv文件获取数据。如果你请求了超过20日的数据，请务必在 `<claude>指令</claude>` 中强制要求它：“请在工作目录下生成一个名为 `_query_meta.json` 的文件(或变更此内容)，里面必须记录所有此次生成的 csv 文件名（使用 `file` 作为键名，值必须是包含这些文件名的数组/列表）”，否则直接令其输出即可。系统仅会传输首次生成或后续发生变化的文件内容。未变化文件不会重复传输。如需更新数据，请覆盖原csv或生成新csv。
6. 你最多有 5 次请求数据的机会。
6. 预测时间段参考（你必须使用这些精确日期标签，禁止使用"明日/未来"等模糊词）：例如
   - 1日目标日期: {dates_info['label_1d']}
   - 3日目标范围: {dates_info['label_3d']}
   - 5日目标范围: {dates_info['label_5d']}
7. 最终你必须输出包含分析加上明确结果的标签（例子如下所示），且注意输出有以下不变规则：
             1.【xx方向】涨 (或者 跌/平)只允许这三个。
             2.涨跌幅区间：【禁止伪精度】除非涨跌幅由明确公式计算得到，否则所有预测涨跌幅只能使用整数%或0.5%步长(如-5%,-4.5%,4.5%,5%)。禁止出现任何其它小数形式(如1.3%,1.7%,2.8%,4.2%全部禁止)。
             3.信心打分(0-10,整数)
             4.理由为你的核心逻辑，理由要明确主谓宾

<result>
【标的】{stock_code}
【{dates_info['label_1d']} 方向】涨 (或者 跌/平) 【信心】y 
【{dates_info['label_1d']} 涨跌幅】x% ~ x% 【信心】y
【{dates_info['label_3d']} 涨跌幅】x% ~ x% 【信心】y
【{dates_info['label_5d']} 涨跌幅】x% ~ x% 【信心】y
【1日逻辑理由】z
【3日逻辑理由】z
【5日逻辑理由】z
{trading_plan_str.strip()}
8.每次输出先分析再请求1个<claude>指令</claude>或1个<result>结果</result>,不能多个。

"""     
        # 因为上下文很长，不在 log 中写完整 user prompt，只给大模型：
        write_log(f"\n[此部分进入模型上下文] sysem prompt:\n{system_prompt}\n\n[此部分进入模型上下文] user prompt:\n{user_initial_prompt}...\n")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_initial_prompt + f"\n[{'新闻数据和' if asset_type == 'stk' else '' } 实时数据如下：]\n{full_context}"}
        ]
        
        max_steps = 5
        sent_files = {}
        for step in range(1, max_steps + 1):
            write_log(f"\n🧠 [DeepSeek 思考中... 第 {step}/{max_steps} 步]")
            
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.7
            )
            
            ds_reply = response.choices[0].message.content or ""
            reasoning = getattr(response.choices[0].message, "reasoning_content", "")
            
            log_text = "[DeepSeek 输出]:\n"
            if reasoning:
                log_text += f"<think>\n{reasoning}\n</think>\n"
            log_text += ds_reply
            
            write_log(log_text)
            messages.append({"role": "assistant", "content": ds_reply})

            # 优先处理 <claude>：DeepSeek 可能一次输出 <claude> + <result>，
            # 必须先执行工具调用，避免跳过 Claude 直接收幻觉生成的 result
            claude_match = re.search(r'<claude>(.*?)</claude>', ds_reply, re.DOTALL)
            if claude_match:
                claude_instruction = claude_match.group(1).strip()
                data_text, tool_log, new_session_id = run_claude_code(
                    f"你好，请使用 TushareMCP/Tushare Skills :\n'{claude_instruction}'",
                    work_dir=work_dir,
                    news_files=news_file_paths,
                    session_id=claude_session_id,
                )
                if new_session_id:
                    claude_session_id = new_session_id
                tools_used.append(f"\n第{step}次数据查询结果\n{claude_instruction}") if not data_text.startswith("Claude 执行失败") and not data_text.startswith("Claude 执行超时") else None
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
                                            csv_contents.append(f"==== 文件: {os.path.basename(fpath)} ====\n{df.to_string()}\n")
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
                rel_news = [os.path.relpath(p, BASE_DIR) for p in news_file_paths]
                return {"stock": stock_code, "asset_type": asset_type, "status": "success", "prediction": result_match.group(1).strip(), "tools": tools_used, "news_paths": rel_news}

            if step < max_steps:
                messages.append({"role": "user", "content": "请遵守输出格式，需要数据给 <claude>指令</claude>，预测请给 <result>预测内容格式</result>"})
        
        rel_news = [os.path.relpath(p, BASE_DIR) for p in news_file_paths]
        return {"stock": stock_code, "asset_type": asset_type, "status": "timeout", "prediction": "未能生成标准判断。", "tools": tools_used, "news_paths": rel_news}

    except Exception as e:
        err_msg = f"进程异常导致失败: {e}"
        write_log(err_msg)
        return {"stock": stock_code, "asset_type": asset_type, "status": "error", "prediction": err_msg, "tools": tools_used, "news_paths": []}

def main(stk_codes, etf_codes, idx_codes):
    """
    入口函数，控制全局复盘、并发协程分发和主报表生成
    stk_codes/etf_codes/idx_codes: 各自的代码列表
    """
    # 收集所有任务: (code, asset_type)
    all_tasks = []
    for code in (stk_codes or []):
        all_tasks.append((code, "stk"))
    for code in (etf_codes or []):
        all_tasks.append((code, "etf"))
    for code in (idx_codes or []):
        all_tasks.append((code, "idx"))

    if not all_tasks:
        print("[X] 请至少指定 --stk / --etf / --idx 中的一个")
        return

    type_names = []
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

    # 第2步：开启多进程执行预测
    print("\n================ 进入多进程智脑分析循环 ================")
    results = []

    max_workers = min(len(all_tasks), multiprocessing.cpu_count(), 3)

    # Windows spawn 模式下 Ctrl+C 会传给子进程干扰 import，先屏蔽
    original_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with multiprocessing.Pool(processes=max_workers) as pool:
            signal.signal(signal.SIGINT, original_sigint)  # 主进程恢复 SIGINT
            tasks = [(code, atype, predict_ex_knowledge, latest_ex_filename, run_date, run_time)
                     for code, atype in all_tasks]
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
    model_root = os.path.join(PREDICT_DIR, MODEL_NAME)
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
            tool_str = "、".join(res['tools']) if res['tools'] else "无"
            rf.write(f"- **调用工具记录**: {tool_str}\n")
            news_str = "、".join(res.get('news_paths', [])) if res.get('news_paths') else "无"
            rf.write(f"- **可读取新闻路径**: {news_str}\n\n")
            rf.write("### AI 模型结论：\n")
            rf.write(f"```text\n{res['prediction']}\n```\n")
            rf.write("""\n注意：不变规则：
             1.【xx方向】涨 (或者 跌/平)只允许这三个。
             2.涨跌幅区间：【禁止伪精度】除非涨跌幅由明确公式计算得到，否则所有预测涨跌幅只能使用整数%或0.5%步长(如-5%,-4.5%,4.5%,5%)。禁止出现任何其它小数形式(如1.3%,1.7%,2.8%,4.2%全部禁止)。
             3.信心打分(0-10,整数)
             4.理由为你的核心逻辑，理由要明确主谓宾
            """)                        
            rf.write("---\n\n")

    print(f"✅ 执行结束！输出目录: {os.path.join(model_root, run_date)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="综合智能投研 Agent 多标的打分系统")
    parser.add_argument("--stk", type=str, nargs="*", default=None, help="股票代码列表")
    parser.add_argument("--etf", type=str, nargs="*", default=None, help="ETF代码列表")
    parser.add_argument("--idx", type=str, nargs="*", default=None, help="指数代码列表")
    args = parser.parse_args()
    main(args.stk, args.etf, args.idx)