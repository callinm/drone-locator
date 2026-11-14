import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const {
  LOCATOR_STEPS,
  LOCATOR_LOCKED_STEPS,
  shouldAdvancePastMissingTarget,
  nextIndexAfterMissingTarget,
  nextIndex,
} = await import(join(root, "web/js/tutorial.js"));

test("unlocked locator tour includes Navigate here and does not skip it", () => {
  const ids = LOCATOR_STEPS.map((s) => s.id);
  assert.deepEqual(ids, ["welcome", "signin", "load-map", "query", "search", "navigate", "settings"]);
  const nav = LOCATOR_STEPS.find((s) => s.id === "navigate");
  assert.equal(nav.keepIfMissing, true);
  assert.equal(nav.title, "Navigate here");
  const navIdx = ids.indexOf("navigate");
  assert.equal(
    shouldAdvancePastMissingTarget(nav, { placed: false, reason: "no-target" }),
    false,
  );
  assert.equal(nextIndexAfterMissingTarget(navIdx - 1, LOCATOR_STEPS), navIdx);
  const finish = LOCATOR_STEPS[LOCATOR_STEPS.length - 1];
  assert.equal(finish.finish, true);
  assert.equal(finish.tab, undefined);
});

test("locked locator tour does not point at hidden search controls", () => {
  const ids = LOCATOR_LOCKED_STEPS.map((s) => s.id);
  assert.deepEqual(ids, ["welcome", "signin", "how-it-works", "settings"]);
  for (const step of LOCATOR_LOCKED_STEPS) {
    assert.ok(!["btnLoadMosaic", "btnLoadRef", "btnFindSearch", "btnDrawQuery"].includes(step.target));
  }
  assert.equal(LOCATOR_LOCKED_STEPS[1].target, "locatorLockCard");
  assert.equal(LOCATOR_LOCKED_STEPS.at(-1).finish, true);
});

test("step counters stay sequential", () => {
  assert.equal(nextIndex(4, 7), 5);
  assert.equal(nextIndex(5, 7), 6);
  assert.equal(nextIndex(6, 7), 6);
});

test("page copy and CSS hide the broken survey-map button and keep the lock on screen", () => {
  const html = readFileSync(join(root, "web/index.html"), "utf8");
  const css = readFileSync(join(root, "web/css/app.css"), "utf8");
  assert.doesNotMatch(html, /Use latest survey map/);
  assert.doesNotMatch(html, /btnUseOrtho/);
  assert.match(html, /btnRequestAccess/);
  assert.match(html, /btnDrawQuery/);
  assert.doesNotMatch(html, /orthophoto|GeoTIFF|GSD \(cm/);
  assert.match(css, /min-height:\s*44px/);
  assert.match(css, /\.note, \.warnbox, \.errorbox, \.okbox \{[^}]*font-size:\s*14px/s);
  assert.doesNotMatch(css, /top:\s*48vh/);
  const src = readFileSync(join(root, "web/js/tutorial.js"), "utf8");
  assert.match(src, /setPageTab\?\.\(isLocator\(\) \? "find" : "planner"\)/);
  const finder = readFileSync(join(root, "web/js/finder.js"), "utf8");
  assert.match(finder, /pointerdown/);
  assert.match(finder, /FINDER_DOWN/);
  assert.doesNotMatch(finder, /127\.0\.0\.1:8091/);
});
