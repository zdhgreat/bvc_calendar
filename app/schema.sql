-- financial-calendar schema
-- 5 张数据表 + 1 张 bbs-go 集成映射表

-- 宏观经济数据 (CPI/PMI/非农/利率决议等)
CREATE TABLE IF NOT EXISTS economic_events (
  id BIGSERIAL PRIMARY KEY,
  event_time TIMESTAMPTZ NOT NULL,
  country VARCHAR(32) NOT NULL,
  indicator VARCHAR(128) NOT NULL,
  importance SMALLINT,
  actual VARCHAR(32),
  forecast VARCHAR(32),
  previous VARCHAR(32),
  source VARCHAR(32) NOT NULL,
  source_id VARCHAR(128),
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_economic_time ON economic_events(event_time);
CREATE INDEX IF NOT EXISTS idx_economic_country ON economic_events(country, event_time);

-- 公司财报发布日
CREATE TABLE IF NOT EXISTS earnings_calendar (
  id BIGSERIAL PRIMARY KEY,
  report_date DATE NOT NULL,
  ticker VARCHAR(32) NOT NULL,
  exchange VARCHAR(16),
  company VARCHAR(128),
  period VARCHAR(16),
  source VARCHAR(32) NOT NULL,
  source_id VARCHAR(128),
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings_calendar(report_date);
CREATE INDEX IF NOT EXISTS idx_earnings_ticker ON earnings_calendar(ticker);

-- 公司事件 (除权除息/股东大会/限售解禁/回购)
CREATE TABLE IF NOT EXISTS corporate_events (
  id BIGSERIAL PRIMARY KEY,
  event_date DATE NOT NULL,
  ticker VARCHAR(32) NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  description TEXT,
  source VARCHAR(32) NOT NULL,
  source_id VARCHAR(128),
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_corp_event_date ON corporate_events(event_date);
CREATE INDEX IF NOT EXISTS idx_corp_event_type ON corporate_events(event_type, event_date);

-- IPO / 新股
CREATE TABLE IF NOT EXISTS ipo_calendar (
  id BIGSERIAL PRIMARY KEY,
  event_date DATE NOT NULL,
  ticker VARCHAR(32),
  company VARCHAR(128),
  exchange VARCHAR(16),
  price_low NUMERIC(12,2),
  price_high NUMERIC(12,2),
  status VARCHAR(16),
  source VARCHAR(32) NOT NULL,
  source_id VARCHAR(128),
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_ipo_date ON ipo_calendar(event_date);

-- 事件词典 / 解读 / 关联标的
-- term 在源表里不规范化 (akshare 中文 / finnhub 英文),所以本表允许同义多行
CREATE TABLE IF NOT EXISTS calendar_metadata (
  id BIGSERIAL PRIMARY KEY,
  term VARCHAR(128) NOT NULL,
  kind VARCHAR(16) NOT NULL CHECK (kind IN ('macro','earnings','corp_event')),
  definition TEXT,
  interpretation TEXT,
  related TEXT[],
  publisher VARCHAR(64),
  frequency VARCHAR(32),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (term, kind)
);
CREATE INDEX IF NOT EXISTS idx_cal_meta_term ON calendar_metadata(term, kind);

-- ──────────────────────────────────────────────────────────────────
-- calendar_topic_map: 事件 ↔ bbs-go 帖子 映射
-- kind: economic / earnings / corporate / ipo
-- source_id: 对应源表的 source_id 字段
-- topic_id: bbs-go 中的 Topic.ID
CREATE TABLE IF NOT EXISTS calendar_topic_map (
  id BIGSERIAL PRIMARY KEY,
  kind VARCHAR(16) NOT NULL,
  source_id VARCHAR(128) NOT NULL,
  topic_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (kind, source_id)
);
CREATE INDEX IF NOT EXISTS idx_ctm_topic ON calendar_topic_map(topic_id);

-- ──────────────────────────────────────────────────────────────────
-- corporate_events 扩展列:支持 IR 爬虫(canonical event dict)的更宽字段。
-- 幂等:ADD COLUMN IF NOT EXISTS 在 PG 9.6+ 支持;DROP NOT NULL 在已可空时也无害。
ALTER TABLE corporate_events ALTER COLUMN ticker DROP NOT NULL;
ALTER TABLE corporate_events ADD COLUMN IF NOT EXISTS title VARCHAR(255);
ALTER TABLE corporate_events ADD COLUMN IF NOT EXISTS company VARCHAR(128);
ALTER TABLE corporate_events ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ;
ALTER TABLE corporate_events ADD COLUMN IF NOT EXISTS timezone VARCHAR(64);
ALTER TABLE corporate_events ADD COLUMN IF NOT EXISTS source_url TEXT;
CREATE INDEX IF NOT EXISTS idx_corp_event_source ON corporate_events(source, event_date);

-- ──────────────────────────────────────────────────────────────────
-- calendar_topic_map: topic_id 从 BIGINT 改 VARCHAR(128),因为 bbs-go
-- 的 topic id 是不透明字符串(非自增整数)。USING 子句把旧整数转文本。
ALTER TABLE calendar_topic_map
  ALTER COLUMN topic_id TYPE VARCHAR(128) USING topic_id::text;
