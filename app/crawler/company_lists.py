#!/usr/bin/env python3
"""Compatibility helpers backed by a configurable shared company-list tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys
from typing import Dict, Iterable, List, Set


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_TOOL = (
    Path.home()
    / "Documents"
    / "Codex"
    / "officecodex"
    / "tools"
    / "company-lists"
    / "company_lists.py"
)
DEFAULT_CONFIG = (
    Path.home()
    / "Library"
    / "CloudStorage"
    / "SynologyDrive-shared"
    / "company_lists.json"
)
SHARED_TOOL = Path(
    os.getenv(
        "COMPANY_LIST_TOOL",
        DEFAULT_TOOL if DEFAULT_TOOL.exists() else Path(__file__).with_name("company_list_source.py"),
    )
).expanduser()
SHARED_TOOL_DIR = SHARED_TOOL.parent
SHARED_CONFIG = Path(
    os.getenv(
        "COMPANY_LIST_CONFIG",
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else PROJECT_DIR / "config" / "company_lists.json",
    )
).expanduser()

if not SHARED_TOOL.exists():
    raise FileNotFoundError(f"共享公司列表工具不存在: {SHARED_TOOL}")
if not SHARED_CONFIG.exists():
    raise FileNotFoundError(f"共享公司列表不存在: {SHARED_CONFIG}")

os.environ["COMPANY_LIST_CONFIG"] = str(SHARED_CONFIG)
sys.path.insert(0, str(SHARED_TOOL_DIR))
_shared = runpy.run_path(str(SHARED_TOOL), run_name="company_lists_shared")
if _shared["get_config_path"]().resolve() != SHARED_CONFIG.resolve():
    raise RuntimeError(f"共享公司列表路径错误: {_shared['get_config_path']()}")

get_active_list_name = _shared["get_active_list_name"]
get_company_list = _shared["get_company_list"]
get_company_names = _shared["get_company_names"]
get_company_codes = _shared["get_company_codes"]
get_company_markets = _shared["get_company_markets"]
get_company_securities = _shared["get_company_securities"]
get_source_order = _shared["get_source_order"]
resolve_list_names = _shared["resolve_list_names"]
export_announcement_radar_csv = _shared["export_announcement_radar_csv"]
config_fingerprint = _shared["config_fingerprint"]
get_config_path = _shared["get_config_path"]
_load_config = _shared["_load_config"]


class CompanyListError(ValueError):
    """Raised when a requested company list is missing or invalid."""


def split_list_names(raw_names: Iterable[str] = None) -> List[str]:
    return _shared["split_list_names"](list(raw_names or []))


def resolve_company_lists(raw_names: Iterable[str] = None) -> Dict:
    selected = resolve_list_names(list(raw_names or []))
    lists = []
    missing = []
    for name in selected:
        try:
            lists.append({"name": name, "companies": get_company_list(name), "sources": get_source_order(name)})
        except ValueError:
            missing.append(name)
    if missing:
        raise CompanyListError(f"Unknown company list(s): {', '.join(missing)}")
    return {"selected": selected, "lists": lists, "config": {"source": str(SHARED_TOOL)}}


IR_SOURCE_ALIASES = {
    "atour_ir": "atour_ir_rss",
    "haier_ir_calendar": "haier_ir_events",
    "joyy_ir": "joyy_ir_events",
    "kioxia_ir": "kioxia_ir_calendar",
    "luckin_ir": "luckin_ir_rss",
    "sandisk_ir": "sandisk_ir_events",
    "skhynix_ir": "skhynix_ir_events",
}

IR_SOURCE_COMPANIONS = {
    "intel_ir_calendar": ["intel_news_releases"],
    "joyy_ir_events": ["joyy_news_releases"],
    "vip_ir_calendar": ["vip_news_releases"],
    "huazhu_ir_calendar": ["huazhu_news_releases"],
    "atour_ir_rss": ["atour_news_releases"],
    "luckin_ir_rss": ["luckin_news_releases"],
    "neworiental_ir_rss": ["neworiental_news_releases"],
    "pdd_ir_rss": ["pdd_news_releases"],
    "sandisk_ir_events": ["sandisk_news_releases"],
    "micron_ir_calendar": ["micron_news_releases"],
    "futu_ir_calendar": ["futu_news_releases"],
}

def _configured_ir_source_ids() -> Set[str]:
    config_file = Path(__file__).resolve().parent.parent.parent / "config" / "ir_sources.json"
    if not config_file.exists():
        return set()
    with config_file.open(encoding="utf-8") as f:
        data = json.load(f)
    return {item.get("id", "") for item in data.get("sources", []) if item.get("enabled", True)}


def allowed_ir_source_ids(raw_names: Iterable[str] = None) -> Set[str]:
    resolved = resolve_company_lists(raw_names)
    configured = _configured_ir_source_ids()
    source_ids: Set[str] = set()
    for item in resolved["lists"]:
        for company in item.get("companies", []):
            for source_id in company.get("ir_sources", []):
                candidates = {source_id, IR_SOURCE_ALIASES.get(source_id, source_id)}
                for candidate in candidates:
                    if candidate in configured:
                        source_ids.add(candidate)
                        source_ids.update(
                            companion for companion in IR_SOURCE_COMPANIONS.get(candidate, [])
                            if companion in configured
                        )
    return source_ids


def allowed_a_share_codes(raw_names: Iterable[str] = None) -> Dict[str, str]:
    resolved = resolve_company_lists(raw_names)
    codes: Dict[str, str] = {}
    for item in resolved["lists"]:
        for company in item.get("companies", []):
            name = company.get("company_name_cn") or company.get("name", "")
            code = str(company.get("code", ""))
            if code.endswith((".SH", ".SZ", ".BJ")):
                codes[code.split(".", 1)[0].zfill(6)] = name
            for security in company.get("securities", []):
                market = security.get("normalized_market", "")
                ticker = str(security.get("ticker", ""))
                if market == "China" and ticker.isdigit():
                    codes[ticker.zfill(6)] = name
    return codes


def allowed_securities(raw_names: Iterable[str] = None, markets: Iterable[str] = None) -> List[Dict]:
    """Return active securities from the selected global company lists.

    markets uses the normalized global company-list market labels, such as
    China, Hong Kong, and US.
    """
    selected = resolve_list_names(list(raw_names or []))
    wanted = {m for m in (markets or []) if m}
    rows: List[Dict] = []
    seen = set()
    for name in selected:
        for row in get_company_securities(name):
            if row.get("status") and row.get("status") != "active":
                continue
            market = row.get("normalized_market", "")
            if wanted and market not in wanted:
                continue
            key = (
                market,
                row.get("exchange", ""),
                row.get("ticker", ""),
                row.get("company_name_cn", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append({**row, "list_name": name})
    return rows


def allowed_securities_by_market(raw_names: Iterable[str] = None) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for row in allowed_securities(raw_names):
        grouped.setdefault(row.get("normalized_market", ""), []).append(row)
    return grouped


def list_summary() -> List[Dict]:
    result = []
    for name in sorted((_load_config().get("lists") or {}).keys()):
        companies = get_company_list(name)
        securities = get_company_securities(name)
        market_counts: Dict[str, int] = {}
        for security in securities:
            market = security.get("normalized_market", "") or "Unknown"
            market_counts[market] = market_counts.get(market, 0) + 1
        result.append({
            "id": name,
            "name": name,
            "ir_sources": len(allowed_ir_source_ids([name])),
            "a_shares": len(allowed_a_share_codes([name])),
            "companies": len(companies),
            "securities": len(securities),
            "markets": market_counts,
        })
    return result


if __name__ == "__main__":
    _shared["main"]()
