import { isLocator, productName } from "./product.js?v=20261114dlrr2";
import { isMosaicEnabled } from "./auth.js?v=20261114dlrr2";
/**
 * First-run guided tour: a pop-up bubble, a spotlight, and an arrow
 * pointing at the control to click. Completion is stored locally and
 * in /sync/settings so it does not auto-start again on another device.
 */
export const TUTORIAL_SETTINGS_KEY = "tutorial_completed";
export const TUTORIAL_SKIPPED_KEY = "tutorial_skipped";
export const TUTORIAL_AT_KEY = "tutorial_completed_at";

export const PILETALLY_STEPS = Object.freeze([
  {
    id: "welcome",
    title: "Welcome to Piletally",
    body: "It counts what's in a pile. This short tour covers planning a flight and counting a stockpile: Upload, Stitch, then Volume.",
    target: null,
    cta: "Start the tour",
  },
  {
    id: "signin",
    title: "Sign in",
    body: "Log in or sign up at the top of the page so your missions, photos, and pile volumes stay with your account on the website and the phone.",
    target: "authChrome",
  },
  {
    id: "map",
    title: "The map",
    body: "This map is your workspace. When no mission is open, it starts at your location if you allow it. Use Locate if you want to jump there again.",
    target: "btnLocate",
    tab: "planner",
  },
  {
    id: "plan-save",
    title: "Plan and save a mission",
    body: "Open Planner, draw the field around the piles, then tap Save to my list. That plan shows up in the Android flight app, ready to fly.",
    target: "btnSaveLocal",
    tab: "planner",
  },
  {
    id: "stockpile-upload",
    title: "Upload",
    body: "Open Pile Count and tap Upload. Choose your photos and the upload starts right away. You will see a percent and a list of files. When it is done, the button reads Upload ✓ Success.",
    target: "btnStockpileUpload",
    tab: "stockpile",
  },
  {
    id: "stockpile-stitch",
    title: "Stitch",
    body: "After Upload complete, tap Stitch. That builds the survey map. It can take a while and uses disk, so it does not start on its own. A timer and percent show progress.",
    target: "btnStockpileStitch",
    tab: "stockpile",
  },
  {
    id: "stockpile-volume",
    title: "Volume",
    body: "When Stitch finishes, Piletally auto-detects the pile and shows the volume at the crushed-stone density. Fix the outline or change density only if you need to.",
    target: "btnStockpileVolume",
    tab: "stockpile",
  },
  {
    id: "settings",
    title: "Settings and this tutorial",
    body: "Account Settings is where you change your password, send feedback, or delete only your own account. You can replay this tutorial from Show tutorial at any time.",
    target: "btnShowTutorial",
  },
  {
    id: "first-job",
    title: "Count a pile",
    body: "Ready to count a pile? Open Pile Count, tap Upload, then Stitch. The volume appears when the map is ready.",
    target: "tabStockpile",
    tab: "stockpile",
    cta: "Start a pile count",
    finish: true,
  },
]);

export const LOCATOR_STEPS = Object.freeze([
  {
    id: "welcome",
    title: "Welcome to Drone Locator",
    body: "Search a map of the area for a lost aircraft. This short tour covers the locator steps.",
    target: null,
    cta: "Start the tour",
  },
  {
    id: "signin",
    title: "Sign in",
    body: "Log in or sign up so your maps and search results stay with your account.",
    target: "authChrome",
  },
  {
    id: "load-map",
    title: "Make your search map",
    body: "Tap Make a map from my flight photos and pick all the photos from your search flight. We stitch them into one map and load it here; steps turn green as they finish. Already have one big picture of the area? Use I already have a map.",
    target: "btnPhotoMap",
    tab: "find",
  },
  {
    id: "query",
    title: "Show us your drone",
    body: "On a phone, tap Draw box, then drag on the map. Or upload a close-up photo of your drone or a marking on it.",
    target: "btnDrawQuery",
    tab: "find",
  },
  {
    id: "search",
    title: "Search",
    body: "Turn on “Look for this color” if you know the paint color, then tap Search. Matches appear as numbered pins with a score.",
    target: "btnFindSearch",
    tab: "find",
  },
  {
    id: "navigate",
    title: "Navigate here",
    body: "The top match gets a Navigate here button on the map. Open it to get walking directions to that point.",
    target: "findNavigate",
    tab: "find",
    keepIfMissing: true,
  },
  {
    id: "settings",
    title: "Settings and this tutorial",
    body: "Account Settings is where you change your password, send feedback, or delete only your own account. Replay this tutorial from Show tutorial at any time.",
    target: "btnShowTutorial",
    finish: true,
    cta: "Finish",
  },
]);

export const LOCATOR_LOCKED_STEPS = Object.freeze([
  {
    id: "welcome",
    title: "Welcome to Drone Locator",
    body: "This site helps you search a map of the area for a lost aircraft. You need an account, and an admin has to turn it on for you first.",
    target: null,
    cta: "Start the tour",
  },
  {
    id: "signin",
    title: "Sign in or request access",
    body: "Log in or create an account at the top of the page. After you sign in, tap Request access on this screen so an admin can turn Drone Locator on for you.",
    target: "locatorLockCard",
  },
  {
    id: "how-it-works",
    title: "How a search works",
    body: "Once it is turned on: make a map from your search-flight photos, show us your drone by drawing a box or uploading a photo, tap Search, then use Navigate here to walk to the top match.",
    target: null,
  },
  {
    id: "settings",
    title: "Settings and this tutorial",
    body: "Account Settings is where you change your password, send feedback, or delete only your own account. Replay this tutorial from Show tutorial at any time.",
    target: "btnShowTutorial",
    finish: true,
    cta: "Finish",
  },
]);

export function steps() {
  if (!isLocator()) return PILETALLY_STEPS;
  try {
    if (!isMosaicEnabled()) return LOCATOR_LOCKED_STEPS;
  } catch {
    return LOCATOR_LOCKED_STEPS;
  }
  return LOCATOR_STEPS;
}

/** @deprecated use steps() — kept so older imports do not break */
export const STEPS = PILETALLY_STEPS;

export function isCompleted(settings, localFlag) {
  if (settings && (settings[TUTORIAL_SETTINGS_KEY] === true || settings[TUTORIAL_SKIPPED_KEY] === true)) {
    return true;
  }
  return localFlag === true;
}

export function completionPayload(kind = "completed") {
  const now = Date.now() / 1000;
  return {
    [TUTORIAL_SETTINGS_KEY]: true,
    [TUTORIAL_SKIPPED_KEY]: kind === "skipped",
    [TUTORIAL_AT_KEY]: now,
  };
}

export function nextIndex(current, total = steps().length) {
  return Math.min(total - 1, Math.max(0, current + 1));
}

export function prevIndex(current) {
  return Math.max(0, current - 1);
}

export function stepById(id) {
  return steps().find((s) => s.id === id) || null;
}

const LOCAL_KEY = "dm_tutorial_completed";
const PENDING_KEY = "dm_pending_onboarding";

function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readLocalCompletion() {
  const s = storage();
  return s?.getItem(LOCAL_KEY) === "1";
}

export function writeLocalCompletion() {
  storage()?.setItem(LOCAL_KEY, "1");
  storage()?.removeItem(PENDING_KEY);
}

export function markPendingOnboarding() {
  storage()?.setItem(PENDING_KEY, "1");
}

export function hasPendingOnboarding() {
  const params = new URLSearchParams(location.search);
  if (params.get("welcome") === "1" || params.get("tour") === "1") return true;
  return storage()?.getItem(PENDING_KEY) === "1";
}

export function forceTourFromQuery() {
  const params = new URLSearchParams(location.search);
  return params.get("welcome") === "1" || params.get("tour") === "1";
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Place the spotlight, arrow, and bubble so the arrow points at the
 * target and the bubble stays fully on screen, including phone widths.
 */
/** On phones the account links sit behind the Menu button; open it when the tour points there. */
function openHeaderMenuFor(target) {
  const bar = document.querySelector(".page-tabs");
  const btn = document.getElementById("btnHeaderMenu");
  if (!bar || !btn) return;
  const inMenu = !!(target && target.closest && target.closest(".auth-chrome"));
  if (inMenu && !bar.classList.contains("menu-open")) {
    bar.classList.add("menu-open");
    bar.dataset.tourOpened = "1";
    btn.setAttribute("aria-expanded", "true");
  } else if (!inMenu && bar.dataset.tourOpened) {
    bar.classList.remove("menu-open");
    delete bar.dataset.tourOpened;
    btn.setAttribute("aria-expanded", "false");
  }
}

export function layoutTourChrome(host, targetId) {
  if (!host) return { placed: false };
  const card = host.querySelector(".tutorial-card");
  const arrow = host.querySelector(".tutorial-arrow");
  const hi = host.querySelector(".tutorial-highlight");
  if (!card || !arrow || !hi) return { placed: false };

  const pad = 10;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const target = targetId ? document.getElementById(targetId) : null;
  openHeaderMenuFor(target);
  const visible = target && target.getClientRects().length > 0 && target.offsetParent !== null;

  if (!visible) {
    hi.hidden = true;
    arrow.hidden = true;
    card.style.left = "50%";
    card.style.right = "auto";
    card.style.top = "auto";
    card.style.bottom = "20px";
    card.style.transform = "translateX(-50%)";
    card.style.maxWidth = `${Math.min(520, vw - 24)}px`;
    return { placed: false, reason: "no-target" };
  }

  target.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "auto" });
  const r = target.getBoundingClientRect();
  hi.hidden = false;
  hi.style.top = `${Math.max(0, r.top - 8)}px`;
  hi.style.left = `${Math.max(0, r.left - 8)}px`;
  hi.style.width = `${Math.min(vw, r.width + 16)}px`;
  hi.style.height = `${r.height + 16}px`;

  card.style.transform = "none";
  card.style.right = "auto";
  card.style.bottom = "auto";
  card.style.maxWidth = `${Math.min(400, vw - 24)}px`;
  const cardW = Math.min(400, vw - 24);
  card.style.width = `${cardW}px`;
  const ch = Math.max(card.offsetHeight, 160);
  const spaceBelow = vh - r.bottom;
  const spaceAbove = r.top;
  const gap = 28;
  let top;
  let dir;
  if (spaceBelow >= ch + gap || spaceBelow >= spaceAbove) {
    top = Math.min(r.bottom + gap, vh - ch - pad);
    dir = "up"; // arrow on top of card, pointing up at the target
  } else {
    top = Math.max(pad, r.top - ch - gap);
    dir = "down";
  }
  if (top < pad) top = pad;
  if (top + ch > vh - pad) top = Math.max(pad, vh - ch - pad);

  let left = r.left + r.width / 2 - cardW / 2;
  left = Math.min(Math.max(pad, left), vw - cardW - pad);
  card.style.top = `${top}px`;
  card.style.left = `${left}px`;

  const cx = r.left + r.width / 2;
  const arrowX = Math.min(Math.max(left + 18, cx), left + cardW - 18);
  arrow.hidden = false;
  arrow.dataset.dir = dir;
  if (dir === "up") {
    arrow.style.top = `${top - 14}px`;
  } else {
    arrow.style.top = `${top + card.offsetHeight - 2}px`;
  }
  arrow.style.left = `${arrowX - 12}px`;
  return { placed: true, dir, top, left };
}

/**
 * A required target that is not on screen must never leave the tour stuck.
 * Welcome-style steps with no target stay put.
 */
export function shouldAdvancePastMissingTarget(step, layoutResult) {
  if (step && step.keepIfMissing) return false;
  return !!(step && step.target && layoutResult && layoutResult.reason === "no-target");
}

export function targetIsVisible(targetId) {
  if (!targetId) return false;
  const target = document.getElementById(targetId);
  return !!(target && target.getClientRects().length > 0 && target.offsetParent !== null);
}

export function nextIndexAfterMissingTarget(startIndex, list) {
  const seq = list || steps();
  let i = startIndex;
  for (let n = 0; n < seq.length; n++) {
    const nxt = nextIndex(i, seq.length);
    if (nxt === i) return i;
    const step = seq[nxt];
    if (!step.target || step.keepIfMissing || step.tab || targetIsVisible(step.target)) return nxt;
    i = nxt;
  }
  return i;
}


function prepareNavigateDemo(step) {
  const bar = document.getElementById("findNavigate");
  if (!bar) return;
  if (step && step.id === "navigate") {
    if (bar.hidden) {
      bar.hidden = false;
      bar.dataset.tutorialDemo = "1";
      const link = document.getElementById("findNavigateLink");
      if (link && !link.getAttribute("href")) {
        link.href = "https://www.google.com/maps";
        link.textContent = "Navigate here";
      }
      const meta = document.getElementById("findNavigateMeta");
      if (meta && !meta.textContent) {
        meta.textContent = "Example: this button opens walking directions to the top match.";
      }
    }
    return;
  }
  clearNavigateDemo();
}

function clearNavigateDemo() {
  const bar = document.getElementById("findNavigate");
  if (bar && bar.dataset.tutorialDemo === "1") {
    bar.hidden = true;
    delete bar.dataset.tutorialDemo;
    const meta = document.getElementById("findNavigateMeta");
    if (meta) meta.textContent = "";
  }
}

function paintStep(host, index, hooks) {
  const step = steps()[index];
  const total = steps().length;
  host.hidden = false;
  host.innerHTML = `
    <div class="tutorial-backdrop" data-tutorial-dim></div>
    <div class="tutorial-highlight" hidden></div>
    <div class="tutorial-arrow" hidden data-dir="up" aria-hidden="true"></div>
    <div class="tutorial-card" role="dialog" aria-modal="false" aria-labelledby="tutorialTitle">
      <button type="button" class="tutorial-close tutorial-hit" data-tutorial-close aria-label="Close tutorial">&times;</button>
      <p class="tutorial-count">${index + 1} of ${total}</p>
      <h2 id="tutorialTitle">${escapeHtml(step.title)}</h2>
      <p>${escapeHtml(step.body)}</p>
      <div class="tutorial-actions">
        <button type="button" class="ghost tutorial-hit" data-tutorial-skip>Skip</button>
        ${index > 0 ? '<button type="button" class="ghost tutorial-hit" data-tutorial-prev>Back</button>' : ""}
        <button type="button" class="primary tutorial-hit" data-tutorial-next>${escapeHtml(step.cta || (step.finish ? "Finish" : "Next"))}</button>
      </div>
    </div>
  `;
  if (step.tab) hooks.setPageTab?.(step.tab);
  prepareNavigateDemo(step);
  const layout = () => layoutTourChrome(host, step.target);
  layout();
  requestAnimationFrame(layout);
  setTimeout(layout, 60);
  return layout;
}

export function initTutorial({
  setPageTab,
  getSettings,
  saveSettings,
  autoStart = false,
} = {}) {
  const host = document.getElementById("tutorialHost");
  if (!host || host.dataset.bound) return { start() {}, replay() {} };
  host.dataset.bound = "1";
  let index = 0;
  let running = false;
  let relayout = null;

  async function persist(kind) {
    writeLocalCompletion();
    const extra = completionPayload(kind);
    try {
      const current = (await getSettings?.()) || {};
      await saveSettings?.({ ...current, ...extra });
    } catch {
      /* local flag is enough if we are offline */
    }
  }

  function stopTracking() {
    if (!relayout) return;
    window.removeEventListener("resize", relayout);
    window.removeEventListener("scroll", relayout, true);
    relayout = null;
  }

  function close() {
    running = false;
    stopTracking();
    clearNavigateDemo();
    host.hidden = true;
    host.innerHTML = "";
  }

  function show(i) {
    running = true;
    index = i;
    const layout = paintStep(host, index, { setPageTab });
    stopTracking();
    const shown = index;
    relayout = () => {
      if (!running) return;
      return layoutTourChrome(host, steps()[index].target);
    };
    window.addEventListener("resize", relayout);
    window.addEventListener("scroll", relayout, true);
    // If the highlighted control never appears, skip ahead so the tour
    // cannot sit on a missing target. Wait for tab switches / map layout.
    setTimeout(() => {
      if (!running || index !== shown) return;
      const step = steps()[index];
      const result = layout();
      if (!shouldAdvancePastMissingTarget(step, result)) return;
      const nxt = nextIndexAfterMissingTarget(index);
      if (nxt !== index) show(nxt);
    }, 250);
  }

  async function dismiss(kind = "skipped") {
    close();
    try {
      await persist(kind);
    } catch {
      /* already closed; local flag is set inside persist */
    }
  }

  function onKeydown(ev) {
    if (!running) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      dismiss("skipped");
    }
  }
  window.addEventListener("keydown", onKeydown);

  host.addEventListener("click", async (ev) => {
    if (ev.target.closest("[data-tutorial-skip], [data-tutorial-close]")) {
      ev.preventDefault();
      await dismiss("skipped");
      return;
    }
    if (ev.target.closest("[data-tutorial-prev]")) {
      show(prevIndex(index));
      return;
    }
    if (ev.target.closest("[data-tutorial-next]")) {
      const step = steps()[index];
      if (step.finish || index >= steps().length - 1) {
        close();
        setPageTab?.(isLocator() ? "find" : "planner");
        try {
          await persist("completed");
        } catch {
          /* already closed */
        }
        return;
      }
      show(nextIndex(index));
    }
  });

  async function maybeAutoStart() {
    if (forceTourFromQuery()) {
      show(0);
      return;
    }
    if (!autoStart) return;
    let settings = null;
    try {
      settings = await getSettings?.();
    } catch {
      settings = null;
    }
    if (isCompleted(settings, readLocalCompletion())) return;
    // First visit or first sign-in (and new accounts flagged at signup).
    show(0);
  }

  maybeAutoStart();

  return {
    start() {
      show(0);
    },
    replay() {
      show(0);
    },
    close,
  };
}
