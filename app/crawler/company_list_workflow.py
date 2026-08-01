#!/usr/bin/env python3
"""Fast workflow entrypoint for the configured global company lists."""

from __future__ import annotations

import argparse
import csv
import json
import os
import runpy
from pathlib import Path
from typing import Iterable, List

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
RADAR_DOCS_DIR = PROJECT_DIR / "announcement_radar" / "docs"
DEFAULT_RADAR_CSV = RADAR_DOCS_DIR / "announcement-radar-watchlist.generated.csv"
DEFAULT_RADAR_MANIFEST = RADAR_DOCS_DIR / "announcement-radar-watchlist.generated.manifest.json"

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
GLOBAL_TOOL = Path(
    os.getenv(
        "COMPANY_LIST_TOOL",
        DEFAULT_TOOL if DEFAULT_TOOL.exists() else Path(__file__).with_name("company_list_source.py"),
    )
).expanduser()
SHARED_CONFIG = Path(
    os.getenv(
        "COMPANY_LIST_CONFIG",
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else PROJECT_DIR / "config" / "company_lists.json",
    )
).expanduser()

if not GLOBAL_TOOL.exists():
    raise FileNotFoundError(f"全局公司列表工具不存在: {GLOBAL_TOOL}")
if not SHARED_CONFIG.exists():
    raise FileNotFoundError(f"共享公司列表不存在: {SHARED_CONFIG}")

os.environ["COMPANY_LIST_CONFIG"] = str(SHARED_CONFIG)
_global = runpy.run_path(str(GLOBAL_TOOL), run_name="officecodex_company_lists")
if _global["get_config_path"]().resolve() != SHARED_CONFIG.resolve():
    raise RuntimeError(f"公司列表路径不符合公告雷达契约: {_global['get_config_path']()}")

from app.crawler.source_gap_audit import audit_lists, print_text_report, write_report

RADAR_OVERLAP_EXCLUSIONS = {
    "Deo Watchlist": ["BV Watchlist"],
}


def security_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("normalized_market", "")).strip(),
        str(row.get("exchange", "")).strip(),
        str(row.get("ticker", "")).strip().upper(),
    )


def _name_key(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def company_keys(row: dict) -> set[str]:
    return {
        key
        for key in (
            _name_key(row.get("company_name_cn", "")),
            _name_key(row.get("company_name_en", "")),
        )
        if key
    }


def split_names(raw_names: Iterable[str] | None) -> List[str]:
    names: List[str] = []
    for raw in raw_names or []:
        for part in str(raw).split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def selected_names(raw_names: Iterable[str] | None) -> List[str]:
    return _global["resolve_list_names"](split_names(raw_names))


def validate_names(names: List[str]) -> dict[str, list[str]]:
    return {name: _global["validate_list"](name) for name in names}


def print_status(names: List[str]) -> None:
    config = _global["_load_config"]()
    active = _global["get_active_list_name"]()
    print(f"全局公司列表: {_global['get_config_path']()}")
    print(f"配置指纹: {_global['config_fingerprint'](config)}")
    print(f"当前默认列表: {active}")
    print()
    print("可用列表:")
    for name, item in sorted(config.get("lists", {}).items()):
        marker = "*" if name == active else " "
        companies = len(item.get("companies", []))
        securities = len(_global["get_company_securities"](name))
        issues = _global["validate_list"](name)
        issue_text = "ok" if not issues else f"{len(issues)} issue(s)"
        selected = " selected" if name in names else ""
        print(f"{marker} {name:16s} | 公司 {companies:2d} | 证券 {securities:2d} | {issue_text}{selected}")


def print_snapshot(name: str, as_json: bool) -> None:
    snapshot = _global["get_list_snapshot"](name)
    issues = _global["validate_list"](name)
    snapshot["issues"] = issues
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return
    print(f"列表: {snapshot['list_name']}")
    print(f"全局配置: {snapshot['config_path']}")
    print(f"配置指纹: {snapshot['config_fingerprint']}")
    print(f"公司数: {snapshot['company_count']}，证券数: {snapshot['security_count']}")
    print("校验: " + ("ok" if not issues else f"{len(issues)} issue(s)"))
    print("公司:")
    for company in snapshot["companies"]:
        code = company.get("code", "")
        market = company.get("market", "")
        print(f"- {company.get('name', '')}\t{code}\t{market}")


def _radar_exclusion_lists(names: List[str]) -> List[str]:
    exclusions: List[str] = []
    seen = set()
    for name in names:
        for exclusion in RADAR_OVERLAP_EXCLUSIONS.get(name, []):
            if exclusion not in names and exclusion not in seen:
                seen.add(exclusion)
                exclusions.append(exclusion)
    return exclusions


def export_radar(names: List[str], out: Path, manifest: Path) -> None:
    rows = _global["export_announcement_radar_rows"](names)
    exclusion_lists = _radar_exclusion_lists(names)
    excluded_rows: List[dict] = []

    if exclusion_lists:
        excluded_company_keys = {
            key
            for exclusion in exclusion_lists
            for row in _global["get_company_securities"](exclusion)
            for key in company_keys(row)
        }
        kept_rows = []
        for row in rows:
            if company_keys(row) & excluded_company_keys:
                excluded_rows.append(row)
            else:
                kept_rows.append(row)
        rows = kept_rows

    out.parent.mkdir(parents=True, exist_ok=True)
    csv_fields = _global["CSV_FIELDS"]
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(rows)

    config = _global["_load_config"]()
    companies = []
    for name in names:
        companies.extend(_global["get_company_list"](name))
    manifest_data = {
        "config_path": str(_global["get_config_path"]()),
        "config_fingerprint": _global["config_fingerprint"](config),
        "active_list": _global["get_active_list_name"](),
        "selected_lists": names,
        "company_count": len(companies),
        "security_count": len(rows),
        "generated_at_utc": _global["datetime"].now(_global["timezone"].utc).isoformat(),
        "csv_path": str(out),
        "csv_fields": csv_fields,
        "radar_overlap_exclusions": [
            {
                "selected_list": selected,
                "excluded_if_also_in": exclusion,
            }
            for selected, exclusions in RADAR_OVERLAP_EXCLUSIONS.items()
            if selected in names
            for exclusion in exclusions
            if exclusion in exclusion_lists
        ],
        "excluded_security_count": len(excluded_rows),
        "overlap_exclusion_scope": "company",
        "excluded_company_count": len({
            tuple(sorted(company_keys(row)))
            for row in excluded_rows
            if company_keys(row)
        }),
        "excluded_securities": excluded_rows,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已导出公告雷达 CSV: {out}")
    print(f"已写入来源 manifest: {manifest}")
    print(f"列表: {', '.join(names)}")
    print(f"证券数: {len(rows)}")
    if excluded_rows:
        excluded_company_labels = sorted({
            row.get("company_name_cn") or row.get("company_name_en") or row.get("ticker")
            for row in excluded_rows
        })
        labels = ", ".join(
            f"{row.get('company_name_cn') or row.get('company_name_en') or row.get('ticker')} {row.get('ticker')}"
            for row in excluded_rows
        )
        print(f"已按公司排除与 {', '.join(exclusion_lists)} 重合的公司: {', '.join(excluded_company_labels)}")
        print(f"已排除证券: {labels}")


def main() -> None:
    parser = argparse.ArgumentParser(description="投研日历公司列表工作流：实时读取配置的全局公司列表")
    parser.add_argument("--company-list", "--watchlist", action="append", dest="company_lists",
                        help="指定公司列表，可重复传入或逗号分隔；默认使用全局 active_list")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="查看全局配置指纹、默认列表、所有列表数量和校验状态")
    status_parser.add_argument("--company-list", "--watchlist", action="append", dest="command_company_lists",
                               help="指定公司列表，可重复传入或逗号分隔")

    snapshot_parser = subparsers.add_parser("snapshot", help="查看指定列表快照")
    snapshot_parser.add_argument("--company-list", "--watchlist", action="append", dest="command_company_lists",
                                 help="指定公司列表，可重复传入或逗号分隔")
    snapshot_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    export_parser = subparsers.add_parser("export-radar", help="从全局列表导出公告雷达 CSV 和 manifest")
    export_parser.add_argument("--company-list", "--watchlist", action="append", dest="command_company_lists",
                               help="指定公司列表，可重复传入或逗号分隔")
    export_parser.add_argument("--out", default=str(DEFAULT_RADAR_CSV), help="CSV 输出路径")
    export_parser.add_argument("--manifest", default=str(DEFAULT_RADAR_MANIFEST), help="manifest 输出路径")

    validate_parser = subparsers.add_parser("validate", help="校验所选全局列表")
    validate_parser.add_argument("--company-list", "--watchlist", action="append", dest="command_company_lists",
                                 help="指定公司列表，可重复传入或逗号分隔")

    audit_parser = subparsers.add_parser("audit-sources", help="审计所选列表的IR源覆盖缺口")
    audit_parser.add_argument("--company-list", "--watchlist", action="append", dest="command_company_lists",
                              help="指定公司列表，可重复传入或逗号分隔")
    audit_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    audit_parser.add_argument("--write", action="store_true", help="写入 data/source_gap_audit/")
    audit_parser.add_argument("--out", help="指定JSON报告输出路径")

    args = parser.parse_args()
    names = selected_names((args.company_lists or []) + (getattr(args, "command_company_lists", None) or []))

    if args.command == "status":
        print_status(names)
    elif args.command == "snapshot":
        for idx, name in enumerate(names):
            if idx:
                print()
            print_snapshot(name, args.json)
    elif args.command == "export-radar":
        export_radar(names, Path(args.out), Path(args.manifest))
    elif args.command == "validate":
        issues_by_name = validate_names(names)
        has_issues = False
        for name, issues in issues_by_name.items():
            if issues:
                has_issues = True
                print(f"{name}:")
                for issue in issues:
                    print(f"- {issue}")
            else:
                print(f"{name}: ok")
        if has_issues:
            raise SystemExit(1)
    elif args.command == "audit-sources":
        report = audit_lists(names)
        if args.write or args.out:
            path = write_report(report, Path(args.out) if args.out else None)
            print(f"已写入源覆盖审计报告: {path}")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_text_report(report)


if __name__ == "__main__":
    main()
