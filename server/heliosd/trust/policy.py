"""Metric policy: the owner's device source of truth, loaded as data."""

from __future__ import annotations

from heliosd.config import load_metric_policy

# Per-day aggregation of raw samples into one value per device per day.
AGG_SUM = {"steps", "active_energy", "basal_energy", "dietary_energy"}
AGG_LAST = {"body_mass", "body_fat_pct", "lean_mass", "bmi", "vo2max",
            "recovery_score", "strain", "sleep_need", "resting_hr"}
# everything else: mean of the day's samples


class MetricPolicy:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_metric_policy()
        self.metrics: dict[str, dict] = cfg.get("metrics", {})
        self.baseline: dict = cfg.get("baseline", {})
        self.confidence: dict = cfg.get("confidence", {})
        self.hk_to_metric: dict[str, str] = {
            m["hk"]: name for name, m in self.metrics.items() if m.get("hk")
        }

    def get(self, metric: str) -> dict:
        return self.metrics.get(metric, {})

    def priority(self, metric: str) -> list[str]:
        return list(self.get(metric).get("priority", []))

    def direction(self, metric: str) -> str:
        return self.get(metric).get("direction", "none")

    def unit(self, metric: str) -> str:
        return self.get(metric).get("unit", "")

    def cadence_hours(self, metric: str) -> float:
        return float(self.get(metric).get("cadence_hours", 26))

    def agg(self, metric: str) -> str:
        if metric in AGG_SUM:
            return "sum"
        if metric in AGG_LAST:
            return "last"
        return "avg"

    def rank(self, metric: str, device_key: str) -> int | None:
        """0 = most trusted. None = device not in this metric's priority list."""
        pr = self.priority(metric)
        return pr.index(device_key) if device_key in pr else None

    @property
    def windows(self) -> list[int]:
        return list(self.baseline.get("windows_days", [30, 60, 90]))

    @property
    def default_window(self) -> int:
        return int(self.baseline.get("default_window", 30))

    @property
    def min_days(self) -> int:
        return int(self.baseline.get("min_days", 7))

    @property
    def mad_k(self) -> float:
        return float(self.baseline.get("mad_flag_multiplier", 1.5))
