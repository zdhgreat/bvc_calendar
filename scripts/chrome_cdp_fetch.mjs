#!/usr/bin/env node
import { setTimeout as sleep } from "node:timers/promises";

const endpoint = process.env.CHROME_CDP_URL || "http://127.0.0.1:9222";
const waitMs = Number(process.env.CHROME_CDP_WAIT_MS || "12000");
const urls = process.argv.slice(2);

if (!urls.length) {
  console.error("Usage: chrome_cdp_fetch.mjs <url> [url...]");
  process.exit(2);
}

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}: ${url}`);
  }
  return response.json();
}

function cdpCall(ws, id, method, params = {}) {
  return new Promise((resolve, reject) => {
    const onMessage = (event) => {
      const message = JSON.parse(event.data.toString());
      if (message.id !== id) {
        return;
      }
      ws.removeEventListener("message", onMessage);
      if (message.error) {
        reject(new Error(`${method}: ${message.error.message}`));
      } else {
        resolve(message.result || {});
      }
    };
    ws.addEventListener("message", onMessage);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function readPage(url) {
  let target;
  const encoded = encodeURIComponent(url);
  try {
    target = await jsonFetch(`${endpoint}/json/new?${encoded}`, { method: "PUT" });
  } catch {
    target = await jsonFetch(`${endpoint}/json/new?${encoded}`);
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });

  let nextId = 1;
  const call = (method, params = {}) => cdpCall(ws, nextId++, method, params);
  try {
    await call("Runtime.enable");
    await call("Page.enable");
    await call("Page.navigate", { url });
    await sleep(waitMs);
    const title = await call("Runtime.evaluate", {
      expression: "document.title",
      returnByValue: true,
    });
    const text = await call("Runtime.evaluate", {
      expression: "document.body ? document.body.innerText : ''",
      returnByValue: true,
    });
    const html = await call("Runtime.evaluate", {
      expression: "document.documentElement ? document.documentElement.outerHTML : ''",
      returnByValue: true,
    });
    return {
      url,
      title: title.result?.value || "",
      text: text.result?.value || "",
      html: html.result?.value || "",
    };
  } finally {
    ws.close();
    await fetch(`${endpoint}/json/close/${target.id}`).catch(() => {});
  }
}

const results = [];
for (const url of urls) {
  try {
    results.push(await readPage(url));
  } catch (error) {
    results.push({ url, error: error.message });
  }
}

console.log(JSON.stringify(results, null, 2));
