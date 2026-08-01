#!/usr/bin/env python3
"""投研日历 - 股息分红事件采集。

把 A 股、港股和美股分红日期统一写入 events.json，作为 IR 爬虫之外的
标准事件源。A 股和港股优先使用 AkShare；美股免费源只稳定采集除息日，
支付日等完整公司行动字段预留给 Polygon/FMP 等数据源。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from app.crawler.company_lists import CompanyListError, allowed_securities, list_summary, resolve_list_names


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CALENDAR_FILE = DATA_DIR / "events.json"  # legacy; shim writes to PG
STATE_FILE = DATA_DIR / "dividend_state.json"
DEFAULT_SOURCE_INTERVAL_SECONDS = float(os.environ.get("DIVIDEND_SOURCE_INTERVAL_SECONDS", "12"))
DEFAULT_SOURCE_JITTER_SECONDS = float(os.environ.get("DIVIDEND_SOURCE_JITTER_SECONDS", "2"))

SOURCE_META = {
    "cninfo_akshare": {
        "name": "巨潮资讯-历史分红(AkShare)",
        "host": "webapi.cninfo.com.cn",
        "coverage": "A股股权登记日/除权除息日/派息日",
    },
    "eastmoney_a_share": {
        "name": "东方财富-A股分红送转",
        "host": "datacenter-web.eastmoney.com",
        "coverage": "A股除权除息日/每股派息；派息日字段不稳定",
    },
    "eastmoney_hk_akshare": {
        "name": "东方财富-港股分红派息(AkShare)",
        "host": "datacenter.eastmoney.com",
        "coverage": "港股除净日/股息发放日",
    },
    "yfinance": {
        "name": "Yahoo Finance-yfinance",
        "host": "query1.finance.yahoo.com",
        "coverage": "美股除息日；支付日需付费或公司源",
    },
}


class SourceUnavailable(RuntimeError):
    def __init__(self, source_id: str, reason: str):
        super().__init__(reason)
        self.source_id = source_id
        self.reason = reason


class SourceGuard:
    """Throttle and circuit-break dividend sources to stay well below anti-bot thresholds."""

    def __init__(self, min_interval_seconds: float, jitter_seconds: float):
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self.last_request_at: Dict[str, float] = {}
        self.unavailable: Dict[str, Dict] = {}
        self.source_failures: List[Dict] = []

    def before_request(self, source_id: str) -> None:
        if source_id in self.unavailable:
            meta = self.unavailable[source_id]
            raise SourceUnavailable(source_id, meta.get("reason", "source unavailable"))

        last = self.last_request_at.get(source_id)
        if last is not None:
            elapsed = time.monotonic() - last
            wait_for = self.min_interval_seconds + random.uniform(0, self.jitter_seconds) - elapsed
            if wait_for > 0:
                logger.info("  数据源限速 %s: sleep %.1fs", SOURCE_META[source_id]["name"], wait_for)
                time.sleep(wait_for)
        self.last_request_at[source_id] = time.monotonic()

    def record_success(self, source_id: str) -> None:
        self.last_request_at[source_id] = time.monotonic()

    def record_failure(self, source_id: str, exc: Exception) -> bool:
        text = str(exc)
        if not is_source_level_error(text):
            return False
        if source_id not in self.unavailable:
            meta = SOURCE_META.get(source_id, {})
            item = {
                "source_id": source_id,
                "source": meta.get("name", source_id),
                "host": meta.get("host", ""),
                "coverage": meta.get("coverage", ""),
                "error": text[:300],
                "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.unavailable[source_id] = {"reason": item["error"], "failed_at": item["failed_at"]}
            self.source_failures.append(item)
            logger.warning("  数据源熔断 %s: %s", item["source"], item["error"])
        return True

    def snapshot(self) -> Dict[str, Dict]:
        status = {}
        for source_id, meta in SOURCE_META.items():
            unavailable = self.unavailable.get(source_id)
            status[source_id] = {
                "source": meta.get("name", source_id),
                "host": meta.get("host", ""),
                "coverage": meta.get("coverage", ""),
                "available": unavailable is None,
                "reason": unavailable.get("reason", "") if unavailable else "",
                "failed_at": unavailable.get("failed_at", "") if unavailable else "",
            }
        return status


def is_source_level_error(text: str) -> bool:
    lowered = text.lower()
    patterns = [
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "failed to establish a new connection",
        "newconnectionerror",
        "max retries exceeded",
        "connection refused",
        "connection reset",
        "read timed out",
        "connect timeout",
        "status code 403",
        "status code 429",
        "forbidden",
        "too many requests",
    ]
    return any(pattern in lowered for pattern in patterns)

sys.path.insert(0, str(SCRIPT_DIR.parent))
# ponytail: shim provides dedup helpers + PG-backed load/save
from app.crawler._event_store import deduplicate_events, make_dedup_key


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text in {"", "NaT", "nan", "None"}:
        return ""
    return text


def parse_date(value) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    match = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text)
    if match:
        text = match.group(0)
    try:
        return datetime.strptime(text.replace("/", "-"), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def today_string() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def event_id(prefix: str, ticker: str, event_type: str, event_date: str, payload: Dict) -> str:
    basis = "|".join(
        clean_text(part)
        for part in [
            payload.get("财政年度") or payload.get("报告时间"),
            payload.get("分配类型") or payload.get("分红类型"),
            payload.get("分红方案") or payload.get("实施方案分红说明"),
            payload.get("公告日期") or payload.get("实施方案公告日期") or payload.get("最新公告日期"),
        ]
        if clean_text(part)
    )
    safe_basis = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", basis)[:20]
    return f"dividend_{prefix}_{ticker}_{event_type}_{event_date}_{safe_basis}"


def make_event(security: Dict, event_date: str, event_label: str, payload: Dict,
               source: str, source_url: str, note_parts: Iterable[str]) -> Dict:
    company = security.get("company_name_cn") or security.get("company_name_en") or security.get("ticker", "")
    ticker = security.get("ticker", "")
    market = security.get("normalized_market", "")
    note = "；".join(part for part in note_parts if part)
    return {
        "id": event_id(market.lower().replace(" ", "_"), ticker, event_label, event_date, payload),
        "date": event_date,
        "company": company,
        "ticker": ticker,
        "market": market,
        "title": f"{company}({ticker}) {event_label}",
        "type": "股息分红",
        "note": note,
        "source": source,
        "source_url": source_url,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def load_events() -> Dict:
    """Read events from PG via the _event_store shim."""
    from app.crawler._event_store import load_events as _shim_load
    return _shim_load()


def save_events(data: Dict) -> None:
    """Write events to PG via the _event_store shim."""
    from app.crawler._event_store import save_events as _shim_save
    _shim_save(data)


def add_events(events: List[Dict], dry_run: bool = False) -> int:
    if dry_run:
        for event in sorted(events, key=lambda e: (e.get("date", ""), e.get("title", ""))):
            print(f"[{event['date']}] {event['title']} | {event.get('note', '')}")
        return len(events)

    data = load_events()
    existing_ids = {e.get("id") for e in data.get("events", [])}
    existing_keys = {make_dedup_key(e) for e in data.get("events", [])}
    added = 0

    for event in events:
        key = make_dedup_key(event)
        if event["id"] in existing_ids or key in existing_keys:
            continue
        data.setdefault("events", []).append({
            "id": event["id"],
            "date": event["date"],
            "company": event.get("company", ""),
            "ticker": event.get("ticker", ""),
            "market": event.get("market", ""),
            "title": event["title"],
            "type": event["type"],
            "note": event.get("note", ""),
            "time": "",
            "timezone": "",
            "source": event.get("source", ""),
            "source_url": event.get("source_url", ""),
            "created_at": event.get("collected_at", ""),
        })
        existing_ids.add(event["id"])
        existing_keys.add(key)
        added += 1

    if deduplicate_events:
        data["events"] = deduplicate_events(data.get("events", []))
    else:
        data["events"] = sorted(data.get("events", []), key=lambda e: e.get("date", ""))
    save_events(data)
    return added


def future_or_recent(event_date: str, lookback_days: int) -> bool:
    if not event_date:
        return False
    threshold = (datetime.now().date() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return event_date >= threshold


def eastmoney_datacenter(report_name: str, filter_str: str, page_size: int = 20,
                         sort_columns: str = "", sort_types: str = "-1") -> List[Dict]:
    import requests

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://data.eastmoney.com/",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("result") or {}
    return result.get("data") or []


def fetch_a_share_cninfo_events(security: Dict, lookback_days: int, guard: SourceGuard) -> Tuple[List[Dict], List[Dict]]:
    import akshare as ak

    source_id = "cninfo_akshare"
    ticker = str(security.get("ticker", "")).zfill(6)
    if ticker.startswith(("15", "51", "56", "58")) or "ETF" in (security.get("company_name_cn") or "").upper():
        logger.info("  %s ETF/基金证券暂不纳入普通股票分红源，跳过", ticker)
        return [], []
    guard.before_request(source_id)
    try:
        df = ak.stock_dividend_cninfo(ticker)
    except KeyError as exc:
        logger.info("  %s 非普通 A 股分红结构或暂无可用字段，跳过: %s", ticker, exc)
        return [], []
    required = {"实施方案公告日期", "股权登记日", "除权日", "派息日"}
    if not required.issubset(set(df.columns)):
        logger.info("  %s 非普通 A 股分红结构或暂无可用字段，跳过", ticker)
        return [], []
    events: List[Dict] = []
    for row in df.to_dict("records"):
        register_date = parse_date(row.get("股权登记日"))
        ex_date = parse_date(row.get("除权日"))
        pay_date = parse_date(row.get("派息日"))
        note_parts = [
            f"分红类型: {clean_text(row.get('分红类型'))}" if clean_text(row.get("分红类型")) else "",
            f"派息比例: {clean_text(row.get('派息比例'))}" if clean_text(row.get("派息比例")) else "",
            f"股权登记日: {register_date}" if register_date else "",
            f"公告日: {parse_date(row.get('实施方案公告日期'))}" if parse_date(row.get("实施方案公告日期")) else "",
            clean_text(row.get("实施方案分红说明")),
        ]
        if future_or_recent(ex_date, lookback_days):
            events.append(make_event(
                security, ex_date, "除权除息日", row,
                "巨潮资讯-历史分红(AkShare)",
                "https://webapi.cninfo.com.cn/",
                note_parts,
            ))
        if future_or_recent(pay_date, lookback_days):
            events.append(make_event(
                security, pay_date, "现金红利派息日", row,
                "巨潮资讯-历史分红(AkShare)",
                "https://webapi.cninfo.com.cn/",
                note_parts,
            ))
    guard.record_success(source_id)
    return events, []


def fetch_a_share_eastmoney_events(security: Dict, lookback_days: int, guard: SourceGuard) -> Tuple[List[Dict], List[Dict]]:
    source_id = "eastmoney_a_share"
    guard.before_request(source_id)
    ticker = str(security.get("ticker", "")).zfill(6)
    if ticker.startswith(("15", "51", "56", "58")) or "ETF" in (security.get("company_name_cn") or "").upper():
        return [], []

    rows = eastmoney_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{ticker}")',
        page_size=20,
        sort_columns="EX_DIVIDEND_DATE",
        sort_types="-1",
    )
    events: List[Dict] = []
    for row in rows:
        ex_date = parse_date(row.get("EX_DIVIDEND_DATE"))
        note_parts = [
            f"每股派息税前: {clean_text(row.get('PRETAX_BONUS_RMB'))}" if clean_text(row.get("PRETAX_BONUS_RMB")) else "",
            f"转增比例: {clean_text(row.get('TRANSFER_RATIO'))}" if clean_text(row.get("TRANSFER_RATIO")) else "",
            f"送股比例: {clean_text(row.get('BONUS_RATIO'))}" if clean_text(row.get("BONUS_RATIO")) else "",
            f"进度: {clean_text(row.get('ASSIGN_PROGRESS'))}" if clean_text(row.get("ASSIGN_PROGRESS")) else "",
            "备用源仅稳定提供除权除息日；现金派息日优先等待巨潮源",
        ]
        if future_or_recent(ex_date, lookback_days):
            events.append(make_event(
                security, ex_date, "除权除息日", row,
                SOURCE_META[source_id]["name"],
                "https://data.eastmoney.com/yjfp/",
                note_parts,
            ))
    guard.record_success(source_id)
    return events, []


def fetch_a_share_events(security: Dict, lookback_days: int, guard: SourceGuard) -> Tuple[List[Dict], List[Dict]]:
    attempts = [
        ("cninfo_akshare", fetch_a_share_cninfo_events),
        ("eastmoney_a_share", fetch_a_share_eastmoney_events),
    ]
    errors: List[Dict] = []
    for source_id, fetcher in attempts:
        try:
            events, source_errors = fetcher(security, lookback_days, guard)
            return events, errors + source_errors
        except SourceUnavailable as exc:
            errors.append({"source_id": source_id, "error": exc.reason, "source_unavailable": True})
            continue
        except Exception as exc:
            if guard.record_failure(source_id, exc):
                errors.append({"source_id": source_id, "error": str(exc)[:300], "source_unavailable": True})
                continue
            logger.warning("  %s 主分红源单票解析失败，尝试备用源: %s", security.get("ticker", ""), exc)
            errors.append({"source_id": source_id, "error": str(exc)[:300], "fallback": True})
            continue
    return [], errors


def fetch_hk_events(security: Dict, lookback_days: int, guard: SourceGuard) -> Tuple[List[Dict], List[Dict]]:
    import akshare as ak

    source_id = "eastmoney_hk_akshare"
    try:
        guard.before_request(source_id)
    except SourceUnavailable as exc:
        return [], [{"source_id": source_id, "error": exc.reason, "source_unavailable": True}]

    ticker = str(security.get("ticker", "")).zfill(5)
    try:
        df = ak.stock_hk_dividend_payout_em(ticker)
    except Exception as exc:
        if guard.record_failure(source_id, exc):
            return [], [{"source_id": source_id, "error": str(exc)[:300], "source_unavailable": True}]
        raise
    events: List[Dict] = []
    for row in df.to_dict("records"):
        ex_date = parse_date(row.get("除净日"))
        pay_date = parse_date(row.get("发放日"))
        transfer_end = parse_date(row.get("截至过户日"))
        note_parts = [
            f"财政年度: {clean_text(row.get('财政年度'))}" if clean_text(row.get("财政年度")) else "",
            f"分配类型: {clean_text(row.get('分配类型'))}" if clean_text(row.get("分配类型")) else "",
            f"分红方案: {clean_text(row.get('分红方案'))}" if clean_text(row.get("分红方案")) else "",
            f"截至过户日: {transfer_end}" if transfer_end else "",
            f"公告日: {parse_date(row.get('最新公告日期'))}" if parse_date(row.get("最新公告日期")) else "",
        ]
        if future_or_recent(ex_date, lookback_days):
            events.append(make_event(
                security, ex_date, "除净日", row,
                "东方财富-港股分红派息(AkShare)",
                "https://datacenter.eastmoney.com/",
                note_parts,
            ))
        if future_or_recent(pay_date, lookback_days):
            events.append(make_event(
                security, pay_date, "股息发放日", row,
                "东方财富-港股分红派息(AkShare)",
                "https://datacenter.eastmoney.com/",
                note_parts,
            ))
    guard.record_success(source_id)
    return events, []


def fetch_us_yfinance_events(security: Dict, lookback_days: int, guard: SourceGuard) -> Tuple[List[Dict], List[Dict]]:
    """Free US fallback: yfinance dividends are indexed by ex-dividend date."""
    import yfinance as yf

    source_id = "yfinance"
    try:
        guard.before_request(source_id)
    except SourceUnavailable as exc:
        return [], [{"source_id": source_id, "error": exc.reason, "source_unavailable": True}]

    ticker = str(security.get("ticker", "")).upper()
    try:
        dividends = yf.Ticker(ticker).dividends
    except Exception as exc:
        if guard.record_failure(source_id, exc):
            return [], [{"source_id": source_id, "error": str(exc)[:300], "source_unavailable": True}]
        raise
    events: List[Dict] = []
    if dividends is None or dividends.empty:
        guard.record_success(source_id)
        return events, []
    for idx, amount in dividends.tail(8).items():
        event_date = parse_date(idx.date() if hasattr(idx, "date") else idx)
        if not future_or_recent(event_date, lookback_days):
            continue
        row = {"amount": amount, "source_date": event_date}
        note = [f"每股股息: {amount}", "免费源仅稳定提供除息日；支付日需接 Polygon/FMP 或公司 IR"]
        events.append(make_event(
            security, event_date, "除息日", row,
            "Yahoo Finance-yfinance",
            f"https://finance.yahoo.com/quote/{ticker}/history/?filter=div",
            note,
        ))
    guard.record_success(source_id)
    return events, []


def run(company_lists: Optional[List[str]] = None, markets: Optional[List[str]] = None,
        dry_run: bool = False, lookback_days: int = 7, include_us: bool = False,
        source_interval_seconds: float = DEFAULT_SOURCE_INTERVAL_SECONDS,
        source_jitter_seconds: float = DEFAULT_SOURCE_JITTER_SECONDS) -> Dict:
    selected = resolve_list_names(company_lists)
    wanted_markets = markets or ["China", "Hong Kong"]
    if include_us and "US" not in wanted_markets:
        wanted_markets.append("US")

    securities = allowed_securities(selected, wanted_markets)
    guard = SourceGuard(source_interval_seconds, source_jitter_seconds)
    logger.info("=== 股息分红事件采集 ===")
    logger.info("公司列表: %s；市场: %s；证券数: %s", ", ".join(selected), ", ".join(wanted_markets), len(securities))
    logger.info("数据源限速: 同一源请求间隔 %.1fs + jitter %.1fs；源级失败后熔断", source_interval_seconds, source_jitter_seconds)

    all_events: List[Dict] = []
    failures: List[Dict] = []
    skipped: Dict[str, int] = {}
    source_skips: Dict[str, int] = {}

    for security in securities:
        market = security.get("normalized_market", "")
        ticker = security.get("ticker", "")
        company = security.get("company_name_cn") or ticker
        try:
            if market == "China":
                events, source_errors = fetch_a_share_events(security, lookback_days, guard)
            elif market == "Hong Kong":
                events, source_errors = fetch_hk_events(security, lookback_days, guard)
            elif market == "US" and include_us:
                events, source_errors = fetch_us_yfinance_events(security, lookback_days, guard)
            else:
                skipped[market] = skipped.get(market, 0) + 1
                continue
            for item in source_errors:
                source_id = item.get("source_id", "unknown")
                source_skips[source_id] = source_skips.get(source_id, 0) + 1
            all_events.extend(events)
            logger.info("  %s(%s) %s: %s 条", company, ticker, market, len(events))
        except Exception as exc:
            failures.append({
                "company": company,
                "ticker": ticker,
                "market": market,
                "error": str(exc)[:300],
            })
            logger.warning("  %s(%s) %s 失败: %s", company, ticker, market, exc)

    added = add_events(all_events, dry_run=dry_run) if all_events else 0

    result = {
        "success": not failures and not guard.source_failures,
        "company_lists": selected,
        "markets": wanted_markets,
        "securities_checked": len(securities),
        "events_found": len(all_events),
        "events_added": 0 if dry_run else added,
        "dry_run_events": added if dry_run else 0,
        "failures": failures,
        "source_failures": guard.source_failures,
        "source_status": guard.snapshot(),
        "source_skips": source_skips,
        "source_interval_seconds": source_interval_seconds,
        "source_jitter_seconds": source_jitter_seconds,
        "skipped": skipped,
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    if not dry_run:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(
        "完成: 发现 %s 条，新增 %s 条，证券失败 %s 个，源失败 %s 个",
        len(all_events),
        result["events_added"],
        len(failures),
        len(guard.source_failures),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="投研日历股息分红事件采集")
    parser.add_argument("--company-list", action="append", dest="company_lists",
                        help="指定公司列表，可重复传入或用逗号分隔；默认使用 officecodex 全局 active_list")
    parser.add_argument("--market", action="append", dest="markets",
                        help="指定 normalized_market: China, Hong Kong, US；可重复传入")
    parser.add_argument("--include-us", action="store_true", help="启用美股 yfinance 除息日低配采集")
    parser.add_argument("--lookback-days", type=int, default=int(os.environ.get("DIVIDEND_LOOKBACK_DAYS", "7")),
                        help="保留最近 N 天到未来的事件，默认 7")
    parser.add_argument("--source-interval-seconds", type=float,
                        default=DEFAULT_SOURCE_INTERVAL_SECONDS,
                        help="同一数据源两次请求的最小间隔，默认 12 秒，避免触发反爬")
    parser.add_argument("--source-jitter-seconds", type=float,
                        default=DEFAULT_SOURCE_JITTER_SECONDS,
                        help="每次同源请求增加 0-N 秒随机抖动，默认 2 秒")
    parser.add_argument("--dry-run", "--test", action="store_true", dest="dry_run", help="只打印，不写入 events.json")
    parser.add_argument("--list-company-lists", action="store_true", help="列出可用公司列表")
    args = parser.parse_args()

    if args.list_company_lists:
        print("公司列表:")
        for item in list_summary():
            markets = ", ".join(f"{k}:{v}" for k, v in sorted(item.get("markets", {}).items()))
            print(f"  {item['id']:16s} | 公司 {item['companies']} | 证券 {item['securities']} | {markets}")
        return

    raw_markets = []
    for raw in args.markets or []:
        raw_markets.extend(part.strip() for part in raw.split(",") if part.strip())

    try:
        result = run(
            company_lists=args.company_lists,
            markets=raw_markets or None,
            dry_run=args.dry_run,
            lookback_days=args.lookback_days,
            include_us=args.include_us,
            source_interval_seconds=args.source_interval_seconds,
            source_jitter_seconds=args.source_jitter_seconds,
        )
    except CompanyListError as exc:
        logger.error(str(exc))
        raise SystemExit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (result.get("failures") or result.get("source_failures")) and not args.dry_run:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
