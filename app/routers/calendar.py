"""Calendar page + JSON API."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import query_all


def _bbsgo_public_url() -> str:
    """Public bbs-go URL for building topic links. Empty if not deployed."""
    return (os.environ.get("BBSGO_PUBLIC_URL")
            or os.environ.get("BBSGO_BASE_URL", "")).strip().rstrip("/")

router = APIRouter()


@router.get("/api/calendar")
def calendar_api(date: str | None = None, view: str = "day") -> dict:
    """Return events for a day / week / month. Defaults to today (Asia/Shanghai).

    view=day   → single date, flat response (backward compatible)
    view=week  → Monday..Sunday of the week containing `date`
    view=month → calendar month (1st to next 1st) containing `date`
    """
    target = _parse_date(date)
    view = (view or "day").lower()
    if view not in ("day", "week", "month"):
        view = "day"

    start, end = _range_for(target, view)

    economic = query_all(
        "SELECT e.event_time, e.country, e.indicator, e.importance, e.actual, e.forecast, e.previous, e.source, "
        "ctm.topic_id AS topic_id "
        "FROM economic_events e "
        "LEFT JOIN calendar_topic_map ctm ON ctm.kind='economic' AND ctm.source_id=e.source_id "
        "WHERE e.event_time >= %s AND e.event_time < %s "
        "ORDER BY e.event_time",
        (start, end),
    )
    earnings = query_all(
        "SELECT e.report_date, e.ticker, e.exchange, e.company, e.period, e.source, "
        "ctm.topic_id AS topic_id "
        "FROM earnings_calendar e "
        "LEFT JOIN calendar_topic_map ctm ON ctm.kind='earnings' AND ctm.source_id=e.source_id "
        "WHERE e.report_date >= %s AND e.report_date < %s ORDER BY e.report_date, e.ticker",
        (start.isoformat(), end.isoformat()),
    )
    corporate = query_all(
        "SELECT e.event_date, e.ticker, e.event_type, e.description, e.source, "
        "ctm.topic_id AS topic_id "
        "FROM corporate_events e "
        "LEFT JOIN calendar_topic_map ctm ON ctm.kind='corporate' AND ctm.source_id=e.source_id "
        "WHERE e.event_date >= %s AND e.event_date < %s ORDER BY e.event_date, e.event_type, e.ticker",
        (start.isoformat(), end.isoformat()),
    )
    ipo = query_all(
        "SELECT e.event_date, e.ticker, e.company, e.exchange, e.price_low, e.price_high, e.status, e.source, "
        "ctm.topic_id AS topic_id "
        "FROM ipo_calendar e "
        "LEFT JOIN calendar_topic_map ctm ON ctm.kind='ipo' AND ctm.source_id=e.source_id "
        "WHERE e.event_date >= %s AND e.event_date < %s ORDER BY e.event_date, e.company",
        (start.isoformat(), end.isoformat()),
    )

    bbsgo_url = _bbsgo_public_url()

    if view == "day":
        return {
            "view": "day",
            "date": target.isoformat(),
            "bbsgo_url": bbsgo_url,
            "economic": [_serialize_row(r) for r in economic],
            "earnings": [_serialize_row(r) for r in earnings],
            "corporate": [_serialize_row(r) for r in corporate],
            "ipo": [_serialize_row(r) for r in ipo],
        }

    # week / month → group by date
    days: dict[str, dict] = {}
    for r in economic:
        d = _date_key(r.get("event_time"))
        days.setdefault(d, _empty_day())
        days[d]["economic"].append(_serialize_row(r))
    for r in earnings:
        d = _date_key(r.get("report_date"))
        days.setdefault(d, _empty_day())
        days[d]["earnings"].append(_serialize_row(r))
    for r in corporate:
        d = _date_key(r.get("event_date"))
        days.setdefault(d, _empty_day())
        days[d]["corporate"].append(_serialize_row(r))
    for r in ipo:
        d = _date_key(r.get("event_date"))
        days.setdefault(d, _empty_day())
        days[d]["ipo"].append(_serialize_row(r))

    # fill empty days so the timeline shows gaps
    cur = start
    while cur < end:
        days.setdefault(cur.date().isoformat(), _empty_day())
        cur += timedelta(days=1)

    return {
        "view": view,
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "bbsgo_url": bbsgo_url,
        "days": {k: days[k] for k in sorted(days)},
    }


def _range_for(target: datetime, view: str) -> tuple[datetime, datetime]:
    if view == "week":
        # Monday=0 .. Sunday=6
        start = target - timedelta(days=target.weekday())
    elif view == "month":
        start = target.replace(day=1)
    else:
        start = target
    end = start + timedelta(days=1) if view == "day" else (
        start + timedelta(days=7) if view == "week" else
        (start.replace(month=start.month + 1) if start.month < 12 else start.replace(year=start.year + 1, month=1))
    )
    return start, end


def _date_key(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v)[:10]


def _empty_day() -> dict:
    return {"economic": [], "earnings": [], "corporate": [], "ipo": []}


def _parse_date(date: str | None) -> datetime:
    if not date:
        # ponytail: hardcode Asia/Shanghai — only place this app targets.
        # If reused in another tz, replace with timezone.from system.
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime.fromisoformat(date)


def _serialize_row(row: dict) -> dict:
    """Make PG types JSON-friendly."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ──────────────────────────────────────────────────────────────────
# /event/{kind}/{id} — single-event page embedded in bbs-go topic iframes
# ponytail: standalone minimal HTML (no Tailwind CDN) so iframe loads fast
# ──────────────────────────────────────────────────────────────────

_EVENT_TABLE = {
    "economic": "economic_events",
    "earnings": "earnings_calendar",
    "corporate": "corporate_events",
    "ipo": "ipo_calendar",
}


@router.get("/event/{kind}/{event_id}", response_class=HTMLResponse)
def event_detail(kind: str, event_id: int) -> HTMLResponse:
    if kind not in _EVENT_TABLE:
        return HTMLResponse("unknown kind", status_code=404)
    rows = query_all(f"SELECT * FROM {_EVENT_TABLE[kind]} WHERE id = %s", (event_id,))
    if not rows:
        return HTMLResponse("event not found", status_code=404)
    r = _serialize_row(rows[0])
    title, rows_html = _event_detail_blocks(kind, r)
    return HTMLResponse(_EVENT_PAGE_TEMPLATE.format(
        title=title.replace("<", "&lt;"),
        rows="\n".join(rows_html),
    ))


def _event_detail_blocks(kind: str, r: dict) -> tuple[str, list[str]]:
    """Return (title, html rows) for one event. Per-kind field shape."""
    if kind == "economic":
        title = f"[{r.get('country','')}] {r.get('indicator','')}"
        fields = [("时间", _fmt_time(r.get("event_time"))),
                  ("国家", r.get("country")),
                  ("重要性", "★" * (r.get("importance") or 0)),
                  ("前值", r.get("previous")), ("预测", r.get("forecast")),
                  ("实际", r.get("actual")), ("来源", r.get("source"))]
    elif kind == "earnings":
        title = f"[财报] {r.get('company') or r.get('ticker','')}"
        fields = [("代码", f"{r.get('ticker','')} {r.get('exchange') or ''}"),
                  ("公司", r.get("company")), ("报告期", r.get("period")),
                  ("发布日", r.get("report_date")), ("来源", r.get("source"))]
    elif kind == "ipo":
        title = f"[IPO] {r.get('company') or r.get('ticker','')}"
        fields = [("代码", f"{r.get('ticker','')} {r.get('exchange') or ''}"),
                  ("公司", r.get("company")), ("日期", r.get("event_date")),
                  ("价格区间", _fmt_range(r.get("price_low"), r.get("price_high"))),
                  ("状态", r.get("status")), ("来源", r.get("source"))]
    else:  # corporate
        title = f"[{r.get('event_type','事件')}] {r.get('title') or (r.get('description','')[:40]) or r.get('company','')}"
        fields = [("公司", f"{r.get('company','')} {r.get('ticker') or ''}"),
                  ("日期", r.get("event_date")), ("类型", r.get("event_type")),
                  ("时间", _fmt_time(r.get("event_time"))),
                  ("说明", r.get("description"))]
        if r.get("source_url"):
            fields.append(("链接", f'<a href="{r["source_url"]}" target="_blank">来源</a>'))
        fields.append(("来源", r.get("source")))
    rows_html = [f'<tr><th>{k}</th><td>{v if v not in (None,"") else "-"}</td></tr>'
                 for k, v in fields]
    return title, rows_html


def _fmt_time(v) -> str:
    if v is None:
        return "-"
    try:
        return str(v)[:19].replace("T", " ")
    except Exception:
        return str(v)


def _fmt_range(lo, hi) -> str:
    if lo is None and hi is None:
        return "-"
    return f"{lo if lo is not None else '?'} ~ {hi if hi is not None else '?'}"


_EVENT_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 16px; background: #fff; color: #1f2937; }}
  h1 {{ font-size: 1.05rem; margin: 0 0 12px; line-height: 1.4; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.875rem; }}
  th {{ text-align: left; width: 5em; color: #6b7280; font-weight: 500;
       padding: 6px 8px 6px 0; vertical-align: top; }}
  td {{ padding: 6px 0; }}
  a {{ color: #2563eb; }}
</style></head><body>
<h1>{title}</h1>
<table>{rows}</table>
</body></html>"""
