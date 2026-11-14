import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "web");

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(js|html)$/.test(name)) out.push(p);
  }
  return out;
}

function importsOf(basename, text) {
  const re = new RegExp(String.raw`(?:from\s+|import\()\s*["']([^"']*${basename}[^"']*)["']`, "g");
  const found = [];
  let m;
  while ((m = re.exec(text))) found.push(m[1]);
  return found;
}

test("every auth.js import uses the same cache-bust query", () => {
  const queries = new Set();
  const rows = [];
  for (const file of walk(root)) {
    const text = readFileSync(file, "utf8");
    for (const spec of importsOf("auth\\.js", text)) {
      const q = spec.includes("?") ? spec.slice(spec.indexOf("?")) : "";
      queries.add(q);
      rows.push(`${file}: ${spec}`);
    }
  }
  assert.ok(rows.length > 0, "expected auth.js imports");
  assert.equal(queries.size, 1, `mismatched auth.js specifiers:\n${rows.join("\n")}`);
  assert.ok([...queries][0].startsWith("?v="), "auth.js imports should share one ?v= token");
});

test("finder.js and tutorial.js imports from app.js use the same token as auth.js", () => {
  const app = readFileSync(join(root, "js/app.js"), "utf8");
  const auth = importsOf("auth\\.js", app)[0];
  const tutorial = importsOf("tutorial\\.js", app)[0];
  const finder = importsOf("finder\\.js", app)[0];
  assert.ok(auth && tutorial && finder);
  const token = auth.slice(auth.indexOf("?"));
  assert.equal(tutorial.slice(tutorial.indexOf("?")), token);
  assert.equal(finder.slice(finder.indexOf("?")), token);
});

test("global [hidden] rule beats display:flex styling", () => {
  const css = readFileSync(join(root, "css/app.css"), "utf8");
  assert.match(css, /(?:^|\n)\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;\s*\}/);
});
