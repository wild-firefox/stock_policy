import os
import glob
import re
from datetime import datetime
import pandas as pd
import sys
import config

'''此文件为生成预测复盘统计md文件所用，可供其他文件导入调用'''

NOTION_PAGE_ID = getattr(config, "NOTION_PAGE_ID", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_pro
pro = get_pro()

REFERENCE_KB_FOOTER_RE = re.compile(
    r'<!--\s*参考复盘知识库版本\s*[:：]\s*'
    r'(predict_ex_\d{8}_\d{6}\.md)\s*-->'
)


def normalize_reflection_task_id(task_id):
    """把复盘任务 ID 统一为评估器使用的固定格式。"""
    task_id = str(task_id).strip()
    canonical_match = re.fullmatch(
        r'(1|3|5)日任务:\s*(stk|etf|idx)\s+([A-Za-z0-9_]+)\s*\((\d{8})\)',
        task_id,
        re.IGNORECASE,
    )
    if canonical_match:
        days, atype, code, target_date = canonical_match.groups()
        return f"{days}日任务: {atype.lower()} {code} ({target_date})"

    legacy_match = re.fullmatch(
        r'TASK_\d+_(1D|3D|5D)_(STK|ETF|IDX)_([A-Za-z0-9_]+)_(\d{8})',
        task_id,
        re.IGNORECASE,
    )
    if legacy_match:
        period, atype, code, target_date = legacy_match.groups()
        days = period[:-1]
        return f"{days}日任务: {atype.lower()} {code} ({target_date})"

    return task_id


def _format_rules_markdown(rules_text):
    """把规则连续渲染为引用行，不插入空引用段落。"""
    return "\n".join(
        f"> {line}" for line in rules_text.splitlines() if line.strip()
    )

def _get_ts_code(code, atype):
    if atype == 'stk':
        if code.startswith('6'): return f"{code}.SH"
        if code.startswith('8') or code.startswith('4'): return f"{code}.BJ"
        return f"{code}.SZ"
    if atype == 'etf':
        return f"{code}.SH" if code.startswith('5') else f"{code}.SZ"
    if atype == 'idx':
        return f"{code}.SZ" if code.startswith('3') else f"{code}.SH"
    return f"{code}.SZ"

name_cache = {}
def get_stock_name(code):
    global name_cache
    if not name_cache:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            df_a = pd.read_csv(os.path.join(base_dir, "a_shares_list.csv"), dtype={'symbol': str})
            for _, row in df_a.iterrows():
                name_cache[row['symbol']] = row['name']
        except: pass
        try:
            df_etf = pd.read_csv(os.path.join(base_dir, "etf_list.csv"), dtype={'ts_code': str})
            for _, row in df_etf.iterrows():
                sym = row['ts_code'].split('.')[0]
                name_cache[sym] = row['extname']
        except: pass
    return name_cache.get(code, "")

def parse_prediction_file(file_path):
    """解析单个预测 md 文件中的预测数据"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 只处理成功状态的
    if "- **状态**: success" not in content:
        return None

    # 提取基本信息
    code_m = re.search(r'【标的】\s*([A-Za-z0-9_]+)(?:\s+([^【\n]+))?', content)
    type_m = re.search(r'- \*\*类型\*\*: (\w+)', content)
    if not code_m or not type_m:
        return None

    code = code_m.group(1)
    name_from_md = code_m.group(2).strip() if code_m.group(2) else ""
    name = name_from_md or get_stock_name(code)
    atype = type_m.group(1)

    # 提取时间(作为基准时间)
    time_m = re.search(r'预测报告 \((\d{8}) (\d{6})\)', content)
    if not time_m:
        return None
    report_date = time_m.group(1)
    report_time = time_m.group(2)

    reference_kb_match = REFERENCE_KB_FOOTER_RE.search(content)
    reference_kb = reference_kb_match.group(1) if reference_kb_match else ""

    # 提取预测项
    # 【2026-06-16 方向】跌 【信心】8
    # 【2026-06-16 涨跌幅】-2% ~ -4% 【信心】8
    # 【2026-06-16~2026-06-18 涨跌幅】-4% ~ -6% 【信心】7

    predictions = []

    # 解析方向
    dir_matches = re.finditer(r'【(\d{4}-\d{2}-\d{2})\s*方向】\s*([涨跌平]+)(?:.*?【信心】\s*(\d+))?', content)
    for m in dir_matches:
        target_date = m.group(1)
        direction = m.group(2)
        conf = m.group(3) if m.group(3) else "?"
        predictions.append({
            'type': 'direction',
            'category': 'direction',
            'target_start': target_date,
            'target_end': target_date,
            'value': direction,
            'conf': conf,
            'original_str': m.group(0)
        })

    # 解析涨跌幅 (单日)
    pct1_matches = re.finditer(r'【(\d{4}-\d{2}-\d{2})\s*涨跌幅】\s*([^【\n]+)(?:.*?【信心】\s*(\d+))?', content)
    for m in pct1_matches:
        target_date = m.group(1)
        val = m.group(2).strip()
        conf = m.group(3) if m.group(3) else "?"
        predictions.append({
            'type': 'pct_chg',
            'category': '1d',
            'target_start': target_date,
            'target_end': target_date,
            'value': val,
            'conf': conf,
            'original_str': m.group(0)
        })

    # 解析涨跌幅 (多日范围)
    pct2_matches = list(re.finditer(r'【(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})\s*涨跌幅】\s*([^【\n]+)(?:.*?【信心】\s*(\d+))?', content))
    for i, m in enumerate(pct2_matches):
        start_date = m.group(1)
        end_date = m.group(2)
        val = m.group(3).strip()
        conf = m.group(4) if m.group(4) else "?"
        predictions.append({
            'type': 'pct_chg_range',
            'category': '3d' if i == 0 else '5d',
            'target_start': start_date,
            'target_end': end_date,
            'value': val,
            'conf': conf,
            'original_str': m.group(0)
        })

    if not predictions:
        return None

    # 提取实战交易计划及逻辑理由
    plan_match = re.search(r'(【\d+日逻辑理由】|---\s*(?:实战交易计划|宏观/大盘应对参考)\s*---)(.*?)```', content, re.DOTALL)
    trading_plan_title = "详细逻辑与交易计划"
    trading_plan_content = None
    if plan_match:
        trading_plan_content = (plan_match.group(1) + plan_match.group(2)).strip()

    return {
        'code': code,
        'name': name,
        'atype': atype,
        'report_date': report_date,
        'report_time': report_time,
        'file_path': file_path,
        'reference_kb': reference_kb,
        'predictions': predictions,
        'plan_title': trading_plan_title,
        'plan_content': trading_plan_content
    }

def fetch_actual_data(ts_code, atype, start_ymd):
    """获取标的的实际行情"""
    today_str = datetime.now().strftime('%Y%m%d')
    start_ymd_str = start_ymd.replace("-", "")

    if atype == 'etf':
        df = pro.fund_daily(ts_code=ts_code, start_date=start_ymd_str, end_date=today_str)
    elif atype == 'idx':
        df = pro.index_daily(ts_code=ts_code, start_date=start_ymd_str, end_date=today_str)
    else:
        df = pro.daily(ts_code=ts_code, start_date=start_ymd_str, end_date=today_str)

    if df is None or df.empty:
        return None

    df = df.sort_values('trade_date').reset_index(drop=True)
    return df

def parse_pct_range(val_str):
    """解析涨跌幅范围，如 '-2% ~ -4%' -> (-4.0, -2.0)"""
    val_str = val_str.replace('%', '').replace('％', '')
    parts = val_str.split('~')
    if len(parts) == 2:
        try:
            v1 = float(parts[0].strip())
            v2 = float(parts[1].strip())
            return min(v1, v2), max(v1, v2)
        except:
            pass
    return None, None

def evaluate_prediction(pred, df):
    """评估单条预测结果"""
    if len(df) == 0:
        return "暂无结果", "[暂无结果]"

    # 获取目标开始与结束日期
    target_start_str = pred.get('target_start', pred['target_end']).replace("-", "")
    target_end_str = pred['target_end'].replace("-", "")

    # 寻找起始日当天或之后的数据，以其第一天的昨收作为基准价格
    start_df = df[df['trade_date'] >= target_start_str]
    if len(start_df) == 0:
        return "暂无结果", "[暂无结果]"

    base_price = start_df.iloc[0].get('pre_close')
    if base_price is None or base_price <= 0:
        return "暂无结果", "[暂无结果]"

    # 寻找结束日期当天或之前的数据
    target_df = df[df['trade_date'] <= target_end_str]

    if len(target_df) == 0:
        # 说明目标时间还没开始
        return "暂无结果", "[暂无结果]"

    actual_end_date = target_df.iloc[-1]['trade_date']
    if actual_end_date < target_end_str:
        # 还没到结束日
        # 判断今天是不是大于结束日期，如果系统时间都没到结束日期，就是暂无结果
        today_str = datetime.now().strftime('%Y%m%d')
        if today_str < target_end_str:
             return "暂无结果", "[暂无结果]"
        # 否则如果是节假日，取节假日前最后一个交易日的数据作为结果？这里暂定取最后一天的数据

    end_price = target_df.iloc[-1]['close']
    actual_pct = (end_price / base_price - 1) * 100

    actual_dir = "涨" if actual_pct > 0 else ("跌" if actual_pct < 0 else "平")

    if pred['type'] == 'direction':
        actual_str = f"实际方向: {actual_dir}"
        if pred['value'] == actual_dir:
            result = "[对]"
        else:
            result = "[错]"
        return actual_str, result

    elif pred['type'] in ['pct_chg', 'pct_chg_range']:
        actual_str = f"实际涨跌幅: {actual_pct:+.2f}%"
        min_v, max_v = parse_pct_range(pred['value'])
        if min_v is not None and max_v is not None:
            # 判断是否落在区间内
            if min_v <= actual_pct <= max_v:
                result = "[对]"
            else:
                result = "[错]"
        else:
            result = "[解析失败]"
        return actual_str, result

    return "未知", "[未知]"


def _new_stats():
    """创建一套全新的胜率统计容器。"""
    return {
        'direction': {
            'total': 0,
            'correct': 0,
            'flat_as_win_correct': 0,
            'flat_as_loss_correct': 0,
            'exclude_flat_total': 0,
            'exclude_flat_correct': 0,
        },
        '1d': {'total': 0, 'correct': 0},
        '3d': {'total': 0, 'correct': 0},
        '5d': {'total': 0, 'correct': 0},
    }


def _update_stats(stats, pred, actual_str, result_flag):
    """按统一口径累计一条已有结果的预测。"""
    category = pred.get('category')
    if category not in stats:
        return

    category_stats = stats[category]
    category_stats['total'] += 1
    is_correct = result_flag == "[对]"
    if is_correct:
        category_stats['correct'] += 1

    if category == 'direction':
        pred_val = pred.get('value')

        # 提取实际方向
        if "实际方向: 涨" in actual_str:
            actual_dir = "涨"
        elif "实际方向: 跌" in actual_str:
            actual_dir = "跌"
        else:
            actual_dir = "平"

        # 固定业务口径：平算胜时把“平”映射为“涨”，
        # 平算负时把“平”映射为“跌”，再与实际方向比较。
        mapped_pred_win = "涨" if pred_val == "平" else pred_val
        if mapped_pred_win == actual_dir:
            category_stats['flat_as_win_correct'] += 1

        mapped_pred_loss = "跌" if pred_val == "平" else pred_val
        if mapped_pred_loss == actual_dir:
            category_stats['flat_as_loss_correct'] += 1

        # 排除平 (只统计预测胜负)
        if pred_val in ["涨", "跌"]:
            category_stats['exclude_flat_total'] += 1
            if pred_val == actual_dir:
                category_stats['exclude_flat_correct'] += 1


def _rate_tuple(correct, total):
    rate = (correct / total * 100) if total > 0 else 0
    return correct, total, rate


def _direction_rates(stats):
    """返回严格匹配、平算胜、平算负三种方向胜率。"""
    direction = stats['direction']
    total = direction['total']
    strict_correct = direction['correct']
    flat_as_win_correct = direction['flat_as_win_correct']
    flat_as_loss_correct = direction['flat_as_loss_correct']
    exclude_flat_total = direction.get('exclude_flat_total', 0)
    exclude_flat_correct = direction.get('exclude_flat_correct', 0)

    return {
        'strict': _rate_tuple(strict_correct, total),
        'flat_as_win': _rate_tuple(flat_as_win_correct, total),
        'flat_as_loss': _rate_tuple(flat_as_loss_correct, total),
        'exclude_flat': _rate_tuple(exclude_flat_correct, exclude_flat_total),
    }


def _format_stats_markdown(stats):
    """将一套统计格式化为全量和近期窗口共用的 Markdown。"""
    direction_rates = _direction_rates(stats)
    lines = []
    direction_names = [
        ('strict', '大盘/个股方向（严格匹配）'),
        ('flat_as_win', '方向胜率（平算胜）'),
        ('flat_as_loss', '方向胜率（平算负）'),
        ('exclude_flat', '方向胜率（排除平，仅看涨跌）'),
    ]
    for key, name in direction_names:
        correct, total, rate = direction_rates[key]
        lines.append(f"- **{name}**: {rate:.1f}% ({correct}/{total})")

    for category, name in [
        ('1d', '1日涨跌幅'),
        ('3d', '3日涨跌幅'),
        ('5d', '5日涨跌幅'),
    ]:
        total = stats[category]['total']
        correct = stats[category]['correct']
        _, _, rate = _rate_tuple(correct, total)
        lines.append(f"- **{name}**: {rate:.1f}% ({correct}/{total})")
    return "\n".join(lines) + "\n"


def _recent_prediction_dates(report_dates, limit=5):
    """按预测记录中实际出现的日期，返回最近若干个不同日期。"""
    unique_dates = sorted({
        date for date in report_dates
        if isinstance(date, str) and re.fullmatch(r'\d{8}', date)
    })
    return unique_dates[-limit:]


def _format_date_range(report_dates):
    if not report_dates:
        return "无可用预测日期"

    def display(date):
        return f"{date[:4]}-{date[4:6]}-{date[6:]}"

    return f"{display(report_dates[0])} ~ {display(report_dates[-1])}"


def _merge_stats(target, source):
    for category, category_stats in source.items():
        for key, value in category_stats.items():
            target[category][key] += value


def run_evaluation(predict_dir="predict/deepseek-v4-pro",page_id=NOTION_PAGE_ID):
    print("\n================ 启动历史预测准确率回测统计 ================")
    experience_dir = os.path.join(predict_dir, "experience")
    history_md = os.path.join(experience_dir, "predict_eval_history.md")
    os.makedirs(experience_dir, exist_ok=True)

    import json
    global_reflections = {}
    json_pattern = os.path.join(experience_dir, "predict_ex_*", "predict_analysis.json")
    for json_file in glob.glob(json_pattern):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                analysis_payload = json.load(f)
            for key, value in analysis_payload.items():
                normalized_key = normalize_reflection_task_id(key)
                global_reflections[normalized_key] = value
        except Exception as e:
            pass

    # 查找所有的预测 md
    search_pattern = os.path.join(predict_dir, "20*", "*", "*", "*", "*.md")
    all_files = glob.glob(search_pattern)

    # 排除不是标的名称命名的文件 (如过滤掉 history.md 等)
    valid_files = [f for f in all_files if re.match(r'^[A-Za-z0-9_]+\.md$', os.path.basename(f))]

    candidates = {}
    for file_path in valid_files:
        doc = parse_prediction_file(file_path)
        if not doc:
            continue

        predict_ymd = None
        for pred in doc['predictions']:
            if pred['type'] == 'pct_chg':
                predict_ymd = pred['target_start'].replace("-", "")
                break

        if not predict_ymd:
            continue

        try:
            file_dt = datetime.strptime(f"{doc['report_date']}{doc['report_time']}", "%Y%m%d%H%M%S")
        except ValueError:
            continue

        key = (doc['atype'], doc['code'], predict_ymd)
        if key not in candidates or file_dt > candidates[key]['file_dt']:
            doc['file_dt'] = file_dt
            doc['target_date'] = predict_ymd
            candidates[key] = doc

    final_docs = list(candidates.values())
    final_docs.sort(key=lambda x: x['file_dt'], reverse=True)

    # 分组统计按标的+时间分组
    results_by_doc = []

    cache_df = {} # 避免同一个标的同日重复拉取

    stats = _new_stats()
    stats_by_target_date = {}
    completed_direction_dates = set()

    for doc in final_docs:
        target_date_stats = stats_by_target_date.setdefault(
            doc['target_date'], _new_stats()
        )
        ts_code = _get_ts_code(doc['code'], doc['atype'])
        start_ymd = doc['report_date']

        # 获取行情数据
        cache_key = f"{ts_code}_{start_ymd}"
        if cache_key not in cache_df:
            cache_df[cache_key] = fetch_actual_data(ts_code, doc['atype'], start_ymd)

        df = cache_df[cache_key]

        doc_eval_results = []
        is_all_completed = True

        for pred in doc['predictions']:
            actual_str, result_flag = evaluate_prediction(pred, df if df is not None else pd.DataFrame())
            if result_flag == "[暂无结果]":
                is_all_completed = False
            else:
                _update_stats(stats, pred, actual_str, result_flag)
                _update_stats(
                    target_date_stats, pred, actual_str, result_flag
                )
                if pred.get('category') == 'direction':
                    completed_direction_dates.add(doc['target_date'])

            eval_line = f"| {pred['original_str'].replace('|', '｜')} | {actual_str} | **{result_flag}** |"
            doc_eval_results.append(eval_line)

        reflections_to_render = []
        rendered_reflection_keys = set()
        atype_lower = doc['atype'].lower()
        for pred in doc['predictions']:
            if pred['type'] in ['pct_chg', 'pct_chg_range']:
                cat = pred['category']
                target_end_ymd = pred['target_end'].replace("-", "")

                if cat == '1d':
                    key = f"1日任务: {atype_lower} {doc['code']} ({target_end_ymd})"
                    title = "【1日复盘归因】"
                elif cat == '3d':
                    key = f"3日任务: {atype_lower} {doc['code']} ({target_end_ymd})"
                    title = "【3日复盘归因】"
                elif cat == '5d':
                    key = f"5日任务: {atype_lower} {doc['code']} ({target_end_ymd})"
                    title = "【5日复盘归因】"
                else:
                    continue

                if key in rendered_reflection_keys:
                    continue

                ref_data = global_reflections.get(key)
                if ref_data:
                    rendered_reflection_keys.add(key)
                    reflections_to_render.append((title, ref_data))

        results_by_doc.append({
            'code': doc['code'],
            'name': doc.get('name', ''),
            'atype': doc['atype'],
            'target_date': doc['target_date'],
            'report_date': doc['report_date'],
            'report_time': doc['report_time'],
            'reference_kb': doc.get('reference_kb', ''),
            'lines': doc_eval_results,
            'plan_title': doc.get('plan_title'),
            'plan_content': doc.get('plan_content'),
            'reflections': reflections_to_render
        })

    # 写入 Markdown 报表
    with open(history_md, 'w', encoding='utf-8') as f:
        f.write("# 🦊 [wild-firefox/stock_policy](https://github.com/wild-firefox/stock_policy) 智能预测统计报告\n\n")
        f.write(f"> 自动更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        rules_markdown = _format_rules_markdown(
            config.PREDICTION_OUTPUT_RULES
        )
        f.write("## 📏 预测输出统一规则\n\n")
        f.write(f"{rules_markdown}\n\n")

        f.write("## 🌌 宇宙级免责声明\n\n")
        f.write(
            "> 本报告由 AI 基于历史数据和公开信息自动生成，仅供研究、学习与娱乐，"
            "不构成任何投资建议、交易指令、收益承诺或法律、财务意见。数据可能存在延迟、"
            "缺失或错误，模型可能发生严重误判。任何投资决策及由此产生的盈亏均由使用者自行承担。"
            "无论市场波动、停牌、政策变化、黑天鹅、系统故障、网络异常、太阳风暴，还是其他地球及"
            "宇宙尺度的不可抗力，项目作者、模型和数据提供方均不承担责任。\n\n"
        )

        completed_dates = sorted(completed_direction_dates)
        global_range = _format_date_range(completed_dates)
        f.write(
            f"## 💡 预测胜率全局统计（{global_range}，排除暂无结果）\n\n"
        )
        f.write(_format_stats_markdown(stats))

        recent_dates = _recent_prediction_dates(
            completed_dates,
            limit=5,
        )
        recent_stats = _new_stats()
        for target_date in recent_dates:
            _merge_stats(
                recent_stats,
                stats_by_target_date.get(target_date, _new_stats()),
            )

        recent_range = _format_date_range(recent_dates)
        f.write(
            f"\n## 📅 最近5个预测日胜率统计"
            f"（{recent_range}）\n\n"
        )
        f.write(
            f"> 按已经产生实际方向结果的一日预测目标日期，取最近 "
            f"{len(recent_dates)} 个不同交易日统计；不是按报告生成日期或当前日期倒推，"
            "排除暂无结果。\n\n"
        )
        f.write(_format_stats_markdown(recent_stats))

        f.write("\n---\n\n")

        for doc in results_by_doc:
            date_fmt = f"{doc['report_date'][:4]}-{doc['report_date'][4:6]}-{doc['report_date'][6:]}"
            time_fmt = f"{doc['report_time'][:2]}:{doc['report_time'][2:4]}:{doc['report_time'][4:]}"

            f.write(f"### 预测时间: {date_fmt} {time_fmt}\n")
            if doc.get('reference_kb'):
                f.write(
                    f"<!-- 参考复盘知识库版本: {doc['reference_kb']} -->\n"
                )

            # 渲染标的名称（如果存在）
            title_str = f"**【标的】{doc['code']}"
            if doc.get('name'):
                title_str += f" {doc['name']}"
            title_str += "**\n\n"
            f.write(title_str)

            f.write("| 预测内容 | 实际情况 | 回测结果 |\n")
            f.write("| --- | --- | --- |\n")
            for line in doc['lines']:
                f.write(f"{line}\n")

            if doc.get('plan_content'):
                f.write(f"\n<details>\n<summary> **{doc['plan_title']}** (点击展开)</summary>\n\n")
                # 使用 blockquote (>) 替代 ```text，避免手机端出现横向滚动条，支持自动换行
                quoted_plan = "\n".join(f"> {line}" for line in doc['plan_content'].split('\n'))
                f.write(f"{quoted_plan}\n\n")
                f.write("</details>\n")

            if doc.get('reflections'):
                f.write(f"\n<details open>\n<summary> **AI 深度复盘记录** (点击展开)</summary>\n\n")
                for title, ref_data in doc['reflections']:
                    f.write(f"**{title}**\n")
                    if ref_data.get('reflection'):
                        f.write(f"- **方向与涨跌幅**: {ref_data['reflection']}\n")

                    has_plans = any(ref_data.get(k) for k in ['plan_1', 'plan_2', 'plan_3'])
                    if has_plans:
                        f.write(f"- **计划分析**:\n")
                        if ref_data.get('plan_1'):
                            p1_label = "核心支撑/阻力" if doc['atype'] == 'idx' else "逻辑失效价位"
                            f.write(f"  - 【{p1_label}】: {ref_data['plan_1']}\n")
                        if ref_data.get('plan_2'):
                            p2_label = "盘面验证信号" if doc['atype'] == 'idx' else "盘中验证信号"
                            f.write(f"  - 【{p2_label}】: {ref_data['plan_2']}\n")
                        if ref_data.get('plan_3'):
                            p3_label = "系统性风险评估" if doc['atype'] == 'idx' else "盈亏比与仓位"
                            f.write(f"  - 【{p3_label}】: {ref_data['plan_3']}\n")
                    f.write("\n")
                f.write("</details>\n")

            f.write("\n---\n\n")

    print(f"[+] 回测统计完成！报告已保存至 {history_md}")

    if not page_id:
        print("提示: 未配置 NOTION_PAGE_ID，已跳过 Notion 网页同步。")
        return

    try:
        import notion_sync_one
        print("正在同步到 Notion...")
        notion_sync_one.sync_md_to_notion(
            history_md,
            page_id,
            max_prediction_dates=20,
        )
    except ImportError:
        print("找不到 notion_sync_one，跳过 Notion 同步")
    except Exception as e:
        print(f"Notion 同步失败: {e}")

if __name__ == '__main__':
    run_evaluation(predict_dir="predict/deepseek-v4-pro",page_id=NOTION_PAGE_ID)
