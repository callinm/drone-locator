import { isLocator } from "./product.js";
/**
 * Session helper shared by the planner and the login / signup / settings / admin pages.
 */
const state = {
  loaded: false,
  user: null,
  csrf: null,
  token: null,
  listeners: [],
};

const TOKEN_KEY = "piletally_api_token";
const CSRF_KEY = "piletally_csrf";

function readStored(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key, value) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    /* private mode */
  }
}

function persistSession(token, csrf) {
  if (token !== undefined) {
    state.token = token || null;
    writeStored(TOKEN_KEY, token || "");
  }
  if (csrf !== undefined && csrf) {
    state.csrf = csrf;
    writeStored(CSRF_KEY, csrf);
  }
}

function clearStoredSession() {
  state.token = null;
  writeStored(TOKEN_KEY, "");
  writeStored(CSRF_KEY, "");
}

state.token = readStored(TOKEN_KEY);
state.csrf = readStored(CSRF_KEY) || null;


export function apiBase() {
  const q = new URLSearchParams(location.search).get("api");
  if (q) return q.replace(/\/$/, "");
  const host = location.hostname;
  if (host === "127.0.0.1" || host === "localhost") {
    return "http://127.0.0.1:8091";
  }
  return `${location.origin}/drone-mapper/api`;
}

export function productRoot() {
  const here = location.pathname || "/";
  if (/\/drone-locator(\/|$)/i.test(here)) return "/drone-locator/";
  if (/\/drone-mapper(\/|$)/i.test(here)) return "/drone-mapper/";
  return isLocator() ? "/drone-locator/" : "/drone-mapper/";
}

export function plannerHref(page) {
  const here = location.pathname || "/";
  const root = productRoot();
  // Local / relative layouts (not the live /drone-mapper or /drone-locator roots).
  const live = /^\/(drone-mapper|drone-locator)(\/|$)/i.test(here);
  if (!live) {
    const inSub = /\/(signup|login|settings|admin)\/?$/.test(here);
    if (page === "home") return inSub ? "../" : "./";
    if (inSub) return `../${page}/`;
    return `./${page}/`;
  }
  if (!page || page === "home") return root;
  return `${root}${page.replace(/^\/+|\/+$/g, "")}/`;
}

function readCsrfCookie() {
  const match = document.cookie.split(";").map((s) => s.trim()).find((s) => s.startsWith("dm_csrf="));
  if (!match) return null;
  return decodeURIComponent(match.slice("dm_csrf=".length));
}

export function currentUser() {
  return state.user;
}

export function isAuthenticated() {
  return !!state.user;
}

export function isMosaicEnabled() {
  return !!(state.user && state.user.mosaic_enabled);
}

export function isAdmin() {
  return !!(state.user && state.user.is_admin);
}

export function onAuthChange(fn) {
  state.listeners.push(fn);
}

function notify() {
  state.listeners.forEach((fn) => fn(state.user));
}

export function requestAuthHeaders(method = "GET") {
  const headers = {};
  const verb = (method || "GET").toUpperCase();
  const csrf = state.csrf || readCsrfCookie();
  if (verb !== "GET" && verb !== "HEAD" && verb !== "OPTIONS" && csrf) {
    headers["X-CSRF-Token"] = csrf;
  }
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  return headers;
}

export async function apiFetch(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const method = (opts.method || "GET").toUpperCase();
  const csrf = state.csrf || readCsrfCookie();
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS" && csrf && !headers.has("X-CSRF-Token")) {
    headers.set("X-CSRF-Token", csrf);
  }
  if (state.token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${state.token}`);
  }
  if (opts.json !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const init = {
    ...opts,
    method,
    headers,
    credentials: "include",
  };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
  }
  delete init.json;
  const res = await fetch(`${apiBase()}${path}`, init);
  if (res.status === 401 && path !== "/auth/me" && path !== "/auth/login" && path !== "/auth/signup") {
    state.user = null;
    clearStoredSession();
    notify();
  }
  return res;
}

export async function loadSession() {
  try {
    const res = await apiFetch("/auth/me");
    const body = await res.json();
    state.loaded = true;
    state.user = body.authenticated ? body.user : null;
    if (body.csrf) persistSession(undefined, body.csrf);
    else state.csrf = state.csrf || readCsrfCookie();
    if (!body.authenticated) {
      // Cookie or stored token was rejected — drop the stale token so we do not keep sending it.
      if (state.token) clearStoredSession();
    }
  } catch {
    state.loaded = true;
    state.user = null;
  }
  notify();
  return state.user;
}

export async function login(username, password) {
  const res = await apiFetch("/auth/login", { method: "POST", json: { username, password } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not sign in.");
  state.user = body.user;
  persistSession(body.token || state.token, body.csrf);
  notify();
  return body.user;
}

export async function signup(username, password, honeypot) {
  const res = await apiFetch("/auth/signup", {
    method: "POST",
    json: { username, password, website: honeypot || "" },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not create that account.");
  state.user = body.user;
  persistSession(body.token || state.token, body.csrf);
  notify();
  return body.user;
}

export async function logout() {
  await apiFetch("/auth/logout", { method: "POST", json: {} }).catch(() => {});
  state.user = null;
  clearStoredSession();
  state.csrf = readCsrfCookie();
  notify();
}

export async function changePassword(currentPassword, newPassword) {
  const res = await apiFetch("/auth/password", {
    method: "POST",
    json: { current_password: currentPassword, new_password: newPassword },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not change password.");
  state.user = body.user;
  persistSession(body.token || state.token, body.csrf);
  notify();
}

export async function deleteAccount(password) {
  const res = await apiFetch("/auth/delete", { method: "POST", json: { password } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not delete the account.");
  state.user = null;
  clearStoredSession();
  notify();
}

export async function listUsers(opts = {}) {
  const params = new URLSearchParams();
  if (opts.q) params.set("q", opts.q);
  if (opts.sort) params.set("sort", opts.sort);
  if (opts.direction) params.set("direction", opts.direction);
  const qs = params.toString();
  const res = await apiFetch("/admin/users" + (qs ? `?${qs}` : ""));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not load users.");
  return body.users || [];
}

export async function setMosaicEnabled(username, enabled) {
  const res = await apiFetch(`/admin/users/${encodeURIComponent(username)}/mosaic`, {
    method: "POST",
    json: { mosaic_enabled: enabled },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not update that user.");
  return body.user;
}

export async function setPhotoLimits(username, { photoCap, plan, clearPhotoCap } = {}) {
  const res = await apiFetch(`/admin/users/${encodeURIComponent(username)}/limits`, {
    method: "POST",
    json: {
      photo_cap: photoCap,
      plan,
      clear_photo_cap: !!clearPhotoCap,
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not update that user.");
  return body.user;
}

export function lockMessage(feature) {
  const name = (feature || "").trim();
  const title = name ? `${name} is locked` : "Mapping is locked";
  if (!state.user) {
    return {
      title,
      body: isLocator()
        ? "Log in or create an account. Drone Locator stays locked until an admin enables mapping for your account."
        : "Log in or create an account. Pile Count stays locked until an admin enables mapping for your account.",
    };
  }
  return {
    title,
    body: isLocator()
      ? "This mapping feature is locked. Contact an admin to enable Drone Locator for your account."
      : "This mapping feature is locked. Contact an admin to enable Pile Count for your account.",
  };
}

export function applyMosaicLock() {
  const locked = !isMosaicEnabled();
  document.querySelectorAll("[data-mosaic-lock]").forEach((el) => {
    el.hidden = !locked;
    const title = el.querySelector("[data-lock-title]");
    const body = el.querySelector("[data-lock-body]");
    const feature = el.getAttribute("data-lock-feature") || "";
    const msg = lockMessage(feature);
    if (title) title.textContent = msg.title;
    if (body) body.textContent = msg.body;
    const login = el.querySelector("[data-lock-login]");
    const signup = el.querySelector("[data-lock-signup]");
    if (login) login.hidden = !!state.user;
    if (signup) signup.hidden = !!state.user;
  });
  document.querySelectorAll("[data-requires-mosaic]").forEach((el) => {
    if ("disabled" in el) el.disabled = locked;
  });
}

export function renderAuthChrome() {
  const signedIn = document.getElementById("authSignedIn");
  const signedOut = document.getElementById("authSignedOut");
  const name = document.getElementById("authUsername");
  const admin = document.getElementById("authAdminLink");
  if (name) name.textContent = state.user ? state.user.username : "";
  if (signedIn) signedIn.hidden = !state.user;
  if (signedOut) signedOut.hidden = !!state.user;
  if (admin) admin.hidden = !isAdmin();
  applyMosaicLock();
}

export async function initAuthChrome() {
  const logoutBtn = document.getElementById("authLogout");
  if (logoutBtn && !logoutBtn.dataset.bound) {
    logoutBtn.dataset.bound = "1";
    logoutBtn.addEventListener("click", async () => {
      await logout();
    });
  }
  onAuthChange(() => {
    renderAuthChrome();
  });
  await loadSession();
  renderAuthChrome();
}


export async function sendFeedback(message) {
  const res = await apiFetch("/feedback", { method: "POST", json: { message } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not send that feedback.");
  return body.feedback;
}

export async function listFeedback() {
  const res = await apiFetch("/admin/feedback");
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not load feedback.");
  return body.feedback || [];
}

export async function setFeedbackRead(id, isRead) {
  const res = await apiFetch(`/admin/feedback/${encodeURIComponent(id)}/read`, {
    method: "POST",
    json: { is_read: !!isRead },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not update that message.");
  return body.feedback;
}

export async function getAdminProcessing() {
  const res = await apiFetch("/admin/processing");
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not load processing status.");
  return body;
}
