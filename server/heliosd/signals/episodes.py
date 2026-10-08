"""Main-sleep episodes per device (fix program B1 and B2; Wave 2 group A).

Interface stub from the Wave 2 scaffold (S0), so the sleep work (group A) and
the day-basis work (group B: sleep_end points) can code against one contract
in parallel. Nothing calls it yet; both functions raise NotImplementedError
until group A fills them in (design.md section B1 (c)).

The rule, from the design:
- Input: each device's eligible sleep_analysis rows with end_ts in
  [start - 1 day, end + 1 day]. Rows of type asleep, core, deep, rem and awake
  chain by start; a gap over EPISODE_GAP_MIN minutes starts a new episode.
  in_bed rows never chain (the iPhone's in-bed spans would merge separate
  sleeps).
- Inside an episode, a sweep over every row boundary gives each elementary
  segment one stage: the most recently ingested covering row wins, ties by
  stage precedence deep, rem, core, asleep, awake. Asleep time is the time in
  asleep-type segments, so overlapping near-duplicate rows count once and the
  stage minutes always sum to the episode. in_bed and awake never count as
  sleep.
- Wake date: the reporting-zone date of the last asleep instant. The main
  episode of a (device, wake date) is the one with the most asleep time, and
  it needs at least MIN_MAIN_HOURS; other episodes are naps or fragments and
  count nowhere.
- In bed: the union of the same device's in_bed rows that overlap
  [start - 3 h, end + 3 h]; None when the device writes none (Apple).
- Keys: Whoop's API night (sample wh:sleep_duration:sleep:<id>, sync path
  whoop_live) is key whoop and is not built here; Whoop's HealthKit stage rows
  build key whoop:healthkit (B2); every other device builds its registry key.
All times are wall times in the reporting zone, like samples.start_ts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

EPISODE_GAP_MIN = 60     # a gap longer than this between chained rows starts a new episode
MIN_MAIN_HOURS = 3.0     # a main episode holds at least this much asleep time


@dataclass(frozen=True)
class Episode:
    """One device's main sleep episode of one wake date."""

    device_key: str                  # arbitration key: a registry device key, or whoop:healthkit
    start: datetime                  # first asleep instant
    end: datetime                    # last asleep instant
    wake_date: date                  # reporting-zone date of `end`
    asleep_h: float                  # time in asleep-type segments, overlaps counted once
    deep_min: float
    rem_min: float
    core_min: float
    asleep_plain_min: float          # 'asleep' time with no stage
    awake_min: float                 # awake segments inside the episode, never sleep
    in_bed_start: datetime | None    # union of the device's in_bed rows near the episode; None without them
    in_bed_end: datetime | None
    in_bed_h: float | None
    n_rows: int                      # sleep_analysis rows chained into the episode
    overlap_removed_min: float       # minutes the sweep removed as overlap (near-duplicate rows)


def main_sleep_episodes(conn, policy, start: date, end: date,
                        devices: list[str] | None = None) -> dict[tuple[str, date], Episode]:
    """The main episode of every (key, wake date) with wake date in
    [start, end], built from eligible sleep_analysis rows by the rule in the
    module docstring. `devices` limits the keys built (registry keys, or
    whoop:healthkit for Whoop's HealthKit stage rows); None builds every
    device with eligible rows in the window. A (key, wake date) without a main
    episode (no episode reaches MIN_MAIN_HOURS) is absent from the result.
    Consumers: _rows_sleep (sleep_duration rows, detail start, end, window,
    basis), sleep_stages.nightly_stages, sleep_report, context._sleep_window."""
    raise NotImplementedError("Wave 2 group A")


def point_wake_dates(conn, policy, device_key: str, instants: list[datetime]) -> list[date | None]:
    """For each instant (a reporting-zone wall time, as samples.start_ts), the
    wake date of `device_key`'s main episode that contains it
    (start <= instant <= end), or None when no main episode of that device
    contains it; same order and length as `instants`. Used by the sleep_end
    day basis for point samples (design B3): Apple respiratory rate at 23:00
    on D-1 and 03:00 on D both file on D, and with sample_context sleep_only
    a point that no main episode contains is dropped."""
    raise NotImplementedError("Wave 2 group A")
