import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "web/js/finder.js"), "utf8");
const body = src.slice(src.indexOf("export function searchBlocker"), src.indexOf("function enableSearch"));
const searchBlocker = new Function(body.replace("export ", "") + "; return searchBlocker;")();

test("search explains what is missing", () => {
  assert.match(searchBlocker({}), /step 1.*step 2/);
  assert.match(searchBlocker({ queryFile: {} }), /step 1: load a search map/);
  assert.match(searchBlocker({ mosaicId: "m" }), /step 2/);
  assert.match(searchBlocker({ mosaicId: "m", apiOk: false, queryFile: {} }), /not answering/);
  assert.equal(searchBlocker({ mosaicId: "m", queryRect: { w: 20, h: 20 } }), "");
});

test("no misleading copy is left", () => {
  assert.doesNotMatch(src, /Your map stays on the server/);
  assert.doesNotMatch(src, /did not upload\. Try again in a few minutes/);
  assert.doesNotMatch(src, /"Sign in to use Drone Locator\."/);
});
