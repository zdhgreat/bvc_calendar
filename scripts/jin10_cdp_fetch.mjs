#!/usr/bin/env node
// Fetch jin10 (金十) calendar data via CDP + the page's Jin10FlashInstance (WebSocket).
// The calendar data travels over an obfuscated WebSocket, not HTTP — see docs/jin10-reverse-notes.md.
// Usage: node scripts/jin10_cdp_fetch.mjs <YYYY-MM-DD> [YYYY-MM-DD...]
// Env: CHROME_CDP_URL (default http://127.0.0.1:9222), JIN10_CDP_WAIT_MS (default 12000)
// Output: JSON to stdout: { "<date>": { cj_data: [...], cj_event: [...], qh_data: [...], us_data: [...], hk_data: [...] } }
import { setTimeout as sleep } from "node:timers/promises";

const endpoint = process.env.CHROME_CDP_URL || "http://127.0.0.1:9222";
const waitMs = Number(process.env.JIN10_CDP_WAIT_MS || "15000");
const dates = process.argv.slice(2);

if (!dates.length) {
  console.error("Usage: jin10_cdp_fetch.mjs <YYYY-MM-DD> [YYYY-MM-DD...]");
  process.exit(2);
}

const PAGE_URL = "https://rili.jin10.com/";
const GETTERS = {
  cj_data: "getCalendarData",
  cj_event: "getCalendarEvent",
  qh_data: "getCalendarFuturesData",
  us_data: "getCalendarUSStockData",
  hk_data: "getCalendarHKStockData",
};

async function jsonFetch(u, o = {}) {
  const r = await fetch(u, o);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${u}`);
  return r.json();
}

const target = await jsonFetch(`${endpoint}/json/new?${encodeURIComponent(PAGE_URL)}`, { method: "PUT" }).catch(() =>
  jsonFetch(`${endpoint}/json/new?${encodeURIComponent(PAGE_URL)}`)
);
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res, { once: true });
  ws.addEventListener("error", rej, { once: true });
});

let nextId = 1;
const pending = new Map();
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data.toString());
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result || {});
  }
});
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

async function evaluate(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.text || "page evaluation failed");
  }
  return r.result?.value;
}

try {
  await send("Runtime.enable");
  // ponytail: rili only opens the flash socket for logged-in users (isLogin gate in
  // styles.*.js). Login = x-token cookie on .jin10.com; inject it from env so a
  // throwaway headless profile works. Token expires ~30d — renewal = update the env var.
  if (process.env.JIN10_X_TOKEN) {
    await send("Network.enable");
    await send("Network.setCookie", {
      name: "x-token", value: process.env.JIN10_X_TOKEN, domain: ".jin10.com", path: "/",
      expires: Math.floor(Date.now() / 1000) + 30 * 86400,
    });
  }
  await send("Page.navigate", { url: PAGE_URL });
  await sleep(waitMs);

  const ready = await evaluate("!!window.Jin10FlashInstance");
  if (!ready) {
    throw new Error("Jin10FlashInstance not available — JIN10_X_TOKEN missing/expired, or page changed");
  }

  const out = {};
  for (const date of dates) {
    out[date] = {};
    for (const [key, method] of Object.entries(GETTERS)) {
      const expr = `window.Jin10FlashInstance.${method}('${date}').then(r => (r && r.list) || []).catch(() => null)`;
      const list = await evaluate(expr);
      if (list === null || list === undefined) {
        out[date][key] = { error: `${method} rejected` };
      } else {
        out[date][key] = list;
      }
    }
  }
  console.log(JSON.stringify(out));
} finally {
  // ponytail: Node 24 on Windows crashes with a libuv assertion if we close the
  // WebSocket and process.exit in the same tick — skip ws.close(), the tab close
  // and process exit will tear it down. Python side parses stdout regardless of
  // exit code for the same reason.
  await fetch(`${endpoint}/json/close/${target.id}`).catch(() => {});
  setTimeout(() => process.exit(0), 500).unref();
}
