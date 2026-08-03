"""Health check for the financial-calendar project.

Checks:
  1. PG connectivity (app.db.get_conn)
  2. Schema has the IR-era columns on corporate_events
  3. config/ir_sources.json exists and parses
  4. config/company_lists.json exists and parses
  5. (optional) Chrome CDP endpoint reachable via CHROME_CDP_URL
  6. (optional) Finnhub API key set

Exits 0 if all required checks pass, 1 otherwise.

Usage:
    python scripts/doctor.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


REQUIRED_COLS = {"title", "company", "event_time", "timezone", "source_url"}


def _ok(label: str, detail: str = "") -> None:
    print(f"  [OK]   {label}{(': ' + detail) if detail else ''}")


def _fail(label: str, detail: str) -> bool:
    print(f"  [FAIL] {label}: {detail}")
    return False


def check_pg() -> bool:
    try:
        from app.db import get_conn, _dsn
    except ImportError as e:
        return _fail("PG db module", f"import failed: {e}")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as e:
        return _fail("PG connectivity", f"{e}")
    d = _dsn()
    _ok("PG connectivity", f"{d['host']}:{d['port']}/{d['dbname']}")
    return True


def check_schema() -> bool:
    try:
        from app.db import get_conn
    except ImportError:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'corporate_events'
                """)
                cols = {r[0] for r in cur.fetchall()}
    except Exception as e:
        return _fail("Schema inspect", str(e))
    missing = REQUIRED_COLS - cols
    if missing:
        return _fail("Schema (corporate_events)", f"missing columns: {sorted(missing)} — re-run `python scripts/init_db.py`")
    _ok("Schema (corporate_events)", f"has {len(cols)} cols incl IR-era fields")
    return True


def check_config(name: str, filename: str) -> bool:
    p = REPO_ROOT / "config" / filename
    if not p.exists():
        return _fail(name, f"missing {p}")
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return _fail(name, f"invalid JSON: {e}")
    _ok(name, str(p.relative_to(REPO_ROOT)))
    return True


def check_optional_env(env: str, hint: str) -> None:
    val = os.environ.get(env, "").strip()
    if val:
        print(f"  [OK]   env {env} set")
    else:
        print(f"  [WARN] env {env} not set ({hint})")


def main() -> int:
    print("financial-calendar doctor")
    results = [
        check_pg(),
        check_schema(),
        check_config("config/ir_sources.json", "ir_sources.json"),
        check_config("config/company_lists.json", "company_lists.json"),
    ]
    print()
    print("Optional:")
    check_optional_env("FINNHUB_API_KEY", "EN calendar data will be skipped")
    check_optional_env("CHROME_CDP_URL", "anti-bot IR sites will fail to protected-source")
    check_optional_env("FEED_TOKEN", "data feed (/api/feed, /api/event) is open")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
