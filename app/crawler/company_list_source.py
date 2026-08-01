#!/usr/bin/env python3
"""
Shared company list management for officecodex.

The default config is the synced `SynologyDrive-shared/company_lists.json`.
Use COMPANY_LIST_CONFIG to point to another JSON file, or COMPANY_LIST_NAME to
temporarily select a list.
"""

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

TOOL_DIR = Path(__file__).resolve().parent
SHARED_CONFIG_FILE = (
    Path.home()
    / "Library"
    / "CloudStorage"
    / "SynologyDrive-shared"
    / "company_lists.json"
)
CONFIG_FILE = Path(
    os.getenv(
        "COMPANY_LIST_CONFIG",
        SHARED_CONFIG_FILE if SHARED_CONFIG_FILE.exists() else TOOL_DIR / "company_lists.json",
    )
)
DEFAULT_SOURCES = [
    "ima_research",
    "zsxq",
    "xueqiu",
    "company_ir",
    "caixin",
    "futu",
    "theinformation_twitter",
    "techcrunch",
    "reuters",
    "theverge",
    "mx",
    "iwencai",
    "news_aggregator",
    "google_news_overseas",
    "marketwatch",
    "seeking_alpha",
    "twitter",
]


def _load_config() -> Dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"公司列表配置不存在: {CONFIG_FILE}")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(config: Dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _stable_json(data: Dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_fingerprint(data: Optional[Dict] = None) -> str:
    config = data if data is not None else _load_config()
    return hashlib.sha256(_stable_json(config).encode("utf-8")).hexdigest()[:16]


def get_config_path() -> Path:
    return CONFIG_FILE


def get_active_list_name() -> str:
    config = _load_config()
    return os.getenv("COMPANY_LIST_NAME") or config.get("active_list", "BV Watchlist")


def get_company_list(list_name: Optional[str] = None) -> List[Dict]:
    config = _load_config()
    resolved_name = list_name or get_active_list_name()
    lists = config.get("lists", {})
    if resolved_name not in lists:
        available = ", ".join(sorted(lists)) or "无"
        raise ValueError(f"公司列表不存在: {resolved_name}。可用列表: {available}")
    return lists[resolved_name].get("companies", [])


def get_company_names(list_name: Optional[str] = None) -> List[str]:
    return [company["name"] for company in get_company_list(list_name)]


def get_company_codes(list_name: Optional[str] = None) -> Dict[str, str]:
    return {
        company["name"]: company.get("code", "")
        for company in get_company_list(list_name)
        if company.get("code")
    }


def get_company_markets(list_name: Optional[str] = None) -> Dict[str, str]:
    return {
        company["name"]: company.get("market", "")
        for company in get_company_list(list_name)
        if company.get("market")
    }


def get_source_order(list_name: Optional[str] = None) -> List[str]:
    config = _load_config()
    resolved_name = list_name or get_active_list_name()
    lists = config.get("lists", {})
    if resolved_name not in lists:
        available = ", ".join(sorted(lists)) or "无"
        raise ValueError(f"公司列表不存在: {resolved_name}。可用列表: {available}")
    return lists[resolved_name].get("sources", DEFAULT_SOURCES)


def get_list_snapshot(list_name: Optional[str] = None) -> Dict:
    config = _load_config()
    resolved_name = list_name or get_active_list_name()
    companies = get_company_list(resolved_name)
    securities = get_company_securities(resolved_name) if "get_company_securities" in globals() else []
    return {
        "config_path": str(get_config_path()),
        "config_fingerprint": config_fingerprint(config),
        "active_list": get_active_list_name(),
        "list_name": resolved_name,
        "companies": companies,
        "company_count": len(companies),
        "security_count": len(securities),
        "sources": get_source_order(resolved_name),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


CSV_FIELDS = [
    "input_region",
    "normalized_market",
    "exchange",
    "ticker",
    "company_name_cn",
    "company_name_en",
    "primary_announcement_source",
    "status",
    "notes",
]


def split_list_names(raw_names: Optional[List[str]] = None) -> List[str]:
    names: List[str] = []
    for raw in raw_names or []:
        for part in str(raw).split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def resolve_list_names(raw_names: Optional[List[str]] = None) -> List[str]:
    return split_list_names(raw_names) or [get_active_list_name()]


def _source_for_exchange(exchange: str) -> str:
    if exchange == "HKEX":
        return "HKEXnews"
    if exchange == "SZSE":
        return "CNINFO / SZSE"
    if exchange == "SSE":
        return "CNINFO / SSE"
    if exchange == "BSE":
        return "CNINFO / BSE"
    if exchange == "KRX":
        return "KRX KIND"
    if exchange == "Tokyo Stock Exchange":
        return "JPX TDnet"
    if exchange == "SGX":
        return "SGX investors / SGXNet"
    if exchange.startswith("Euronext") or exchange.startswith("Deutsche"):
        return "Euronext issuer announcements"
    return "SEC EDGAR"


def _security_from_code(company: Dict) -> Dict:
    code = str(company.get("code", "")).strip().upper()
    market = company.get("market", "")
    if not code:
        return {}

    exchange = company.get("exchange", "")
    normalized_market = "US"
    ticker = code
    input_region = market or infer_market_from_code(code)

    if code.endswith(".HK"):
        normalized_market, exchange = "Hong Kong", "HKEX"
        ticker = code[:-3][-4:]
    elif code.endswith(".SZ"):
        normalized_market, exchange, ticker = "China", "SZSE", code[:-3]
    elif code.endswith(".SH"):
        normalized_market, exchange, ticker = "China", "SSE", code[:-3]
    elif code.endswith(".BJ"):
        normalized_market, exchange, ticker = "China", "BSE", code[:-3]
    elif code.endswith(".KS"):
        normalized_market, exchange, ticker = "Korea", "KRX", code[:-3]
    elif code.endswith(".T"):
        normalized_market, exchange, ticker = "Japan", "Tokyo Stock Exchange", code[:-2]
    elif code.endswith(".SI"):
        normalized_market, exchange, ticker = "Singapore", "SGX", code[:-3]
    elif code.endswith(".PA"):
        normalized_market, exchange, ticker = "Europe", "Euronext Paris", code[:-3]
    elif code.endswith(".AS"):
        normalized_market, exchange, ticker = "Europe", "Euronext Amsterdam", code[:-3]
    elif code.endswith(".F"):
        normalized_market, exchange, ticker = "Europe", "Deutsche Boerse Xetra", code[:-2]

    return {
        "input_region": input_region,
        "normalized_market": normalized_market,
        "exchange": exchange or company.get("exchange", "US"),
        "ticker": ticker,
        "primary_announcement_source": company.get("primary_announcement_source") or _source_for_exchange(exchange),
        "status": company.get("status", "active"),
        "notes": company.get("notes", ""),
    }


def get_company_securities(list_name: Optional[str] = None) -> List[Dict]:
    rows: List[Dict] = []
    seen = set()
    for company in get_company_list(list_name):
        securities = company.get("securities") or [_security_from_code(company)]
        for security in securities:
            if not security:
                continue
            row = {field: security.get(field, "") for field in CSV_FIELDS}
            row["company_name_cn"] = company.get("company_name_cn") or company.get("name", "")
            row["company_name_en"] = company.get("company_name_en", "")
            key = (row["exchange"], row["ticker"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def export_announcement_radar_rows(raw_names: Optional[List[str]] = None) -> List[Dict]:
    rows: List[Dict] = []
    seen = set()
    for name in resolve_list_names(raw_names):
        for row in get_company_securities(name):
            key = (row["exchange"], row["ticker"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def selected_lists_snapshot(raw_names: Optional[List[str]] = None) -> Dict:
    selected = resolve_list_names(raw_names)
    config = _load_config()
    rows = export_announcement_radar_rows(selected)
    companies = []
    for name in selected:
        companies.extend(get_company_list(name))
    return {
        "config_path": str(get_config_path()),
        "config_fingerprint": config_fingerprint(config),
        "active_list": get_active_list_name(),
        "selected_lists": selected,
        "company_count": len(companies),
        "security_count": len(rows),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def export_announcement_radar_csv(
    out_path: str,
    raw_names: Optional[List[str]] = None,
    manifest_path: Optional[str] = None,
) -> List[Dict]:
    rows = export_announcement_radar_rows(raw_names)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    manifest = selected_lists_snapshot(raw_names)
    manifest.update({
        "csv_path": str(out),
        "csv_fields": CSV_FIELDS,
    })
    if manifest_path:
        manifest_out = Path(manifest_path)
        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rows


def create_list(name: str, description: str = "", source: Optional[str] = None) -> None:
    config = _load_config()
    lists = config.setdefault("lists", {})
    if name in lists:
        raise ValueError(f"公司列表已存在: {name}")
    companies = get_company_list(source) if source else []
    sources = get_source_order(source) if source else DEFAULT_SOURCES
    lists[name] = {"description": description, "companies": companies, "sources": sources}
    _save_config(config)


def set_sources(list_name: str, sources: List[str]) -> None:
    config = _load_config()
    lists = config.setdefault("lists", {})
    if list_name not in lists:
        raise ValueError(f"公司列表不存在: {list_name}")
    lists[list_name]["sources"] = sources
    _save_config(config)


def set_active_list(name: str) -> None:
    config = _load_config()
    if name not in config.get("lists", {}):
        raise ValueError(f"公司列表不存在: {name}")
    config["active_list"] = name
    _save_config(config)


CODE_MARKET_RULES = [
    (re.compile(r"^\d{4,5}\.HK$"), "港股"),
    (re.compile(r"^\d{6}\.(SH|SZ|BJ)$"), "A股"),
    (re.compile(r"^[0-9A-Z]{4}\.T$"), "日股"),
    (re.compile(r"^\d{6}\.KS$"), "韩股"),
    (re.compile(r"^[A-Z0-9]{1,5}\.SI$"), "新交所"),
    (re.compile(r"^[A-Z0-9]{1,5}\.(PA|AS|F|JO)$"), "欧股"),
    (re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,3})?$"), "美股"),
]


def infer_market_from_code(code: str) -> str:
    normalized = (code or "").strip().upper()
    for pattern, market in CODE_MARKET_RULES:
        if pattern.match(normalized):
            return market
    return ""


def validate_company_entry(name: str, code: str, keyword: str, market: str) -> str:
    if not name:
        raise ValueError("新增标的必须填写名称。")
    if not code:
        raise ValueError("新增标的必须填写证券代码；请先检查是否多地上市并确认目标市场。")
    resolved_market = market or infer_market_from_code(code)
    if not resolved_market:
        raise ValueError("新增标的必须填写市场；请先检查是否多地上市并确认目标市场。")
    inferred = infer_market_from_code(code)
    if inferred and inferred != resolved_market:
        raise ValueError(f"代码 {code} 推断市场为 {inferred}，但传入市场为 {resolved_market}，请核对多地上市代码。")
    if not keyword:
        raise ValueError("新增标的必须填写 keyword，建议包含中文名、英文名/简称和关键代码。")
    return resolved_market


def add_company(list_name: str, name: str, code: str = "", keyword: str = "", market: str = "") -> None:
    resolved_market = validate_company_entry(name, code, keyword, market)
    config = _load_config()
    lists = config.setdefault("lists", {})
    if list_name not in lists:
        raise ValueError(f"公司列表不存在: {list_name}")
    companies = lists[list_name].setdefault("companies", [])
    if any(company.get("name") == name for company in companies):
        raise ValueError(f"{list_name} 中已存在公司: {name}")
    companies.append({
        "name": name,
        "keyword": keyword,
        "code": code,
        "market": resolved_market,
    })
    _save_config(config)


def remove_company(list_name: str, name: str) -> None:
    config = _load_config()
    lists = config.setdefault("lists", {})
    if list_name not in lists:
        raise ValueError(f"公司列表不存在: {list_name}")
    companies = lists[list_name].get("companies", [])
    kept = [company for company in companies if company.get("name") != name]
    if len(kept) == len(companies):
        raise ValueError(f"{list_name} 中未找到公司: {name}")
    lists[list_name]["companies"] = kept
    _save_config(config)


def _print_lists() -> None:
    config = _load_config()
    active = config.get("active_list")
    for name, item in config.get("lists", {}).items():
        marker = "*" if name == active else " "
        count = len(item.get("companies", []))
        description = item.get("description", "")
        print(f"{marker} {name} ({count}家公司) {description}".rstrip())


def _print_companies(list_name: Optional[str] = None) -> None:
    resolved_name = list_name or get_active_list_name()
    print(f"{resolved_name}:")
    for idx, company in enumerate(get_company_list(resolved_name), 1):
        code = company.get("code", "")
        market = company.get("market", "")
        suffix = " ".join(part for part in [code, market] if part)
        print(f"{idx}. {company['name']}" + (f" ({suffix})" if suffix else ""))


def _print_codes(list_name: Optional[str] = None) -> None:
    for company in get_company_list(list_name):
        if company.get("code"):
            print(f"{company['name']}\t{company['code']}")


def _print_sources(list_name: Optional[str] = None) -> None:
    resolved_name = list_name or get_active_list_name()
    print(f"{resolved_name} sources:")
    for idx, source in enumerate(get_source_order(resolved_name), 1):
        print(f"{idx}. {source}")


def validate_list(list_name: Optional[str] = None) -> List[str]:
    resolved_name = list_name or get_active_list_name()
    issues: List[str] = []
    seen_names = set()
    seen_codes = set()
    for idx, company in enumerate(get_company_list(resolved_name), 1):
        name = str(company.get("name") or "").strip()
        code = str(company.get("code") or "").strip()
        keyword = str(company.get("keyword") or "").strip()
        market = str(company.get("market") or "").strip()
        label = name or f"#{idx}"
        if not name:
            issues.append(f"{label}: 缺 name")
        if name in seen_names:
            issues.append(f"{label}: name 重复")
        seen_names.add(name)
        if not code:
            issues.append(f"{label}: 缺 code")
        elif code in seen_codes:
            issues.append(f"{label}: code 重复 {code}")
        seen_codes.add(code)
        if not keyword:
            issues.append(f"{label}: 缺 keyword")
        if not market:
            issues.append(f"{label}: 缺 market")
        inferred = infer_market_from_code(code)
        if inferred and market and inferred != market:
            issues.append(f"{label}: code {code} 推断为 {inferred}，配置为 {market}")
    return issues


def _print_snapshot(list_name: Optional[str] = None) -> None:
    snapshot = get_list_snapshot(list_name)
    issues = validate_list(snapshot["list_name"])
    print(f"config: {snapshot['config_path']}")
    print(f"fingerprint: {snapshot['config_fingerprint']}")
    print(f"active: {snapshot['active_list']}")
    print(f"list: {snapshot['list_name']} ({snapshot['company_count']}家公司 / {snapshot['security_count']}个证券)")
    print("sources: " + ", ".join(snapshot["sources"]))
    print("companies:")
    for company in snapshot["companies"]:
        print(
            f"- {company.get('name', '')}\t{company.get('code', '')}\t"
            f"{company.get('market', '')}\t{company.get('keyword', '')}"
        )
    if issues:
        print("issues:")
        for issue in issues:
            print(f"- {issue}")
    else:
        print("issues: none")


def main() -> None:
    parser = argparse.ArgumentParser(description="共享公司列表管理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("lists", help="列出所有公司列表")

    show_parser = subparsers.add_parser("show", help="查看列表内公司")
    show_parser.add_argument("name", nargs="?", help="列表名称，默认当前激活列表")

    codes_parser = subparsers.add_parser("codes", help="输出列表公司证券代码，供脚本读取")
    codes_parser.add_argument("name", nargs="?", help="列表名称，默认当前激活列表")

    sources_parser = subparsers.add_parser("sources", help="查看列表的数据源顺序")
    sources_parser.add_argument("name", nargs="?", help="列表名称，默认当前激活列表")

    snapshot_parser = subparsers.add_parser("snapshot", help="输出列表快照和校验结果")
    snapshot_parser.add_argument("name", nargs="?", help="列表名称，默认当前激活列表")
    snapshot_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    validate_parser = subparsers.add_parser("validate", help="校验列表字段、重复项和代码市场一致性")
    validate_parser.add_argument("name", nargs="?", help="列表名称，默认当前激活列表")

    export_parser = subparsers.add_parser("export-announcement-radar", help="导出公告雷达 CSV")
    export_parser.add_argument("--watchlist", "--company-list", action="append", dest="watchlists",
                               help="列表名称，可重复传入或用逗号分隔；默认当前激活列表")
    export_parser.add_argument("--out", required=True, help="输出 CSV 路径")
    export_parser.add_argument("--manifest", help="输出本次导出的来源、列表名、数量、指纹等元信息")

    create_parser = subparsers.add_parser("create", help="创建公司列表")
    create_parser.add_argument("name")
    create_parser.add_argument("--description", default="")
    create_parser.add_argument("--copy-from", dest="copy_from")

    active_parser = subparsers.add_parser("activate", help="设置默认激活列表")
    active_parser.add_argument("name")

    set_sources_parser = subparsers.add_parser("set-sources", help="设置列表的数据源顺序")
    set_sources_parser.add_argument("list_name")
    set_sources_parser.add_argument("sources", nargs="+")

    add_parser = subparsers.add_parser("add", help="向列表添加公司")
    add_parser.add_argument("list_name")
    add_parser.add_argument("name")
    add_parser.add_argument("--code", default="")
    add_parser.add_argument("--keyword", default="")
    add_parser.add_argument("--market", default="")

    remove_parser = subparsers.add_parser("remove", help="从列表移除公司")
    remove_parser.add_argument("list_name")
    remove_parser.add_argument("name")

    args = parser.parse_args()

    if args.command == "lists":
        _print_lists()
    elif args.command == "show":
        _print_companies(args.name)
    elif args.command == "codes":
        _print_codes(args.name)
    elif args.command == "sources":
        _print_sources(args.name)
    elif args.command == "snapshot":
        if args.json:
            print(json.dumps(get_list_snapshot(args.name), ensure_ascii=False, indent=2))
        else:
            _print_snapshot(args.name)
    elif args.command == "validate":
        issues = validate_list(args.name)
        if issues:
            for issue in issues:
                print(issue)
            raise SystemExit(1)
        print("ok")
    elif args.command == "export-announcement-radar":
        rows = export_announcement_radar_csv(args.out, args.watchlists, args.manifest)
        print(f"exported {len(rows)} securities -> {args.out}")
        if args.manifest:
            print(f"manifest -> {args.manifest}")
    elif args.command == "create":
        create_list(args.name, args.description, args.copy_from)
        print(f"已创建公司列表: {args.name}")
    elif args.command == "activate":
        set_active_list(args.name)
        print(f"当前激活列表: {args.name}")
    elif args.command == "set-sources":
        set_sources(args.list_name, args.sources)
        print(f"已更新 {args.list_name} 的数据源顺序")
    elif args.command == "add":
        add_company(args.list_name, args.name, args.code, args.keyword, args.market)
        print(f"已添加公司到 {args.list_name}: {args.name}")
    elif args.command == "remove":
        remove_company(args.list_name, args.name)
        print(f"已从 {args.list_name} 移除: {args.name}")


if __name__ == "__main__":
    main()
