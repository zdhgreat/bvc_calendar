"""Calendar aggregator runner.

Pulls CN (akshare) + EN (finnhub) calendar data and UPSERTs into PostgreSQL.
Idempotent via UNIQUE(source, source_id).

bbs-go integration is pull-only: bbs reads the read-only JSON feed
(app.routers.calendar /api/feed, /api/event) and creates its own topics.
This runner no longer pushes or knows about bbs-go.

Usage:
    python -m app.crawler.runner                 # today
    python -m app.crawler.runner --date 2026-07-27
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2.extras

# ponytail: ensure local imports work regardless of CWD
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.crawler import akshare_calendar, finnhub_calendar, ir_calendar
from app.db import get_conn


TABLE_COLUMNS = {
    "economic_events": ["event_time", "country", "indicator", "importance",
                        "actual", "forecast", "previous", "source", "source_id"],
    "earnings_calendar": ["report_date", "ticker", "exchange", "company",
                          "period", "source", "source_id"],
    "corporate_events": ["event_date", "ticker", "event_type", "description",
                         "source", "source_id"],
    "ipo_calendar": ["event_date", "ticker", "company", "exchange",
                     "price_low", "price_high", "status", "source", "source_id"],
}


def _upsert(cur, table: str, rows: list[dict]) -> None:
    """UPSERT rows into `table`. Idempotent via UNIQUE(source, source_id)."""
    if not rows:
        return
    cols = TABLE_COLUMNS[table]
    sql = f"""
        INSERT INTO {table} ({", ".join(cols)}, fetched_at)
        VALUES %s
        ON CONFLICT (source, source_id) DO UPDATE
        SET fetched_at = NOW(),
            {", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ('source','source_id'))}
    """
    values = [[r.get(c) for c in cols] + [datetime.now(timezone.utc)] for r in rows]
    template = f"({', '.join(['%s'] * (len(cols) + 1))})"
    psycopg2.extras.execute_values(cur, sql, values, template=template)


def run(target_date: datetime) -> dict[str, int]:
    """Run all sources, UPSERT, return {table: rows_affected}."""
    print(f"[calendar.runner] target_date={target_date.date()}")

    cn = akshare_calendar.fetch_all(target_date)
    try:
        en = finnhub_calendar.fetch_all(target_date)
    except RuntimeError as e:
        print(f"[calendar.runner] finnhub skipped: {e}", file=sys.stderr)
        en = {t: [] for t in TABLE_COLUMNS}

    # IR crawler writes directly to corporate_events via _event_store shim.
    # Returns {} — do not merge into `merged`; runner's _upsert is a no-op for IR.
    try:
        ir_calendar.fetch_all(target_date)
    except Exception as e:
        print(f"[calendar.runner] ir_crawler skipped: {e}", file=sys.stderr)

    merged: dict[str, list[dict]] = {tbl: [] for tbl in TABLE_COLUMNS}
    for tbl in TABLE_COLUMNS:
        merged[tbl] = cn.get(tbl, []) + en.get(tbl, [])

    counts: dict[str, int] = {}
    conn = _connect_compat()
    try:
        with conn:
            with conn.cursor() as cur:
                for tbl, rows in merged.items():
                    _upsert(cur, tbl, rows)
                    counts[tbl] = len(rows)
                    print(f"  {tbl}: +{len(rows)} rows")
    finally:
        conn.close()
    return counts


def _connect_compat():
    """Reuse app.db config — kept as a function so runner is testable in isolation."""
    from app.db import _dsn
    import psycopg2
    return psycopg2.connect(**_dsn())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    target = datetime.now()
    if args.date:
        target = datetime.fromisoformat(args.date)

    counts = run(target)

    # ponytail self-check: at least one row must have landed
    total = sum(counts.values())
    assert total > 0, "all tables empty — both sources failed?"
    print(f"[calendar.runner] done: {total} rows upserted")


if __name__ == "__main__":
    main()
