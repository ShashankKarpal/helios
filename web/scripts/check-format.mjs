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

// A21: units and values in the metric's own grammar.
check("unitLabel turns store keys into words", () => {
  assert.equal(f.unitLabel("heart_rate", "count/min"), "bpm");
  assert.equal(f.unitLabel("resting_hr", "count/min"), "bpm");
  assert.equal(f.unitLabel("respiratory_rate", "count/min"), "breaths/min");
  assert.equal(f.unitLabel("steps", "count"), "steps");
  assert.equal(f.unitLabel("spo2", "%"), "%");
  assert.equal(f.unitLabel("active_energy", "kcal"), "kcal");
});
check("formatMetricValue: sleep as h:mm, counts as whole numbers", () => {
  assert.deepEqual(f.formatMetricValue("sleep_duration", 5.34, "h"), { text: "5h 20m", unit: "" });
  assert.deepEqual(f.formatMetricValue("sleep_duration", 6.5, "h"), { text: "6h 30m", unit: "" });
  assert.equal(f.formatMetricValue("steps", 4361.5, "count").text, (4362).toLocaleString());
  assert.equal(f.formatMetricValue("steps", 114, "count").unit, "steps");
  assert.equal(f.formatMetricValue("resting_hr", 67, "count/min").text, "67");
  assert.equal(f.formatMetricValue("resting_hr", 67, "count/min").unit, "bpm");
  assert.equal(f.formatMetricValue("respiratory_rate", null, "count/min").text, "--");
});
check("flagLabel reads flags in words", () => {
  assert.equal(f.flagLabel("travel_or_shifted_schedule"), "Schedule shift");
  assert.equal(f.flagLabel("heat"), "Heat season");
  assert.equal(f.flagLabel("some_new_flag"), "Some new flag");
});
check("humanizeDevice names Whoop's Apple Health copy as such", () => {
  assert.equal(f.humanizeDevice("whoop:healthkit"), "Whoop (Apple Health copy)");
  assert.equal(f.humanizeDevice("whoop"), "Whoop");
});
check("humanizeMetric names the ring temperature honestly", () => {
  assert.equal(f.humanizeMetric("body_temp"), "Skin temperature (ring)");
  assert.equal(f.humanizeMetric("resting_hr"), "Resting heart rate");
  assert.equal(f.humanizeMetric("hrv_rmssd"), "HRV (rMSSD)");
});
check("awaitingLabel lists the missing markers in words", () => {
  assert.equal(f.awaitingLabel(["recovery_score", "hrv_rmssd"]), "recovery score and HRV (rMSSD)");
  assert.equal(f.awaitingLabel(["sleep_duration"]), "sleep");
  assert.equal(f.awaitingLabel([]), "");
});
// B10: the legacy watch fills history days and is named as the owner reads it.
check("humanizeDevice names the earlier watch", () => {
  assert.equal(f.humanizeDevice("apple_watch_6_legacy"), "Apple Watch 6");
  assert.equal(f.humanizeDevice("apple_watch_ultra"), "Apple Watch Ultra");
});
check("trendArrow shows nothing when there is no comparison", () => {
  assert.equal(f.trendArrow(null).glyph, "");
  assert.equal(f.trendArrow(3).label, "trending up");
  assert.equal(f.trendArrow(-3).label, "trending down");
  assert.equal(f.trendArrow(0.5).label, "steady");
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
