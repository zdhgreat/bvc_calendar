"""bbs-go integration: ensure each calendar event has a corresponding topic.

Calls bbs-go's POST /api/topic/create (see ../bbs-go/docs/script-api.md).
Auth via X-User-Token header. Env vars:
  BBSGO_BASE_URL     — e.g. http://localhost:8080
  BBSGO_API_TOKEN    — user token obtained via /api/login/signin
  BBSGO_CATEGORY_ID  — default category for created topics (integer)
  BBSGO_CATEGORY_MAP — optional JSON {kind: categoryId} to override per event kind

If any required env is missing, records a stub topic_id (negative number-as-text)
so re-runs don't retry; run a one-shot backfill after bbs-go is configured.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

from app.db import get_conn


CREATE_TIMEOUT = (5, 30)  # (connect, read) seconds — per script-api.md §10.6


def ensure_topic(*, kind: str, source_id: str, table: str, event_id: int) -> str | None:
    """Ensure a bbs-go topic exists for this event. Idempotent.

    Returns the topic_id (string — bbs-go uses opaque IDs) existing or newly
    created; None only if the event row couldn't be loaded.
    """
    existing = _lookup_map(kind, source_id)
    if existing is not None:
        return existing

    base = os.environ.get("BBSGO_BASE_URL", "").strip()
    token = os.environ.get("BBSGO_API_TOKEN", "").strip()
    if not base or not token:
        return _record_stub(kind, source_id, event_id)

    event = _load_event(table, event_id)
    if not event:
        return None

    category_id = _resolve_category_id(kind)
    if not category_id:
        return _record_stub(kind, source_id, event_id, reason="no_category")

    title, content = _build_topic_payload(event, table, kind, event_id)

    try:
        topic_id = _create_topic_via_api(base, token, category_id, title, content)
    except Exception as e:
        # ponytail: don't crash the runner for bbs-go outages; stub it so we
        # don't retry every run. Backfill after bbs-go is healthy.
        import sys
        print(f"[bbs_integration] create failed ({kind}/{source_id}): {e}",
              file=sys.stderr)
        return _record_stub(kind, source_id, event_id, reason="api_error")

    _record_map(kind, source_id, topic_id)
    return topic_id


# ──────────────────────────────────────────────────────────────────
# PG event loading — generic across the 4 event tables
# ──────────────────────────────────────────────────────────────────

_TABLE_TITLE_COL = {
    "economic_events": "indicator",
    "earnings_calendar": "company",
    "corporate_events": "title",
    "ipo_calendar": "company",
}


def _load_event(table: str, event_id: int) -> dict | None:
    """Load one event row by id. Returns dict keyed by column name."""
    # Avoid SQL injection on table name — whitelist
    if table not in _TABLE_TITLE_COL:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (event_id,))
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def _build_topic_payload(event: dict, table: str, kind: str,
                         event_id: int | None = None) -> tuple[str, str]:
    """Construct (title, markdown content) for the topic post.

    Each table has a different shape; we build a uniform title + a markdown body
    that summarizes the event with its provenance link. If
    FINANCIAL_CALENDAR_PUBLIC_URL is set and event_id is known, the body embeds
    an <iframe> pointing at /event/{kind}/{id} so bbs-go readers see the live
    detail page (markdown backup below — survives even if the iframe is stripped
    by the sanitizer).
    """
    if table == "economic_events":
        title = f"[{event.get('country','')}] {event.get('indicator','')} — {event.get('event_time','')}"
        if event.get("actual"):
            title = f"{title}  实际 {event['actual']}"
        body = _fmt_macro(event)
    elif table == "earnings_calendar":
        title = f"[财报] {event.get('company','') or event.get('ticker','')} {event.get('period','') or ''} {event.get('report_date','')}"
        body = _fmt_earnings(event)
    elif table == "ipo_calendar":
        title = f"[IPO] {event.get('company','') or event.get('ticker','')} {event.get('event_date','')}"
        body = _fmt_ipo(event)
    else:  # corporate_events (incl. IR/dividend/meeting/disclosure)
        title = event.get("title") or event.get("description") or event.get("event_type", "事件")
        title = f"[{event.get('event_type','事件')}] {title} {event.get('event_date','')}"
        body = _fmt_corporate(event)
    # ponytail: title max 128 Unicode chars per script-api.md §5.1
    if len(title) > 128:
        title = title[:125] + "..."

    iframe = _event_iframe(kind, event_id)
    if iframe:
        # iframe first (live view), then markdown backup so the post still has
        # content if bbs-go's markdown sanitizer strips raw HTML.
        body = f"{iframe}\n\n{body}"
    return title, body


def _event_iframe(kind: str, event_id: int | None) -> str:
    """Build an <iframe> pointing at /event/{kind}/{id} when public URL is set."""
    base = os.environ.get("FINANCIAL_CALENDAR_PUBLIC_URL", "").strip().rstrip("/")
    if not base or event_id is None:
        return ""
    src = f"{base}/event/{kind}/{event_id}"
    # ponytail: fixed height; per-event sizing would need a /event-meta endpoint
    return (f'<iframe src="{src}" width="100%" height="360" frameborder="0" '
            f'style="border:1px solid #e5e7eb;border-radius:6px;" '
            f'loading="lazy"></iframe>')


def _fmt_macro(e: dict) -> str:
    rows = [f"- 指标: {e.get('indicator','')}",
            f"- 国家: {e.get('country','')}",
            f"- 时间: {e.get('event_time','')}",
            f"- 重要性: {e.get('importance','')}",
            f"- 前值: {e.get('previous','')}",
            f"- 预测: {e.get('forecast','')}",
            f"- 实际: {e.get('actual','')}",
            f"- 来源: {e.get('source','')}"]
    return "## 宏观事件\n\n" + "\n".join(rows)


def _fmt_earnings(e: dict) -> str:
    rows = [f"- 公司: {e.get('company','') or ''}",
            f"- 代码: {e.get('ticker','')} {e.get('exchange','') or ''}",
            f"- 报告期: {e.get('period','')}",
            f"- 发布日: {e.get('report_date','')}",
            f"- 来源: {e.get('source','')}"]
    return "## 财报发布\n\n" + "\n".join(rows)


def _fmt_ipo(e: dict) -> str:
    rows = [f"- 公司: {e.get('company','') or ''}",
            f"- 代码: {e.get('ticker','')} {e.get('exchange','') or ''}",
            f"- 日期: {e.get('event_date','')}",
            f"- 价格区间: {e.get('price_low','')} ~ {e.get('price_high','')}",
            f"- 状态: {e.get('status','')}",
            f"- 来源: {e.get('source','')}"]
    return "## IPO\n\n" + "\n".join(rows)


def _fmt_corporate(e: dict) -> str:
    rows = [f"- 公司: {e.get('company','') or ''}",
            f"- 代码: {e.get('ticker','') or ''}",
            f"- 日期: {e.get('event_date','')}",
            f"- 类型: {e.get('event_type','')}"]
    if e.get("event_time"):
        rows.append(f"- 时间: {e.get('event_time','')} {e.get('timezone','') or ''}")
    if e.get("description"):
        rows.append("")
        rows.append(f"> {e['description']}")
    if e.get("source_url"):
        rows.append("")
        rows.append(f"[来源链接]({e['source_url']})")
    rows.append(f"- 来源: {e.get('source','')}")
    return "## 公司事件\n\n" + "\n".join(rows)


# ──────────────────────────────────────────────────────────────────
# bbs-go HTTP client
# ──────────────────────────────────────────────────────────────────


def _resolve_category_id(kind: str) -> int | None:
    """Resolve target category. Per-kind map overrides default."""
    raw_map = os.environ.get("BBSGO_CATEGORY_MAP", "").strip()
    if raw_map:
        try:
            kind_map = json.loads(raw_map)
            if str(kind_map.get(kind)):
                return int(kind_map[kind])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    default = os.environ.get("BBSGO_CATEGORY_ID", "").strip()
    return int(default) if default else None


def _create_topic_via_api(base_url: str, token: str, category_id: int,
                          title: str, content: str) -> str:
    """POST /api/topic/create. Returns the new topic's id (string)."""
    url = f"{base_url.rstrip('/')}/api/topic/create"
    payload = {
        "type": 0,  # normal topic
        "categoryId": category_id,
        "title": title,
        "contentType": "markdown",
        "content": content,
        "tags": ["投研日历"],
    }
    resp = requests.post(url, json=payload,
                         headers={"X-User-Token": token},
                         timeout=CREATE_TIMEOUT)
    resp.raise_for_status()
    envelope = resp.json()
    if envelope.get("success") is not True:
        raise RuntimeError(f"bbs-go error {envelope.get('errorCode')}: "
                           f"{envelope.get('message')}")
    data = envelope.get("data") or {}
    topic_id = data.get("id")
    if not topic_id:
        raise RuntimeError(f"bbs-go response missing topic id: {envelope}")
    return str(topic_id)


# ──────────────────────────────────────────────────────────────────
# calendar_topic_map persistence
# ──────────────────────────────────────────────────────────────────


def _lookup_map(kind: str, source_id: str) -> str | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT topic_id FROM calendar_topic_map WHERE kind=%s AND source_id=%s",
                (kind, source_id),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _record_map(kind: str, source_id: str, topic_id: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO calendar_topic_map (kind, source_id, topic_id, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (kind, source_id) DO UPDATE SET topic_id = EXCLUDED.topic_id
                """,
                (kind, source_id, str(topic_id), datetime.now(timezone.utc)),
            )
        conn.commit()


def _record_stub(kind: str, source_id: str, event_id: int,
                 reason: str = "no_config") -> str:
    """Record a negative-id stub when bbs-go is unavailable.

    Negative-prefixed strings make stubs visually distinct from real bbs-go IDs
    (which are opaque non-numeric strings). Backfill by deleting rows WHERE
    topic_id LIKE '-%' after bbs-go is configured.
    """
    stub = f"-{event_id}"  # event_id stays numeric; backfill matches '-%'
    if reason:
        # Don't lose the reason — encode in a comment-like form isn't useful;
        # the stub value itself signals "needs backfill".
        pass
    _record_map(kind, source_id, stub)
    return stub


__all__ = ["ensure_topic"]
