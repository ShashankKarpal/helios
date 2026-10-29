#!/usr/bin/env node
// Checks for the navigation model in src/lib/nav.ts (rank 4, 2026-10-29): the
// app has exactly three tabs, Today, History and System, and History holds the
// record screens. Ask Helios, Insights and the Actions log are frozen
// (owner decisions 2026-10-21 and 2026-10-29) and must not come back as tabs.
// Node strips the type annotations itself, as in check-format.mjs.
import assert from "node:assert/strict";
import * as nav from "../src/lib/nav.ts";

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}

check("exactly three tabs: Today, History, System, in that order", () => {
  assert.deepEqual(nav.TABS.map((t) => t.key), ["today", "history", "system"]);
  assert.deepEqual(nav.TABS.map((t) => t.label), ["Today", "History", "System"]);
});
check("the app opens on Today", () => {
  assert.equal(nav.DEFAULT_TAB, "today");
});
check("no frozen or retired screen is a tab", () => {
  const keys = nav.TABS.map((t) => t.key);
  for (const gone of ["chat", "insights", "actions", "sleep", "activity", "labs", "workouts", "ecg"]) {
    assert.ok(!keys.includes(gone), `${gone} must not be a tab`);
  }
});
check("History holds Sleep, Activity, Workouts, ECG and Labs, in that order (rank 5)", () => {
  assert.deepEqual(nav.HISTORY_SEGMENTS.map((s) => s.key), ["sleep", "activity", "workouts", "ecg", "labs"]);
  assert.deepEqual(nav.HISTORY_SEGMENTS.map((s) => s.label), ["Sleep", "Activity", "Workouts", "ECG", "Labs"]);
  assert.equal(nav.DEFAULT_SEGMENT, "sleep");
});
check("every tab has an icon and keys are unique", () => {
  for (const t of nav.TABS) assert.ok(typeof t.icon === "string" && t.icon.length > 0, t.key);
  assert.equal(new Set(nav.TABS.map((t) => t.key)).size, nav.TABS.length);
  assert.equal(new Set(nav.HISTORY_SEGMENTS.map((s) => s.key)).size, nav.HISTORY_SEGMENTS.length);
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
console.log(`${checks.length - failed} of ${checks.length} nav checks passed`);
process.exit(failed ? 1 : 0);
