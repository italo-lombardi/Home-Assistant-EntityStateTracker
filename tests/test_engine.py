"""Correctness guards for :mod:`engine` — the shared spine (§12 R1-R10).

Pure-function tests: freezegun is unnecessary because every ``engine`` function
takes ``now``/``tz`` explicitly, so time is injected as plain arguments. The
recorder is mocked at ``query_recorder``'s lazy import site.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.entity_state_tracker import engine as E
from custom_components.entity_state_tracker.const import LEDGER_MAX_DAYS

NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
UTC = dt.UTC

# US spring-forward 2026-03-08 (23h local day); fall-back 2026-11-01 (25h).
DST_SPRING = dt.datetime(2026, 3, 8, 12, 0, tzinfo=NY)
DST_FALL = dt.datetime(2026, 11, 1, 12, 0, tzinfo=NY)


class FakeState:
    """Minimal stand-in for ``homeassistant.core.State`` (only fields used)."""

    def __init__(self, state: str, last_changed: dt.datetime) -> None:
        self.state = state
        self.last_changed = last_changed


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# resolve_frame_bounds — all 8 frames + the ValueError guard
# --------------------------------------------------------------------------- #


def test_resolve_frame_bounds_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown frame key"):
        E.resolve_frame_bounds("nonsense", dt.datetime(2026, 1, 1, tzinfo=UTC), NY)


@pytest.mark.parametrize(
    ("frame", "expected_start_local", "end_is_now"),
    [
        pytest.param("today", (2026, 1, 15, 0, 0), True, id="today"),
        pytest.param("week", (2026, 1, 12, 0, 0), True, id="week"),
        pytest.param("month", (2026, 1, 1, 0, 0), True, id="month"),
        pytest.param("year", (2026, 1, 1, 0, 0), True, id="year"),
    ],
)
def test_resolve_frame_bounds_open_calendar(
    frame: str,
    expected_start_local: tuple[int, ...],
    end_is_now: bool,
) -> None:
    now = dt.datetime(2026, 1, 15, 9, 30, tzinfo=NY)
    start, end = E.resolve_frame_bounds(frame, now, NY)
    assert start.astimezone(NY) == dt.datetime(*expected_start_local, tzinfo=NY)
    assert end == now.astimezone(UTC)


def test_resolve_frame_bounds_yesterday_is_closed() -> None:
    now = dt.datetime(2026, 1, 15, 9, 30, tzinfo=NY)
    start, end = E.resolve_frame_bounds("yesterday", now, NY)
    assert start.astimezone(NY) == dt.datetime(2026, 1, 14, 0, 0, tzinfo=NY)
    # Ends at today's local midnight, NOT now.
    assert end.astimezone(NY) == dt.datetime(2026, 1, 15, 0, 0, tzinfo=NY)


@pytest.mark.parametrize(
    ("frame", "delta"),
    [
        pytest.param("24h", dt.timedelta(hours=24), id="24h"),
        pytest.param("7d", dt.timedelta(days=7), id="7d"),
    ],
)
def test_resolve_frame_bounds_rolling(frame: str, delta: dt.timedelta) -> None:
    now = dt.datetime(2026, 1, 15, 9, 30, tzinfo=NY)
    start, end = E.resolve_frame_bounds(frame, now, NY)
    assert end == now.astimezone(UTC)
    assert start == now.astimezone(UTC) - delta


def test_resolve_frame_bounds_30d_is_last_30_whole_days() -> None:
    now = dt.datetime(2026, 1, 31, 9, 30, tzinfo=NY)
    start, end = E.resolve_frame_bounds("30d", now, NY)
    assert end.astimezone(NY) == dt.datetime(2026, 1, 31, 0, 0, tzinfo=NY)
    assert start.astimezone(NY) == dt.datetime(2026, 1, 1, 0, 0, tzinfo=NY)
    assert (end - start).total_seconds() == 30 * 86400


@pytest.mark.parametrize(
    ("now_local", "expected_monday"),
    [
        # 2026-01-12 is a Monday: week starts at that same local midnight.
        pytest.param((2026, 1, 12, 9, 30), (2026, 1, 12, 0, 0), id="on-monday"),
        # 2026-01-15 is a Thursday: week rewinds to Monday 2026-01-12.
        pytest.param((2026, 1, 15, 9, 30), (2026, 1, 12, 0, 0), id="midweek"),
        # 2026-01-18 is a Sunday: still the same week's Monday.
        pytest.param((2026, 1, 18, 23, 59), (2026, 1, 12, 0, 0), id="sunday"),
    ],
)
def test_resolve_frame_bounds_week_starts_local_monday(
    now_local: tuple[int, ...],
    expected_monday: tuple[int, ...],
) -> None:
    now = dt.datetime(*now_local, tzinfo=NY)
    start, end = E.resolve_frame_bounds("week", now, NY)
    assert start.astimezone(NY) == dt.datetime(*expected_monday, tzinfo=NY)
    assert end == now.astimezone(UTC)


def test_resolve_frame_bounds_normalises_foreign_zone_now() -> None:
    # now given in UTC; boundary must still land on NY local midnight.
    now_utc = dt.datetime(2026, 1, 15, 4, 0, tzinfo=UTC)  # = 2026-01-14 23:00 NY
    start, _ = E.resolve_frame_bounds("today", now_utc, NY)
    assert start.astimezone(NY) == dt.datetime(2026, 1, 14, 0, 0, tzinfo=NY)


# --------------------------------------------------------------------------- #
# R1 — DST denominator (never 86400)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("now", "expected_secs"),
    [
        pytest.param(
            dt.datetime(2026, 3, 9, 0, 0, tzinfo=NY), 82800.0, id="spring_forward_23h"
        ),
        pytest.param(
            dt.datetime(2026, 11, 2, 0, 0, tzinfo=NY), 90000.0, id="fall_back_25h"
        ),
    ],
)
def test_r1_dst_window_seconds_full_day(now: dt.datetime, expected_secs: float) -> None:
    # "today" resolved at the following local midnight == the whole DST day.
    start, end = E.resolve_frame_bounds("today", now - dt.timedelta(seconds=1), NY)
    # The whole prior day: recompute bounds for a `now` still inside that day.
    inside = now - dt.timedelta(minutes=1)
    start, end = E.resolve_frame_bounds("today", inside, NY)
    window = (end - start).total_seconds()
    assert window == pytest.approx(expected_secs - 60, abs=1)


@pytest.mark.parametrize(
    ("day_start", "next_midnight", "expected_secs"),
    [
        pytest.param(
            dt.datetime(2026, 3, 8, 0, 0, tzinfo=NY),
            dt.datetime(2026, 3, 9, 0, 0, tzinfo=NY),
            82800.0,
            id="spring_23h",
        ),
        pytest.param(
            dt.datetime(2026, 11, 1, 0, 0, tzinfo=NY),
            dt.datetime(2026, 11, 2, 0, 0, tzinfo=NY),
            90000.0,
            id="fall_25h",
        ),
    ],
)
def test_r1_continuously_on_is_100pct(
    day_start: dt.datetime,
    next_midnight: dt.datetime,
    expected_secs: float,
) -> None:
    start = day_start.astimezone(UTC)
    now = next_midnight.astimezone(UTC)
    states = [FakeState("on", start - dt.timedelta(hours=1))]  # on since before
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["on"]["secs"] == pytest.approx(expected_secs)
    # Resolve "today" from an instant strictly inside the day (one second before
    # the closing midnight), else "today" would roll to the next, empty day.
    inside = now - dt.timedelta(seconds=1)
    fr = E.compute_frame(
        "today",
        inside,
        NY,
        blocks,
        {},
        None,
        mode="specific_states",
        tracked_states=["on"],
        target_states=None,
        prior_dominant=None,
    )
    assert fr.window_seconds == pytest.approx(expected_secs - 1, abs=1)
    assert fr.percent == pytest.approx(100.0, abs=0.1)


# --------------------------------------------------------------------------- #
# split_visit_across_days
# --------------------------------------------------------------------------- #


def test_split_reversed_interval_empty() -> None:
    a = _utc(2026, 1, 2)
    b = _utc(2026, 1, 1)
    assert E.split_visit_across_days(a, b, NY) == []
    assert E.split_visit_across_days(a, a, NY) == []


def test_split_within_one_local_day() -> None:
    s = dt.datetime(2026, 1, 15, 8, 0, tzinfo=NY).astimezone(UTC)
    e = dt.datetime(2026, 1, 15, 10, 0, tzinfo=NY).astimezone(UTC)
    assert E.split_visit_across_days(s, e, NY) == [("2026-01-15", 7200.0)]


# --------------------------------------------------------------------------- #
# R3 — fold across midnight, no double count (parametrized zones + DST)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tz", "day1_local", "hours", "expected"),
    [
        pytest.param(
            NY,
            dt.datetime(2026, 1, 14, 23, 0, tzinfo=NY),
            2,
            [("2026-01-14", 3600.0), ("2026-01-15", 3600.0)],
            id="america_new_york",
        ),
        pytest.param(
            TOKYO,
            dt.datetime(2026, 1, 14, 23, 0, tzinfo=TOKYO),
            2,
            [("2026-01-14", 3600.0), ("2026-01-15", 3600.0)],
            id="asia_tokyo",
        ),
        pytest.param(
            NY,
            dt.datetime(2026, 3, 7, 23, 0, tzinfo=NY),
            # 3/7 23:00 → 3/8 01:00 across a NORMAL midnight (spring-forward is
            # at 02:00, later in the day), so 1h / 1h.
            2,
            [("2026-03-07", 3600.0), ("2026-03-08", 3600.0)],
            id="dst_day_normal_midnight",
        ),
        pytest.param(
            NY,
            dt.datetime(2026, 3, 7, 23, 0, tzinfo=NY),
            # 3/7 23:00 → 3/8 03:00 straddles BOTH the midnight boundary AND the
            # 02:00 spring-forward fold. Day 3/7 gets 1h (23:00→00:00). Day 3/8's
            # 00:00→03:00 is only 2 WALL-CLOCK hours because 02:00–03:00 never
            # exists — proving the split attributes real elapsed time at the fold,
            # not a naive 3h. Total = 3h wall-clock (matches end−start).
            4,
            [("2026-03-07", 3600.0), ("2026-03-08", 7200.0)],
            id="dst_day_across_spring_forward_fold",
        ),
    ],
)
def test_r3_fold_across_midnight_no_double_count(
    tz: ZoneInfo,
    day1_local: dt.datetime,
    hours: int,
    expected: list[tuple[str, float]],
) -> None:
    start = day1_local.astimezone(UTC)
    end = (day1_local + dt.timedelta(hours=hours)).astimezone(UTC)
    parts = E.split_visit_across_days(start, end, tz)
    assert parts == expected
    # Conservation: the split never invents or loses wall-clock.
    assert sum(secs for _, secs in parts) == pytest.approx(
        (end - start).total_seconds()
    )


def test_r3_seam_closed_ledger_plus_today_equals_whole_window() -> None:
    # Ledger owns closed days < today; today arrives via recent_blocks. The
    # today row in the ledger must be IGNORED (no double count).
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    ledger = {
        "2026-01-13": {"on": {"secs": 3600.0, "count": 1}},
        "2026-01-14": {"on": {"secs": 7200.0, "count": 2}},
        "2026-01-15": {"on": {"secs": 99999.0, "count": 9}},  # today — excluded
    }
    recent = {"on": {"secs": 1800.0, "count": 1}}
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        ledger,
        None,
        mode="specific_states",
        tracked_states=["on"],
        target_states=None,
        prior_dominant=None,
    )
    assert fr.breakdown_seconds["on"] == pytest.approx(3600.0 + 7200.0 + 1800.0)
    assert fr.counts["on"] == 1 + 2 + 1


def test_r3_ledger_day_before_window_excluded() -> None:
    # A ledger day OLDER than the window start must not be summed in.
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    ledger = {
        "2026-01-01": {"on": {"secs": 5000.0, "count": 1}},  # < 7d window start
        "2026-01-14": {"on": {"secs": 7200.0, "count": 1}},
    }
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        {},
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.breakdown_seconds["on"] == pytest.approx(7200.0)


# --------------------------------------------------------------------------- #
# accumulate_blocks — edge cases + trailing/leading behaviour
# --------------------------------------------------------------------------- #


def test_accumulate_empty_states() -> None:
    now = _utc(2026, 1, 15, 12)
    assert E.accumulate_blocks([], _utc(2026, 1, 15), now, 0.0, now) == {}


def test_accumulate_measure_end_before_window_start() -> None:
    # now precedes window_start → measure_end <= window_start guard.
    now = _utc(2026, 1, 14)
    states = [FakeState("on", _utc(2026, 1, 15))]
    assert (
        E.accumulate_blocks(states, _utc(2026, 1, 15), _utc(2026, 1, 16), 0.0, now)
        == {}
    )


def test_accumulate_row_starting_after_measure_end_breaks() -> None:
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 1)
    states = [
        FakeState("on", start),
        FakeState("off", _utc(2026, 1, 15, 2)),  # begins after measure_end → break
    ]
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks == {"on": {"secs": 3600.0, "count": 1}}


def test_accumulate_zero_length_row_skipped() -> None:
    # Two rows with identical timestamps → the first yields a zero block,
    # exercising the `block_end <= block_start` continue.
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 1)
    states = [
        FakeState("on", start),
        FakeState("off", start),  # same instant → on block is zero-length
    ]
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks == {"off": {"secs": 3600.0, "count": 1}}


def test_accumulate_consecutive_same_state_merged() -> None:
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 3)
    states = [
        FakeState("on", start),
        FakeState("on", _utc(2026, 1, 15, 1)),  # duplicate → merge into raw block
        FakeState("off", _utc(2026, 1, 15, 2)),
    ]
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["on"] == {"secs": 7200.0, "count": 1}
    assert blocks["off"] == {"secs": 3600.0, "count": 1}


# --------------------------------------------------------------------------- #
# E1 — leading in-force block (visit began before window) is a continuation:
# secs but NO count, so the ledger-day + today-slice seam counts one continuous
# midnight-spanning visit exactly once (not per-day).
# --------------------------------------------------------------------------- #


def test_e1_leading_in_force_block_has_no_count() -> None:
    # First row's last_changed is BEFORE window_start → the visit started earlier
    # and is only continuing here. It accrues seconds but count 0.
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 6)
    states = [FakeState("on", _utc(2026, 1, 14, 23))]  # began 1h before window
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["on"]["secs"] == pytest.approx(6 * 3600.0)
    assert blocks["on"]["count"] == 0


def test_e1_entry_at_window_start_is_counted() -> None:
    # First row's last_changed == window_start → a genuine entry at the edge,
    # counted (not a pre-window continuation).
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 6)
    states = [FakeState("on", start)]
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["on"]["count"] == 1


def test_e1_second_state_after_continuation_is_counted() -> None:
    # Leading `on` is a continuation (count 0); the following genuine `off` entry
    # inside the window is counted normally.
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 6)
    states = [
        FakeState("on", _utc(2026, 1, 14, 22)),  # continuation → count 0
        FakeState("off", _utc(2026, 1, 15, 2)),  # genuine entry → count 1
    ]
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["on"]["count"] == 0
    assert blocks["off"]["count"] == 1


def test_e1_midnight_spanning_continuous_visit_counted_once() -> None:
    # THE repro: a single continuous `on` visit that began yesterday 23:00 and is
    # still on now. Ledger owns yesterday (count 1, on its start day); today's
    # recorder slice re-sees the in-force `on` leading block. Before the fix the
    # today slice counted it as a fresh entry → compute_frame summed to count 2.
    # After the fix the today slice's leading continuation is count 0 → total 1.
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    visit_start = dt.datetime(2026, 1, 14, 23, 0, tzinfo=NY).astimezone(UTC)
    # Ledger: the closed start day carries the single count.
    ledger = {"2026-01-14": {"on": {"secs": 3600.0, "count": 1}}}
    # Today's slice recomputed fresh from the recorder (in force since 23:00).
    today_start = dt.datetime(2026, 1, 15, 0, 0, tzinfo=NY).astimezone(UTC)
    today_states = [FakeState("on", visit_start)]
    recent = E.accumulate_blocks(
        today_states, today_start, now.astimezone(UTC), 0.0, now
    )
    assert recent["on"]["count"] == 0  # continuation, not a fresh entry
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.counts["on"] == 1  # counted once, on the day it began


def test_e1_continuously_on_seven_days_counts_once() -> None:
    # A continuously-on entity across a whole 7d frame must count ≈1 (the entry),
    # never 7 (one per backfilled day). Backfill recomputes each closed day
    # independently; only the start day sees a genuine (count 1) entry — every
    # later day's leading `on` is a pre-window continuation (count 0).
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    on_since = dt.datetime(2026, 1, 9, 8, 0, tzinfo=NY)  # entry inside the 7d window
    ledger: dict[str, dict[str, dict[str, float]]] = {}
    for offset in range(6):  # closed days 01-09 .. 01-14
        day = dt.date(2026, 1, 9) + dt.timedelta(days=offset)
        day_start = dt.datetime.combine(day, dt.time(), tzinfo=NY).astimezone(UTC)
        day_end = day_start + dt.timedelta(days=1)
        day_states = [FakeState("on", on_since.astimezone(UTC))]
        blocks = E.accumulate_blocks(day_states, day_start, day_end, 0.0, day_end)
        ledger[day.isoformat()] = {k: dict(v) for k, v in blocks.items()}
    today_start = dt.datetime(2026, 1, 15, 0, 0, tzinfo=NY).astimezone(UTC)
    recent = E.accumulate_blocks(
        [FakeState("on", on_since.astimezone(UTC))],
        today_start,
        now.astimezone(UTC),
        0.0,
        now,
    )
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.counts["on"] == 1


# --------------------------------------------------------------------------- #
# R6 — glitch filter (dropped from both secs AND count; recent == ledger path)
# --------------------------------------------------------------------------- #


def test_r6_glitch_dropped_from_secs_and_count() -> None:
    start = _utc(2026, 1, 15, 0, 0)
    now = _utc(2026, 1, 15, 1, 0)
    states = [
        FakeState("on", start),
        FakeState("off", _utc(2026, 1, 15, 0, 40)),
        FakeState("on", start + dt.timedelta(minutes=40, seconds=2)),  # 2s glitch
    ]
    blocks = E.accumulate_blocks(states, start, now, 5.0, now)
    assert "off" not in blocks  # dropped entirely
    assert blocks["on"] == {"secs": 3600.0, "count": 1}  # one contiguous visit


def test_r6_no_filter_keeps_glitch() -> None:
    start = _utc(2026, 1, 15, 0, 0)
    now = _utc(2026, 1, 15, 1, 0)
    states = [
        FakeState("on", start),
        FakeState("off", _utc(2026, 1, 15, 0, 40)),
        FakeState("on", start + dt.timedelta(minutes=40, seconds=2)),
    ]
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["off"] == {"secs": 2.0, "count": 1}
    assert blocks["on"] == {"secs": 3598.0, "count": 2}


def test_r6_glitch_boundary_exactly_at_threshold_is_kept() -> None:
    # A block whose duration is EXACTLY min_state_duration is NOT a glitch: the
    # filter is `secs < min_state_duration` (strict). Pins the `<` vs `<=`
    # boundary — a `<=` mutation would drop this block and this test fails.
    start = _utc(2026, 1, 15, 0, 0)
    now = _utc(2026, 1, 15, 1, 0)
    states = [
        FakeState("on", start),
        FakeState("off", _utc(2026, 1, 15, 0, 30)),
        FakeState("on", _utc(2026, 1, 15, 0, 30, 5)),  # off block == 5s exactly
    ]
    blocks = E.accumulate_blocks(states, start, now, 5.0, now)
    # off lasted exactly 5s == threshold → KEPT (not merged into on).
    assert blocks["off"] == {"secs": 5.0, "count": 1}
    # on is two separate visits — the exact-threshold off really separated them.
    assert blocks["on"]["count"] == 2


def test_r6_glitch_just_under_threshold_is_dropped() -> None:
    # One tick below the threshold IS a glitch → merged into the preceding `on`,
    # dropped from both secs and count. Pairs with the ==threshold test to pin
    # the boundary from both sides.
    start = _utc(2026, 1, 15, 0, 0)
    now = _utc(2026, 1, 15, 1, 0)
    states = [
        FakeState("on", start),
        FakeState("off", _utc(2026, 1, 15, 0, 30)),
        FakeState("on", _utc(2026, 1, 15, 0, 30, 4)),  # off block == 4s < 5s
    ]
    blocks = E.accumulate_blocks(states, start, now, 5.0, now)
    assert "off" not in blocks
    assert blocks["on"]["count"] == 1  # one contiguous visit


def test_r6_leading_glitch_dropped() -> None:
    # First block is a sub-threshold glitch with no predecessor → dropped.
    start = _utc(2026, 1, 15, 0, 0)
    now = _utc(2026, 1, 15, 1, 0)
    states = [
        FakeState("off", start),  # 3s glitch, no predecessor
        FakeState("on", start + dt.timedelta(seconds=3)),
    ]
    blocks = E.accumulate_blocks(states, start, now, 5.0, now)
    assert "off" not in blocks
    assert blocks["on"]["count"] == 1


def test_r6_glitch_between_different_states_no_coalesce() -> None:
    # on → (glitch off) → cool: glitch merges into on; cool stays its own block.
    start = _utc(2026, 1, 15, 0, 0)
    now = _utc(2026, 1, 15, 1, 0)
    states = [
        FakeState("on", start),
        FakeState("off", _utc(2026, 1, 15, 0, 30)),  # 2s glitch
        FakeState("cool", start + dt.timedelta(minutes=30, seconds=2)),
    ]
    blocks = E.accumulate_blocks(states, start, now, 5.0, now)
    assert "off" not in blocks
    assert blocks["on"]["count"] == 1
    assert blocks["cool"]["count"] == 1


def test_r6_recent_path_equals_ledger_path() -> None:
    # The same visit, accumulated directly vs. folded per-day into a ledger and
    # summed via compute_frame, must agree.
    start = dt.datetime(2026, 1, 14, 22, 0, tzinfo=NY)
    end = dt.datetime(2026, 1, 15, 2, 0, tzinfo=NY)  # 4h across midnight
    parts = E.split_visit_across_days(start.astimezone(UTC), end.astimezone(UTC), NY)
    ledger: dict[str, dict[str, dict[str, float]]] = {}
    for i, (day, secs) in enumerate(parts):
        ledger.setdefault(day, {})["on"] = {"secs": secs, "count": 1 if i == 0 else 0}
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    # Recompute today's slice (2026-01-15 00:00 → 02:00) as the recent block.
    today_start = dt.datetime(2026, 1, 15, 0, 0, tzinfo=NY).astimezone(UTC)
    today_states = [FakeState("on", start.astimezone(UTC))]
    recent = E.accumulate_blocks(
        today_states, today_start, end.astimezone(UTC), 0.0, now
    )
    # Ledger holds only 2026-01-14 (closed); drop the today row so no double count.
    ledger.pop("2026-01-15", None)
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    # 2h (14th, ledger) + 2h (15th, recent) = 4h.
    assert fr.breakdown_seconds["on"] == pytest.approx(4 * 3600.0)
    # The REAL seam guard: the today-slice's leading `on` began at 22:00 the
    # prior day (a continuation), so accumulate_blocks emits count 0 for it and
    # the single visit is counted once — on the ledger day it began. Without the
    # leading-continuation suppression this sums to 2 (the seam double-count).
    assert recent["on"]["count"] == 0
    assert fr.counts["on"] == 1


# --------------------------------------------------------------------------- #
# R4 — carry-forward seam
# --------------------------------------------------------------------------- #


def test_r4_carry_forward_noop_when_no_shutdown_marker() -> None:
    # No trailing `unavailable` → list passes through unchanged (identity).
    states = [FakeState("on", _utc(2026, 1, 15))]
    assert E.carry_forward_states(states) is states


def test_r4_shutdown_on_row_carried_as_occupancy() -> None:
    # Last row before an HA-down gap is `on`; include_start_time_state carries it
    # forward, so the whole window counts as `on` occupancy.
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 6)
    states = E.carry_forward_states([FakeState("on", start - dt.timedelta(hours=2))])
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["on"]["secs"] == pytest.approx(6 * 3600.0)


def test_r4_trailing_shutdown_unavailable_dropped_as_gap_start() -> None:
    # HA writes `unavailable` on clean shutdown, then goes down. That trailing
    # row is the GAP START, not real occupancy: carry_forward_states drops it so
    # the preceding steady state (`on`) carries across the gap instead of the
    # whole outage being counted as `unavailable`. This test FAILS if
    # carry_forward_states is a no-op (it would attribute 4h to `unavailable`).
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 6)
    raw = [
        FakeState("on", start),
        FakeState("unavailable", _utc(2026, 1, 15, 2)),  # clean-shutdown marker
    ]
    states = E.carry_forward_states(raw)
    assert states == raw[:-1]  # the trailing shutdown marker was dropped
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    # `on` carries across the whole 6h window; `unavailable` never accrues.
    assert "unavailable" not in blocks
    assert blocks["on"]["secs"] == pytest.approx(6 * 3600.0)


def test_r4_mid_window_unavailable_kept_as_ordinary_state() -> None:
    # A pre-shutdown `unavailable` row that is NOT last (HA recovered from it) is
    # an ordinary state: it occupies only its own interval, and the list is
    # unchanged by carry_forward_states.
    start = _utc(2026, 1, 15, 0)
    now = _utc(2026, 1, 15, 4)
    raw = [
        FakeState("on", start),
        FakeState("unavailable", _utc(2026, 1, 15, 1)),  # transient, recovered
        FakeState("on", _utc(2026, 1, 15, 3)),  # back after restart
    ]
    states = E.carry_forward_states(raw)
    assert states is raw  # not a trailing marker → untouched
    blocks = E.accumulate_blocks(states, start, now, 0.0, now)
    assert blocks["unavailable"]["secs"] == pytest.approx(2 * 3600.0)
    assert blocks["on"]["secs"] == pytest.approx(2 * 3600.0)


def test_r4_sole_trailing_unavailable_kept() -> None:
    # A result that is ONLY a trailing shutdown marker has no predecessor to
    # carry forward; dropping it would erase all history, so it is kept.
    states = [FakeState("unavailable", _utc(2026, 1, 15))]
    assert E.carry_forward_states(states) is states


# --------------------------------------------------------------------------- #
# R7 — partial first day
# --------------------------------------------------------------------------- #


def test_r7_partial_coverage_flags_gap() -> None:
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    # 7d window starts 2026-01-08; data only since 2026-01-14.
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        {"on": {"secs": 100.0, "count": 1}},
        {},
        "2026-01-14",
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.has_gap is True
    assert 0.0 < fr.window_coverage < 1.0
    assert fr.data_start == dt.datetime(2026, 1, 14, 0, 0, tzinfo=NY).isoformat()


def test_r7_sum_pct_about_100_on_covered_denominator() -> None:
    # First-seen mid-day: the covered window is [12:00, 24:00) = 12h; the state
    # occupies all of it, so breakdown_pct sums to ~100 on the covered denom.
    day_start = dt.datetime(2026, 1, 15, 0, 0, tzinfo=NY).astimezone(UTC)
    first_seen = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY).astimezone(UTC)
    now = dt.datetime(2026, 1, 16, 0, 0, tzinfo=NY).astimezone(UTC)
    states = [FakeState("on", first_seen)]
    blocks = E.accumulate_blocks(states, day_start, now, 0.0, now)
    # 12h of `on` against a 24h window ⇒ 50%.
    fr = E.compute_frame(
        "today",
        now - dt.timedelta(seconds=1),
        NY,
        blocks,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.breakdown_pct["on"] == pytest.approx(50.0, abs=0.1)


def test_r7_coverage_data_start_before_window_returns_full() -> None:
    # data_start older than the window start → fully covered, no gap.
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {"on": {"secs": 100.0, "count": 1}},
        {},
        "2020-01-01",
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.has_gap is False
    assert fr.window_coverage == 1.0
    assert fr.data_start is None


def test_r7_coverage_unparseable_data_start_returns_full() -> None:
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {},
        {},
        "not-a-date",
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.has_gap is False
    assert fr.data_start is None


# --------------------------------------------------------------------------- #
# R9 — prune vs year (Dec 31 leap year keeps Jan 1)
# --------------------------------------------------------------------------- #


def test_r9_prune_year_keeps_jan1_leap() -> None:
    # 2024 is a leap year. On Dec 31 with `year` enabled the cutoff must be on or
    # before Jan 1 so the Jan 1 bucket survives.
    now = dt.datetime(2024, 12, 31, 23, 0, tzinfo=NY)
    cutoff = E.prune_cutoff_iso(["year", "today"], now, NY)
    assert cutoff <= "2024-01-01"
    assert "2024-01-01" >= cutoff  # Jan 1 not strictly-before cutoff → survives


def test_r9_prune_hard_floor_caps_at_ledger_max() -> None:
    # A `year` frame in early January reaches back < LEDGER_MAX_DAYS, but the
    # hard floor still applies as the max() lower bound.
    now = dt.datetime(2026, 1, 2, 12, 0, tzinfo=NY)
    cutoff = E.prune_cutoff_iso(["year"], now, NY)
    floor = (
        (dt.datetime(2026, 1, 2, 0, 0, tzinfo=NY) - dt.timedelta(days=LEDGER_MAX_DAYS))
        .date()
        .isoformat()
    )
    assert cutoff >= floor


def test_r9_prune_unknown_frame_skipped() -> None:
    now = dt.datetime(2026, 6, 15, 12, 0, tzinfo=NY)
    # An unknown frame is skipped; only `today` counts → cutoff ≈ today − 2d.
    cutoff = E.prune_cutoff_iso(["bogus", "today"], now, NY)
    assert cutoff == "2026-06-13"


def test_r9_prune_empty_frames_uses_today() -> None:
    now = dt.datetime(2026, 6, 15, 12, 0, tzinfo=NY)
    cutoff = E.prune_cutoff_iso([], now, NY)
    assert cutoff == "2026-06-13"


# --------------------------------------------------------------------------- #
# R10 — dominant-state hysteresis
# --------------------------------------------------------------------------- #


def test_r10_hysteresis_keeps_incumbent_on_near_tie() -> None:
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    # off leads on by 10s; 7d window ⇒ 1% margin is 6048s. 10s < margin → keep on.
    recent = {"on": {"secs": 3600.0, "count": 1}, "off": {"secs": 3610.0, "count": 1}}
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant="on",
    )
    assert fr.dominant == "on"


def test_r10_hysteresis_flips_when_margin_exceeded() -> None:
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    recent = {"on": {"secs": 3600.0, "count": 1}, "off": {"secs": 100000.0, "count": 1}}
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant="on",
    )
    assert fr.dominant == "off"


def test_pick_dominant_empty_none() -> None:
    assert E._pick_dominant({}, 100.0, None) is None


def test_pick_dominant_no_prior_returns_leader() -> None:
    assert E._pick_dominant({"a": 5.0, "b": 3.0}, 100.0, None) == "a"


def test_pick_dominant_prior_equals_leader() -> None:
    assert E._pick_dominant({"a": 5.0, "b": 3.0}, 100.0, "a") == "a"


def test_pick_dominant_prior_absent_returns_leader() -> None:
    # prior_dominant no longer present in the breakdown → adopt the new leader.
    assert E._pick_dominant({"a": 5.0, "b": 3.0}, 100.0, "gone") == "a"


def test_pick_dominant_never_unaccounted() -> None:
    # _pick_dominant selects from breakdown_seconds, which never carries an
    # "unaccounted" key — so the remainder can never win, even as the largest
    # fraction of the window.
    assert E._pick_dominant({"on": 5.0}, 1000.0, None) == "on"


def test_compute_frame_dominant_ignores_unaccounted_remainder() -> None:
    """dominant is a real state (never "unaccounted") even when the gap dominates.

    A mostly-gap window: only 100s of `on` recorded in a 43200s frame, so the
    unaccounted remainder (43100s) is by far the largest fraction — yet dominant
    must be the real state `on`, never the pseudo-key.
    """
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {"on": {"secs": 100.0, "count": 1}},
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    # Remainder is the biggest slice…
    assert fr.unaccounted_seconds > fr.breakdown_seconds["on"]
    assert fr.breakdown_pct["unaccounted"] > fr.breakdown_pct["on"]
    # …but dominant is the real state, never "unaccounted".
    assert fr.dominant == "on"


def test_compute_frame_dominant_none_when_no_states_all_gap() -> None:
    """With no recorded states the whole window is unaccounted; dominant is None."""
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {},
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.dominant is None
    assert fr.dominant != "unaccounted"
    # The remainder still shows as ~100% in breakdown_pct.
    assert fr.breakdown_pct["unaccounted"] == pytest.approx(100.0, abs=0.1)


# --------------------------------------------------------------------------- #
# compute_frame — breakdown_pct balances to EXACTLY 100.00 (sum-of-rounded fix)
# --------------------------------------------------------------------------- #


def _today_noon_ny() -> dt.datetime:
    # "today" @ noon NY, no DST transition → window is exactly 43200s. Every
    # balancing case below is pinned to this literal denominator.
    return dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)


def _frame_from_secs(recent: dict[str, dict[str, float]]) -> E.FrameResult:
    return E.compute_frame(
        "today",
        _today_noon_ny(),
        NY,
        recent,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )


def test_balance_full_coverage_drift_into_largest_real_slice() -> None:
    # Σsecs == window (43200s) ⇒ fully covered, no real "unaccounted" slice.
    # Independent 2-dp rounds are 0.14 + 0.56 + 99.31 = 100.01, so the −0.01
    # drift is folded into the largest-secs slice ("c": 99.31 → 99.30); the two
    # small slices keep their own round. unaccounted is present as 0.0.
    recent = {
        "a": {"secs": 60.0, "count": 1},
        "b": {"secs": 240.0, "count": 1},
        "c": {"secs": 42900.0, "count": 1},
    }
    fr = _frame_from_secs(recent)
    assert fr.window_seconds == pytest.approx(43200.0)
    assert fr.breakdown_pct["unaccounted"] == 0.0
    assert fr.breakdown_pct["a"] == 0.14
    assert fr.breakdown_pct["b"] == 0.56
    assert fr.breakdown_pct["c"] == 99.30  # 99.31 − 0.01 drift
    assert round(sum(fr.breakdown_pct.values()), 2) == 100.00


def test_balance_full_coverage_tie_break_largest_by_name() -> None:
    # Two states tie at the max secs (14760 each) with a smaller third. The
    # drift (−0.01) lands on the deterministic max-secs winner, tie-broken by
    # name (max name): "zed" over "alf". 34.17 → 34.16.
    recent = {
        "alf": {"secs": 14760.0, "count": 1},
        "zed": {"secs": 14760.0, "count": 1},
        "s": {"secs": 13680.0, "count": 1},
    }
    fr = _frame_from_secs(recent)
    assert fr.breakdown_pct["unaccounted"] == 0.0
    assert fr.breakdown_pct["alf"] == 34.17  # untouched
    assert fr.breakdown_pct["zed"] == 34.16  # drift absorbed here (name tie-break)
    assert fr.breakdown_pct["s"] == 31.67
    assert round(sum(fr.breakdown_pct.values()), 2) == 100.00


def test_balance_single_state_full_coverage() -> None:
    # One state fills the whole window: {state: 100.0, unaccounted: 0.0}.
    fr = _frame_from_secs({"on": {"secs": 43200.0, "count": 1}})
    assert fr.breakdown_pct == {"on": 100.0, "unaccounted": 0.0}
    assert round(sum(fr.breakdown_pct.values()), 2) == 100.00


def test_balance_gap_unaccounted_absorbs_balance() -> None:
    # Genuine gap: two small real states (300s total) leave 42900s uncovered.
    # Real slices keep their own independent round (0.14, 0.56); "unaccounted"
    # is the BALANCE 100 − 0.70 = 99.30 — NOT its own naive round (99.31),
    # which is what proves it absorbed the drift as the least-meaningful slice.
    recent = {
        "on": {"secs": 60.0, "count": 1},
        "off": {"secs": 240.0, "count": 1},
    }
    fr = _frame_from_secs(recent)
    assert fr.unaccounted_seconds == pytest.approx(42900.0)
    assert fr.has_gap is False  # gap here is intra-window, not a ledger-start gap
    assert fr.breakdown_pct["on"] == 0.14
    assert fr.breakdown_pct["off"] == 0.56
    assert fr.breakdown_pct["unaccounted"] == 99.30  # balance, not naive 99.31
    assert round(sum(fr.breakdown_pct.values()), 2) == 100.00


def test_balance_empty_real_states_only_unaccounted() -> None:
    # No recorded states → the whole window is unaccounted.
    fr = _frame_from_secs({})
    assert fr.breakdown_pct == {"unaccounted": 100.0}
    assert round(sum(fr.breakdown_pct.values()), 2) == 100.00


def test_balance_seconds_and_derived_fields_unchanged() -> None:
    # The balance touches ONLY the displayed breakdown_pct. breakdown_seconds,
    # counts, avg_duration, unaccounted_seconds, percent, compliance_percent are
    # unaffected by the rounding fix.
    recent = {
        "a": {"secs": 60.0, "count": 3},
        "b": {"secs": 240.0, "count": 2},
        "c": {"secs": 42900.0, "count": 1},
    }
    fr = E.compute_frame(
        "today",
        _today_noon_ny(),
        NY,
        recent,
        {},
        None,
        mode="specific_states",
        tracked_states=["c"],
        target_states=["c"],
        prior_dominant=None,
    )
    assert fr.breakdown_seconds == {"a": 60.0, "b": 240.0, "c": 42900.0}
    assert fr.counts == {"a": 3, "b": 2, "c": 1}
    assert fr.avg_duration == {"a": 20.0, "b": 120.0, "c": 42900.0}
    assert fr.unaccounted_seconds == pytest.approx(0.0)
    # 42900s of 43200s = 99.30… → 1-dp percent is 99.3, independent of the fix.
    assert fr.percent == pytest.approx(99.3)
    assert fr.compliance_percent == pytest.approx(99.3)


def test_balance_full_coverage_no_drift_leaves_slices_untouched() -> None:
    # Σsecs == window and the independent rounds already sum to 100.00 (drift
    # 0), so NO slice is adjusted — the drift==0 short-circuit path.
    recent = {
        "on": {"secs": 21600.0, "count": 1},
        "off": {"secs": 21600.0, "count": 1},
    }
    fr = _frame_from_secs(recent)
    assert fr.breakdown_pct == {"on": 50.0, "off": 50.0, "unaccounted": 0.0}
    assert round(sum(fr.breakdown_pct.values()), 2) == 100.00


def test_balance_zero_window_unaccounted_zero() -> None:
    # window_seconds <= 0: keep the current 0.0 behavior — no balance/no drift
    # (a drift of 100 must NOT be foisted onto a real slice). resolve_frame_bounds
    # is patched to a degenerate empty window (start == end).
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    edge = now.astimezone(UTC)
    with patch.object(E, "resolve_frame_bounds", return_value=(edge, edge)):
        fr = E.compute_frame(
            "today",
            now,
            NY,
            {"on": {"secs": 30000.0, "count": 1}},
            {},
            None,
            mode="all_states",
            tracked_states=None,
            target_states=None,
            prior_dominant=None,
        )
    assert fr.window_seconds == 0.0
    assert fr.breakdown_pct["unaccounted"] == 0.0
    assert fr.breakdown_pct["on"] == 0.0  # _pct's zero-window guard, untouched


def test_balance_empty_reals_subepsilon_window_no_max_on_empty() -> None:
    # Fully covered (unaccounted <= 1.0s) but NO real states: the drift-into-
    # largest path must short-circuit on the empty real set (no max() on {}).
    # A 0.5s window with empty breakdown leaves unaccounted_seconds == 0.5.
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    start = now.astimezone(UTC)
    end = start + dt.timedelta(seconds=0.5)
    with patch.object(E, "resolve_frame_bounds", return_value=(start, end)):
        fr = E.compute_frame(
            "today",
            now,
            NY,
            {},
            {},
            None,
            mode="all_states",
            tracked_states=None,
            target_states=None,
            prior_dominant=None,
        )
    assert fr.window_seconds == pytest.approx(0.5)
    assert fr.unaccounted_seconds == pytest.approx(0.5)
    assert fr.breakdown_pct == {"unaccounted": 0.0}


# --------------------------------------------------------------------------- #
# compute_frame — percent/compliance/avg_duration + zero-window guards
# --------------------------------------------------------------------------- #


def test_compute_frame_percent_and_compliance() -> None:
    # now = 2026-01-15 12:00 NY (no DST transition) → "today" window is exactly
    # local-midnight→noon = 43200s. Assert percent/compliance against that HARD
    # literal denominator, NOT fr.window_seconds — otherwise the test recomputes
    # with the code's own denominator and a `secs / 86400` mutation survives (F3).
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    recent = {
        "heat": {"secs": 3600.0, "count": 2},
        "off": {"secs": 3600.0, "count": 1},
    }
    fr = E.compute_frame(
        "today",
        now,
        NY,
        recent,
        {},
        None,
        mode="specific_states",
        tracked_states=["heat"],
        target_states=["heat", "auto"],
        prior_dominant=None,
    )
    assert fr.window_seconds == pytest.approx(43200.0)  # pin the denominator
    # 3600s of 43200s = 8.333… → percent is 1-dp (8.3); breakdown_pct is 2-dp
    # (8.33) after the sentinel change. Literals, not derived from the code.
    assert fr.percent == pytest.approx(8.3)
    assert fr.compliance_percent == pytest.approx(8.3)
    assert fr.breakdown_pct["heat"] == pytest.approx(8.33)
    assert fr.avg_duration["heat"] == 1800.0
    assert fr.avg_duration["off"] == 3600.0


def test_avg_duration_is_1dp_float_not_floor() -> None:
    # avg_duration is round(secs / count, 1), a 1-dp float — NOT floor division.
    # 359s over 2 visits is 179.5 per visit; a `secs // count` regression yields
    # 179.0 and fails here. None at count 0 (a ledger continuation day).
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    recent = {
        "on": {"secs": 359.0, "count": 2},
        "flap": {"secs": 100.0, "count": 3},  # 33.333… → 33.3
        "carry": {"secs": 500.0, "count": 0},  # continuation day → None
    }
    fr = E.compute_frame(
        "today",
        now,
        NY,
        recent,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.avg_duration["on"] == 179.5
    assert fr.avg_duration["flap"] == 33.3
    assert fr.avg_duration["carry"] is None


def test_f3_breakdown_pct_dst_denominator_not_86400() -> None:
    # THE F3 guard: on a 23h spring-forward day, 12h of `on` is 12h/23h = 52.2%,
    # NOT 12h/24h = 50.0%. Asserting the literal 52.2 makes a `secs / 86400`
    # (or any 24h-assuming) denominator FAIL. The DST day 2026-03-08 is 23h in NY
    # (spring-forward at 02:00), so its "today" window is 82800s.
    day_start = dt.datetime(2026, 3, 8, 0, 0, tzinfo=NY).astimezone(UTC)
    # `on` for the second half of the day; first-seen at the local 12:00 wall
    # clock. On a 23h day local noon is 11h after midnight (the missing hour is
    # 02:00–03:00), so `on` occupies 82800 − 39600 = 43200s.
    noon_local = dt.datetime(2026, 3, 8, 12, 0, tzinfo=NY).astimezone(UTC)
    now = dt.datetime(2026, 3, 9, 0, 0, tzinfo=NY).astimezone(UTC)
    states = [FakeState("on", noon_local)]
    blocks = E.accumulate_blocks(states, day_start, now, 0.0, now)
    fr = E.compute_frame(
        "today",
        now - dt.timedelta(seconds=1),
        NY,
        blocks,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.window_seconds == pytest.approx(82800.0 - 1, abs=1)  # 23h day
    # HARD literal: 43200s of a 23h day = 52.2%. NOT derived from the code's
    # denominator. A `secs / 86400` mutation yields 50.0 here and this FAILS.
    assert blocks["on"]["secs"] == pytest.approx(43200.0)
    assert fr.breakdown_pct["on"] == pytest.approx(52.2, abs=0.05)
    # Decisively not the 24h-denominator value (that would be 50.0).
    assert fr.breakdown_pct["on"] > 50.0


def test_compute_frame_avg_duration_none_at_zero_count() -> None:
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    # A ledger-only closed day contributed secs with count 0 (midnight-spanning
    # visit counted on a prior day) → avg_duration is None.
    ledger = {"2026-01-14": {"on": {"secs": 3600.0, "count": 0}}}
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        {},
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.avg_duration["on"] is None


def test_compute_frame_no_tracked_states_percent_none() -> None:
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {"on": {"secs": 100.0, "count": 1}},
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.percent is None
    assert fr.compliance_percent is None


def test_subset_percent_zero_window_none() -> None:
    assert E._subset_percent({"on": 10.0}, ["on"], 0.0) is None


def test_subset_percent_empty_subset_none() -> None:
    assert E._subset_percent({"on": 10.0}, [], 100.0) is None


# --------------------------------------------------------------------------- #
# query_recorder — recorder-off (None) and normal paths
# --------------------------------------------------------------------------- #


async def test_query_recorder_none_when_recorder_off() -> None:
    # get_instance RAISES KeyError when the recorder is not set up (verified
    # against HA core: it reads hass.data[DATA_INSTANCE] via lru_cache). The
    # dead `instance is None` branch is gone; the KeyError is the absence signal
    # and query_recorder maps it to None so the caller falls back to live-only.
    hass = MagicMock()
    with patch(
        "homeassistant.components.recorder.get_instance",
        side_effect=KeyError("recorder"),
    ):
        result = await E.query_recorder(
            hass, "sensor.x", _utc(2026, 1, 1), _utc(2026, 1, 2)
        )
    assert result is None


async def test_query_recorder_returns_state_list() -> None:
    hass = MagicMock()
    sentinel = [FakeState("on", _utc(2026, 1, 1))]

    async def _run(func, *args):
        return func(*args)

    instance = MagicMock()
    instance.async_add_executor_job = _run

    with (
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.history.state_changes_during_period",
            return_value={"sensor.x": sentinel},
        ) as query,
    ):
        result = await E.query_recorder(
            hass, "sensor.x", _utc(2026, 1, 1), _utc(2026, 1, 2)
        )
    assert result == sentinel
    _, kwargs = query.call_args
    assert kwargs["include_start_time_state"] is True
    assert kwargs["no_attributes"] is True


async def test_query_recorder_missing_entity_returns_empty() -> None:
    hass = MagicMock()

    async def _run(func, *args):
        return func(*args)

    instance = MagicMock()
    instance.async_add_executor_job = _run

    with (
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.history.state_changes_during_period",
            return_value={},  # entity absent from result
        ),
    ):
        result = await E.query_recorder(
            hass, "sensor.x", _utc(2026, 1, 1), _utc(2026, 1, 2)
        )
    assert result == []


# --------------------------------------------------------------------------- #
# Fix 3 — percentages: tiny-slice sentinel + unaccounted_seconds
# --------------------------------------------------------------------------- #


def test_f3_breakdown_pct_and_unaccounted_sum_to_about_100_on_gap_frame() -> None:
    """breakdown_pct (incl. its "unaccounted" key) sums to ~100 on a partial frame.

    The remainder now lives INSIDE breakdown_pct as an additive "unaccounted"
    key, so a template looping breakdown_pct.values() reaches ~100 on its own —
    while unaccounted_seconds stays a separate top-level attribute and
    breakdown_seconds/counts/avg_duration stay per-state pure (no such key).
    """
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    # "today" window is midnight→noon = 43200s; only 30000s of `on` recorded.
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {"on": {"secs": 30000.0, "count": 1}},
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.window_seconds == pytest.approx(43200.0)
    assert fr.unaccounted_seconds == pytest.approx(13200.0)
    # "unaccounted" is present in breakdown_pct and equals the remainder pct.
    assert "unaccounted" in fr.breakdown_pct
    assert fr.breakdown_pct["unaccounted"] == pytest.approx(
        13200.0 / 43200.0 * 100, abs=0.01
    )
    # Looping breakdown_pct.values() alone now sums to ~100 (no separate add).
    assert sum(fr.breakdown_pct.values()) == pytest.approx(100.0, abs=0.1)
    # The pseudo-key is confined to breakdown_pct — the pure per-state dicts
    # never carry it.
    assert "unaccounted" not in fr.breakdown_seconds
    assert "unaccounted" not in fr.counts
    assert "unaccounted" not in fr.avg_duration


def test_f3_tiny_slice_never_renders_zero() -> None:
    """A nonzero slice too small to round to 2-dp uses the 0.01 sentinel."""
    # 272s of a 604800s (7d) window = 0.0449… % → round(,2)=0.04. Use an even
    # tinier slice to force the round-to-zero → sentinel path.
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        {"blip": {"secs": 30.0, "count": 1}},  # 30/604800*100 = 0.00496 → 0.0
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.breakdown_pct["blip"] == 0.01  # sentinel, NOT 0.0
    assert fr.breakdown_pct["blip"] > 0


def test_f3_pct_helper_sentinel_and_zero_window() -> None:
    """_pct: nonzero→sentinel, true-zero→0.0, zero window→0.0 (branch cover)."""
    assert E._pct(30.0, 604800.0) == 0.01  # rounds to 0 but secs>0 → sentinel
    assert E._pct(0.0, 604800.0) == 0.0  # genuinely zero
    assert E._pct(302400.0, 604800.0) == 50.0  # normal 2-dp value
    assert E._pct(100.0, 0.0) == 0.0  # zero/negative window guard


def test_f3_full_coverage_unaccounted_is_zero() -> None:
    """A window fully attributed to states has unaccounted_seconds == 0."""
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {"on": {"secs": 43200.0, "count": 1}},  # exactly the full 43200s window
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    assert fr.window_seconds == pytest.approx(43200.0)
    assert fr.unaccounted_seconds == pytest.approx(0.0)
    # On a fully-covered frame the additive key is present-with-0.0 (stable) and
    # breakdown_pct still sums to ~100 on its own.
    assert fr.breakdown_pct["unaccounted"] == 0.0
    assert sum(fr.breakdown_pct.values()) == pytest.approx(100.0, abs=0.1)


def test_f3_compliance_percent_unchanged_window_denominator() -> None:
    """Regression guard: compliance_percent still divides by window_seconds.

    30000s of `heat` over the 43200s "today" window = 69.4% — NOT covered-seconds
    (which would be 100%). Pins the full-window denominator for compliance.
    """
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=NY)
    fr = E.compute_frame(
        "today",
        now,
        NY,
        {"heat": {"secs": 30000.0, "count": 1}},
        {},
        None,
        mode="specific_states",
        tracked_states=["heat"],
        target_states=["heat"],
        prior_dominant=None,
    )
    assert fr.window_seconds == pytest.approx(43200.0)
    # 30000 / 43200 * 100 = 69.44… → 1-dp 69.4 (window denominator, not 100.0).
    assert fr.compliance_percent == pytest.approx(69.4)
    assert fr.percent == pytest.approx(69.4)


# --------------------------------------------------------------------------- #
# Rolling-frame seam: recorder recent + ledger whole-days-below-recorder_floor
# (the fix for the 24h/7d partial-oldest-day over-count).
# --------------------------------------------------------------------------- #

# All seven frame keys — the frame-agnostic invariant runs across every one.
_ALL_FRAMES = ("today", "yesterday", "24h", "7d", "30d", "month", "year")


@pytest.mark.parametrize("frame", _ALL_FRAMES)
@pytest.mark.parametrize("tz", [NY, TOKYO])
def test_frame_agnostic_breakdown_never_exceeds_window(
    frame: str, tz: ZoneInfo
) -> None:
    """Σbreakdown ≤ window for EVERY frame — the guard that catches over-count.

    Mid-day ``now``, a ledger populated across the window_start boundary, and a
    straddling open visit. Pre-fix, 24h/7d summed a WHOLE oldest-day ledger
    bucket on top of the recorder recent slice and blew past the window; the
    recorder-seam fix (recent from the recorder, ledger only for whole days
    below recorder_floor) keeps every frame within its window.

    Here recorder_floor == window_start (ample retention), so rolling frames
    pass ledger_upper_local_day = window_start's local day: the ledger's oldest
    partial day is EXCLUDED (recorder owns it), no over-count.
    """
    now = dt.datetime(2026, 5, 20, 15, 40, 40, tzinfo=tz)
    start_utc, _ = E.resolve_frame_bounds(frame, now, tz)
    window_start_local_day = start_utc.astimezone(tz).date().isoformat()

    # A ledger holding a per-day bucket on EVERY day from the window start
    # through today. 3600s/day (not a full 86400) keeps synthetic buckets below
    # any real local-day length — including a 23h DST spring-forward day — so
    # the invariant reflects the SEAM logic, not a synthetic DST over-fill.
    # The window_start day's bucket is the trap the pre-fix 24h/7d wrongly
    # summed on top of the recorder recent slice.
    ledger: dict[str, dict[str, dict[str, float]]] = {}
    day = start_utc.astimezone(tz).date()
    while day <= now.date():
        ledger[day.isoformat()] = {"on": {"secs": 3600.0, "count": 1}}
        day += dt.timedelta(days=1)

    # Model exactly what the coordinator feeds compute_frame per frame kind:
    #   * rolling (24h/7d): recorder recent over [recorder_floor==window_start,
    #     now) as a leading continuation; ledger seam = window_start's day.
    #   * calendar open (today/month/year): recent = today-slice only; ledger
    #     fills closed days (seam = today, the default).
    #   * calendar closed (yesterday/30d): recent empty (window ends at
    #     midnight); ledger fills its whole days (seam = today).
    today_midnight = dt.datetime.combine(now.date(), dt.time(), tzinfo=tz).astimezone(
        dt.UTC
    )
    if frame in ("24h", "7d"):
        recent = {"on": {"secs": (now - start_utc).total_seconds(), "count": 0}}
        upper = window_start_local_day
    elif frame in ("yesterday", "30d"):
        recent = {}
        upper = None
    else:  # today / month / year — open calendar, recent = today slice
        recent = {"on": {"secs": (now - today_midnight).total_seconds(), "count": 0}}
        upper = None

    fr = E.compute_frame(
        frame,
        now,
        tz,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
        ledger_upper_local_day=upper,
    )
    assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1e-6
    assert fr.unaccounted_seconds >= 0.0
    assert sum(fr.breakdown_pct.values()) <= 100.0 + 1.0


@pytest.mark.parametrize("tz", [NY, TOKYO])
def test_rolling_24h_partial_oldest_day_not_whole_bucket(tz: ZoneInfo) -> None:
    """24h @ day2 15:00: the oldest partial day is the recorder's REAL seconds,
    NOT the whole 86400 ledger bucket for day1.

    now = day2 15:00 → window_start = day1 15:00. The ledger holds a full
    86400s "yesterday" bucket for day1, but the recorder covers [day1 15:00,
    now) at real seconds. With recorder_floor == window_start (day1 15:00), the
    ledger seam is day1's local day, so the day1 whole bucket is EXCLUDED — no
    142240s-in-an-86400s-window over-count.
    """
    now = dt.datetime(2026, 5, 20, 15, 0, 0, tzinfo=tz)
    day1 = now.date() - dt.timedelta(days=1)
    # Recorder recent = 24h of continuous "on" as a leading continuation
    # (count 0), i.e. the real covered timeline over the whole window.
    recent = {"on": {"secs": 86400.0, "count": 0}}
    ledger = {
        day1.isoformat(): {"on": {"secs": 86400.0, "count": 1}},  # whole bucket
    }
    fr = E.compute_frame(
        "24h",
        now,
        tz,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
        ledger_upper_local_day=day1.isoformat(),  # recorder_floor's local day
    )
    # Only the recorder's real 86400s counts — the ledger day1 bucket is below
    # the seam's UPPER but it IS the seam day itself (>= upper → excluded).
    assert fr.breakdown_seconds["on"] == pytest.approx(86400.0)
    assert fr.window_seconds == pytest.approx(86400.0)
    assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1e-6
    assert fr.unaccounted_seconds == pytest.approx(0.0)
    assert sum(fr.breakdown_pct.values()) <= 100.0 + 1.0


def test_rolling_count_only_in_window_entries() -> None:
    """count fix: a pre-window-start ledger entry does not inflate a rolling
    frame's count.

    The recorder recent slice for a rolling frame is a leading continuation
    (count 0) when the visit began before recorder_floor; the ledger below the
    seam contributes its own counts, but a bucket ON the seam day (>= upper) is
    excluded, so a whole-day count from the partial oldest day never leaks in.
    """
    now = dt.datetime(2026, 5, 20, 15, 0, 0, tzinfo=NY)
    day1 = now.date() - dt.timedelta(days=1)
    recent = {"on": {"secs": 86400.0, "count": 0}}  # leading continuation
    ledger = {day1.isoformat(): {"on": {"secs": 86400.0, "count": 5}}}
    fr = E.compute_frame(
        "24h",
        now,
        NY,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
        ledger_upper_local_day=day1.isoformat(),
    )
    # Seam day excluded → the ledger's count of 5 does not leak; the recorder
    # continuation contributes 0. So count is 0 (a single unbroken visit that
    # started before the window).
    assert fr.counts["on"] == 0


def test_retention_edge_ledger_fills_head_below_recorder_floor() -> None:
    """7d with keep_days=5: recorder covers [now-5d, now); the purged head
    [now-7d, now-5d) falls to the ledger as WHOLE days below recorder_floor.

    Simulates what the coordinator passes when retention < window: recent =
    recorder's [now-5d, now) blocks, ledger_upper = recorder_floor's local day
    (now-5d). Ledger days below that seam are summed; the day AT the seam is
    owned by the recorder (excluded from the ledger). No double-count.
    """
    now = dt.datetime(2026, 5, 20, 15, 0, 0, tzinfo=NY)
    recorder_floor = now - dt.timedelta(days=5)
    floor_day = recorder_floor.astimezone(NY).date().isoformat()
    # Recorder recent: 5 days of continuous "on" as a leading continuation.
    recent = {"on": {"secs": 5 * 86400.0, "count": 0}}
    # Ledger head: the 2 purged whole days [now-7d, now-5d) plus a bucket ON the
    # seam day (which must be EXCLUDED — the recorder owns it).
    d = now.astimezone(NY).date()
    ledger = {
        (d - dt.timedelta(days=7)).isoformat(): {"on": {"secs": 40000.0, "count": 1}},
        (d - dt.timedelta(days=6)).isoformat(): {"on": {"secs": 86400.0, "count": 1}},
        floor_day: {"on": {"secs": 99999.0, "count": 9}},  # seam day — excluded
    }
    fr = E.compute_frame(
        "7d",
        now,
        NY,
        recent,
        ledger,
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
        ledger_upper_local_day=floor_day,
    )
    # recorder 5*86400 + ledger head (40000 + 86400); seam-day 99999 excluded.
    assert fr.breakdown_seconds["on"] == pytest.approx(5 * 86400.0 + 40000.0 + 86400.0)
    assert fr.counts["on"] == 0 + 1 + 1  # seam day's count 9 excluded
    assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1e-6


def test_invariant_guard_clamps_overflow_to_zero() -> None:
    """v8/L4: Σbreakdown > window clamps unaccounted to 0.0 (never negative).

    The engine is now warning-free — it only clamps; the once-per-(entry, frame)
    diagnostic warning moved to the coordinator (L4), tested in
    ``tests/test_coordinator.py``. Here we pin the pure engine's clamp: an
    over-count leaves ``unaccounted_seconds == 0.0``, never negative, never
    raising.
    """
    now = dt.datetime(2026, 5, 20, 15, 0, 0, tzinfo=NY)
    # Force an over-count: recent alone exceeds the 24h window.
    recent = {"on": {"secs": 200000.0, "count": 1}}  # > 86400 window
    fr = E.compute_frame(
        "24h",
        now,
        NY,
        recent,
        {},
        None,
        mode="all_states",
        tracked_states=None,
        target_states=None,
        prior_dominant=None,
    )
    # Clamped, never negative.
    assert fr.unaccounted_seconds == 0.0
    # The breakdown itself is untouched (clamp only affects unaccounted).
    assert fr.breakdown_seconds["on"] == pytest.approx(200000.0)
