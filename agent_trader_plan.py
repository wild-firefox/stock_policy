import os
import re
import json
import glob
from datetime import datetime
from openai import OpenAI

import config
from position_manager import get_my_positions
from agent_predict import get_predict_target_dates

def parse_eval_history(filepath):
    """
    解析 predict_eval_history.md，提取每个标的最新的预测逻辑
    """
    if not os.path.exists(filepath):
        print(f"找不到 {filepath}")
        return {}

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    pattern = r'### 预测时间: (.*?)\n\*\*【标的】(\w+) (.*?)\*\*(.*?)</details>'
    matches = re.finditer(pattern, content, re.DOTALL)

    predictions = {}
    for match in matches:
        pred_time_str = match.group(1).strip()
        code = match.group(2).strip()
        name = match.group(3).strip()
        details = match.group(4).strip()

        # 清理 html 标签
        details = re.sub(r'<.*?>', '', details).strip()

        # 只保留每个标的最新的一次预测
        if code not in predictions:
            predictions[code] = {
                "time": pred_time_str,
                "name": name,
                "logic": details
            }
        else:
            if pred_time_str > predictions[code]["time"]:
                predictions[code] = {
                    "time": pred_time_str,
                    "name": name,
                    "logic": details
                }

    return predictions

def get_latest_reflection(exp_dir):
    """获取最新的复盘知识库"""
    pattern = os.path.join(exp_dir, 'predict_ex_*.md')
    files = glob.glob(pattern)
    if not files:
        return ""
    files.sort()
    latest_file = files[-1]
    with open(latest_file, 'r', encoding='utf-8') as f:
        return f.read()

def generate_all_trading_plans(valid_predictions, positions, account_summary, reflection_rules):
    """调用大模型一次性生成所有股票的条件交易单"""
    client = OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1"
    )

    total_assets = account_summary.get('总资产', '未知')
    available_cash = account_summary.get('可用金额', '未知')

    system_prompt = f"""你是一个顶级的量化交易执行基金经理。
你的任务是将【多个研究员的宏观预测逻辑】结合【账户真实仓位】，一次性转化为明天盘中的【严格条件交易单列表】。

你必须输出一段严格的 JSON 数组（List格式）。
【JSON 格式约定】（数组中包含多个对象）：
[
  {{
    "stock": "601138",
    "stock_name": "工业富联",
    "current_position": 1100,
    "cost_price": 71.85,
    "action_rules": [
      {{
        "time_range": ["09:30", "10:30"],
        "condition": "current_price > 75.0 and get_macd()['macd'] < 0",
        "action": "SELL",
        "ratio": 0.5,
        "reason": "早盘冲高且 MACD 翻绿，逢高减仓一半"
      }}
    ],
    "default_action": "HOLD"  // 若无仓位且未触发买入条件，则观望
  }}
]

【规则约束】：
1. ratio 表示占操作基数的比例（卖出时指占现有持仓的比例，买入时指占账户总资产的比例）。
2. condition 必须是合法的 Python 表达式！
允许使用的变量（不需要用大括号包裹，直接作为变量名使用，例如 current_price > cost_price，或者提取预测文中具体的数字，如 current_price > 10.5）：
- current_price (当前最新价)
- cost_price (持仓成本价，如果没有仓位则该变量不可用于比较)
- open_price (开盘价)
允许使用的抽象函数：
- get_macd(days_ago=0) 返回字典如 {{"dif":0.1, "dea":0.05, "macd":0.02}}。如果需要表达“MACD柱扩大/缩小”，必须通过对比当日和前一日的值来实现，例如：get_macd(0)['macd'] < get_macd(1)['macd'] (表示绿柱扩大或红柱缩小)
- get_ma(days) 返回均线价，如 get_ma(5)
以及类似的这种函数 get_dif()、get_dea() 等
严禁捏造未声明的变量和函数！
3. 如果当前某只股票持仓为 0，只能生成 action="BUY" 的规则，此时 cost_price 请输出 0。
4. 如果有持仓，可以生成 SELL 或加仓 BUY 规则。
"""

    stocks_info_text = ""
    for code, pdata in valid_predictions.items():
        pos = positions.get(code)
        has_pos = pos is not None
        curr_pos = pos.get('股票余额', '0') if has_pos else '0'
        cost_price = pos.get('参考成本价', '0') if has_pos else '0'

        stocks_info_text += f"""
---
【标的】：{code} {pdata['name']}
当前持仓：{curr_pos} 股
持仓成本：{cost_price}
预测时间：{pdata['time']}
【预测逻辑】：
{pdata['logic']}
"""

    user_prompt = f"""
当前账户总资产：{total_assets}
当前可用资金：{available_cash}

【历史复盘避雷规则参考】：
{reflection_rules}... (已截断)

以下是需要生成交易计划的所有标的预测信息与仓位情况：
{stocks_info_text}

请一次性输出上述所有标的（请包含 JSON 数组格式）交易计划：
"""

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1
        )
        result_text = response.choices[0].message.content.strip()
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.startswith("```"):
            result_text = result_text[3:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]

        return json.loads(result_text.strip())
    except Exception as e:
        print(f"批量生成计划失败: {e}")
        return []

def main():
    print("1. 正在获取真实账户仓位...")
    my_data = get_my_positions()
    if my_data:
        account_summary = my_data.get('account_summary', {})
        positions = {str(p.get('证券代码')): p for p in my_data.get('positions', [])}
        print(f"成功获取仓位：总资产 {account_summary.get('总资产')}，持仓股票 {len(positions)} 只。")
    else:
        print("未能获取到仓位，将默认空仓状态生成计划。")
        account_summary = {}
        positions = {}

    exp_dir = os.path.join(config.PROJECT_ROOT, "predict", "deepseek-v4-pro", "experience")
    eval_history_path = os.path.join(exp_dir, "predict_eval_history.md")

    print("2. 正在解析历史预测和复盘规则...")
    predictions = parse_eval_history(eval_history_path)
    reflection_rules = get_latest_reflection(exp_dir)

    if not predictions:
        print("未找到任何历史预测")
        return

    # 参考 agent_predict 获取“今天可以预测的最新的交易日”
    target_dates = get_predict_target_dates()
    target_1d_date = target_dates.get("label_1d", "")
    print(f"根据交易日历计算出的最新单日预测目标交易日: {target_1d_date}")

    if not target_1d_date or target_1d_date == "?":
        print("未能计算出有效的目标交易日，退出。")
        return

    valid_predictions = {}
    for code, pdata in predictions.items():
        # 必须是针对该交易日的最新【单日预测】（匹配“【YYYY-MM-DD 方向】”），防止误匹配到往期预测的3日或5日目标区间
        if f"【{target_1d_date} 方向】" in pdata['logic']:
            valid_predictions[code] = pdata

    print(f"找到 {len(valid_predictions)} 个属于最新交易日的标的，正在一次性请求大模型生成计划...")

    trade_dir = os.path.join(config.PROJECT_ROOT, "trade")
    os.makedirs(trade_dir, exist_ok=True)

    all_plans = generate_all_trading_plans(valid_predictions, positions, account_summary, reflection_rules)

    # 输出到 JSON
    if all_plans:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = os.path.join(trade_dir, f"trading_plan_{timestamp}.json")
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_plans, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 全部交易计划已生成，保存在: {out_file}")
    else:
        print("\n❌ 未生成任何有效的交易计划。")

if __name__ == "__main__":
    main()
