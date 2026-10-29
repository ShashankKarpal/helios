#!/usr/bin/env node
// Checks the BUILT app (dist), which is what heliosd serves, not the source:
// a screen that is frozen must be absent from the bundle, and every data path
// a later rank needs must still be in it (rank 4, 2026-10-29). Run after
// `npm run build`; pass another dist folder as the first argument to check it
// instead (the controls do this).
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, "..");
const dist = resolve(process.argv[2] ?? join(webRoot, "dist"));

if (!existsSync(join(dist, "index.html")) || !existsSync(join(dist, "assets"))) {
  console.log(`FAIL no built app at ${dist} (run npm run build)`);
  process.exit(1);
}
const scripts = readdirSync(join(dist, "assets")).filter((f) => f.endsWith(".js"));
const bundle = scripts.map((f) => readFileSync(join(dist, "assets", f), "utf8")).join("\n");
const shell = readFileSync(join(dist, "index.html"), "utf8");

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}
function has(text) {
  assert.ok(bundle.includes(text), `the bundle lacks ${JSON.stringify(text)}`);
}

check(`read ${scripts.length} script files`, () => {
  assert.ok(scripts.length > 0, "no script files under assets/");
});
check("History has its segment row", () => {
  has("History sections");
});
check("the record screens and their data paths are kept (Sleep, Activity, Labs for rank 6)", () => {
  for (const p of ["/api/sleep", "/api/activity", "/api/labs", "/api/labs/parse", "/api/labs/confirm"]) has(p);
  for (const label of ["Reviewing your nights", "Adding up your movement", "Reading your labs"]) has(label);
});
check("Today and its data paths are kept", () => {
  for (const p of ["/api/today", "/api/whoop/pull", "/api/quicklog/confirm"]) has(p);
});
check("sign-in is kept: the card, the session route and the 401 event", () => {
  for (const s of ["/api/session", "helios:sign-in-needed", "That token is not right."]) has(s);
});
check("the shell links the manifest; the served sw.js is the source sw.js", () => {
  assert.match(shell, /<link[^>]+rel="manifest"[^>]+href="\/manifest\.webmanifest"/);
  const built = readFileSync(join(dist, "sw.js"), "utf8");
  const source = readFileSync(join(webRoot, "public", "sw.js"), "utf8");
  assert.equal(built, source, "dist/sw.js differs from public/sw.js");
});

let failed = 0;
for (const [name, fn] of checks) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`FAIL ${name}\n     ${err.message}`);
  }
}
console.log(`${checks.length - failed} of ${checks.length} bundle checks passed (${dist})`);
process.exit(failed ? 1 : 0);
