import { spawn } from "node:child_process";
const port = 9224;
const chrome = spawn("google-chrome", [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  `--remote-debugging-port=${port}`, `--user-data-dir=/tmp/dl-touch-${Date.now()}`,
  "about:blank",
], { stdio: ["ignore", "pipe", "pipe"] });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitPort() {
  for (let i = 0; i < 40; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) return r.json(); } catch {}
    await sleep(150);
  }
  throw new Error("no chrome");
}
let id = 0;
const ver = await waitPort();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener("open", res); ws.addEventListener("error", rej); });
const pending = new Map();
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  }
});
const send = (method, params = {}, sessionId) => {
  const mid = ++id;
  const payload = { id: mid, method, params };
  if (sessionId) payload.sessionId = sessionId;
  ws.send(JSON.stringify(payload));
  return new Promise((resolve, reject) => pending.set(mid, { resolve, reject }));
};
const { targetId } = await send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
const s = (method, params = {}) => send(method, params, sessionId);
await s("Page.enable");
await s("Runtime.enable");
await s("Emulation.setDeviceMetricsOverride", { width: 412, height: 915, deviceScaleFactor: 1, mobile: true, screenWidth: 412, screenHeight: 915 });
await s("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
await s("Page.navigate", { url: "http://127.0.0.1:8765/touch-draw-fixture.html" });
await sleep(800);
const ready = await s("Runtime.evaluate", { expression: "document.title", returnByValue: true });
if (ready.result?.value !== "Draw box touch fixture") throw new Error("fixture not loaded: " + JSON.stringify(ready));

// Dispatch a finger drag on #stage
const box = await s("Runtime.evaluate", { expression: "JSON.stringify(document.getElementById('stage').getBoundingClientRect())", returnByValue: true });
const r = JSON.parse(box.result.value);
const x0 = r.left + 80, y0 = r.top + 80, x1 = r.left + 260, y1 = r.top + 240;
await s("Input.dispatchTouchEvent", {
  type: "touchStart",
  touchPoints: [{ x: x0, y: y0, id: 0, force: 1, radiusX: 10, radiusY: 10 }],
});
await s("Input.dispatchTouchEvent", {
  type: "touchMove",
  touchPoints: [{ x: x1, y: y1, id: 0, force: 1, radiusX: 10, radiusY: 10 }],
});
await s("Input.dispatchTouchEvent", {
  type: "touchEnd",
  touchPoints: [],
});
await sleep(300);
const result = await s("Runtime.evaluate", { expression: "JSON.stringify(window.__draw)", returnByValue: true });
const shot = await s("Page.captureScreenshot", { format: "png", fromSurface: true });
import { writeFileSync } from "node:fs";
writeFileSync("/workspace/drone-locator-fixes/screenshots/after-touch-draw-phone.png", Buffer.from(shot.data, "base64"));
console.log("touch_result", result.result.value);
chrome.kill("SIGTERM");
const parsed = JSON.parse(result.result.value);
if (!parsed.box || parsed.box.w < 50 || parsed.box.h < 50) {
  console.error("FAIL box too small", parsed);
  process.exit(1);
}
console.log("PASS touch box", parsed.box);
process.exit(0);
