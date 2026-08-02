"""Render a calendar event into a bbs-go post payload.

Pure functions: (kind, event_dict) -> {title, content_md, category, tags}.
The data interface attaches this under each event's `post` key so an agent can
feed it straight to the portal-push skill (which posts Markdown + title +
category-name + tags to bbs-go) with no domain formatting of its own.

category is a NAME — portal-push resolves it by unique exact match in bbs-go,
so override POST_CATEGORIES to match real category names if the defaults don't.
"""
from __future__ import annotations

import json
import os

TITLE_MAX = 128  # bbs-go topic title ceiling (script-api.md §5.1)

DEFAULT_CATEGORIES = {
    "economic": "宏观经济",
    "earnings": "财报",
    "corporate": "公司事件",
    "ipo": "IPO",
}
DEFAULT_TAG = "投研日历"


def render_post(kind: str, e: dict) -> dict:
    """Return {title, content_md, category, tags} for one serialized event."""
    title, body = _RENDERERS[kind](e)
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1] + "…"
    return {
        "title": title,
        "content_md": body,
        "category": _category_for(kind),
        "tags": _tags(),
    }


# ──────────────────────────────────────────────────────────────────
# config (env-overridable, sane defaults)
# ──────────────────────────────────────────────────────────────────

def _category_for(kind: str) -> str:
    raw = os.environ.get("POST_CATEGORIES", "").strip()
    if raw:
        try:
            m = json.loads(raw)
            if isinstance(m, dict) and m.get(kind):
                return str(m[kind])
        except (json.JSONDecodeError, TypeError):
            pass
    return DEFAULT_CATEGORIES[kind]


def _tags() -> list[str]:
    raw = os.environ.get("POST_DEFAULT_TAG", "").strip()
    return [raw or DEFAULT_TAG]


# ──────────────────────────────────────────────────────────────────
# formatting helpers
# ──────────────────────────────────────────────────────────────────

def _d(v) -> str:
    """Trim an iso/date value to a readable 'YYYY-MM-DD[ HH:MM]' or '-'."""
    if not v:
        return "-"
    s = str(v).replace("T", " ")
    return s[:16] if len(s) >= 16 else s


def _stars(n) -> str:
    return "★" * int(n) if n else "-"


def _or(v, fallback="") -> str:
    return v if v not in (None, "") else fallback


def _kv(rows: list[tuple]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in rows)


# ──────────────────────────────────────────────────────────────────
# per-kind renderers: (event) -> (title, markdown_body)
# ──────────────────────────────────────────────────────────────────

def _r_macro(e: dict) -> tuple[str, str]:
    title = f"[{_or(e.get('country'))}] {_or(e.get('indicator'))} — {_d(e.get('event_time'))}"
    if e.get("actual"):
        title += f"  实际 {e['actual']}"
    body = "## 宏观事件\n\n" + _kv([
        ("指标", _or(e.get("indicator"))),
        ("国家", _or(e.get("country"))),
        ("时间", _d(e.get("event_time"))),
        ("重要性", _stars(e.get("importance"))),
        ("前值", _or(e.get("previous"), "-")),
        ("预测", _or(e.get("forecast"), "-")),
        ("实际", _or(e.get("actual"), "-")),
        ("来源", _or(e.get("source"))),
    ])
    return title, body


def _r_earnings(e: dict) -> tuple[str, str]:
    who = _or(e.get("company")) or _or(e.get("ticker"))
    title = f"[财报] {who} {_or(e.get('period'))} {_d(e.get('report_date'))}".strip()
    body = "## 财报发布\n\n" + _kv([
        ("公司", _or(e.get("company"))),
        ("代码", f"{_or(e.get('ticker'))} {_or(e.get('exchange'))}".strip()),
        ("报告期", _or(e.get("period"))),
        ("发布日", _d(e.get("report_date"))),
        ("来源", _or(e.get("source"))),
    ])
    return title, body


def _r_ipo(e: dict) -> tuple[str, str]:
    who = _or(e.get("company")) or _or(e.get("ticker"))
    title = f"[IPO] {who} {_d(e.get('event_date'))}".strip()
    lo, hi = e.get("price_low"), e.get("price_high")
    price = f"{lo} ~ {hi}" if (lo is not None or hi is not None) else "-"
    body = "## IPO\n\n" + _kv([
        ("公司", _or(e.get("company"))),
        ("代码", f"{_or(e.get('ticker'))} {_or(e.get('exchange'))}".strip()),
        ("日期", _d(e.get("event_date"))),
        ("价格区间", price),
        ("状态", _or(e.get("status"))),
        ("来源", _or(e.get("source"))),
    ])
    return title, body


_CORPORATE_TYPE_CN = {"unlock_summary": "解禁"}


def _type_cn(e: dict) -> str:
    """Map a corporate event_type machine name to a readable Chinese label."""
    return _CORPORATE_TYPE_CN.get(_or(e.get("event_type"))) or _or(e.get("event_type")) or "事件"


def _r_corporate(e: dict) -> tuple[str, str]:
    head = _or(e.get("title")) or (_or(e.get("description"))[:40]) or _or(e.get("company"))
    title = f"[{_type_cn(e)}] {head} {_d(e.get('event_date'))}".strip()
    rows = [
        ("公司", f"{_or(e.get('company'))} {_or(e.get('ticker'))}".strip()),
        ("日期", _d(e.get("event_date"))),
        ("类型", _type_cn(e)),
    ]
    if e.get("event_time"):
        rows.append(("时间", f"{_d(e.get('event_time'))} {_or(e.get('timezone'))}".strip()))
    body = "## 公司事件\n\n" + _kv(rows)
    if e.get("description"):
        body += f"\n\n> {e['description']}"
    if e.get("source_url"):
        body += f"\n\n[来源链接]({e['source_url']})"
    body += "\n\n" + _kv([("来源", _or(e.get("source")))])
    return title, body


_RENDERERS = {
    "economic": _r_macro,
    "earnings": _r_earnings,
    "ipo": _r_ipo,
    "corporate": _r_corporate,
}


if __name__ == "__main__":
    # ponytail self-check: every kind renders a complete post payload
    samples = {
        "economic": {"country": "CN", "indicator": "CPI", "event_time": "2026-08-09T09:30:00+08:00",
                     "importance": 3, "previous": "2.3%", "forecast": "2.4%", "actual": "2.5%",
                     "source": "akshare"},
        "earnings": {"company": "腾讯", "ticker": "00700", "exchange": "HKEX",
                     "period": "Q2", "report_date": "2026-08-15", "source": "akshare"},
        "ipo": {"company": "某公司", "ticker": "688001", "exchange": "科创板",
                "event_date": "2026-08-20", "price_low": 10.0, "price_high": 12.0,
                "status": "申购", "source": "akshare"},
        "corporate": {"event_type": "股东大会", "title": "2026年第二次临时股东大会",
                      "company": "腾讯", "ticker": "00700", "event_date": "2026-08-22",
                      "event_time": "2026-08-22T10:00:00+08:00", "timezone": "CST",
                      "description": "审议利润分配", "source_url": "https://example.com/ir",
                      "source": "ir"},
    }
    for kind, e in samples.items():
        p = render_post(kind, e)
        assert set(p) == {"title", "content_md", "category", "tags"}, p
        assert p["title"] and p["content_md"] and p["category"]
        assert isinstance(p["tags"], list) and p["tags"]
        assert len(p["title"]) <= TITLE_MAX
        print(f"{kind:9} → {p['title']}")
    print("ok — all kinds render a complete post payload")
