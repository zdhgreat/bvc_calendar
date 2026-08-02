"""CN calendar source via akshare.

Each fetch_* function returns a list of plain dicts ready for INSERT.
All return values normalize to the schema in app/schema.sql.

Function → event-type map:
  news_economic_baidu(date)                    -> economic_events
  stock_yjkb_em(Q-end)  filter 公告日期==today  -> earnings_calendar
  stock_fhps_em(Q-end)  filter 除权除息日==today-> corporate_events (dividend)
  stock_gddh_em()       filter 召开开始日==today-> corporate_events (gm)
  stock_restricted_release_summary_em(range)   -> corporate_events (unlock, daily aggregate)
  stock_ipo_declare_em()                       -> ipo_calendar (pre-IPO pipeline)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

# ponytail: akshare writes tqdm progress bars to stderr; harmless noise, leave as-is.
import akshare as ak

# Reconfigure stdout so Chinese column names don't crash on cp1252 Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------- helpers ----------------

def _safe_str(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def _safe_int(v: Any) -> int | None:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _last_quarter_end(d: datetime) -> str:
    """Most recent quarter-end strictly before d, as YYYYMMDD."""
    y, m = d.year, d.month
    qe = [(y, 3, 31), (y, 6, 30), (y, 9, 30), (y, 12, 31),
          (y - 1, 12, 31)]
    prev = [e for e in qe if datetime(*e) < d.replace(day=1, hour=0, minute=0, second=0)]
    return f"{prev[-1][0]}{prev[-1][1]:02d}{prev[-1][2]:02d}"


# ---------------- economic (macro) ----------------

def fetch_economic_events(date_str: str) -> list[dict]:
    """date_str: YYYYMMDD. Global macro calendar via Baidu."""
    try:
        df = ak.news_economic_baidu(date=date_str)
    except Exception as e:
        print(f"[akshare.economic] fetch failed: {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    out: list[dict] = []
    base = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    for _, r in df.iterrows():
        t = _safe_str(r.get("时间"))
        dt_str = f"{base} {t}" if t and ":" in t else f"{base} 00:00"
        try:
            dt = datetime.fromisoformat(dt_str)
        except ValueError:
            continue
        country = _safe_str(r.get("地区")) or "未知"
        indicator = _safe_str(r.get("事件")) or "(unknown)"
        out.append({
            "event_time": dt,
            "country": country[:32],
            "indicator": indicator[:128],
            "importance": _safe_int(r.get("重要性")),
            "actual": _safe_str(r.get("公布")),
            "forecast": _safe_str(r.get("预期")),
            "previous": _safe_str(r.get("前值")),
            "source": "akshare_baidu",
            "source_id": f"{date_str}|{country}|{indicator[:60]}|{t}",
        })
    return out


# ---------------- earnings ----------------

def fetch_earnings(target_date: datetime) -> list[dict]:
    """A-share companies whose 公告日期 == target_date (most recent quarter)."""
    qend = _last_quarter_end(target_date)
    try:
        df = ak.stock_yjkb_em(date=qend)
    except Exception as e:
        print(f"[akshare.earnings] fetch failed (qend={qend}): {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    target_iso = target_date.strftime("%Y-%m-%d")
    out: list[dict] = []
    for _, r in df.iterrows():
        announce = _safe_str(r.get("公告日期"))
        if not announce or not announce.startswith(target_iso):
            continue
        code = _safe_str(r.get("股票代码")) or ""
        name = _safe_str(r.get("股票简称")) or ""
        out.append({
            "report_date": target_iso,
            "ticker": code[:32],
            "exchange": _exchange_from_code(code),
            "company": name[:128],
            "period": qend,
            "source": "akshare_em",
            "source_id": f"yjkb|{qend}|{code}",
        })
    return out


# ---------------- corporate events ----------------

def _exchange_from_code(code: str) -> str:
    if not code:
        return None
    if code.startswith(("60", "68", "90", "11")):
        return "SSE"
    if code.startswith(("00", "30", "20", "12")):
        return "SZSE"
    if code.startswith(("43", "83", "87", "88")):
        return "BSE"
    if code.startswith("688") or code.startswith("787"):
        return "STAR"
    return None


def fetch_dividends(target_date: datetime) -> list[dict]:
    """A-share 除权除息 events on target_date."""
    qend = _last_quarter_end(target_date)
    try:
        df = ak.stock_fhps_em(date=qend)
    except Exception as e:
        print(f"[akshare.dividend] fetch failed: {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    target_iso = target_date.strftime("%Y-%m-%d")
    out: list[dict] = []
    for _, r in df.iterrows():
        ex_date = _safe_str(r.get("除权除息日"))
        if not ex_date or not ex_date.startswith(target_iso):
            continue
        code = _safe_str(r.get("代码")) or ""
        name = _safe_str(r.get("名称")) or ""
        cash = _safe_str(r.get("现金分红-现金分红比例"))
        stock_ratio = _safe_str(r.get("送转股份-送转总比例"))
        status = _safe_str(r.get("方案进度")) or ""
        desc = f"现金分红{cash}元/10股; 送转{stock_ratio}" if (cash or stock_ratio) else status
        out.append({
            "event_date": target_iso,
            "ticker": code[:32],
            "event_type": "dividend",
            "description": desc[:500],
            "source": "akshare_em",
            "source_id": f"fhps|{qend}|{code}",
        })
    return out


def fetch_shareholder_meetings(target_date: datetime) -> list[dict]:
    """A-share 股东大会 on target_date."""
    try:
        df = ak.stock_gddh_em()
    except Exception as e:
        print(f"[akshare.gm] fetch failed: {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    target_iso = target_date.strftime("%Y-%m-%d")
    out: list[dict] = []
    for _, r in df.iterrows():
        open_day = _safe_str(r.get("召开开始日"))
        if not open_day or not open_day.startswith(target_iso):
            continue
        code = _safe_str(r.get("代码")) or ""
        name = _safe_str(r.get("简称")) or ""
        meeting = _safe_str(r.get("股东大会名称")) or ""
        out.append({
            "event_date": target_iso,
            "ticker": code[:32],
            "event_type": "gm",
            "description": f"{name} {meeting}"[:500],
            "source": "akshare_em",
            "source_id": f"gddh|{target_iso}|{code}|{meeting[:30]}",
        })
    return out


def _cn_amount(v) -> str:
    """Format a numeric amount with 万/亿 units; fall back to raw on error.

    akshare returns share counts / market values as raw floats (e.g.
    610724628.0, 5629560454.7699995) whose str() leaks '.0' and float drift.
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v) if v is not None else "-"
    if n != n or n == 0:  # NaN or zero
        return "0"
    if abs(n) >= 1e8:
        return f"{n / 1e8:.2f}亿"
    if abs(n) >= 1e4:
        return f"{n / 1e4:.2f}万"
    return f"{n:.0f}"


def fetch_unlock_summary(start: datetime, end: datetime) -> list[dict]:
    """Daily 限售解禁 aggregate; one synthetic 'ticker' = AGG-CN per day."""
    try:
        df = ak.stock_restricted_release_summary_em(
            symbol="全部股票",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        print(f"[akshare.unlock] fetch failed: {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    out: list[dict] = []
    for _, r in df.iterrows():
        d = _safe_str(r.get("解禁时间"))
        if not d:
            continue
        try:
            d_iso = datetime.fromisoformat(d).strftime("%Y-%m-%d")
        except ValueError:
            continue
        count = _safe_str(r.get("当日解禁股票家数"))
        shares = _cn_amount(r.get("解禁数量"))
        value = _cn_amount(r.get("实际解禁市值"))
        desc = f"全市场解禁:{count}家,解禁{shares}股,市值{value}元"
        out.append({
            "event_date": d_iso,
            "ticker": "AGG-CN",
            "event_type": "unlock_summary",
            "description": desc[:500],
            "source": "akshare_em",
            "source_id": f"unlock_summary|{d_iso}",
        })
    return out


# ---------------- IPO ----------------

def fetch_ipo_pipeline() -> list[dict]:
    """CN IPO declaration queue (pre-IPO companies). Captures current snapshot."""
    try:
        df = ak.stock_ipo_declare_em()
    except Exception as e:
        print(f"[akshare.ipo] fetch failed: {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    today_iso = datetime.now().strftime("%Y-%m-%d")
    out: list[dict] = []
    for _, r in df.iterrows():
        name = _safe_str(r.get("企业名称")) or ""
        status = _safe_str(r.get("最新状态")) or ""
        exchange_target = _safe_str(r.get("拟上市地点")) or ""
        updated = _safe_str(r.get("更新日期"))
        if not updated:
            continue
        try:
            d_iso = datetime.fromisoformat(updated).strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({
            "event_date": d_iso,  # status-update date as event_date
            "ticker": None,
            "company": name[:128],
            "exchange": exchange_target[:16] if exchange_target else None,
            "price_low": None,
            "price_high": None,
            "status": status[:16] if status else None,
            "source": "akshare_em",
            "source_id": f"ipo_declare|{name[:60]}|{exchange_target}",
        })
    # ponytail: pre-IPO pipeline has thousands of stale entries; only keep last 30 days of updates
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    out = [x for x in out if x["event_date"] >= cutoff]
    return out


# ---------------- entrypoint for runner ----------------

def fetch_all(target_date: datetime) -> dict[str, list[dict]]:
    """Run all CN fetchers. Returns {table_name: [row_dicts...]}."""
    date_str = target_date.strftime("%Y%m%d")
    # Unlock summary covers a week centered on target_date
    week_start = target_date - timedelta(days=3)
    week_end = target_date + timedelta(days=3)

    economic = fetch_economic_events(date_str)
    earnings = fetch_earnings(target_date)
    dividends = fetch_dividends(target_date)
    gms = fetch_shareholder_meetings(target_date)
    unlocks = fetch_unlock_summary(week_start, week_end)
    ipo = fetch_ipo_pipeline()

    return {
        "economic_events": economic,
        "earnings_calendar": earnings,
        "corporate_events": dividends + gms + unlocks,
        "ipo_calendar": ipo,
    }


if __name__ == "__main__":
    # Self-check: fetch today, assert each table has rows or print empties.
    today = datetime.now()
    data = fetch_all(today)
    print(f"=== akshare CN fetch @ {today.date()} ===")
    empty: list[str] = []
    for tbl, rows in data.items():
        print(f"  {tbl}: {len(rows)} rows")
        if not rows:
            empty.append(tbl)
    # IPO pipeline should always have entries; macro calendar usually has several per day.
    assert data["economic_events"], "economic_events empty — baidu endpoint broken?"
    assert data["ipo_calendar"], "ipo_calendar empty — endpoint broken?"
    # Empty earnings/dividends/GM on weekends/holidays is normal.
    if empty:
        print(f"  (empty tables: {empty} — OK on non-trading days)")
