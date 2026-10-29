#!/usr/bin/env node
// Checks for the pure helpers behind History's Workouts and ECG segments
// (src/lib/history.ts, rank 5, 2026-10-29). Wall times are read from the
// offset the server sends, never from the process zone: run under several TZ
// values, as check-format.mjs. Synthetic values only.
import assert from "node:assert/strict";
import * as h from "../src/lib/history.ts";

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}

check("wall time is read from the string, not the process zone", () => {
  assert.deepEqual(h.wallParts("2026-01-07T08:00:00+04:00"), { date: "2026-01-07", time: "08:00", offset: "+04:00" });
  assert.deepEqual(h.wallParts("2026-01-07T23:59:59-07:00"), { date: "2026-01-07", time: "23:59", offset: "-07:00" });
  assert.deepEqual(h.wallParts("2026-01-07T08:00:00Z"), { date: "2026-01-07", time: "08:00", offset: "+00:00" });
  assert.equal(h.wallParts("not a time"), null);
  assert.equal(h.wallParts(null), null);
});
check("day and month labels are fixed English, computed from the calendar date", () => {
  assert.equal(h.dayLabel("2026-01-07"), "Wed 7 Jan 2026");
  assert.equal(h.dayLabel("2024-02-29"), "Thu 29 Feb 2024");
  assert.equal(h.monthLabel("2026-06-24"), "June 2026");
  assert.equal(h.dayLabel("bad"), "bad");
});
check("durations", () => {
  assert.equal(h.formatDuration(2520), "42 min");
  assert.equal(h.formatDuration(3900), "1 h 5 min");
  assert.equal(h.formatDuration(7200), "2 h");
  assert.equal(h.formatDuration(45), "45 s");
  assert.equal(h.formatDuration(30), "30 s");
  assert.equal(h.formatDuration(null), null);
});
check("distance keeps the recorded unit; metres over 1000 read as km", () => {
  assert.equal(h.formatDistance(3.125, "km"), "3.13 km");
  assert.equal(h.formatDistance(850, "m"), "850 m");
  assert.equal(h.formatDistance(1500, "m"), "1.50 km");
  assert.equal(h.formatDistance(1.94, "mi"), "1.94 mi");
  assert.equal(h.formatDistance(500, "yd"), "500 yd");
  assert.equal(h.formatDistance(null, "km"), null);
  assert.equal(h.formatDistance(0, "km"), "0.00 km");
});
check("energy and heart rate; a missing value stays missing, never 0", () => {
  assert.equal(h.formatEnergy(186.5, "kcal"), "187 kcal");
  assert.equal(h.formatEnergy(186.5, "Cal"), "187 kcal");
  assert.equal(h.formatEnergy(null, "kcal"), null);
  assert.equal(h.formatHr(118.5), "119 bpm");
  assert.equal(h.formatHr(null), null);
});
check("archive and newer-records lines (owner wording: never call raw records workouts)", () => {
  assert.equal(
    h.archiveLine(1105, "2026-06-24T10:00:00Z", "UTC", "session", "sessions"),
    "1,105 sessions, from the Apple Health export (to 24 Jun 2026)",
  );
  assert.equal(h.archiveLine(1, "2026-02-23T06:15:00Z", "UTC", "recording", "recordings"),
    "1 recording, from the Apple Health export (to 23 Feb 2026)");
  // the date is the reporting zone's: 22:30 UTC is the next day in Dubai
  assert.match(h.archiveLine(2, "2026-06-24T22:30:00Z", "Asia/Dubai", "session", "sessions"), /25 Jun 2026/);
  assert.equal(h.archiveLine(0, null, "UTC", "session", "sessions"), null);
  assert.equal(
    h.newerLine(100, "workout"),
    "100 newer workout records reached Helios through the Bridge and are not shown here yet. One workout can have a record from each device.",
  );
  assert.equal(h.newerLine(1, "ECG"), "1 newer ECG record reached Helios through the Bridge and is not shown here yet.");
  assert.equal(h.newerLine(0, "ECG"), null);
  for (const kind of ["workout", "ECG"]) assert.doesNotMatch(h.newerLine(5, kind), /\b(workouts|ECGs) (reached|are)\b/);
});
check("HealthKit names read mechanically: prefix off, words split", () => {
  assert.equal(h.hkLabel("HKQuantityTypeIdentifierActiveEnergyBurned"), "Active energy burned");
  assert.equal(h.hkLabel("HKWorkoutEventTypeSegment"), "Segment");
  assert.equal(h.hkLabel("HKQuantityTypeIdentifierDistanceWalkingRunning"), "Distance walking running");
  assert.equal(h.hkLabel("HKIndoorWorkout"), "Indoor workout");
  assert.equal(h.hkLabel("WHOOP Strain"), "WHOOP strain");
  assert.equal(h.hkLabel(null), "");
});
check("a device label that is still a store key reads as words; a real label is kept", () => {
  assert.equal(h.deviceLabel("apple_watch_ultra"), "Apple Watch Ultra");
  assert.equal(h.deviceLabel("whoop"), "Whoop");
  assert.equal(h.deviceLabel("Upper-arm strap (Zepp)"), "Upper-arm strap (Zepp)");
  assert.equal(h.deviceLabel("Whoop (Apple Health copy)"), "Whoop (Apple Health copy)");
  assert.equal(h.deviceLabel(""), "Unknown source");
});
check("the route outline is scaled into the box, aspect kept, no coordinate in or out", () => {
  const pts = h.outlinePoints([[0, 0], [1000, 500], [500, 250]], 1000, 500, 200, 10);
  assert.equal(pts, "10,10 190,100 100,55");
  assert.equal(h.outlinePoints([], 0, 0, 200, 10), "");
  assert.equal(h.outlineHeight(1000, 500, 200, 10), 110);
  assert.equal(h.outlineHeight(0, 0, 200, 10), 20);
});
check("a narrow outline is centred across the box (review)", () => {
  assert.equal(h.outlinePoints([[0, 1000], [0, 0]], 0, 1000, 200, 10), "100,190 100,10");
});
check("months follow the reporting zone, so the newest-first list never repeats a month (review)", () => {
  assert.equal(h.monthOfInstant("2026-06-30T21:00:00Z", "Asia/Dubai"), "July 2026");
  assert.equal(h.monthOfInstant("2026-06-30T21:00:00Z", "UTC"), "June 2026");
  assert.equal(h.monthOfInstant(null, "UTC"), "Time not recorded");
});
check("the first page decides whether the older pages still follow it (review)", () => {
  assert.equal(h.olderStillFollow("cur-a", "cur-a"), true);
  assert.equal(h.olderStillFollow("cur-b", "cur-a"), false);
  assert.equal(h.olderStillFollow(null, "cur-a"), false);
  assert.equal(h.olderStillFollow("cur-a", undefined), false);
});
check("ECG rows leave room for 3 mV either side of the baseline (review)", () => {
  assert.equal(h.ECG_ROW.height, 240);
});
check("the ECG is drawn in rows of 10 s at a fixed scale, every sample kept", () => {
  const rate = 512;
  const mv = Array.from({ length: rate * 30 }, (_, i) => (i % 512 === 0 ? 1000 : 0));
  const rows = h.waveformRows(mv, rate, 10);
  assert.equal(rows.length, 3);
  for (const r of rows) assert.equal(r.points.split(" ").length, 5120);
  // 100 units per second across, 40 units per millivolt up (25 mm/s, 10 mm/mV)
  assert.equal(h.ECG_ROW.width, 1000);
  assert.equal(h.ECG_ROW.unitsPerMv, 40);
  const first = rows[0].points.split(" ")[0].split(",").map(Number);
  const second = rows[0].points.split(" ")[1].split(",").map(Number);
  assert.equal(first[0], 0);
  assert.ok(Math.abs(second[0] - 100 / rate) < 0.01);
  // a 1000 uV spike is 40 units above the row's baseline (its median, 0 here)
  assert.equal(h.ECG_ROW.height / 2 - first[1], 40);
  // a partial last row: 514.039 Hz for 30 s
  const odd = h.waveformRows(new Array(15421).fill(0), 514.039, 10);
  assert.equal(odd.length, 3);
  assert.equal(odd.reduce((n, r) => n + r.points.split(" ").length, 0), 15421);
  assert.equal(odd[0].startS, 0);
  assert.equal(odd[2].startS, 20);
  assert.deepEqual(h.waveformRows([], 512, 10), []);
});
check("the ECG grid is 0.2 s by 0.5 mV", () => {
  assert.equal(h.ECG_ROW.gridX, 20);
  assert.equal(h.ECG_ROW.gridY, 20);
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
console.log(`${checks.length - failed} of ${checks.length} history checks passed`);
process.exit(failed ? 1 : 0);
