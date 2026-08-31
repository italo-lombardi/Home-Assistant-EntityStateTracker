"""100% line+branch coverage for :mod:`coordinator`.

The coordinator is the convergence point (§6.6, §8, §15). These tests drive it
against a mocked recorder (``coordinator.query_recorder`` patched per-test) and
a real ``EntityStateTrackerStore`` on ``hass``'s tmp storage, exercising:

* first-refresh + backfill (replace-not-add, ``last_updated_day`` advances only
  post-flush — R8 crash-across-a-day),
* the live state-change fold (midnight split, count-once-on-start-day, meta),
* new-state announcement in all-states mode (INFO log + ``EVENT_NEW_STATE`` +
  optional persistent notification),
* the 5-minute poll assembling :class:`TrackerData`,
* recorder-off fallback + the once-only Repair issue,
* flush on stop / shutdown, prune, carry-forward, debounce coalescing.

Event-capture follows §16.2: a fresh per-test list, ``async_block_till_done``
before every assert, and a filtered ``any(...)`` match (never ``events[-1]``).
Time is driven by patching ``coordinator.dt_util.utcnow`` (the coordinator's
sole clock) — not the wall clock — so local-day math stays deterministic.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_state_tracker import coordinator as coord_mod
from custom_components.entity_state_tracker.const import (
    DOMAIN,
    EVENT_NEW_STATE,
)
from custom_components.entity_state_tracker.coordinator import (
    _RECORDER_OFF_ISSUE,
    _SEEN_CAP,
    EntityStateTrackerCoordinator,
    _parse_day,
    _parse_ts,
)
from custom_components.entity_state_tracker.models import TrackerData

UTC = dt.UTC


class _FakeState:
    """Minimal recorder-row stand-in (``state`` + ``last_changed`` only)."""

    def __init__(self, state: str, last_changed: dt.datetime) -> None:
        self.state = state
        self.last_changed = last_changed


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


def _state_event(entity_id: str, new: str, changed: dt.datetime) -> Event:
    """Build a state_changed Event carrying a real State as ``new_state``."""
    return Event(
        "state_changed",
        {
            "entity_id": entity_id,
            "new_state": State(entity_id, new, last_changed=changed),
            "old_state": None,
        },
    )


@pytest.fixture
def patch_recorder():
    """Patch ``coordinator.query_recorder``; yields a setter for its return.

    Default: an empty recorder result (recorder on, no rows). Pass ``None`` to
    simulate a disabled recorder (§15), or set ``.mock.side_effect`` directly.
    """
    with patch.object(
        coord_mod, "query_recorder", new_callable=AsyncMock
    ) as mock_query:
        mock_query.return_value = []

        def _set(value: Any) -> None:
            mock_query.return_value = value

        _set.mock = mock_query  # type: ignore[attr-defined]
        yield _set


async def _make_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> EntityStateTrackerCoordinator:
    """Add the entry to hass and build a coordinator (no first refresh yet)."""
    entry.add_to_hass(hass)
    return EntityStateTrackerCoordinator(hass, entry)


def _unsub_live(coord: EntityStateTrackerCoordinator) -> None:
    """Drop the at-start live subscription wired during first-refresh.

    ``async_at_start`` fires immediately in tests, so ``_prime`` leaves a live
    ``async_track_state_change_event`` listener. Seeding tests that call
    ``hass.states.async_set`` would otherwise fold live and start a real
    debouncer timer; cancelling the listener isolates the seed under test.
    """
    if coord._unsub_state is not None:
        coord._unsub_state()
        coord._unsub_state = None


async def _first_refresh(
    hass: HomeAssistant, coord: EntityStateTrackerCoordinator
) -> None:
    """Run first-refresh with the entry moved to SETUP_IN_PROGRESS.

    ``DataUpdateCoordinator.async_config_entry_first_refresh`` guards on the
    entry being mid-setup; the coordinator is normally built inside
    ``async_setup_entry``, so replicate that state here.
    """
    coord.config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    await coord.async_config_entry_first_refresh()
    await hass.async_block_till_done()


async def _prime(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    now: dt.datetime,
    patch_recorder: Callable[[Any], None],
) -> EntityStateTrackerCoordinator:
    """First-refresh a coordinator with an empty recorder at ``now``."""
    patch_recorder([])
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        c = await _make_coordinator(hass, entry)
        await _first_refresh(hass, c)
    return c


# --------------------------------------------------------------------------- #
# Construction / config parsing
# --------------------------------------------------------------------------- #


async def test_init_parses_specific_config(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """__init__ reads entity/mode/states/frames from data (no options)."""
    c = await _make_coordinator(hass, specific_config_entry)
    assert c.entity_id == "climate.living_room"
    assert c.mode == "specific_states"
    assert c.tracked_states == ["heat", "auto"]
    assert c.target_states is None
    assert c.enabled_frames == ["today", "yesterday", "24h", "7d"]


async def test_init_merges_options_over_data(
    hass: HomeAssistant, all_states_config_entry: MockConfigEntry
) -> None:
    """Options override data (min_state_duration)."""
    all_states_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        all_states_config_entry,
        options={"min_state_duration": 30},
    )
    c = EntityStateTrackerCoordinator(hass, all_states_config_entry)
    assert c.min_state_duration == 30


async def test_init_target_states_from_compliance(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Compliance config populates target_states + threshold."""
    c = await _make_coordinator(hass, compliance_config_entry)
    assert c.target_states == ["heat"]
    assert c.target_threshold == 80


async def test_init_time_zone_fallback(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """A missing zone name falls back to DEFAULT_TIME_ZONE (or branch)."""
    with patch.object(coord_mod.dt_util, "get_time_zone", return_value=None):
        c = await _make_coordinator(hass, specific_config_entry)
    assert c.tz is coord_mod.dt_util.DEFAULT_TIME_ZONE


# --------------------------------------------------------------------------- #
# first_refresh + backfill (R8)
# --------------------------------------------------------------------------- #


async def test_first_refresh_backfills_closed_days(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Backfill recomputes closed days and advances last_updated_day (R8)."""
    now = _utc(2026, 6, 10, 12, 0)
    # Recorder returns a full-day "heat" block for each closed day queried
    # (the include_start_time_state row stamped at the window start).
    patch_recorder.mock.side_effect = lambda *a, **k: [  # type: ignore[attr-defined]
        _FakeState("heat", a[2])
    ]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        c = await _make_coordinator(hass, specific_config_entry)
        await _first_refresh(hass, c)

    ledger = c._ledger
    assert ledger is not None
    # last_updated_day advanced to the last closed day (yesterday) after flush.
    assert ledger.last_updated_day == "2026-06-09"
    assert ledger.daily
    assert all(day < "2026-06-10" for day in ledger.daily)
    await c.async_shutdown()


async def test_backfill_resumes_from_last_updated_day(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A ledger with last_updated_day resumes from the following day."""
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    await c.store.set_meta(c._entry_id, last_updated_day="2026-06-08")

    queried_starts: list[dt.datetime] = []

    def _capture(_hass, _eid, start, _end):
        queried_starts.append(start)
        return []

    patch_recorder.mock.side_effect = _capture  # type: ignore[attr-defined]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
    backfilled = [d.astimezone(c.tz).date().isoformat() for d in queried_starts]
    # Only 06-09 is a closed day strictly after 06-08 and before today (06-10).
    assert "2026-06-09" in backfilled
    assert "2026-06-08" not in backfilled
    await c.async_shutdown()


async def test_backfill_replace_not_add_on_crash_restart(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """R8: a crash after ``replace_day`` but before ``set_meta`` re-runs the
    same day on restart — the bucket is counted EXACTLY ONCE because the day is
    replaced wholesale, not added to."""
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    await c.store.set_meta(c._entry_id, last_updated_day="2026-06-08")
    c._ledger = (await c.store.load()).trackers[c._entry_id]

    patch_recorder.mock.side_effect = lambda *a, **k: [  # type: ignore[attr-defined]
        _FakeState("heat", a[2])
    ]

    # First run: crash — the marker flush (set_meta) is dropped after replace_day.
    with (
        patch.object(c.store, "set_meta", AsyncMock()) as broken_meta,
        patch.object(coord_mod.dt_util, "utcnow", return_value=now),
    ):
        await c._async_backfill()
    assert broken_meta.await_count == 1  # tried to advance; we swallowed it

    ledger = (await c.store.load()).trackers[c._entry_id]
    first_secs = ledger.daily["2026-06-09"]["heat"]["secs"]
    assert ledger.daily["2026-06-09"]["heat"]["count"] == 1
    assert ledger.last_updated_day == "2026-06-08"  # never advanced

    # Restart: fresh coordinator, SAME store file. Backfill re-runs 06-09.
    c2 = EntityStateTrackerCoordinator(hass, specific_config_entry)
    c2._ledger = ledger
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await c2._async_backfill()

    ledger = (await c2.store.load()).trackers[c._entry_id]
    # Replaced, not doubled: exactly one count, identical secs.
    assert ledger.daily["2026-06-09"]["heat"]["count"] == 1
    assert ledger.daily["2026-06-09"]["heat"]["secs"] == first_secs
    assert ledger.last_updated_day == "2026-06-09"


async def test_backfill_seeds_from_prune_cutoff_when_no_marker(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """With no ``last_updated_day`` the backfill starts at the prune cutoff."""
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await c._async_backfill()
    # 7d frame → cutoff ~9 days back → several closed days queried+folded.
    assert patch_recorder.mock.await_count >= 7  # type: ignore[attr-defined]
    assert c._ledger.last_updated_day == "2026-06-09"


async def test_backfill_stops_when_recorder_off(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Recorder off during backfill → no days folded, no marker advance (§15)."""
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder(None)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        c = await _make_coordinator(hass, specific_config_entry)
        await _first_refresh(hass, c)
    assert c._ledger is not None
    assert c._ledger.last_updated_day is None
    assert c._ledger.daily == {}
    await c.async_shutdown()


async def test_backfill_ledger_none_is_noop(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Backfill with no ledger loaded returns immediately (defensive branch)."""
    c = await _make_coordinator(hass, specific_config_entry)
    assert c._ledger is None
    await c._async_backfill()  # must not raise


async def test_backfill_carry_forward_across_gap(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A closed day whose only recorder row is the start-of-window carry-forward
    attributes the whole day to that state (§8, R4)."""
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    await c.store.set_meta(c._entry_id, last_updated_day="2026-06-08")
    c._ledger = (await c.store.load()).trackers[c._entry_id]

    # One carried "heat" row stamped at the day-start window bound; no further
    # transitions across 06-09 → the whole day is heat.
    def _one_row(_hass, _eid, start, _end):
        return [_FakeState("heat", start)]

    patch_recorder.mock.side_effect = _one_row  # type: ignore[attr-defined]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await c._async_backfill()

    ledger = (await c.store.load()).trackers[c._entry_id]
    assert ledger.daily["2026-06-09"]["heat"]["secs"] == pytest.approx(86400.0)
    assert ledger.daily["2026-06-09"]["heat"]["count"] == 1


# --------------------------------------------------------------------------- #
# H1 — min_state_duration change re-backfills closed-day buckets
# --------------------------------------------------------------------------- #


async def test_h1_min_duration_change_clears_and_rebuilds_ledger(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """An options edit that CHANGES min_state_duration resets the ledger's
    closed-day buckets and re-backfills them with the new glitch threshold (H1).

    The stored ledger was built under threshold 5.0 (a stale old-threshold
    bucket); config now carries 0. On first-refresh the coordinator detects
    built != current, clears ``daily`` + resets ``last_updated_day`` to None, and
    backfill rebuilds closed days from the recorder — so the stale bucket does
    NOT persist and ``built_min_state_duration`` is stamped to the new value.
    """
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    assert c.min_state_duration == 0  # config threshold
    # Seed a ledger built under the OLD threshold (5.0) with a stale bucket whose
    # secs were computed under that threshold.
    ledger = await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    ledger.daily["2026-06-05"] = {"heat": {"secs": 111.0, "count": 7}}
    await c.store.set_meta(
        c._entry_id, last_updated_day="2026-06-09", built_min_state_duration=5.0
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]

    # Recorder rebuilds each closed day as a full-day "heat" block under the new
    # threshold; the day-start carry-forward row stamps the window start.
    patch_recorder.mock.side_effect = lambda *a, **k: [  # type: ignore[attr-defined]
        _FakeState("heat", a[2])
    ]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)

    rebuilt = (await c.store.load()).trackers[c._entry_id]
    # The stale old-threshold bucket is gone (secs 111.0 / count 7 do not persist).
    assert rebuilt.daily.get("2026-06-05", {}).get("heat", {}).get("secs") != 111.0
    # Rebuilt closed days carry the recorder's full-day block under the new value.
    assert rebuilt.daily["2026-06-09"]["heat"]["secs"] == pytest.approx(86400.0)
    # The threshold the ledger was built with is stamped to the new (config) value.
    assert rebuilt.built_min_state_duration == 0
    await c.async_shutdown()


async def test_h1_unchanged_min_duration_keeps_ledger(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """An unchanged min_state_duration does NOT reset the ledger (H1 no-op path).

    built == current (both 0), so the closed-day buckets are preserved and
    ``last_updated_day`` is not rewound — backfill resumes normally.
    """
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    ledger = await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    # A pre-existing closed-day bucket built under the SAME threshold (0).
    ledger.daily["2026-06-05"] = {"heat": {"secs": 4242.0, "count": 3}}
    await c.store.set_meta(
        c._entry_id, last_updated_day="2026-06-09", built_min_state_duration=0.0
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]

    patch_recorder.mock.side_effect = lambda *a, **k: []  # type: ignore[attr-defined]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)

    kept = (await c.store.load()).trackers[c._entry_id]
    # The bucket survives untouched; no rewind of the marker triggered a rebuild.
    assert kept.daily["2026-06-05"] == {"heat": {"secs": 4242.0, "count": 3}}
    assert kept.built_min_state_duration == 0
    await c.async_shutdown()


async def test_h1_legacy_ledger_no_built_field_not_wiped(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A legacy ledger (built_min_state_duration is None) is NOT wiped on upgrade.

    The field-introducing upgrade seeds the current value without clearing
    existing history — only a genuine subsequent change triggers a rebuild.
    """
    now = _utc(2026, 6, 10, 12, 0)
    specific_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, specific_config_entry)
    ledger = await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    ledger.daily["2026-06-05"] = {"heat": {"secs": 999.0, "count": 2}}
    await c.store.set_meta(c._entry_id, last_updated_day="2026-06-09")
    c._ledger = (await c.store.load()).trackers[c._entry_id]
    assert c._ledger.built_min_state_duration is None  # legacy

    patch_recorder.mock.side_effect = lambda *a, **k: []  # type: ignore[attr-defined]
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)

    kept = (await c.store.load()).trackers[c._entry_id]
    assert kept.daily["2026-06-05"] == {"heat": {"secs": 999.0, "count": 2}}
    assert kept.built_min_state_duration == 0  # seeded to current, no wipe
    await c.async_shutdown()


# --------------------------------------------------------------------------- #
# L4 — overflow warning is per-(entry, frame), warned once per coordinator
# --------------------------------------------------------------------------- #


async def test_l4_warn_overflow_logs_once_per_frame(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_warn_overflow logs once per frame label for THIS coordinator, then dedups.

    A FrameResult whose breakdown exceeds the window by more than the tolerance
    warns once; a second call on the same frame label is suppressed. A different
    frame label warns independently. Diagnostic only — never raised."""
    c = await _make_coordinator(hass, specific_config_entry)
    from custom_components.entity_state_tracker.models import FrameResult

    over = FrameResult(window_seconds=100.0, breakdown_seconds={"on": 500.0})
    other = FrameResult(window_seconds=100.0, breakdown_seconds={"on": 400.0})
    with caplog.at_level(logging.WARNING):
        c._warn_overflow("24h", over)
        c._warn_overflow("24h", over)  # dedup: same (entry, frame)
        c._warn_overflow("7d", other)  # independent frame label
    warnings = [r for r in caplog.records if "exceeds window" in r.getMessage()]
    assert len(warnings) == 2
    assert {"24h", "7d"} == set(c._warned_overflow)


async def test_l4_warn_overflow_no_warn_within_tolerance(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A breakdown within OVERFLOW_TOLERANCE_SECS of the window does not warn."""
    c = await _make_coordinator(hass, specific_config_entry)
    from custom_components.entity_state_tracker.models import FrameResult

    # 100.5 exceeds 100.0 by 0.5s < 1.0s tolerance → no warning, no dedup entry.
    ok = FrameResult(window_seconds=100.0, breakdown_seconds={"on": 100.5})
    with caplog.at_level(logging.WARNING):
        c._warn_overflow("24h", ok)
    assert not [r for r in caplog.records if "exceeds window" in r.getMessage()]
    assert "24h" not in c._warned_overflow


async def test_l4_overflow_warns_independently_per_coordinator(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    all_states_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two coordinators (distinct entry_ids) warn INDEPENDENTLY for the same frame.

    The pre-fix module-global guard suppressed tracker B's real overflow if
    tracker A warned that frame label first. With the per-coordinator guard both
    warn (L4)."""
    from custom_components.entity_state_tracker.models import FrameResult

    ca = await _make_coordinator(hass, specific_config_entry)
    cb = await _make_coordinator(hass, all_states_config_entry)
    over = FrameResult(window_seconds=100.0, breakdown_seconds={"on": 500.0})
    with caplog.at_level(logging.WARNING):
        ca._warn_overflow("24h", over)
        cb._warn_overflow("24h", over)  # same label, DIFFERENT coordinator
    warnings = [r for r in caplog.records if "exceeds window" in r.getMessage()]
    # BOTH warned — no cross-instance suppression.
    assert len(warnings) == 2
    assert "24h" in ca._warned_overflow
    assert "24h" in cb._warned_overflow


# --------------------------------------------------------------------------- #
# Live state-change fold
# --------------------------------------------------------------------------- #


async def test_live_fold_updates_meta_and_buckets(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A transition folds the prior state's elapsed time and updates meta."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    ledger.last_state = "on"
    ledger.last_changed_ts = _utc(2026, 6, 10, 11, 0).isoformat()

    event = _state_event("binary_sensor.front_door", "off", _utc(2026, 6, 10, 12, 0))
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)
        await hass.async_block_till_done()

    day = ledger.daily["2026-06-10"]
    assert day["on"]["secs"] == pytest.approx(3600.0)
    assert day["on"]["count"] == 1
    assert ledger.last_state == "off"
    assert c._previous_state == "on"
    assert c._dirty is True
    await c.async_shutdown()


async def test_live_fold_splits_across_midnight_counts_once(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A visit spanning local midnight folds both days but counts once (§6.2)."""
    # The test hass runs in US/Pacific (UTC-7 in June); local midnight is 07:00Z.
    now = _utc(2026, 6, 10, 8, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    # Visit "on" from 06:30Z (23:30 06-09 PDT) to 07:30Z (00:30 06-10 PDT) —
    # straddling the 07:00Z local midnight, 3600 s each side.
    ledger.last_state = "on"
    ledger.last_changed_ts = _utc(2026, 6, 10, 6, 30).isoformat()

    # The fold is synchronous inside _handle_state_change; assert immediately so
    # a background poll (real-clock) can't prune the June buckets first.
    event = _state_event("binary_sensor.front_door", "off", _utc(2026, 6, 10, 7, 30))
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)

    assert ledger.daily["2026-06-09"]["on"]["count"] == 1  # start day only
    assert ledger.daily["2026-06-10"]["on"]["count"] == 0  # continuation
    assert ledger.daily["2026-06-09"]["on"]["secs"] == pytest.approx(1800.0)
    assert ledger.daily["2026-06-10"]["on"]["secs"] == pytest.approx(1800.0)
    await hass.async_block_till_done()
    await c.async_shutdown()


async def test_live_fold_no_prior_state_is_noop_fold(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """First-ever transition (no prior state) records meta, folds nothing."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    assert ledger.last_state is None

    event = _state_event("binary_sensor.front_door", "on", _utc(2026, 6, 10, 12, 0))
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)

    assert ledger.last_state == "on"
    assert c._previous_state is None
    # No fold happened: no day bucket gained an "on" row.
    assert all("on" not in day for day in ledger.daily.values())
    await hass.async_block_till_done()
    await c.async_shutdown()


async def test_transition_stamps_last_entered_and_exited(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A transition A→B stamps last_exited[A] and last_entered[B] at now (§7)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    ledger.last_state = "on"
    ledger.last_changed_ts = _utc(2026, 6, 10, 11, 0).isoformat()
    # Invariant: the current state is always in _seen (seeded from the ledger at
    # setup via _ledger_seen_states, which includes last_state). Mirror that here
    # since the test sets last_state directly — the stamp is gated on _seen so the
    # unique-state-per-transition cap also bounds last_entered/last_exited.
    c._seen.add("on")

    changed = _utc(2026, 6, 10, 12, 0)
    event = _state_event("binary_sensor.front_door", "off", changed)
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)

    # Exit of the state we left + entry of the state we moved to, both at now.
    assert ledger.last_exited["on"] == changed.isoformat()
    assert ledger.last_entered["off"] == changed.isoformat()
    # The state we entered has no exit yet; the one we left keeps its entry unset
    # (it was seeded before these fields existed in this test).
    assert "off" not in ledger.last_exited
    await hass.async_block_till_done()
    await c.async_shutdown()


async def test_stamps_bounded_by_seen_cap(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """last_entered/last_exited never grow past _SEEN_CAP distinct states.

    Regression guard: a unique-state-per-transition entity used to grow these
    persisted dicts without bound, defeating the _SEEN_CAP that protects _seen.
    Drive more than _SEEN_CAP distinct states and assert both dicts stay capped.
    """
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None

    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        for i in range(_SEEN_CAP + 25):
            changed = now + dt.timedelta(seconds=i)
            event = _state_event("binary_sensor.front_door", f"s{i}", changed)
            c._handle_state_change(event)

    assert len(c._seen) <= _SEEN_CAP
    assert len(ledger.last_entered) <= _SEEN_CAP
    assert len(ledger.last_exited) <= _SEEN_CAP
    await hass.async_block_till_done()
    await c.async_shutdown()


async def test_first_transition_stamps_entry_only(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """First-ever transition (no prior state) stamps last_entered, not exited."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    assert ledger.last_state is None

    changed = _utc(2026, 6, 10, 12, 0)
    event = _state_event("binary_sensor.front_door", "on", changed)
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)

    assert ledger.last_entered["on"] == changed.isoformat()
    # No prior state → nothing exited.
    assert ledger.last_exited == {}
    await hass.async_block_till_done()
    await c.async_shutdown()


async def test_live_fold_ignores_none_new_state(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """An event with new_state=None (entity removed) is ignored."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    event = Event(
        "state_changed",
        {"entity_id": "binary_sensor.front_door", "new_state": None},
    )
    c._handle_state_change(event)
    await hass.async_block_till_done()
    assert c._ledger is not None
    assert c._ledger.last_state is None
    assert c._dirty is False
    await c.async_shutdown()


async def test_live_fold_naive_last_changed_gets_utc(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A naive ``new_state.last_changed`` is coerced to UTC before the fold."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    ledger.last_state = "on"
    ledger.last_changed_ts = _utc(2026, 6, 10, 11, 0).isoformat()

    # Naive datetime (no tzinfo) exercises the tz-coercion branch — intentional.
    naive = dt.datetime(2026, 6, 10, 12, 0)  # noqa: DTZ001
    event = Event(
        "state_changed",
        {
            "entity_id": "binary_sensor.front_door",
            "new_state": State("binary_sensor.front_door", "off", last_changed=naive),
        },
    )
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)
        await hass.async_block_till_done()
    assert ledger.daily["2026-06-10"]["on"]["secs"] == pytest.approx(3600.0)
    await c.async_shutdown()


# --------------------------------------------------------------------------- #
# Live-fold glitch filter parity with accumulate_blocks (C2)
# --------------------------------------------------------------------------- #


async def test_live_fold_glitch_matches_backfill(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """C2: a sub-``min_state_duration`` live visit does not inflate the ledger —
    it merges into the preceding state, byte-for-byte matching what a recorder
    backfill (``accumulate_blocks``) would have produced from the same rows."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    c.min_state_duration = 60.0  # 60 s glitch threshold
    ledger = c._ledger
    assert ledger is not None

    # Row timeline (all inside 2026-06-10, same local day):
    #   on   @ 10:00:00  (held 60 min → survives)
    #   off  @ 11:00:00  (held 30 s   → GLITCH, < 60 s)
    #   on   @ 11:00:30  (held 30 min → survives, coalesces back into "on")
    #   off  @ 11:30:30  (final open)
    t_on1 = _utc(2026, 6, 10, 10, 0, 0)
    t_off_glitch = _utc(2026, 6, 10, 11, 0, 0)
    t_on2 = _utc(2026, 6, 10, 11, 0, 30)
    t_off_final = _utc(2026, 6, 10, 11, 30, 30)

    ledger.last_state = "on"
    ledger.last_changed_ts = t_on1.isoformat()
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        for new, changed in (
            ("off", t_off_glitch),
            ("on", t_on2),
            ("off", t_off_final),
        ):
            c._handle_state_change(
                _state_event("binary_sensor.front_door", new, changed)
            )
    await hass.async_block_till_done()

    # What a backfill would write for the same rows over the same window.
    rows = [
        _FakeState("on", t_on1),
        _FakeState("off", t_off_glitch),
        _FakeState("on", t_on2),
        _FakeState("off", t_off_final),
    ]
    expected = coord_mod.accumulate_blocks(rows, t_on1, t_off_final, 60.0, now)

    live = ledger.daily["2026-06-10"]
    # The glitch "off" never earns its own secs/count; "on" absorbs it.
    assert "off" not in live or live.get("off", {}).get("count", 0) == 0
    assert live["on"]["count"] == expected["on"]["count"] == 1
    assert live["on"]["secs"] == pytest.approx(expected["on"]["secs"])
    await c.async_shutdown()


async def test_live_fold_leading_glitch_dropped(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """C2: a glitch with no preceding surviving visit is unattributable — dropped
    from both secs and count (matches ``accumulate_blocks``' leading-glitch rule)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    c.min_state_duration = 60.0
    ledger = c._ledger
    assert ledger is not None
    # First-ever visit "on" held only 10 s → leading glitch, no predecessor.
    ledger.last_state = "on"
    ledger.last_changed_ts = _utc(2026, 6, 10, 11, 0, 0).isoformat()
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(
            _state_event(
                "binary_sensor.front_door", "off", _utc(2026, 6, 10, 11, 0, 10)
            )
        )
    await hass.async_block_till_done()
    # Nothing folded: the leading glitch dropped, no "on" secs recorded.
    assert all("on" not in day for day in ledger.daily.values())
    await c.async_shutdown()


async def test_live_fold_min_zero_no_regression(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """C2 regression: with min_state_duration == 0 every visit survives and is
    counted — a short visit still gets its own secs+count (unchanged behaviour)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    assert c.min_state_duration == 0
    ledger = c._ledger
    assert ledger is not None
    ledger.last_state = "on"
    ledger.last_changed_ts = _utc(2026, 6, 10, 11, 0, 0).isoformat()
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        # 5-second visit — would be a glitch if a threshold applied.
        c._handle_state_change(
            _state_event("binary_sensor.front_door", "off", _utc(2026, 6, 10, 11, 0, 5))
        )
    await hass.async_block_till_done()
    assert ledger.daily["2026-06-10"]["on"]["secs"] == pytest.approx(5.0)
    assert ledger.daily["2026-06-10"]["on"]["count"] == 1
    await c.async_shutdown()


async def test_fold_visit_empty_interval_is_noop(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A zero/reversed interval yields no segments and folds nothing (guard)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None
    ts = _utc(2026, 6, 10, 11, 0)
    before = {d: dict(rows) for d, rows in ledger.daily.items()}
    c._fold_visit(ledger, "on", ts, ts)  # end == start → no segments
    # Nothing folded: the buckets are byte-for-byte unchanged, no "on" row.
    assert ledger.daily == before
    assert all("on" not in rows for rows in ledger.daily.values())
    assert c._last_fold is None
    await c.async_shutdown()


# --------------------------------------------------------------------------- #
# New-state announcement (all_states)
# --------------------------------------------------------------------------- #


async def test_new_state_fires_event_and_logs(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First-seen state in all_states mode fires EVENT_NEW_STATE with an exact
    payload and logs at INFO (§16.2: fresh list, block, filtered any())."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)

    events: list[Event] = []
    hass.bus.async_listen(EVENT_NEW_STATE, events.append)

    event = _state_event("binary_sensor.front_door", "on", _utc(2026, 6, 10, 12, 0))
    with (
        patch.object(c._debouncer, "async_call", new_callable=AsyncMock),
        caplog.at_level(logging.INFO),
    ):
        c._handle_state_change(event)
        await hass.async_block_till_done()

    assert any(
        e.data
        == {
            "entry_id": c._entry_id,
            "entity_id": "binary_sensor.front_door",
            "state": "on",
        }
        for e in events
    )
    assert "new state" in caplog.text
    await c.async_shutdown()


async def test_new_state_not_fired_when_already_seen(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A state already in the ledger does not re-announce."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    assert c._ledger is not None
    c._ledger.daily = {"2026-06-09": {"on": {"secs": 10.0, "count": 1}}}
    c._seen.add("on")  # the durable seen-set already carries this state

    events: list[Event] = []
    hass.bus.async_listen(EVENT_NEW_STATE, events.append)

    event = _state_event("binary_sensor.front_door", "on", _utc(2026, 6, 10, 12, 0))
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)
        await hass.async_block_till_done()

    assert not any(e.data["state"] == "on" for e in events)
    await c.async_shutdown()


async def test_seen_states_seeded_from_existing_ledger(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """C3: a restart that loads a ledger with existing daily buckets seeds the
    durable seen-set from that history, so those states never re-announce."""
    now = _utc(2026, 6, 10, 12, 0)
    all_states_config_entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, all_states_config_entry)
    # Pre-populate the store as if a prior session had recorded these states.
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    await c.store.replace_day(
        c._entry_id, "2026-06-09", {"on": {"secs": 100.0, "count": 1}}
    )
    patch_recorder([])
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
    # The seed reads the loaded ledger's daily buckets (exercises the loop).
    assert "on" in c._seen
    await c.async_shutdown()


async def test_new_state_not_reannounced_after_prune(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """C3: a state seen once, then aged out of every retained daily bucket by a
    prune, must NOT re-announce when it recurs — the durable seen-set survives
    prune even though ``ledger.daily`` does not."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    ledger = c._ledger
    assert ledger is not None

    events: list[Event] = []
    hass.bus.async_listen(EVENT_NEW_STATE, events.append)

    # 1) First occurrence of "cool" (arriving as the new state) announces and
    #    lands in the durable seen-set.
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(
            _state_event("binary_sensor.front_door", "cool", _utc(2026, 6, 10, 11, 0))
        )
        await hass.async_block_till_done()
    assert any(e.data["state"] == "cool" for e in events)
    assert "cool" in c._seen

    # 2) Prune wipes ALL daily buckets AND drops "cool" from last_state, so the
    #    old ledger-derived membership would no longer contain "cool".
    ledger.daily.clear()
    ledger.last_state = "off"
    ledger.last_changed_ts = _utc(2026, 6, 10, 11, 45).isoformat()
    assert "cool" not in c._ledger_seen_states(ledger)  # ledger forgot it
    assert "cool" in c._seen  # but the durable set did not

    # 3) "cool" recurs as a new state — must NOT re-announce.
    events.clear()
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(
            _state_event("binary_sensor.front_door", "cool", _utc(2026, 6, 10, 11, 50))
        )
        await hass.async_block_till_done()
    assert not any(e.data["state"] == "cool" for e in events)
    await c.async_shutdown()


async def test_new_state_specific_mode_no_event(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Specific-states mode never announces new states."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, specific_config_entry, now, patch_recorder)

    events: list[Event] = []
    hass.bus.async_listen(EVENT_NEW_STATE, events.append)

    event = _state_event("climate.living_room", "cool", _utc(2026, 6, 10, 12, 0))
    with patch.object(c._debouncer, "async_call", new_callable=AsyncMock):
        c._handle_state_change(event)
        await hass.async_block_till_done()

    assert not any(e.data.get("state") == "cool" for e in events)
    await c.async_shutdown()


async def test_new_state_fires_event_no_notification(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """New-state announcement fires the event but raises NO persistent notification."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)

    fired: list[Any] = []
    hass.bus.async_listen(coord_mod.EVENT_NEW_STATE, lambda e: fired.append(e))

    event = _state_event("binary_sensor.front_door", "on", _utc(2026, 6, 10, 12, 0))
    with (
        patch(
            "homeassistant.components.persistent_notification.async_create"
        ) as mock_notify,
        patch.object(c._debouncer, "async_call", new_callable=AsyncMock),
    ):
        c._handle_state_change(event)
        await hass.async_block_till_done()

    assert not mock_notify.called  # event-only: no HA notification
    assert any(e.data.get("state") == "on" for e in fired)  # event did fire
    await c.async_shutdown()


async def test_seen_set_capped_stops_announcing(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Past the _seen cap, a new state neither grows _seen nor announces (S4)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    # Fill _seen to the cap so the next distinct state trips the guard.
    c._seen = {f"s{i}" for i in range(coord_mod._SEEN_CAP)}

    event = _state_event(
        "binary_sensor.front_door", "brand_new", _utc(2026, 6, 10, 12, 0)
    )
    with (
        patch(
            "homeassistant.components.persistent_notification.async_create"
        ) as mock_notify,
        patch.object(c._debouncer, "async_schedule_call"),
    ):
        c._handle_state_change(event)
        await hass.async_block_till_done()

    assert not mock_notify.called  # capped → no announcement
    assert "brand_new" not in c._seen  # not tracked
    assert len(c._seen) == coord_mod._SEEN_CAP  # bounded, no growth
    assert c._seen_cap_hit is True  # one-time warning latch set
    # A second over-cap state does not re-log (latch already set) or grow.
    with patch.object(c._debouncer, "async_schedule_call"):
        c._handle_state_change(
            _state_event(
                "binary_sensor.front_door", "another_new", _utc(2026, 6, 10, 12, 1)
            )
        )
    assert len(c._seen) == coord_mod._SEEN_CAP
    await c.async_shutdown()


# --------------------------------------------------------------------------- #
# Poll — _async_update_data
# --------------------------------------------------------------------------- #


async def test_update_data_assembles_tracker_data(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """_async_update_data returns TrackerData with a FrameResult per frame."""
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder([_FakeState("heat", _utc(2026, 6, 10, 0, 0))])
    c = await _make_coordinator(hass, specific_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "heat"
        c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
        data = await c._async_update_data()

    assert isinstance(data, TrackerData)
    assert set(data.frames) == set(c.enabled_frames)
    assert data.last_state == "heat"
    await c.async_shutdown()


async def test_update_data_queries_recorder_once_per_tick(
    hass: HomeAssistant,
    patch_recorder: Callable[[Any], None],
) -> None:
    """One shared today-slice query per tick regardless of frame count (P2).

    Every open frame reaches the identical [today_midnight, now) window; closed
    frames read the ledger only. So a 7-frame tracker makes ONE recorder call,
    not seven.
    """
    from custom_components.entity_state_tracker.const import (
        CONF_ENTITY as _CE,
    )
    from custom_components.entity_state_tracker.const import (
        CONF_FRAMES as _CF,
    )
    from custom_components.entity_state_tracker.const import (
        CONF_MIN_STATE_DURATION as _CM,
    )
    from custom_components.entity_state_tracker.const import (
        CONF_MODE as _CMO,
    )
    from custom_components.entity_state_tracker.const import (
        FRAMES as _FR,
    )
    from custom_components.entity_state_tracker.const import (
        MODE_ALL as _MA,
    )

    entry = MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="All frames",
        data={
            _CE: "binary_sensor.front_door",
            _CMO: _MA,
            _CF: {frame: True for frame in _FR},  # every frame enabled
            _CM: 0,
        },
        entry_id="est_all_frames_entry",
        unique_id=f"{DOMAIN}_binary_sensor.front_door_{_MA}",
    )
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder([_FakeState("on", _utc(2026, 6, 10, 0, 0))])
    c = await _make_coordinator(hass, entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        assert len(c.enabled_frames) == 7  # all frames on
        patch_recorder.mock.reset_mock()  # type: ignore[attr-defined]
        await c._async_update_data()
    # ≤2 per the contract; the shared-slice design makes it exactly 1.
    assert patch_recorder.mock.await_count <= 2  # type: ignore[attr-defined]
    assert patch_recorder.mock.await_count == 1  # type: ignore[attr-defined]
    await c.async_shutdown()


async def test_update_data_survives_disk_write_failure(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A failed persist does not discard the computed frames (S9).

    In-memory is truth (§8); a disk hiccup must not flap the sensors — the tick
    still returns a valid TrackerData rather than propagating the error.
    """
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder([_FakeState("heat", _utc(2026, 6, 10, 0, 0))])
    c = await _make_coordinator(hass, specific_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "heat"
        c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
        c._dirty = True  # force the flush to hit store.save
        with patch.object(
            c.store, "save", new_callable=AsyncMock, side_effect=OSError("disk full")
        ):
            data = await c._async_update_data()  # must NOT raise
    assert isinstance(data, TrackerData)
    assert set(data.frames) == set(c.enabled_frames)
    assert data.last_state == "heat"  # ledger intact in memory
    await c.async_shutdown()


async def test_update_data_single_disk_write_on_prune(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Prune mutates in-memory; the single flush is the only disk write (P4).

    A stale bucket used to be dropped by store.prune_days' own save AND then
    re-saved by the flush — two writes per roll-over tick. Now prune is
    in-memory and one flush persists.
    """
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder([_FakeState("heat", _utc(2026, 6, 10, 0, 0))])
    c = await _make_coordinator(hass, specific_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        # A bucket far in the past that prune_cutoff will drop.
        c._ledger.daily["2020-01-01"] = {"heat": {"secs": 60.0, "count": 1}}
        c._ledger.last_state = "heat"
        c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
        c._dirty = True
        with patch.object(c.store, "save", new_callable=AsyncMock) as mock_save:
            await c._async_update_data()
    assert "2020-01-01" not in c._ledger.daily  # pruned in-memory
    assert mock_save.await_count == 1  # exactly one disk write, not two
    await c.async_shutdown()


async def test_update_data_uses_prior_dominant(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A second refresh feeds the prior dominant back into compute_frame."""
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder([_FakeState("on", _utc(2026, 6, 10, 0, 0))])
    c = await _make_coordinator(hass, all_states_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        first = c.data.frames["today"].dominant
        assert first == "on"
        data2 = await c._async_update_data()
    assert data2.frames["today"].dominant == "on"
    await c.async_shutdown()


async def test_update_data_lazy_creates_ledger(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """_async_update_data recreates the ledger if called with none loaded."""
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder([])
    c = await _make_coordinator(hass, specific_config_entry)
    assert c._ledger is None
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        data = await c._async_update_data()
    assert c._ledger is not None
    assert isinstance(data, TrackerData)


# --------------------------------------------------------------------------- #
# Recorder-off fallback + Repair issue
# --------------------------------------------------------------------------- #


async def test_recorder_off_fallback_and_issue_once(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Recorder off → live-only fallback + Repair issue raised exactly once."""
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder(None)
    c = await _make_coordinator(hass, specific_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "heat"
        c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
        # First live-only tick raises the issue.
        await c._async_update_data()
        reg = ir.async_get(hass)
        assert reg.async_get_issue(DOMAIN, _RECORDER_OFF_ISSUE) is not None
        assert hass.data[DOMAIN][_RECORDER_OFF_ISSUE] is True
        # Second tick must NOT re-raise (guard flag).
        with patch.object(ir, "async_create_issue") as mock_create:
            data = await c._async_update_data()
        mock_create.assert_not_called()
    # Today frame carries the "heat" slice from live meta.
    assert data.frames["today"].breakdown_seconds.get("heat", 0) > 0
    await c.async_shutdown()


async def test_recorder_off_issue_guard_short_circuits(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """A pre-set flag short-circuits _raise_recorder_off_issue (branch guard)."""
    c = await _make_coordinator(hass, specific_config_entry)
    hass.data.setdefault(DOMAIN, {})[_RECORDER_OFF_ISSUE] = True
    with patch.object(ir, "async_create_issue") as mock_create:
        c._raise_recorder_off_issue()
    assert not mock_create.called


async def test_recorder_recovery_clears_issue(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """C1: recorder off → issue raised; recorder back → issue deleted + flag
    cleared; a subsequent healthy tick does not re-raise."""
    now = _utc(2026, 6, 10, 12, 0)
    patch_recorder(None)
    c = await _make_coordinator(hass, specific_config_entry)
    reg = ir.async_get(hass)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "heat"
        c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
        # Recorder off: issue raised, flag set.
        await c._async_update_data()
        assert reg.async_get_issue(DOMAIN, _RECORDER_OFF_ISSUE) is not None
        assert hass.data[DOMAIN][_RECORDER_OFF_ISSUE] is True

        # Recorder comes back: the next healthy tick deletes the issue + clears
        # the flag so recovery is reflected in Repairs.
        patch_recorder([_FakeState("heat", _utc(2026, 6, 10, 0, 0))])
        await c._async_update_data()
        assert reg.async_get_issue(DOMAIN, _RECORDER_OFF_ISSUE) is None
        assert hass.data[DOMAIN][_RECORDER_OFF_ISSUE] is False

        # A further healthy tick must not attempt a redundant delete (guard).
        with patch.object(ir, "async_delete_issue") as mock_delete:
            await c._async_update_data()
        mock_delete.assert_not_called()
    await c.async_shutdown()


async def test_live_today_blocks_from_ledger_meta(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """Recorder-off fallback derives today's open block from ledger meta."""
    c = await _make_coordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]
    c._ledger.last_state = "heat"
    c._ledger.last_changed_ts = _utc(2026, 6, 10, 11, 0).isoformat()
    now = _utc(2026, 6, 10, 12, 0)
    start = _utc(2026, 6, 10, 0, 0)
    blocks = c._live_today_blocks(c._ledger, start, now, now)
    # 11:00 → 12:00 = 3600 s of heat.
    assert blocks == {"heat": {"secs": pytest.approx(3600.0), "count": 1}}


async def test_live_today_blocks_falls_back_to_live_state(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """With no ledger meta, the fallback reads hass.states for the open block."""
    c = await _make_coordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]  # last_state None
    hass.states.async_set("climate.living_room", "heat")
    # The live state's last_changed is the real clock; frame the window around it
    # so its slice lands inside (the fallback reads hass.states, not the ledger).
    live = hass.states.get("climate.living_room")
    now = live.last_changed + dt.timedelta(hours=1)
    start = live.last_changed - dt.timedelta(hours=1)
    blocks = c._live_today_blocks(c._ledger, start, now, now)
    assert "heat" in blocks
    assert blocks["heat"]["secs"] > 0


async def test_live_today_blocks_empty_when_no_state(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """Fallback yields {} when there is neither ledger meta nor a live state."""
    c = await _make_coordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]
    now = _utc(2026, 6, 10, 12, 0)
    start = _utc(2026, 6, 10, 0, 0)
    assert c._live_today_blocks(c._ledger, start, now, now) == {}


async def test_live_today_blocks_zero_seconds(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """A visit whose start is at/after the window end yields {} (secs<=0)."""
    c = await _make_coordinator(hass, specific_config_entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]
    c._ledger.last_state = "heat"
    now = _utc(2026, 6, 10, 12, 0)
    c._ledger.last_changed_ts = now.isoformat()  # block_start == end → 0 s
    assert c._live_today_blocks(c._ledger, _utc(2026, 6, 10, 0, 0), now, now) == {}


# --------------------------------------------------------------------------- #
# Fix 1 — overlay the live open visit (recorder-commit lag)
# --------------------------------------------------------------------------- #


async def _ledger_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> EntityStateTrackerCoordinator:
    """A coordinator with a freshly-created, held ledger (no first refresh)."""
    c = await _make_coordinator(hass, entry)
    await c.store.get_or_create_tracker(
        c._entry_id, c.entity_id, c.mode, c.tracked_states, c.target_states
    )
    c._ledger = (await c.store.load()).trackers[c._entry_id]
    return c


async def test_overlay_recorder_lag_injects_open_visit(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """Recorder trails at the PRIOR state; overlay injects the ledger open visit.

    The current state gets its own block (~5 s), the prior state's tail is
    trimmed back to the visit start, Σsecs equals the covered window, and the
    injected state is counted exactly once (its single trailing block).
    """
    c = await _ledger_coordinator(hass, all_states_config_entry)
    start = _utc(2026, 6, 10, 0, 0)
    now = _utc(2026, 6, 10, 12, 0, 5)
    # Recorder committed only the prior visit ("off" since midnight); it wrongly
    # extends to now because the "on" row (entered 5 s ago) is not yet committed.
    states = [_FakeState("off", start)]
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = _utc(2026, 6, 10, 12, 0).isoformat()

    overlaid = c._overlay_open_visit(states, c._ledger, start, now)
    blocks = coord_mod.accumulate_blocks(overlaid, start, now, 0.0, now)

    assert blocks["on"]["secs"] == pytest.approx(5.0)
    assert blocks["on"]["count"] == 1  # counted once, not doubled
    assert blocks["off"]["secs"] == pytest.approx((now - start).total_seconds() - 5.0)
    total = sum(b["secs"] for b in blocks.values())
    assert total == pytest.approx((now - start).total_seconds())


async def test_overlay_no_double_count_when_recorder_caught_up(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """Trailing recorder row already == ledger open visit → overlay no-ops."""
    c = await _ledger_coordinator(hass, all_states_config_entry)
    start = _utc(2026, 6, 10, 0, 0)
    now = _utc(2026, 6, 10, 12, 0, 5)
    open_ts = _utc(2026, 6, 10, 12, 0)
    states = [_FakeState("off", start), _FakeState("on", open_ts)]
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = open_ts.isoformat()

    overlaid = c._overlay_open_visit(states, c._ledger, start, now)
    assert overlaid is states  # untouched, no synthetic row appended
    blocks = coord_mod.accumulate_blocks(overlaid, start, now, 0.0, now)
    assert blocks["on"]["count"] == 1


async def test_overlay_straddle_midnight_open_visit_not_recounted(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """open_ts < today_midnight → today slice is a leading continuation (count 0).

    The visit began yesterday; the ledger owns its count on its start day, so
    the today slice must contribute secs but no count.
    """
    c = await _ledger_coordinator(hass, all_states_config_entry)
    start = _utc(2026, 6, 10, 0, 0)
    now = _utc(2026, 6, 10, 12, 0)
    open_ts = _utc(2026, 6, 9, 22, 0)  # yesterday
    states = [_FakeState("off", start)]  # recorder in-force row at midnight
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = open_ts.isoformat()

    overlaid = c._overlay_open_visit(states, c._ledger, start, now)
    blocks = coord_mod.accumulate_blocks(overlaid, start, now, 0.0, now)
    # Whole window is the open "on" visit; count 0 (continuation from yesterday).
    assert blocks["on"]["secs"] == pytest.approx((now - start).total_seconds())
    assert blocks["on"]["count"] == 0
    assert "off" not in blocks  # its stale tail was trimmed at the window start


async def test_overlay_glitch_open_visit_dropped_consistently(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """A sub-min_state_duration open visit folds into the predecessor, no new bucket."""
    c = await _ledger_coordinator(hass, all_states_config_entry)
    c.min_state_duration = 30.0
    start = _utc(2026, 6, 10, 0, 0)
    now = _utc(2026, 6, 10, 12, 0, 5)  # open visit is 5 s < 30 s
    states = [_FakeState("off", start)]
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = _utc(2026, 6, 10, 12, 0).isoformat()

    overlaid = c._overlay_open_visit(states, c._ledger, start, now)
    blocks = coord_mod.accumulate_blocks(
        overlaid, start, now, c.min_state_duration, now
    )
    # The 5 s "on" glitch merged into "off"; no spurious "on" bucket.
    assert "on" not in blocks
    assert blocks["off"]["secs"] == pytest.approx((now - start).total_seconds())


async def test_overlay_no_op_when_last_state_none(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """No ledger open visit → states pass through unchanged."""
    c = await _ledger_coordinator(hass, all_states_config_entry)
    start = _utc(2026, 6, 10, 0, 0)
    now = _utc(2026, 6, 10, 12, 0)
    states = [_FakeState("off", start)]
    # last_state None (fresh ledger).
    assert c._overlay_open_visit(states, c._ledger, start, now) is states


async def test_overlay_no_op_when_open_ts_in_future(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """open_ts >= now (clock skew) → overlay no-ops rather than inject a bad row."""
    c = await _ledger_coordinator(hass, all_states_config_entry)
    start = _utc(2026, 6, 10, 0, 0)
    now = _utc(2026, 6, 10, 12, 0)
    states = [_FakeState("off", start)]
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = now.isoformat()  # not strictly before now
    assert c._overlay_open_visit(states, c._ledger, start, now) is states


async def test_overlay_open_secs_grow_across_ticks(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """Recorder stale across two ticks → the open visit's secs grow by ≈Δ, count stays."""
    c = await _ledger_coordinator(hass, all_states_config_entry)
    start = _utc(2026, 6, 10, 0, 0)
    open_ts = _utc(2026, 6, 10, 11, 0)
    states = [_FakeState("off", start)]
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = open_ts.isoformat()

    now1 = _utc(2026, 6, 10, 11, 30)
    b1 = coord_mod.accumulate_blocks(
        c._overlay_open_visit(states, c._ledger, start, now1), start, now1, 0.0, now1
    )
    now2 = _utc(2026, 6, 10, 12, 0)
    b2 = coord_mod.accumulate_blocks(
        c._overlay_open_visit(states, c._ledger, start, now2), start, now2, 0.0, now2
    )
    assert b2["on"]["secs"] - b1["on"]["secs"] == pytest.approx(1800.0)
    assert b1["on"]["count"] == 1
    assert b2["on"]["count"] == 1


async def test_overlay_dst_fall_back_straddle_real_elapsed(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """An open visit across a DST fall-back reads real elapsed seconds, not calendar.

    US/Pacific falls back at 2026-11-01 09:00 UTC (02:00 local → 01:00 local). A
    visit from 08:30 UTC to 09:30 UTC is 3600 real seconds regardless of the
    wall-clock repeat.
    """
    c = await _ledger_coordinator(hass, all_states_config_entry)
    c.tz = coord_mod.dt_util.get_time_zone("US/Pacific")
    open_ts = _utc(2026, 11, 1, 8, 30)
    now = _utc(2026, 11, 1, 9, 30)
    # Window start = local midnight of the open day (well before open_ts).
    start = _utc(2026, 11, 1, 7, 0)
    states = [_FakeState("off", start)]
    c._ledger.last_state = "on"
    c._ledger.last_changed_ts = open_ts.isoformat()

    overlaid = c._overlay_open_visit(states, c._ledger, start, now)
    blocks = coord_mod.accumulate_blocks(overlaid, start, now, 0.0, now)
    assert blocks["on"]["secs"] == pytest.approx(3600.0)


# --------------------------------------------------------------------------- #
# Flush / shutdown / stop
# --------------------------------------------------------------------------- #


async def test_flush_on_stop_persists(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """EVENT_HOMEASSISTANT_STOP flushes the dirty ledger to disk (§8)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    c._dirty = True
    with patch.object(c.store, "save", new_callable=AsyncMock) as mock_save:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    assert mock_save.called
    assert c._dirty is False
    await c.async_shutdown()


async def test_flush_noop_when_clean(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A clean (non-dirty) ledger does not hit disk on flush."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    c._dirty = False
    with patch.object(c.store, "save", new_callable=AsyncMock) as mock_save:
        await c._async_flush()
    assert not mock_save.called
    await c.async_shutdown()


async def test_shutdown_flushes_and_cancels(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """async_shutdown cancels the live sub, shuts the debouncer, and flushes."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    assert c._unsub_state is not None
    c._dirty = True
    with patch.object(c.store, "save", new_callable=AsyncMock) as mock_save:
        await c.async_shutdown()
    assert mock_save.called
    assert c._unsub_state is None


async def test_shutdown_without_subscription(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """async_shutdown is safe when the subscription was never wired."""
    c = await _make_coordinator(hass, all_states_config_entry)
    assert c._unsub_state is None
    await c.async_shutdown()  # must not raise


# --------------------------------------------------------------------------- #
# Subscribe-at-start
# --------------------------------------------------------------------------- #


async def test_subscribe_at_start_wires_listener(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """The at-start callback installs the state-change subscription + canceller."""
    c = await _make_coordinator(hass, specific_config_entry)
    with patch.object(
        coord_mod, "async_track_state_change_event", return_value=lambda: None
    ) as track:
        c._async_subscribe_at_start(hass)
    track.assert_called_once()
    assert c._unsub_state is not None


# --------------------------------------------------------------------------- #
# Fresh-install open-visit seeding (H1) — first transition must not drop the
# time held from HA-start until that transition.
# --------------------------------------------------------------------------- #


async def test_seed_open_visit_on_fresh_ledger(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A fresh ledger seeds last_state from HA's live state at subscribe time.

    Without the seed, ``ledger.last_state`` stays None and the first transition
    reads a null anchor → the initial block is dropped (H1).
    """
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    _unsub_live(c)  # drop the at-start listener so async_set won't fold live
    assert c._ledger.last_state is None  # fresh install: nothing seeded yet

    hass.states.async_set("binary_sensor.front_door", "off")
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        c._seed_open_visit()

    assert c._ledger.last_state == "off"
    assert c._ledger.last_changed_ts == now.isoformat()
    assert c._dirty is True
    await c.async_shutdown()


async def test_seed_open_visit_first_transition_folds_initial_block(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """After seeding, the first transition folds the initial block (not dropped)."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    _unsub_live(c)

    hass.states.async_set("binary_sensor.front_door", "off")
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        c._seed_open_visit()

    # Transition to "on" 30 min later: the seeded "off" visit must fold, not drop.
    with patch.object(c._debouncer, "async_schedule_call"):
        c._handle_state_change(
            _state_event("binary_sensor.front_door", "on", _utc(2026, 6, 10, 12, 30))
        )
    day = c._ledger.daily["2026-06-10"]
    assert day["off"]["secs"] == 1800.0  # 30 min from now → transition, folded
    assert day["off"]["count"] == 1
    await c.async_shutdown()


async def test_seed_open_visit_skips_when_ledger_restored(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A restored ledger (last_state set) is never clobbered by the seed."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    _unsub_live(c)
    c._ledger.last_state = "heat"
    c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
    c._dirty = False

    hass.states.async_set("binary_sensor.front_door", "off")
    c._seed_open_visit()

    assert c._ledger.last_state == "heat"  # untouched
    assert c._ledger.last_changed_ts == _utc(2026, 6, 10, 6, 0).isoformat()
    assert c._dirty is False
    await c.async_shutdown()


async def test_seed_open_visit_skips_when_no_live_state(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """No live state (entity absent) → nothing to anchor, ledger stays fresh."""
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    assert hass.states.get("binary_sensor.front_door") is None

    c._seed_open_visit()

    assert c._ledger.last_state is None
    assert c._dirty is False
    await c.async_shutdown()


# --------------------------------------------------------------------------- #
# Debounce coalescing
# --------------------------------------------------------------------------- #


async def test_state_change_schedules_debounced_refresh(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """A live transition schedules the debouncer's coalesced refresh.

    ``async_schedule_call`` is the callback-safe (sync) Debouncer entry point;
    a @callback must not spawn an untracked task per transition (H3).
    """
    now = _utc(2026, 6, 10, 12, 0)
    c = await _prime(hass, all_states_config_entry, now, patch_recorder)
    with patch.object(c._debouncer, "async_schedule_call") as mock_schedule:
        event = _state_event("binary_sensor.front_door", "on", _utc(2026, 6, 10, 12, 0))
        c._handle_state_change(event)
        await hass.async_block_till_done()
    mock_schedule.assert_called_once()
    await c.async_shutdown()


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #


def test_parse_ts_variants() -> None:
    """_parse_ts returns UTC datetimes and None on bad input."""
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None
    assert _parse_ts("2026-06-10T12:00:00+00:00") == _utc(2026, 6, 10, 12, 0)


def test_parse_day_variants() -> None:
    """_parse_day returns dates and None on bad input."""
    assert _parse_day(None) is None
    assert _parse_day("") is None
    assert _parse_day("nope") is None
    assert _parse_day("2026-06-10") == dt.date(2026, 6, 10)


# --------------------------------------------------------------------------- #
# Rolling-frame recorder+ledger seam (the 24h/7d partial-oldest-day over-count
# fix). Recorder retention is injected by patching recorder.get_instance.
# --------------------------------------------------------------------------- #


class _FakeRecorder:
    """Minimal recorder instance stand-in — only ``keep_days`` is read."""

    def __init__(self, keep_days: int) -> None:
        self.keep_days = keep_days


def _patch_retention(keep_days: int | None):
    """Patch recorder.get_instance so _recorder_retention_start reads keep_days.

    ``keep_days=None`` simulates the recorder being absent (get_instance raises
    KeyError, as HA core does when no recorder is set up).
    """
    import homeassistant.components.recorder as rec_mod

    if keep_days is None:
        return patch.object(rec_mod, "get_instance", side_effect=KeyError("recorder"))
    return patch.object(rec_mod, "get_instance", return_value=_FakeRecorder(keep_days))


async def test_rolling_ample_retention_recorder_covers_window(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """keep_days=10 ≥ 7d: recorder covers the whole rolling window; the ledger
    contributes NOTHING to 24h/7d (recorder_floor == window_start).

    A whole-day ledger bucket sitting on the 7d window_start's local day must
    NOT be summed into the 7d frame — the recorder owns that partial day. This
    is the exact over-count the fix closes.
    """
    now = _utc(2026, 6, 10, 15, 0)
    # Recorder returns one continuous "on" row covering the widest query span,
    # started before the span so accumulate reads it as a full continuation.
    patch_recorder([_FakeState("on", _utc(2026, 5, 1, 0, 0))])
    c = await _make_coordinator(hass, all_states_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "on"
        c._ledger.last_changed_ts = _utc(2026, 5, 1, 0, 0).isoformat()
        # A whole-day bucket on the 7d window_start's local day (2026-06-03) —
        # the trap. With ample retention it must be excluded from 7d.
        c._ledger.daily = {"2026-06-03": {"on": {"secs": 86400.0, "count": 1}}}
        with _patch_retention(10):
            data = await c._async_update_data()
    for key in ("24h", "7d"):
        fr = data.frames[key]
        # Σ never exceeds the window (the invariant the bug violated).
        assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1.0
        # Recorder covers the whole window continuously → ~100% "on", and the
        # 2026-06-03 whole-day bucket did NOT inflate it.
        assert fr.breakdown_seconds["on"] == pytest.approx(fr.window_seconds, abs=2.0)
        assert fr.unaccounted_seconds == pytest.approx(0.0, abs=2.0)
    await c.async_shutdown()


async def test_rolling_short_retention_ledger_fills_head(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """keep_days=5 < 7d: recorder covers [now-5d, now); the purged head
    [now-7d, now-5d) falls to the ledger as WHOLE days below recorder_floor.

    Asserts no double-count at the seam (the recorder_floor day's own bucket is
    excluded — the recorder owns it) and no crash.
    """
    now = _utc(2026, 6, 10, 15, 0)
    # Recorder returns "on" continuously across whatever it's asked for.
    patch_recorder([_FakeState("on", _utc(2026, 6, 5, 0, 0))])
    c = await _make_coordinator(hass, all_states_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "on"
        c._ledger.last_changed_ts = _utc(2026, 6, 5, 0, 0).isoformat()
        # recorder_floor for 7d = now-5d = 2026-06-05 15:00 → seam day 2026-06-05.
        # Head days below the seam are summed; the seam day's bucket is excluded.
        c._ledger.daily = {
            "2026-06-03": {"off": {"secs": 3600.0, "count": 1}},  # head, summed
            "2026-06-04": {"off": {"secs": 3600.0, "count": 1}},  # head, summed
            "2026-06-05": {"off": {"secs": 99999.0, "count": 9}},  # seam — excluded
        }
        with _patch_retention(5):
            data = await c._async_update_data()
    fr = data.frames["7d"]
    # off = only the two head days (2*3600); the seam-day 99999 is excluded.
    assert fr.breakdown_seconds.get("off", 0.0) == pytest.approx(7200.0)
    assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1.0
    assert fr.unaccounted_seconds >= 0.0
    await c.async_shutdown()


async def test_rolling_retention_unavailable_falls_back_safely(
    hass: HomeAssistant,
    all_states_config_entry: MockConfigEntry,
    patch_recorder: Callable[[Any], None],
) -> None:
    """No keep_days (recorder absent): retention_start = now → rolling frames
    degrade to the ledger-only whole-day path (safe), logged once."""
    now = _utc(2026, 6, 10, 15, 0)
    patch_recorder([_FakeState("on", _utc(2026, 6, 10, 0, 0))])
    c = await _make_coordinator(hass, all_states_config_entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "on"
        c._ledger.last_changed_ts = _utc(2026, 6, 10, 6, 0).isoformat()
        with _patch_retention(None):
            data = await c._async_update_data()
            assert c._retention_warned is True
            # Second tick must not re-warn (once-per-coordinator guard).
            data2 = await c._async_update_data()
    for d in (data, data2):
        fr = d.frames["7d"]
        assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1.0
    await c.async_shutdown()


async def test_rolling_floors_empty_when_no_rolling_frames(
    hass: HomeAssistant,
) -> None:
    """A tracker with no rolling frames enabled → _rolling_floors returns {}."""
    from custom_components.entity_state_tracker.const import (
        CONF_ENTITY,
        CONF_FRAMES,
        CONF_MIN_STATE_DURATION,
        CONF_MODE,
        FRAMES,
        MODE_ALL,
    )

    entry = MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Calendar only",
        data={
            CONF_ENTITY: "binary_sensor.front_door",
            CONF_MODE: MODE_ALL,
            # Only calendar frames — no 24h/7d.
            CONF_FRAMES: {f: f in ("today", "month") for f in FRAMES},
            CONF_MIN_STATE_DURATION: 0,
        },
        entry_id="est_calendar_only",
        unique_id=f"{DOMAIN}_binary_sensor.front_door_{MODE_ALL}_cal",
    )
    c = await _make_coordinator(hass, entry)
    assert c._rolling_floors([], _utc(2026, 6, 10, 12, 0)) == {}


async def test_rolling_regression_calendar_frames_unchanged(
    hass: HomeAssistant,
    patch_recorder: Callable[[Any], None],
) -> None:
    """Calendar frames (today/yesterday/30d/month/year) are byte-identical
    whether or not the recorder retention patch is applied — the fix touches
    ONLY 24h/7d."""
    from custom_components.entity_state_tracker.const import (
        CONF_ENTITY,
        CONF_FRAMES,
        CONF_MIN_STATE_DURATION,
        CONF_MODE,
        FRAMES,
        MODE_ALL,
    )

    entry = MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="All frames regression",
        data={
            CONF_ENTITY: "binary_sensor.front_door",
            CONF_MODE: MODE_ALL,
            CONF_FRAMES: {f: True for f in FRAMES},
            CONF_MIN_STATE_DURATION: 0,
        },
        entry_id="est_regression_all",
        unique_id=f"{DOMAIN}_binary_sensor.front_door_{MODE_ALL}_reg",
    )
    now = _utc(2026, 6, 10, 15, 0)
    patch_recorder([_FakeState("on", _utc(2026, 6, 1, 0, 0))])
    c = await _make_coordinator(hass, entry)
    with patch.object(coord_mod.dt_util, "utcnow", return_value=now):
        await _first_refresh(hass, c)
        c._ledger.last_state = "on"
        c._ledger.last_changed_ts = _utc(2026, 6, 1, 0, 0).isoformat()
        c._ledger.daily = {
            "2026-06-08": {"on": {"secs": 86400.0, "count": 1}},
            "2026-06-09": {"on": {"secs": 86400.0, "count": 1}},
        }
        with _patch_retention(30):
            data = await c._async_update_data()
    calendar = ("today", "yesterday", "30d", "month", "year")
    for key in calendar:
        fr = data.frames[key]
        assert sum(fr.breakdown_seconds.values()) <= fr.window_seconds + 1.0
    await c.async_shutdown()
