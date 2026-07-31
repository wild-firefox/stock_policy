import time
import os
import subprocess
import cv2
import numpy as np
from PIL import ImageGrab
from pywinauto import Application
from rapidocr_onnxruntime import RapidOCR
import re
import json
import base64
from openai import OpenAI
import config

'''通过ths 远航版获取个人仓位'''

# 1. 初始化 OCR 引擎 (使用 RapidOCR，完美兼容 Windows 和 numpy 2.x，不闪退)
ocr = RapidOCR(det_use_dml=True, cls_use_dml=True, rec_use_dml=True)

# 优先级从上至下 先视觉模型识别，失败后回退文本解析
POSITION_VISION_MODEL = "gemini-3.1-flash-image-preview-free" # AIHUBMIX_API_KEY
POSITION_TEXT_PARSE_CLI_MODEL = "deepseek-v4-flash" #"grok-4.5" #"deepseek-v4-flash" # cli
POSITION_TEXT_PARSE_API_MODEL = "deepseek-v4-flash" # DEEPSEEK_API_KEY


CLAUDE_EXE = os.environ.get("CLAUDE_EXE", "claude")

def bring_window_to_front(exe_path=None):
    """激活并前置同花顺交易窗口"""
    import pyautogui
    target_pid = None
    import psutil
    for p in psutil.process_iter(['pid', 'name']):
        if p.info['name'] and 'happ' in p.info['name'].lower():
            target_pid = p.info['pid']
            break

    if target_pid:
        try:
            print(f"已找到同花顺主进程 happ.exe (PID: {target_pid})，尝试连接...")
            app = Application(backend="uia").connect(process=target_pid)
            win = app.top_window()
            win.set_focus()
            time.sleep(0.5)
            print("成功前置同花顺远航版主窗口！")
        except Exception as e:
            print(f"按PID激活/点击窗口失败: {e}")
            return False
    else:
        print("未找到同花顺远航版进程！")
        return False

    return win

def capture_roi(bbox):
    """
    截取感兴趣区域 (Region of Interest)
    bbox 格式: (left, top, right, bottom)
    """
    img_pil = ImageGrab.grab(bbox=bbox)
    img_cv = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    return img_cv


def call_ai_to_parse(top_img_path, table_img_path):
    def encode_image(image_path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    base64_top = encode_image(top_img_path)
    base64_table = encode_image(table_img_path)

    client = OpenAI(
        api_key=config.AIHUBMIX_API_KEY,
        base_url="https://aihubmix.com/v1",
    )

    prompt = """
请提取这两张图片中的持仓数据，并严格按照以下 JSON 格式输出，不要包含任何 markdown 代码块或其他说明文字。
你需要从第一张图（顶部资金区）提取以下字段：
资金余额, 冻结金额, 可用金额, 股票市值, 总资产, 持仓盈亏

从第二张图（下方表格区）提取以下列的每一行数据：
证券代码, 证券名称, 股票余额, 可用余额, 冻结数量, 参考成本价, 市价, 参考盈亏比, 参考盈亏, 市值, 仓位占比

请确保数字格式正确，去除逗号，百分比保留为小数或原始字符串均可。

输出示例：
{
  "account_summary": {
    "资金余额": "10213.81",
    "冻结金额": "0.00",
    "可用金额": "10213.81",
    "股票市值": "83479.00",
    "总资产": "93692.81",
    "持仓盈亏": "-9893.56"
  },
  "positions": [
    {
      "证券代码": "601138",
      "证券名称": "工业富联",
      "股票余额": "1100",
      "可用余额": "1100",
      "冻结数量": "0",
      "参考成本价": "71.853",
      "市价": "63.130",
      "参考盈亏比": "-12.14",
      "参考盈亏": "-9589.19",
      "市值": "69443.00",
      "仓位占比": "71.14"
    }
  ]
}
"""
    print("正在调用 AI 进行图像识别...")
    try:
        response = client.chat.completions.create(
            model=POSITION_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_top}"}},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_table}"}}
                    ]
                }
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
        print(f"AI JSON 解析失败: {e}")
        return None

def _build_position_text_prompt(top_rows, table_rows):
    """把 OCR 行数据整理为持仓 JSON 提取提示词。"""
    top_str = "\n".join(["\t".join([item['text'] for item in row]) for row in top_rows])
    table_str = "\n".join(["\t".join([item['text'] for item in row]) for row in table_rows])

    return f"""
请提取以下由 OCR 识别的文本中的持仓数据，并严格按照以下 JSON 格式输出，不要包含任何 markdown 代码块或其他说明文字。

【顶部资金区文本】：
{top_str}

【下方表格区文本】：
{table_str}

你需要提取以下字段：
资金余额, 冻结金额, 可用金额, 股票市值, 总资产, 持仓盈亏

以及表格中每行证券的信息：
证券代码, 证券名称, 股票余额, 可用余额, 冻结数量, 参考成本价, 市价, 参考盈亏比, 参考盈亏, 市值, 仓位占比

输出示例：
{{
  "account_summary": {{
    "资金余额": "10213.81",
    "冻结金额": "0.00",
    "可用金额": "10213.81",
    "股票市值": "83479.00",
    "总资产": "93692.81",
    "持仓盈亏": "-9893.56"
  }},
  "positions": [
    {{
      "证券代码": "601138",
      "证券名称": "工业富联",
      "股票余额": "1100",
      "可用余额": "1100",
      "冻结数量": "0",
      "参考成本价": "71.853",
      "市价": "63.130",
      "参考盈亏比": "-12.14",
      "参考盈亏": "-9589.19",
      "市值": "69443.00",
      "仓位占比": "71.14"
    }}
  ]
}}
"""


def _parse_position_json(result_text):
    """清理模型可能返回的代码块或说明文字并解析 JSON。"""
    result_text = result_text.strip()
    fenced_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        result_text,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced_match:
        result_text = fenced_match.group(1)

    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        json_start = result_text.find("{")
        json_end = result_text.rfind("}")
        if json_start < 0 or json_end <= json_start:
            raise
        parsed = json.loads(result_text[json_start:json_end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("持仓解析结果不是 JSON 对象")
    return parsed


def _run_claude_text_parser(prompt, model_name):
    """通过 Claude CLI 解析 OCR 文本并返回原始文本结果。"""
    env = os.environ.copy()
    env["TUSHARE_TOKEN"] = config.TUSHARE_TOKEN
    # 强制覆盖当前环境变量，确保 Claude Code 读取到指定的模型。
    env["ANTHROPIC_MODEL"] = model_name

    cmd = [
        CLAUDE_EXE,
        "-p",
        "--input-format", "text",
        "--output-format", "text",
        "--model", model_name,
    ]
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        shell=True,
    )
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "未知错误").strip()
        raise RuntimeError(
            f"Claude CLI 执行失败 (Exit Code {result.returncode}): "
            f"{error_text}"
        )
    if not result.stdout.strip():
        raise ValueError("Claude CLI 未返回任何文本")
    return result.stdout.strip()


def _run_deepseek_text_parser(prompt, model_name):
    """通过 DeepSeek API 解析 OCR 文本并返回原始文本结果。"""
    client = OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    return response.choices[0].message.content.strip()


def call_text_ai_to_parse(
    top_rows,
    table_rows,
    cli_model=POSITION_TEXT_PARSE_CLI_MODEL,
    api_model=POSITION_TEXT_PARSE_API_MODEL,
):
    """默认使用 Claude CLI 解析，失败后回退 DeepSeek API。"""
    prompt = _build_position_text_prompt(top_rows, table_rows)

    print("[*] 正在使用 Claude CLI 解析持仓 OCR 文本...")
    try:
        claude_text = _run_claude_text_parser(prompt, cli_model)
        return _parse_position_json(claude_text)
    except Exception as claude_error:
        print(f"[!] Claude CLI 解析失败，回退 DeepSeek API: {claude_error}")

    try:
        api_text = _run_deepseek_text_parser(prompt, api_model)
        return _parse_position_json(api_text)
    except Exception as api_error:
        print(f"[X] DeepSeek API 文本解析失败: {api_error}")
        return None

def parse_table_data(ocr_results, img=None, app_window=None):
    """
    根据识别到的锚点（"证券代码", "汇总" 等），动态截取并解析表格数据和顶部资金数据
    ocr_results: RapidOCR 返回的列表 [[[x1,y1,x2,y2,x3,y3,x4,y4], text, conf], ...]
    """
    header_y = None
    bottom_y = None
    table_left_cx = 9999
    table_right_cx = 0
    top_y_anchor = 0

    for line in ocr_results:
        box = line[0]
        text = line[1]

        if "证券代码" in text:
            header_y = min(pt[1] for pt in box)

        if "汇总" in text:
            bottom_y = max(pt[1] for pt in box)

        if "资金余额" in text:
            table_left_cx = min(table_left_cx, min(pt[0] for pt in box) )
            top_y_anchor = min(pt[1] for pt in box) - 20

        if "仓位占比" in text:
            table_right_cx = max(table_right_cx, max(pt[0] for pt in box) )

    if table_left_cx == 9999:
        table_left_cx = 0
    if table_right_cx == 0:
        table_right_cx = 9999

    if header_y is None or bottom_y is None:
        print("未找到完整的表格边界锚点 ('证券代码', '汇总')，判定为非持仓页面。")
        return None

    known_headers = [
        "证券代码", "证券名称", "股票余额", "可用余额", "冻结数量",
        "参考成本价", "市价", "参考盈亏比", "参考盈亏", "市值", "仓位占比"
    ]
    matched_headers_count = 0
    for line in ocr_results:
        text = line[1]
        for kh in known_headers:
            if kh in text:
                matched_headers_count += 1

    if matched_headers_count < 3:
        print(f"警告：虽然找到了锚点，但只匹配到 {matched_headers_count} 个表头，判定为误识别！")
        return None

    table_width = table_right_cx - table_left_cx
    if table_right_cx != 9999 and table_width < 500:
        print(f"警告：检测到的表格宽度仅为 {table_width:.1f} 像素，这不符合 18 列宽表的物理特征！")
        return None

    print(f"动态计算表格区域: X轴 {table_left_cx:.1f} 到 {table_right_cx if table_right_cx != 9999 else 'INF'}, Y轴 {header_y:.1f} 到 {bottom_y:.1f}")

    if img is not None:
        try:
            crop_left = int(max(0, table_left_cx))
            crop_right = int(table_right_cx) if table_right_cx != 9999 else img.shape[1]
            crop_top = int(max(0, header_y ))
            crop_bottom = int(bottom_y)

            if crop_bottom > crop_top and crop_right > crop_left:
                debug_img = img[crop_top:crop_bottom, crop_left:crop_right]
                cv2.imwrite("debug_ocr_boundaries.png", debug_img)
                print("已将核心表格区域裁剪并保存为: debug_ocr_boundaries.png")
            else:
                print("计算的表格边界无效，跳过裁剪。")
        except Exception as e:
            print(f"保存调试裁剪图失败: {e}")

    top_texts = []
    table_texts = []
    bottom_texts = []

    for line in ocr_results:
        box = line[0]
        text = line[1]
        cy = sum([pt[1] for pt in box]) / 4.0
        cx = sum([pt[0] for pt in box]) / 4.0

        if cx < table_left_cx or cx > table_right_cx:
            continue

        item = {'text': text, 'cx': cx, 'cy': cy, 'box': box}

        if cy < header_y - 20:
            top_texts.append(item)
        elif header_y - 20 <= cy <= bottom_y:
            table_texts.append(item)
        else:
            bottom_texts.append(item)

    def cluster_to_rows(items, y_tolerance=10):
        items.sort(key=lambda x: x['cy'])
        rows = []
        current_row = []
        for item in items:
            if not current_row:
                current_row.append(item)
            else:
                row_cy = sum(x['cy'] for x in current_row) / len(current_row)
                if abs(item['cy'] - row_cy) < y_tolerance:
                    current_row.append(item)
                else:
                    current_row.sort(key=lambda x: x['cx'])
                    rows.append(current_row)
                    current_row = [item]
        if current_row:
            current_row.sort(key=lambda x: x['cx'])
            rows.append(current_row)
        return rows

    top_rows = cluster_to_rows(top_texts)
    table_rows = cluster_to_rows(table_texts, y_tolerance=15)
    bottom_rows = cluster_to_rows(bottom_texts)

    if img is not None:
        try:
            top_y = int(top_y_anchor) if 0 < top_y_anchor < header_y - 20 else 0
            bottom_y_bound = int(max(0, header_y - 20))
            left_x = int(table_left_cx) if 0 < table_left_cx < img.shape[1] else 0
            right_bound = int(table_right_cx) if table_right_cx != 9999 else img.shape[1]

            if right_bound <= left_x:
                left_x = 0
                right_bound = img.shape[1]

            if bottom_y_bound > top_y:
                top_crop = img[top_y:bottom_y_bound, left_x:right_bound]
                cv2.imwrite("debug_ocr_top_crop.png", top_crop)
                print("已专门截取顶部数据区域，保存为: debug_ocr_top_crop.png")

                print("\n[清理工作] 两张识别图已保存，提前最小化主窗口...")
                if app_window:
                    try:
                        app_window.minimize()
                        print("成功使用纯代码 win.minimize() 瞬间最小化窗口！")
                    except Exception as e:
                        print(f"原生最小化失败: {e}")
                print("[清理工作] 提前最小化完成！")

                result = call_ai_to_parse("debug_ocr_top_crop.png", "debug_ocr_boundaries.png")
                if not result:
                    print("视觉模型解析失败或未识别到内容，回退到 DeepSeek 文本解析...")
                    result = call_text_ai_to_parse(top_rows, table_rows)

                if result:
                    with open("my_positions.json", "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    print("已将图片解析结果保存至 my_positions.json")
                return result
            else:
                print(f"警告：无法截取顶部区域，计算的边界无效 (top_y={top_y}, bottom_y_bound={bottom_y_bound})")
                result = call_text_ai_to_parse(top_rows, table_rows)
                if result:
                    with open("my_positions.json", "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    print("已将文本解析结果保存至 my_positions.json")
                return result
        except Exception as e:
            print(f"顶部二次截取识别失败: {e}")
            result = call_text_ai_to_parse(top_rows, table_rows)
            if result:
                with open("my_positions.json", "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print("已将文本解析结果保存至 my_positions.json")
            return result


def get_my_positions(use_mock_images=False, use_mock_json=False):
    if use_mock_json:
        try:
            with open("my_positions.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            print("已成功从本地 my_positions.json 读取仓位信息。")
            return data
        except Exception as e:
            print(f"读取本地 JSON 失败: {e}")
            return None

    if use_mock_images:
        import os
        if os.path.exists("debug_ocr_top_crop.png") and os.path.exists("debug_ocr_boundaries.png"):
            print("正在使用本地 debug_ocr_top_crop.png 和 debug_ocr_boundaries.png 解析仓位...")
            data = call_ai_to_parse("debug_ocr_top_crop.png", "debug_ocr_boundaries.png")
            if not data:
                print("视觉模型解析失败，对本地图片进行 OCR 并尝试 DeepSeek 文本解析...")
                top_img = cv2.imread("debug_ocr_top_crop.png")
                table_img = cv2.imread("debug_ocr_boundaries.png")

                top_res, _ = ocr(top_img)
                table_res, _ = ocr(table_img)

                top_texts = []
                if top_res:
                    for line in top_res:
                        cx = sum([pt[0] for pt in line[0]]) / 4.0
                        cy = sum([pt[1] for pt in line[0]]) / 4.0
                        top_texts.append({'text': line[1], 'cx': cx, 'cy': cy})

                table_texts = []
                if table_res:
                    for line in table_res:
                        cx = sum([pt[0] for pt in line[0]]) / 4.0
                        cy = sum([pt[1] for pt in line[0]]) / 4.0
                        table_texts.append({'text': line[1], 'cx': cx, 'cy': cy})

                def cluster_to_rows_local(items, y_tolerance=10):
                    items.sort(key=lambda x: x['cy'])
                    rows = []
                    current_row = []
                    for item in items:
                        if not current_row:
                            current_row.append(item)
                        else:
                            row_cy = sum(x['cy'] for x in current_row) / len(current_row)
                            if abs(item['cy'] - row_cy) < y_tolerance:
                                current_row.append(item)
                            else:
                                current_row.sort(key=lambda x: x['cx'])
                                rows.append(current_row)
                                current_row = [item]
                    if current_row:
                        current_row.sort(key=lambda x: x['cx'])
                        rows.append(current_row)
                    return rows

                top_rows = cluster_to_rows_local(top_texts)
                table_rows = cluster_to_rows_local(table_texts, y_tolerance=15)
                data = call_text_ai_to_parse(top_rows, table_rows)

            if data:
                with open("my_positions.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print("已将解析结果保存至 my_positions.json")
            return data
        else:
            print("找不到 debug_ocr_top_crop.png 或 debug_ocr_boundaries.png")
            return None

    app_window = bring_window_to_front()
    if not app_window:
        return None

    import pyautogui
    screen_width, screen_height = pyautogui.size()
    print("正在扫描同花顺主窗口，检查持仓页面是否已打开...")
    rect = app_window.rectangle()
    bbox = (0,0,screen_width,screen_height)
    roi_img = capture_roi(bbox)

    scale_factor = 0.5
    h, w = roi_img.shape[:2]
    small_img = cv2.resize(roi_img, (int(w * scale_factor), int(h * scale_factor)))
    res, _ = ocr(small_img)

    if res:
        for line in res:
            box = line[0]
            for pt in box:
                pt[0] = pt[0] / scale_factor
                pt[1] = pt[1] / scale_factor

        parsed_data = parse_table_data(res, img=roi_img, app_window=app_window)
        if parsed_data:
            print("持仓页面已打开，直接读取成功！")
            return parsed_data

    print(f"未检测到完整的持仓页面数据，尝试点击'{config.THS_WINDOW_TITLE}'标签...")

    target_tab = None
    rect = app_window.rectangle()
    tab_bbox = (rect.left, rect.top, rect.right, rect.top + 200)
    tab_roi_img = capture_roi(tab_bbox)

    print("正在专门对交易窗口的顶部 Tab 栏进行局部 OCR 分析...")
    tab_res, _ = ocr(tab_roi_img)

    # 提取纯文本部分作为搜索词
    search_keyword = config.THS_WINDOW_TITLE.replace('.*', '').replace('^', '').replace('$', '')
    if not search_keyword:
        search_keyword = "证券" # 兜底

    if tab_res:
        for line in tab_res:
            text = line[1]
            if search_keyword in text:
                target_tab = line[0]
                break

    if target_tab:
        center_x = int(sum([pt[0] for pt in target_tab]) / 4) + rect.left
        center_y = int(sum([pt[1] for pt in target_tab]) / 4) + rect.top
        print(f"精准发现标签，点击全局坐标: ({center_x}, {center_y})")
        pyautogui.click(x=center_x, y=center_y)
        time.sleep(1.5)
    else:
        print(f"屏幕上未找到'{search_keyword}'标签！尝试把主窗口最大化...")
        try:
            app_window.maximize()
            app_window.set_focus()
        except Exception as e:
            print(f"原生最大化失败: {e}")
        time.sleep(1.5)

    max_retries = 3
    for attempt in range(max_retries):
        print(f"正在截取交易界面并进行 OCR 分析 (第 {attempt+1} 次)...")
        roi_img = capture_roi(bbox)

        h, w = roi_img.shape[:2]
        small_img = cv2.resize(roi_img, (int(w * scale_factor), int(h * scale_factor)))
        res, _ = ocr(small_img)

        if not res:
            print("未识别到任何文本")
            time.sleep(1)
            continue

        for line in res:
            box = line[0]
            for pt in box:
                pt[0] = pt[0] / scale_factor
                pt[1] = pt[1] / scale_factor

        parsed_data = parse_table_data(res, img=roi_img, app_window=app_window)

        if parsed_data is not None:
            return parsed_data

        print(f"第 {attempt+1} 次解析失败（可能未完全展开），尝试强制最大化窗口...")
        try:
            app_window.maximize()
            app_window.set_focus()
        except Exception as e:
            print(f"最大化失败: {e}")
        time.sleep(1.5)

    return app_window.minimize()



# 测试运行
if __name__ == "__main__":
    # use_mock_images=True 测试一下使用本地的图片和cli
    result = get_my_positions(use_mock_images=True)
    if result:
        print("\n=== 最终结构化持仓数据 ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("解析失败。")
