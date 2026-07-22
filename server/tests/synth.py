"""Synthetic fixture generator. No real health data ever enters the repo:
these are plausible-shaped random series with seeded reproducibility."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta


def synth_batch(days: int = 60, seed: int = 7, end_day: date | None = None) -> dict:
    """A Bridge-shaped payload covering `days` of multi-device synthetic data."""
    rng = random.Random(seed)
    end_day = end_day or date.today()
    samples: list[dict] = []

    def add(hk_type, value, unit, start: datetime, end: datetime, source):
        samples.append({"hk_type": hk_type, "value": value, "unit": unit,
                        "start": start.isoformat(), "end": end.isoformat(),
                        "source_name": source, "uuid": f"u-{len(samples)}"})

    for i in range(days, 0, -1):
        d = end_day - timedelta(days=i - 1)
        wake = datetime.combine(d, datetime.min.time()) + timedelta(hours=7, minutes=rng.randint(-40, 40))
        bed = wake - timedelta(hours=rng.uniform(6.2, 8.6))

        # Whoop-style sleep stages written to HealthKit (source WHOOP)
        t = bed
        while t < wake - timedelta(minutes=30):
            stage = rng.choices(
                ["HKCategoryValueSleepAnalysisAsleepCore", "HKCategoryValueSleepAnalysisAsleepDeep",
                 "HKCategoryValueSleepAnalysisAsleepREM", "HKCategoryValueSleepAnalysisAwake"],
                weights=[5, 2, 2.5, 0.7])[0]
            dur = timedelta(minutes=rng.randint(20, 70))
            add("HKCategoryTypeIdentifierSleepAnalysis", stage, "min", t, min(t + dur, wake), "WHOOP")
            t += dur

        # Zepp dense HR (sampled here hourly to keep fixtures small)
        for h in range(0, 24, 1):
            ts = datetime.combine(d, datetime.min.time()) + timedelta(hours=h)
            hr = rng.gauss(62 if 1 <= h <= 6 else 78, 6)
            add("HKQuantityTypeIdentifierHeartRate", round(max(45, hr), 1), "count/min",
                ts, ts + timedelta(minutes=1), "Zepp")

        # Apple Watch Ultra daily markers (curly apostrophe on purpose)
        awu = "Shashank’s Ultra 1"
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
            wake, wake + timedelta(hours=14), "Shashank's 16 Pro Max")
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
