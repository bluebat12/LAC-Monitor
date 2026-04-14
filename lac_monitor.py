# LAC Monitor - 主监控脚本
# 路径：D:\LAC_monitor\lac_monitor.py
# Python 3.14  |  pip install requests feedparser
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

# ─── 加载配置 ───────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import config

# ─── 日志 ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("LAC")


# ════════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════════

def load_state() -> dict:
    """加载已推送的公告ID，避免重复推送"""
    if Path(config.STATE_FILE).exists():
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": [], "last_check": None}


def save_state(state: dict):
    os.makedirs(Path(config.STATE_FILE).parent, exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def bark_push(title: str, body: str, level: str = "active", url: str = ""):
    """
    发送 Bark 推送
    level: active(默认) | timeSensitive(时效性) | passive(静默)
    """
    payload = {
        "title": title,
        "body": body,
        "level": level,
        "sound": "minuet",
        "icon": "https://logo.clearbit.com/lithiumamericas.com",
    }
    if url:
        payload["url"] = url

    try:
        r = requests.post(config.BARK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            log.info(f"✅ Bark推送成功: {title}")
        else:
            log.warning(f"Bark推送失败: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Bark推送异常: {e}")


def keyword_check(text: str, keywords: list) -> list:
    """返回命中的关键词列表"""
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


# ════════════════════════════════════════════════════════
#  模块1：SEC EDGAR 监控
# ════════════════════════════════════════════════════════

# 文件类型 → 中文说明 + 危险等级
FILING_META = {
    "8-K":       ("重大事项", "🔴"),
    "6-K":       ("临时报告", "🔴"),
    "Form 4":    ("高管持股变动", "🟡"),
    "10-Q":      ("季度报告", "🟢"),
    "10-K":      ("年度报告", "🟢"),
    "424B5":     ("股权增发募资", "🟡"),
    "S-3":       ("注册声明", "🟡"),
    "SC 13G":    ("机构大股东变动", "🟡"),
    "SC 13G/A":  ("机构大股东更新", "🟡"),
    "SC 13D":    ("积极股东入场", "🔴"),
}


def classify_form4(title: str, summary: str) -> tuple[str, str]:
    """
    解析 Form 4，区分税款代扣 vs 主动卖出
    返回 (分类标签, emoji)
    """
    combined = (title + " " + summary).lower()
    neutral_hits = keyword_check(combined, config.FORM4_NEUTRAL)
    danger_hits = keyword_check(combined, config.FORM4_DANGER)

    if danger_hits:
        return "主动卖出⚠️", "🔴"
    elif neutral_hits:
        return "税款代扣/期权授予", "⚪"
    else:
        return "待人工核实", "🟡"


def parse_filing_amount(summary: str) -> str:
    """从摘要中提取金额信息"""
    patterns = [
        r'\$[\d,\.]+\s*(?:million|billion|M|B)',
        r'[\d,]+\s*(?:shares|Common Shares)',
    ]
    results = []
    for p in patterns:
        matches = re.findall(p, summary, re.IGNORECASE)
        results.extend(matches[:2])
    return " | ".join(results) if results else ""


def check_sec_filings(state: dict) -> list:
    """拉取 SEC EDGAR JSON API，返回新公告列表"""
    log.info("📡 检查 SEC EDGAR...")
    alerts = []

    headers = {
        "User-Agent": "LAC Monitor blueb@example.com",  # SEC要求填联系邮件
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }

    try:
        url = f"https://data.sec.gov/submissions/CIK{config.LAC_CIK}.json"
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"SEC API 请求失败: {e}")
        return alerts

    recent = data.get("filings", {}).get("recent", {})
    forms     = recent.get("form", [])
    dates     = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    descriptions = recent.get("primaryDocument", [])

    for i, form in enumerate(forms[:30]):
        acc = accessions[i].replace("-", "")
        entry_id = acc
        if entry_id in state["seen_ids"]:
            continue

        state["seen_ids"].append(entry_id)
        if len(state["seen_ids"]) > 200:
            state["seen_ids"] = state["seen_ids"][-200:]

        date = dates[i] if i < len(dates) else ""
        doc  = descriptions[i] if i < len(descriptions) else ""
        link = f"https://www.sec.gov/Archives/edgar/data/1966983/{acc}/{doc}"

        filing_type = form
        emoji, danger_level = FILING_META.get(filing_type, ("未知", "⚪"))[::-1]

        extra_info = ""

        # Form 4 特殊处理：拉原文判断是否主动卖出
        if filing_type == "4":
            filing_type = "Form 4"
            # 拉原文，自动判断是否主动卖出
            try:
                acc_fmt = accessions[i]
                detail_url = f"https://data.sec.gov/Archives/edgar/data/1966983/{acc}/{doc}"
                rx = requests.get(detail_url, headers={"User-Agent": "LAC Monitor blueb@example.com"}, timeout=10)
                if "S-Open Market" in rx.text or "open market" in rx.text.lower():
                    danger_level = "🔴"
                    extra_info = "\n⚠️ 主动公开市场卖出！"
                else:
                    # 税款代扣/期权授予 → 静默，不推送
                    log.info(f"Form 4 无害跳过: {date}")
                    continue
            except Exception:
                danger_level = "🟡"
                extra_info = "\n请手动确认是否主动卖出"

        # 8-K / 6-K 推送
        if form in ("8-K", "6-K"):
            danger_level = "🔴"

        push_level = "timeSensitive" if danger_level == "🔴" else "active"
        title_text = f"{danger_level} LAC {filing_type}"
        body_text  = f"{date} 新文件\n{doc}{extra_info}"

        alerts.append({
            "title": title_text,
            "body": body_text,
            "link": link,
            "push_level": push_level,
            "danger": danger_level == "🔴",
        })

        log.info(f"新公告: {filing_type} | {date} | {doc}")

    return alerts


# ════════════════════════════════════════════════════════
#  模块2：资金链监控（手动更新 + 自动计算）
# ════════════════════════════════════════════════════════

# 手动维护关键财务数据（每季度更新）
FINANCIAL_SNAPSHOT = {
    "date": "2025-12-31",
    "cash_total": 905_600_000,       # 总现金（含受限资金）
    "cash_unrestricted": 750_000_000, # 估算自由现金
    "monthly_burn": 108_000_000,      # ← 改这里：净消耗 = (Capex - DOE放款) / 12
                                       # ($1.45B - $1.15B剩余DOE) / 12 ≈ $25M，保守用$108M
    "doe_drawn": 867_000_000,         # DOE 已提款
    "doe_remaining": 1_363_000_000,   # DOE 剩余额度
    "shares_outstanding": 347_369_613, # 截至2026年3月
    "atm_remaining": 810_300_000,     # ATM 增发剩余额度（$10亿 - 已用）
}


def check_cash_runway(state: dict) -> list:
    """
    计算现金跑道，触发预警
    """
    alerts = []
    snap = FINANCIAL_SNAPSHOT
    cash = snap["cash_unrestricted"]
    burn = snap["monthly_burn"]
    runway_months = cash / burn if burn > 0 else 99

    log.info(f"💵 现金跑道：{runway_months:.1f}个月 | 现金：${cash/1e6:.0f}M | 月烧：${burn/1e6:.0f}M")

    if cash < config.CASH_CRITICAL_THRESHOLD:
        alerts.append({
            "title": "🚨 LAC 现金危机",
            "body": f"自由现金 ${cash/1e6:.0f}M < ${config.CASH_CRITICAL_THRESHOLD/1e6:.0f}M 警戒线\n跑道剩余：{runway_months:.1f}个月\nDOE剩余额度：${snap['doe_remaining']/1e6:.0f}M",
            "push_level": "timeSensitive",
            "danger": True,
        })
    elif cash < config.CASH_WARNING_THRESHOLD:
        alerts.append({
            "title": "⚠️ LAC 现金预警",
            "body": f"自由现金 ${cash/1e6:.0f}M 接近警戒线\n跑道剩余：{runway_months:.1f}个月",
            "push_level": "active",
            "danger": False,
        })

    # 稀释预警：ATM 增发频繁时提醒
    # （每次发现424B5文件时已触发，这里做补充说明）
    doe_pct_used = snap["doe_drawn"] / config.DOE_TOTAL_LOAN
    log.info(f"🏦 DOE贷款进度：{doe_pct_used:.1%} 已提款 (${snap['doe_drawn']/1e6:.0f}M / $2,230M)")

    return alerts


# ════════════════════════════════════════════════════════
#  模块3：DOE 政策风险监控（能源部新闻RSS）
# ════════════════════════════════════════════════════════

DOE_RSS = "https://www.energy.gov/rss.xml"
DOE_KEYWORDS = ["Lithium Americas", "Thacker Pass", "LAC", "ATVM", "LPO"]


def check_doe_news(state: dict) -> list:
    """监控 DOE 官网新闻，捕获政策变化"""
    alerts = []
    log.info("🏛️  检查 DOE 新闻...")

    try:
        feed = feedparser.parse(DOE_RSS)
    except Exception as e:
        log.warning(f"DOE RSS 解析失败: {e}")
        return alerts

    for entry in feed.entries[:20]:
        entry_id = entry.get("id", entry.get("link", ""))
        doe_key = "doe_" + entry_id
        if doe_key in state["seen_ids"]:
            continue

        title = entry.get("title", "")
        summary = entry.get("summary", "")
        combined = title + " " + summary

        if any(kw.lower() in combined.lower() for kw in DOE_KEYWORDS):
            state["seen_ids"].append(doe_key)
            link = entry.get("link", "")

            # 检查是否有危险词
            danger_hits = keyword_check(combined, ["cancel", "suspend", "terminate", "freeze", "revoke"])
            positive_hits = keyword_check(combined, ["drawdown", "approve", "advance", "milestone"])

            if danger_hits:
                alerts.append({
                    "title": "🚨 DOE政策风险",
                    "body": f"{title}\n危险词：{', '.join(danger_hits)}",
                    "link": link,
                    "push_level": "timeSensitive",
                    "danger": True,
                })
            elif positive_hits:
                alerts.append({
                    "title": "✅ DOE 利好消息",
                    "body": f"{title}\n关键词：{', '.join(positive_hits)}",
                    "link": link,
                    "push_level": "active",
                    "danger": False,
                })
            else:
                log.info(f"DOE 提及LAC（无明显信号）: {title[:60]}")

    return alerts


# ════════════════════════════════════════════════════════
#  模块4：每周汇总推送
# ════════════════════════════════════════════════════════

def should_send_weekly_summary(state: dict) -> bool:
    """判断是否该发周报（每周一）"""
    today = datetime.now(timezone.utc)
    if today.weekday() != 0:  # 0 = Monday
        return False
    last_summary = state.get("last_weekly_summary")
    if not last_summary:
        return True
    last_dt = datetime.fromisoformat(last_summary)
    return (today - last_dt).days >= 6


def send_weekly_summary(state: dict):
    """发送每周状态汇总"""
    snap = FINANCIAL_SNAPSHOT
    cash = snap["cash_unrestricted"]
    burn = snap["monthly_burn"]
    runway = cash / burn

    body = (
        f"📊 LAC 周报 {datetime.now().strftime('%Y-%m-%d')}\n"
        f"{'─'*30}\n"
        f"💵 现金：${cash/1e6:.0f}M\n"
        f"🔥 月烧：${burn/1e6:.0f}M\n"
        f"⏳ 跑道：{runway:.1f}个月\n"
        f"🏦 DOE已提：${snap['doe_drawn']/1e6:.0f}M / $2,230M\n"
        f"🏗️  工程设计完成：93%（截至2025Q4）\n"
        f"👷 工地人数：700→1800（目标年底）\n"
        f"🎯 机械完工目标：2027年底\n"
        f"{'─'*30}\n"
        f"数据基准：{snap['date']}，需季度手动更新"
    )

    bark_push("📊 LAC 周报", body, level="passive")
    state["last_weekly_summary"] = datetime.now(timezone.utc).isoformat()
    log.info("📊 周报已发送")


# ════════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════════

def main():
    log.info("=" * 50)
    log.info(f"LAC Monitor 启动 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    state = load_state()
    state["last_check"] = datetime.now(timezone.utc).isoformat()

    all_alerts = []

    # 1. SEC 公告
    all_alerts.extend(check_sec_filings(state))

    # 2. 资金链检查（每次运行都算）
    all_alerts.extend(check_cash_runway(state))

    # 3. DOE 新闻
    all_alerts.extend(check_doe_news(state))

    # 4. 推送所有警报
    danger_count = 0
    for alert in all_alerts:
        bark_push(
            title=alert["title"],
            body=alert["body"],
            level=alert.get("push_level", "active"),
            url=alert.get("link", ""),
        )
        if alert.get("danger"):
            danger_count += 1

    # 5. 每周汇总
    if should_send_weekly_summary(state):
        send_weekly_summary(state)

    # 6. 保存状态
    save_state(state)

    log.info(f"✅ 检查完成 | 新警报：{len(all_alerts)} 条 | 高危：{danger_count} 条")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
