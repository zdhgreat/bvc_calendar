# financial-calendar

独立的金融日历项目。每天从中外源(akshare + finnhub)抓取宏观/财报/公司事件/IPO,
存入本地 PostgreSQL。**只提供只读数据接口(JSON API),没有页面。**

消费方(bbs-go 那边的 agent)拉接口、自己渲染、自己发帖。本项目对 bbs 完全无感知。

## 架构

```
financial-calendar (本服务, 纯数据接口)
      │  GET /api/feed   GET /api/event/{kind}/{id}
      ▼
 agent (部署在 bvc 的 Mac)
      │  读事件 → 生成 标题 + Markdown + 分类 + tags
      │  调用 portal-push skill (curl → bbs-go)
      ▼
   bbs-go  (每个事件一个可评论的帖子)
```

bbs-go 建话题**没有幂等键**,所以 agent 用每条事件的 `(kind, source_id)` 自行去重,
避免重复发帖——这正是接口暴露稳定 `source_id` 的原因。

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

## 启动数据接口

```bash
uvicorn app.main:app --port 8088
# 接口: http://localhost:8088/api/feed
```

## 目录

```
app/
  main.py             FastAPI 入口(纯 API,无 UI)
  db.py               PG 连接
  schema.sql          表结构 (5 张事件/metadata + calendar_topic_map)
  crawler/            akshare + finnhub + IR + runner
  routers/calendar.py 数据接口 (/api/feed, /api/event)
scripts/
  init_db.py          建 schema
  run_daily.py        crontab 入口
tests/
  test_runner.py      self-check (数据接口字段映射 + where 构造)
```

## 数据接口

| 端点 | 说明 |
|---|---|
| `GET /api/feed` | 事件流(按 kind 分组返回)。支持增量与日期范围过滤,见下。 |
| `GET /api/event/{kind}/{event_id}` | 单事件 JSON。`kind`: economic / earnings / corporate / ipo |
| `GET /health` | 存活检查 |

`/api/feed` 查询参数(均可选):

- `since` — ISO-8601 时间戳,只返回 `fetched_at >= since` 的行。消费方记下上次拉取时间
  传入即可做增量同步。注意 `fetched_at` 每次重新 UPSERT 都会刷新,所以仅被"触碰"过的
  行也会再次出现;用 `(kind, source_id)` 自行去重。
- `kind` — 只取某一类,省略则返回全部四类。
- `date_from` / `date_to` — `YYYY-MM-DD`,按事件日期(含端点)过滤。

每条事件都带稳定标识 `id` + `source_id`(消费方去重用)和 `fetched_at`(增量用),
外加该类别的具体字段,**以及一个 `post` 对象**。

### `post` 发帖视图

每条事件附带 `post`,是 FC 渲染好的、可直接喂给 portal-push skill 的发帖载荷:

```json
"post": {
  "title":      "[股东大会] 2026年第二次临时股东大会 2026-08-22",
  "content_md": "## 公司事件\n\n- 公司: 腾讯 00700\n- 日期: 2026-08-22\n...",
  "category":   "公司事件",
  "tags":       ["投研日历"]
}
```

agent 拿到后:把 `content_md` 写进临时 `.md` 文件,用 `title` / `category` / `tags`
作为 portal-push 的 `BBS_TOPIC_TITLE` / `BBS_CATEGORY_NAME` / `BBS_TAGS_JSON` 即可发帖。
agent 无需懂每种事件的格式,格式化由 FC 单一来源负责(`app/render.py`)。

`category` 是**分类名**(portal-push 按唯一精确匹配解析),默认 `宏观经济 / 财报 / 公司事件 / IPO`,
可用环境变量覆盖成 bbs-go 里真实存在的分类名(见下)。

### 鉴权与渲染配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FEED_TOKEN` | (空=开放) | 设了之后 `/api/feed`、`/api/event` 需带 `?token=` 或 `X-Feed-Token`。跨网络调用建议设 |
| `POST_CATEGORIES` | 见下 | JSON 映射 `{kind: 分类名}`,覆盖默认分类名以匹配 bbs-go 实际分类 |
| `POST_DEFAULT_TAG` | `投研日历` | `post.tags` 的默认标签 |

`POST_CATEGORIES` 默认:`{"economic":"宏观经济","earnings":"财报","corporate":"公司事件","ipo":"IPO"}`

### agent 侧职责(不在本项目内)

agent:定时拉 `/api/feed?since=...` → 对没发过帖的事件用 `(kind, source_id)` 去重 →
生成标题/Markdown/分类/tags → 用 portal-push skill 发到 bbs-go。

> 备注:`schema.sql` 里的 `calendar_topic_map` 表是旧 push 模型遗留,现已无代码读写,
> 保留仅为不破坏已部署库。`source_id` 即消费方去重键。
