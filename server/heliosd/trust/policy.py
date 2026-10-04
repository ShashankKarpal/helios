"""Metric policy: the owner's device source of truth, loaded as data."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from heliosd.config import load_metric_policy

# Effective defaults for metrics that do not set `agg` (plan v2 section 4.3).
AGG_SUM = {"steps", "active_energy", "basal_energy", "dietary_energy"}
AGG_LAST = {"body_mass", "body_fat_pct", "lean_mass", "bmi", "vo2max",
            "recovery_score", "strain", "sleep_need", "resting_hr"}
AGGS = ("sum", "avg", "last", "min", "max")
DAY_BASES = ("calendar", "sleep_end", "whoop_cycle")
# Top-level policy blocks kept as data (plan v2 4.2). MetricPolicy never drops them.
TOP_BLOCKS = ("reporting_timezone", "unknown_types", "workouts", "activity_rings", "ecg", "labs")


class MetricPolicy:
    def __init__(self, cfg: dict | None = None, default_tz: str | None = None):
        cfg = cfg or load_metric_policy()
        self.metrics: dict[str, dict] = cfg.get("metrics", {})
        self.baseline: dict = cfg.get("baseline", {})
        self.confidence: dict = cfg.get("confidence", {})
        # Informational external feeds (files other local apps write), watched
        # for freshness and optionally ingested as `system` events. Declared
        # under `sources:`; the public default has none, the HELIOS_HOME
        # overlay adds the owner's. Each: key, label, path, cadence_hours,
        # optional ts_field, optional ingest: events.
        self.sources: list[dict] = list(cfg.get("sources", []) or [])
        self.blocks: dict = {k: cfg[k] for k in TOP_BLOCKS if k in cfg}
        # Reporting zone: policy block, else the daemon's owner timezone, else
        # UTC. Every reporting date and wall time comes from this zone, never
        # from the Mac's clock (that is what produced the twins).
        self.reporting_timezone: str = str(cfg.get("reporting_timezone") or default_tz or "UTC")
        self.zone: ZoneInfo = ZoneInfo(self.reporting_timezone)
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
        """Explicit `agg` key wins; otherwise the historical defaults."""
        explicit = self.get(metric).get("agg")
        if explicit:
            return str(explicit)
        if metric in AGG_SUM:
            return "sum"
        if metric in AGG_LAST:
            return "last"
        return "avg"

    def daily(self, metric: str) -> bool:
        """Whether the metric gets a canonical daily value. Default: every
        metric except sleep_analysis (raw stages; sleep_duration is the day)."""
        v = self.get(metric).get("daily")
        if v is None:
            return metric != "sleep_analysis"
        return bool(v)

    def day_basis(self, metric: str) -> str:
        v = self.get(metric).get("day_basis")
        if v:
            return str(v)
        return "sleep_end" if metric == "sleep_duration" else "calendar"

    def rank(self, metric: str, device_key: str) -> int | None:
        """0 = most trusted. None = device not in this metric's priority list."""
        pr = self.priority(metric)
        return pr.index(device_key) if device_key in pr else None

    def sync_registry(self, conn) -> int:
        """Mirror the loaded policy into metric_registry so SQL (the
        eligibility view) can join registration and units. Replaces the
        table's content; called at daemon start and by every test fixture."""
        rows = [[name, m.get("hk"), m.get("unit", ""), self.agg(name), self.daily(name),
                 self.day_basis(name), self.direction(name), m.get("trust")]
                for name, m in self.metrics.items()]
        from heliosd.store import db
        with db.transaction(conn) as c:
            c.execute("DELETE FROM metric_registry")
            if rows:
                c.executemany("INSERT INTO metric_registry (metric, hk, unit, agg, daily, day_basis, direction, trust) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    @property
    def windows(self) -> list[int]:
        return list(self.baseline.get("windows_days", [30, 60, 90]))

    @property
    def max_window(self) -> int:
        return max(self.windows) if self.windows else 30

    @property
    def default_window(self) -> int:
        return int(self.baseline.get("default_window", 30))

    @property
    def min_days(self) -> int:
        return int(self.baseline.get("min_days", 7))

    @property
    def mad_k(self) -> float:
        return float(self.baseline.get("mad_flag_multiplier", 1.5))
