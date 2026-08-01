#!/usr/bin/env python3
"""
投研日历 - A股定期报告预约披露时间监控
数据源: 巨潮资讯网 (cninfo.com.cn) via AkShare

功能:
- 获取A股公司定期报告预约披露时间
- 跟踪预约时间变更（初次/二次/三次变更）
- 将事件写入投研日历 events.json

用法:
    python3 report_disclosure_monitor.py --test          # 测试模式，只查池内公司
    python3 report_disclosure_monitor.py --period 2025年报  # 指定报告期
    python3 report_disclosure_monitor.py --all           # 全量采集（所有A股）
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Set

from app.crawler.company_lists import CompanyListError, allowed_a_share_codes, list_summary, resolve_list_names

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CALENDAR_FILE = DATA_DIR / "events.json"  # legacy; shim writes to PG
STATE_FILE = DATA_DIR / "disclosure_state.json"

# 当前可用报告期（按需更新）
AVAILABLE_PERIODS = ["2025年报", "2026一季", "2025半年报", "2025三季"]


def load_state() -> Dict:
    """加载上次采集状态"""
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"last_run": None, "records": {}}


def save_state(state: Dict):
    """保存采集状态"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_events() -> Dict:
    from app.crawler._event_store import load_events as _shim_load
    return _shim_load()


def save_events(data: Dict):
    from app.crawler._event_store import save_events as _shim_save
    _shim_save(data)


def fetch_disclosure_data(market: str = "沪深京", period: str = "2025年报") -> Optional[List[Dict]]:
    """从AkShare获取预约披露数据"""
    try:
        import akshare as ak
        logger.info(f"获取 {market} {period} 预约披露数据...")
        df = ak.stock_report_disclosure(market=market, period=period)
        logger.info(f"获取到 {len(df)} 条记录")
        return df.to_dict('records')
    except Exception as e:
        logger.error(f"获取数据失败: {e}")
        return None


def filter_watchlist(records: List[Dict], codes: Set[str]) -> List[Dict]:
    """筛选池内公司"""
    results = []
    for r in records:
        code = str(r.get('股票代码', '')).zfill(6)
        if code in codes:
            results.append(r)
    return results


def detect_changes(new_records: List[Dict], old_state: Dict) -> List[Dict]:
    """检测预约时间变更"""
    changes = []
    old_records = old_state.get("records", {})

    for r in new_records:
        code = str(r.get('股票代码', '')).zfill(6)
        name = r.get('股票简称', '')
        first = str(r.get('首次预约', ''))
        change1 = str(r.get('初次变更', ''))
        change2 = str(r.get('二次变更', ''))
        change3 = str(r.get('三次变更', ''))
        actual = str(r.get('实际披露', ''))

        key = code
        old = old_records.get(key, {})

        # 检测变更
        if old:
            old_changes = [old.get('初次变更', ''), old.get('二次变更', ''), old.get('三次变更', '')]
            new_changes = [change1, change2, change3]
            for i, (o, n) in enumerate(zip(old_changes, new_changes)):
                if o != n and n and n != 'NaT':
                    changes.append({
                        "code": code,
                        "name": name,
                        "change_type": f"第{i+1}次变更",
                        "old_date": o if o and o != 'NaT' else '无',
                        "new_date": n,
                        "first预约": first,
                    })

    return changes


def make_event_id(code: str, period: str) -> str:
    """生成事件ID"""
    return f"disclosure_{code}_{period}"


def add_disclosure_events(records: List[Dict], period: str, dry_run: bool = False) -> int:
    """将预约披露事件写入日历"""
    events_data = load_events()
    existing_ids = {e.get('id') for e in events_data.get('events', [])}
    added = 0

    for r in records:
        code = str(r.get('股票代码', '')).zfill(6)
        name = r.get('股票简称', '').strip()
        first = str(r.get('首次预约', ''))
        actual = str(r.get('实际披露', ''))

        # 使用实际披露日期，若无则用首次预约
        date_str = actual if actual and actual != 'NaT' else first
        if not date_str or date_str == 'NaT':
            continue

        event_id = make_event_id(code, period)
        if event_id in existing_ids:
            continue

        event = {
            "id": event_id,
            "date": date_str,
            "title": f"{name}({code}) {period}报告披露",
            "type": "财报",
            "note": f"首次预约: {first}",
            "source": "巨潮资讯-预约披露",
            "source_url": "http://www.cninfo.com.cn/new/commonUrl?url=data/yypl",
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        if not dry_run:
            events_data['events'].append(event)
            existing_ids.add(event_id)
        added += 1
        logger.info(f"  新增: {name}({code}) {period} -> {date_str}")

    if not dry_run and added > 0:
        save_events(events_data)
        logger.info(f"写入 {added} 条事件到 events.json")

    return added


def run(market: str = "沪深京", period: str = "2025年报",
        watchlist_only: bool = True, dry_run: bool = False,
        company_lists: List[str] = None) -> Dict:
    """主采集流程"""
    selected_lists = resolve_list_names(company_lists)
    logger.info(f"=== 定期报告预约披露监控 ===")
    logger.info(f"市场: {market}, 报告期: {period}, 仅池内: {watchlist_only}, 公司列表: {', '.join(selected_lists)}")

    # 加载状态
    state = load_state()

    # 获取数据
    records = fetch_disclosure_data(market, period)
    if not records:
        return {"success": False, "error": "获取数据失败"}

    # 筛选
    if watchlist_only:
        codes = set(allowed_a_share_codes(selected_lists).keys())
        records = filter_watchlist(records, codes)
        logger.info(f"池内公司: {len(records)} 条")

    # 检测变更
    changes = detect_changes(records, state)
    if changes:
        logger.info(f"检测到 {len(changes)} 项变更:")
        for c in changes:
            logger.info(f"  {c['name']}: {c['change_type']} {c['old_date']} -> {c['new_date']}")

    # 添加事件到日历
    added = add_disclosure_events(records, period, dry_run=dry_run)

    # 更新状态
    if not dry_run:
        new_state = {
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "last_period": period,
            "records": {}
        }
        for r in records:
            code = str(r.get('股票代码', '')).zfill(6)
            new_state["records"][code] = {
                "首次预约": str(r.get('首次预约', '')),
                "初次变更": str(r.get('初次变更', '')),
                "二次变更": str(r.get('二次变更', '')),
                "三次变更": str(r.get('三次变更', '')),
                "实际披露": str(r.get('实际披露', '')),
            }
        save_state(new_state)

    result = {
        "success": True,
        "period": period,
        "total_records": len(records),
        "events_added": added,
        "changes_detected": len(changes),
        "changes": changes,
    }
    logger.info(f"完成: 新增 {added} 事件, 检测到 {len(changes)} 项变更")
    return result


def main():
    parser = argparse.ArgumentParser(description='A股定期报告预约披露时间监控')
    parser.add_argument('--test', action='store_true', help='测试模式（仅池内公司，dry run）')
    parser.add_argument('--all', action='store_true', help='全量采集（所有A股）')
    parser.add_argument('--period', default='2025年报', help='报告期，如 2025年报')
    parser.add_argument('--market', default='沪深京', help='市场范围')
    parser.add_argument('--company-list', action='append', dest='company_lists',
                        help='指定公司列表，可重复传入或用逗号分隔；默认使用 officecodex 全局公司列表的 active_list')
    parser.add_argument('--list-company-lists', action='store_true', help='列出可用公司列表')
    args = parser.parse_args()

    if args.list_company_lists:
        print("公司列表:")
        for item in list_summary():
            print(f"  {item['id']:16s} | {item['name']} | IR源 {item['ir_sources']} | A股 {item['a_shares']}")
        return

    if args.test:
        logger.info("=== 测试模式 ===")
        try:
            result = run(
                market=args.market,
                period=args.period,
                watchlist_only=True,
                dry_run=True,
                company_lists=args.company_lists,
            )
        except CompanyListError as e:
            logger.error(str(e))
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        try:
            run(
                market=args.market,
                period=args.period,
                watchlist_only=not args.all,
                dry_run=False,
                company_lists=args.company_lists,
            )
        except CompanyListError as e:
            logger.error(str(e))


if __name__ == "__main__":
    main()
