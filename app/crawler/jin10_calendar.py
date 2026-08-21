"""CN macro/stock calendar source via jin10 (金十数据) web calendar.

Data path: jin10's calendar is delivered over an obfuscated WebSocket, not HTTP
(see docs/jin10-reverse-notes.md). We drive a real Chrome via CDP and call the
page's `window.Jin10FlashInstance.getCalendar*` getters, which return clean JSON.

Requires: node + a Chrome started with --remote-debugging-port (CHROME_CDP_URL).
Tables covered:
  cj 宏观数据/事件  -> economic_events   (source="jin10" / "jin10_event")
  qh 期货数据       -> economic_events   (source="jin10_qh")
  us/hk 个股财报    -> earnings_calendar (source="jin10_us" / "jin10_hk")
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# ponytail: this module shells out to node with os.environ — load .env itself so the
# standalone self-check (python -m app.crawler.jin10_calendar) sees JIN10_X_TOKEN too,
# not just the runner path (which loads .env via app.db).
load_dotenv()

NODE_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "jin10_cdp_fetch.mjs"
_TIMEOUT = 120  # CDP round-trip budget for a multi-date batch
_DEFAULT_CDP = "http://127.0.0.1:9222"

# ponytail: per-platform Chrome candidates; CHROME_BIN env overrides all.
_CHROME_BINS = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
    "linux": ["google-chrome", "chromium"],
}


def _cdp_alive(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=2):
            return True
    except OSError:
        return False


def _ensure_cdp(endpoint: str) -> tuple[subprocess.Popen, str] | None:
    """jin10 needs a real Chrome with CDP. If none is listening on `endpoint`, spawn a
    temporary headless one (throwaway profile) and return (proc, profile_dir) so the
    caller can tear it down — keeps the daily cron self-contained, no launchd needed."""
    if _cdp_alive(endpoint):
        return None
    candidates = [os.environ.get("CHROME_BIN"), *_CHROME_BINS.get(sys.platform, [])]
    binary = next((b for b in candidates if b and (Path(b).exists() or shutil.which(b))), None)
    if not binary:
        raise RuntimeError(f"no Chrome CDP at {endpoint} and no Chrome binary found — set CHROME_BIN")
    port = urllib.parse.urlparse(endpoint).port or 9222
    profile = tempfile.mkdtemp(prefix="jin10-chrome-")
    proc = subprocess.Popen(
        [binary, "--headless=new", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if _cdp_alive(endpoint):
            return proc, profile
        if proc.poll() is not None:
            raise RuntimeError(f"launched Chrome exited early (rc={proc.returncode})")
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"Chrome did not open CDP on {endpoint} within 15s")


# ---------------- CDP transport ----------------

def _fetch_raw(dates: list[str]) -> dict[str, dict[str, list[dict]]]:
    """Run the node CDP helper; return {date: {category: [rows]}}.

    Raises on hard failure — a jin10 outage must NOT be silently reported as
    "no new events" (data-contract: failure != empty).
    """
    if not dates:
        return {}
    endpoint = os.environ.get("CHROME_CDP_URL", _DEFAULT_CDP)
    chrome = _ensure_cdp(endpoint)
    node = os.environ.get("JIN10_NODE_BIN", "node")
    cmd = [node, str(NODE_SCRIPT), *dates]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
        except FileNotFoundError as e:
            raise RuntimeError(f"node not found ({node}) — set JIN10_NODE_BIN") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"jin10 CDP fetch timed out after {_TIMEOUT}s") from e
    finally:
        if chrome:
            chrome_proc, profile = chrome
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
            shutil.rmtree(profile, ignore_errors=True)
    import json
    # ponytail: parse stdout FIRST — Node 24 on Windows may abort with a libuv
    # assertion on exit even after printing valid JSON (non-zero exit code).
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise RuntimeError(
            f"jin10 CDP fetch failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[:400]}"
        )


# ---------------- shared mapping helpers ----------------

def _dt(s: str | None) -> datetime | None:
    """jin10 pub_time/event_time is Beijing time 'YYYY-MM-DD HH:MM'; store naive (akshare convention)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        return None


def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s != "--" else None


def _star(v) -> int | None:
    try:
        return min(max(int(v), 1), 3)  # clamp: cj uses 1-3; us stock uses up to 4
    except (TypeError, ValueError):
        return None


def _with_unit(value: str | None, unit: str | None) -> str | None:
    """akshare rows carry unit inside the value string ('5.16%'); match that."""
    if value is None:
        return None
    u = _s(unit)
    if u and u not in value:
        return f"{value}{u}"[:32]
    return value[:32]


# ---------------- cj 宏观数据 / 事件 / qh 期货 → economic_events ----------------

def _map_data_row(it: dict, source: str) -> dict | None:
    dt = _dt(it.get("pub_time"))
    name = _s(it.get("indicator_name"))
    if not dt or not name:
        return None
    country = _s(it.get("country")) or "全球"
    unit = _s(it.get("unit"))
    # indicator carries the period so rows stay self-describing, e.g. "商品出口年率(7月)"
    period = _s(it.get("time_period"))
    indicator = f"{name}({period})" if period else name
    return {
        "event_time": dt,
        "country": country[:32],
        "indicator": indicator[:128],
        "importance": _star(it.get("star")),
        "actual": _with_unit(_s(it.get("actual")), unit),
        "forecast": _with_unit(_s(it.get("consensus")), unit),
        "previous": _with_unit(_s(it.get("previous")), unit),
        "source": source,
        "source_id": f"{source}|{it.get('data_id')}",
    }


def _map_event_row(it: dict) -> dict | None:
    dt = _dt(it.get("event_time"))
    content = _s(it.get("event_content"))
    if not dt or not content:
        return None
    country = _s(it.get("country")) or "全球"
    return {
        "event_time": dt,
        "country": country[:32],
        "indicator": content[:128],
        "importance": _star(it.get("star")),
        "actual": None,
        "forecast": None,
        "previous": None,
        "source": "jin10_event",
        "source_id": f"jin10_event|{it.get('id')}",
    }


# ---------------- us / hk 个股财报 → earnings_calendar ----------------

_TICKER_RE = re.compile(r"\(([A-Za-z0-9.]+)\)")


def _map_earnings_row(it: dict, source: str) -> dict | None:
    dt = _dt(it.get("pub_time"))
    name = _s(it.get("indicator_name"))  # e.g. "富途控股(FUTU.O)"
    if not dt or not name:
        return None
    m = _TICKER_RE.search(name)
    ticker = m.group(1) if m else None
    company = _TICKER_RE.sub("", name).strip() or None
    period = _s(it.get("time_period"))  # "2026年Q2" -> "2026Q2"
    if period:
        period = period.replace("年", "")
    session = _s(it.get("time_status"))  # 盘前/盘后 — keep company column clean, append to period
    if session and period:
        period = f"{period} {session}"
    elif session:
        period = session
    return {
        "report_date": dt.date().isoformat(),
        "ticker": (ticker or "")[:32],
        "exchange": (_s(it.get("country")) or "")[:16] or None,  # jin10 puts 纳斯达克/港交所 in country
        "company": (company or "")[:128] or None,
        "period": (period or "")[:32] or None,
        "source": source,
        "source_id": f"{source}|{it.get('data_id')}",
    }


# ---------------- fetchers ----------------

def _dates_between(start: datetime, end: datetime) -> list[str]:
    days = (end.date() - start.date()).days
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


def fetch_all(target_date: datetime) -> dict[str, list[dict]]:
    """jin10 fetchers. Returns {table_name: [row_dicts...]} matching runner.TABLE_COLUMNS keys."""
    start = target_date - timedelta(days=1)
    end = target_date + timedelta(days=7)  # same forward-looking window as finnhub
    raw = _fetch_raw(_dates_between(start, end))

    economic: list[dict] = []
    earnings: list[dict] = []
    errors: list[str] = []
    for date, cats in raw.items():
        for cat, rows in cats.items():
            if isinstance(rows, dict):  # {error: ...} from the node helper
                errors.append(f"{date}/{cat}: {rows['error']}")
                continue
            for it in rows:
                row = None
                if cat == "cj_data":
                    row = _map_data_row(it, "jin10")
                elif cat == "qh_data":
                    row = _map_data_row(it, "jin10_qh")
                elif cat == "cj_event":
                    row = _map_event_row(it)
                elif cat == "us_data":
                    row = _map_earnings_row(it, "jin10_us")
                    if row:
                        earnings.append(row)
                        continue
                elif cat == "hk_data":
                    row = _map_earnings_row(it, "jin10_hk")
                    if row:
                        earnings.append(row)
                        continue
                if row:
                    economic.append(row)
    if errors:
        # partial failure is NOT "无新增" — surface it loudly
        print(f"[jin10] partial category failures: {'; '.join(errors)}", file=sys.stderr)

    # ponytail: jin10 splits one earnings release into per-metric rows (EPS/营收/净利润
    # share company+date but have different data_id) — collapse to one row per company/day.
    seen_earn: set[tuple] = set()
    deduped_earnings: list[dict] = []
    for r in earnings:
        key = (r["source"], r["ticker"] or r["company"], r["report_date"])
        if key in seen_earn:
            continue
        seen_earn.add(key)
        deduped_earnings.append(r)
    earnings = deduped_earnings

    if not economic and not earnings:
        raise RuntimeError(f"jin10 returned zero rows for {start.date()}..{end.date()} — fetch broken?")

    return {
        "economic_events": economic,
        "earnings_calendar": earnings,
        "corporate_events": [],
        "ipo_calendar": [],
    }


if __name__ == "__main__":
    today = datetime.now()
    print(f"=== jin10 fetch @ {today.date()} (CHROME_CDP_URL={os.environ.get('CHROME_CDP_URL', 'http://127.0.0.1:9222')}) ===")
    data = fetch_all(today)
    for tbl, rows in data.items():
        print(f"  {tbl}: {len(rows)} rows")
    econ = data["economic_events"]
    assert econ, "economic_events empty — jin10 fetch broken?"
    by_source: dict[str, int] = {}
    for r in econ:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    print("  economic by source:", by_source)
    print("  sample:", {k: v for k, v in econ[0].items()})
