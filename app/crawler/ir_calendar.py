"""IR crawler entry point — matches the akshare/finnhub fetch_all contract.

Unlike akshare/finnhub which return rows for runner._upsert to UPSERT, this
module triggers ir_crawler's full crawl (which writes directly to PG via the
_event_store shim) and returns an empty dict. The runner's UPSERT step is a
no-op for IR; the rows are already in corporate_events.

Why: ir_crawler.main() owns non-trivial state — per-source coverage/failure
JSON, cross-source dedup, supplemental RSS follow-ups. Re-routing all that
through runner._upsert would duplicate the shim. Let ir_crawler own its write
path; runner just schedules it.

Usage:
    python -m app.crawler.ir_calendar                 # default watchlist
    python -m app.crawler.ir_calendar --company-list "BV Watchlist"
    python -m app.crawler.ir_calendar --test          # dry-run, no PG write
"""
from __future__ import annotations

import sys
from datetime import datetime


def fetch_all(target_date: datetime, company_lists: list[str] | None = None,
              test_mode: bool = False, include_monitors: bool = True) -> dict[str, list[dict]]:
    """Run the IR crawler + three A-share monitors. Returns {} — events land in PG via the shim.

    The signature matches akshare_calendar.fetch_all / finnhub_calendar.fetch_all
    so runner.run() can call it uniformly. `target_date` is accepted for
    signature parity but ignored: IR pages are crawled as-is; the crawler does
    not take a date argument.

    Monitors (dividend_calendar / shareholder_meeting / report_disclosure) are
    on by default; each swallows its own exceptions so a single source outage
    doesn't abort the daily run.
    """
    from app.crawler import ir_crawler

    argv = ["ir_crawler", "--all"]
    if test_mode:
        argv.append("--test")
    for name in (company_lists or []):
        argv += ["--company-list", name]

    saved = sys.argv
    sys.argv = argv
    try:
        ir_crawler.main()
    except SystemExit:
        pass  # argparse --help etc.
    finally:
        sys.argv = saved

    if include_monitors and not test_mode:
        from app.crawler import dividend_calendar, shareholder_meeting, report_disclosure
        kw = {"company_lists": company_lists} if company_lists else {}
        for mod, fn_name, fn_kwargs in (
            (dividend_calendar, "run", kw),
            (shareholder_meeting, "run", kw),
            (report_disclosure, "run", kw),
        ):
            try:
                getattr(mod, fn_name)(**fn_kwargs)
            except Exception as e:
                print(f"[ir_calendar] {mod.__name__}.{fn_name} skipped: {e}", file=sys.stderr)
    return {}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="IR calendar crawler (PG-backed)")
    ap.add_argument("--company-list", action="append", dest="company_lists",
                    help="Watchlist name(s); default: active list in config/company_lists.json")
    ap.add_argument("--test", action="store_true", help="Dry-run — no PG writes")
    ap.add_argument("--date", help="Ignored — kept for run_daily signature parity")
    args = ap.parse_args()

    fetch_all(datetime.now(), company_lists=args.company_lists, test_mode=args.test)


if __name__ == "__main__":
    main()
