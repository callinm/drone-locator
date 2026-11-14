import { applyProductChrome, isLocator } from "./product.js";

const VIEWS = {
  planner: { view: "viewPlanner", tab: "tabPlanner" },
  saved: { view: "viewSaved", tab: "tabSaved" },
  stockpile: { view: "viewStockpile", tab: "tabStockpile" },
  find: { view: "viewFind", tab: "tabFind" },
};

const listeners = [];

export function setPageTab(name) {
  for (const [key, ids] of Object.entries(VIEWS)) {
    const on = key === name;
    const view = document.getElementById(ids.view);
    const tab = document.getElementById(ids.tab);
    if (view) view.hidden = !on;
    if (tab) tab.setAttribute("aria-selected", on ? "true" : "false");
  }
  listeners.forEach((fn) => fn(name));
}

export function onPageTab(fn) {
  listeners.push(fn);
}

export function initPageTabs() {
  applyProductChrome();
  const bind = (id, name) => {
    document.getElementById(id)?.addEventListener("click", () => setPageTab(name));
  };
  bind("tabPlanner", "planner");
  bind("tabSaved", "saved");
  bind("tabStockpile", "stockpile");
  bind("tabFind", "find");
  const params = new URLSearchParams(location.search);
  const tab = params.get("tab");
  if (tab && VIEWS[tab]) setPageTab(tab);
  else if (isLocator()) setPageTab("find");
}
