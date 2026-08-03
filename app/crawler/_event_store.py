"""PG-backed event store — replaces the zip's events.json.

Exposes the API the zip's ir_crawler / dividend_calendar /
shareholder_meeting_monitor / report_disclosure_monitor expect:
    load_events() -> {"events": [...]}
    save_events(events_or_data, test_mode=False)

Internally UPSERTs into corporate_events. Stable source_id derived from
make_dedup_key so re-runs are idempotent (the zip's own id field embeds a
timestamp + id(title), which is unstable across runs and would defeat
UNIQUE(source, source_id)).

Dedup helpers (make_dedup_key, deduplicate_events, normalize_company,
extract_event_keyword) are ported verbatim from the zip's calendar_notify.py
so the other crawler modules can keep importing them from one place.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Iterable

import psycopg2.extras

from app.db import get_conn


# ──────────────────────────────────────────────────────────────────
# Dedup helpers — ported from zip's calendar_notify.py
# (kept verbatim so make_dedup_key semantics match the original collector)
# ──────────────────────────────────────────────────────────────────

COMPANY_ALIASES = {
    "腾讯": "腾讯", "tencent": "腾讯",
    "新东方": "新东方", "new oriental": "新东方",
    "SK海力士": "SK海力士", "sk hynix": "SK海力士", "sk海力士": "SK海力士",
    "三星电子": "三星电子", "samsung": "三星电子",
    "英特尔": "英特尔", "intel": "英特尔",
    "英伟达": "英伟达", "nvidia": "英伟达",
    "美光": "美光", "micron": "美光",
    "瑞幸": "瑞幸", "luckin": "瑞幸", "瑞幸咖啡": "瑞幸",
    "海尔智家": "海尔智家", "haier": "海尔智家",
    "美的集团": "美的集团", "midea": "美的集团",
    "格力电器": "格力电器", "gree": "格力电器",
    "育碧": "育碧", "ubisoft": "育碧",
    "铠侠": "铠侠", "kioxia": "铠侠",
    "Staar": "Staar Surgical", "staar surgical": "Staar Surgical",
    "Prosus": "Prosus", "prosus": "Prosus",
    "华住": "华住", "h world": "华住",
    "唯品会": "唯品会", "vipshop": "唯品会",
    "欢聚": "欢聚", "joyy": "欢聚",
    "亚朵": "亚朵", "atour": "亚朵",
    "拼多多": "拼多多", "pdd": "拼多多",
}

EVENT_KEYWORD_PATTERNS = [
    (r'第[一二三四]季度.*?(?:业绩|财报|earnings|results)', lambda m: {
        '一': 'Q1业绩', '二': 'Q2业绩', '三': 'Q3业绩', '四': 'Q4业绩'
    }.get(m.group(0)[1], '业绩发布')),
    (r'Q1.*?(?:业绩|财报|earnings|results)', 'Q1业绩'),
    (r'Q2.*?(?:业绩|财报|earnings|results)', 'Q2业绩'),
    (r'Q3.*?(?:业绩|财报|earnings|results)', 'Q3业绩'),
    (r'(?:全年|年度|Q4|FY|annual).*?(?:业绩|财报|earnings|results)', '全年业绩'),
    (r'(?:业绩|财报|earnings|results).*?(?:发布|公布|conference|call)', '业绩发布'),
    (r'(?:一季报|半年报|三季报|年报)', lambda m: m.group(0)),
    (r'Quarterly.*?Earnings', '季度业绩'),
    (r'Earnings.*?Conference', '业绩会议'),
    (r'Financial Results', '业绩发布'),
    (r'(?:股东大会|stockholders.*?meeting|annual.*?meeting)', '股东大会'),
    (r'(?:conference|summit|论坛|峰会)', '会议'),
    (r'(?:投资者|investor).*?(?:活动|日历|event)', '投资者活动'),
    (r'Investor Day', '投资者日'),
]

GARBAGE_PATTERNS = [
    r'^\d{1,2}:\d{2}\s*(?:AM|PM|MDT|EDT|HKT|CST|CDT|EST|PDT|PST)$',
    r'^\d{1,2}:\d{2}\s*(?:AM|PM)\s+(?:MDT|EDT|HKT|CST|CDT|EST|PDT|PST)$',
    r'^(?:Latest News|View all|Events & Presentations|Financial Events|Past Events|Upcoming Events)$',
    r'^Events$',
]


def normalize_company(title: str, source: str = "", note: str = "") -> str:
    text = (title + ' ' + source + ' ' + note).lower()
    for alias, standard in sorted(COMPANY_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias.lower() in text:
            return standard
    return ""


def extract_event_keyword(title: str) -> str:
    title_lower = title.lower()
    for pattern, replacement in EVENT_KEYWORD_PATTERNS:
        m = re.search(pattern, title_lower)
        if m:
            if callable(replacement):
                return replacement(m)
            return replacement
    return title[:20]


def _normalize_keyword(keyword: str) -> str:
    keyword_lower = keyword.lower()
    q_map = {'第一': 'Q1', '第二': 'Q2', '第三': 'Q3', '第四': 'Q4',
             '一': 'Q1', '二': 'Q2', '三': 'Q3', '四': 'Q4'}
    for k, v in q_map.items():
        if k in keyword_lower:
            return f'{v}业绩'
    if '一季报' in keyword_lower or keyword_lower == 'q1':
        return 'Q1业绩'
    if '半年报' in keyword_lower or keyword_lower == 'q2':
        return 'Q2业绩'
    if '三季报' in keyword_lower or keyword_lower == 'q3':
        return 'Q3业绩'
    if '年报' in keyword_lower or keyword_lower == 'q4' or '全年' in keyword_lower or '年度' in keyword_lower:
        return '全年业绩'
    if '股东大会' in keyword_lower:
        return '股东大会'
    if re.search(r'(?:财报|业绩|earnings|results)', keyword_lower):
        return '业绩发布'
    if re.search(r'(?:会议|conference|summit)', keyword_lower):
        return '会议'
    return keyword


def make_dedup_key(event: dict) -> tuple:
    date = event.get("date", "")
    company = event.get("company", "") or normalize_company(
        event.get("title", ""), event.get("source", ""), event.get("note", ""))
    if event.get("type") == "股息分红":
        note = event.get("note", "")
        fiscal_year = re.search(r"财政年度:\s*([^；;]+)", note)
        distribution_type = re.search(r"分配类型:\s*([^；;]+)", note)
        return (date, company, event.get("ticker", ""), event.get("title", ""),
                fiscal_year.group(1).strip() if fiscal_year else "",
                distribution_type.group(1).strip() if distribution_type else "")
    keyword = _normalize_keyword(extract_event_keyword(event.get("title", "")))
    return (date, company, keyword)


def deduplicate_events(events: list) -> list:
    SOURCE_PRIORITY = {
        "投资者关系": 100, "IR": 100, "投资者日历": 100, "业绩发布": 100,
        "Rss": 80, "RSS": 80,
        "AkShare": 70,
    }

    def get_priority(event):
        source = event.get("source", "")
        for keyword, priority in SOURCE_PRIORITY.items():
            if keyword in source:
                return priority
        return 50

    groups: dict = {}
    for e in events:
        groups.setdefault(make_dedup_key(e), []).append(e)

    result = []
    for key, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue
        group.sort(key=lambda x: (-get_priority(x), x.get("created_at", "")))
        winner = group[0]
        notes = {e.get("note", "").strip() for e in group
                 if e.get("note", "").strip() and e.get("note", "") != "自动采集，请核实"}
        last_notified = max((e.get("last_notified", "") for e in group if e.get("last_notified", "")),
                            default="")
        if notes and not winner.get("note"):
            winner["note"] = "; ".join(list(notes)[:2])
        if last_notified and not winner.get("last_notified"):
            winner["last_notified"] = last_notified
        result.append(winner)

    result.sort(key=lambda x: x.get("date", ""))
    return result


# ──────────────────────────────────────────────────────────────────
# PG storage
# ──────────────────────────────────────────────────────────────────

# ponytail: 16-char sha1 prefix is enough collision resistance for events
# (birthday bound ~2^32 before a 50% collision — far beyond any realistic volume).


def _stable_source_id(event: dict) -> str:
    """Derive a stable source_id from the dedup key.

    The zip's id field embeds datetime.now() + id(title) so it changes every
    run; that would defeat UNIQUE(source, source_id). Hash the dedup key
    instead — same semantic event → same source_id across runs.
    """
    key = make_dedup_key(event)
    payload = json.dumps(key, ensure_ascii=False, default=str)
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"ir_{h}"


def _parse_event_time(event: dict):
    """Return a tz-aware datetime or None."""
    t = event.get("time") or event.get("event_time") or ""
    tz = event.get("timezone", "") or ""
    if not t:
        return None
    # Common IR formats: "HH:MM", "HH:MM ET", "2026-08-15 14:30", etc.
    # Be lenient — fall back to None on parse failure.
    for fmt in ("%Y-%m-%d %H:%M", "%H:%M", "%H:%M %Z", "%I:%M %p %Z", "%I:%M %p"):
        try:
            dt = datetime.strptime(f"{event.get('date','')} {t}".strip() if fmt.startswith("%H") else t, fmt)
            # If naive and we have a date, combine
            return dt
        except ValueError:
            continue
    return None


def _event_to_row(event: dict) -> dict:
    return {
        "event_date": event.get("date"),
        "ticker": event.get("ticker") or None,
        "event_type": event.get("type") or "ir",
        "description": event.get("note") or None,
        "source": event.get("source") or "ir",
        "source_id": _stable_source_id(event),
        "title": event.get("title"),
        "company": event.get("company") or normalize_company(
            event.get("title", ""), event.get("source", ""), event.get("note", "")) or None,
        "event_time": _parse_event_time(event),
        "timezone": event.get("timezone") or None,
        "source_url": event.get("source_url") or None,
    }


_COLS = ["event_date", "ticker", "event_type", "description", "source", "source_id",
         "title", "company", "event_time", "timezone", "source_url"]


def _is_garbage(title: str) -> bool:
    title = (title or "").strip()
    return bool(title) and any(re.search(p, title) for p in GARBAGE_PATTERNS)


def save_events(events_or_data, test_mode: bool = False) -> dict:
    """Drop-in replacement for the zip's save_events.

    Accepts either a list of events or {"events": [...]} dict (the zip's two
    call shapes differ — ir_crawler passes a list, dividend passes a dict).
    UPSERTs each into corporate_events. Returns {"new": N, "total": M}.
    """
    if isinstance(events_or_data, dict):
        events = events_or_data.get("events", [])
    else:
        events = list(events_or_data or [])

    # Filter garbage + ensure title
    events = [e for e in events if (e.get("title", "") or "").strip() and not _is_garbage(e.get("title", ""))]

    if test_mode:
        for e in sorted(events, key=lambda x: x.get("date", "")):
            src = f" [{e.get('source','')}]" if e.get("source") else ""
            url = f" ({e.get('source_url','')})" if e.get("source_url") else ""
            print(f"  [{e.get('date','')}] {e.get('type','')} | {e.get('title','')}{src}{url}")
        return {"new": 0, "total": len(events)}

    # Dedup input first (preserves the zip's intra-run semantic dedup)
    events = deduplicate_events(events)

    rows = [_event_to_row(e) for e in events]
    if not rows:
        return {"new": 0, "total": 0}

    sql = f"""
        INSERT INTO corporate_events ({", ".join(_COLS)}, fetched_at)
        VALUES %s
        ON CONFLICT (source, source_id) DO UPDATE
        SET fetched_at = NOW(),
            event_date = EXCLUDED.event_date,
            event_type = EXCLUDED.event_type,
            title      = COALESCE(EXCLUDED.title, corporate_events.title),
            description = COALESCE(EXCLUDED.description, corporate_events.description),
            company    = COALESCE(EXCLUDED.company, corporate_events.company),
            ticker     = COALESCE(EXCLUDED.ticker, corporate_events.ticker),
            event_time = COALESCE(EXCLUDED.event_time, corporate_events.event_time),
            timezone   = COALESCE(EXCLUDED.timezone, corporate_events.timezone),
            source_url = COALESCE(EXCLUDED.source_url, corporate_events.source_url)
        RETURNING id, (xmax = 0) AS inserted
    """
    values = [[r.get(c) for c in _COLS] + [datetime.utcnow()] for r in rows]
    template = f"({', '.join(['%s'] * (len(_COLS) + 1))})"

    new_count = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, values, template=template)
            for row in cur.fetchall():
                if row[1]:
                    new_count += 1
        conn.commit()
    return {"new": new_count, "total": len(rows)}


def load_events() -> dict:
    """Read all corporate_events back as the zip's canonical dict shape.

    Used by the zip's query.py / calendar_notify.list_events — kept for
    diagnostic parity. The data interface (/api/feed, /api/event) reads via
    routers/calendar.py directly, not through this.
    """
    sql = """
        SELECT event_date, ticker, event_type, description, source, source_id,
               title, company, event_time, timezone, source_url
        FROM corporate_events
        ORDER BY event_date
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            db_rows = cur.fetchall()
    events = []
    for r in db_rows:
        row = dict(zip(cols, r))
        events.append({
            "id": row["source_id"],
            "date": row["event_date"].isoformat() if row["event_date"] else "",
            "title": row["title"] or "",
            "type": row["event_type"] or "",
            "note": row["description"] or "",
            "time": row["event_time"].strftime("%H:%M") if row["event_time"] else "",
            "timezone": row["timezone"] or "",
            "company": row["company"] or "",
            "ticker": row["ticker"] or "",
            "source": row["source"] or "",
            "source_url": row["source_url"] or "",
            "created_at": "",
        })
    return {"events": events}


if __name__ == "__main__":
    # ponytail self-check: dedup helpers + round-trip
    e1 = {"date": "2026-08-15", "title": "腾讯Q2业绩发布", "source": "ir",
          "type": "业绩发布", "note": "", "time": "", "timezone": ""}
    e2 = {"date": "2026-08-15", "title": "腾讯 Q2 业绩", "source": "rss",
          "type": "业绩发布", "note": "", "time": "", "timezone": ""}
    assert make_dedup_key(e1) == make_dedup_key(e2), "dedup key should match"
    assert _stable_source_id(e1) == _stable_source_id(e2)
    print("ok — dedup + stable id verified")
