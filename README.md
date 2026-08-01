# financial-calendar

独立的金融日历项目。每天从中外源(akshare + finnhub)抓取宏观/财报/公司事件/IPO,
存入本地 PostgreSQL。提供日历 UI 和 API。

后续会与 [bbs-go](../bbs-go) 集成: 每个事件在 bbs-go 生成一个可评论的帖子。

## 数据源

| 源 | 覆盖 | 表 |
|---|---|---|
| akshare (百度财经) | 宏观数据 (CN+全球) | `economic_events` |
| akshare (东方财富) | A股财报/分红/股东大会/解禁 | `earnings_calendar` / `corporate_events` |
| akshare (东方财富) | A股 IPO 申报 | `ipo_calendar` |
| finnhub | 全球宏观 + 美股财报 + IPO | 同上 |

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env,填 POSTGRES_* 和 FINNHUB_API_KEY
```

## 初始化数据库

```bash
python scripts/init_db.py
```

## 每日抓取

```bash
python scripts/run_daily.py                  # today
python scripts/run_daily.py --date 2026-07-28
```

## 启动 Web

```bash
uvicorn app.main:app --reload --port 8088
# 浏览器打开 http://localhost:8088
```

## 目录

```
app/
  main.py             FastAPI 入口
  db.py               PG 连接
  schema.sql          表结构 (5 张事件/metadata + calendar_topic_map)
  bbs_integration.py  bbs-go 集成 (TODO: 等 bbs-go 接口)
  crawler/            akshare + finnhub + runner
  routers/calendar.py 日历页 + JSON API
  templates/          Jinja2 模板
scripts/
  init_db.py          建 schema
  run_daily.py        crontab 入口
tests/
  test_runner.py      self-check
```

## bbs-go 集成 (待实现)

爬虫每次写入新事件后,调 `app.bbs_integration.ensure_topic(kind, source_id)`,
通过 HTTP 让 bbs-go 创建一个对应的 Topic,把 `(kind, source_id, topic_id)` 写到
`calendar_topic_map`。用户在 bbs-go 那边评论。

当前 `ensure_topic` 是占位实现,只记 map 不调 bbs-go。
