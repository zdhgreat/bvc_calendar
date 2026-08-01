#!/bin/bash
# 投研日历采集版统一入口(已并入 financial-calendar 单体仓库)
# 1. 校验全局公司列表并审计IR源覆盖
# 2. 运行IR与股息分红采集
# 3. 检查逐源访问/解析状态
# 4. 写入本地正式运行状态

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$APP_DIR"

# ponytail: project uses python-dotenv (.env at repo root); source it if present
# so non-Python helpers (e.g. node CDP) see the env. Python modules load .env
# themselves via app.db.
if [ -f "$APP_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$APP_DIR/.env"
    set +a
fi

PYTHON_BIN="${RESEARCH_CALENDAR_PYTHON:-python3}"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
SELECTED_WATCHLISTS="${WATCHLISTS:-${COMPANY_LISTS:-}}"
IR_STATUS=0
DIVIDEND_STATUS=0

if [ -n "$SELECTED_WATCHLISTS" ]; then
    "$PYTHON_BIN" -m app.crawler.company_list_workflow status --company-list "$SELECTED_WATCHLISTS" 2>&1 || IR_STATUS=$?
    "$PYTHON_BIN" -m app.crawler.company_list_workflow audit-sources --company-list "$SELECTED_WATCHLISTS" --out "data/source_gap_audit/latest.json" 2>&1 || IR_STATUS=$?
    "$PYTHON_BIN" -m app.crawler.ir_crawler --all --company-list "$SELECTED_WATCHLISTS" 2>&1 || IR_STATUS=$?
    "$PYTHON_BIN" -m app.crawler.dividend_calendar --company-list "$SELECTED_WATCHLISTS" 2>&1 || DIVIDEND_STATUS=$?
else
    "$PYTHON_BIN" -m app.crawler.company_list_workflow status 2>&1 || IR_STATUS=$?
    "$PYTHON_BIN" -m app.crawler.company_list_workflow audit-sources --out "data/source_gap_audit/latest.json" 2>&1 || IR_STATUS=$?
    "$PYTHON_BIN" -m app.crawler.ir_crawler --all 2>&1 || IR_STATUS=$?
    "$PYTHON_BIN" -m app.crawler.dividend_calendar 2>&1 || DIVIDEND_STATUS=$?
fi

FAILURE_FILE="data/crawl_failures.json"
NOTIFY_FILE="data/needs_notification.txt"
COVERAGE_FILE="data/ir_coverage_state.json"
WORKFLOW_STATE_FILE="data/daily_workflow_state.json"
SOURCE_AUDIT_FILE="data/source_gap_audit/latest.json"

"$PYTHON_BIN" -m app.crawler.ir_crawler --check-coverage 2>&1
"$PYTHON_BIN" -c "
import json
from pathlib import Path
p = Path('$COVERAGE_FILE')
if not p.exists():
    raise SystemExit(1)
state = json.loads(p.read_text(encoding='utf-8'))
summary = state.get('summary', {})
raise SystemExit(1 if summary.get('partial', 0) or summary.get('failed', 0) else 0)
" || IR_STATUS=1

if [ -f "$FAILURE_FILE" ]; then
    echo "ir-failure: $(date '+%Y-%m-%d %H:%M')" > "$NOTIFY_FILE"
    echo "---" >> "$NOTIFY_FILE"
    "$PYTHON_BIN" -c "
import json
with open('$FAILURE_FILE') as handle:
    data = json.load(handle)
for item in data.get('failures', []):
    print(f'[{item[\"company\"]}] {item[\"error\"][:120]}')
" >> "$NOTIFY_FILE" 2>/dev/null
else
    rm -f "$NOTIFY_FILE"
fi

if [ "$DIVIDEND_STATUS" -ne 0 ]; then
    {
        echo "dividend-failure: $(date '+%Y-%m-%d %H:%M')"
        echo "---"
        if [ -f "data/dividend_state.json" ]; then
            "$PYTHON_BIN" -c "
import json
with open('data/dividend_state.json', encoding='utf-8') as handle:
    data = json.load(handle)
for item in data.get('source_failures', []):
    print(f'[源失败] {item.get(\"source\", \"\")}: {item.get(\"error\", \"\")[:160]}')
for item in data.get('failures', []):
    print(f'[{item.get(\"company\", \"\")}] {item.get(\"market\", \"\")} {item.get(\"ticker\", \"\")}: {item.get(\"error\", \"\")[:120]}')
if not data.get('source_failures') and not data.get('failures'):
    print('股息分红采集返回失败，但状态文件没有记录具体失败项。')
"
        else
            echo "股息分红采集失败，未生成状态文件。"
        fi
    } >> "$NOTIFY_FILE"
fi

"$PYTHON_BIN" -c "
import json
from datetime import datetime
from pathlib import Path

coverage = {}
coverage_path = Path('$COVERAGE_FILE')
if coverage_path.exists():
    coverage = json.loads(coverage_path.read_text(encoding='utf-8')).get('summary', {})

source_gap_summary = {'high': 0, 'medium': 0, 'low': 0}
audit_path = Path('$SOURCE_AUDIT_FILE')
if audit_path.exists():
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    for item in audit.get('lists', []):
        for key in source_gap_summary:
            source_gap_summary[key] += int(item.get('risk_summary', {}).get(key, 0))

execution_success = not any([$IR_STATUS, $DIVIDEND_STATUS])
coverage_complete = source_gap_summary.get('high', 0) == 0
state = {
    'last_run': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    'workflow_variant': 'collector',
    'company_lists': '$SELECTED_WATCHLISTS',
    'ir_status': $IR_STATUS,
    'dividend_status': $DIVIDEND_STATUS,
    'coverage_summary': coverage,
    'company_source_gap_summary': source_gap_summary,
    'coverage_complete': coverage_complete,
    'success': execution_success,
    'status': 'success' if execution_success and coverage_complete else (
        'partial_coverage' if execution_success else 'incomplete'
    ),
}
Path('$WORKFLOW_STATE_FILE').write_text(
    json.dumps(state, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
print(json.dumps(state, ensure_ascii=False))
"

if [ "$IR_STATUS" -ne 0 ] || [ "$DIVIDEND_STATUS" -ne 0 ]; then
    exit 1
fi
