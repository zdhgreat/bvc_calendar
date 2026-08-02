"""Read-only data interface (JSON) for bbs-go / agents.

No HTML, no pages — pure event feed. A consumer (bbs-go, or an agent driving
the portal-push skill) pulls /api/feed and /api/event and renders/posts with
its own logic. financial-calendar never calls bbs-go.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.db import query_all
from app.render import render_post


router = APIRouter()

# Per-kind query shape shared by both endpoints. date_col: column used for
# date_from/date_to filtering. fields always carry id + source_id (stable
# identity for consumer dedup — bbs-go topic creation has no idempotency key)
# + fetched_at (incremental-sync key) + kind-specific fields.
_KIND_SPEC = {
    "economic": {
        "table": "economic_events",
        "date_col": "event_time",  # timestamptz
        "fields": ["id", "source_id", "fetched_at", "event_time", "country",
                   "indicator", "importance", "actual", "forecast", "previous", "source"],
    },
    "earnings": {
        "table": "earnings_calendar",
        "date_col": "report_date",  # date
        "fields": ["id", "source_id", "fetched_at", "report_date", "ticker",
                   "exchange", "company", "period", "source"],
    },
    "corporate": {
        "table": "corporate_events",
        "date_col": "event_date",  # date
        "fields": ["id", "source_id", "fetched_at", "event_date", "ticker",
                   "event_type", "title", "company", "description", "event_time",
                   "timezone", "source_url", "source"],
    },
    "ipo": {
        "table": "ipo_calendar",
        "date_col": "event_date",  # date
        "fields": ["id", "source_id", "fetched_at", "event_date", "ticker",
                   "company", "exchange", "price_low", "price_high", "status", "source"],
    },
}


def _require_feed_token(request: Request) -> None:
    """Optional shared-secret guard. Off by default; set FEED_TOKEN to require
    ?token= or X-Feed-Token — a trust boundary consumers cross over a network."""
    expected = os.environ.get("FEED_TOKEN", "").strip()
    if not expected:
        return
    got = request.query_params.get("token") or request.headers.get("x-feed-token", "")
    if got != expected:
        raise HTTPException(status_code=401, detail="invalid feed token")


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


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_dt(s: str) -> datetime:
    """Parse an ISO-8601 timestamp (handles trailing Z)."""
    return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))


def _feed_where(spec: dict, since: datetime | None,
                date_from: datetime | None, date_to: datetime | None) -> tuple[str, list]:
    clauses, params = [], []
    if since is not None:
        clauses.append("fetched_at >= %s")
        params.append(since)
    dc = spec["date_col"]
    if date_from is not None:
        clauses.append(f"{dc} >= %s")
        params.append(date_from)
    if date_to is not None:
        clauses.append(f"{dc} <= %s")
        params.append(date_to)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


@router.get("/api/feed")
def feed_api(request: Request, since: str | None = None, kind: str | None = None,
             date_from: str | None = None, date_to: str | None = None) -> dict:
    """Read-only event feed.

    Query params (all optional):
      since      ISO-8601 timestamp → only rows with fetched_at >= since.
                 Consumer stores its last poll time and passes it here for
                 incremental sync. fetched_at bumps on every re-UPSERT, so a
                 merely re-touched row reappears; dedup by (kind, source_id).
      kind       one of economic/earnings/corporate/ipo; omit for all four.
      date_from  YYYY-MM-DD → filter on the event date (inclusive).
      date_to    YYYY-MM-DD → filter on the event date (inclusive).

    Returns one array per kind; each event carries id + source_id + fetched_at
    plus its kind-specific fields, and a `post` object
    ({title, content_md, category, tags}) ready to feed the portal-push skill.
    """
    _require_feed_token(request)
    kinds = [kind] if kind in _KIND_SPEC else list(_KIND_SPEC)
    since_dt = _parse_dt(since) if since else None
    df = _parse_date(date_from) if date_from else None
    dt = _parse_date(date_to) if date_to else None

    out: dict[str, list] = {}
    for k in kinds:
        spec = _KIND_SPEC[k]
        where, params = _feed_where(spec, since_dt, df, dt)
        sql = (f"SELECT {', '.join(spec['fields'])} FROM {spec['table']} {where} "
               f"ORDER BY {spec['date_col']}")
        rows = []
        for r in query_all(sql, params):
            s = _serialize_row(r)
            s["post"] = render_post(k, s)
            rows.append(s)
        out[k] = rows
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "since": since, "date_from": date_from, "date_to": date_to, **out}


@router.get("/api/event/{kind}/{event_id}")
def event_json(kind: str, event_id: int, request: Request) -> dict:
    """Single event as JSON, with a `post` object ready for the portal-push skill."""
    _require_feed_token(request)
    spec = _KIND_SPEC.get(kind)
    if not spec:
        raise HTTPException(status_code=404, detail="unknown kind")
    rows = query_all(
        f"SELECT {', '.join(spec['fields'])} FROM {spec['table']} WHERE id = %s",
        (event_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="event not found")
    s = _serialize_row(rows[0])
    s["post"] = render_post(kind, s)
    return {"kind": kind, **s}
