"""Persistent storage helper for Entity State Tracker.

The store keeps a single in-memory copy of :class:`StoredData` and only
hits disk on cold start (first read after setup) and on writes. Every
ledger mutator routes its read through the cached accessor so the
coordinator's per-tick fold path no longer issues redundant
``Store.async_load()`` calls (mirrors the WashWise store contract).

This module is pure persistence: it holds the daily-bucket ledger and
exposes mutators the coordinator calls. It does no frame math, no
recorder access, and no datetime-boundary logic — local-day ISO strings
arrive already computed by the engine (§6.2, §8).
"""

from __future__ import annotations

import asyncio
import logging
from json import JSONDecodeError
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY_FMT, STORAGE_VERSION
from .models import StoredData, TrackerLedger

_LOGGER = logging.getLogger(__name__)


class _MigratingStore(Store[dict[str, Any]]):
    """``Store`` subclass with a migration hook keyed on ``STORAGE_VERSION``.

    v1 is the only shipped version, so migration is a no-op: the stored document
    is returned unchanged. The override exists so a future v2 has a single place
    to transform old documents (e.g. rename/reshape a ledger field) instead of
    HA's default ``_async_migrate_func`` raising ``NotImplementedError`` the
    first time ``STORAGE_VERSION`` is bumped. Keep the branch structure — add an
    ``elif old_major_version < N`` block per future bump.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate a stored document to ``STORAGE_VERSION`` (no-op for v1)."""
        # v1: nothing to migrate; return the document as-is. A v2 bump adds an
        # `if old_major_version < 2: ... ` transform above this return.
        return old_data


class EntityStateTrackerStore:
    """Cached, single-flight persistence for one config entry's ledger."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Store = _MigratingStore(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_FMT.format(entry_id=entry_id),
        )
        self._data: StoredData | None = None
        # Single-flight guard so concurrent first-readers don't both
        # trigger ``Store.async_load()`` while the cache is empty.
        self._load_lock: asyncio.Lock = asyncio.Lock()
        # Diagnostics: count actual disk reads (not cache hits).
        self._disk_read_count: int = 0

    @property
    def disk_read_count(self) -> int:
        """Return the number of ``Store.async_load()`` calls made so far."""
        return self._disk_read_count

    async def load(self) -> StoredData:
        """Return cached :class:`StoredData`, hitting disk only when cold."""
        if self._data is not None:
            return self._data
        return await self._load_from_disk("cold cache")

    async def _load_from_disk(self, reason: str) -> StoredData:
        """Read from disk under the single-flight lock and cache the result."""
        async with self._load_lock:
            # Re-check inside the lock: another waiter may have populated
            # the cache while we were queued.
            if self._data is not None:
                return self._data

            self._disk_read_count += 1
            _LOGGER.debug(
                "Entity State Tracker storage read for %s: %s (total reads=%d)",
                self._entry_id,
                reason,
                self._disk_read_count,
            )
            try:
                raw = await self._store.async_load()
            except (JSONDecodeError, OSError) as err:
                _LOGGER.warning(
                    "Entity State Tracker storage for %s is corrupt (%s); "
                    "resetting to empty.",
                    self._entry_id,
                    err,
                )
                self._data = StoredData.empty()
                return self._data

            if raw is None:
                self._data = StoredData.empty()
                return self._data

            try:
                self._data = StoredData.from_dict(raw)
                return self._data
            except (TypeError, ValueError, KeyError) as err:
                _LOGGER.warning(
                    "Entity State Tracker storage for %s failed to deserialize "
                    "(%s); resetting to empty.",
                    self._entry_id,
                    err,
                )
                self._data = StoredData.empty()
                return self._data

    async def save(self, data: StoredData) -> None:
        # Update the in-memory cache FIRST so subsequent reads see the new
        # state immediately, even if the disk write is still in flight.
        # If the disk write fails, revert the cache so a follow-up read
        # doesn't observe state that was never persisted.
        #
        # NOTE on the save→load race window: ``self._data`` flips to the new
        # value before ``async_save`` awaits, so a concurrent ``load()`` mid-
        # flight observes the new state; if the disk write then fails we roll
        # back, leaving any pre-rollback reader holding a value that was never
        # persisted. In practice HA runs on a single-threaded asyncio event
        # loop and the coordinator serializes its own ticks, so no caller
        # interleaves with an in-flight ``save()`` from this integration.
        previous = self._data
        self._data = data
        try:
            await self._store.async_save(data.to_dict())
        except (OSError, HomeAssistantError):
            self._data = previous
            raise

    async def remove(self) -> None:
        # Hold the load lock so an in-flight ``_load_from_disk`` cannot
        # re-populate the cache after the file is deleted.
        async with self._load_lock:
            await self._store.async_remove()
            self._data = None

    async def get_or_create_tracker(
        self,
        entry_id: str,
        entity_id: str,
        mode: str,
        states: list[str] | None,
        target: list[str] | None,
    ) -> TrackerLedger:
        """Return the ledger for ``entry_id``, creating an empty one if absent."""
        data = await self.load()
        ledger = data.trackers.get(entry_id)
        if ledger is None:
            ledger = TrackerLedger(
                entity_id=entity_id,
                mode=mode,
                states=list(states) if states is not None else None,
                target=list(target) if target is not None else None,
            )
            data.trackers[entry_id] = ledger
            await self.save(data)
        return ledger

    async def fold_into_day(
        self,
        entry_id: str,
        local_day_iso: str,
        state: str,
        secs: float,
        count_delta: int,
    ) -> None:
        """Add ``secs`` and ``count_delta`` into ``daily[day][state]``."""
        data = await self.load()
        ledger = data.trackers[entry_id]
        day_bucket = ledger.daily.setdefault(local_day_iso, {})
        row = day_bucket.setdefault(state, {"secs": 0.0, "count": 0})
        row["secs"] = float(row["secs"]) + secs
        row["count"] = int(row["count"]) + count_delta
        await self.save(data)

    async def replace_day(
        self,
        entry_id: str,
        local_day_iso: str,
        day_map: dict[str, dict[str, Any]],
    ) -> None:
        """Replace ``daily[day]`` wholesale (backfill — §8 R8 replace-not-add)."""
        data = await self.load()
        ledger = data.trackers[entry_id]
        ledger.daily[local_day_iso] = day_map
        await self.save(data)

    async def set_meta(
        self,
        entry_id: str,
        last_state: str | None = None,
        last_changed_ts: str | None = None,
        last_updated_day: str | None = None,
        built_min_state_duration: float | None = None,
    ) -> None:
        """Partially update the ledger's live-transition metadata."""
        data = await self.load()
        ledger = data.trackers.get(entry_id)
        if ledger is None:  # no tracker registered yet — nothing to update
            return
        if last_state is not None:
            ledger.last_state = last_state
        if last_changed_ts is not None:
            ledger.last_changed_ts = last_changed_ts
        if last_updated_day is not None:
            ledger.last_updated_day = last_updated_day
        if built_min_state_duration is not None:
            ledger.built_min_state_duration = built_min_state_duration
        await self.save(data)

    async def prune_days(self, entry_id: str, before_iso: str) -> None:
        """Drop daily buckets with a day key strictly before ``before_iso``."""
        data = await self.load()
        ledger = data.trackers.get(entry_id)
        if ledger is None:  # no tracker registered yet — nothing to prune
            return
        stale = [day for day in ledger.daily if day < before_iso]
        if not stale:
            return
        for day in stale:
            del ledger.daily[day]
        await self.save(data)
