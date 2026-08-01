#!/usr/bin/env python3
"""
投研日历 - A股股东大会日期监控
数据源: 东方财富网 via AkShare

功能:
- 获取A股公司股东大会召开日期、股权登记日等
- 将事件写入投研日历 events.json

用法:
    python3 shareholder_meeting_monitor.py --test      # 测试模式
    python3 shareholder_meeting_monitor.py             # 正式采集
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
STATE_FILE = DATA_DIR / "meeting_state.json"


def load_state() -> Dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"last_run": None, "known_meetings": []}


def save_state(state: Dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_events() -> Dict:
    from app.crawler._event_store import load_events as _shim_load
    return _shim_load()


def save_events(data: Dict):
    from app.crawler._event_store import save_events as _shim_save
    _shim_save(data)


def fetch_meeting_data() -> Optional[List[Dict]]:
    """从AkShare获取股东大会数据"""
    try:
        import akshare as ak
        logger.info("获取股东大会数据...")
        df = ak.stock_gddh_em()
        logger.info(f"获取到 {len(df)} 条记录")
        return df.to_dict('records')
    except Exception as e:
        logger.error(f"获取数据失败: {e}")
        return None


def filter_watchlist(records: List[Dict], codes: Set[str]) -> List[Dict]:
    """筛选池内公司"""
    return [r for r in records if r.get('代码', '') in codes]


def make_event_id(code: str, meeting_name: str, date: str) -> str:
    """生成事件ID"""
    safe_name = meeting_name.replace(' ', '').replace('　', '')[:20]
    return f"meeting_{code}_{date}_{safe_name}"


def add_meeting_events(records: List[Dict], dry_run: bool = False) -> int:
    """将股东大会事件写入日历"""
    events_data = load_events()
    existing_ids = {e.get('id') for e in events_data.get('events', [])}
    added = 0

    for r in records:
        code = r.get('代码', '')
        name = r.get('简称', '').strip()
        meeting_name = r.get('股东大会名称', '').strip()
        start_date = str(r.get('召开开始日', ''))
        reg_date = str(r.get('股权登记日', ''))
        vote_start = str(r.get('网络投票时间-开始日', ''))
        vote_end = str(r.get('网络投票时间-结束日', ''))
        proposals = str(r.get('提案', ''))[:100]  # 截取前100字

        if not start_date or start_date == 'NaT' or start_date == 'nan':
            continue

        event_id = make_event_id(code, meeting_name, start_date)
        if event_id in existing_ids:
            continue

        # 构建备注
        note_parts = [f"股权登记日: {reg_date}"]
        if vote_start and vote_start != 'NaT' and vote_start != 'nan':
            note_parts.append(f"网络投票: {vote_start} ~ {vote_end}")
        if proposals and proposals != 'nan':
            note_parts.append(f"提案: {proposals}")

        event = {
            "id": event_id,
            "date": start_date,
            "title": f"{name}({code}) {meeting_name}",
            "type": "股东大会",
            "note": "; ".join(note_parts),
            "source": "东方财富-股东大会",
            "source_url": "https://data.eastmoney.com/gddh/",
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        if not dry_run:
            events_data['events'].append(event)
            existing_ids.add(event_id)
        added += 1
        logger.info(f"  新增: {name}({code}) {meeting_name} -> {start_date}")

    if not dry_run and added > 0:
        save_events(events_data)
        logger.info(f"写入 {added} 条事件到 events.json")

    return added


def run(watchlist_only: bool = True, dry_run: bool = False, company_lists: List[str] = None) -> Dict:
    """主采集流程"""
    selected_lists = resolve_list_names(company_lists)
    logger.info(f"=== 股东大会日期监控 ===")
    logger.info(f"公司列表: {', '.join(selected_lists)}")

    records = fetch_meeting_data()
    if not records:
        return {"success": False, "error": "获取数据失败"}

    if watchlist_only:
        codes = set(allowed_a_share_codes(selected_lists).keys())
        records = filter_watchlist(records, codes)
        logger.info(f"池内公司: {len(records)} 条")

    added = add_meeting_events(records, dry_run=dry_run)

    # 更新状态
    if not dry_run:
        state = {
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "known_meetings": [
                {
                    "code": r.get('代码', ''),
                    "name": r.get('简称', ''),
                    "meeting": r.get('股东大会名称', ''),
                    "date": str(r.get('召开开始日', '')),
                }
                for r in records
            ]
        }
        save_state(state)

    result = {
        "success": True,
        "total_records": len(records),
        "events_added": added,
    }
    logger.info(f"完成: 新增 {added} 事件")
    return result


def main():
    parser = argparse.ArgumentParser(description='A股股东大会日期监控')
    parser.add_argument('--test', action='store_true', help='测试模式（dry run）')
    parser.add_argument('--all', action='store_true', help='全量采集')
    parser.add_argument('--company-list', action='append', dest='company_lists',
                        help='指定公司列表，可重复传入或用逗号分隔；默认使用 officecodex 全局公司列表的 active_list')
    parser.add_argument('--list-company-lists', action='store_true', help='列出可用公司列表')
    args = parser.parse_args()

    if args.list_company_lists:
        print("公司列表:")
        for item in list_summary():
            print(f"  {item['id']:16s} | {item['name']} | IR源 {item['ir_sources']} | A股 {item['a_shares']}")
        return

    try:
        result = run(
            watchlist_only=not args.all,
            dry_run=args.test,
            company_lists=args.company_lists,
        )
    except CompanyListError as e:
        logger.error(str(e))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
