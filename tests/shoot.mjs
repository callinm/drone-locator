// Headless Chrome screenshots + basic measurements for Drone Locator.
import { writeFileSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "screenshots");
mkdirSync(outDir, { recursive: true });

const prefix = process.argv[2] || "after";
const base = process.argv[3] || "https://wheresthepelican.com/";
const chrome = process.env.CHROME || "google-chrome";

function runChrome(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(chrome, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (d) => { stderr += d.toString(); });
    child.on("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`chrome exit ${code}: ${stderr.slice(-800)}`));
    });
  });
}

const evalJs = `
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const tourNext = () => {
    const btn = document.querySelector("[data-tutorial-next]");
    if (btn) btn.click();
  };
  const skipTour = () => {
    const btn = document.querySelector("[data-tutorial-skip], [data-tutorial-close]");
    if (btn) btn.click();
  };
  const measure = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      sel,
      w: Math.round(r.width),
      h: Math.round(r.height),
      top: Math.round(r.top),
      left: Math.round(r.left),
      visible: r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < (window.innerHeight || 0),
      fontSize: style.fontSize,
      hidden: el.hidden,
    };
  };
  const text = (sel) => (document.querySelector(sel)?.textContent || "").trim();
  const lock = document.querySelector(".feature-lock");
  const lockRect = lock && !lock.hidden ? lock.getBoundingClientRect() : null;
  const notes = Array.from(document.querySelectorAll(".note, .legend-zoom, [data-lock-body]"))
    .slice(0, 8)
    .map((el) => ({ text: (el.textContent || "").trim().slice(0, 120), fontSize: getComputedStyle(el).fontSize }));
  return {
    title: document.title,
    path: location.pathname,
    vw: window.innerWidth,
    vh: window.innerHeight,
    lockHidden: !lock || lock.hidden,
    lockTop: lockRect ? Math.round(lockRect.top) : null,
    lockHeight: lockRect ? Math.round(lockRect.height) : null,
    hasRequest: !!document.querySelector("[data-lock-request]"),
    requestHidden: document.querySelector("[data-lock-request]")?.hidden ?? null,
    hasUseOrtho: !!document.getElementById("btnUseOrtho"),
    hasDraw: !!document.getElementById("btnDrawQuery"),
    tourCount: text(".tutorial-count"),
    tourTitle: text("#tutorialTitle"),
    lockTitle: text("[data-lock-title]"),
    lockBody: text("[data-lock-body]"),
    tabFind: measure("#tabFind"),
    tabPlanner: measure("#tabPlanner"),
    login: measure("#authSignedOut a[href*='login']"),
    signup: measure("#authSignedOut a[href*='signup']"),
    showTut: measure("#btnShowTutorial"),
    zoomIn: measure(".leaflet-control-zoom-in"),
    zoomOut: measure(".leaflet-control-zoom-out"),
    requestBtn: measure("[data-lock-request]"),
    notes,
  };
})()
`;

async function dump(name, url, { width, height, mobile = false, script = null } = {}) {
  const out = join(outDir, `${prefix}-${name}.png`);
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    `--window-size=${width},${height}`,
    `--screenshot=${out}`,
    `--virtual-time-budget=8000`,
  ];
  if (mobile) {
    args.push("--user-agent=Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36");
    args.push(`--force-device-scale-factor=1`);
  }
  if (script) {
    const jsPath = join(outDir, `._${name}.js`);
    writeFileSync(jsPath, script);
    args.push(`--evaluate-file=${jsPath}`); // may not exist on all chromes
  }
  args.push(url);
  await runChrome(args);
  return out;
}

// Chrome --screenshot doesn't easily return JS values. Use a small CDP script via puppeteer-core if available,
// otherwise screenshots only plus a separate eval with chrome-remote or a data URL.

console.log("shots go to", outDir);
await dump("desktop", base, { width: 1440, height: 900 });
await dump("phone", base, { width: 412, height: 915, mobile: true });
await dump("login-desktop", new URL("login/", base).href, { width: 1440, height: 900 });
await dump("login-phone", new URL("login/", base).href, { width: 412, height: 915, mobile: true });
console.log("done");
