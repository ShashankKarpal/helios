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

// Rank 2: the last valid brief, in the reporting zone, worded as facts.
check("briefStatusLine: today's brief says when it was first written", () => {
  const now = new Date("2026-10-08T06:52:00Z"); // 10:52 Dubai
  const s = { first_published_at: "2026-10-08T07:12:00+04:00", published_at: "2026-10-08T10:50:00+04:00",
              age_hours: 0, today: true, overdue: false, due: "09:00", waiting_since: null };
  assert.deepEqual(f.briefStatusLine(s, "Asia/Dubai", now), { text: "Today's brief first written at 07:12", overdue: false });
});
check("briefStatusLine: a brief waiting for Whoop is not delivered", () => {
  const now = new Date("2026-10-08T05:30:00Z"); // 09:30 Dubai
  const s = { first_published_at: "2026-10-07T07:10:00+04:00", published_at: "2026-10-07T07:10:00+04:00",
              age_hours: 26.3, today: false, overdue: true, due: "09:00", waiting_since: "2026-10-08T06:10:00+04:00" };
  assert.deepEqual(f.briefStatusLine(s, "Asia/Dubai", now),
                   { text: "Brief late: still waiting for last night's Whoop data (since 06:10, due by 09:00)", overdue: true });
  assert.equal(f.briefStatusLine({ ...s, overdue: false }, "Asia/Dubai", now).text,
               "Brief waiting for last night's Whoop data since 06:10");
});
check("briefStatusLine: no complete brief today says how old the last one is", () => {
  const now = new Date("2026-10-08T05:30:00Z"); // 09:30 Dubai
  const s = { first_published_at: "2026-10-07T07:10:00+04:00", published_at: "2026-10-07T07:10:00+04:00",
              age_hours: 26.3, today: false, overdue: true, due: "09:00", waiting_since: null };
  const line = f.briefStatusLine(s, "Asia/Dubai", now);
  assert.equal(line.overdue, true);
  assert.match(line.text, /^Brief late: no complete brief yet today \(due by 09:00\); the last one is 26 h old \((Oct 7|7 Oct), 07:10\)$/);
  const early = f.briefStatusLine({ ...s, overdue: false, age_hours: 22.8 }, "Asia/Dubai", now);
  assert.match(early.text, /^No complete brief yet today; the last one is 23 h old/);
  assert.equal(early.overdue, false);
  assert.match(f.briefStatusLine({ ...s, age_hours: 75 }, "Asia/Dubai", now).text, /3 days old/);
});
// Rank 5 (P1, owner yes 2026-10-29): after the due time the line says late,
// as the check line does; before it the wording is unchanged.
check("briefStatusLine: after the due time every late case starts with Brief late", () => {
  const base = { first_published_at: null, published_at: null, age_hours: null, today: false, due: "10:00" };
  const waiting = { ...base, overdue: true, waiting_since: "2026-10-29T08:12:00+04:00" };
  assert.equal(f.briefStatusLine(waiting, "Asia/Dubai", new Date("2026-10-29T06:30:00Z")).text,
               "Brief late: still waiting for last night's Whoop data (since 08:12, due by 10:00)");
  for (const s of [waiting, { ...base, overdue: true },
                   { ...base, overdue: true, published_at: "2026-10-28T07:30:00+04:00", age_hours: 27 }]) {
    const line = f.briefStatusLine(s, "Asia/Dubai", new Date("2026-10-29T06:30:00Z"));
    assert.match(line.text, /^Brief late: /);
    assert.doesNotMatch(line.text, /^Brief waiting/);
    assert.equal(line.overdue, true);
  }
  for (const s of [{ ...waiting, overdue: false }, { ...base, overdue: false }]) {
    assert.doesNotMatch(f.briefStatusLine(s, "Asia/Dubai").text, /late/i);
  }
});
check("briefStatusLine: nothing recorded yet", () => {
  const s = { first_published_at: null, published_at: null, age_hours: null, today: false, overdue: false, due: "09:00" };
  assert.deepEqual(f.briefStatusLine(s, "Asia/Dubai"), { text: "No complete brief yet today", overdue: false });
  assert.equal(f.briefStatusLine({ ...s, overdue: true }, "Asia/Dubai").text, "Brief late: no complete brief yet today (due by 09:00)");
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
  assert.equal(f.humanizeMetric("glucose_cgm"), "Glucose (CGM)");      // B15: the CGM series
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
// B11 (owner decision D4): a merged steps day says which devices fed it.
check("mergedSourceLabel reads two feeding devices as Watch + iPhone", () => {
  assert.equal(f.mergedSourceLabel({ apple_watch_ultra: 1200, iphone: 300 }), "Watch + iPhone");
  assert.equal(f.mergedSourceLabel({ apple_watch_6_legacy: 900, iphone: 12.5 }), "Watch + iPhone");
  assert.equal(f.mergedSourceLabel({ apple_watch_ultra: 1200 }), null);          // one device: the chip names it
  assert.equal(f.mergedSourceLabel({ apple_watch_ultra: 0, iphone: 300 }), null); // the watch added nothing
  assert.equal(f.mergedSourceLabel(null), null);
  assert.equal(f.mergedSourceLabel(undefined), null);
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
