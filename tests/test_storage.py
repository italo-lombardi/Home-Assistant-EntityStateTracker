"""Tests for the Entity State Tracker cached, single-flight store (§8)."""

from __future__ import annotations

import asyncio
from json import JSONDecodeError
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.entity_state_tracker.const import (
    STORAGE_KEY_FMT,
    STORAGE_VERSION,
)
from custom_components.entity_state_tracker.models import StoredData, TrackerLedger
from custom_components.entity_state_tracker.storage import (
    EntityStateTrackerStore,
    _MigratingStore,
)

ENTRY_ID = "est_entry"


def _store(hass: HomeAssistant) -> EntityStateTrackerStore:
    return EntityStateTrackerStore(hass, ENTRY_ID)


async def test_migration_v1_doc_roundtrips_unchanged(hass: HomeAssistant) -> None:
    """M4: the migration hook is a no-op for v1 — a v1 doc loads unchanged.

    The stub exists so a future v2 has a single place to transform old
    documents instead of HA's default ``_async_migrate_func`` raising. For v1
    the document is returned byte-for-byte identical."""
    store = _MigratingStore(
        hass, STORAGE_VERSION, STORAGE_KEY_FMT.format(entry_id=ENTRY_ID)
    )
    doc = {
        "version": STORAGE_VERSION,
        "trackers": {
            ENTRY_ID: {
                "entity_id": "climate.living_room",
                "mode": "all_states",
                "daily": {"2026-08-29": {"heat": {"secs": 12.5, "count": 3}}},
                "built_min_state_duration": 5.0,
            }
        },
    }
    migrated = await store._async_migrate_func(1, 1, doc)
    assert migrated == doc
    assert migrated is doc  # no-op: same object, nothing transformed


async def test_load_cold_then_warm(hass: HomeAssistant) -> None:
    """Cold load hits disk once; the warm load serves from cache (0 more reads)."""
    store = _store(hass)
    assert store.disk_read_count == 0
    first = await store.load()
    assert isinstance(first, StoredData)
    assert store.disk_read_count == 1
    second = await store.load()
    assert second is first
    assert store.disk_read_count == 1


async def test_load_single_flight(hass: HomeAssistant) -> None:
    """Concurrent first-readers collapse to a single disk read."""
    store = _store(hass)
    a, b = await asyncio.gather(store.load(), store.load())
    assert a is b
    assert store.disk_read_count == 1


async def test_load_single_flight_second_waiter_recheck(hass: HomeAssistant) -> None:
    """A waiter that enters the lock after the cache is warm returns without a read."""
    store = _store(hass)

    started = asyncio.Event()
    release = asyncio.Event()

    real_load = store._store.async_load

    async def _slow_load():
        started.set()
        await release.wait()
        return await real_load()

    with patch.object(store._store, "async_load", side_effect=_slow_load):
        first = asyncio.ensure_future(store.load())
        await started.wait()
        # Second reader queues on the lock while the first read is in flight.
        second = asyncio.ensure_future(store.load())
        await asyncio.sleep(0)
        release.set()
        a = await first
        b = await second
    assert a is b
    assert store.disk_read_count == 1


async def test_load_corrupt_json_resets_empty(hass: HomeAssistant) -> None:
    """A JSONDecodeError from disk resets the cache to empty."""
    store = _store(hass)
    with patch.object(
        store._store,
        "async_load",
        side_effect=JSONDecodeError("bad", "doc", 0),
    ):
        data = await store.load()
    assert data == StoredData.empty()
    assert store.disk_read_count == 1


async def test_load_oserror_resets_empty(hass: HomeAssistant) -> None:
    """An OSError from disk resets the cache to empty."""
    store = _store(hass)
    with patch.object(store._store, "async_load", side_effect=OSError("io")):
        data = await store.load()
    assert data == StoredData.empty()


async def test_load_none_resets_empty(hass: HomeAssistant) -> None:
    """A missing file (None) yields empty StoredData."""
    store = _store(hass)
    with patch.object(store._store, "async_load", return_value=None):
        data = await store.load()
    assert data == StoredData.empty()


async def test_load_deserialize_error_resets_empty(hass: HomeAssistant) -> None:
    """A from_dict failure resets to empty."""
    store = _store(hass)
    with (
        patch.object(store._store, "async_load", return_value={"trackers": {}}),
        patch.object(StoredData, "from_dict", side_effect=ValueError("nope")),
    ):
        data = await store.load()
    assert data == StoredData.empty()


async def test_save_writes_and_caches(hass: HomeAssistant) -> None:
    """save() persists and flips the cache to the saved value."""
    store = _store(hass)
    data = StoredData(trackers={ENTRY_ID: TrackerLedger("light.x", "all_states")})
    await store.save(data)
    assert await store.load() is data
    # A fresh store over the same key loads what was written.
    fresh = _store(hass)
    loaded = await fresh.load()
    assert ENTRY_ID in loaded.trackers


async def test_last_entered_exited_survive_reload(hass: HomeAssistant) -> None:
    """Stamps flushed to disk reload intact into a fresh store (§7 persistence)."""
    store = _store(hass)
    ledger = TrackerLedger("light.x", "all_states")
    ledger.last_entered = {"on": "2026-08-31T10:00:00+00:00"}
    ledger.last_exited = {"off": "2026-08-31T09:30:00+00:00"}
    await store.save(StoredData(trackers={ENTRY_ID: ledger}))

    # Fresh store over the same key → cold disk read, not the cache.
    reloaded = (await _store(hass).load()).trackers[ENTRY_ID]
    assert reloaded.last_entered == {"on": "2026-08-31T10:00:00+00:00"}
    assert reloaded.last_exited == {"off": "2026-08-31T09:30:00+00:00"}


async def test_save_rollback_on_oserror(hass: HomeAssistant) -> None:
    """An OSError during write reverts the cache to the previous value."""
    store = _store(hass)
    previous = await store.load()
    data = StoredData(trackers={ENTRY_ID: TrackerLedger("light.x", "all_states")})
    with (
        patch.object(store._store, "async_save", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await store.save(data)
    assert store._data is previous


async def test_save_rollback_on_ha_error(hass: HomeAssistant) -> None:
    """A HomeAssistantError during write reverts the cache to the previous value."""
    store = _store(hass)
    previous = await store.load()
    data = StoredData(trackers={ENTRY_ID: TrackerLedger("light.x", "all_states")})
    with (
        patch.object(
            store._store, "async_save", side_effect=HomeAssistantError("boom")
        ),
        pytest.raises(HomeAssistantError),
    ):
        await store.save(data)
    assert store._data is previous


async def test_remove_clears_cache(hass: HomeAssistant) -> None:
    """remove() deletes the file and drops the cache so the next load is cold."""
    store = _store(hass)
    await store.load()
    assert store.disk_read_count == 1
    await store.remove()
    assert store._data is None
    await store.load()
    assert store.disk_read_count == 2


async def test_get_or_create_tracker_new(hass: HomeAssistant) -> None:
    """A missing tracker is created, persisted, and returned."""
    store = _store(hass)
    ledger = await store.get_or_create_tracker(
        ENTRY_ID, "climate.living_room", "specific_states", ["heat"], ["heat"]
    )
    assert ledger.entity_id == "climate.living_room"
    assert ledger.states == ["heat"]
    assert ledger.target == ["heat"]
    data = await store.load()
    assert data.trackers[ENTRY_ID] is ledger


async def test_get_or_create_tracker_new_null_states(hass: HomeAssistant) -> None:
    """None states/target are stored as None (all-states path)."""
    store = _store(hass)
    ledger = await store.get_or_create_tracker(
        ENTRY_ID, "binary_sensor.door", "all_states", None, None
    )
    assert ledger.states is None
    assert ledger.target is None


async def test_get_or_create_tracker_existing(hass: HomeAssistant) -> None:
    """An existing tracker is returned without a rewrite."""
    store = _store(hass)
    first = await store.get_or_create_tracker(
        ENTRY_ID, "light.x", "all_states", None, None
    )
    with patch.object(store._store, "async_save") as saver:
        second = await store.get_or_create_tracker(
            ENTRY_ID, "light.x", "all_states", None, None
        )
    assert second is first
    saver.assert_not_called()


async def test_fold_into_day_creates_paths(hass: HomeAssistant) -> None:
    """fold_into_day creates the day + state row on first fold."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.fold_into_day(ENTRY_ID, "2026-08-29", "on", 10.0, 1)
    ledger = (await store.load()).trackers[ENTRY_ID]
    assert ledger.daily["2026-08-29"]["on"] == {"secs": 10.0, "count": 1}


async def test_fold_into_day_accumulates(hass: HomeAssistant) -> None:
    """A second fold into the same day/state accumulates secs and count."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.fold_into_day(ENTRY_ID, "2026-08-29", "on", 10.0, 1)
    await store.fold_into_day(ENTRY_ID, "2026-08-29", "on", 5.5, 2)
    row = (await store.load()).trackers[ENTRY_ID].daily["2026-08-29"]["on"]
    assert row == {"secs": 15.5, "count": 3}


async def test_fold_into_day_missing_tracker_raises(hass: HomeAssistant) -> None:
    """fold_into_day on an unknown tracker raises KeyError."""
    store = _store(hass)
    await store.load()
    with pytest.raises(KeyError):
        await store.fold_into_day("nope", "2026-08-29", "on", 1.0, 1)


async def test_replace_day_wholesale(hass: HomeAssistant) -> None:
    """replace_day overwrites the entire day bucket."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.fold_into_day(ENTRY_ID, "2026-08-29", "on", 99.0, 9)
    await store.replace_day(ENTRY_ID, "2026-08-29", {"off": {"secs": 3.0, "count": 1}})
    day = (await store.load()).trackers[ENTRY_ID].daily["2026-08-29"]
    assert day == {"off": {"secs": 3.0, "count": 1}}


async def test_replace_day_missing_tracker_raises(hass: HomeAssistant) -> None:
    """replace_day on an unknown tracker raises KeyError."""
    store = _store(hass)
    await store.load()
    with pytest.raises(KeyError):
        await store.replace_day("nope", "2026-08-29", {})


async def test_set_meta_updates_provided_fields(hass: HomeAssistant) -> None:
    """set_meta writes only the non-None fields."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.set_meta(
        ENTRY_ID,
        last_state="on",
        last_changed_ts="2026-08-29T00:00:00+00:00",
        last_updated_day="2026-08-29",
    )
    ledger = (await store.load()).trackers[ENTRY_ID]
    assert ledger.last_state == "on"
    assert ledger.last_changed_ts == "2026-08-29T00:00:00+00:00"
    assert ledger.last_updated_day == "2026-08-29"


async def test_set_meta_none_leaves_unchanged(hass: HomeAssistant) -> None:
    """None arguments (the sentinel) leave each field untouched."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.set_meta(ENTRY_ID, last_state="on", last_updated_day="2026-08-29")
    # Second call passes all-None: nothing should change.
    await store.set_meta(ENTRY_ID)
    ledger = (await store.load()).trackers[ENTRY_ID]
    assert ledger.last_state == "on"
    assert ledger.last_changed_ts is None
    assert ledger.last_updated_day == "2026-08-29"


async def test_set_meta_missing_tracker_noop(hass: HomeAssistant) -> None:
    """set_meta on an unknown tracker is a no-op (reset-race guard, no write)."""
    store = _store(hass)
    await store.load()
    with patch.object(store._store, "async_save") as saver:
        await store.set_meta("nope", last_state="on")
    saver.assert_not_called()


async def test_prune_days_drops_stale(hass: HomeAssistant) -> None:
    """prune_days removes day keys lexicographically before the cutoff."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.fold_into_day(ENTRY_ID, "2026-08-01", "on", 1.0, 1)
    await store.fold_into_day(ENTRY_ID, "2026-08-15", "on", 1.0, 1)
    await store.fold_into_day(ENTRY_ID, "2026-08-29", "on", 1.0, 1)
    await store.prune_days(ENTRY_ID, "2026-08-15")
    daily = (await store.load()).trackers[ENTRY_ID].daily
    assert set(daily) == {"2026-08-15", "2026-08-29"}


async def test_prune_days_no_stale_early_return(hass: HomeAssistant) -> None:
    """When nothing is stale, prune_days returns without a write."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.fold_into_day(ENTRY_ID, "2026-08-29", "on", 1.0, 1)
    with patch.object(store._store, "async_save") as saver:
        await store.prune_days(ENTRY_ID, "2026-01-01")
    saver.assert_not_called()
    assert (await store.load()).trackers[ENTRY_ID].daily == {
        "2026-08-29": {"on": {"secs": 1.0, "count": 1}}
    }


async def test_prune_days_missing_tracker_noop(hass: HomeAssistant) -> None:
    """prune_days on an unknown tracker is a no-op (reset-race guard, no write).

    A concurrent reset_ledger can delete the tracker between a coordinator's
    load and its prune tick; prune must tolerate the missing key silently.
    """
    store = _store(hass)
    await store.load()
    with patch.object(store._store, "async_save") as saver:
        await store.prune_days("nope", "2026-08-29")
    saver.assert_not_called()


async def test_reset_single_entry(hass: HomeAssistant) -> None:
    """reset(entry_id) drops just that tracker."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.get_or_create_tracker("other", "light.y", "all_states", None, None)
    await store.reset(ENTRY_ID)
    trackers = (await store.load()).trackers
    assert ENTRY_ID not in trackers
    assert "other" in trackers


async def test_reset_single_entry_absent_early_return(hass: HomeAssistant) -> None:
    """reset() on an unknown tracker is a no-op (no write)."""
    store = _store(hass)
    await store.load()
    with patch.object(store._store, "async_save") as saver:
        await store.reset("nope")
    saver.assert_not_called()


async def test_reset_all(hass: HomeAssistant) -> None:
    """reset(None) clears every tracker."""
    store = _store(hass)
    await store.get_or_create_tracker(ENTRY_ID, "light.x", "all_states", None, None)
    await store.get_or_create_tracker("other", "light.y", "all_states", None, None)
    await store.reset(None)
    assert (await store.load()).trackers == {}


async def test_reset_all_empty_early_return(hass: HomeAssistant) -> None:
    """reset(None) with no trackers is a no-op (no write)."""
    store = _store(hass)
    await store.load()
    with patch.object(store._store, "async_save") as saver:
        await store.reset(None)
    saver.assert_not_called()
