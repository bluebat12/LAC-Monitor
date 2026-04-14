import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

# ════════════════════════════════════════════════════════
#  配置（原 config.py 合并进来）
# ════════════════════════════════════════════════════════
BARK_KEY    = os.environ.get("BARK_KEY", "")
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app")
BARK_URL    = f"{BARK_SERVER}/{BARK_KEY}"

LAC_CIK    = "0001966983"
LAC_TICKER = "LAC"

FORM4_DANGER = ["S-Open Market", "open market sale", "Open Market Sale"]

FILING_DANGER_KEYWORDS = [
    "resignation", "terminate", "default", "waiver",
    "cease", "stop work", "Notice of Violation",
    "material adverse", "draw stop", "suspend",
]

FORM4_NEUTRAL = ["Tax withholding", "tax-withholding", "grant", "award", "F-InKind"]

POSITIVE_KEYWORDS = [
    "drawdown", "mechanical completion", "commissioning",
    "first production", "offtake",
]

CASH_WARNING_THRESHOLD  = 300_000_000
CASH_CRITICAL_THRESHOLD = 150_000_000
DOE_TOTAL_LOAN          = 2_230_000_000
DOE_DRAWN_TO_DATE       =   867_000_000

STATE_FILE = "seen_filings.json"
LOG_FILE   = "monitor.log"

# ─── 日志 ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("LAC")


# ════════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════════

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": [], "last_check": None}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def bark_push(title: str, body: str, level: str = "active", url: str = ""):
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
        r = requests.post(BARK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            log.info(f"✅ Bark推送成功: {title}")
        else:
            log.warning(f"Bark推送失败: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Bark推送异常: {e}")


def keyword_check(text: str, keywords: list) -> list:
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


# ════════════════════════════════════════════════════════
#  模块1：SEC EDGAR 监控
# ════════════════════════════════════════════════════════

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


def parse_filing_amount(summary: str) -> str:
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
    log.info("📡 检查 SEC EDGAR...")
    alerts = []

    headers = {
        "User-Agent": "LAC Monitor blueb@example.com",
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }

    try:
        url = f"https://data.sec.gov/submissions/CIK{LAC_CIK}.json"
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        log.info(f"SEC API 返回公告数: {len(data.get('filings', {}).get('recent', {}).get('form', []))}")
        data = r.json()
    except Exception as e:
        log.error(f"SEC API 请求失败: {e}")
        return alerts

    recent       = data.get("filings", {}).get("recent", {})
    forms        = recent.get("form", [])
    dates        = recent.get("filingDate", [])
    accessions   = recent.get("accessionNumber", [])
    descriptions = recent.get("primaryDocument", [])

    for i, form in enumerate(forms[:30]):
        acc      = accessions[i].replace("-", "")
        entry_id = acc
        if entry_id in state["seen_ids"]:
            continue

        state["seen_ids"].append(entry_id)
        if len(state["seen_ids"]) > 200:
            state["seen_ids"] = state["seen_ids"][-200:]

        date = dates[i] if i < len(dates) else ""
        doc  = descriptions[i] if i < len(descriptions) else ""
        link = f"https://www.sec.gov/Archives/edgar/data/1966983/{acc}/{doc}"

        filing_type  = form
        danger_level = "⚪"
        extra_info   = ""

        if filing_type == "4":
            filing_type = "Form 4"
            try:
                detail_url = f"https://data.sec.gov/Archives/edgar/data/1966983/{acc}/{doc}"
                rx = requests.get(detail_url, headers={"User-Agent": "LAC Monitor blueb@example.com"}, timeout=10)
                if "S-Open Market" in rx.text or "open market" in rx.text.lower():
                    danger_level = "🔴"
                    extra_info   = "\n⚠️ 主动公开市场卖出！"
                else:
                    log.info(f"Form 4 无害跳过: {date}")
                    continue
            except Exception:
                danger_level = "🟡"
                extra_info   = "\n请手动确认是否主动卖出"

        summary_text = ""
        if form in ("8-K", "6-K"):
            try:
                index_url = f"https://data.sec.gov/Archives/edgar/data/1966983/{acc}/index.json"
                ix = requests.get(index_url, headers={"User-Agent": "LAC Monitor blueb@example.com"}, timeout=10)
                files = ix.json().get("directory", {}).get("item", [])

                doc_name = ""
                for fi in files:
                    name = fi.get("name", "")
                    if name.endswith(".htm") and "index" not in name.lower():
                        doc_name = name
                        break

                if doc_name:
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/1966983/{acc}/{doc_name}"
                    rx = requests.get(doc_url, headers={"User-Agent": "LAC Monitor blueb@example.com"}, timeout=15)
                    clean = re.sub(r'<[^>]+>', ' ', rx.text)
                    clean = re.sub(r'\s+', ' ', clean).strip()

                    metrics = []

                    # 工人数量：approximately 700 workers / 1,800 workers
                    m = re.search(r'approximately\s+([\d,]+)\s*(?:skilled\s+)?(?:craftspeople|workers)', clean, re.I)
                    if m:
                        metrics.append(f"👷 工人：{m.group(1)}")

                    # DOE放款：$435 million / first drawdown
                    m = re.search(r'\$([\d,\.]+)\s*(million|billion)\s*(?:DOE|drawdown|loan)', clean, re.I)
                    if not m:
                        m = re.search(r'(?:drawdown|advance)\s+of\s+\$([\d,\.]+)\s*(million|billion)', clean, re.I)
                    if m:
                        unit = "亿" if m.group(2).lower() == "billion" else "百万"
                        metrics.append(f"🏦 DOE放款：${m.group(1)}{unit} / $22.3亿")

                    # 工程完成度：93% complete / 80% complete
                    m = re.search(r'([\d]+)%\s*(?:of\s+)?(?:detailed\s+)?engineering\s*(?:design\s*)?(?:complete|completed)', clean, re.I)
                    if m:
                        metrics.append(f"🏗️ 工程设计：{m.group(1)}%")

                    # 采购完成度
                    m = re.search(r'procurement\s+(?:is\s+)?([\d]+)%', clean, re.I)
                    if m:
                        metrics.append(f"📦 采购：{m.group(1)}%")

                    # 现金余额
                    m = re.search(r'cash\s+(?:and\s+)?(?:restricted\s+cash\s+)?(?:of\s+)?\$?([\d,\.]+)\s*(million|billion)', clean, re.I)
                    if m:
                        unit = "亿" if m.group(2).lower() == "billion" else "百万"
                        metrics.append(f"💵 现金：${m.group(1)}{unit}")

                    # Capex指引
                    m = re.search(r'\$([\d\.]+)\s*(?:billion|B)\s*(?:to|-)\s*\$([\d\.]+)\s*(?:billion|B)\s*(?:capex|capital)', clean, re.I)
                    if m:
                        metrics.append(f"💰 Capex指引：${m.group(1)}B-${m.group(2)}B")

                    # 机械完工目标
                    m = re.search(r'mechanical\s+completion\s+(?:targeted?\s+for\s+)?(\w+[-\s]?\d{4})', clean, re.I)
                    if m:
                        metrics.append(f"🎯 完工目标：{m.group(1)}")

                    if metrics:
                        summary_text = "\n" + "\n".join(metrics)
                    else:
                        # 没提取到结构化数据，退回关键句
                        KEY_TERMS = ["drawdown","loan","resign","terminat","default","construction","workforce","completion"]
                        sentences = re.split(r'(?<=[.!?])\s+', clean)
                        picked = [s.strip() for s in sentences if any(t.lower() in s.lower() for t in KEY_TERMS)][:2]
                        summary_text = "\n" + " ".join(picked)[:200]

            except Exception as ex:
                log.warning(f"摘要提取失败: {ex}")

        body_text = f"{date}{summary_text}\n{link}".strip()

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
#  模块2：资金链监控
# ════════════════════════════════════════════════════════

FINANCIAL_SNAPSHOT = {
    "date": "2025-12-31",
    "cash_total": 905_600_000,
    "cash_unrestricted": 750_000_000,
    "monthly_burn": 108_000_000,
    "doe_drawn": 867_000_000,
    "doe_remaining": 1_363_000_000,
    "shares_outstanding": 347_369_613,
    "atm_remaining": 810_300_000,
}


def check_cash_runway(state: dict) -> list:
    alerts = []
    snap   = FINANCIAL_SNAPSHOT
    cash   = snap["cash_unrestricted"]
    burn   = snap["monthly_burn"]
    runway = cash / burn if burn > 0 else 99

    log.info(f"💵 现金跑道：{runway:.1f}个月 | 现金：${cash/1e6:.0f}M | 月烧：${burn/1e6:.0f}M")

    if cash < CASH_CRITICAL_THRESHOLD:
        alerts.append({
            "title": "🚨 LAC 现金危机",
            "body": f"自由现金 ${cash/1e6:.0f}M < ${CASH_CRITICAL_THRESHOLD/1e6:.0f}M 警戒线\n跑道剩余：{runway:.1f}个月\nDOE剩余额度：${snap['doe_remaining']/1e6:.0f}M",
            "push_level": "timeSensitive",
            "danger": True,
        })
    elif cash < CASH_WARNING_THRESHOLD:
        alerts.append({
            "title": "⚠️ LAC 现金预警",
            "body": f"自由现金 ${cash/1e6:.0f}M 接近警戒线\n跑道剩余：{runway:.1f}个月",
            "push_level": "active",
            "danger": False,
        })

    doe_pct = snap["doe_drawn"] / DOE_TOTAL_LOAN
    log.info(f"🏦 DOE贷款进度：{doe_pct:.1%} 已提款 (${snap['doe_drawn']/1e6:.0f}M / $2,230M)")

    return alerts


# ════════════════════════════════════════════════════════
#  模块3：DOE 政策风险监控
# ════════════════════════════════════════════════════════

DOE_RSS      = "https://www.energy.gov/rss.xml"
DOE_KEYWORDS = ["Lithium Americas", "Thacker Pass", "LAC", "ATVM", "LPO"]


def check_doe_news(state: dict) -> list:
    alerts = []
    log.info("🏛️  检查 DOE 新闻...")

    try:
        feed = feedparser.parse(DOE_RSS)
    except Exception as e:
        log.warning(f"DOE RSS 解析失败: {e}")
        return alerts

    for entry in feed.entries[:20]:
        entry_id = entry.get("id", entry.get("link", ""))
        doe_key  = "doe_" + entry_id
        if doe_key in state["seen_ids"]:
            continue

        title    = entry.get("title", "")
        summary  = entry.get("summary", "")
        combined = title + " " + summary

        if any(kw.lower() in combined.lower() for kw in DOE_KEYWORDS):
            state["seen_ids"].append(doe_key)
            link          = entry.get("link", "")
            danger_hits   = keyword_check(combined, ["cancel", "suspend", "terminate", "freeze", "revoke"])
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
#  模块4：每周汇总
# ════════════════════════════════════════════════════════

def should_send_weekly_summary(state: dict) -> bool:
    today = datetime.now(timezone.utc)
    if today.weekday() != 0:
        return False
    last_summary = state.get("last_weekly_summary")
    if not last_summary:
        return True
    last_dt = datetime.fromisoformat(last_summary)
    return (today - last_dt).days >= 6


def send_weekly_summary(state: dict):
    snap   = FINANCIAL_SNAPSHOT
    cash   = snap["cash_unrestricted"]
    burn   = snap["monthly_burn"]
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
    all_alerts.extend(check_sec_filings(state))
    all_alerts.extend(check_cash_runway(state))
    all_alerts.extend(check_doe_news(state))

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

    if should_send_weekly_summary(state):
        send_weekly_summary(state)

    save_state(state)

    log.info(f"✅ 检查完成 | 新警报：{len(all_alerts)} 条 | 高危：{danger_count} 条")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
