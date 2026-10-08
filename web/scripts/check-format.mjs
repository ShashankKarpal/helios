#!/usr/bin/env node
// Checks for the pure helpers in src/lib/format.ts. Node strips the type
// annotations itself (type stripping, Node 22.18+ and 23.6+), so the checks run
// against the real source with no build step. Run under several TZ values to
// prove the date helpers never consult the browser (process) zone:
//   TZ=America/Los_Angeles node scripts/check-format.mjs
import assert from "node:assert/strict";
import * as f from "../src/lib/format.ts";

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}

// A9: reporting-zone calendar, never the process zone.
check("zoneToday follows the reporting zone", () => {
  const now = new Date("2026-10-07T22:30:00Z"); // 02:30 on Oct 8 in Dubai
  assert.equal(f.zoneToday("Asia/Dubai", now), "2026-10-08");
  assert.equal(f.zoneToday("America/New_York", now), "2026-10-07");
  assert.equal(f.zoneToday("UTC", now), "2026-10-07");
});
check("addDays is pure calendar arithmetic", () => {
  assert.equal(f.addDays("2026-10-01", -1), "2026-09-30");
  assert.equal(f.addDays("2026-12-31", 1), "2027-01-01");
  assert.equal(f.addDays("2026-03-01", -1), "2026-02-28");
  assert.equal(f.addDays("not-a-date", 1), "not-a-date");
});
check("formatDate and shortDate never shift a calendar date", () => {
  // Whatever TZ this process runs in, Oct 8 stays Oct 8.
  assert.match(f.formatDate("2026-10-08"), /Oct.*8|8.*Oct/);
  assert.doesNotMatch(f.formatDate("2026-10-08"), /\b7\b/);
  assert.match(f.shortDate("2026-10-08"), /Oct.*8|8.*Oct/);
  assert.match(f.shortDate("2026-01-01"), /Jan.*1|1.*Jan/);
});
check("formatAsOf reads a naive wall time as the zone's own clock", () => {
  const now = new Date("2026-10-08T03:00:00Z"); // 07:00 Oct 8 Dubai
  assert.equal(f.formatAsOf("2026-10-08 06:17:39.123456", "Asia/Dubai", now), "today 06:17");
  assert.match(f.formatAsOf("2026-10-07 20:20:35.891754", "Asia/Dubai", now), /^Oct 7, 20:20$|^7 Oct, 20:20$/);
});
check("formatAsOf converts an offset-aware instant into the zone", () => {
  const now = new Date("2026-10-08T03:00:00Z");
  assert.equal(f.formatAsOf("2026-10-08T02:17:39Z", "Asia/Dubai", now), "today 06:17");
  assert.equal(f.formatAsOf("2026-10-08T06:17:39+04:00", "Asia/Dubai", now), "today 06:17");
  assert.match(f.formatAsOf("2026-10-07T20:20:35+04:00", "Asia/Dubai", now), /^Oct 7, 20:20$|^7 Oct, 20:20$/);
  // The same instant viewed from New York is still filed by the reporting zone.
  assert.equal(f.formatAsOf("2026-10-08T02:17:39Z", "Asia/Dubai", now), "today 06:17");
});
check("formatAsOf leaves an unparseable value alone", () => {
  assert.equal(f.formatAsOf("soon", "Asia/Dubai"), "soon");
});

let failed = 0;
for (const [name, fn] of checks) {
  try {
    fn();
    console.log("ok   " + name);
  } catch (e) {
    failed += 1;
    console.log("FAIL " + name + ": " + (e && e.message ? e.message : e));
  }
}
console.log(`${checks.length - failed} of ${checks.length} checks passed (TZ=${process.env.TZ || "system"})`);
process.exit(failed ? 1 : 0);
