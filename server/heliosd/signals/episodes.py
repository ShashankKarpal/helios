"""Main-sleep episodes per device (fix program B1 and B2; Wave 2 group A).

Before Wave 2 every sleep reader bucketed stage rows by the date their END
fell on and summed them: a night that began before midnight was split across
two dates, a nap was added to the night, and an overlapping near-duplicate of
a session (the same night written twice, new ids, edges shifted by seconds)
counted twice (audit K2, S1, S6, T2). This module is the one place a night is
built; the sleep_duration daily value, the stages card, the sleep report, the
context window and (through the daily values) baselines and grades all read
what it builds.

The rule (design section B1 (c)):
- Input: each device's eligible sleep_analysis rows with end_ts in
  [start - 1 day, end + 1 day]. Rows of type asleep, core, deep, rem and awake
  chain by start; a row that starts more than EPISODE_GAP_MIN minutes after the
  latest end so far starts a new episode. in_bed rows never chain (the
  iPhone's in-bed spans would merge separate sleeps).
- Inside an episode, a sweep over every row boundary gives each elementary
  segment one stage: the most recently ingested covering row wins, ties by
  stage precedence deep, rem, core, asleep, awake. Asleep time is the time in
  asleep-type segments, so overlapping near-duplicate rows count once and the
  stage minutes always sum to the time the episode's rows cover. in_bed and
  awake never count as sleep.
- Wake date: the reporting-zone date of the last asleep instant. The main
  episode of a (device, wake date) is the one with the most asleep time, and
  it needs at least MIN_MAIN_HOURS; other episodes are naps or fragments and
  count nowhere.
- In bed: the union of the same device's in_bed rows that overlap
  [start - 3 h, end + 3 h]; None when the device writes none (Apple since 2024).
- Keys: Whoop's API night (sample wh:sleep_duration:sleep:<id>, sync path
  whoop_live) is key whoop and is not built here; Whoop's HealthKit stage rows
  build key whoop:healthkit (B2); every other device builds its registry key
  (episode_key).
All times are wall times in the reporting zone, like samples.start_ts.

The independent check is the SQL oracle of the design (section 4): it chains
the same rows the same way and takes the union of the asleep-type rows, so the
two agree on every night except where an awake row ingested later than an
asleep row overlaps it (the sweep counts that stretch as awake; the oracle
cannot see ingestion order).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NamedTuple

from heliosd.store import db
from heliosd.trust.schema import HEALTHKIT_QUALIFIER, base_device

EPISODE_GAP_MIN = 60     # a gap longer than this between chained rows starts a new episode
MIN_MAIN_HOURS = 3.0     # a main episode holds at least this much asleep time (owner question Q3)
IN_BED_MARGIN_H = 3      # in_bed rows this close to the episode are its time in bed

ASLEEP_STAGES = ("deep", "rem", "core", "asleep")
CHAINED_STAGES = ASLEEP_STAGES + ("awake",)
# Sweep precedence between rows ingested at the same instant (higher wins).
STAGE_RANK = {"deep": 5, "rem": 4, "core": 3, "asleep": 2, "awake": 1}
# Devices whose night value comes from their own API. Their HealthKit stage
# rows are a copy of a night the API already reports, so they build the
# qualified key <device>:healthkit (B2) and never stand in as the API night.
HEALTHKIT_COPY_DEVICES = frozenset({"whoop"})

_US = timedelta(microseconds=1)
_US_PER_MIN = 60_000_000
_US_PER_H = 3_600_000_000


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
    awake_min: float                 # awake segments of the episode's rows, never sleep
    in_bed_start: datetime | None    # union of the device's in_bed rows near the episode; None without them
    in_bed_end: datetime | None
    in_bed_h: float | None
    n_rows: int                      # sleep_analysis rows chained into the episode
    overlap_removed_min: float       # minutes the sweep removed as overlap (near-duplicate rows)


class _Row(NamedTuple):
    start: datetime
    end: datetime
    stage: str
    ingested: datetime


def episode_key(device_key: str) -> str:
    """The key a device's stage rows build: <device>:healthkit for a device
    whose night comes from its own API (HEALTHKIT_COPY_DEVICES), else the
    registry key. Accepts a registry key or a qualified key."""
    base = base_device(device_key)
    return f"{base}:{HEALTHKIT_QUALIFIER}" if base in HEALTHKIT_COPY_DEVICES else base


def _read_rows(conn, first: date, last: date, devices: list[str] | None) -> dict[str, list[_Row]]:
    """Each device's eligible sleep rows ending in [first, last], by start."""
    where, params = "", [first, last]
    if devices is not None:
        where = f" AND device_key IN ({', '.join(['?'] * len(devices))})"
        params += devices
    out: dict[str, list[_Row]] = {}
    for dk, s, e, stage, ing in db.fetchall(conn, f"""
            SELECT device_key, start_ts, end_ts, text_value, ingested_at FROM eligible_samples
            WHERE metric = 'sleep_analysis' AND end_ts IS NOT NULL
              AND text_value IN ('asleep', 'core', 'deep', 'rem', 'awake', 'in_bed')
              AND CAST(end_ts AS DATE) BETWEEN ? AND ?{where}
            ORDER BY device_key, start_ts, end_ts, sample_id""", params):
        out.setdefault(dk, []).append(_Row(s, e, stage, ing or datetime.min))
    return out


def _chains(rows: list[_Row]) -> list[list[_Row]]:
    """Rows sorted by (start, end), cut where a row starts more than
    EPISODE_GAP_MIN after the latest end so far (the oracle's rule)."""
    gap = timedelta(minutes=EPISODE_GAP_MIN)
    out: list[list[_Row]] = []
    reach: datetime | None = None
    for r in rows:
        if reach is None or r.start > reach + gap:
            out.append([])
            reach = r.end
        out[-1].append(r)
        reach = max(reach, r.end)
    return out


def _sweep(chain: list[_Row]) -> tuple[dict[str, int], datetime | None, datetime | None, int]:
    """One stage per elementary segment of the chain: the most recently
    ingested covering row wins, ties by STAGE_RANK. Returns (microseconds per
    stage, first asleep instant, last asleep instant, microseconds of overlap
    removed: the rows' own lengths minus the time they cover)."""
    bounds = sorted({r.start for r in chain} | {r.end for r in chain})
    per = dict.fromkeys(CHAINED_STAGES, 0)
    first = last = None
    active: list[_Row] = []
    i = 0
    for a, b in zip(bounds, bounds[1:]):
        while i < len(chain) and chain[i].start <= a:
            active.append(chain[i])
            i += 1
        # Every row end is a boundary, so a row that reaches past `a` covers [a, b].
        active = [r for r in active if r.end > a]
        if not active:
            continue
        win = max(active, key=lambda r: (r.ingested, STAGE_RANK[r.stage]))
        per[win.stage] += (b - a) // _US
        if win.stage in ASLEEP_STAGES:
            first = a if first is None else first
            last = b
    raw = sum((r.end - r.start) // _US for r in chain)
    return per, first, last, raw - sum(per.values())


def _union_us(rows: list[_Row]) -> int:
    total, cur_s, cur_e = 0, None, None
    for r in sorted(rows):
        if cur_e is None or r.start > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s) // _US
            cur_s, cur_e = r.start, r.end
        else:
            cur_e = max(cur_e, r.end)
    if cur_e is not None:
        total += (cur_e - cur_s) // _US
    return total


def _main_of_device(key: str, rows: list[_Row]) -> dict[date, Episode]:
    """{wake date: main episode} of one device's rows (sorted by start)."""
    chained = [r for r in rows if r.stage in CHAINED_STAGES]
    best: dict[date, tuple] = {}
    for chain in _chains(chained):
        per, first, last, removed = _sweep(chain)
        asleep = sum(per[s] for s in ASLEEP_STAGES)
        if not asleep or asleep < MIN_MAIN_HOURS * _US_PER_H:
            continue
        wake = last.date()
        # The most asleep time wins; an exact tie goes to the later episode.
        if wake not in best or (asleep, last) > (best[wake][0], best[wake][2]):
            best[wake] = (asleep, first, last, per, len(chain), removed)
    beds = [r for r in rows if r.stage == "in_bed"]
    margin = timedelta(hours=IN_BED_MARGIN_H)
    out: dict[date, Episode] = {}
    for wake, (asleep, first, last, per, n_rows, removed) in best.items():
        near = [r for r in beds if r.start <= last + margin and r.end >= first - margin]
        bed_s = min(r.start for r in near) if near else None
        bed_e = max(r.end for r in near) if near else None
        out[wake] = Episode(
            device_key=key, start=first, end=last, wake_date=wake, asleep_h=asleep / _US_PER_H,
            deep_min=per["deep"] / _US_PER_MIN, rem_min=per["rem"] / _US_PER_MIN, core_min=per["core"] / _US_PER_MIN,
            asleep_plain_min=per["asleep"] / _US_PER_MIN, awake_min=per["awake"] / _US_PER_MIN,
            in_bed_start=bed_s, in_bed_end=bed_e, in_bed_h=(_union_us(near) / _US_PER_H) if near else None,
            n_rows=n_rows, overlap_removed_min=removed / _US_PER_MIN)
    return out


def main_sleep_episodes(conn, policy, start: date, end: date,
                        devices: list[str] | None = None) -> dict[tuple[str, date], Episode]:
    """The main episode of every (key, wake date) with wake date in
    [start, end], built from eligible sleep_analysis rows by the rule in the
    module docstring. `devices` limits the keys built (registry keys, or
    whoop:healthkit for Whoop's HealthKit stage rows; a key that is not a
    device's episode key, such as plain whoop, builds nothing); None builds
    every device with eligible rows in the window. A (key, wake date) without a
    main episode (no episode reaches MIN_MAIN_HOURS) is absent from the result.
    Consumers: _rows_sleep (sleep_duration rows, detail start, end, window,
    basis), sleep_stages.nightly_stages, sleep_report, point_wake_dates.
    `policy` is part of the contract; times are already reporting-zone walls."""
    read = None
    if devices is not None:
        read = sorted({base_device(k) for k in devices if episode_key(k) == k})
        if not read:
            return {}
    out: dict[tuple[str, date], Episode] = {}
    for dk, rows in _read_rows(conn, start - timedelta(days=1), end + timedelta(days=1), read).items():
        key = episode_key(dk)
        for wake, ep in _main_of_device(key, rows).items():
            if start <= wake <= end:
                out[(key, wake)] = ep
    return out


def point_wake_dates(conn, policy, device_key: str, instants: list[datetime]) -> list[date | None]:
    """For each instant (a reporting-zone wall time, as samples.start_ts), the
    wake date of `device_key`'s main episode that contains it
    (start <= instant <= end), or None when no main episode of that device
    contains it; same order and length as `instants`. Used by the sleep_end
    day basis for point samples (design B3): Apple respiratory rate at 23:00
    on D-1 and 03:00 on D both file on D, and with sample_context sleep_only
    a point that no main episode contains is dropped. The episodes are the
    device's own stage rows (whoop and whoop:healthkit both read Whoop's
    HealthKit stage rows: its API night has no stage rows)."""
    if not instants:
        return []
    key = episode_key(device_key)
    eps = sorted(main_sleep_episodes(conn, policy, min(instants).date(), max(instants).date() + timedelta(days=1),
                                     devices=[key]).values(), key=lambda e: e.start)
    starts = [e.start for e in eps]
    out: list[date | None] = []
    for t in instants:
        i = bisect_right(starts, t) - 1          # main episodes of one device never overlap
        out.append(eps[i].wake_date if i >= 0 and t <= eps[i].end else None)
    return out
