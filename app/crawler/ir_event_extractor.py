#!/usr/bin/env python3
"""Generic extraction of forward-looking IR events from official text."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup


MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

MONTH_TOKEN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_TOKEN = (
    rf"(?:{MONTH_TOKEN}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH_TOKEN}\.?,?\s+\d{{4}}|"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{4}年\d{1,2}月\d{1,2}日)"
)

SCHEDULE_CUE_RE = re.compile(
    r"\b(?:scheduled\s+(?:for|on)|to\s+(?:report|announce|release|hold|host|present|participate)|"
    r"will\s+(?:report|announce|release|hold|host|present|participate)|"
    r"upcoming|takes?\s+place|will\s+be\s+held)\b|"
    r"(?:定于|将于|予定|開催予定|発表予定)",
    re.I,
)

EVENT_RULES = [
    (
        "财报",
        re.compile(
            r"\b(?:financial\s+results?|earnings(?:\s+(?:release|call|conference\s+call))?|"
            r"results?\s+(?:release|announcement|conference\s+call)|quarterly\s+results?|"
            r"(?:fiscal|quarter|annual|full[- ]year)[^.;]{0,80}\bresults?)\b|"
            r"(?:业绩(?:发布|公布|说明会|电话会)?|財務業績|決算(?:発表|説明会)?)",
            re.I,
        ),
    ),
    (
        "投资者会议",
        re.compile(
            r"\b(?:investor\s+day|analyst\s+day|capital\s+markets?\s+day|"
            r"investor\s+conference|investor\s+meeting|conference\s+appearance)\b|"
            r"(?:投资者日|分析师日|资本市场日|投資家向け説明会)",
            re.I,
        ),
    ),
    (
        "股东大会",
        re.compile(
            r"\b(?:annual\s+general\s+meeting|annual\s+meeting\s+of\s+(?:shareholders|stockholders)|AGM)\b|"
            r"(?:年度股东大会|股東総会)",
            re.I,
        ),
    ),
]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_date(raw: str) -> Optional[str]:
    value = _clean_text(raw).replace("Sept.", "Sep.").replace("sept.", "sep.")
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value, flags=re.I)
    value = re.sub(r"(?<=[A-Za-z])\.(?=\s+\d)", "", value)

    ymd = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", value)
    if ymd:
        year, month, day = map(int, ymd.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    month_first = re.fullmatch(
        rf"({MONTH_TOKEN})\s+(\d{{1,2}}),?\s+(\d{{4}})",
        value,
        re.I,
    )
    if month_first:
        month_name, day, year = month_first.groups()
        month = MONTHS.get(month_name.lower().rstrip("."))
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    day_first = re.fullmatch(
        rf"(\d{{1,2}})\s+({MONTH_TOKEN}),?\s+(\d{{4}})",
        value,
        re.I,
    )
    if day_first:
        day, month_name, year = day_first.groups()
        month = MONTHS.get(month_name.lower().rstrip("."))
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    return None


def _extract_time_and_timezone(text: str) -> tuple[str, str]:
    time_match = re.search(
        r"\b(?:at\s+)?(\d{1,2}):(\d{2})\s*"
        r"(a\.?m\.?|p\.?m\.?)?\s*"
        r"(?:\(?\s*((?:Eastern|Central|Mountain|Pacific)\s+Time|"
        r"JST|KST|HKT|SGT|ICT|ET|CT|MT|PT|EST|EDT|CST|CDT|MST|MDT|PST|PDT|UTC|GMT)\s*\)?)?",
        text,
        re.I,
    )
    if not time_match:
        return "", ""

    hour = int(time_match.group(1))
    minute = int(time_match.group(2))
    meridiem = (time_match.group(3) or "").lower().replace(".", "")
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return "", ""

    timezone = (time_match.group(4) or "").strip().upper()
    timezone = {
        "EASTERN TIME": "ET",
        "CENTRAL TIME": "CT",
        "MOUNTAIN TIME": "MT",
        "PACIFIC TIME": "PT",
    }.get(timezone, timezone)
    return f"{hour:02d}:{minute:02d}", timezone


def _date_matches(text: str) -> List[Dict[str, Any]]:
    matches = []
    for match in re.finditer(DATE_TOKEN, text, re.I):
        parsed = _parse_date(match.group(0))
        if parsed:
            matches.append({
                "date": parsed,
                "start": match.start(),
                "end": match.end(),
                "raw": match.group(0),
            })
    return matches


def _nearest_date(
    dates: Iterable[Dict[str, Any]],
    trigger_start: int,
    trigger_end: int,
) -> Optional[Dict[str, Any]]:
    candidates = []
    for item in dates:
        if item["start"] >= trigger_end:
            distance = item["start"] - trigger_end
            direction_penalty = 0
        else:
            distance = trigger_start - item["end"]
            direction_penalty = 80
        if distance <= 220:
            candidates.append((distance + direction_penalty, item))
    return min(candidates, key=lambda row: row[0])[1] if candidates else None


def _canonical_title(
    original_title: str,
    company: str,
    event_type: str,
    trigger_text: str,
    multiple_events: bool,
) -> str:
    title = _clean_text(original_title)
    if title and not multiple_events:
        return title
    trigger = _clean_text(trigger_text)
    if event_type == "投资者会议":
        if re.search(r"investor\s+day|投资者日", trigger, re.I):
            return f"{company} Investor Day"
        if re.search(r"analyst\s+day|分析师日", trigger, re.I):
            return f"{company} Analyst Day"
        if re.search(r"capital\s+markets?\s+day|资本市场日", trigger, re.I):
            return f"{company} Capital Markets Day"
    if event_type == "股东大会":
        return f"{company} Annual General Meeting"
    return title or f"{company} {trigger}"


def extract_forward_ir_events(
    text: str,
    *,
    title: str,
    link: str,
    company: str,
    today: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Extract dated future IR events and report unresolved scheduling signals."""
    now = today or datetime.now()
    normalized = _clean_text(text)
    dates = _date_matches(normalized)
    matches = []
    signal_count = 0
    resolved_signal_count = 0
    unresolved = []

    for event_type, trigger_re in EVENT_RULES:
        for trigger in trigger_re.finditer(normalized):
            context_start = max(0, trigger.start() - 100)
            context_end = min(len(normalized), trigger.end() + 260)
            context = normalized[context_start:context_end]
            has_schedule_cue = bool(SCHEDULE_CUE_RE.search(context))
            date_item = _nearest_date(dates, trigger.start(), trigger.end())
            prefix = normalized[max(0, trigger.start() - 40):trigger.start()]
            announces_dated_event = bool(
                date_item
                and date_item["start"] >= trigger.end()
                and re.search(r"\bannounces?\s+(?:an?\s+)?$", prefix, re.I)
            )
            has_schedule_cue = has_schedule_cue or announces_dated_event
            if not has_schedule_cue:
                continue
            signal_count += 1
            if not date_item:
                unresolved.append(_clean_text(context)[:300])
                continue
            resolved_signal_count += 1
            event_date = datetime.strptime(date_item["date"], "%Y-%m-%d")
            if event_date < now - timedelta(days=7):
                continue

            detail_start = max(0, min(trigger.start(), date_item["start"]) - 80)
            detail_end = min(len(normalized), max(trigger.end(), date_item["end"]) + 140)
            detail = normalized[detail_start:detail_end]
            event_time, timezone = _extract_time_and_timezone(detail)
            matches.append({
                "date": date_item["date"],
                "type": event_type,
                "trigger": trigger.group(0),
                "time": event_time,
                "timezone": timezone,
                "source_url": link,
            })

    unique = []
    seen = set()
    for item in matches:
        key = (item["date"], item["type"], item["trigger"].casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    multiple_events = len({(item["date"], item["type"]) for item in unique}) > 1
    events = []
    for item in unique:
        event_title = _canonical_title(
            title,
            company,
            item["type"],
            item["trigger"],
            multiple_events,
        )
        events.append({
            "date": item["date"],
            "title": event_title,
            "type": item["type"],
            "time": item["time"],
            "timezone": item["timezone"],
            "source_url": item["source_url"],
        })

    if not events and resolved_signal_count:
        unresolved = []

    return {
        "events": events,
        "signal_count": signal_count,
        "resolved_signal_count": resolved_signal_count,
        "unparsed_signals": list(dict.fromkeys(unresolved)),
    }


def extract_events_from_html(
    html: str,
    *,
    page_url: str,
    company: str,
    today: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Scan news/event list items first, then use the whole page as a fallback."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen_text = set()
    selectors = [
        "article",
        "li.box",
        "li[class*='news']",
        "li[class*='event']",
        "div[class*='news-item']",
        "div[class*='event-item']",
        "tr",
    ]
    for node in soup.select(", ".join(selectors)):
        text = _clean_text(node.get_text(" ", strip=True))
        if len(text) < 20 or len(text) > 2500 or text in seen_text:
            continue
        seen_text.add(text)
        anchor = node.find("a", href=True)
        heading = node.find(["h1", "h2", "h3", "h4", "strong"])
        node_title = _clean_text(
            (heading.get_text(" ", strip=True) if heading else "")
            or (anchor.get_text(" ", strip=True) if anchor else "")
        )
        if not node_title:
            title_prefix = re.split(
                r"\s*\((?:Scheduled|予定)|\s+(?:Scheduled|予定)\s+",
                text,
                maxsplit=1,
                flags=re.I,
            )[0]
            if 8 <= len(title_prefix) <= 300:
                node_title = title_prefix
        link = urljoin(page_url, anchor["href"]) if anchor else page_url
        candidates.append((text, node_title, link))

    if not candidates:
        candidates.append((_clean_text(soup.get_text(" ", strip=True)), "", page_url))

    events = []
    signal_count = 0
    unresolved = []
    for text, title, link in candidates:
        result = extract_forward_ir_events(
            text,
            title=title,
            link=link,
            company=company,
            today=today,
        )
        events.extend(result["events"])
        signal_count += result["signal_count"]
        unresolved.extend(result["unparsed_signals"])

    if not events:
        result = extract_forward_ir_events(
            _clean_text(soup.get_text(" ", strip=True)),
            title="",
            link=page_url,
            company=company,
            today=today,
        )
        events.extend(result["events"])
        if result["events"] or result["signal_count"]:
            signal_count = result["signal_count"]
            unresolved = result["unparsed_signals"]

    deduped = []
    seen = set()
    for event in events:
        key = (event["date"], event["title"], event["type"])
        if key not in seen:
            seen.add(key)
            deduped.append(event)
    return {
        "events": deduped,
        "signal_count": signal_count,
        "unparsed_signals": list(dict.fromkeys(unresolved)),
    }
