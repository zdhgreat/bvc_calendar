#!/usr/bin/env python3
"""Audit IR source coverage for companies in the global watchlists.

This is intentionally configuration-only: it reads the latest officecodex
company lists and local IR source definitions, then points out likely source
gaps before we spend time opening IR pages one by one.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from app.crawler.company_lists import (
    IR_SOURCE_ALIASES,
    _load_config,
    allowed_securities,
    get_company_list,
    get_config_path,
    resolve_list_names,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
IR_SOURCES_FILE = PROJECT_DIR / "config" / "ir_sources.json"
REPORT_DIR = PROJECT_DIR / "data" / "source_gap_audit"

CORE_LAYERS = [
    "calendar",
    "events_rss",
    "news_releases",
    "presentations_investor_day",
    "regulatory",
    "dividends",
]

LAYER_LABELS = {
    "calendar": "IR日历/事件页",
    "events_rss": "事件RSS",
    "news_releases": "新闻稿/公告RSS",
    "presentations_investor_day": "Presentation/Investor Day补充路径",
    "regulatory": "交易所/监管公告",
    "dividends": "股息分红",
}


def load_ir_sources() -> Dict[str, Dict[str, Any]]:
    with IR_SOURCES_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return {item.get("id", ""): item for item in data.get("sources", []) if item.get("id")}


def enabled_source(source: Dict[str, Any] | None) -> bool:
    return bool(source) and source.get("enabled", True)


def source_text(source: Dict[str, Any]) -> str:
    parts = [
        source.get("id", ""),
        source.get("name", ""),
        source.get("url", ""),
        source.get("type", ""),
        source.get("crawl_method", ""),
        source.get("note", ""),
    ]
    for key in ("fallback_url", "supplemental_urls"):
        value = source.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    tags = source.get("coverage_tags") or []
    parts.extend(str(tag) for tag in tags)
    return " ".join(parts).lower()


def classify_source(source: Dict[str, Any]) -> Set[str]:
    text = source_text(source)
    source_type = str(source.get("type", "")).lower()
    crawl_method = str(source.get("crawl_method", "")).lower()
    tags = {str(tag).strip() for tag in source.get("coverage_tags", []) if str(tag).strip()}
    layers: Set[str] = set()

    if "calendar" in source_type or "event" in text or "ir日历" in source.get("name", ""):
        layers.add("calendar")
    if crawl_method == "rss" and ("events.xml" in text or "event.aspx" in text or "event" in text):
        layers.add("events_rss")
    if (
        "news-release" in text
        or "news_releases" in text
        or "news.html" in text
        or crawl_method == "rss_news_releases"
        or "news_releases" in tags
    ):
        layers.add("news_releases")
    if any(token in text for token in ("presentation", "investor day", "capital market", "roadshow")):
        layers.add("presentations_investor_day")
    if source_type == "announcement" or any(token in text for token in ("cninfo", "sgx", "hkex", "sec edgar", "jpx", "tdnet")):
        layers.add("regulatory")

    layers.update(tag for tag in tags if tag in CORE_LAYERS)
    return layers


def markets_for_company(list_name: str, company: Dict[str, Any], securities_by_company: Dict[str, List[Dict[str, Any]]]) -> Set[str]:
    names = {
        value
        for value in (
            company.get("name", ""),
            company.get("company_name_cn", ""),
            company.get("company_name_en", ""),
        )
        if value
    }
    code = str(company.get("code", "")).split(".", 1)[0].lstrip("0")
    markets = set()
    for row in securities_by_company.get(list_name, []):
        row_names = {
            value
            for value in (row.get("company_name_cn", ""), row.get("company_name_en", ""))
            if value
        }
        ticker = str(row.get("ticker", "")).lstrip("0")
        if (names & row_names) or (code and code == ticker):
            markets.add(row.get("normalized_market", "") or "Unknown")
    if not markets:
        market = str(company.get("market", ""))
        if market:
            markets.add(market)
    return markets


def regulatory_layers(markets: Iterable[str]) -> Set[str]:
    # The calendar workflow already has market-level regulatory fallbacks for
    # A/H shares; US events are covered via SEC in the company information stack.
    known = {"China", "Hong Kong", "US", "Japan", "Korea", "Singapore", "Europe"}
    return {"regulatory"} if any(m in known or m in {"A股", "港股", "美股"} for m in markets) else set()


def dividend_layers(markets: Iterable[str]) -> Set[str]:
    # dividend_calendar.py currently supports A-share and Hong Kong ordinary
    # shares in the unified wrapper.
    return {"dividends"} if any(m in {"China", "Hong Kong", "A股", "港股"} for m in markets) else set()


def recommendation_for(layers: Set[str], markets: Set[str], has_ir_source: bool) -> List[str]:
    recs = []
    if not has_ir_source:
        recs.append("先补官方IR calendar/events页；没有前瞻日历时，补新闻稿RSS或news releases页")
    if "news_releases" not in layers:
        recs.append("补新闻稿/news releases/RSS，用于捕捉AGM、业绩披露预告、Investor Day补录")
    if "presentations_investor_day" not in layers:
        recs.append("补presentations/events-presentations/investor day路径，避免Kioxia这类事件漏抓")
    if "events_rss" not in layers and any(m in {"US", "美股"} for m in markets):
        recs.append("美股/ADR优先确认是否有events.xml或Event.aspx RSS，通常比网页稳定")
    if "dividends" not in layers and any(m in {"US", "美股"} for m in markets):
        recs.append("如需美股分红日历，另补SEC/交易所或行情源的ex-dividend/payment date路径")
    return recs


def audit_lists(raw_names: Iterable[str] | None = None) -> Dict[str, Any]:
    list_names = resolve_list_names(list(raw_names or []))
    sources = load_ir_sources()
    securities_by_company = {
        name: allowed_securities([name])
        for name in list_names
    }
    config = _load_config()

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "global_config_path": str(get_config_path()),
        "lists": [],
    }

    for list_name in list_names:
        companies = []
        summary = defaultdict(int)
        for company in get_company_list(list_name):
            raw_ids = company.get("ir_sources") or []
            source_ids = []
            source_rows = []
            layers: Set[str] = set()

            for raw_id in raw_ids:
                source_id = IR_SOURCE_ALIASES.get(raw_id, raw_id)
                source = sources.get(source_id)
                source_ids.append(source_id)
                if not enabled_source(source):
                    source_rows.append({
                        "id": source_id,
                        "status": "disabled" if source else "missing_config",
                        "url": source.get("url", "") if source else "",
                        "layers": [],
                    })
                    continue
                source_layers = sorted(classify_source(source))
                layers.update(source_layers)
                source_rows.append({
                    "id": source_id,
                    "status": "enabled",
                    "url": source.get("url", ""),
                    "layers": source_layers,
                })

            markets = markets_for_company(list_name, company, securities_by_company)
            layers.update(regulatory_layers(markets))
            layers.update(dividend_layers(markets))

            has_ir_source = any(row["status"] == "enabled" for row in source_rows)
            missing = [layer for layer in CORE_LAYERS if layer not in layers]
            recommendations = recommendation_for(layers, markets, has_ir_source)
            risk = "low"
            if not has_ir_source:
                risk = "high"
            elif "news_releases" not in layers and "presentations_investor_day" not in layers:
                risk = "medium"

            summary[risk] += 1
            companies.append({
                "company": company.get("name") or company.get("company_name_cn") or company.get("code", ""),
                "code": company.get("code", ""),
                "markets": sorted(markets),
                "ir_sources": source_rows,
                "covered_layers": sorted(layers),
                "missing_layers": missing,
                "risk": risk,
                "recommendations": recommendations,
            })

        result["lists"].append({
            "list_name": list_name,
            "company_count": len(companies),
            "risk_summary": dict(summary),
            "companies": companies,
        })
    return result


def print_text_report(report: Dict[str, Any]) -> None:
    print(f"生成时间(UTC): {report['generated_at_utc']}")
    print(f"IR源配置: {IR_SOURCES_FILE}")
    print()
    for list_report in report["lists"]:
        summary = list_report.get("risk_summary", {})
        print(f"列表: {list_report['list_name']} | 公司 {list_report['company_count']} | high {summary.get('high', 0)} / medium {summary.get('medium', 0)} / low {summary.get('low', 0)}")
        for row in list_report["companies"]:
            missing_labels = "、".join(LAYER_LABELS[x] for x in row["missing_layers"])
            covered_labels = "、".join(LAYER_LABELS[x] for x in row["covered_layers"] if x in LAYER_LABELS)
            print(f"- [{row['risk']}] {row['company']} {row['code']} | 已覆盖: {covered_labels or '无'}")
            if missing_labels:
                print(f"  缺口: {missing_labels}")
            for src in row["ir_sources"]:
                layer_text = "、".join(LAYER_LABELS[x] for x in src["layers"] if x in LAYER_LABELS)
                print(f"  源: {src['id']} ({src['status']}) {layer_text} {src['url']}".rstrip())
            for rec in row["recommendations"]:
                print(f"  建议: {rec}")
        print()


def write_report(report: Dict[str, Any], out: Path | None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        names = "_".join(item["list_name"].replace(" ", "_").lower() for item in report["lists"])
        out = REPORT_DIR / f"source_gap_audit_{names}_{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="审计公司列表的IR源覆盖缺口")
    parser.add_argument("--company-list", "--watchlist", action="append", dest="company_lists",
                        help="指定公司列表，可重复传入或逗号分隔；默认使用全局 active_list")
    parser.add_argument("--json", action="store_true", help="输出JSON")
    parser.add_argument("--write", action="store_true", help="写入 data/source_gap_audit/")
    parser.add_argument("--out", help="指定JSON报告输出路径")
    args = parser.parse_args()

    report = audit_lists(args.company_lists)
    if args.write or args.out:
        path = write_report(report, Path(args.out) if args.out else None)
        print(f"已写入源覆盖审计报告: {path}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)


if __name__ == "__main__":
    main()
