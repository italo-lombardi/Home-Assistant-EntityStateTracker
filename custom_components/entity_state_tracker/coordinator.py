"""DataUpdateCoordinator for Entity State Tracker.

The convergence point: it wires the pure :mod:`engine` frame math to the
persisted :mod:`storage` ledger and the live state-change stream, and emits a
:class:`~.models.TrackerData` every tick that the sensors read.

Data flow (§6.6, §8):

* **Setup** — load the ledger, ``get_or_create_tracker`` (once, before any
  fold), backfill closed days since ``last_updated_day`` from the recorder
  (replace-not-add, advancing ``last_updated_day`` only after a successful
  flush — R8), then the first frame computation.
* **Live** — one ``async_track_state_change_event`` subscription, registered
  *inside* an ``async_at_start`` callback so subscribe→backfill ordering holds.
  Each transition folds the *previous* state's elapsed time into the correct
  local-day bucket(s) (split at midnight), bumps ``count`` once on the start
  day, and updates the live transition metadata; a debounced refresh coalesces
  the resulting recompute.
* **Poll** — the 5-minute base-class timer advances open blocks and flushes the
  in-memory ledger to disk (in-memory is truth; disk writes are debounced —
  §8). A final flush fires on ``EVENT_HOMEASSISTANT_STOP`` and on shutdown.

Recorder-off (§15): :func:`engine.query_recorder` returns ``None`` when the
recorder is disabled; we then compute live-only (ledger + in-memory today) and
raise a Repair issue once.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_start
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITY,
    CONF_FRAMES,
    CONF_MIN_STATE_DURATION,
    CONF_MODE,
    CONF_STATES,
    CONF_TARGET,
    CONF_TARGET_THRESHOLD,
    DEFAULT_MIN_STATE_DURATION,
    DOMAIN,
    EVENT_NEW_STATE,
    FRAMES,
    MODE_ALL,
    SCAN_INTERVAL,
)
from .engine import (
    OVERFLOW_TOLERANCE_SECS,
    accumulate_blocks,
    carry_forward_states,
    compute_frame,
    prune_cutoff_iso,
    query_recorder,
    resolve_frame_bounds,
    split_visit_across_days,
)
from .models import FrameResult, TrackerData, TrackerLedger
from .storage import EntityStateTrackerStore

_LOGGER = logging.getLogger(__name__)

# Coalesce a burst of same-entity transitions before recomputing. A high-churn
# entity can fire several state_changed events within a poll; 0.5s batches them
# into one refresh without perceptible latency (matches the sibling repos).
_REFRESH_DEBOUNCE = 0.5  # seconds

# hass.data flag so the recorder-off Repair issue is raised at most once per
# process, not once per tick per tracker.
_RECORDER_OFF_ISSUE = "recorder_off"

# Rolling frames whose window_start lands MID-DAY. Their recent portion is
# computed from the recorder (real intra-day timeline) clamped at recorder
# retention, and the ledger only fills WHOLE days strictly below that seam — so
# the oldest partial day is never counted as a whole 86400 bucket (the
# over-count this seam closes). Calendar frames start on a local midnight and
# need no such treatment (their windows are whole local days already).
_ROLLING_FRAMES = ("24h", "7d")

# Upper bound on the durable per-session ``_seen`` set. A pathological entity
# that emits a unique state per transition (e.g. a timestamp as its state) would
# otherwise grow ``_seen`` without bound AND fire a persistent notification per
# distinct state. Past the cap we stop tracking/announcing new states and log
# once — new-state announce is a UX nicety, not a correctness feature (§5.2).
_SEEN_CAP = 10_000


class EntityStateTrackerCoordinator(DataUpdateCoordinator[TrackerData]):
    """Coordinator that folds live transitions into a ledger and computes frames."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator from the config entry."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Entity State Tracker - {entry.title}",
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry
        config = {**entry.data, **entry.options}

        self.entity_id: str = config[CONF_ENTITY]
        self.mode: str = config[CONF_MODE]
        states = config.get(CONF_STATES)
        self.tracked_states: list[str] | None = list(states) if states else None
        target = config.get(CONF_TARGET)
        self.target_states: list[str] | None = list(target) if target else None
        self.target_threshold: float | None = config.get(CONF_TARGET_THRESHOLD)
        self.min_state_duration: float = config.get(
            CONF_MIN_STATE_DURATION, DEFAULT_MIN_STATE_DURATION
        )
        # Enabled frames = canonical order filtered by the per-frame flags.
        frame_flags: dict[str, bool] = config.get(CONF_FRAMES, {})
        self.enabled_frames: list[str] = [k for k in FRAMES if frame_flags.get(k)]

        self.tz: dt.tzinfo = (
            dt_util.get_time_zone(hass.config.time_zone) or dt_util.DEFAULT_TIME_ZONE
        )

        self.store = EntityStateTrackerStore(hass, entry.entry_id)
        # Held reference to the cached ledger (same object the store mutates), so
        # live folds mutate in-memory and disk writes stay debounced (§8).
        self._ledger: TrackerLedger | None = None
        self._dirty = False
        # Previous state name (the one held before the current live state), used
        # only for the TrackerData.previous_state attribute. Seeded on the first
        # transition after start; None until then.
        self._previous_state: str | None = None
        # Durable per-session set of every state name ever announced/observed.
        # Seeded from the ledger at first refresh (and reset) so a state that
        # later ages out of every retained daily bucket is NOT re-announced when
        # it recurs — prune erodes ``ledger.daily`` but must not erode "seen".
        # (Per-session only: not persisted across restart — new-state announce
        # is a UX nicety, so a restart may re-announce a long-dormant state.)
        self._seen: set[str] = set()
        # One-time guard: True once _seen hit _SEEN_CAP, so the "cap reached"
        # warning logs once, not per subsequent new state (S4).
        self._seen_cap_hit = False
        # One-time guard: True once the recorder-retention warning has logged,
        # so the "keep_days unavailable" diagnostic fires once per coordinator,
        # not once per tick when the recorder is off (rolling-frame seam).
        self._retention_warned = False
        # Frame labels whose Σbreakdown > window invariant has already warned FOR
        # THIS tracker, so the diagnostic logs once per (entry, frame) rather than
        # once per frame label process-wide — a real overflow in one tracker is no
        # longer suppressed by an unrelated tracker that warned the same label
        # first (L4). Per-coordinator, so each instance warns independently.
        self._warned_overflow: set[str] = set()
        # (state, start_day_iso) of the most recent SURVIVING live fold — the
        # "preceding block" a sub-``min_state_duration`` glitch merges into, so
        # the live fold matches ``accumulate_blocks``' glitch rule (§6.5). None
        # until the first surviving fold; unused when min_state_duration == 0.
        self._last_fold: tuple[str, str] | None = None
        self._unsub_state: CALLBACK_TYPE | None = None
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=_REFRESH_DEBOUNCE,
            immediate=False,
            function=self.async_refresh,
        )

    @property
    def _entry_id(self) -> str:
        return self.entry.entry_id

    async def async_config_entry_first_refresh(self) -> None:
        """Load the ledger, backfill closed days, then do the first refresh."""
        data = await self.store.load()
        self._ledger = await self.store.get_or_create_tracker(
            self._entry_id,
            self.entity_id,
            self.mode,
            self.tracked_states,
            self.target_states,
        )
        # Re-read the held reference from the freshly loaded document (a cold
        # load inside get_or_create replaces the cache object).
        self._ledger = data.trackers.get(self._entry_id, self._ledger)
        # Seed the durable seen-set from whatever history the ledger carries now,
        # BEFORE any prune, so states already recorded never re-announce (§5.2).
        self._seen = self._ledger_seen_states(self._ledger)

        self._reconcile_min_state_duration(self._ledger)
        await self._async_backfill()
        await super().async_config_entry_first_refresh()

        # Subscribe INSIDE async_at_start so the live stream is wired only after
        # startup — backfill above owns everything up to now; startup transitions
        # then flow through the push path (§6.6). Register both cancellers.
        self.entry.async_on_unload(
            async_at_start(self.hass, self._async_subscribe_at_start)
        )
        self.entry.async_on_unload(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_flush_on_stop
            )
        )

    @callback
    def _async_subscribe_at_start(self, _hass: HomeAssistant) -> None:
        """Wire the live state-change subscription (runs at HA start).

        The canceller is owned solely by :meth:`async_shutdown` (itself an
        ``entry.async_on_unload`` callback). Registering it a second time via
        ``async_on_unload`` would double-invoke it on unload — HA pops the
        listener first, then ``async_shutdown`` calls the (already-removed)
        canceller again, raising ``ValueError: list.remove(x): x not in list``
        and aborting the unload, which strands the reloaded entities.
        """
        self._seed_open_visit()
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.entity_id], self._handle_state_change
        )

    @callback
    def _seed_open_visit(self) -> None:
        """Anchor the open visit from the live state when the ledger is fresh.

        On a fresh install the ledger carries no ``last_state`` — backfill only
        advances ``last_updated_day`` (§8), never seeds the open block. Without
        an anchor the first transition reads ``last_state is None`` and DROPS the
        time the entity held its state from start until that transition (H1).

        Seed ``last_state`` from HA's current state and anchor
        ``last_changed_ts`` at *now*: today's recent slice is reconstructed from
        the recorder, so anchoring at now (not the state's older ``last_changed``)
        avoids double-counting recorder-covered time while still giving the first
        live fold a real start. Only seeds when unset — never clobbers a restored
        ledger.
        """
        ledger = self._ledger
        if ledger is None or ledger.last_state is not None:
            return
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return
        ledger.last_state = state.state
        ledger.last_changed_ts = dt_util.utcnow().isoformat()
        self._dirty = True

    async def async_shutdown(self) -> None:
        """Flush the ledger and cancel subscriptions on unload."""
        await super().async_shutdown()
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self._debouncer.async_shutdown()
        await self._async_flush()

    async def _async_flush_on_stop(self, _event: Event) -> None:
        """Flush the ledger to disk on Home Assistant stop (§8)."""
        await self._async_flush()

    async def _async_flush(self) -> None:
        """Persist the in-memory ledger if it has unsaved mutations."""
        if not self._dirty:
            return
        data = await self.store.load()
        # Clear the dirty flag BEFORE awaiting the save: store.save snapshots
        # data synchronously, so a fold that runs during the save's await
        # mutates the ledger in place and must re-dirty for the next flush.
        # Clearing after the await would wipe that re-dirty and silently drop
        # the mutation (high-churn entity during a flush). F2.
        self._dirty = False
        await self.store.save(data)

    # --- Live path --------------------------------------------------------

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Fold the previous state's elapsed time on each transition (§6.6)."""
        ledger = self._ledger
        if ledger is None:  # pragma: no cover - subscription starts after load
            return
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        now = new_state.last_changed
        if now.tzinfo is None:  # pragma: no cover - HA always sets tz
            now = now.replace(tzinfo=dt.UTC)

        # Fold the interval [last_changed_ts, now) spent in last_state into the
        # ledger, split at local midnights; count once on the start day (§6.2).
        prev_state = ledger.last_state
        prev_ts = _parse_ts(ledger.last_changed_ts)
        now_iso = now.isoformat()
        if prev_state is not None and prev_ts is not None:
            self._fold_visit(ledger, prev_state, prev_ts, now)
            # The state we just left is now the "previous" state.
            self._previous_state = prev_state

        new_name = new_state.state
        first_seen = new_name not in self._seen
        # Cap _seen so a unique-state-per-transition entity can't grow it (and
        # spam notifications) without bound — past the cap, stop tracking and
        # announcing new states; warn once (S4).
        if first_seen and len(self._seen) >= _SEEN_CAP:
            if not self._seen_cap_hit:
                self._seen_cap_hit = True
                _LOGGER.warning(
                    "Entity State Tracker: %s exceeded %d distinct states; "
                    "stopping new-state tracking to bound memory",
                    self.entity_id,
                    _SEEN_CAP,
                )
            first_seen = False
        else:
            self._seen.add(new_name)
        # Stamp entry of B and exit of A (§7) — but only for states the cap still
        # tracks (present in _seen). Stamping unconditionally would let the same
        # unique-state-per-transition entity _SEEN_CAP guards against grow these
        # persisted dicts without bound, defeating the cap. A state past the cap
        # is untracked here too; the very first observed state has no prior A, so
        # only its entry is recorded.
        # ponytail: no recorder-backfill for last_entered/exited — these are
        # best-effort from the first post-start transition, same class as the
        # §8 carry-forward heuristic. Days before the first live transition
        # carry no per-state entry/exit stamp.
        if new_name in self._seen:
            ledger.last_entered[new_name] = now_iso
        if prev_state is not None and prev_ts is not None and prev_state in self._seen:
            ledger.last_exited[prev_state] = now_iso
        ledger.last_state = new_name
        ledger.last_changed_ts = now_iso
        self._dirty = True

        if self.mode == MODE_ALL and first_seen:
            self._announce_new_state(new_name)

        # Coalesce the recompute; async_schedule_call is the callback-safe
        # (sync) Debouncer entry point — no untracked task per transition.
        self._debouncer.async_schedule_call()

    def _fold_visit(
        self,
        ledger: TrackerLedger,
        state: str,
        start: dt.datetime,
        end: dt.datetime,
    ) -> None:
        """Fold one visit's elapsed seconds into the daily buckets in-memory.

        Applies the same glitch rule as :func:`engine.accumulate_blocks` so a
        closed day written live matches one a recorder backfill would produce
        (§6.5, guards R6). A visit shorter than ``min_state_duration`` is a
        glitch: its whole duration merges into the PRECEDING surviving visit's
        state (no new ``count``); a leading glitch with no predecessor is
        dropped outright as unattributable. When ``min_state_duration == 0``
        every visit survives — the common case, unchanged from before.
        """
        segments = split_visit_across_days(start, end, self.tz)
        if not segments:
            return
        total_secs = sum(secs for _, secs in segments)

        if self.min_state_duration > 0 and total_secs < self.min_state_duration:
            # Glitch: re-attribute its time to the preceding surviving visit's
            # state, in that visit's start-day bucket; never opens a new count.
            if self._last_fold is not None:
                prev_state, prev_day = self._last_fold
                row = ledger.daily.setdefault(prev_day, {}).setdefault(
                    prev_state, {"secs": 0.0, "count": 0}
                )
                row["secs"] = float(row["secs"]) + total_secs
            # else: leading glitch, unattributable — drop.
            return

        start_day = segments[0][0]
        # A surviving visit whose state matches the preceding surviving visit
        # (they can only be adjacent because a glitch was absorbed between them)
        # coalesces into ONE visit — extend secs, no second count (matches the
        # engine's post-glitch same-state merge).
        coalesce = self._last_fold is not None and self._last_fold[0] == state
        for i, (day_iso, secs) in enumerate(segments):
            day_bucket = ledger.daily.setdefault(day_iso, {})
            row = day_bucket.setdefault(state, {"secs": 0.0, "count": 0})
            row["secs"] = float(row["secs"]) + secs
            # Count the visit once, on the day it began (§6.2) — unless this
            # visit coalesces into the preceding same-state visit.
            if i == 0 and not coalesce:
                row["count"] = int(row["count"]) + 1
        # The predecessor for the next glitch is this visit's start-day bucket
        # (or the coalesced original, whose day we keep).
        self._last_fold = self._last_fold if coalesce else (state, start_day)

    def _ledger_seen_states(self, ledger: TrackerLedger) -> set[str]:
        """Return every state name the ledger currently records (pre-prune seed).

        Used once at first-refresh/reset to seed the durable :attr:`_seen` set.
        After that, :attr:`_seen` is the authority — it survives prune, whereas
        re-deriving from ``ledger.daily`` here would shrink as buckets age out.
        """
        seen: set[str] = set()
        for day_bucket in ledger.daily.values():
            seen.update(day_bucket)
        if ledger.last_state is not None:
            seen.add(ledger.last_state)
        return seen

    @callback
    def _announce_new_state(self, state: str) -> None:
        """Log + fire the event for a new state (§5.2).

        Fires the ``entity_state_tracker_new_state`` event so automations can
        react; deliberately does NOT raise a persistent notification (user
        decision — event-only, no HA notification clutter). Build a notification
        in an automation off the event if you want one.
        """
        _LOGGER.info("Entity State Tracker: %s saw new state %r", self.entity_id, state)
        self.hass.bus.async_fire(
            EVENT_NEW_STATE,
            {
                "entry_id": self._entry_id,
                "entity_id": self.entity_id,
                "state": state,
            },
        )

    # --- Backfill ---------------------------------------------------------

    @callback
    def _reconcile_min_state_duration(self, ledger: TrackerLedger) -> None:
        """Re-backfill closed days when the glitch threshold changed (H1).

        Closed-day daily buckets are baked with the ``min_state_duration`` in
        force when they were folded/backfilled. An OptionsFlow edit that changes
        ``min_state_duration`` reloads the entry, but the change would otherwise
        apply ONLY to future folds and the open-day recompute — leaving the
        closed days (which every 7d/30d/month/year frame sums) built with the OLD
        threshold, silently mixing old- and new-threshold buckets.

        Fix: persist the threshold each ledger was built with
        (``built_min_state_duration``) and, on setup/reload, compare it to the
        configured value. When they differ, the closed-day buckets are stale, so
        clear ``ledger.daily`` and reset ``last_updated_day`` to ``None`` — the
        subsequent :meth:`_async_backfill` then rebuilds every closed day within
        recorder retention using the NEW threshold. Days older than recorder
        retention cannot be rebuilt (the ledger stores whole-day sums, not the
        intra-day timeline) — that granularity limit is documented and accepted;
        those far-back frames simply refill over time as new days close.

        A fresh or legacy (pre-field) ledger carries ``built is None``: we seed it
        to the current value WITHOUT wiping, so the field-introducing upgrade
        never discards a user's existing history — only a genuine subsequent
        change triggers a rebuild.
        """
        built = ledger.built_min_state_duration
        if built is not None and built != self.min_state_duration:
            _LOGGER.info(
                "Entity State Tracker: %s min_state_duration changed %s→%s; "
                "clearing closed-day buckets to re-backfill from the recorder "
                "with the new glitch threshold (days older than recorder "
                "retention rebuild from whole-day sums as they age in)",
                self.entity_id,
                built,
                self.min_state_duration,
            )
            ledger.daily.clear()
            ledger.last_updated_day = None
            # The open-visit anchor was measured under the old threshold's fold
            # semantics; drop the last_fold predecessor so the first post-reset
            # fold starts clean (no stale glitch-merge target).
            self._last_fold = None
        ledger.built_min_state_duration = self.min_state_duration
        self._dirty = True

    async def _async_backfill(self) -> None:
        """Recompute closed days since ``last_updated_day`` from the recorder (§8).

        Each recomputed closed day is written wholesale (replace-not-add, R8);
        ``last_updated_day`` advances only after the flush succeeds, so a crash
        mid-backfill re-runs the same days rather than double-counting them.
        """
        ledger = self._ledger
        if ledger is None:  # pragma: no cover
            return
        now = dt_util.utcnow()
        today_local = now.astimezone(self.tz).date()
        # Backfill from the day after the last fully-recorded day, else from the
        # oldest queryable point (prune cutoff) so a first run seeds recent days.
        last_day = _parse_day(ledger.last_updated_day)
        start_day = (
            last_day + dt.timedelta(days=1)
            if last_day is not None
            else dt.date.fromisoformat(
                prune_cutoff_iso(self.enabled_frames, now, self.tz)
            )
        )

        day = start_day
        while day < today_local:
            day_start = dt.datetime.combine(day, dt.time(), tzinfo=self.tz)
            day_end = day_start + dt.timedelta(days=1)
            states = await query_recorder(
                self.hass,
                self.entity_id,
                day_start.astimezone(dt.UTC),
                day_end.astimezone(dt.UTC),
            )
            if states is None:
                # Recorder off: no backfill possible (§15). Stop; live-only path
                # will take over in _async_update_data.
                return
            blocks = accumulate_blocks(
                carry_forward_states(states),
                day_start.astimezone(dt.UTC),
                day_end.astimezone(dt.UTC),
                self.min_state_duration,
                now,
            )
            day_iso = day.isoformat()
            # A day with no recorded activity yields no buckets — skip the
            # wholesale write so backfill never pollutes the ledger with empty
            # day dicts (they'd distort prune/coverage and defeat the no-op
            # fold invariant). The marker still advances so the day counts as
            # backfilled.
            if blocks:
                await self.store.replace_day(self._entry_id, day_iso, blocks)
            # Flush succeeded (replace_day saves) → safe to advance the marker.
            await self.store.set_meta(self._entry_id, last_updated_day=day_iso)
            day += dt.timedelta(days=1)

    # --- Frame computation ------------------------------------------------

    async def _async_update_data(self) -> TrackerData:
        """Compute every enabled frame from ledger + recorder (§6.4)."""
        ledger = self._ledger
        if ledger is None:  # pragma: no cover - first_refresh sets it
            ledger = await self.store.get_or_create_tracker(
                self._entry_id,
                self.entity_id,
                self.mode,
                self.tracked_states,
                self.target_states,
            )
            self._ledger = ledger

        now = dt_util.utcnow()
        today_midnight_utc = dt.datetime.combine(
            now.astimezone(self.tz).date(), dt.time(), tzinfo=self.tz
        ).astimezone(dt.UTC)
        data_start_iso = min(ledger.daily) if ledger.daily else None

        # ONE recorder query per tick (P2). Every open frame's recent portion is
        # a sub-window of the widest span we need: the today-slice
        # [today_midnight, now) for calendar frames, and [recorder_floor, now)
        # for each rolling frame (24h/7d), where recorder_floor is the later of
        # the window start and recorder retention. Fetch the earliest of those
        # starts once; accumulate_blocks (pure, clamps to any start) then derives
        # each sub-window from the same states list. Rolling frames start MID-DAY
        # so their recent portion MUST come from the recorder's real intra-day
        # timeline, not a whole-day ledger bucket — that, plus the ledger seam at
        # recorder_floor's local day, removes the oldest-partial-day over-count.
        rolling = [f for f in self.enabled_frames if f in _ROLLING_FRAMES]
        floors = self._rolling_floors(rolling, now)
        query_start = min([today_midnight_utc, *floors.values()])
        states = await self._window_states(ledger, query_start, now)

        def _blocks(start_utc: dt.datetime) -> dict[str, dict[str, float]]:
            """Accumulate [start_utc, now) from the shared states list."""
            if states is None:  # recorder off — live-meta fallback (§15)
                return self._live_today_blocks(ledger, start_utc, now, now)
            return accumulate_blocks(
                states, start_utc, now, self.min_state_duration, now
            )

        today_blocks = _blocks(today_midnight_utc)

        frames: dict[str, Any] = {}
        prior = self.data.frames if self.data is not None else {}
        for frame_key in self.enabled_frames:
            _start_utc, end_utc = resolve_frame_bounds(frame_key, now, self.tz)
            ledger_upper_local_day: str | None = None
            if frame_key in _ROLLING_FRAMES and states is not None:
                # Recorder covers [recorder_floor, now) at real seconds; the
                # ledger fills only WHOLE days strictly below recorder_floor's
                # local day (passed as ledger_upper_local_day) — seam, no
                # over-count, no double-count.
                floor = floors[frame_key]
                recent_blocks = _blocks(floor)
                ledger_upper_local_day = floor.astimezone(self.tz).date().isoformat()
            else:
                # Calendar frames, and rolling frames when the recorder is off:
                # reuse the shared today-slice for windows that reach today (the
                # recorder-off rolling fallback degrades to ledger whole-days +
                # the live open block, the pre-existing safe path, §15); a closed
                # frame's recent window is empty.
                recent_blocks = today_blocks if today_midnight_utc < end_utc else {}

            prior_dominant = prior[frame_key].dominant if frame_key in prior else None
            result = compute_frame(
                frame_key,
                now,
                self.tz,
                recent_blocks,
                ledger.daily,
                data_start_iso,
                mode=self.mode,
                tracked_states=self.tracked_states,
                target_states=self.target_states,
                prior_dominant=prior_dominant,
                ledger_upper_local_day=ledger_upper_local_day,
            )
            self._warn_overflow(frame_key, result)
            frames[frame_key] = result

        # Prune stale buckets IN-MEMORY and let the single flush persist (one
        # disk write, not prune's own save + flush — P4). Disk is best-effort:
        # the in-memory ledger is truth (§8), so a failed write must NOT discard
        # the frames we just computed (S9) — log and carry on.
        try:
            self._prune_ledger(
                ledger, prune_cutoff_iso(self.enabled_frames, now, self.tz)
            )
            await self._async_flush()
        except (OSError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Entity State Tracker: ledger persist failed for %s (kept in memory): %s",
                self.entity_id,
                err,
            )

        return TrackerData(
            frames=frames,
            last_state=ledger.last_state,
            previous_state=self._previous_state,
            # Flat {state: iso_ts} snapshots from the ledger — tracker-global,
            # so every frame's sensor exposes the same dicts (§7). Copied so a
            # later live fold can't mutate the emitted TrackerData in place.
            last_entered=dict(ledger.last_entered),
            last_exited=dict(ledger.last_exited),
        )

    async def _window_states(
        self,
        ledger: TrackerLedger,
        start_utc: dt.datetime,
        now: dt.datetime,
    ) -> list[State] | None:
        """Recorder states for ``[start_utc, now)``, carry-forwarded + overlaid.

        Returns the normalised states list the tick's frames all accumulate from
        (one query per tick, P2), or ``None`` when the recorder is off (§15) so
        the caller falls back to the live-meta block. ``_overlay_open_visit``
        clamps its cutoff at ``max(open_ts, start_utc)`` so it is correct for ANY
        start_utc — a mid-day rolling recorder_floor as well as today-midnight.
        """
        states = await query_recorder(self.hass, self.entity_id, start_utc, now)
        if states is None:
            self._raise_recorder_off_issue()
            return None
        self._clear_recorder_off_issue()
        states = carry_forward_states(states)
        return self._overlay_open_visit(states, ledger, start_utc, now)

    def _rolling_floors(
        self,
        rolling: list[str],
        now: dt.datetime,
    ) -> dict[str, dt.datetime]:
        """Recorder-floor per rolling frame: ``max(window_start, retention)``.

        The recorder purges rows older than ``now − keep_days`` (recorder core:
        ``purge_before = utcnow() - timedelta(days=self.keep_days)``), so the
        recorder can only be trusted from ``retention_start`` forward. The floor
        is therefore the later of the frame's window start and retention_start:

        * Common case (retention ≥ window, HA default keep_days=10 ≥ 7d):
          floor = window_start → the recorder covers the ENTIRE rolling window
          at real seconds (partial oldest day included exactly), ledger adds
          nothing.
        * Edge case (keep_days < window): floor = retention_start → the recorder
          covers ``[retention_start, now)``; the purged head falls to the ledger
          as WHOLE local days below retention_start's day. Granularity loss is
          bounded to the single oldest partial day and unavoidable (daily-sum
          buckets carry no intra-day timeline).

        Detection is defensive: recorder off / no ``keep_days`` → retention_start
        = now (recorder covers nothing → ledger-only whole days, the pre-existing
        safe path), logged once.
        """
        if not rolling:
            return {}
        retention_start = self._recorder_retention_start(now)
        floors: dict[str, dt.datetime] = {}
        for frame_key in rolling:
            start_utc, _ = resolve_frame_bounds(frame_key, now, self.tz)
            floors[frame_key] = max(start_utc, retention_start)
        return floors

    def _recorder_retention_start(self, now: dt.datetime) -> dt.datetime:
        """Earliest instant the recorder still retains, from its ``keep_days``.

        Defensive: if the recorder is off or exposes no ``keep_days``, return
        ``now`` (recorder covers nothing → recorder_floor = now → the rolling
        frame falls back to ledger-only whole days) and log once. Verified
        against HA core: ``get_instance(hass)`` returns the ``Recorder`` whose
        ``keep_days`` attribute drives ``purge_before``.
        """
        from homeassistant.components.recorder import get_instance

        try:
            instance = get_instance(self.hass)
        except KeyError:
            # Recorder not set up: get_instance raises rather than returning
            # None (its lru_cache reads hass.data[DATA_INSTANCE] directly).
            instance = None
        keep_days = getattr(instance, "keep_days", None) if instance else None
        if not keep_days:
            if not self._retention_warned:
                self._retention_warned = True
                _LOGGER.warning(
                    "Entity State Tracker: recorder retention (keep_days) "
                    "unavailable for %s; rolling frames fall back to whole-day "
                    "ledger granularity",
                    self.entity_id,
                )
            return now
        return now - dt.timedelta(days=keep_days)

    @callback
    def _overlay_open_visit(
        self,
        states: list[State],
        ledger: TrackerLedger,
        start_utc: dt.datetime,
        now: dt.datetime,
    ) -> list[State]:
        """Ensure the states list ends with the ledger's open visit.

        The recorder commits on an interval (~5s), so the just-entered state's row
        may not be queryable yet; accumulate_blocks would then attribute the open
        tail to the PRIOR committed state and omit the current one. The ledger holds
        the open visit synchronously — inject it so accumulate_blocks sees reality.
        Idempotent: once the recorder has committed the same row, the trailing state
        already == ledger.last_state and we no-op, so the visit is counted exactly
        once (never double).
        """
        open_state = ledger.last_state
        open_ts = _parse_ts(ledger.last_changed_ts)
        if open_state is None or open_ts is None:
            return states
        if open_ts >= now:
            return states
        # Recorder already fresh for this visit? trailing row is the open state and
        # began at/after the visit start -> nothing to inject.
        if (
            states
            and states[-1].state == open_state
            and states[-1].last_changed >= open_ts
        ):
            return states
        # Drop the stale trailing rows the recorder wrongly extended past the visit
        # start (within/after the window), then append the true open row at its REAL
        # start (unclamped) so accumulate_blocks' leading-continuation rule handles a
        # straddle-midnight visit as count=0 (already counted on its start day).
        cutoff = max(open_ts, start_utc)
        trimmed = [s for s in states if s.last_changed < cutoff]
        return [*trimmed, State(self.entity_id, open_state, last_changed=open_ts)]

    @callback
    def _warn_overflow(self, frame_key: str, result: FrameResult) -> None:
        """Warn once per (entry, frame) when Σbreakdown exceeds the window (L4).

        The engine clamps the overflow so ``unaccounted_seconds`` never goes
        negative, but a >``OVERFLOW_TOLERANCE_SECS`` overshoot is a diagnostic
        signal (a bucket seam over-counted). We log it here — the coordinator
        owns ``entry_id`` — so each tracker warns independently; a real overflow
        in tracker B is no longer suppressed because tracker A warned the same
        frame label first. Diagnostic only: logged once per frame label per
        coordinator, never raised.
        """
        if frame_key in self._warned_overflow:
            return
        breakdown_total = sum(result.breakdown_seconds.values())
        if breakdown_total <= result.window_seconds + OVERFLOW_TOLERANCE_SECS:
            return
        self._warned_overflow.add(frame_key)
        _LOGGER.warning(
            "Entity State Tracker: %s %s breakdown %.1fs exceeds window %.1fs "
            "(clamped); this indicates a bucket-seam over-count",
            self.entity_id,
            frame_key,
            breakdown_total,
            result.window_seconds,
        )

    @staticmethod
    def _prune_ledger(ledger: TrackerLedger, before_iso: str) -> None:
        """Drop in-memory daily buckets before ``before_iso`` (mirrors storage).

        The coordinator holds the same ledger object the store persists, so
        pruning here + one debounced flush avoids ``store.prune_days``' own save
        stacking a second disk write on the flush (P4).
        """
        stale = [day for day in ledger.daily if day < before_iso]
        for day in stale:
            del ledger.daily[day]

    def _live_today_blocks(
        self,
        ledger: TrackerLedger,
        start_utc: dt.datetime,
        end_utc: dt.datetime,
        now: dt.datetime,
    ) -> dict[str, dict[str, float]]:
        """Recorder-off fallback: the open state's slice from live meta (§15).

        With no recorder we cannot reconstruct today's transitions; the best we
        have is the current state carried from ``last_changed_ts`` to now.
        """
        state = ledger.last_state
        ts = _parse_ts(ledger.last_changed_ts)
        if state is None or ts is None:
            live_state = self.hass.states.get(self.entity_id)
            if live_state is None:
                return {}
            state = live_state.state
            ts = live_state.last_changed
            if ts.tzinfo is None:  # pragma: no cover
                ts = ts.replace(tzinfo=dt.UTC)
        block_start = max(ts, start_utc)
        secs = (min(end_utc, now) - block_start).total_seconds()
        if secs <= 0:
            return {}
        return {state: {"secs": secs, "count": 1}}

    @callback
    def _raise_recorder_off_issue(self) -> None:
        """Raise the recorder-disabled Repair issue at most once (§15)."""
        flags = self.hass.data.setdefault(DOMAIN, {})
        if flags.get(_RECORDER_OFF_ISSUE):
            return
        flags[_RECORDER_OFF_ISSUE] = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _RECORDER_OFF_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_RECORDER_OFF_ISSUE,
        )

    @callback
    def _clear_recorder_off_issue(self) -> None:
        """Delete the recorder-disabled Repair once the recorder recovers (§15).

        The raise-side guard flag suppresses re-raising while the recorder stays
        down; without a matching clear the Repair would linger forever after the
        recorder came back. When a query succeeds and the flag is set, drop the
        issue and reset the flag so a later outage can raise it afresh.
        """
        flags = self.hass.data.setdefault(DOMAIN, {})
        if not flags.get(_RECORDER_OFF_ISSUE):
            return
        flags[_RECORDER_OFF_ISSUE] = False
        ir.async_delete_issue(self.hass, DOMAIN, _RECORDER_OFF_ISSUE)


def _parse_ts(iso: str | None) -> dt.datetime | None:
    """Parse an ISO timestamp to a tz-aware UTC datetime (``None`` on failure)."""
    if not iso:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:  # pragma: no cover
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _parse_day(iso: str | None) -> dt.date | None:
    """Parse a ``YYYY-MM-DD`` local-day key (``None`` on failure)."""
    if not iso:
        return None
    try:
        return dt.date.fromisoformat(iso)
    except ValueError:
        return None
