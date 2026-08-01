"""EN calendar source via Finnhub REST API.

Endpoints:
  GET /calendar/economic  -> economic_events
  GET /calendar/earnings  -> earnings_calendar
  GET /calendar/ipo       -> ipo_calendar

API key from env: FINNHUB_API_KEY. Free tier: 60 req/min, plenty for daily.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

API_BASE = "https://finnhub.io/api/v1"
_TIMEOUT = 15


def _key() -> str:
    k = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not k or k == "TODO":
        raise RuntimeError("FINNHUB_API_KEY missing — set it in .env (register at finnhub.io)")
    return k


def _get(path: str, params: dict[str, Any]) -> list[dict]:
    params = {**params, "token": _key()}
    url = f"{API_BASE}{path}"
    try:
        r = requests.get(url, params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # ponytail: Finnhub wraps economicCalendar in {"economicCalendar": [...]}, others return lists directly
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
            return []
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[finnhub.{path}] failed: {e}", file=sys.stderr)
        return []


# ---------------- economic ----------------

def fetch_economic(start: datetime, end: datetime) -> list[dict]:
    items = _get("/calendar/economic", {
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
    })
    out: list[dict] = []
    for it in items:
        country = (it.get("country") or "WW")[:32]
        indicator = (it.get("event") or it.get("indicator") or "(unknown)")[:128]
        ts = it.get("time") or it.get("releaseTime")
        event_time = _parse_ts(ts, start)
        actual = it.get("actual")
        est = it.get("estimate") or it.get("forecast")
        prev = it.get("prev") or it.get("previous")
        impact = it.get("impact")  # low/medium/high
        importance = {"low": 1, "medium": 2, "high": 3}.get((impact or "").lower())
        out.append({
            "event_time": event_time,
            "country": country,
            "indicator": indicator,
            "importance": importance,
            "actual": _num_to_str(actual),
            "forecast": _num_to_str(est),
            "previous": _num_to_str(prev),
            "source": "finnhub",
            "source_id": f"fh|{country}|{indicator[:60]}|{ts}",
        })
    return out


# ---------------- earnings ----------------

def fetch_earnings(start: datetime, end: datetime) -> list[dict]:
    items = _get("/calendar/earnings", {
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
    })
    out: list[dict] = []
    for it in items:
        date_iso = it.get("date")
        if not date_iso:
            continue
        ticker = (it.get("symbol") or "")[:32]
        exchange = None
        out.append({
            "report_date": date_iso,
            "ticker": ticker,
            "exchange": exchange,
            "company": (it.get("name") or "")[:128] or None,
            "period": _quarter_label(date_iso),
            "source": "finnhub",
            "source_id": f"fh_earn|{date_iso}|{ticker}",
        })
    return out


# ---------------- IPO ----------------

def fetch_ipo(start: datetime, end: datetime) -> list[dict]:
    items = _get("/calendar/ipo", {
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
    })
    out: list[dict] = []
    for it in items:
        date_iso = it.get("date") or it.get("startDate")
        if not date_iso:
            continue
        price_low = it.get("priceRangeLow") or it.get("low")
        price_high = it.get("priceRangeHigh") or it.get("high")
        out.append({
            "event_date": date_iso[:10],
            "ticker": (it.get("symbol") or "")[:32] or None,
            "company": (it.get("name") or "")[:128] or None,
            "exchange": (it.get("exchange") or "")[:16] or None,
            "price_low": _to_num(price_low),
            "price_high": _to_num(price_high),
            "status": (it.get("status") or "upcoming")[:16],
            "source": "finnhub",
            "source_id": f"fh_ipo|{date_iso}|{it.get('symbol') or it.get('name')}",
        })
    return out


# ---------------- helpers ----------------

def _parse_ts(s: str | None, default_day: datetime) -> datetime:
    if not s:
        return default_day.replace(tzinfo=timezone.utc)
    # Finnhub uses formats like "2026-07-27 13:30:00" or with Z
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", ""), fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return default_day.replace(tzinfo=timezone.utc)


def _num_to_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).strip()
    return s if s else None


def _to_num(v: Any):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quarter_label(date_iso: str) -> str:
    try:
        d = datetime.fromisoformat(date_iso[:10])
        q = (d.month - 1) // 3 + 1
        return f"{d.year}Q{q}"
    except ValueError:
        return None


# ---------------- entrypoint ----------------

def fetch_all(target_date: datetime) -> dict[str, list[dict]]:
    """EN fetchers. Returns {table_name: [row_dicts...]}."""
    start = target_date - timedelta(days=1)
    end = target_date + timedelta(days=7)  # forward-looking 7-day window for earnings/IPO

    economic = fetch_economic(start, end)
    time.sleep(1)  # ponytail: free tier rate-limit courtesy
    earnings = fetch_earnings(start, end)
    time.sleep(1)
    ipo = fetch_ipo(start, end)

    return {
        "economic_events": economic,
        "earnings_calendar": earnings,
        "corporate_events": [],  # ponytail: Finnhub has no bulk corporate-event calendar API; CN covers this
        "ipo_calendar": ipo,
    }


if __name__ == "__main__":
    if os.environ.get("FINNHUB_API_KEY", "").strip() in ("", "TODO"):
        print("SKIP self-check: FINNHUB_API_KEY not set (register at finnhub.io)")
        sys.exit(0)
    today = datetime.now(timezone.utc)
    data = fetch_all(today)
    print(f"=== finnhub EN fetch @ {today.date()} ===")
    for tbl, rows in data.items():
        print(f"  {tbl}: {len(rows)} rows")
    assert data["economic_events"] or data["earnings_calendar"] or data["ipo_calendar"], \
        "all empty — check API key / rate limit"
