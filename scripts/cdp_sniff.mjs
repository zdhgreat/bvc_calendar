#!/usr/bin/env node
// One-off: sniff XHR/Fetch requests on a page via CDP Network domain.
// Usage: node scripts/cdp_sniff.mjs <url> [waitMs]
import { setTimeout as sleep } from "node:timers/promises";

const endpoint = process.env.CHROME_CDP_URL || "http://127.0.0.1:9222";
const url = process.argv[2];
const waitMs = Number(process.argv[3] || "15000");

async function jsonFetch(u, options = {}) {
  const r = await fetch(u, options);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${u}`);
  return r.json();
}

const target = await jsonFetch(`${endpoint}/json/new?${encodeURIComponent(url)}`, { method: "PUT" }).catch(() =>
  jsonFetch(`${endpoint}/json/new?${encodeURIComponent(url)}`)
);
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res, { once: true });
  ws.addEventListener("error", rej, { once: true });
});

let nextId = 1;
const pending = new Map();
const requests = new Map(); // requestId -> {url, method, headers, postData, type}
const responses = []; // {url, status, headers, body}

function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

ws.addEventListener("message", async (event) => {
  const msg = JSON.parse(event.data.toString());
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result || {});
    return;
  }
  if (msg.method === "Network.requestWillBeSent") {
    const { requestId, request, type } = msg.params;
    requests.set(requestId, {
      url: request.url,
      method: request.method,
      headers: request.headers,
      postData: request.postData || null,
      type: type || "",
    });
  }
  if (msg.method === "Network.responseReceived") {
    const { requestId, response, type } = msg.params;
    const req = requests.get(requestId);
    if (!req) return;
    if (process.env.SNIFF_ALL !== "1" && type !== "XHR" && type !== "Fetch") return;
    responses.push({ requestId, type, url: req.url, method: req.method, reqHeaders: req.headers, postData: req.postData, status: response.status });
  }
  if (msg.method === "Network.webSocketFrameReceived" || msg.method === "Network.webSocketFrameSent") {
    const { response } = msg.params;
    const payload = response?.payloadData || "";
    if (payload.includes("calendar") || payload.includes("economic") || payload.includes("CPI") || payload.length > 500) {
      responses.push({ type: msg.method === "Network.webSocketFrameSent" ? "WS_SENT" : "WS_RECV", url: "", status: 0, body: payload.slice(0, 3000) });
    }
  }
  if (msg.method === "Network.loadingFinished") {
    const { requestId } = msg.params;
    const idx = responses.findIndex((r) => r.requestId === requestId && r.body === undefined);
    if (idx >= 0) {
      try {
        const body = await send("Network.getResponseBody", { requestId });
        responses[idx].body = body.body ? body.body.slice(0, 3000) : "";
      } catch {
        responses[idx].body = "(unavailable)";
      }
    }
  }
});

await send("Network.enable");
await send("Page.enable");
await send("Page.navigate", { url });
await sleep(waitMs);

// pull bodies for any stragglers
for (const r of responses) {
  if (r.body === undefined && r.requestId) {
    try {
      const b = await send("Network.getResponseBody", { requestId: r.requestId });
      r.body = b.body ? b.body.slice(0, 3000) : "";
    } catch {
      r.body = "(unavailable)";
    }
  }
}

console.log(JSON.stringify(responses, null, 2));
ws.close();
await fetch(`${endpoint}/json/close/${target.id}`).catch(() => {});
process.exit(0);
