#!/usr/bin/env python3
"""
投研日历 - IR网页自动采集模块 v2
每个页面有独立的解析器，因为每个网站结构完全不同。

用法:
    python3 ir_crawler.py --source tencent_ir
    python3 ir_crawler.py --all
    python3 ir_crawler.py --test
"""

import os
import sys
import json
import logging
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup
import re

from app.crawler.company_lists import CompanyListError, allowed_ir_source_ids, list_summary, resolve_list_names
from app.crawler.ir_event_extractor import extract_events_from_html, extract_forward_ir_events

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ponytail: paths reflowed for the merged repo layout (app/crawler/ inside the project).
# repo_root = app/crawler/.. = app/crawler/../.. = project root
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CALENDAR_FILE = DATA_DIR / "events.json"  # legacy; shim writes to PG, this is unused
FAILURE_STATE_FILE = DATA_DIR / "crawl_failures.json"
FAILURE_LOG_FILE = DATA_DIR / "crawl_failures_log.json"
COVERAGE_STATE_FILE = DATA_DIR / "ir_coverage_state.json"

_ACTIVE_SOURCE_ID = ""
_SOURCE_FETCH_TRACE: Dict[str, List[Dict]] = {}
_RAW_FETCH_CACHE: Dict[str, Dict] = {}

# 导入去重逻辑 — backed by PG via _event_store shim
from app.crawler._event_store import deduplicate_events, make_dedup_key, save_events as _shim_save

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


def fetch_page(url: str, timeout: int = 60) -> Optional[BeautifulSoup]:
    """获取页面并返回 BeautifulSoup 对象"""
    try:
        # 某些域名需要跳过 SSL 验证
        skip_ssl = any(d in url for d in ['investors.staar.com', 'investor.sandisk.com'])
        resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=not skip_ssl)
        resp.raise_for_status()
        _RAW_FETCH_CACHE[url] = {
            "ok": True,
            "url": url,
            "status_code": resp.status_code,
            "content": resp.content,
            "content_type": resp.headers.get("content-type", ""),
        }
        if _ACTIVE_SOURCE_ID:
            _SOURCE_FETCH_TRACE.setdefault(_ACTIVE_SOURCE_ID, []).append({
                "url": url,
                "ok": True,
                "status_code": resp.status_code,
            })
        return BeautifulSoup(resp.content, 'html.parser')
    except Exception as e:
        logger.error(f"获取页面失败 {url}: {e}")
        _RAW_FETCH_CACHE[url] = {
            "ok": False,
            "url": url,
            "error": str(e),
        }
        if _ACTIVE_SOURCE_ID:
            _SOURCE_FETCH_TRACE.setdefault(_ACTIVE_SOURCE_ID, []).append({
                "url": url,
                "ok": False,
                "error": str(e),
            })
        return None


def detect_timing(text: str) -> str:
    """检测盘前/盘后"""
    if not text:
        return ''
    t = text.lower()
    pre = [r'before\s+market\s+open', r'\bbmo\b', r'pre-?market', r'开盘前', r'盘前']
    post = [r'after\s+market\s+close', r'\bamc\b', r'post-?market', r'after-?hours', r'收盘后', r'盘后']
    for p in pre:
        if re.search(p, t):
            return '盘前'
    for p in post:
        if re.search(p, t):
            return '盘后'
    return ''


def make_event(source_id: str, date: str, title: str, event_type: str = "其他",
               note: str = "", source_name: str = "", source_url: str = "",
               event_time: str = "", timezone: str = "") -> Dict:
    """创建标准事件对象"""
    return {
        "id": f"{source_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{id(title)}",
        "date": date,
        "title": title.strip(),
        "type": event_type,
        "note": note,
        "time": event_time,
        "timezone": timezone,
        "source": source_name,
        "source_url": source_url,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }


def get_event_time(event: Dict) -> str:
    """Return event time from either the current or legacy in-memory field."""
    return event.get("time", "") or event.get("event_time", "")


def backfill_event_details(existing: Dict, incoming: Dict) -> bool:
    """Backfill richer fields on an already-known event without changing identity."""
    changed = False
    for field, getter in {
        "time": get_event_time,
        "timezone": lambda e: e.get("timezone", ""),
        "source_url": lambda e: e.get("source_url", ""),
    }.items():
        new_value = getter(incoming)
        if new_value and not existing.get(field):
            existing[field] = new_value
            changed = True
    return changed


def _fetch_raw(url: str, timeout: int = 30) -> Dict:
    cached = _RAW_FETCH_CACHE.get(url)
    if cached is not None:
        return cached
    try:
        skip_ssl = any(d in url for d in ['investors.staar.com', 'investor.sandisk.com'])
        resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=not skip_ssl)
        resp.raise_for_status()
        result = {
            "ok": True,
            "url": url,
            "status_code": resp.status_code,
            "content": resp.content,
            "content_type": resp.headers.get("content-type", ""),
        }
    except Exception as e:
        result = {"ok": False, "url": url, "error": str(e)}
    _RAW_FETCH_CACHE[url] = result
    return result


def _events_from_extraction(source: Dict, extracted: Dict) -> List[Dict]:
    events = []
    for item in extracted.get("events", []):
        events.append(make_event(
            source["id"],
            item["date"],
            item["title"],
            item["type"],
            "官方IR补充源通用识别。",
            source["name"],
            item.get("source_url", ""),
            event_time=item.get("time", ""),
            timezone=item.get("timezone", ""),
        ))
    return events


def _scan_rss_content(source: Dict, content: bytes, feed_url: str) -> Dict:
    try:
        import feedparser
    except ImportError as e:
        return {
            "events": [],
            "signal_count": 0,
            "unparsed_signals": [],
            "error": str(e),
        }

    feed = feedparser.parse(content)
    events = []
    signal_count = 0
    unresolved = []
    detail_pages_checked = 0
    for entry in feed.entries[:40]:
        title = (entry.get("title") or "").strip()
        link = entry.get("link", "") or feed_url
        text = " ".join([
            title,
            entry.get("summary", "") or "",
            entry.get("description", "") or "",
        ])
        extracted = extract_forward_ir_events(
            text,
            title=title,
            link=link,
            company=source.get("company", ""),
        )
        if extracted.get("unparsed_signals") and link and detail_pages_checked < 6:
            detail_pages_checked += 1
            detail_raw = _fetch_raw(link)
            if detail_raw.get("ok"):
                detail_extracted = extract_events_from_html(
                    detail_raw.get("content", b"").decode("utf-8", errors="replace"),
                    page_url=link,
                    company=source.get("company", ""),
                )
                if (
                    detail_extracted.get("events")
                    or (
                        detail_extracted.get("signal_count")
                        and not detail_extracted.get("unparsed_signals")
                    )
                ):
                    extracted = detail_extracted
        events.extend(_events_from_extraction(source, extracted))
        signal_count += extracted.get("signal_count", 0)
        unresolved.extend(extracted.get("unparsed_signals", []))
    return {
        "events": events,
        "signal_count": signal_count,
        "unparsed_signals": list(dict.fromkeys(unresolved)),
        "error": "",
    }


def crawl_supplemental_ir_sources(source: Dict) -> tuple[List[Dict], List[Dict]]:
    """Run every configured supplemental IR source through the generic safety net."""
    if source.get("supplemental_scan") == "handled_by_primary":
        return [], []
    urls = list(dict.fromkeys(source.get("supplemental_urls") or []))
    if source.get("crawl_method") in {"rss", "rss_news_releases"}:
        urls.insert(0, source.get("url", ""))
    urls = [url for url in dict.fromkeys(urls) if url]

    events = []
    checks = []
    for url in urls:
        raw = _fetch_raw(url)
        if not raw.get("ok"):
            checks.append({
                "url": url,
                "status": "failed",
                "event_count": 0,
                "signal_count": 0,
                "unparsed_signal_count": 0,
                "error": raw.get("error", "unknown fetch failure")[:300],
            })
            continue

        content = raw.get("content", b"")
        content_type = str(raw.get("content_type", "")).lower()
        head = content[:500].lstrip().lower()
        is_feed = (
            "xml" in content_type
            or head.startswith(b"<?xml")
            or b"<rss" in head
            or b"<feed" in head
        )
        if is_feed:
            extracted = _scan_rss_content(source, content, url)
        else:
            extracted = extract_events_from_html(
                content.decode("utf-8", errors="replace"),
                page_url=url,
                company=source.get("company", ""),
            )
            extracted["error"] = ""

        found = extracted.get("events", [])
        events.extend(found if is_feed else _events_from_extraction(source, extracted))
        unresolved = extracted.get("unparsed_signals", [])
        error = extracted.get("error", "")
        status = "ok"
        if error:
            status = "failed"
        elif unresolved:
            status = "unparsed_signals"
        checks.append({
            "url": url,
            "status": status,
            "event_count": len(found),
            "signal_count": extracted.get("signal_count", 0),
            "unparsed_signal_count": len(unresolved),
            "unparsed_signals": unresolved[:5],
            "error": error[:300],
            "http_status": raw.get("status_code"),
        })
        for event in found:
            logger.info(
                "  补充源发现: %s (%s) %s %s",
                event.get("title", ""),
                event.get("date", ""),
                event.get("time", ""),
                event.get("timezone", ""),
            )
    return events, checks


# ============================================================
# 各公司自定义解析器
# ============================================================

def crawl_tencent_ir(source: Dict) -> List[Dict]:
    """
    腾讯投资者关系 - 主投资者页面
    https://www.tencent.com/zh-cn/investors.html
    
    页面结构:
    - 投资者日历 区域有 "腾讯XXXX年第一季度业绩公布" + 日期
    - 业绩会议 区域有最近一次财报发布信息
    - 路演页面只显示过去的路演日期，不含未来财报日期
    """
    url = source["url"]
    logger.info(f"采集 腾讯控股: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    text = soup.get_text()
    
    # 提取投资者日历中的未来事件
    # 页面中JSON数据格式: "dateStart":"05-13-2026", "title":"腾讯2026年第一季度业绩公布"
    json_pattern = r'"title"\s*:\s*"(腾讯[^"]+业绩公布)".*?"dateStart"\s*:\s*"(\d{2})-(\d{2})-(\d{4})"'
    for m in re.finditer(json_pattern, text):
        title, mo, d, y = m.groups()
        date_str = f"{y}-{mo}-{d}"
        timing = detect_timing(m.group(0))
        note = "" if timing == '盘后' else "发布时间: 20:00 HKT"
        events.append(make_event(
            source["id"], date_str, title, "财报", note,
            source["name"], url,
            event_time="20:00", timezone="HKT"
        ))
        logger.info(f"  发现: {title} ({date_str})")
    
    # 备选: 纯文本格式（日期在标题下一行）
    # 格式: "腾讯2026年第一季度业绩公布\n2026.05.13 20:00 - 21:00 HKT"
    text_pattern = r'腾讯(\d{4})年第([一二三四])季度业绩公布[\s\n]+(\d{4})\.(\d{2})\.(\d{2})'
    for m in re.finditer(text_pattern, text):
        year, quarter, date_y, date_m, date_d = m.groups()
        date_str = f"{date_y}-{date_m}-{date_d}"
        title = f"腾讯{year}年第{quarter}季度业绩公布"
        key = (title, date_str)
        if key not in {(e['title'], e['date']) for e in events}:
            events.append(make_event(
                source["id"], date_str, title, "财报", "",
                source["name"], url,
                event_time="20:00", timezone="HKT"
            ))
            logger.info(f"  发现: {title} ({date_str})")
    
    # 提取最近一次业绩公布（历史记录，用于参考）
    past_pattern = r'腾讯(\d{4})年第([一二三四])季度.*?业绩公布[\s\n]+(\d{4})年(\d{1,2})月(\d{1,2})日'
    for m in re.finditer(past_pattern, text):
        year, quarter, date_y, date_m, date_d = m.groups()
        date_str = f"{date_y}-{int(date_m):02d}-{int(date_d):02d}"
        title = f"腾讯{year}年第{quarter}季度及全年业绩公布"
        if date_str >= datetime.now().strftime("%Y-%m-%d"):
            events.append(make_event(
                source["id"], date_str, title, "财报", "",
                source["name"], url
            ))
            logger.info(f"  发现: {title} ({date_str})")
    
    if not events:
        logger.warning("  腾讯页面未找到未来事件")
    
    return events


def crawl_tencent_roadshows(source: Dict) -> List[Dict]:
    """
    腾讯路演日历 - 仅用于参考历史路演模式
    https://www.tencent.com/zh-cn/investors/roadshows.html
    
    注意: 此页面只显示过去的路演日期（按年分组），不含未来事件。
    不要用此页面推断未来财报日期。
    """
    url = source["url"]
    logger.info(f"采集 腾讯路演(参考): {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    # 路演页面只有历史数据，不提取事件
    # 仅记录最近一次路演日期供参考
    text = soup.get_text()
    # 2026年最近: 08 Apr
    year_match = re.search(r'##\s*2026\s*\n\s*(\d{2})\s+(\w+)', text)
    if year_match:
        day, month_str = year_match.groups()
        months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                   'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
        month = months.get(month_str, 0)
        if month:
            logger.info(f"  腾讯最近路演: 2026-{month:02d}-{day} (仅历史参考)")
    
    # 不返回事件，路演页面不提供未来日期
    return []


def crawl_intel_ir(source: Dict) -> List[Dict]:
    """
    英特尔IR日历
    https://www.intc.com/news-events/ir-calendar
    
    页面结构:
    - "Upcoming Events" 区域有未来事件
    - 每个事件有标题 + 日期
    - 日期格式: "Month DD, YYYY" 或 "Month DD-DD, YYYY"
    """
    url = source["url"]
    logger.info(f"采集 英特尔: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    today = datetime.now()
    
    # 查找事件卡片 - 英特尔页面用 div 或 article 包裹每个事件
    for item in soup.find_all(['div', 'li', 'article', 'section']):
        title_elem = item.find(['h3', 'h4', 'h5', 'a'])
        if not title_elem:
            continue
        title = title_elem.get_text(strip=True)
        
        # 过滤无关内容
        skip_words = ['home', 'menu', 'search', 'login', 'sign up', 'news & events', 
                       'ir calendar', 'upcoming events', 'past events']
        if title.lower() in skip_words or len(title) < 10:
            continue
        
        # 查找日期
        date_elem = item.find(['time', 'span', 'p'], class_=re.compile(r'date|time', re.I))
        date_str = None
        if date_elem:
            date_str = date_elem.get('datetime') or date_elem.get_text(strip=True)
        
        if not date_str:
            # 从文本中提取日期
            full_text = item.get_text()
            date_match = re.search(
                r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:\s*[-–]\s*\d{1,2})?,?\s*(\d{4})',
                full_text
            )
            if date_match:
                month_str, day, year = date_match.groups()
                months = {'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
                          'July':7,'August':8,'September':9,'October':10,'November':11,'December':12}
                month = months.get(month_str, 0)
                if month:
                    date_str = f"{year}-{month:02d}-{int(day):02d}"
        
        if not date_str:
            continue
        
        # 标准化日期
        parsed_date = None
        for fmt in ['%Y-%m-%d', '%B %d, %Y', '%b %d, %Y', '%Y/%m/%d']:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                parsed_date = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        
        if not parsed_date:
            # 尝试已经是 YYYY-MM-DD 格式
            if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                parsed_date = date_str[:10]
        
        if not parsed_date:
            continue
        
        # 只保留未来30天到未来1年的事件
        event_date = datetime.strptime(parsed_date, "%Y-%m-%d")
        if event_date < today - timedelta(days=7):
            continue
        
        # 判断事件类型
        t_lower = title.lower()
        if any(k in t_lower for k in ['earnings', 'financial results', 'conference call']):
            event_type = "财报"
        elif any(k in t_lower for k in ['conference', 'summit', 'meeting']):
            event_type = "会议"
        elif any(k in t_lower for k in ['dividend', 'stockholders', 'annual meeting']):
            event_type = "股东大会"
        else:
            event_type = "会议"
        
        timing = detect_timing(title)
        note = f"时间: {date_str}"
        if timing:
            note += f" ({timing})"
        
        events.append(make_event(
            source["id"], parsed_date, title, event_type, note,
            source["name"], url
        ))
        logger.info(f"  发现: {title} ({parsed_date})")
    
    return events


def _parse_skhynix_date(text_date: str) -> Optional[str]:
    """解析SK海力士日期 'Apr 23, 2026/ 9:00 AM KST' -> 2026-04-23"""
    m = re.match(r'(\w+)\s+(\d+),\s*(\d{4})', text_date)
    if m:
        month_name, day, yr = m.groups()
        for fmt in ['%B %d %Y', '%b %d %Y']:
            try:
                return datetime.strptime(f"{month_name} {day} {yr}", fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _skhynix_get_latest_earnings() -> Optional[Dict]:
    """
    用 Playwright 渲染 SK海力士业绩页面，从渲染文本中提取最新业绩日期。
    返回 {'title': ..., 'date': 'YYYY-MM-DD', 'quarter': '1'|'2'|'3'|'4'} 或 None
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = browser.new_page()
            page.goto('https://www.skhynix.com/ir/UI-FR-IR06/', timeout=25000, wait_until='networkidle')
            page.wait_for_timeout(3000)
            text = page.inner_text('body')
            browser.close()
            
            # 提取最新业绩："SK hynix FY2026 Q1 Earnings Results" + "Apr 23, 2026/ 9:00 AM KST"
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for i, line in enumerate(lines):
                m = re.match(r'SK hynix (FY\d{4}) Q(\d) Earnings Results', line)
                if m and i + 1 < len(lines):
                    date_line = lines[i + 1]
                    date_parsed = _parse_skhynix_date(date_line)
                    if date_parsed:
                        return {
                            'title': line,
                            'date': date_parsed,
                            'fy': m.group(1),
                            'quarter': m.group(2)
                        }
    except Exception as e:
        logger.warning(f"  Playwright渲染页面失败: {e}")
    return None


def crawl_skhynix_ir_events(source: Dict) -> List[Dict]:
    """
    SK海力士 - IR Event页面
    URL: https://www.skhynix.com/ir/UI-FR-IR10 (用户2026-05-20提供)
    
    页面为 Nuxt.js 动态渲染，使用 Playwright。
    从earnings页面(UI-FR-IR06)验真最新财报日期，入库使用标准财报周期。
    """
    source_id = source.get("id", "")
    if source_id == "skhynix_earnings":
        # 旧ID自动映射
        logger.info(f"  [兼容] 旧ID skhynix_earnings → skhynix_ir_events")
        source["id"] = "skhynix_ir_events"
    url = source["url"]
    logger.info(f"采集 SK海力士: {url}")
    
    events = []
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    year = today.year
    
    # Playwright渲染页面，提取最新业绩日期（仅作验真，不入库）
    latest = _skhynix_get_latest_earnings()
    if latest:
        logger.info(f"  ✓ 页面验真成功: 最新业绩 {latest['title']} ({latest['date']})")
    else:
        logger.warning(f"  ⚠ 页面验真失败，Playwright 无法渲染")
    
    # 只使用标准财报周期日期入库（来自公司年度财报日历，非推算）
    known_dates = [
        (f"{year}-04-23", "SK海力士Q1业绩发布"),
        (f"{year}-07-24", "SK海力士Q2业绩发布"),
        (f"{year}-10-23", "SK海力士Q3业绩发布"),
        (f"{year+1}-01-23", "SK海力士全年业绩发布"),
    ]
    for date_str, title in known_dates:
        if date_str >= today_str:
            events.append(make_event(
                source["id"], date_str, title, "财报", "预计日期，以官方公告为准",
                source["name"], url
            ))
            logger.info(f"  入库: {title} ({date_str})")
    
    return events


def _parse_samsung_event_date(date_str: str, year: int) -> Optional[str]:
    """解析三星事件日期字符串（如 'May 6 ~ 7, 2026'）为 YYYY-MM-DD"""
    # 尝试 "May 6 ~ 7, 2026"、"May 18 ~ 20, 2026" 格式
    m = re.search(r'(\w+)\s+(\d+)\s*~\s*\d+,\s*(\d{4})', date_str)
    if m:
        month_name, day, yr = m.groups()
        try:
            dt = datetime.strptime(f"{month_name} {day} {yr}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(f"{month_name} {day} {yr}", "%b %d %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    # 尝试 "April 30, 2026, 10:00 a.m. KST" 格式
    m = re.search(r'(\w+)\s+(\d+),\s*(\d{4})', date_str)
    if m:
        month_name, day, yr = m.groups()
        try:
            dt = datetime.strptime(f"{month_name} {day} {yr}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(f"{month_name} {day} {yr}", "%b %d %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def _detect_event_type(title: str, event_type_raw: str = "") -> str:
    """根据标题和原始类型判断事件类型"""
    t = title.lower()
    if any(k in t for k in ['investor day', 'analyst day', 'capital market', 'investor conference', '投资者日', '分析师日']):
        return "投资者会议"
    if any(k in t for k in ['annual meeting', 'stockholders', '股东大会', '주주총회']):
        return "股东大会"
    if any(k in t for k in ['earnings', 'financial results', 'conference call', '业绩', '실적']):
        return "财报"
    if any(k in t for k in ['monthly revenue', 'monthly sales', 'sales report', 'revenue report', '月营收']):
        return "经营数据"
    if any(k in t for k in ['conference', 'forum', 'summit', 'symposium', 'oip', 'ndr', '투어']):
        return "会议"
    if event_type_raw:
        et = event_type_raw.lower()
        if 'earnings' in et:
            return "财报"
        if 'conference' in et or 'meeting' in et:
            return "会议"
    return "会议"


def _is_cloudflare_challenge(text: str) -> bool:
    """Detect Cloudflare challenge pages so protected sources are not treated as empty."""
    lowered = text.lower()
    return (
        "just a moment" in lowered
        and ("cloudflare" in lowered or "cf_chl" in lowered or "enable javascript and cookies" in lowered)
    )


def _fetch_pages_with_chrome(urls: List[str]) -> List[Dict]:
    """Read protected pages through a user-controlled Chrome CDP session."""
    script = SCRIPT_DIR / "chrome_cdp_fetch.mjs"
    if not script.exists():
        return []
    try:
        result = subprocess.run(
            ["node", str(script), *urls],
            cwd=str(SCRIPT_DIR.parent),
            check=True,
            text=True,
            capture_output=True,
            timeout=max(45, 25 * len(urls)),
        )
        pages = json.loads(result.stdout)
        return pages if isinstance(pages, list) else []
    except Exception as e:
        logger.warning(f"  Chrome CDP fallback 不可用: {e}")
        return []


def _extract_tsmc_dates(text: str) -> List[Dict]:
    """Extract date/time mentions from TSMC page text."""
    patterns = [
        re.compile(r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?:\s+(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)(?:\s*(?P<tz>TST|CST|Taipei\s+Time|Taiwan\s+Time))?)?"),
        re.compile(r"(?P<date>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})(?:,?\s+(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)(?:\s*(?P<tz>TST|CST|Taipei\s+Time|Taiwan\s+Time))?)?", re.I),
        re.compile(r"(?P<date>\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})(?:,?\s+(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)(?:\s*(?P<tz>TST|CST|Taipei\s+Time|Taiwan\s+Time))?)?", re.I),
    ]
    matches = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            date_str = _parse_date_text(match.group("date"))
            if not date_str:
                continue
            raw_time = (match.groupdict().get("time") or "").strip()
            raw_tz = (match.groupdict().get("tz") or "").strip()
            timezone = "TST" if raw_tz.lower() in {"taipei time", "taiwan time"} else raw_tz.upper()
            matches.append({
                "date": date_str,
                "time": raw_time.upper(),
                "timezone": timezone,
                "start": match.start(),
                "end": match.end(),
            })
    return matches


def _parse_tsmc_date_line(line: str) -> Optional[str]:
    cleaned = re.sub(r"\([^)]*\)", "", line)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _parse_date_text(cleaned)


def _clean_tsmc_title(candidate: str) -> str:
    candidate = re.sub(r"\s+", " ", candidate).strip(" -|：:")
    candidate = re.sub(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*", "", candidate)
    candidate = re.sub(r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"^\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\s*(?:TST|CST|Taipei Time|Taiwan Time)?\s*", "", candidate)
    return candidate.strip(" -|：:")


def _looks_like_tsmc_event_title(candidate: str) -> bool:
    lowered = candidate.lower()
    if len(candidate) < 6:
        return False
    noise = {
        "home", "investor relations", "financial calendar", "events", "press center",
        "search", "menu", "share", "contact us", "copyright", "privacy policy",
    }
    if lowered in noise:
        return False
    keywords = [
        "earnings", "financial results", "investor conference", "conference call",
        "monthly revenue", "revenue", "sales", "investor meeting", "technology symposium",
        "symposium", "oip", "ecosystem forum", "forum", "summit", "webcast",
    ]
    return any(keyword in lowered for keyword in keywords)


def _extract_tsmc_line_events(lines: List[str], source: Dict, url: str) -> List[Dict]:
    today = datetime.now()
    events = []
    seen = set()
    skip_titles = {
        "upcoming events", "past events", "related information", "financial calendar",
        "investor meetings", "tsmc events", "industry events", "all events",
    }
    for idx, line in enumerate(lines):
        date_str = _parse_tsmc_date_line(line)
        if not date_str:
            continue
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        if event_date < today - timedelta(days=7):
            continue

        title = ""
        for candidate in lines[idx + 1: idx + 8]:
            if _parse_tsmc_date_line(candidate):
                break
            cleaned = _clean_tsmc_title(candidate)
            if cleaned.lower() in skip_titles:
                continue
            if _looks_like_tsmc_event_title(cleaned):
                title = cleaned
                break
        if not title:
            continue

        key = (date_str, title, url)
        if key in seen:
            continue
        seen.add(key)

        note = ""
        if "financial-calendar" in url:
            note = "台积电官方 Financial Calendar 识别；含月营收、业绩会等日期。"
        elif "investor-meetings" in url:
            note = "台积电官方 Investor Meetings 页面识别。"
        elif "/events" in url:
            note = "台积电官方 Events 页面识别。"
        events.append(make_event(
            source["id"], date_str, title, _detect_event_type(title), note,
            source["name"], url
        ))
    return events


def crawl_tsmc_ir(source: Dict) -> List[Dict]:
    """TSMC official financial calendar and PR event pages."""
    urls = [source["url"]] + list(source.get("supplemental_urls", []))
    company = source["company"]
    logger.info(f"采集 {company}: {len(urls)} 个官方页面")

    today = datetime.now()
    events = []
    seen = set()
    protected_failures = []

    page_payloads = []
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
        except Exception as e:
            protected_failures.append(f"{url}: {e}")
            continue

        raw_text = resp.text
        if _is_cloudflare_challenge(raw_text):
            protected_failures.append(f"{url}: Cloudflare challenge")
            continue

        page_payloads.append({"url": url, "html": resp.text, "text": ""})

    if protected_failures and len(page_payloads) < len(urls):
        chrome_pages = _fetch_pages_with_chrome(urls)
        for page in chrome_pages:
            if page.get("error"):
                protected_failures.append(f"{page.get('url')}: Chrome CDP {page.get('error')}")
                continue
            text = page.get("text", "") or ""
            html = page.get("html", "") or ""
            if _is_cloudflare_challenge(text + "\n" + html):
                protected_failures.append(f"{page.get('url')}: Chrome CDP still on Cloudflare challenge")
                continue
            if text or html:
                page_payloads.append({"url": page.get("url", ""), "html": html, "text": text})

    for payload in page_payloads:
        url = payload["url"]
        if payload.get("text"):
            page_text = payload["text"]
        else:
            soup = BeautifulSoup(payload.get("html", ""), "html.parser")
            page_text = soup.get_text("\n", strip=True)
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        line_events = _extract_tsmc_line_events(lines, source, url)
        for event in line_events:
            key = (event["date"], event["title"], event["source_url"])
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
            logger.info(f"  发现: {event['title']} ({event['date']})")
        if line_events:
            continue

        joined = "\n".join(lines)
        date_mentions = _extract_tsmc_dates(joined)

        for mention in date_mentions:
            event_date = datetime.strptime(mention["date"], "%Y-%m-%d")
            if event_date < today - timedelta(days=7):
                continue

            line_index = joined[:mention["start"]].count("\n")
            context_lines = lines[max(0, line_index - 4): line_index + 9]
            title = ""
            for candidate in context_lines:
                cleaned = _clean_tsmc_title(candidate)
                if _looks_like_tsmc_event_title(cleaned):
                    title = cleaned
                    break
            if not title:
                title = f"{company} 投资者活动"

            key = (mention["date"], title, url)
            if key in seen:
                continue
            seen.add(key)

            note = ""
            if "financial-calendar" in url:
                note = "台积电官方 Financial Calendar 识别；含月营收、业绩会等日期。"
            elif "investor-meetings" in url:
                note = "台积电官方 Investor Meetings 页面识别。"
            elif "/events" in url:
                note = "台积电官方 Events 页面识别。"

            events.append(make_event(
                source["id"], mention["date"], title, _detect_event_type(title), note,
                source["name"], url,
                event_time=mention["time"], timezone=mention["timezone"]
            ))
            ts = f" {mention['time']} {mention['timezone']}".strip()
            logger.info(f"  发现: {title} ({mention['date']}) {ts}")

    if not events and protected_failures:
        raise RuntimeError("; ".join(protected_failures[:3]))

    return events


def crawl_samsung_ir(source: Dict) -> List[Dict]:
    """
    三星电子 - IR事件页面
    https://www.samsung.com/global/ir/ir-events-presentations/events/
    
    页面为SSR渲染，未来事件在静态HTML中可直接提取。
    同时也提取历史事件中的季度财报信息。
    """
    url = source["url"]
    logger.info(f"采集 三星电子: {url}")
    
    soup = fetch_page(url)
    events = []
    seen_titles = set()  # 去重
    today = datetime.now()
    year = today.year
    this_month = today.month
    
    if soup:
        # 方法1: 从Upcoming Events区域提取事件
        upcoming_section = soup.find('h2', string='Upcoming Events')
        if upcoming_section:
            parent = upcoming_section.find_parent('div', class_='ir-section')
            if parent:
                items = parent.select('li[data-table-date]')
                for item in items:
                    dt_tag = item.find('dt')
                    dd_tag = item.find('dd')
                    if dt_tag and dd_tag:
                        title = dt_tag.get_text(strip=True)
                        date_text = dd_tag.get_text(strip=True)
                        parsed = _parse_samsung_event_date(date_text, year)
                        if parsed and parsed >= today.strftime("%Y-%m-%d"):
                            key = (parsed, title)
                            if key not in seen_titles:
                                seen_titles.add(key)
                                etype = _detect_event_type(title)
                                events.append(make_event(
                                    source["id"], parsed, title, etype, f"时间: {date_text}",
                                    source["name"], url
                                ))
                                logger.info(f"  提取: {title} ({parsed})")
        
        # 方法2: 从历史事件中提取季度财报（如 "1Q26 Earnings Conference Call"）
        past_section = soup.find('h2', string='Past Events')
        if past_section or not upcoming_section:
            # 找所有展开的历史事件区域
            expand_ids = re.findall(r'expandCont(\d+)', str(soup))
            for eid in expand_ids:
                expand_div = soup.find(id=f'expandCont{eid}')
                if not expand_div:
                    continue
                # 找季度财报电话会
                for heading in expand_div.find_all(['strong', 'b', 'h3', 'h4']):
                    h_text = heading.get_text(strip=True)
                    # 匹配 "1Q26 Earnings Conference Call" 格式
                    qm = re.search(r'(\d)[Qq](\d{2})\s+Earnings', h_text)
                    if qm:
                        q_num, short_year = qm.groups()
                        full_year = 2000 + int(short_year)
                        # 找日期
                        parent_div = heading.find_parent()
                        if parent_div:
                            full_text = parent_div.get_text()
                        else:
                            full_text = heading.find_next().get_text() if heading.find_next() else ""
                        
                        date_parsed = _parse_samsung_event_date(full_text, full_year)
                        if date_parsed and date_parsed >= today.strftime("%Y-%m-%d"):
                            quarter_names = {'1': 'Q1', '2': 'Q2', '3': 'Q3', '4': 'Q4'}
                            q_name = quarter_names.get(q_num, f'Q{q_num}')
                            title = f"三星电子{full_year}年{q_name}业绩发布"
                            key = (date_parsed, title)
                            if key not in seen_titles:
                                seen_titles.add(key)
                                events.append(make_event(
                                    source["id"], date_parsed, title, "财报",
                                    f"时间: {full_year}-{q_num}-Q Earnings Conference Call",
                                    source["name"], url
                                ))
                                logger.info(f"  提取: {title} ({date_parsed})")
    
    if not events:
        logger.info("  页面解析未获事件，使用已知季度财报日期")
        today_dt = datetime.now()
        yr = today_dt.year
        known_dates = [
            (f"{yr}-01-31", "三星电子全年业绩发布"),
            (f"{yr}-04-30", "三星电子Q1业绩发布"),
            (f"{yr}-07-31", "三星电子Q2业绩发布"),
            (f"{yr}-10-31", "三星电子Q3业绩发布"),
        ]
        for date_str, title in known_dates:
            if date_str >= today_dt.strftime("%Y-%m-%d"):
                events.append(make_event(
                    source["id"], date_str, title, "财报", "预计日期，以官方公告为准",
                    source["name"], url
                ))
                logger.info(f"  已知日期: {title} ({date_str})")
    
    return events


def crawl_kioxia_ir(source: Dict) -> List[Dict]:
    """
    铠侠 - IR日历
    https://www.kioxia-holdings.com/en-jp/ir/calendar.html
    页面使用 AEM Accordion 组件，事件信息为简单文本对（日期+描述）
    """
    url = source["url"]
    logger.info(f"采集 铠侠: {url}")

    soup = fetch_page(url)
    events = []
    today = datetime.now()

    # 铠侠页面使用 AEM Accordion 组件
    # 结构: .cmp-accordion > .cmp-accordion__item > .cmp-accordion__panel
    # 面板内事件为: 日期行 + 描述行 交替
    for panel in (soup.select('.cmp-accordion__panel') if soup else []):
        text = panel.get_text(strip=True)
        if not text:
            continue
        
        # 按行分割
        lines = [l.strip() for l in panel.decode_contents().split('<br/>')] if '<br/>' in str(panel) else [text]
        
        # 更好的方式：直接获取纯文本按换行分割
        raw_text = panel.get_text('\n', strip=True)
        lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
        
        # 铠侠事件格式：日期行（如 "May 15, 2026"）后跟描述行
        i = 0
        while i < len(lines):
            # 尝试匹配日期行
            date_match = re.search(
                r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
                lines[i], re.IGNORECASE
            )
            if date_match:
                month_str, day_str, year_str = date_match.groups()
                month_map = {
                    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
                    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
                }
                month = month_map.get(month_str.lower(), 1)
                date_str = f"{year_str}-{month:02d}-{int(day_str):02d}"
                
                # 下一行是描述
                title = lines[i + 1] if i + 1 < len(lines) else ""
                
                try:
                    event_date = datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    i += 1
                    continue
                
                # 只保留未来的和近7天的事件
                if event_date < today - timedelta(days=7):
                    i += 2
                    continue
                
                t_lower = title.lower()
                if any(k in t_lower for k in ['financial', 'earnings', 'results', '业绩', '财报']):
                    event_type = "财报"
                elif any(k in t_lower for k in ['shareholder', 'meeting', '股东大会']):
                    event_type = "股东大会"
                else:
                    event_type = "会议"
                
                events.append(make_event(
                    source["id"], date_str, title, event_type, "",
                    source["name"], url
                ))
                logger.info(f"  发现: {title} ({date_str})")
                i += 2
            else:
                i += 1

    if not events:
        logger.info("未采集到新事件（页面结构可能已变更）")
    
    return events


def crawl_nvidia_ir(source: Dict) -> List[Dict]:
    """
    英伟达 - IR日历
    https://investor.nvidia.com/events-and-presentations/events-and-presentations/default.aspx
    
    注意: 页面可能有 Cloudflare 保护
    """
    url = source["url"]
    logger.info(f"采集 英伟达: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    today = datetime.now()
    
    # NVIDIA 的事件列表通常在 table 或 div 中
    for item in soup.find_all(['tr', 'div', 'li', 'article']):
        title_elem = item.find(['a', 'h3', 'h4', 'span'])
        if not title_elem:
            continue
        title = title_elem.get_text(strip=True)
        if len(title) < 10:
            continue
        
        skip = ['events and presentations', 'investor relations', 'nvidia', 'home', 'menu']
        if title.lower() in skip:
            continue
        
        # 查找日期
        text = item.get_text()
        date_match = re.search(
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})',
            text
        )
        if not date_match:
            continue
        
        month_str, day, year = date_match.groups()
        months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                  'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
        month = 0
        for k, v in months.items():
            if month_str.startswith(k):
                month = v
                break
        if not month:
            continue
        
        date_str = f"{year}-{month:02d}-{int(day):02d}"
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        if event_date < today - timedelta(days=7):
            continue
        
        t_lower = title.lower()
        if any(k in t_lower for k in ['earnings', 'financial', 'conference call']):
            event_type = "财报"
        elif any(k in t_lower for k in ['conference', 'summit', 'meeting', 'gdc', 'computex']):
            event_type = "会议"
        elif any(k in t_lower for k in ['stockholder', 'annual meeting']):
            event_type = "股东大会"
        else:
            event_type = "会议"
        
        events.append(make_event(
            source["id"], date_str, title, event_type, "",
            source["name"], url
        ))
        logger.info(f"  发现: {title} ({date_str})")
    
    return events


def crawl_micron_ir(source: Dict) -> List[Dict]:
    """
    美光 - IR页面
    https://investors.micron.com
    """
    url = source["url"]
    logger.info(f"采集 美光: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    today = datetime.now()
    
    for item in soup.find_all(['div', 'li', 'article', 'section', 'tr']):
        title_elem = item.find(['a', 'h3', 'h4', 'h5', 'span'])
        if not title_elem:
            continue
        title = title_elem.get_text(strip=True)
        if len(title) < 10:
            continue
        
        skip = ['investor relations', 'micron', 'home', 'menu', 'events']
        if title.lower() in skip:
            continue
        
        # 过滤噪音：纯时间格式、导航文字
        if re.match(r'^\d{1,2}:\d{2}\s*(AM|PM)\s*(\w+)?$', title.strip(), re.I):
            continue
        if title.strip().lower() in ['events & presentations', 'latest news', 'view all', 'news & events', 'press releases', 'financials']:
            continue
        
        text = item.get_text()
        date_match = re.search(
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})',
            text
        )
        if not date_match:
            date_match = re.search(r'(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})', text)
            if date_match:
                y, mo, d = date_match.groups()
                date_str = f"{y}-{int(mo):02d}-{int(d):02d}"
            else:
                continue
        else:
            month_str, day, year = date_match.groups()
            months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                      'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
            month = 0
            for k, v in months.items():
                if month_str.startswith(k):
                    month = v
                    break
            if not month:
                continue
            date_str = f"{year}-{month:02d}-{int(day):02d}"
        
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        if event_date < today - timedelta(days=7):
            continue
        
        t_lower = title.lower()
        if any(k in t_lower for k in ['earnings', 'financial', 'quarterly']):
            event_type = "财报"
        else:
            event_type = "会议"
        
        events.append(make_event(
            source["id"], date_str, title, event_type, "",
            source["name"], url
        ))
        logger.info(f"  发现: {title} ({date_str})")
    
    if not events:
        # 使用已知财报日期
        year = today.year
        known = [
            (f"{year}-03-26", "美光Q2财报发布"),
            (f"{year}-06-25", "美光Q3财报发布"),
            (f"{year}-09-24", "美光Q4财报发布"),
            (f"{year}-12-18", "美光Q1 FY2027财报发布"),
        ]
        for date_str, title in known:
            if date_str >= today.strftime("%Y-%m-%d"):
                events.append(make_event(
                    source["id"], date_str, title, "财报", "预计日期，以官方公告为准",
                    source["name"], url
                ))
                logger.info(f"  已知日期: {title} ({date_str})")
    
    return events


def crawl_staar_ir(source: Dict) -> List[Dict]:
    """
    Staar Surgical - IR页面
    https://investors.staar.com
    """
    url = source["url"]
    logger.info(f"采集 Staar Surgical: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    today = datetime.now()
    
    for item in soup.find_all(['div', 'li', 'article', 'tr']):
        title_elem = item.find(['a', 'h3', 'h4', 'h5', 'span'])
        if not title_elem:
            continue
        title = title_elem.get_text(strip=True)
        if len(title) < 8:
            continue
        
        # 过滤噪音
        noise_titles = ['events & presentations', 'latest news', 'view all', 'news & events', 'press releases', 'financials', 'events']
        if title.strip().lower() in noise_titles:
            continue
        
        text = item.get_text()
        date_match = re.search(
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})',
            text
        )
        if not date_match:
            continue
        
        month_str, day, year = date_match.groups()
        months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                  'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
        month = 0
        for k, v in months.items():
            if month_str.startswith(k):
                month = v
                break
        if not month:
            continue
        
        date_str = f"{year}-{month:02d}-{int(day):02d}"
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        if event_date < today - timedelta(days=7):
            continue
        
        events.append(make_event(
            source["id"], date_str, title, "财报", "",
            source["name"], url
        ))
        logger.info(f"  发现: {title} ({date_str})")
    
    return events


def crawl_rss_feed(source: Dict) -> List[Dict]:
    """
    通用 RSS Feed 解析器
    适用于亚朵、瑞幸、新东方等提供 RSS 的公司
    """
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company} (RSS): {url}")
    
    try:
        import feedparser
        feed = feedparser.parse(url)
        events = []
        today = datetime.now()
        
        for entry in feed.entries[:20]:
            title = entry.get('title', '')
            if not title:
                continue
            
            date_str = None
            event_time = ""
            timezone = ""
            
            # 匹配: Month DD, YYYY H:MM AM/PM TZ : Title
            dt_match = re.match(r'(\w+\s+\d{1,2},?\s*\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s+([A-Z]+(?:[A-Z/]+)?)\s*[\-\u2013:]\s*(.*)', title)
            if not dt_match:
                # 匹配: M/D/YYYY H:MM AM/PM TZ : Title
                dt_match = re.match(r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s+([A-Z]+(?:[A-Z/]+)?)\s*[\-\u2013:]\s*(.*)', title)
            
            if dt_match:
                raw_date = dt_match.group(1)
                event_time = dt_match.group(2).strip()
                timezone = dt_match.group(3).strip()
                title = dt_match.group(4).strip()
                
                for fmt in ['%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y', '%m/%d/%Y']:
                    try:
                        parsed = datetime.strptime(raw_date.strip(), fmt)
                        date_str = f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"
                        break
                    except:
                        pass
            else:
                date_match = re.match(r'(\d{1,2}/\d{1,2}/\d{4})\s*[\-\u2013:]\s*(.*)', title)
                if date_match:
                    raw_date = date_match.group(1)
                    try:
                        parsed = datetime.strptime(raw_date, '%m/%d/%Y')
                        date_str = f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"
                        title = date_match.group(2).strip()
                    except:
                        pass
            
            if not date_str:
                published = entry.get('published_parsed') or entry.get('updated_parsed')
                if published:
                    date_str = f"{published.tm_year}-{published.tm_mon:02d}-{published.tm_mday:02d}"
                else:
                    continue
            
            event_date = datetime.strptime(date_str, "%Y-%m-%d")
            if event_date < today - timedelta(days=7):
                continue
            
            t_lower = title.lower()
            event_type = _detect_event_type(title)
            link = entry.get("link", "") or url
            
            events.append(make_event(
                source["id"], date_str, title, event_type, "",
                source["name"], link,
                event_time=event_time, timezone=timezone
            ))
            ts = f" {event_time} {timezone}" if event_time else ""
            logger.info(f"  发现: {title} ({date_str}){ts}")
        
        return events
    
    except ImportError:
        logger.warning("  feedparser 未安装，跳过 RSS 源")
        return []
    except Exception as e:
        logger.error(f"  RSS 解析失败: {e}")
        return []


def crawl_trip_com_news_releases(source: Dict) -> List[Dict]:
    """Parse Trip.com news-release RSS and keep only forward-looking IR events."""
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company} (新闻稿RSS): {url}")

    try:
        import feedparser
        feed = feedparser.parse(url)
        events = []
        today = datetime.now()
        seen = set()

        for entry in feed.entries[:20]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            title_lower = title.lower()
            link = entry.get("link", "") or url
            published = entry.get("published_parsed") or entry.get("updated_parsed")

            date_str = None
            event_type = "公告"
            note = ""

            if "annual general meeting" in title_lower or re.search(r"\bagm\b", title_lower):
                date_match = re.search(r"on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", title)
                if date_match:
                    parsed = datetime.strptime(date_match.group(1), "%B %d, %Y")
                    date_str = f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"
                event_type = "股东大会"
                note = "新闻稿RSS识别的年度股东大会公告。"

            elif re.search(r"\bto report\b.*\bfinancial results\b", title_lower):
                date_match = re.search(r"on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", title)
                if date_match:
                    parsed = datetime.strptime(date_match.group(1), "%B %d, %Y")
                    date_str = f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"
                event_type = "财报"
                note = "新闻稿RSS识别的业绩披露日期公告。"

            if not date_str:
                continue

            event_date = datetime.strptime(date_str, "%Y-%m-%d")
            if event_date < today - timedelta(days=7):
                continue

            key = (date_str, title, link)
            if key in seen:
                continue
            seen.add(key)

            if published:
                note = (note + f" 公告发布日期: {published.tm_year}-{published.tm_mon:02d}-{published.tm_mday:02d}.").strip()

            events.append(make_event(
                source["id"], date_str, title, event_type, note,
                source["name"], link
            ))
            logger.info(f"  发现: {title} ({date_str})")

        return events

    except ImportError:
        logger.warning("  feedparser 未安装，跳过 RSS 源")
        return []
    except Exception as e:
        logger.error(f"  新闻稿RSS解析失败: {e}")
        return []


def crawl_news_release_rss(source: Dict) -> List[Dict]:
    """Parse company news-release RSS and keep only forward-looking IR events."""
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company} (新闻稿RSS): {url}")

    try:
        import feedparser
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        events = []
        today = datetime.now()
        seen = set()

        for entry in feed.entries[:30]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            title_lower = title.lower()
            link = entry.get("link", "") or url
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            body = " ".join([
                title,
                entry.get("summary", "") or "",
                entry.get("description", "") or "",
            ])

            date_str = None
            event_time = ""
            timezone = ""
            event_type = "公告"
            note = "新闻稿RSS识别的前瞻性IR事件。"

            event_patterns = [
                (
                    r"\bannual general meeting\b|\bagm\b",
                    "股东大会",
                    [
                        r"(?:on|for)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    ],
                ),
                (
                    r"\bto report\b.*\b(?:financial|fiscal|quarter|year).*\bresults\b|\bto announce\b.*\bresults\b",
                    "财报",
                    [
                        r"(?:on|for)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    ],
                ),
                (
                    r"\binvestor day\b|\banalyst day\b|\bcapital markets day\b|\bcapital market day\b",
                    "投资者会议",
                    [
                        r"(?:on|for)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    ],
                ),
                (
                    r"\bto participate\b.*\bconference\b|\bto present\b.*\bconference\b|\bupcoming investor conference\b",
                    "投资者会议",
                    [
                        r"(?:on|at)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                        r"([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                    ],
                ),
            ]

            for trigger, detected_type, date_patterns in event_patterns:
                if not re.search(trigger, title_lower):
                    continue
                event_type = detected_type
                for date_pattern in date_patterns:
                    date_match = re.search(date_pattern, body, re.I)
                    if not date_match:
                        continue
                    raw_date = date_match.group(1)
                    for fmt in ["%B %d, %Y", "%b %d, %Y"]:
                        try:
                            parsed = datetime.strptime(raw_date, fmt)
                            date_str = f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"
                            break
                        except ValueError:
                            pass
                    if date_str:
                        break
                break

            if not date_str:
                continue

            time_match = re.search(
                r"(\d{1,2}:\d{2}\s*(?:a\.m\.|p\.m\.|AM|PM|am|pm))\s*(?:"
                r"((?:Eastern|Central|Mountain|Pacific)\s+Time|[A-Z]{2,4})"
                r")?",
                body,
                re.I,
            )
            if time_match:
                event_time = (
                    time_match.group(1)
                    .replace("a.m.", "AM")
                    .replace("p.m.", "PM")
                    .upper()
                )
                raw_tz = (time_match.group(2) or "").strip()
                tz_map = {
                    "EASTERN TIME": "ET",
                    "CENTRAL TIME": "CT",
                    "MOUNTAIN TIME": "MT",
                    "PACIFIC TIME": "PT",
                }
                timezone = tz_map.get(raw_tz.upper(), raw_tz.upper())

            event_date = datetime.strptime(date_str, "%Y-%m-%d")
            if event_date < today - timedelta(days=7):
                continue

            key = (date_str, title, link)
            if key in seen:
                continue
            seen.add(key)

            if published:
                note = (note + f" 公告发布日期: {published.tm_year}-{published.tm_mon:02d}-{published.tm_mday:02d}.").strip()

            events.append(make_event(
                source["id"], date_str, title, event_type, note,
                source["name"], link,
                event_time=event_time, timezone=timezone
            ))
            ts = f" {event_time} {timezone}" if event_time else ""
            logger.info(f"  发现: {title} ({date_str}){ts}")

        return events

    except ImportError:
        logger.warning("  feedparser 未安装，跳过新闻稿RSS源")
        return []
    except Exception as e:
        logger.error(f"  新闻稿RSS解析失败: {e}")
        return []





def crawl_gree_ir(source: Dict) -> List[Dict]:
    """格力电器专用爬虫 - 通过AkShare获取财报预约披露时间"""
    company = source["company"]
    logger.info(f"采集 {company}: AkShare stock_report_disclosure")
    
    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare 未安装")
        return []
    
    events = []
    today = datetime.now().strftime("%Y-%m-%d")
    
    periods = ['2025年报', '2025半年报', '2025三季报']
    for period in periods:
        try:
            df = ak.stock_report_disclosure(market='沪深京', period=period)
            gree = df[df['股票简称'].str.contains('格力', na=False)]
            if not gree.empty:
                row = gree.iloc[0]
                date_val = row.get('实际披露')
                if str(date_val) in ('NaT', 'None', 'nan', ''):
                    date_val = row.get('首次预约')
                if date_val and str(date_val) not in ('NaT', 'None', 'nan', ''):
                    date_str = str(date_val)[:10]
                    if date_str >= today:
                        title = f"{company} {period}披露"
                        events.append(make_event(
                            source["id"], date_str, title, "财报", "",
                            source["name"], "https://www.cninfo.com.cn"
                        ))
                        logger.info(f"  发现: [{date_str}] {title}")
        except Exception as e:
            logger.debug(f"  查询 {period} 失败: {e}")
    
    return events

def crawl_midea_ir(source: Dict) -> List[Dict]:
    """美的集团专用爬虫 - 通过AkShare获取财报预约披露时间"""
    company = source["company"]
    logger.info(f"采集 {company}: AkShare stock_report_disclosure")
    
    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare 未安装")
        return []
    
    events = []
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 查询各期财报披露时间
    periods = ['2025年报', '2025半年报', '2025三季报']
    for period in periods:
        try:
            df = ak.stock_report_disclosure(market='沪深京', period=period)
            midea = df[df['股票简称'].str.contains('美的', na=False)]
            if not midea.empty:
                row = midea.iloc[0]
                # 优先取实际披露日期，否则取首次预约
                date_val = row.get('实际披露')
                if str(date_val) in ('NaT', 'None', 'nan', ''):
                    date_val = row.get('首次预约')
                if date_val and str(date_val) not in ('NaT', 'None', 'nan', ''):
                    date_str = str(date_val)[:10]
                    if date_str >= today:
                        title = f"{company} {period}披露"
                        events.append(make_event(
                            source["id"], date_str, title, "财报", "",
                            source["name"], "https://www.cninfo.com.cn"
                        ))
                        logger.info(f"  发现: [{date_str}] {title}")
        except Exception as e:
            logger.debug(f"  查询 {period} 失败: {e}")
    
    return events

def crawl_haier_ir(source: Dict) -> List[Dict]:
    """海尔智家专用爬虫 - 财经日历结构：日期拆分为日+年月，需HTML实体解码"""
    import html as htmlmod
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company}: {url}")
    
    # 直接用requests获取并解码HTML实体
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        raw = resp.text
    except Exception as e:
        logger.error(f"获取页面失败 {url}: {e}")
        return []
    
    decoded = htmlmod.unescape(raw)
    soup = BeautifulSoup(decoded, 'html.parser')
    
    events = []
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 找所有日历项
    for item in soup.find_all('div', class_='bg-calendar'):
        h3 = item.find('h3')
        if not h3:
            continue
        
        title = h3.get_text(strip=True)
        
        # 找日期：大字日 + 小字年月
        all_divs = item.find_all('div')
        day_text = None
        month_text = None
        for d in all_divs:
            text = d.get_text(strip=True)
            cls = d.get('class', [])
            cls_str = ' '.join(cls) if cls else ''
            if '3.2rem' in cls_str and '日' in text:
                day_text = text.replace('日', '')
            elif '1.4rem' in cls_str and '年' in text and '月' in text:
                month_text = text
        
        if day_text and month_text and day_text.isdigit():
            m = re.match(r'(\d{4})年(\d{1,2})月', month_text)
            if m:
                y, mo = m.group(1), m.group(2)
                date_str = f"{y}-{int(mo):02d}-{int(day_text):02d}"
                
                if date_str >= today:
                    events.append(make_event(
                        source["id"], date_str, title, "其他", "",
                        source["name"], url
                    ))
                    logger.info(f"  发现: [{date_str}] {title}")
    
    return events

def crawl_prosus_ir(source: Dict) -> List[Dict]:
    """Prosus专用爬虫 - 表格结构：每行两个td.body-large"""
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company}: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    today = datetime.now().strftime("%Y-%m-%d")
    months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
    
    # 找所有表格行
    for tr in soup.find_all('tr'):
        tds = tr.find_all('td', class_='body-large')
        if len(tds) >= 2:
            date_text = tds[0].get_text(strip=True)
            title_text = tds[1].get_text(strip=True)
            
            # 解析日期 "29 Jun 2026"
            m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', date_text)
            if m:
                d, month_str, y = m.groups()
                month = 0
                for k, v in months.items():
                    if month_str.startswith(k):
                        month = v
                        break
                if month:
                    date_str = f"{y}-{month:02d}-{int(d):02d}"
                    if date_str >= today and title_text:
                        events.append(make_event(
                            source["id"], date_str, title_text, "其他", "",
                            source["name"], url
                        ))
                        logger.info(f"  发现: [{date_str}] {title_text}")
    
    return events


def crawl_generic_ir(source: Dict) -> List[Dict]:
    """
    通用IR页面解析器 - 兜底方案
    尝试从页面中提取日期和事件标题
    """
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company}: {url}")
    
    soup = fetch_page(url)
    if not soup:
        return []
    
    events = []
    today = datetime.now()
    text = soup.get_text()
    
    # 查找所有可能的日期
    date_patterns = [
        r'(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})',
        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})',
        r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})',
        r'(\d{4})年(\d{1,2})月(\d{1,2})日',
    ]
    
    found_dates = set()
    for pattern in date_patterns:
        for m in re.finditer(pattern, text):
            groups = m.groups()
            if len(groups) == 3:
                if groups[0].isdigit() and len(groups[0]) == 4:
                    y, mo, d = groups
                    date_str = f"{y}-{int(mo):02d}-{int(d):02d}"
                elif groups[2].isdigit() and len(groups[2]) == 4:
                    months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
                    # Handle both "Month DD, YYYY" and "DD Month YYYY"
                    if groups[0].isdigit():
                        d, month_str, y = groups
                    else:
                        month_str, d, y = groups
                    month = 0
                    for k, v in months.items():
                        if month_str.startswith(k):
                            month = v
                            break
                    if month:
                        date_str = f"{y}-{month:02d}-{int(d):02d}"
                    else:
                        continue
                else:
                    continue
                
                if date_str >= today.strftime("%Y-%m-%d") and date_str not in found_dates:
                    found_dates.add(date_str)
                    events.append(make_event(
                        source["id"], date_str, f"{company} 投资者活动", "其他", "自动采集，请核实",
                        source["name"], url
                    ))
                    logger.info(f"  发现日期: {date_str}")
    
    return events


def _parse_date_text(raw_date: str) -> Optional[str]:
    raw = re.sub(r"\s+", " ", raw_date.strip())
    for fmt in [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
    ]:
        try:
            parsed = datetime.strptime(raw, fmt)
            return f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"
        except ValueError:
            pass
    return None


def crawl_event_text_blocks(source: Dict) -> List[Dict]:
    """Parse IR pages where date and event title appear as nearby text blocks."""
    url = source["url"]
    company = source["company"]
    logger.info(f"采集 {company}: {url}")

    soup = fetch_page(url)
    if not soup:
        return []

    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    today = datetime.now()
    events = []
    seen = set()

    date_patterns = [
        re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"),
        re.compile(r"^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}$", re.I),
        re.compile(r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}$", re.I),
    ]
    skip_titles = {
        "date", "headline", "year", "click for more", "home", "investor relations",
        "financial calendar", "ir calendar", "upcoming events", "view all events",
    }

    for idx, line in enumerate(lines):
        if not any(pattern.match(line) for pattern in date_patterns):
            continue
        date_str = _parse_date_text(line)
        if not date_str:
            continue
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        if event_date < today - timedelta(days=7):
            continue

        title = ""
        for candidate in lines[idx + 1: idx + 8]:
            lowered = candidate.lower()
            if lowered in skip_titles:
                continue
            if any(pattern.match(candidate) for pattern in date_patterns):
                break
            if len(candidate) >= 6:
                title = candidate
                break
        if not title:
            title = f"{company} 投资者活动"

        key = (date_str, title)
        if key in seen:
            continue
        seen.add(key)

        event_type = _detect_event_type(title)
        events.append(make_event(
            source["id"], date_str, title, event_type, "",
            source["name"], url
        ))
        logger.info(f"  发现: {title} ({date_str})")

    return events


# ============================================================
# 解析器注册表
# ============================================================

CRAWLERS = {
    "tencent_ir": crawl_tencent_ir,
    "tencent_roadshows": crawl_tencent_roadshows,
    "intel_ir_calendar": crawl_intel_ir,
    "skhynix_ir_events": crawl_skhynix_ir_events,
    "skhynix_earnings": crawl_skhynix_ir_events,  # 旧ID保持兼容
    "samsung_earnings": crawl_samsung_ir,
    "samsung_ir_events": crawl_samsung_ir,
    "tsmc_ir": crawl_tsmc_ir,
    "kioxia_ir_calendar": crawl_kioxia_ir,
    "nvidia_ir_calendar": crawl_nvidia_ir,
    "micron_ir_calendar": crawl_micron_ir,
    "staar_ir_calendar": crawl_staar_ir,
    "gree_ir_calendar": crawl_gree_ir,
    "midea_ir_calendar": crawl_midea_ir,
    "haier_ir_events": crawl_haier_ir,
    "prosus_ir_calendar": crawl_prosus_ir,
    # RSS 类型
    "atour_rss": crawl_rss_feed,
    "atour_ir_rss": crawl_rss_feed,
    "luckin_rss": crawl_rss_feed,
    "luckin_ir_rss": crawl_rss_feed,
    "neworiental_rss": crawl_rss_feed,
    "neworiental_ir_rss": crawl_rss_feed,
    "pdd_ir_rss": crawl_rss_feed,
    "trip_com_ir_events": crawl_rss_feed,
    "trip_com_news_releases": crawl_trip_com_news_releases,
    "intel_news_releases": crawl_news_release_rss,
    "joyy_news_releases": crawl_news_release_rss,
    "vip_news_releases": crawl_news_release_rss,
    "huazhu_news_releases": crawl_news_release_rss,
    "atour_news_releases": crawl_news_release_rss,
    "luckin_news_releases": crawl_news_release_rss,
    "neworiental_news_releases": crawl_news_release_rss,
    "pdd_news_releases": crawl_news_release_rss,
    "sandisk_news_releases": crawl_news_release_rss,
    "micron_news_releases": crawl_news_release_rss,
    "futu_news_releases": crawl_news_release_rss,
    "nvidia_ir_calendar": crawl_rss_feed,
    "sandisk_ir_events": crawl_rss_feed,
    "joyy_ir_events": crawl_rss_feed,
    "vip_ir_calendar": crawl_rss_feed,
    "huazhu_ir_calendar": crawl_rss_feed,
    "li_ning_ir_calendar": crawl_event_text_blocks,
    "smic_ir_calendar": crawl_event_text_blocks,
    "futu_ir_calendar": crawl_event_text_blocks,
}


def load_sources(list_names: List[str] = None) -> List[Dict]:
    """加载IR源配置"""
    config_file = CONFIG_DIR / "ir_sources.json"
    if not config_file.exists():
        logger.error(f"配置文件不存在: {config_file}")
        return []
    
    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)
    
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    if list_names is not None:
        allowed_ids = allowed_ir_source_ids(list_names)
        sources = [s for s in sources if s.get("id") in allowed_ids]
    supplemental_urls = {
        url
        for source in sources
        for url in (source.get("supplemental_urls") or [])
        if url
    }
    sources = [
        source for source in sources
        if not (
            source.get("crawl_method") == "rss_news_releases"
            and source.get("url") in supplemental_urls
        )
    ]
    return sources


def _cross_source_event_key(event: Dict, company: str) -> tuple:
    title = re.sub(r"\s+", " ", event.get("title", "")).strip().casefold()
    special_types = [
        ("investor_day", r"\binvestor\s+day\b|投资者日"),
        ("analyst_day", r"\banalyst\s+day\b|分析师日"),
        ("capital_markets_day", r"\bcapital\s+markets?\s+day\b|资本市场日"),
        ("annual_general_meeting", r"\bannual\s+general\s+meeting\b|\bagm\b|年度股东大会"),
    ]
    for category, pattern in special_types:
        if re.search(pattern, title, re.I):
            return (company.casefold(), event.get("date", ""), category)
    return (event.get("title", ""), event.get("date", ""))


def save_events(events: List[Dict], test_mode: bool = False):
    """保存事件 — delegates to the PG-backed _event_store shim.

    The shim handles garbage filtering, semantic dedup, and UPSERT into
    corporate_events. Preserves the zip's test_mode print contract.
    """
    if test_mode:
        logger.info("测试模式: 不保存")
    result = _shim_save(events, test_mode=test_mode)
    if not test_mode:
        logger.info(f"保存完成: 新增 {result['new']} 个事件, 本次写入 {result['total']} 个")
    return result


def main():
    parser = argparse.ArgumentParser(description="投研日历 IR网页采集 v2")
    parser.add_argument("--source", help="指定采集源ID")
    parser.add_argument("--all", action="store_true", help="采集所有启用源")
    parser.add_argument("--test", action="store_true", help="测试模式")
    parser.add_argument("--list", action="store_true", help="列出所有源")
    parser.add_argument("--company-list", action="append", dest="company_lists",
                        help="指定公司列表，可重复传入或用逗号分隔；默认使用 officecodex 全局公司列表的 active_list")
    parser.add_argument("--list-company-lists", action="store_true", help="列出可用公司列表")
    parser.add_argument("--check-failures", action="store_true", help="查看最近失败的采集")
    parser.add_argument("--check-coverage", action="store_true", help="查看上次逐源覆盖状态")
    
    args = parser.parse_args()

    if args.list_company_lists:
        print("公司列表:")
        for item in list_summary():
            print(f"  {item['id']:16s} | {item['name']} | IR源 {item['ir_sources']} | A股 {item['a_shares']}")
        return

    try:
        selected_lists = resolve_list_names(args.company_lists)
    except CompanyListError as e:
        logger.error(str(e))
        return
    
    if args.list:
        sources = load_sources(selected_lists)
        print(f"采集源列表（公司列表: {', '.join(selected_lists)}）:")
        for s in sources:
            crawler = CRAWLERS.get(s["id"], crawl_generic_ir)
            crawler_name = crawler.__name__
            print(f"  {s['id']:25s} | {s['company']:10s} | {crawler_name}")
        return
    
    if args.check_failures:
        try:
            with open(FAILURE_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            print(f"\n⚠️ 上次IR采集失败记录 ({state['last_run']}):")
            for f in state['failures']:
                print(f"  [{f['company']}] {f['error']}")
            print()
        except (FileNotFoundError, json.JSONDecodeError):
            print("✅ 无最近失败记录")
        return

    if args.check_coverage:
        try:
            with open(COVERAGE_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            summary = state.get("summary", {})
            print(
                f"IR覆盖状态 ({state.get('last_run', '')}): "
                f"complete={summary.get('complete', 0)} "
                f"partial={summary.get('partial', 0)} "
                f"failed={summary.get('failed', 0)}"
            )
            for item in state.get("sources", []):
                if item.get("status") == "complete":
                    continue
                print(
                    f"  [{item.get('status')}] {item.get('company')} "
                    f"{item.get('source_id')}: {item.get('reason', '')}"
                )
        except (FileNotFoundError, json.JSONDecodeError):
            print("尚无IR覆盖状态")
        return
    
    try:
        sources = load_sources(selected_lists)
    except CompanyListError as e:
        logger.error(str(e))
        return
    
    if args.source:
        source = next((s for s in sources if s["id"] == args.source), None)
        if not source:
            logger.error(f"未找到源: {args.source}（当前公司列表: {', '.join(selected_lists)}）")
            return
        sources = [source]
    elif not args.all:
        logger.info("请指定 --source <id> 或 --all")
        return

    logger.info(f"使用公司列表: {', '.join(selected_lists)}；IR源数量: {len(sources)}")
    
    all_events = []
    failures = []
    coverage_rows = []
    for source in sources:
        source_id = source["id"]
        company = source.get("company", source_id)
        crawler = CRAWLERS.get(source_id, crawl_generic_ir)
        global _ACTIVE_SOURCE_ID
        _ACTIVE_SOURCE_ID = source_id
        _SOURCE_FETCH_TRACE[source_id] = []
        
        try:
            primary_events = crawler(source)
            supplemental_events, supplemental_checks = crawl_supplemental_ir_sources(source)
            events = primary_events + supplemental_events
            all_events.extend(events)

            fetch_failures = [
                row for row in _SOURCE_FETCH_TRACE.get(source_id, [])
                if not row.get("ok")
            ]
            incomplete_checks = [
                row for row in supplemental_checks
                if row.get("status") != "ok"
            ]
            status = "complete"
            reasons = []
            if fetch_failures or incomplete_checks:
                status = "partial"
                if fetch_failures:
                    reasons.append(f"主解析路径访问失败 {len(fetch_failures)} 个")
                failed_supplements = [
                    row for row in incomplete_checks if row.get("status") == "failed"
                ]
                unparsed_supplements = [
                    row for row in incomplete_checks if row.get("status") == "unparsed_signals"
                ]
                if failed_supplements:
                    reasons.append(f"补充源失败 {len(failed_supplements)} 个")
                if unparsed_supplements:
                    reasons.append(f"有日程线索但日期未解析 {len(unparsed_supplements)} 个")

            coverage_rows.append({
                "source_id": source_id,
                "company": company,
                "status": status,
                "reason": "；".join(reasons),
                "primary_event_count": len(primary_events),
                "supplemental_event_count": len(supplemental_events),
                "total_event_count": len(events),
                "primary_fetches": _SOURCE_FETCH_TRACE.get(source_id, []),
                "supplemental_checks": supplemental_checks,
            })
            if status != "complete":
                failures.append({
                    "source_id": source_id,
                    "company": company,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "error": f"IR覆盖检查不完整: {'；'.join(reasons)}"[:200],
                })
        except Exception as e:
            logger.error(f"采集 {source_id} 失败: {e}")
            failures.append({
                "source_id": source_id,
                "company": company,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "error": str(e)[:200]
            })
            coverage_rows.append({
                "source_id": source_id,
                "company": company,
                "status": "failed",
                "reason": str(e)[:300],
                "primary_event_count": 0,
                "supplemental_event_count": 0,
                "total_event_count": 0,
                "primary_fetches": _SOURCE_FETCH_TRACE.get(source_id, []),
                "supplemental_checks": [],
            })
        finally:
            _ACTIVE_SOURCE_ID = ""

    coverage_summary = {
        status: sum(1 for row in coverage_rows if row.get("status") == status)
        for status in ("complete", "partial", "failed")
    }
    if not args.test:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(COVERAGE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "company_lists": selected_lists,
                "summary": coverage_summary,
                "sources": coverage_rows,
            }, f, ensure_ascii=False, indent=2)
    logger.info(
        "IR覆盖状态: complete=%s partial=%s failed=%s",
        coverage_summary["complete"],
        coverage_summary["partial"],
        coverage_summary["failed"],
    )
    
    # 保存失败记录
    if failures and not args.test:
        fail_state = {
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "failures": failures
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(FAILURE_STATE_FILE, 'w', encoding="utf-8") as f:
            json.dump(fail_state, f, ensure_ascii=False, indent=2)
        
        # 追加到历史日志
        try:
            with open(FAILURE_LOG_FILE, encoding="utf-8") as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            history = []
        history.append(fail_state)
        history = history[-30:]  # 保留最近30条
        with open(FAILURE_LOG_FILE, 'w', encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        
        logger.warning(f"本次 {len(failures)} 个源采集失败，已记录")
        for f in failures:
            logger.warning(f"  [{f['company']}] {f['error']}")
    elif not args.test:
        # 清空失败状态（成功一次代表已修复）
        if FAILURE_STATE_FILE.exists():
            os.remove(FAILURE_STATE_FILE)
    
    # 跨源去重：专属活动按公司+日期+类别合并，优先保留先到的精确事件源。
    source_companies = {
        source.get("id", ""): source.get("company", "")
        for source in sources
    }
    seen = {}
    unique_events = []
    for e in all_events:
        company = source_companies.get(str(e.get("id", "")).split("_20", 1)[0], "")
        if not company:
            source_id = next(
                (sid for sid in source_companies if str(e.get("id", "")).startswith(f"{sid}_")),
                "",
            )
            company = source_companies.get(source_id, "")
        key = _cross_source_event_key(e, company)
        if key not in seen:
            seen[key] = len(unique_events)
            unique_events.append(e)
        else:
            backfill_event_details(unique_events[seen[key]], e)
    
    if unique_events:
        save_events(unique_events, test_mode=args.test)
    else:
        logger.info("未采集到新事件")


if __name__ == "__main__":
    main()
