/**
 * Live verification for the auth-module and [hidden] regressions.
 * Reads logins from /workspace/uxreview-logins/logins.txt and never prints secrets.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "screenshots");
mkdirSync(outDir, { recursive: true });
const base = "https://wheresthepelican.com/";
const port = 9225;

function parseLogins(text) {
  const accounts = {};
  let current = null;
  for (const raw of text.splitlines ? text.splitlines() : text.split(/\r?\n/)) {
    const line = raw.trim();
    const um = line.match(/^Username:\s*(.+)$/);
    if (um) {
      current = um[1].trim();
      accounts[current] = { username: current };
      continue;
    }
    const pm = line.match(/^Password:\s*(.+)$/);
    if (pm && current) accounts[current].password = pm[1].trim();
  }
  return accounts;
}

const accounts = parseLogins(readFileSync("/workspace/uxreview-logins/logins.txt", "utf8"));
const unlocked = accounts["uxreview-dl-20261114"];
const locked = accounts["uxreview-pt-20261114"];
if (!unlocked?.password || !locked?.password) {
  console.error("Missing expected reviewer accounts in the logins file.");
  process.exit(1);
}

const chrome = spawn("google-chrome", [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=/tmp/dl-fix2-${Date.now()}`,
  "--hide-scrollbars",
  "about:blank",
], { stdio: ["ignore", "pipe", "pipe"] });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitPort() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return r.json();
    } catch {}
    await sleep(150);
  }
  throw new Error("chrome debug port did not come up");
}

let id = 0;
const ver = await waitPort();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener("open", res); ws.addEventListener("error", rej); });
const pending = new Map();
const events = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  }
  if (msg.method) events.push(msg);
});

function send(method, params = {}, sessionId) {
  const mid = ++id;
  const payload = { id: mid, method, params };
  if (sessionId) payload.sessionId = sessionId;
  ws.send(JSON.stringify(payload));
  return new Promise((resolve, reject) => pending.set(mid, { resolve, reject }));
}

async function newPage({ width, height, mobile }) {
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  const s = (method, params = {}) => send(method, params, sessionId);
  await s("Page.enable");
  await s("Runtime.enable");
  await s("Network.enable");
  await s("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile,
    screenWidth: width, screenHeight: height,
  });
  if (mobile) {
    await s("Network.setUserAgentOverride", {
      userAgent: "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    });
    await s("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
  }
  return { s, targetId, sessionId };
}

async function evalExpr(s, expr) {
  const res = await s("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) {
    const d = res.exceptionDetails;
    const desc = d.exception?.description || d.text || JSON.stringify(d);
    throw new Error("eval: " + String(desc).slice(0, 400));
  }
  return res.result?.value;
}

async function waitReady(s, ms = 12000) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    try {
      const ready = await evalExpr(s, "document.readyState");
      if (ready === "complete") {
        await sleep(800);
        return;
      }
    } catch {}
    await sleep(200);
  }
}

async function screenshot(s, name) {
  const { data } = await s("Page.captureScreenshot", { format: "png", fromSurface: true });
  const path = join(outDir, `fix2-${name}.png`);
  writeFileSync(path, Buffer.from(data, "base64"));
  return path;
}

async function clearSession(s) {
  await s("Network.clearBrowserCookies");
  await evalExpr(s, `(() => { try { localStorage.clear(); sessionStorage.clear(); } catch {} return true; })()`);
}

async function go(s, url) {
  await s("Page.navigate", { url });
  await waitReady(s);
}

const SNAP = `(() => {
  const measure = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { missing: true, sel };
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      sel,
      hiddenAttr: !!el.hidden,
      display: style.display,
      w: Math.round(r.width),
      h: Math.round(r.height),
      inView: r.width > 0 && r.height > 0 && style.display !== "none" && r.bottom > 0 && r.top < window.innerHeight,
      text: (el.textContent || "").trim().slice(0, 180),
    };
  };
  const lock = document.querySelector(".feature-lock");
  return {
    path: location.pathname,
    username: (document.getElementById("authUsername")?.textContent || "").trim(),
    signedIn: !document.getElementById("authSignedIn")?.hidden,
    lockHidden: !lock || lock.hidden || getComputedStyle(lock).display === "none",
    lockTitle: measure("[data-lock-title]"),
    lockBody: measure("[data-lock-body]"),
    request: measure("[data-lock-request]"),
    admin: measure("#authAdminLink"),
    tourCount: (document.querySelector(".tutorial-count")?.textContent || "").trim(),
    tourTitle: (document.getElementById("tutorialTitle")?.textContent || "").trim(),
    selectedTab: document.querySelector(".page-tabs button[aria-selected='true']")?.id || null,
    findError: (document.getElementById("findError")?.hidden ? "" : (document.getElementById("findError")?.textContent || "")).trim(),
    findProgress: (document.getElementById("findProgress")?.textContent || "").trim(),
    mosaicStatus: (document.getElementById("mosaicStatus")?.textContent || "").trim(),
    refStatus: (document.getElementById("refStatus")?.textContent || "").trim(),
    drawPressed: document.getElementById("btnDrawQuery")?.getAttribute("aria-pressed") || null,
    mapClass: document.getElementById("findMap")?.className || "",
  };
})()`;

function collectNetwork(sessionId) {
  const posts = [];
  for (const ev of events) {
    if (ev.sessionId !== sessionId) continue;
    if (ev.method === "Network.responseReceived") {
      const { response, type } = ev.params;
      const url = response.url || "";
      if (/\/mosaics/.test(url) && ev.params.hasExtraInfo !== undefined) {
        posts.push({ url: url.replace(/https?:\/\/[^/]+/, ""), status: response.status, mime: response.mimeType, type });
      }
    }
  }
  return posts;
}

async function loginOnPage(s, username, password) {
  await s("Network.clearBrowserCookies");
  try { await s("Network.setCacheDisabled", { cacheDisabled: true }); } catch {}
  await evalExpr(s, `(() => { try { localStorage.clear(); sessionStorage.clear(); } catch {} return true; })()`);
  await go(s, base + "login/?_=" + Date.now());
  const already = await evalExpr(s, "(document.getElementById('authUsername')?.textContent || '').trim()");
  if (already) {
    await evalExpr(s, `document.getElementById("authLogout")?.click()`);
    await sleep(600);
    await go(s, base + "login/?_=" + Date.now());
  }
  const startForm = Date.now();
  while (Date.now() - startForm < 8000) {
    const hasForm = await evalExpr(s, "!!document.getElementById('loginForm')");
    if (hasForm) break;
    await sleep(200);
  }
  await evalExpr(s, `(() => {
    const u = document.querySelector("input[name=username]");
    const p = document.querySelector("input[name=password]");
    const form = document.getElementById("loginForm");
    if (!u || !p || !form) throw new Error("login form missing at " + location.href);
    u.value = ${JSON.stringify(username)};
    p.value = ${JSON.stringify(password)};
    u.dispatchEvent(new Event("input", { bubbles: true }));
    p.dispatchEvent(new Event("input", { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`);
  const start = Date.now();
  while (Date.now() - start < 15000) {
    const path = await evalExpr(s, "location.pathname + location.search");
    const user = await evalExpr(s, "(document.getElementById('authUsername')?.textContent || '').trim()");
    const err = await evalExpr(s, `(() => { const e = document.getElementById("error"); return e && !e.hidden ? e.textContent.trim() : ""; })()`);
    if (user) return { ok: true, user, path };
    if (err) return { ok: false, error: err, path };
    await sleep(300);
  }
  return { ok: false, error: "login timed out", path: await evalExpr(s, "location.pathname") };
}

async function dismissTour(s) {
  await evalExpr(s, `document.querySelector("[data-tutorial-skip], [data-tutorial-close]")?.click()`);
  await sleep(400);
}

async function walkTour(s, namePrefix, maxSteps = 10) {
  const steps = [];
  await evalExpr(s, `document.getElementById("btnShowTutorial")?.click()`);
  await sleep(500);
  for (let i = 0; i < maxSteps; i++) {
    const snap = await evalExpr(s, SNAP);
    const label = `${snap.tourCount || "?"} ${snap.tourTitle || ""}`.trim();
    steps.push({ count: snap.tourCount, title: snap.tourTitle, tab: snap.selectedTab });
    if (snap.tourCount) await screenshot(s, `${namePrefix}-${String(i + 1).padStart(2, "0")}`);
    const cta = await evalExpr(s, `(document.querySelector("[data-tutorial-next]")?.textContent || "").trim()`);
    if (!cta) break;
    await evalExpr(s, `document.querySelector("[data-tutorial-next]")?.click()`);
    await sleep(500);
    if (/finish/i.test(cta)) {
      const after = await evalExpr(s, SNAP);
      steps.push({ finished: true, tab: after.selectedTab, tourOpen: !!after.tourTitle });
      break;
    }
  }
  return steps;
}

const report = { ok: true, checks: [] };
function check(name, pass, detail) {
  report.checks.push({ name, pass: !!pass, detail: detail || "" });
  if (!pass) report.ok = false;
  console.log(`${pass ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`);
}

try {
  // 1) Signed out
  for (const [label, size] of [["phone", { width: 412, height: 915, mobile: true }], ["desktop", { width: 1280, height: 800, mobile: false }]]) {
    const page = await newPage(size);
    await clearSession(page.s);
    await go(page.s, base);
    await dismissTour(page.s);
    const snap = await evalExpr(page.s, SNAP);
    await screenshot(page.s, `signedout-${label}`);
    check(`signed-out ${label} locked`, snap.lockHidden === false && /locked/i.test(snap.lockTitle.text || ""), snap.lockTitle.text);
    check(`signed-out ${label} request hidden`, !snap.request.inView && (snap.request.hiddenAttr || snap.request.display === "none" || snap.request.missing));
    check(`signed-out ${label} not signed in`, snap.signedIn === false);
  }

  // 2) Locked non-admin
  for (const [label, size] of [["phone", { width: 412, height: 915, mobile: true }], ["desktop", { width: 1280, height: 800, mobile: false }]]) {
    console.log("section locked", label);
    const page = await newPage(size);
    await clearSession(page.s);
    const login = await loginOnPage(page.s, locked.username, locked.password);
    check(`locked ${label} login`, login.ok && login.user === locked.username, login.ok ? login.user : login.error);
    await sleep(800);
    await dismissTour(page.s);
    const snap = await evalExpr(page.s, SNAP);
    await screenshot(page.s, `locked-${label}`);
    check(`locked ${label} lock visible`, snap.lockHidden === false, snap.lockTitle.text);
    check(`locked ${label} Request access visible`, snap.request.inView === true && !snap.request.hiddenAttr && /request access/i.test(snap.request.text || ""), JSON.stringify({ hiddenAttr: snap.request.hiddenAttr, inView: snap.request.inView, w: snap.request.w, h: snap.request.h }));
    check(`locked ${label} Admin hidden`, !snap.admin.inView && (snap.admin.hiddenAttr || snap.admin.display === "none" || snap.admin.w === 0), JSON.stringify(snap.admin));
  }

  // 3) Unlocked user — phone first for demo, draw, tour; then desktop demo
  const unlockedPhone = await newPage({ width: 412, height: 915, mobile: true });
  await clearSession(unlockedPhone.s);
  const loginU = await loginOnPage(unlockedPhone.s, unlocked.username, unlocked.password);
  check("unlocked phone login", loginU.ok && loginU.user === unlocked.username, loginU.ok ? loginU.user : loginU.error);
  await sleep(1000);
  await dismissTour(unlockedPhone.s);
  let snap = await evalExpr(unlockedPhone.s, SNAP);
  await screenshot(unlockedPhone.s, "unlocked-phone");
  check("unlocked phone has no lock", snap.lockHidden === true);
  check("unlocked phone Admin hidden", !snap.admin.inView && (snap.admin.hiddenAttr || snap.admin.display === "none" || snap.admin.w === 0), JSON.stringify(snap.admin));
  check("unlocked phone shows username", snap.username === unlocked.username, snap.username);

  // Load demo and watch /mosaics
  const beforeNet = events.length;
  await evalExpr(unlockedPhone.s, `document.getElementById("btnFindDemo")?.click()`);
  let demoOk = false;
  let lastSnap = snap;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    lastSnap = await evalExpr(unlockedPhone.s, SNAP);
    const posts = collectNetwork(unlockedPhone.sessionId);
    const okPost = posts.some((p) => p.status >= 200 && p.status < 300);
    const signedOutErr = /sign in to use/i.test(lastSnap.findError || "");
    if (okPost && !signedOutErr && (/found \d+ match/i.test(lastSnap.findProgress) || /on the server/i.test(lastSnap.mosaicStatus) || /demo is ready/i.test(lastSnap.refStatus))) {
      demoOk = true;
      break;
    }
    if (signedOutErr) break;
  }
  const posts = collectNetwork(unlockedPhone.sessionId);
  await screenshot(unlockedPhone.s, "unlocked-demo-phone");
  const mosaicsOk = posts.filter((p) => /\/mosaics/.test(p.url) && p.status >= 200 && p.status < 300);
  check("unlocked Load demo reached server", mosaicsOk.length > 0, mosaicsOk.map((p) => `${p.status} ${p.url}`).join(", ") || "no /mosaics response");
  check("unlocked Load demo no sign-in error", !/sign in to use/i.test(lastSnap.findError || ""), lastSnap.findError || lastSnap.findProgress);
  check("unlocked Load demo completed", demoOk, lastSnap.findProgress || lastSnap.mosaicStatus);

  // Finger Draw box on the loaded map
  await evalExpr(unlockedPhone.s, `document.getElementById("btnDrawQuery")?.click()`);
  await sleep(300);
  const drawState = await evalExpr(unlockedPhone.s, `(() => {
    const btn = document.getElementById("btnDrawQuery");
    const map = document.getElementById("findMap");
    const r = map.getBoundingClientRect();
    return { pressed: btn.getAttribute("aria-pressed"), mapClass: map.className, left: r.left, top: r.top, w: r.width, h: r.height };
  })()`);
  check("Draw box mode on", drawState.pressed === "true" && /is-draw-mode/.test(drawState.mapClass), drawState.pressed + " " + drawState.mapClass);
  const x0 = Math.round(drawState.left + drawState.w * 0.35);
  const y0 = Math.round(drawState.top + drawState.h * 0.35);
  const x1 = Math.round(drawState.left + drawState.w * 0.65);
  const y1 = Math.round(drawState.top + drawState.h * 0.65);
  await unlockedPhone.s("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: x0, y: y0, id: 0, force: 1, radiusX: 8, radiusY: 8 }] });
  await unlockedPhone.s("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x1, y: y1, id: 0, force: 1, radiusX: 8, radiusY: 8 }] });
  await unlockedPhone.s("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await sleep(500);
  const afterDraw = await evalExpr(unlockedPhone.s, SNAP);
  await screenshot(unlockedPhone.s, "unlocked-draw-phone");
  const boxDrawn = /area marked/i.test(afterDraw.refStatus || "") || /query region/i.test(afterDraw.refStatus || "");
  check("finger Draw box marked an area", boxDrawn, afterDraw.refStatus);

  // Unlocked tutorial
  const tour = await walkTour(unlockedPhone.s, "unlocked-tour-phone");
  const titles = tour.filter((s) => s.title).map((s) => `${s.count} ${s.title}`);
  check("unlocked tutorial starts at 1 of 7", /^1\s+of\s+7$/i.test(tour[0]?.count || "") && /welcome/i.test(tour[0]?.title || ""), titles.join(" → "));
  check("unlocked tutorial includes Navigate here", titles.some((t) => /navigate here/i.test(t)), titles.join(" → "));
  check("unlocked tutorial does not use locked 4-step copy", !titles.some((t) => /admin has to turn it on/i.test(t) || /once it is turned on/i.test(t)), titles.join(" → "));
  const counts = tour.filter((s) => s.count).map((s) => s.count);
  const jumped = counts.includes("1 of 4") || counts.some((c, i, a) => i && a[i - 1].startsWith("1 ") && c.startsWith("3 "));
  check("unlocked tutorial step numbers stay sequential", !jumped && counts.length >= 5, counts.join(" → "));
  const finish = tour.find((s) => s.finished);
  check("unlocked tutorial finishes on Drone Locator tab", finish && finish.tab === "tabFind", finish ? finish.tab : "no finish");

  // Unlocked desktop: login, no lock, admin hidden, load demo
  const unlockedDesk = await newPage({ width: 1280, height: 800, mobile: false });
  await clearSession(unlockedDesk.s);
  const loginD = await loginOnPage(unlockedDesk.s, unlocked.username, unlocked.password);
  check("unlocked desktop login", loginD.ok && loginD.user === unlocked.username, loginD.ok ? loginD.user : loginD.error);
  await sleep(800);
  await dismissTour(unlockedDesk.s);
  snap = await evalExpr(unlockedDesk.s, SNAP);
  await screenshot(unlockedDesk.s, "unlocked-desktop");
  check("unlocked desktop has no lock", snap.lockHidden === true);
  check("unlocked desktop Admin hidden", !snap.admin.inView && (snap.admin.hiddenAttr || snap.admin.display === "none" || snap.admin.w === 0), JSON.stringify(snap.admin));
  await evalExpr(unlockedDesk.s, `document.getElementById("btnFindDemo")?.click()`);
  let deskDemo = false;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    snap = await evalExpr(unlockedDesk.s, SNAP);
    const p = collectNetwork(unlockedDesk.sessionId).filter((x) => x.status >= 200 && x.status < 300);
    if (p.length && !/sign in to use/i.test(snap.findError || "") && (/found \d+ match/i.test(snap.findProgress) || /on the server/i.test(snap.mosaicStatus))) {
      deskDemo = true;
      break;
    }
    if (/sign in to use/i.test(snap.findError || "")) break;
  }
  await screenshot(unlockedDesk.s, "unlocked-demo-desktop");
  const deskPosts = collectNetwork(unlockedDesk.sessionId).filter((p) => /\/mosaics/.test(p.url) && p.status >= 200 && p.status < 300);
  check("unlocked desktop Load demo reached server", deskPosts.length > 0, deskPosts.map((p) => `${p.status} ${p.url}`).join(", ") || "no /mosaics");
  check("unlocked desktop Load demo completed", deskDemo && !/sign in to use/i.test(snap.findError || ""), snap.findProgress || snap.findError);

  writeFileSync(join(outDir, "fix2-verify.json"), JSON.stringify(report, null, 2));
  console.log(report.ok ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
  chrome.kill("SIGTERM");
  process.exit(report.ok ? 0 : 1);
} catch (err) {
  console.error("VERIFY FAILED", err && err.stack ? err.stack : err);
  try { chrome.kill("SIGKILL"); } catch {}
  process.exit(1);
}
