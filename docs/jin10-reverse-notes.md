# 金十数据日历接口逆向笔记（2026-08-20）

## 结论

金十财经日历（rili.jin10.com）的日历数据**不走 HTTP API**，而是走 **WebSocket**（页面里的 `window.Jin10FlashInstance` 全局实例，由 `https://cdn.jin10.com/plugins/flash/v2/index.js` 注入）。WS 帧是自定义混淆 + base64，直接解协议成本高。

**推荐取数方式**：CDP 驱动 Chrome 打开 `https://rili.jin10.com/`，等 socket 连接完成后直接在页面里 `await window.Jin10FlashInstance.getCalendar*(date)`，返回**解密后的纯净 JSON**。

## 已排除的路线

| 路线 | 状态 |
|---|---|
| 官方 API open.jin10.com | 付费，不采用 |
| `cdn-rili.jin10.com/web_data/...`（旧静态 JSON） | 域名已注销，全球 NXDOMAIN |
| `jad-api.jin10.com/internal/*`（现行内部 HTTP API） | 裸 curl 全 502（需浏览器环境）；浏览器内仅见 `/aw/single*` 广告位调用，日历数据不经过它 |
| 纯 requests 抓 rili HTML | 只给 4KB SPA 壳；真实浏览器得到 SSR 完整表格（服务端按客户端特征区分） |
| 解析 SSR HTML 表格 | 可行但脆（class 名会变）；socket JSON 更优 |

## 取数接口（页面内 JS 调用）

```js
await window.Jin10FlashInstance.getCalendarData('2026-08-20')        // cj 宏观数据
await window.Jin10FlashInstance.getCalendarEvent('2026-08-20')       // 事件（央行讲话等）
await window.Jin10FlashInstance.getCalendarHoliday('2026-08-20')     // 假期
await window.Jin10FlashInstance.getCalendarFuturesData('2026-08-20') // qh 期货数据
await window.Jin10FlashInstance.getCalendarFuturesEvent(date)        // qh 期货事件
await window.Jin10FlashInstance.getCalendarUSStockData(date)         // us 美股（财报等）
await window.Jin10FlashInstance.getCalendarUSStockEvent(date)
await window.Jin10FlashInstance.getCalendarHKStockData(date)         // hk 港股
await window.Jin10FlashInstance.getCalendarHKStockEvent(date)
await window.Jin10FlashInstance.getAShareData(date)                  // A股
```

返回均为 `{date, list: [...]}`。

## 响应 schema（实测样例）

### getCalendarData（cj 宏观）→ 对齐 economic_events
```json
{
  "data_id": 1183961,            // 稳定唯一 id → source_id
  "indicator_name": "商品出口年率",
  "country": "日本",
  "star": 2,                     // 重要性 1-3（美股里见到 4，注意截断/映射）
  "pub_time": "2026-08-20 07:50",// 北京时间
  "pub_time_unix": 1787183400,
  "previous": "19.30", "consensus": "19.9", "actual": "23.2",
  "unit": "%", "time_period": "7月",
  "time_status": null            // 美股财报为 "盘前"/"盘后" 等
}
```

### getCalendarEvent（事件）
```json
{"id": 1185231, "event_time": "2026-08-20 00:00", "country": "新西兰",
 "star": 1, "category": "cj", "event_content": "新西兰联储高级经济官员...讲话。",
 "time_status": "待定"}
```

### getCalendarUSStockData（美股财报）
同 data 结构，`indicator_name` = "富途控股(FUTU.O)"，`country` = 交易所（纳斯达克），`measure` = "EPS"，`time_status` = "盘前"。

### getCalendarFuturesData（qh 期货）
同 data 结构，多 `qh_affect` / `qh_affect_text`（影响品种，如 "铝"）。

## 其他技术细节

- **登录门槛（2026-08-20 补充，关键）**：页面只在**已登录**时创建 `Jin10FlashInstance`
  （`styles.*.js` 里 `watch: hasGetUser → isLogin && connectSocket()` → `new Jin10Flash({loginType:"calendar",...})`）。
  未登录拿到的是 193KB 空壳页（无实例、无数据）；与 headless/UA/cookies-did 无关——实测唯一需要的是
  `.jin10.com` 域下的 `x-token` cookie。抓取脚本用 CDP `Network.setCookie` 注入 `JIN10_X_TOKEN` 环境变量即可，
  **无头 Chrome + 一次性 profile 完全可用**（无需常驻浏览器、无需拷贝 profile）。
  x-token 有效期约 30 天；过期症状 = 零行报错，重新登录金十换新 token 即可。
- URL 参数 `https://rili.jin10.com/?date=YYYY-MM-DD` 可让页面直接渲染指定日期（SSR 表格可用作 fallback 解析源）
- 路由参数形式 `/dates/<date>`（`$route.params.id`）
- 页面标签：财经数据 / 事件 / 假期 / 期货 / A股 / 港股 / 美股 / 基金 / 债券 / 重要
- 服务器（Mac mini）网络在 fake-ip 网关后，但 **浏览器内访问 jin10 正常**（2026-08-20 实测 jad-api 浏览器内 200）——CDP 方案天然绕过网关对裸 curl 的干扰
- 合规：公开页面、低频（每天 1-2 次），不触碰登录态与付费内容

## 复用设施

- CDP 抓取惯例：项目已有 `scripts/chrome_cdp_fetch.mjs` + `CHROME_CDP_URL`（默认 `http://127.0.0.1:9222`）
- 新增 `scripts/jin10_cdp_fetch.mjs`：打开页面 → 调 getter → 打印 JSON（Python 经 subprocess 调用）
