import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "web");
function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === "vendor") continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(js|html)$/.test(name)) out.push(p);
  }
  return out;
}

test("every relative module import and the app.js/app.css tags share one ?v= token", () => {
  const tokens = new Set();
  const bare = [];
  const re = /(?:from\s+|import\()\s*["'](\.{1,2}\/[^"']+\.js)(\?v=[^"']+)?["']|(?:src|href)=["'][^"']*(?:js\/app\.js|css\/app\.css)(\?v=[^"']+)?["']/g;
  for (const file of walk(root)) {
    const text = readFileSync(file, "utf8");
    let m;
    while ((m = re.exec(text))) {
      const tok = m[2] || m[3];
      if (!tok) bare.push(`${file}: ${m[0]}`);
      else tokens.add(tok);
    }
  }
  assert.deepEqual(bare, [], "unversioned imports");
  assert.equal(tokens.size, 1, `tokens: ${[...tokens].join(", ")}`);
});
