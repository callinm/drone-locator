/**
 * Headless Chrome verification for the live Drone Locator UX fixes.
 * Usage: node tests/verify-live.mjs [prefix] [baseUrl]
 */
import { createWriteStream, mkdirSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "screenshots");
mkdirSync(outDir, { recursive: true });
const prefix = process.argv[2] || "after";
const base = (process.argv[3] || "https://wheresthepelican.com/").replace(/\/?$/, "/");
const port = 9223;
const userDir = `/tmp/dl-chrome-${Date.now()}`;

const chrome = spawn("google-chrome", [
  "--headless=new",
  "--no-sandbox",
  "--disable-gpu",
  "--disable-dev-shm-usage",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${userDir}`,
  "--hide-scrollbars",
  "about:blank",
], { stdio: ["ignore", "pipe", "pipe"] });

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function waitPort(tries = 40) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return r.json();
    } catch {}
    await sleep(150);
  }
  throw new Error("chrome debug port did not come up");
}

let id = 0;
async function openCdp() {
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
  return { ws, send };
}

async function attachPage(send, url, { width, height, mobile }) {
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  const s = (method, params = {}) => send(method, params, sessionId);
  await s("Page.enable");
  await s("Runtime.enable");
  await s("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile, screenWidth: width, screenHeight: height,
  });
  if (mobile) {
    await s("Network.setUserAgentOverride", {
      userAgent: "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    });
    await s("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
  }
  await s("Page.navigate", { url });
  await s("Page.loadEventFired").catch(() => {});
  // Wait for load via evaluate + timeout
  const start = Date.now();
  while (Date.now() - start < 12000) {
    const ready = await evalExpr(s, "document.readyState");
    if (ready === "complete") break;
    await sleep(200);
  }
  await sleep(1500);
  return { s, targetId };
}

async function evalExpr(s, expr) {
  const res = await s("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) {
    throw new Error(res.exceptionDetails.text || "eval failed");
  }
  return res.result?.value;
}

async function screenshot(s, name) {
  const { data } = await s("Page.captureScreenshot", { format: "png", fromSurface: true });
  const path = join(outDir, `${prefix}-${name}.png`);
  writeFileSync(path, Buffer.from(data, "base64"));
  return path;
}

const MEASURE_JS = `(() => {
  const measure = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      sel, w: Math.round(r.width), h: Math.round(r.height),
      top: Math.round(r.top), left: Math.round(r.left),
      inView: r.width > 0 && r.height > 0 && r.top >= 0 && r.top < window.innerHeight,
      fontSize: style.fontSize, hidden: !!el.hidden, text: (el.textContent || "").trim().slice(0, 160),
    };
  };
  const lock = document.querySelector(".feature-lock");
  const lockRect = lock && !lock.hidden ? lock.getBoundingClientRect() : null;
  const tourSteps = [];
  return {
    title: document.title,
    footer: (document.getElementById("siteFooter")?.textContent || "").trim(),
    vw: window.innerWidth, vh: window.innerHeight,
    lockHidden: !lock || lock.hidden,
    lockTop: lockRect ? Math.round(lockRect.top) : null,
    lockBottom: lockRect ? Math.round(lockRect.bottom) : null,
    hasRequest: !!document.querySelector("[data-lock-request]"),
    requestHidden: document.querySelector("[data-lock-request]")?.hidden ?? null,
    hasUseOrtho: !!document.getElementById("btnUseOrtho"),
    hasDraw: !!document.getElementById("btnDrawQuery"),
    tourCount: (document.querySelector(".tutorial-count")?.textContent || "").trim(),
    tourTitle: (document.getElementById("tutorialTitle")?.textContent || "").trim(),
    lockTitle: measure("[data-lock-title]"),
    lockBody: measure("[data-lock-body]"),
    lockCard: measure("#locatorLockCard"),
    tabFind: measure("#tabFind"),
    tabPlanner: measure("#tabPlanner"),
    tabSaved: measure("#tabSaved"),
    login: measure("#authSignedOut a[href*='login']"),
    signup: measure("#authSignedOut a[href*='signup']"),
    showTut: measure("#btnShowTutorial"),
    zoomIn: measure(".leaflet-control-zoom-in"),
    zoomOut: measure(".leaflet-control-zoom-out"),
    requestBtn: measure("[data-lock-request]"),
    drawBtn: measure("#btnDrawQuery"),
    noteFont: measure("#mosaicStatus") || measure(".note"),
    legendZoom: measure(".legend-zoom"),
    selectedTab: document.querySelector(".page-tabs button[aria-selected='true']")?.id || null,
  };
})()`;

const report = { prefix, base, measurements: {}, tutorial: [], checks: [] };

try {
  const { ws, send } = await openCdp();

  // Phone home
  const phone = await attachPage(send, base, { width: 412, height: 915, mobile: true });
  // Dismiss the auto-tour so the lock card is visible in the main after shot.
  await evalExpr(phone.s, `document.querySelector("[data-tutorial-skip], [data-tutorial-close]")?.click()`);
  await sleep(400);
  report.measurements.phone = await evalExpr(phone.s, MEASURE_JS.replace(".leaflet-control-zoom-in", "#findMap .leaflet-control-zoom-in").replace(".leaflet-control-zoom-out", "#findMap .leaflet-control-zoom-out"));
  await screenshot(phone.s, "phone");
  // Re-open the tour for the step walkthrough
  await evalExpr(phone.s, `document.getElementById("btnShowTutorial")?.click()`);
  await sleep(500);
  // full page-ish: scroll lock into view already at top — also capture after dismissing nothing
  // Walk locked tutorial
  const startSteps = [];
  startSteps.push(report.measurements.phone.tourCount + " " + report.measurements.phone.tourTitle);
  for (let i = 0; i < 8; i++) {
    const next = await evalExpr(phone.s, `!!document.querySelector("[data-tutorial-next]")`);
    if (!next) break;
    const finish = await evalExpr(phone.s, `(document.querySelector("[data-tutorial-next]")?.textContent || "").trim()`);
    if (/finish/i.test(finish) && i > 0) {
      await evalExpr(phone.s, `document.querySelector("[data-tutorial-next]").click()`);
      await sleep(500);
      const after = await evalExpr(phone.s, MEASURE_JS);
      startSteps.push("FINISHED selectedTab=" + after.selectedTab + " tourOpen=" + !!after.tourTitle);
      report.measurements.phoneAfterTour = after;
      break;
    }
    await evalExpr(phone.s, `document.querySelector("[data-tutorial-next]").click()`);
    await sleep(400);
    const snap = await evalExpr(phone.s, MEASURE_JS);
    startSteps.push(snap.tourCount + " " + snap.tourTitle);
    await screenshot(phone.s, `phone-tour-${(snap.tourCount || "x").replace(/\\s+/g, "-").toLowerCase()}`);
  }
  report.tutorial = startSteps;

  // Draw-mode toggle via script click (button is under the lock visually)
  const drawToggle = await evalExpr(phone.s, `(() => {
    const btn = document.getElementById("btnDrawQuery");
    if (!btn) return { ok: false, reason: "no button" };
    btn.click();
    const pressed = btn.getAttribute("aria-pressed");
    const map = document.getElementById("findMap");
    return { ok: true, pressed, mapClass: map ? map.className : null };
  })()`);
  report.drawToggle = drawToggle;
  // turn it back off
  await evalExpr(phone.s, `document.getElementById("btnDrawQuery")?.getAttribute("aria-pressed") === "true" && document.getElementById("btnDrawQuery").click()`);

  // Desktop
  const desk = await attachPage(send, base, { width: 1440, height: 900, mobile: false });
  await evalExpr(desk.s, `document.querySelector("[data-tutorial-skip], [data-tutorial-close]")?.click()`);
  await sleep(400);
  report.measurements.desktop = await evalExpr(desk.s, MEASURE_JS.replace(".leaflet-control-zoom-in", "#findMap .leaflet-control-zoom-in").replace(".leaflet-control-zoom-out", "#findMap .leaflet-control-zoom-out"));
  await screenshot(desk.s, "desktop");

  // Login pages
  const loginD = await attachPage(send, base + "login/", { width: 1440, height: 900, mobile: false });
  report.measurements.loginDesktop = await evalExpr(loginD.s, `(() => {
    const btn = document.querySelector(".auth-card button, .auth-card .btn, button[type=submit]");
    const r = btn ? btn.getBoundingClientRect() : null;
    return { title: document.title, btnH: r ? Math.round(r.height) : null, btnW: r ? Math.round(r.width) : null };
  })()`);
  await screenshot(loginD.s, "login-desktop");

  const loginP = await attachPage(send, base + "login/", { width: 412, height: 915, mobile: true });
  report.measurements.loginPhone = await evalExpr(loginP.s, `(() => {
    const btn = document.querySelector(".auth-card button, button[type=submit]");
    const r = btn ? btn.getBoundingClientRect() : null;
    return { title: document.title, btnH: r ? Math.round(r.height) : null, btnW: r ? Math.round(r.width) : null };
  })()`);
  await screenshot(loginP.s, "login-phone");

  // Wrong password through the form
  await evalExpr(loginP.s, `(() => {
    const u = document.querySelector("input[name=username]");
    const p = document.querySelector("input[name=password]");
    if (u) { u.value = "nobody-uxfix"; u.dispatchEvent(new Event("input", { bubbles: true })); }
    if (p) { p.value = "definitely-wrong-password-xxx"; p.dispatchEvent(new Event("input", { bubbles: true })); }
    document.getElementById("loginForm").requestSubmit();
    return true;
  })()`);
  await sleep(1500);
  report.wrongPassword = await evalExpr(loginP.s, `(() => {
    const err = document.getElementById("error");
    return { hidden: err ? err.hidden : null, text: err ? err.textContent.trim() : null, stillOnLogin: /login/i.test(location.pathname) };
  })()`);
  await screenshot(loginP.s, "login-wrong-password");

  writeFileSync(join(outDir, `${prefix}-verify.json`), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  chrome.kill("SIGTERM");
  process.exit(0);
} catch (err) {
  console.error("VERIFY FAILED", err);
  try { chrome.kill("SIGKILL"); } catch {}
  process.exit(1);
}
