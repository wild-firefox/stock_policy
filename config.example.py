"""
配置文件 — 使用前将其复制一份为config.py并填入你的 API Token
"""
import os
import logging
from datetime import datetime
from pathlib import Path

# ============ 动态计算相对项目根目录 ============
PROJECT_ROOT = Path(__file__).resolve().parent

# ============ API Token 配置 ============
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "YOUR_TUSHARE_TOKEN")
BOCHA_API_KEY = os.environ.get("BOCHA_API_KEY", "YOUR_BOCHA_API_KEY")


# ============ 统一全局数据存储路径 ============
DATA_DIR = PROJECT_ROOT / "data_manager" / "data"

STK_RAW_DIR = DATA_DIR / "stk" / "raw"
STK_RAW_H5_DIR = DATA_DIR / "stk" / "raw_h5"
ETF_RAW_DIR = DATA_DIR / "etf" / "raw"
ETF_RAW_H5_DIR = DATA_DIR / "etf" / "raw_h5"
IDX_RAW_DIR = DATA_DIR / "idx" / "raw"

LOG_DIR = DATA_DIR / "log"

# 新闻抓取路径
NEWS_RAW_DIR = PROJECT_ROOT / "news_manager" / "data" / "stk" / "raw_ths"
NEWS_BOCHA_DIR = PROJECT_ROOT / "news_manager" / "data" / "stk" / "raw_Bocha"
NEWS_JYGS_DIR = PROJECT_ROOT / "news_manager" / "data" / "jygs" / "pqjy"

# 股票列表路径
A_SHARES_LIST_CSV = PROJECT_ROOT / "a_shares_list.csv"

# ============ 初始化目录 ============
def init_dirs():
    for d in [STK_RAW_DIR, STK_RAW_H5_DIR, ETF_RAW_DIR, IDX_RAW_DIR, LOG_DIR, NEWS_RAW_DIR, NEWS_BOCHA_DIR, NEWS_JYGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)



# ============ 全局运行参数 ============
API_SLEEP = 0.3          # 正常请求间隔（秒）
RATE_LIMIT_SLEEP = 30    # 触发频率限制后等待（秒）
MAX_RETRY = 3            # 单只股票最大重试次数

def get_pro():
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()



# ============ 日志 ============
def setup_logger(name="fetch"):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = LOG_DIR / f"{name}_{datetime.now().strftime('%Y-%m-%d')}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s,%(msecs)03d [%(levelname)s] %(filename)s:%(lineno)d %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger

# ============ 保存的列 ============
SAVE_COLS = [
    "trade_date",
    "open", "high", "low", "close",
    "change", "pct_chg",
    "vol", "amount",
    "turnover_rate", "turnover_rate_f", "volume_ratio",
    "pe", "pe_ttm", "pb", "ps", "ps_ttm",
    "dv_ratio", "dv_ttm",
    "total_share", "float_share", "free_share",
    "total_mv", "circ_mv",
    "adj_factor",
    "updays", "downdays", "topdays", "lowdays",
    "ma_bfq_5", "ma_bfq_10", "ma_bfq_20", "ma_bfq_30",
    "ma_bfq_60", "ma_bfq_90", "ma_bfq_250",
    "ema_bfq_5", "ema_bfq_10", "ema_bfq_20", "ema_bfq_30",
    "ema_bfq_60", "ema_bfq_90", "ema_bfq_250",
    "macd_dif_bfq", "macd_dea_bfq", "macd_bfq",
    "kdj_k_bfq", "kdj_d_bfq", "kdj_bfq",
    "rsi_bfq_6", "rsi_bfq_12", "rsi_bfq_24",
    "boll_upper_bfq", "boll_mid_bfq", "boll_lower_bfq",
    "cci_bfq",
    "asi_bfq", "asit_bfq",
    "atr_bfq", "bbi_bfq",
    "bias1_bfq", "bias2_bfq", "bias3_bfq",
    "brar_ar_bfq", "brar_br_bfq",
    "cr_bfq",
    "dfma_dif_bfq", "dfma_difma_bfq",
    "dmi_adx_bfq", "dmi_adxr_bfq", "dmi_mdi_bfq", "dmi_pdi_bfq",
    "dpo_bfq", "madpo_bfq",
    "emv_bfq", "maemv_bfq",
    "expma_12_bfq", "expma_50_bfq",
    "ktn_upper_bfq", "ktn_mid_bfq", "ktn_down_bfq",
    "mass_bfq", "ma_mass_bfq",
    "mfi_bfq",
    "mtm_bfq", "mtmma_bfq",
    "obv_bfq",
    "psy_bfq", "psyma_bfq",
    "roc_bfq", "maroc_bfq",
    "taq_up_bfq", "taq_mid_bfq", "taq_down_bfq",
    "trix_bfq", "trma_bfq",
    "vr_bfq",
    "wr_bfq", "wr1_bfq",
    "xsii_td1_bfq", "xsii_td2_bfq", "xsii_td3_bfq", "xsii_td4_bfq",
    "open_qfq", "high_qfq", "low_qfq", "close_qfq",
    "ma_qfq_5", "ma_qfq_10", "ma_qfq_20", "ma_qfq_30",
    "ma_qfq_60", "ma_qfq_90", "ma_qfq_250",
    "ema_qfq_5", "ema_qfq_10", "ema_qfq_20", "ema_qfq_30",
    "ema_qfq_60", "ema_qfq_90", "ema_qfq_250",
    "macd_dif_qfq", "macd_dea_qfq", "macd_qfq",
    "kdj_k_qfq", "kdj_d_qfq", "kdj_qfq",
    "rsi_qfq_6", "rsi_qfq_12", "rsi_qfq_24",
    "boll_upper_qfq", "boll_mid_qfq", "boll_lower_qfq",
    "cci_qfq",
    "asi_qfq", "asit_qfq",
    "atr_qfq", "bbi_qfq",
    "bias1_qfq", "bias2_qfq", "bias3_qfq",
    "brar_ar_qfq", "brar_br_qfq",
    "cr_qfq",
    "dfma_dif_qfq", "dfma_difma_qfq",
    "dmi_adx_qfq", "dmi_adxr_qfq", "dmi_mdi_qfq", "dmi_pdi_qfq",
    "dpo_qfq", "madpo_qfq",
    "emv_qfq", "maemv_qfq",
    "expma_12_qfq", "expma_50_qfq",
    "ktn_upper_qfq", "ktn_mid_qfq", "ktn_down_qfq",
    "mass_qfq", "ma_mass_qfq",
    "mfi_qfq",
    "mtm_qfq", "mtmma_qfq",
    "obv_qfq",
    "psy_qfq", "psyma_qfq",
    "roc_qfq", "maroc_qfq",
    "taq_up_qfq", "taq_mid_qfq", "taq_down_qfq",
    "trix_qfq", "trma_qfq",
    "vr_qfq",
    "wr_qfq", "wr1_qfq",
    "xsii_td1_qfq", "xsii_td2_qfq", "xsii_td3_qfq", "xsii_td4_qfq",
]

SAVE_ETF_COLS = [
    'trade_date', 'open', 'high', 'low', 'close',
    'change', 'pct_chg', 'vol', 'amount', 'adj_factor'
]

# ============ 回测引擎及构建数据专用的列 (剥离了 _qfq 等多余列) ============
BACKTEST_BASE = [
    'trade_date','open','high','low','close','change','pct_chg','vol','amount',
    'turnover_rate','turnover_rate_f','volume_ratio','pe','pe_ttm','pb','ps','ps_ttm',
    'dv_ratio','dv_ttm','total_share','float_share','free_share','total_mv','circ_mv','adj_factor',
    'updays','downdays','topdays','lowdays','ma_bfq_5','ma_bfq_10','ma_bfq_20','ma_bfq_30',
    'ma_bfq_60','ma_bfq_90','ma_bfq_250','ema_bfq_5','ema_bfq_10','ema_bfq_20','ema_bfq_30',
    'ema_bfq_60','ema_bfq_90','ema_bfq_250','macd_dif_bfq','macd_dea_bfq','macd_bfq',
    'kdj_k_bfq','kdj_d_bfq','kdj_bfq','rsi_bfq_6','rsi_bfq_12','rsi_bfq_24','boll_upper_bfq',
    'boll_mid_bfq','boll_lower_bfq','cci_bfq','asi_bfq','asit_bfq','atr_bfq','bbi_bfq',
    'bias1_bfq','bias2_bfq','bias3_bfq','brar_ar_bfq','brar_br_bfq','cr_bfq','dfma_dif_bfq',
    'dfma_difma_bfq','dmi_adx_bfq','dmi_adxr_bfq','dmi_mdi_bfq','dmi_pdi_bfq','dpo_bfq',
    'madpo_bfq','emv_bfq','maemv_bfq','expma_12_bfq','expma_50_bfq','ktn_upper_bfq',
    'ktn_mid_bfq','ktn_down_bfq','mass_bfq','ma_mass_bfq','mfi_bfq','mtm_bfq','mtmma_bfq',
    'obv_bfq','psy_bfq','psyma_bfq','roc_bfq','maroc_bfq','taq_up_bfq','taq_mid_bfq',
    'taq_down_bfq','trix_bfq','trma_bfq','vr_bfq','wr_bfq','wr1_bfq',
    'xsii_td1_bfq','xsii_td2_bfq','xsii_td3_bfq','xsii_td4_bfq'
]

BACKTEST_ETF_BASE = [
    'trade_date', 'open', 'high', 'low', 'close',
    'change', 'pct_chg', 'vol', 'amount', 'adj_factor'
]

# ============ 股票板块过滤函数 ============
def is_excluded(ts_code: str, exclude_boards: list = ['gem', 'star', 'bj']) -> bool:
    """根据股票代码判断是否需要排除
    排除板块，可选值：'gem' (创业板), 'star' (科创板), 'bj' (北交所)
    return True 表示该股票在排除的板块中，不应被处理或回测；返回 False 则表示该股票应被保留。
    """

    if 'gem' in exclude_boards and ts_code.startswith(('30')):
        return True
    if 'star' in exclude_boards and ts_code.startswith('688'):
        return True
    if 'bj' in exclude_boards and ts_code.startswith((("92", "43", "81", "82", "83", "87", "88"))):
        return True

    return False

def get_latest_h5_path(base_dir: str, filename: str = "market_data.h5") -> str:
    """
    在指定的 base_dir 目录下寻找以日期命名的最新文件夹（如 20260506 或 20000104_20260416），
    并返回完整的 h5 文件路径。
    """
    if not os.path.exists(base_dir):
        return ""

    # 筛选出目录下所有以数字开头的文件夹（符合日期特征）
    dirs = [d for d in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, d)) and d and d[0].isdigit()]

    if not dirs:
        return ""

    # 利用字典序降序排序：'20260506' 会排在 '20260501' 前面
    # 同理 '20000104_20260416' 会排在 '20000104_20250416' 前面
    dirs.sort(reverse=True)
    latest_dir = dirs[0]

    return os.path.join(base_dir, latest_dir, filename)
