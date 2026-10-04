"""Synthetic fixture generator. No real health data ever enters the repo:
these are plausible-shaped random series with seeded reproducibility.

Two shapes, matching the two real writers:
- synth_batch(): a Bridge payload (HealthKit samples only, each with a uuid).
  Since Phase 1a a payload cannot name its own metric, so Whoop's direct
  nightly values are no longer smuggled in here.
- synth_whoop_direct(): the per-night records the Whoop API puller stores
  through heliosd.ingest.whoop.store_direct_sample (record-keyed, SCORED).
Timestamps are naive and are read as wall time in the policy's reporting zone.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta


def _nights(days: int, seed: int, end_day: date):
    rng = random.Random(seed)
    for i in range(days, 0, -1):
        d = end_day - timedelta(days=i - 1)
        wake = datetime.combine(d, datetime.min.time()) + timedelta(hours=7, minutes=rng.randint(-40, 40))
        bed = wake - timedelta(hours=rng.uniform(6.2, 8.6))
        yield d, bed, wake, rng


def synth_batch(days: int = 60, seed: int = 7, end_day: date | None = None) -> dict:
    """A Bridge-shaped payload covering `days` of multi-device synthetic data."""
    end_day = end_day or date.today()
    samples: list[dict] = []

    def add(hk_type, value, unit, start: datetime, end: datetime, source):
        samples.append({"hk_type": hk_type, "value": value, "unit": unit,
                        "start": start.isoformat(), "end": end.isoformat(),
                        "source_name": source, "uuid": f"u-{seed}-{len(samples)}"})

    for d, bed, wake, rng in _nights(days, seed, end_day):
        # Whoop-style sleep stages written to HealthKit (source WHOOP)
        t = bed
        while t < wake - timedelta(minutes=30):
            stage = rng.choices(
                ["HKCategoryValueSleepAnalysisAsleepCore", "HKCategoryValueSleepAnalysisAsleepDeep",
                 "HKCategoryValueSleepAnalysisAsleepREM", "HKCategoryValueSleepAnalysisAwake"],
                weights=[5, 2, 2.5, 0.7])[0]
            dur = timedelta(minutes=rng.randint(20, 70))
            seg_end = min(t + dur, wake)
            add("HKCategoryTypeIdentifierSleepAnalysis", stage, "min", t, seg_end, "WHOOP")
            t += dur

        # Zepp dense HR (sampled here hourly to keep fixtures small)
        for h in range(0, 24, 1):
            ts = datetime.combine(d, datetime.min.time()) + timedelta(hours=h)
            hr = rng.gauss(62 if 1 <= h <= 6 else 78, 6)
            add("HKQuantityTypeIdentifierHeartRate", round(max(45, hr), 1), "count/min",
                ts, ts + timedelta(minutes=1), "Zepp")

        # Apple Watch Ultra daily markers (curly apostrophe on purpose)
        awu = "Owner’s Ultra 1"
        add("HKQuantityTypeIdentifierRestingHeartRate", round(rng.gauss(57, 2.5), 0),
            "count/min", wake, wake, awu)
        for _ in range(3):
            ts = bed + timedelta(hours=rng.uniform(0.5, 6.5))
            add("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", round(rng.gauss(52, 9), 1),
                "ms", ts, ts, awu)
        add("HKQuantityTypeIdentifierRespiratoryRate", round(rng.gauss(15.2, 0.7), 1),
            "count/min", bed + timedelta(hours=2), bed + timedelta(hours=2), awu)
        add("HKQuantityTypeIdentifierOxygenSaturation", round(rng.gauss(0.97, 0.008), 3),
            "%", bed + timedelta(hours=3), bed + timedelta(hours=3), awu)
        add("HKQuantityTypeIdentifierAppleSleepingWristTemperature", round(rng.gauss(34.6, 0.25), 2),
            "degC", bed + timedelta(hours=4), bed + timedelta(hours=4), awu)
        add("HKQuantityTypeIdentifierActiveEnergyBurned", round(rng.gauss(650, 140), 0),
            "kcal", wake, wake + timedelta(hours=14), awu)

        # iPhone steps (priority source) + Watch steps (corroboration)
        steps = max(1500, rng.gauss(7200, 2300))
        add("HKQuantityTypeIdentifierStepCount", round(steps), "count",
            wake, wake + timedelta(hours=14), "Owner's 16 Pro Max")
        add("HKQuantityTypeIdentifierStepCount", round(steps * rng.uniform(0.85, 1.1)), "count",
            wake, wake + timedelta(hours=14), awu)

        # Scale, weekly
        if d.weekday() == 0:
            add("HKQuantityTypeIdentifierBodyMass", round(rng.gauss(109.2, 0.6), 1), "kg",
                wake, wake, "Zepp Life")

        # An ignored source that must be filtered out
        add("HKQuantityTypeIdentifierHeartRate", 200, "count/min", wake, wake, "Athlytic")

    return {"batch_id": f"synth-{seed}", "device": "test", "sent_at": datetime.now().isoformat(),
            "samples": samples, "deleted": [], "anchors": {}}


def synth_whoop_direct(days: int = 60, seed: int = 7, end_day: date | None = None) -> list[dict]:
    """Whoop API-shaped nightly records: one SCORED sleep per night with the
    asleep hours the stage copy above implies. Each carries a native record id."""
    end_day = end_day or date.today()
    out = []
    for d, bed, wake, rng in _nights(days, seed, end_day):
        # Re-derive the staged asleep minutes deterministically (same rng stream
        # as synth_batch, so the direct value agrees with the HealthKit copy).
        t, asleep = bed, 0.0
        while t < wake - timedelta(minutes=30):
            stage = rng.choices(["core", "deep", "rem", "awake"], weights=[5, 2, 2.5, 0.7])[0]
            dur = timedelta(minutes=rng.randint(20, 70))
            seg_end = min(t + dur, wake)
            if stage != "awake":
                asleep += (seg_end - t).total_seconds() / 60.0
            t += dur
        out.append({"kind": "sleep", "id": f"sl-{seed}-{d.isoformat()}", "nap": False,
                    "score_state": "SCORED", "start": bed, "end": wake,
                    "asleep_hours": round(asleep / 60.0, 2)})
    return out


def store_whoop_direct(conn, records: list[dict], zone) -> int:
    """Store synth_whoop_direct records the way the puller does (record-keyed,
    inside one transaction). Returns rows written."""
    from heliosd.ingest.whoop import store_direct_sample
    from heliosd.store import db
    n = 0
    with db.transaction(conn) as c:
        for r in records:
            store_direct_sample(c, "sleep_duration", f"sleep:{r['id']}", r["asleep_hours"], "h",
                                r["start"].replace(tzinfo=zone), r["end"].replace(tzinfo=zone), zone,
                                score_state=r["score_state"])
            n += 1
    return n
