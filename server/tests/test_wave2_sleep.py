"""Wave 2 group A: the main-sleep episode builder (B1), the Whoop HealthKit
episode as the labelled fallback (B2) and grades after B1 (B12).

Synthetic nights only: the dates, devices and minutes are invented, and every
expected value is worked out here by hand from the rows written, never taken
from the code under test. Wall times are in the reporting zone (Asia/Dubai),
as samples.start_ts is."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from heliosd.config import load_metric_policy
from heliosd.insights.weekly_review import build_weekly_review
from heliosd.signals import context, episodes
from heliosd.signals.baselines import compute_daily_values
from heliosd.signals.sleep_report import build_sleep_report
from heliosd.signals.sleep_stages import nightly_stages
from heliosd.store import db
from heliosd.trust.policy import MetricPolicy
from heliosd.trust.registry import SourceRegistry

D = date(2025, 2, 12)              # the wake date of the main synthetic night
AWU, ZEPP, WHOOP = "apple_watch_ultra", "zepp_helio", "whoop"
ING = datetime(2025, 2, 20, 9, 0)  # default ingestion instant of a synthetic row


def T(s: str) -> datetime:
    """'02-11 22:10' -> 2025-02-11 22:10 (a reporting-zone wall time)."""
    return datetime.fromisoformat(f"2025-{s}")


def _policy(**patch) -> MetricPolicy:
    """The shipped default merged with the test fixture overlay, metrics patched."""
    cfg = load_metric_policy()
    for metric, keys in patch.items():
        cfg["metrics"][metric] = {**cfg["metrics"].get(metric, {}), **keys}
    return MetricPolicy(cfg, default_tz="Asia/Dubai")


def _env(policy: MetricPolicy | None = None):
    conn = db.connect_memory()
    policy = policy or _policy()
    policy.sync_registry(conn)
    return conn, policy, SourceRegistry()


_n = [0]


def _stages(conn, rows, device: str = AWU, ingested: datetime = ING) -> None:
    """rows: (stage, start, end) as 'MM-DD HH:MM' walls; one sleep_analysis row each."""
    out = []
    for stage, s, e in rows:
        _n[0] += 1
        s, e = T(s), T(e)
        out.append([f"hk:t{_n[0]}", (e - s).total_seconds() / 60.0, stage, s, e, f"Synthetic {device}", device, ingested])
    db.insert_batch(conn, "INSERT INTO samples (sample_id, metric, value, text_value, unit, start_ts, end_ts, "
                          "source_name, device_key, sync_path, ingested_at) "
                          "VALUES (?, 'sleep_analysis', ?, ?, 'min', ?, ?, ?, ?, 'bridge', ?)", out)


def _api_night(conn, rid: str, start: str, end: str, hours: float, path: str = "whoop_live") -> None:
    """A Whoop API night as the puller stores it (record-keyed, filed by its end)."""
    db.execute(conn, "INSERT INTO samples (sample_id, metric, value, unit, start_ts, end_ts, source_name, device_key, "
                     "sync_path, score_state) VALUES (?, 'sleep_duration', ?, 'h', ?, ?, 'WHOOP', 'whoop', ?, 'SCORED')",
               [f"wh:sleep_duration:sleep:{rid}", hours, T(start), T(end), path])


def _compute(conn, policy, reg, first: date, last: date) -> None:
    compute_daily_values(conn, policy, reg, first, last, now=datetime.combine(last + timedelta(days=1),
                                                                             datetime.min.time()) + timedelta(hours=9),
                         as_of=last + timedelta(days=1))


def _nights(conn) -> dict[date, tuple]:
    """{date: (value, device_key, corroboration dict, detail dict)} of sleep_duration."""
    return {d: (v, dk, json.loads(co) if co else None, json.loads(de) if de else None)
            for d, v, dk, co, de in db.fetchall(conn, "SELECT date, value, device_key, corroboration, detail "
                                                      "FROM daily_values WHERE metric = 'sleep_duration'")}


# ---- B1: the main-sleep episode builder ----

def test_night_files_whole_under_wake_date():
    """A night that starts before midnight is one night on its wake date. The
    old end-date buckets gave 1.82 h on the bed date and 5.33 h on the wake date."""
    conn, policy, reg = _env()
    _stages(conn, [("core", "02-11 22:10", "02-11 23:59"),      # 109 min, ends on the bed date
                   ("core", "02-12 00:00", "02-12 05:20")])     # 320 min; the 1 min gap chains
    _compute(conn, policy, reg, D - timedelta(days=1), D)
    nights = _nights(conn)
    assert set(nights) == {D}                                   # nothing on the bed date
    value, device, corr, detail = nights[D]
    assert (value, device, corr) == (7.15, AWU, None)           # 429 min = 7.15 h
    assert detail == {"start": "2025-02-11T22:10:00", "end": "2025-02-12T05:20:00",
                      "window": "asleep", "basis": "episode"}


def test_overlapping_near_duplicate_rows_count_once():
    """The same session written twice (new ids, edges 45 s later, ingested
    later): the stretch both cover counts once and the newer copy decides the
    stages. The old sums gave 12.0 h."""
    conn, policy, reg = _env()
    night = [("core", "02-12 00:00:00", "02-12 02:00:00"), ("deep", "02-12 02:00:00", "02-12 03:00:00"),
             ("rem", "02-12 03:00:00", "02-12 04:00:00"), ("core", "02-12 04:00:00", "02-12 06:00:00")]
    later = [(st, (T(s) + timedelta(seconds=45)).strftime("%m-%d %H:%M:%S"),
              (T(e) + timedelta(seconds=45)).strftime("%m-%d %H:%M:%S")) for st, s, e in night]
    _stages(conn, night)
    _stages(conn, later, ingested=ING + timedelta(days=1))
    _compute(conn, policy, reg, D, D)
    assert _nights(conn)[D][:2] == (6.01, AWU)                  # 00:00:00 to 06:00:45 = 6.0125 h
    ep = episodes.main_sleep_episodes(conn, policy, D, D)[(AWU, D)]
    assert ep.asleep_h == 6.0125 and ep.n_rows == 8
    # The newer copy covers 00:00:45 to 06:00:45 and decides it; the first 45 s are the older core.
    assert (ep.deep_min, ep.rem_min, ep.core_min, ep.asleep_plain_min, ep.awake_min) == (60, 60, 240.75, 0, 0)
    assert ep.overlap_removed_min == 720 - 360.75               # the rows' own minutes minus the time covered
    assert (ep.start, ep.end) == (T("02-12 00:00:00"), T("02-12 06:00:45"))


def test_nap_is_not_the_night():
    """A 1.5 h nap ending 15:00 on the wake date is its own episode and counts
    nowhere. The old buckets added it to the night's post-midnight part (7.5 h)."""
    conn, policy, reg = _env()
    _stages(conn, [("core", "02-11 23:00", "02-12 01:00"), ("deep", "02-12 01:00", "02-12 02:00"),
                   ("core", "02-12 02:00", "02-12 06:00"),
                   ("core", "02-12 13:30", "02-12 15:00")])          # the nap, 7.5 h after the night
    _compute(conn, policy, reg, D - timedelta(days=1), D)
    assert {d: v[:2] for d, v in _nights(conn).items()} == {D: (7.0, AWU)}
    st = nightly_stages(conn, policy, D, D)[D]
    assert (st["deep_min"], st["light_min"], st["woke"]) == (60, 360, T("02-12 06:00"))


def test_short_fragment_is_not_a_main_episode():
    """A main episode needs MIN_MAIN_HOURS (3.0 h, owner question Q3) asleep: a
    2.5 h fragment alone gives no value; exactly 3.0 h does. The old code
    stored the fragment."""
    assert episodes.MIN_MAIN_HOURS == 3.0
    conn, policy, reg = _env()
    _stages(conn, [("core", "02-12 01:00", "02-12 03:30")])        # 2.5 h, wake date D
    _stages(conn, [("core", "02-13 02:00", "02-13 05:00")])        # 3.0 h, wake date D + 1
    _compute(conn, policy, reg, D, D + timedelta(days=1))
    assert {d: v[:2] for d, v in _nights(conn).items()} == {D + timedelta(days=1): (3.0, AWU)}
    assert set(episodes.main_sleep_episodes(conn, policy, D, D + timedelta(days=1))) == {(AWU, D + timedelta(days=1))}


def _regular_night(conn, wake: date, bed_h: int = 22, device: str = AWU) -> None:
    """A night of five rows from bed_h:00 on the day before `wake` to 8 h later;
    with bed_h 22: core 22:00-23:30, deep 23:30-00:30, core 00:30-03:00,
    rem 03:00-04:00, core 04:00-06:00 (8 h asleep)."""
    t0 = datetime.combine(wake - timedelta(days=1), datetime.min.time()) + timedelta(hours=bed_h)
    cuts = [0, 90, 150, 300, 360, 480]
    stages = ["core", "deep", "core", "rem", "core"]
    _stages(conn, [(st, (t0 + timedelta(minutes=a)).strftime("%m-%d %H:%M"), (t0 + timedelta(minutes=b)).strftime("%m-%d %H:%M"))
                   for st, a, b in zip(stages, cuts, cuts[1:])], device=device)


def test_context_flag_quiet_on_normal_nights():
    """Fifteen regular nights 22:00 to 06:00 give no schedule-shift flag. The
    old window spanned every row ending on a date (about 23:30 to 23:30 on
    past dates, 23:30 to 06:00 on the newest), so every morning read as a
    shift of almost nine hours."""
    conn, policy, reg = _env()
    for i in range(15):
        _regular_night(conn, D - timedelta(days=i))
    _compute(conn, policy, reg, D - timedelta(days=15), D)
    assert context._sleep_window(conn, D) == (T("02-11 22:00"), T("02-12 06:00"))
    flags = context.context_flags(conn, D, heat_months=[8])
    assert "travel_or_shifted_schedule" not in flags and "late_night" not in flags
    # A real shift still flags: the next night from 04:00 to 12:00 (midpoint 08:00, not 02:00).
    _regular_night(conn, D + timedelta(days=1), bed_h=28)
    _compute(conn, policy, reg, D + timedelta(days=1), D + timedelta(days=1))
    flags = context.context_flags(conn, D + timedelta(days=1), heat_months=[8])
    assert "travel_or_shifted_schedule" in flags and "late_night" in flags


def test_stages_card_uses_the_episode():
    """The stages card holds the whole night: the 80 core minutes before
    midnight count, and fell asleep is 22:10. The old card cut the night at
    midnight (light 230 min, fell asleep 23:30)."""
    conn, policy, reg = _env()
    _stages(conn, [("core", "02-11 22:10", "02-11 23:30"), ("deep", "02-11 23:30", "02-12 00:30"),
                   ("rem", "02-12 00:30", "02-12 01:30"), ("core", "02-12 01:30", "02-12 05:20"),
                   ("awake", "02-12 05:20", "02-12 05:30")])
    _compute(conn, policy, reg, D - timedelta(days=1), D)
    st = nightly_stages(conn, policy, D - timedelta(days=1), D)
    assert set(st) == {D}
    s = st[D]
    assert (s["device"], s["deep_min"], s["rem_min"], s["light_min"], s["awake_min"]) == (AWU, 60, 60, 310, 10)
    assert (s["fell_asleep"], s["woke"], s["window"]) == (T("02-11 22:10"), T("02-12 05:20"), "asleep")
    rep = build_sleep_report(conn, days=7, policy=policy, today=D + timedelta(days=1))
    night = rep["nights"][-1]
    assert night["stages"] == {"deep_min": 60, "rem_min": 60, "light_min": 310, "awake_min": 10}
    assert (night["fell_asleep"], night["woke"]) == ("22:10", "05:20")


def test_weekly_review_stages_follow_episodes():
    """Three nights, each with 60 deep minutes before midnight and 40 after:
    the weekly review averages 100 deep minutes over 3 nights. The old end-date
    buckets made 4 nights (60, 100, 100, 40)."""
    conn, policy, reg = _env()
    for i in range(3):
        wake = D - timedelta(days=i)
        bed = wake - timedelta(days=1)
        b, w = bed.strftime("%m-%d"), wake.strftime("%m-%d")
        _stages(conn, [("core", f"{b} 22:00", f"{b} 22:30"), ("deep", f"{b} 22:30", f"{b} 23:30"),
                       ("core", f"{b} 23:30", f"{w} 02:00"), ("deep", f"{w} 02:00", f"{w} 02:40"),
                       ("rem", f"{w} 02:40", f"{w} 03:40"), ("core", f"{w} 03:40", f"{w} 06:00")])
    _compute(conn, policy, reg, D - timedelta(days=4), D)
    sleep = build_weekly_review(conn, policy, today=D + timedelta(days=1))["data"]["sleep"]
    assert (sleep["nights"], sleep["deep_min"], sleep["rem_min"]) == (3, 100.0, 60.0)


def test_episode_rule_gap_sweep_and_in_bed():
    """The episode rule in detail: a gap of exactly EPISODE_GAP_MIN chains and a
    longer one splits; within an episode the most recently ingested row
    decides a stretch's stage (same instant: deep, rem, core, asleep, awake);
    in bed is the union of the device's in_bed rows within 3 h of the
    episode."""
    assert episodes.EPISODE_GAP_MIN == 60
    conn, policy, _ = _env()
    # Night 1 (wake D): 22:00-01:00 core, a 60 min gap, 02:00-05:00 core: one episode of 6 h.
    _stages(conn, [("core", "02-11 22:00", "02-12 01:00"), ("core", "02-12 02:00", "02-12 05:00"),
                   ("in_bed", "02-11 21:40", "02-12 01:30"), ("in_bed", "02-12 01:20", "02-12 05:30"),
                   ("in_bed", "02-12 09:00", "02-12 09:30")])         # 09:00 is more than 3 h after 05:00
    # Night 2 (wake D + 1): a 61 min gap splits; the 3.5 h part is the main episode.
    _stages(conn, [("core", "02-12 22:00", "02-13 01:00"), ("core", "02-13 02:01", "02-13 05:31")])
    eps = episodes.main_sleep_episodes(conn, policy, D, D + timedelta(days=1))
    one, two = eps[(AWU, D)], eps[(AWU, D + timedelta(days=1))]
    assert (one.start, one.end, one.asleep_h, one.n_rows) == (T("02-11 22:00"), T("02-12 05:00"), 6.0, 2)
    assert (one.in_bed_start, one.in_bed_end) == (T("02-11 21:40"), T("02-12 05:30"))
    assert one.in_bed_h == pytest.approx(7 + 50 / 60, abs=1e-9)        # the union: 21:40 to 05:30
    assert (two.start, two.asleep_h, two.n_rows, two.in_bed_h) == (T("02-13 02:01"), 3.5, 1, None)
    # Night 3 (wake D + 2): an awake row ingested after the core row it overlaps wins
    # that stretch; one ingested earlier loses; at the same instant deep beats awake.
    _stages(conn, [("core", "02-13 23:00", "02-14 03:00"), ("deep", "02-14 03:00", "02-14 04:00"),
                   ("core", "02-14 04:00", "02-14 07:00")])
    _stages(conn, [("awake", "02-14 01:00", "02-14 01:30")], ingested=ING + timedelta(hours=1))   # newer: wins
    _stages(conn, [("awake", "02-14 05:00", "02-14 05:20")], ingested=ING - timedelta(hours=1))   # older: loses
    _stages(conn, [("awake", "02-14 03:10", "02-14 03:20")])                                     # same instant: deep wins
    ep = episodes.main_sleep_episodes(conn, policy, D + timedelta(days=2), D + timedelta(days=2))[(AWU, D + timedelta(days=2))]
    assert (ep.awake_min, ep.deep_min, ep.core_min) == (30, 60, 390)
    assert ep.asleep_h == 7.5 and ep.overlap_removed_min == 60          # 30 + 20 + 10 min covered twice
    assert ep.deep_min + ep.rem_min + ep.core_min + ep.asleep_plain_min + ep.awake_min == 8 * 60


def test_point_wake_dates_follow_the_main_episode():
    """The sleep_end day basis of point samples (group B): a point inside a
    device's main episode files on that episode's wake date; a point outside
    every main episode of that device (a daytime reading, another device's
    night) has none."""
    conn, policy, _ = _env()
    _stages(conn, [("core", "02-11 22:00", "02-12 06:00")])                       # Apple, wake D
    _stages(conn, [("core", "02-12 23:00", "02-13 04:00")], device=ZEPP)          # Zepp only, wake D + 1
    _stages(conn, [("asleep", "02-12 23:30", "02-13 06:30")], device=WHOOP)       # Whoop's HealthKit copy
    points = [T("02-11 23:00"), T("02-12 03:00"), T("02-12 15:00"), T("02-11 21:00"), T("02-13 01:00"),
              T("02-12 06:00")]
    assert episodes.point_wake_dates(conn, policy, AWU, points) == [D, D, None, None, None, D]
    assert episodes.point_wake_dates(conn, policy, ZEPP, points[-2:]) == [D + timedelta(days=1), None]
    # Whoop's points follow Whoop's own HealthKit episode, keyed whoop:healthkit.
    assert episodes.point_wake_dates(conn, policy, WHOOP, [T("02-13 01:00")]) == [D + timedelta(days=1)]
    assert episodes.point_wake_dates(conn, policy, "whoop:healthkit", [T("02-13 01:00")]) == [D + timedelta(days=1)]
    assert episodes.point_wake_dates(conn, policy, AWU, []) == []


def test_night_detail_says_what_the_value_covers():
    """Each stored night carries its window: the Whoop API record's in-bed
    edges (basis whoop_api) or the episode's first and last asleep instant
    (basis episode). Whoop's HealthKit stage rows never stand in as its API
    night, and with sync_paths only the listed path's API rows count."""
    conn, policy, reg = _env(_policy(sleep_duration={"sync_paths": {"whoop": ["whoop_live"]}}))
    _api_night(conn, "n1", "02-11 23:05", "02-12 06:35", 6.9)
    _stages(conn, [("core", "02-11 23:20", "02-12 06:20")])                          # Apple, 7 h
    _stages(conn, [("asleep", "02-12 23:30", "02-13 05:30")], device=WHOOP)          # Whoop HK copy only, wake D + 1
    _api_night(conn, "n2", "02-13 23:00", "02-14 06:00", 6.5, path="backfill")       # not a listed path
    _compute(conn, policy, reg, D, D + timedelta(days=2))
    nights = _nights(conn)
    assert nights[D] == (6.9, WHOOP, {AWU: 7.0}, {"start": "2025-02-11T23:05:00", "end": "2025-02-12T06:35:00",
                                                  "window": "in_bed", "basis": "whoop_api"})
    assert D + timedelta(days=1) not in nights and D + timedelta(days=2) not in nights


def test_sleep_report_lists_every_device_of_the_night():
    """The sleep report shows the night's value and, beside it, every other
    device's own whole night (its corroboration), in the policy's order. Before
    Wave 2 the report carried the owner's value only."""
    conn, policy, reg = _env()
    _api_night(conn, "n1", "02-11 22:50", "02-12 06:10", 6.9)
    _stages(conn, [("core", "02-11 22:30", "02-11 23:50"), ("deep", "02-11 23:50", "02-12 01:00"),
                   ("core", "02-12 01:00", "02-12 05:40")])                          # Apple 7 h 10 m across midnight
    _stages(conn, [("core", "02-11 23:00", "02-12 06:15")], device=ZEPP)             # Zepp 7 h 15 m
    _compute(conn, policy, reg, D - timedelta(days=1), D)
    night = build_sleep_report(conn, days=7, policy=policy, today=D + timedelta(days=1))["nights"][-1]
    assert (night["date"], night["asleep_h"], night["device"]) == (str(D), 6.9, WHOOP)
    assert night["per_device"] == [{"device": WHOOP, "asleep_h": 6.9}, {"device": AWU, "asleep_h": 7.17},
                                   {"device": ZEPP, "asleep_h": 7.25}]
