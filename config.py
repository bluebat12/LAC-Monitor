# LAC Monitor - 配置文件
# 路径：D:\finance\LAC_monitor\config.py

# ─── Bark 推送（iPhone）───
import os
BARK_KEY = os.environ.get("BARK_KEY", "")
BARK_URL = f"{os.environ.get('BARK_SERVER', 'https://api.day.app')}/{BARK_KEY}"

# ─── LAC 基本信息 ───
LAC_CIK = "0001966983"
LAC_TICKER = "LAC"

# ─── SEC EDGAR RSS ───
SEC_RSS_URL = (
    f"https://www.sec.gov/cgi-bin/browse-edgar"
    f"?action=getcompany&CIK={LAC_CIK}"
    f"&type=&dateb=&owner=include&count=20&output=atom"
)

# ─── 高危 Form 4 关键词（主动卖出）───
FORM4_DANGER = [
    "S-Open Market",       # 公开市场卖出
    "open market sale",
    "Open Market Sale",
]

# ─── 高危 8-K / 6-K 关键词 ───
FILING_DANGER_KEYWORDS = [
    "resignation",         # 高管辞职
    "terminate",           # 终止协议
    "default",             # 违约
    "waiver",              # 豁免申请（通常伴随违约风险）
    "cease",               # 停工
    "stop work",
    "Notice of Violation", # NDEP 违规
    "material adverse",    # 重大不利变化
    "draw stop",           # DOE 停止放款
    "suspend",             # 暂停
]

# ─── 中性/无害 Form 4 关键词（可忽略）───
FORM4_NEUTRAL = [
    "Tax withholding",
    "tax-withholding",
    "grant",
    "award",
    "F-InKind",            # 税款代扣标准代码
]

# ─── 利好关键词 ───
POSITIVE_KEYWORDS = [
    "drawdown",            # DOE 新放款
    "mechanical completion",
    "commissioning",
    "first production",
    "offtake",             # 新销售协议
]

# ─── 资金链预警阈值（美元）───
CASH_WARNING_THRESHOLD = 300_000_000   # $3亿，低于触发警报
CASH_CRITICAL_THRESHOLD = 150_000_000  # $1.5亿，触发紧急警报

# ─── DOE 贷款总额（用于计算剩余额度）───
DOE_TOTAL_LOAN = 2_230_000_000   # $22.3亿
DOE_DRAWN_TO_DATE = 867_000_000  # 截至2026年2月已提款（手动更新）

# ─── 本地状态文件（记录已推送的公告，避免重复）───
STATE_FILE = "D:\\finance\\LAC_monitor\\seen_filings.json"

# ─── 日志文件 ───
LOG_FILE = "D:\\finance\\LAC_monitor\\monitor.log"
