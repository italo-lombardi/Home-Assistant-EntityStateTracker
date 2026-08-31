"""Correctness-critical shared spine for Entity State Tracker.

Pure functions only — no class state, no ``Store``, no coordinator. ``hass``
is threaded in solely for the recorder query and the local time zone; every
other input is an explicit argument so each function is unit-testable in
isolation (the R1/R3/R4/R6/R7/R8/R9/R10 guards hit these directly).

Design invariants (see ENTITYSTATETRACKER_PLAN.md §6):

* **One boundary source.** :func:`resolve_frame_bounds` is the *only* place a
  frame window is turned into ``(start_utc, end_utc)``. The fold-split and the
  today-slice-start both resolve through it, which is what closes the
  UTC-vs-local seam (§6.3, guards R3).
* **Real elapsed seconds everywhere.** Durations are wall-clock deltas between
  ``datetime`` instants, never ``86400`` or a count of calendar days, so a
  23h/25h DST day is accounted for correctly (§6.3, guards R1).
* **``unavailable``/``unknown``/``none`` are ordinary state names.** No
  coalescing, no "offline" bucket — a single wall-clock denominator (§5.2).
* **Glitch filter merges into the PRECEDING block.** A contiguous block shorter
  than ``min_state_duration`` is absorbed by the state that preceded it and is
  dropped from *both* ``secs`` and ``count`` (§6.5, guards R6).
"""

from __future__ import annotations

import datetime as dt
import logging

from homeassistant.core import HomeAssistant, State

from .const import DOMINANT_HYSTERESIS_PCT, FRAMES, LEDGER_MAX_DAYS
from .models import FrameResult

_LOGGER = logging.getLogger(__name__)

# Prune margin (days) added below the oldest enabled-frame start before the
# LEDGER_MAX_DAYS cap applies (§6.2).
_PRUNE_MARGIN_DAYS = 2

# Tolerance (seconds) below which a breakdown that exceeds the window is treated
# as sub-second rounding noise, not a real seam over-count. The coordinator's
# once-per-(entry, frame) overflow warning uses the same floor (L4).
OVERFLOW_TOLERANCE_SECS = 1.0

# A frame is treated as fully covered (no real "unaccounted" slice) when its
# uncovered remainder is at or below this floor — the same 1.0s tolerance the
# overflow guard uses. At/below it the sub-0.05% pct-rounding drift is folded
# into the largest real slice; above it "unaccounted" is a real slice and
# absorbs the drift as the balancing term.
_UNACCOUNTED_EPSILON_SECS = 1.0

type StatesList = list[State]
type BlockMap = dict[str, dict[str, float]]


def resolve_frame_bounds(
    frame_key: str,
    now: dt.datetime,
    tz: dt.tzinfo,
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve one frame key to its ``(start_utc, end_utc)`` window.

    THE single local-day boundary helper. ``now`` may be in any zone; it is
    normalised to ``tz`` first so every calendar boundary lands on a real local
    midnight/1st/Jan-1. ``end_utc`` is ``now`` for every open-ended window and
    the local-midnight edge for closed calendar frames (``yesterday``).

    * calendar — ``today``/``yesterday``/``week``/``month``/``year`` snap to
      local boundaries; ``end`` is ``now`` (or the closing midnight for
      ``yesterday``). ``week`` starts at local Monday 00:00 (week-to-date).
    * rolling — ``24h``/``7d`` are ``now − delta → now``.
    * ``30d`` — "last 30 whole local days": ``[today_midnight − 30 days,
      today_midnight)`` (§6.4), so the tail day is queryable-complete and the
      window never includes the partial open day.

    Raises ``ValueError`` for an unknown frame key so a typo fails loudly rather
    than silently producing a zero window.
    """
    if frame_key not in FRAMES:
        raise ValueError(f"Unknown frame key: {frame_key!r}")

    local_now = now.astimezone(tz)
    now_utc = now.astimezone(dt.UTC)
    today_midnight_local = _start_of_local_day(local_now, tz)

    if frame_key == "today":
        return today_midnight_local.astimezone(dt.UTC), now_utc

    if frame_key == "yesterday":
        start = _rewind_local_days(today_midnight_local, 1, tz)
        return start.astimezone(dt.UTC), today_midnight_local.astimezone(dt.UTC)

    if frame_key == "24h":
        return now_utc - dt.timedelta(hours=24), now_utc

    if frame_key == "week":
        # Week-to-date: since local Monday 00:00 (ISO weekday 1) → now.
        start = _rewind_local_days(today_midnight_local, local_now.weekday(), tz)
        return start.astimezone(dt.UTC), now_utc

    if frame_key == "7d":
        return now_utc - dt.timedelta(days=7), now_utc

    if frame_key == "30d":
        # Last 30 WHOLE local days — ends at today's local midnight, not now.
        start = _rewind_local_days(today_midnight_local, 30, tz)
        return (
            start.astimezone(dt.UTC),
            today_midnight_local.astimezone(dt.UTC),
        )

    if frame_key == "month":
        start = today_midnight_local.replace(day=1)
        return start.astimezone(dt.UTC), now_utc

    # year
    start = today_midnight_local.replace(month=1, day=1)
    return start.astimezone(dt.UTC), now_utc


def split_visit_across_days(
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    tz: dt.tzinfo,
) -> list[tuple[str, float]]:
    """Split ``[start_utc, end_utc)`` at LOCAL midnights.

    Returns ``[(local_day_iso, secs), ...]`` where each ``secs`` is the *real*
    elapsed wall-clock time attributed to that local day (DST-safe — a fold that
    straddles a spring-forward yields 23h for that day, not 24h). The caller
    attributes ``count`` once, on the *start* day only (§6.2).

    An empty or reversed interval yields ``[]``.
    """
    if end_utc <= start_utc:
        return []

    out: list[tuple[str, float]] = []
    cursor = start_utc
    while cursor < end_utc:
        local_cursor = cursor.astimezone(tz)
        day_iso = local_cursor.date().isoformat()
        # Next local midnight strictly after ``cursor``.
        next_midnight_local = _start_of_local_day(local_cursor, tz) + dt.timedelta(
            days=1
        )
        segment_end = min(next_midnight_local.astimezone(dt.UTC), end_utc)
        # ``segment_end`` is strictly after ``cursor`` (next midnight > cursor and
        # end_utc > cursor by the loop guard), so the segment is always positive.
        out.append((day_iso, (segment_end - cursor).total_seconds()))
        cursor = segment_end
    return out


def accumulate_blocks(
    states_list: StatesList,
    window_start: dt.datetime,
    window_end: dt.datetime,
    min_state_duration: float,
    now: dt.datetime,
) -> BlockMap:
    """History-stats-style contiguous-block accumulation, per state.

    ``states_list`` must be the recorder result queried with
    ``include_start_time_state=True`` — its first element is the state in force
    at ``window_start``. Returns ``{state: {"secs": float, "count": int}}``.

    Rules (§6.5, guards R6):

    * Each maximal run of one state name is a block; ``secs`` is its real
      elapsed duration clamped to ``[window_start, min(window_end, now)]``.
    * A block shorter than ``min_state_duration`` is a *glitch*: its time is
      re-attributed to the **preceding** block's state (carry-forward) and it
      is dropped from both ``secs`` and ``count``. A leading glitch (no
      predecessor) is dropped outright — its time is unattributable.
    * The trailing open block extends to ``min(window_end, now)``.
    * ``Σ(secs)`` equals the covered wall-clock of the window (modulo any
      unattributable leading-glitch time).

    ``count`` is the number of *qualifying* entries into each state within the
    window (a block that survives the glitch filter increments its state's
    count by one) — EXCEPT the leading in-force block. When
    ``states_list[0].last_changed < window_start`` the recorder's synthetic
    ``include_start_time_state`` row describes a visit that *began before this
    window*: it is a continuation, already counted in the ledger day it began
    (§6.2, guards R1). It contributes ``secs`` but ``count == 0``, so the
    ledger-day-before + today-slice seam does not double-count one continuous
    visit that straddles midnight. This mirrors the live-fold path, which counts
    a visit once on its start day only.
    """
    result: BlockMap = {}
    measure_end = min(window_end, now)
    if measure_end <= window_start or not states_list:
        return result

    # The recorder's first row is the state in force AT window_start; if its
    # own ``last_changed`` predates the window, the leading block is a
    # continuation of a pre-window visit and must not be counted as a fresh
    # entry (it's already counted on its true start day).
    leading_is_continuation = states_list[0].last_changed < window_start

    # Build (state, block_start, block_end, counts) quads clamped to the window,
    # collapsing consecutive same-state rows into one contiguous block. The
    # ``counts`` flag rides through the glitch filter so the leading
    # continuation stays count-free even after merges.
    raw: list[tuple[str, dt.datetime, dt.datetime, bool]] = []
    for i, state in enumerate(states_list):
        block_start = max(window_start, state.last_changed)
        if block_start >= measure_end:
            break
        if i + 1 < len(states_list):
            block_end = min(states_list[i + 1].last_changed, measure_end)
        else:
            block_end = measure_end
        if block_end <= block_start:
            continue
        name = state.state
        counts = not (i == 0 and leading_is_continuation)
        if raw and raw[-1][0] == name:
            # Merge consecutive identical-state rows into the open block.
            prev_name, prev_start, _, prev_counts = raw[-1]
            raw[-1] = (prev_name, prev_start, block_end, prev_counts)
        else:
            raw.append((name, block_start, block_end, counts))

    # Apply the glitch filter: fold sub-threshold blocks into the predecessor.
    # When a glitch is absorbed, the block that follows it coalesces into that
    # same predecessor if it shares its state — the entity never really left,
    # so it stays ONE visit (one ``count``), not two (guards R6).
    filtered: list[tuple[str, float, bool]] = []
    for name, b_start, b_end, counts in raw:
        secs = (b_end - b_start).total_seconds()
        is_glitch = min_state_duration > 0 and secs < min_state_duration
        if is_glitch and filtered:
            # Merge glitch time into the preceding surviving block.
            prev_name, prev_secs, prev_counts = filtered[-1]
            filtered[-1] = (prev_name, prev_secs + secs, prev_counts)
            continue
        if is_glitch and not filtered:
            # Leading glitch with no predecessor — unattributable, drop it.
            continue
        if filtered and filtered[-1][0] == name:
            # Same state as the (now-adjacent) predecessor — one contiguous
            # block: extend it rather than opening a second visit.
            prev_name, prev_secs, prev_counts = filtered[-1]
            filtered[-1] = (prev_name, prev_secs + secs, prev_counts)
            continue
        filtered.append((name, secs, counts))

    for name, secs, counts in filtered:
        row = result.setdefault(name, {"secs": 0.0, "count": 0})
        row["secs"] += secs
        if counts:
            row["count"] += 1
    return result


def carry_forward_states(states_list: StatesList) -> StatesList:
    """Normalise a recorder result for HA-down-gap carry-forward (§8, guards R4).

    Across an HA-down gap the recorder holds no rows, so the last-known state is
    implicitly carried forward — matching ``history_stats`` and the recorder's
    own behaviour, which is correct for steady entities (a light left ``on``).

    The one correction (R4): HA writes an ``unavailable`` row on a *clean*
    shutdown. If that is the *last* row the recorder returns, everything after it
    to ``now`` is the HA-down gap, and the trailing-block rule in
    :func:`accumulate_blocks` would otherwise attribute the whole gap to
    ``unavailable`` as real occupancy. That row marks the gap *start*, not a real
    ``unavailable`` visit, so we drop it: the block that precedes it then carries
    forward across the gap (the documented steady-entity heuristic — a light left
    ``on`` reads ``on`` across the outage, not ``unavailable``).

    A ``unavailable`` row that is *not* the last row (a real transient
    unavailability that HA later recovered from) is left untouched — it occupies
    only its own interval, exactly like any ordinary state name.

    Dropping the sole row (a result that is *only* a trailing shutdown marker)
    would erase all history, so it is kept — there is no preceding state to carry
    forward and an empty list is strictly worse.
    """
    if len(states_list) > 1 and states_list[-1].state == "unavailable":
        return states_list[:-1]
    return states_list


async def query_recorder(
    hass: HomeAssistant,
    entity_id: str,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> StatesList | None:
    """Query recorder state changes for ``[start_utc, end_utc)``.

    Uses ``state_changes_during_period(include_start_time_state=True,
    no_attributes=True)`` on the recorder executor. Returns the entity's state
    list (possibly empty), or ``None`` when the recorder is unavailable so the
    caller can fall back to live-only accumulation (§15).

    Recorder-absent detection (verified against HA core): ``get_instance`` reads
    ``hass.data[DATA_INSTANCE]`` through an ``lru_cache`` and *raises* ``KeyError``
    when the recorder is not set up — it never returns ``None``. So the absence
    signal is the ``KeyError``, caught here and mapped to ``None`` (mirroring
    :meth:`EntityStateTrackerCoordinator._recorder_retention_start`); there is no
    ``instance is None`` case to guard.
    """
    # Imported lazily: recorder is an ``after_dependencies`` and may be absent.
    from homeassistant.components.recorder import get_instance, history

    try:
        instance = get_instance(hass)
    except KeyError:
        # Recorder not set up: get_instance raises rather than returning None.
        return None

    def _query() -> StatesList:
        return history.state_changes_during_period(
            hass,
            start_utc,
            end_utc,
            entity_id,
            include_start_time_state=True,
            no_attributes=True,
        ).get(entity_id, [])

    return await instance.async_add_executor_job(_query)


def _merge_block_maps(base: BlockMap, other: BlockMap) -> BlockMap:
    """Return ``base`` with ``other`` summed in (secs added, counts added)."""
    for name, row in other.items():
        into = base.setdefault(name, {"secs": 0.0, "count": 0})
        into["secs"] += row["secs"]
        into["count"] += row["count"]
    return base


def _ledger_days_before(
    daily: dict[str, dict[str, dict[str, float]]],
    window_start_local_day: str,
    upper_exclusive_local_day: str,
) -> BlockMap:
    """Sum ledger buckets for closed local days inside ``[start_day, upper)``.

    ``upper_exclusive_local_day`` is the local day at which the *recorder* takes
    over. For a calendar frame that is ``today_local_day`` (the ledger owns every
    closed day; the open day is recomputed fresh from the recorder). For a
    ROLLING frame it is the recorder-floor's local day — the ledger only fills
    WHOLE days strictly below the point the recorder query starts at, so a
    mid-day window_start never pulls in the oldest partial day as a whole bucket
    (§6.4, no double count, no over-count at the seam).
    """
    agg: BlockMap = {}
    for day, states in daily.items():
        if day < window_start_local_day or day >= upper_exclusive_local_day:
            continue
        for name, row in states.items():
            into = agg.setdefault(name, {"secs": 0.0, "count": 0})
            into["secs"] += float(row.get("secs", 0.0))
            into["count"] += int(row.get("count", 0))
    return agg


def compute_frame(
    frame_key: str,
    now: dt.datetime,
    tz: dt.tzinfo,
    recent_blocks: BlockMap,
    ledger_daily: dict[str, dict[str, dict[str, float]]],
    ledger_data_start_iso: str | None,
    *,
    mode: str,
    tracked_states: list[str] | None,
    target_states: list[str] | None,
    prior_dominant: str | None,
    ledger_upper_local_day: str | None = None,
) -> FrameResult:
    """Combine recorder (recent) + ledger (long) into one :class:`FrameResult`.

    No double-count: the ledger contributes *closed local days only* and the
    recent portion arrives via ``recent_blocks``. The seam between them is
    ``ledger_upper_local_day`` — the local day at which the recorder takes over.

    * **Calendar frames** (``today``/``yesterday``/``30d``/``month``/``year``)
      start on a local midnight, so their windows are whole local days and the
      seam is ``today_local_day`` (the default): the ledger owns every closed
      day and the recorder recomputes the open day.
    * **Rolling frames** (``24h``/``7d``) start MID-DAY. The caller anchors the
      recorder query at ``recorder_floor`` and passes that day here as
      ``ledger_upper_local_day``, so the ledger fills only WHOLE days strictly
      below it — the oldest partial day is counted at its real recorder seconds,
      never as a whole ``86400`` bucket (the over-count the frame-agnostic
      invariant guards against).

    For windows entirely within retention the caller may pass the whole window
    as ``recent_blocks`` and an empty ``ledger_daily`` — the seam math still
    holds because ledger days are only counted below ``ledger_upper_local_day``.

    ``recent_blocks`` MUST already be glitch-filtered (via
    :func:`accumulate_blocks`); this function does no block math, only summation
    and derived metrics.

    Percent denominator is ``(end_utc − start_utc)`` in real seconds, never
    ``86400`` (§6.3, guards R1). ``dominant`` flips from ``prior_dominant`` only
    when a new leader exceeds it by more than ``DOMINANT_HYSTERESIS_PCT`` of the
    window (§6.6, guards R10). ``avg_duration`` is ``round(secs / count, 1)``
    (``None`` at ``count == 0``). ``breakdown_pct`` is 2-dp with a nonzero-slice sentinel
    (a state holding real time never renders as ``0.0`` — see :func:`_pct`); it
    additionally carries an ``"unaccounted"`` key (the remainder as a percent,
    ``0.0`` when fully covered) and is balanced so a template looping
    ``breakdown_pct`` sums to EXACTLY ``100.00``: the least-meaningful term
    (``"unaccounted"`` on a gap frame, else the largest real slice) absorbs the
    sub-0.05% rounding drift. That key is absent from
    ``breakdown_seconds``/``counts``/
    ``avg_duration`` — it is not a real state, so those stay per-state pure and
    ``dominant`` can never be ``"unaccounted"``.
    ``unaccounted_seconds`` is the window time attributed to no state.
    """
    start_utc, end_utc = resolve_frame_bounds(frame_key, now, tz)
    window_seconds = (end_utc - start_utc).total_seconds()

    today_local_day = now.astimezone(tz).date().isoformat()
    window_start_local_day = start_utc.astimezone(tz).date().isoformat()
    upper_local_day = ledger_upper_local_day or today_local_day

    combined: BlockMap = {}
    _merge_block_maps(
        combined,
        _ledger_days_before(ledger_daily, window_start_local_day, upper_local_day),
    )
    _merge_block_maps(combined, {k: dict(v) for k, v in recent_blocks.items()})

    breakdown_seconds = {name: row["secs"] for name, row in combined.items()}
    counts = {name: int(row["count"]) for name, row in combined.items()}
    avg_duration: dict[str, float | None] = {
        # Seconds per visit, 1-dp float (359s / 2 → 179.5), matching
        # breakdown_pct's precision so the display layer sees a consistent
        # granularity across metrics. ``None`` at count 0 (a ledger
        # continuation day).
        name: (round(row["secs"] / row["count"], 1) if row["count"] else None)
        for name, row in combined.items()
    }
    # Window time attributed to no state — the pre-data gap and/or a transient
    # open-state lag. A single honest number the card renders as a trailing
    # slice so the donut sums to 100. Kept OUT of breakdown_seconds/counts/
    # avg_duration so per-state counts/color/dominant stay pure (it is not a
    # real state and has no count/avg).
    breakdown_total = sum(breakdown_seconds.values())
    # Invariant (v8): the breakdown can never exceed the window. A >1s overflow
    # means a bucket seam over-counted (the rolling-frame partial-day bug this
    # fix closes). We clamp so a negative unaccounted can never surface; the
    # once-per-(entry, frame) diagnostic warning lives in the coordinator (L4),
    # which owns entry_id and can warn per tracker instead of once per label
    # process-wide.
    unaccounted_seconds = max(0.0, window_seconds - breakdown_total)

    breakdown_pct = {
        name: _pct(secs, window_seconds) for name, secs in breakdown_seconds.items()
    }
    # Inject an additive "unaccounted" key in breakdown_pct ONLY (never a real
    # state) so a template looping breakdown_pct sums to EXACTLY 100.00 — not
    # 100.0x, which sum-of-independently-rounded-parts otherwise produces. Real
    # per-state slices keep their own _pct (rounding + tiny-nonzero sentinel);
    # only the LEAST-meaningful term absorbs the sub-0.05% rounding drift:
    #   - Genuine gap (unaccounted_seconds > EPSILON): "unaccounted" is a real
    #     slice, so it takes the balance (100 - Σ reals). Drift lands there.
    #   - Fully covered (unaccounted_seconds <= EPSILON, has_gap False): there is
    #     no real remainder, so "unaccounted" is 0.0 and the drift is folded into
    #     the largest real slice (max secs, tie-break by name) instead — the real
    #     states themselves then sum to 100.00 without a phantom gap slice.
    # dominant is picked from breakdown_seconds (no "unaccounted" key), so the
    # remainder can never win dominant regardless of this balancing.
    real_pct_sum = round(sum(breakdown_pct.values()), 2)
    if window_seconds <= 0:
        breakdown_pct["unaccounted"] = 0.0
    elif unaccounted_seconds <= _UNACCOUNTED_EPSILON_SECS:
        breakdown_pct["unaccounted"] = 0.0
        drift = round(100.0 - real_pct_sum, 2)
        if drift and breakdown_seconds:
            largest = max(
                breakdown_seconds,
                key=lambda k: (breakdown_seconds[k], k),
            )
            breakdown_pct[largest] = round(breakdown_pct[largest] + drift, 2)
    else:
        breakdown_pct["unaccounted"] = round(100.0 - real_pct_sum, 2)

    dominant = _pick_dominant(breakdown_seconds, window_seconds, prior_dominant)

    percent = _subset_percent(breakdown_seconds, tracked_states, window_seconds)
    compliance_percent = _subset_percent(
        breakdown_seconds, target_states, window_seconds
    )

    data_start, window_coverage, has_gap = _coverage(
        start_utc, end_utc, ledger_data_start_iso, tz, window_seconds
    )

    return FrameResult(
        window_seconds=window_seconds,
        breakdown_seconds=breakdown_seconds,
        breakdown_pct=breakdown_pct,
        counts=counts,
        avg_duration=avg_duration,
        dominant=dominant,
        window_start=start_utc.astimezone(tz).isoformat(),
        data_start=data_start,
        window_coverage=window_coverage,
        has_gap=has_gap,
        percent=percent,
        compliance_percent=compliance_percent,
        unaccounted_seconds=unaccounted_seconds,
    )


def _pct(secs: float, window_seconds: float) -> float:
    """Percent of the window, 2-dp, with a nonzero-slice sentinel (Fix 3a).

    A slice that holds real time must never render as exactly ``0.0`` (the card
    and templates do arithmetic on this number, so it stays numeric). When the
    rounded value collapses to zero but ``secs > 0``, floor it to a small
    sentinel so a tiny-but-present state stays visible.
    """
    if window_seconds <= 0:
        return 0.0
    pct = round(secs / window_seconds * 100, 2)
    if pct == 0.0 and secs > 0:
        return 0.01
    return pct


def _pick_dominant(
    breakdown_seconds: dict[str, float],
    window_seconds: float,
    prior_dominant: str | None,
) -> str | None:
    """Return the max-seconds state, applying hysteresis vs ``prior_dominant``.

    Only flip away from ``prior_dominant`` when the new leader exceeds the prior
    dominant's seconds by more than ``DOMINANT_HYSTERESIS_PCT`` of the window
    (§6.6, guards R10). Near-ties therefore keep the incumbent and don't flap.
    """
    if not breakdown_seconds:
        return None
    leader = max(breakdown_seconds, key=lambda k: breakdown_seconds[k])
    if prior_dominant is None or prior_dominant == leader:
        return leader
    if prior_dominant not in breakdown_seconds:
        return leader
    margin = window_seconds * DOMINANT_HYSTERESIS_PCT / 100
    if breakdown_seconds[leader] - breakdown_seconds[prior_dominant] > margin:
        return leader
    return prior_dominant


def _subset_percent(
    breakdown_seconds: dict[str, float],
    subset: list[str] | None,
    window_seconds: float,
) -> float | None:
    """Percent of the window spent in any of ``subset`` (``None`` when N/A)."""
    if not subset or window_seconds <= 0:
        return None
    wanted = set(subset)
    matched = sum(secs for name, secs in breakdown_seconds.items() if name in wanted)
    return round(matched / window_seconds * 100, 1)


def _coverage(
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    ledger_data_start_iso: str | None,
    tz: dt.tzinfo,
    window_seconds: float,
) -> tuple[str | None, float, bool]:
    """Compute ``(data_start_iso, window_coverage, has_gap)``.

    When the window reaches further back than the oldest data we hold, the
    window is only partially covered: ``data_start`` is surfaced (so the card
    can render "since <date>"), ``window_coverage`` is the covered fraction, and
    ``has_gap`` is set (§8, guards R7). ``data_start`` is returned as an ISO
    timestamp in local time.
    """
    if ledger_data_start_iso is None:
        return None, 1.0, False

    data_start_local = _parse_local_day_start(ledger_data_start_iso, tz)
    if data_start_local is None:
        return None, 1.0, False

    data_start_utc = data_start_local.astimezone(dt.UTC)
    if data_start_utc <= start_utc:
        return None, 1.0, False

    covered = (end_utc - data_start_utc).total_seconds()
    coverage = (
        max(0.0, min(1.0, covered / window_seconds)) if window_seconds > 0 else 0.0
    )
    return data_start_local.isoformat(), round(coverage, 4), True


def prune_cutoff_iso(
    enabled_frames: list[str],
    now: dt.datetime,
    tz: dt.tzinfo,
) -> str:
    """Return the local-day ISO before which ledger buckets may be dropped.

    The cutoff is the earliest enabled-frame start (via the same
    :func:`resolve_frame_bounds` helper — no second boundary source) minus
    ``_PRUNE_MARGIN_DAYS``, floored at ``LEDGER_MAX_DAYS`` back from now so an
    always-on ``year`` frame near a leap-year boundary keeps Jan 1 (§6.2, guards
    R9). Days with a key strictly less than the returned value are stale.
    """
    local_now = now.astimezone(tz)
    today_midnight = _start_of_local_day(local_now, tz)
    hard_floor = today_midnight - dt.timedelta(days=LEDGER_MAX_DAYS)

    earliest_start = today_midnight
    for frame_key in enabled_frames:
        if frame_key not in FRAMES:
            continue
        start_utc, _ = resolve_frame_bounds(frame_key, now, tz)
        start_local = start_utc.astimezone(tz)
        earliest_start = min(earliest_start, start_local)

    cutoff = earliest_start - dt.timedelta(days=_PRUNE_MARGIN_DAYS)
    cutoff = max(cutoff, hard_floor)
    return cutoff.date().isoformat()


def _start_of_local_day(local_dt: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """Local midnight for ``local_dt``'s calendar day, tz-aware in ``tz``.

    Mirrors ``dt_util.start_of_local_day`` but honours the caller-supplied
    ``tz`` (freezegun/test zones) rather than the process default zone.
    """
    return dt.datetime.combine(
        local_dt.astimezone(tz).date(), dt.time(0, 0, 0), tzinfo=tz
    )


def _rewind_local_days(
    midnight_local: dt.datetime, n: int, tz: dt.tzinfo
) -> dt.datetime:
    """Return the local midnight ``n`` calendar days before ``midnight_local``.

    THE day-rewind rule for calendar frames whose start is "N whole local days
    ago" (``yesterday`` N=1, ``week`` N=weekday(), ``30d`` N=30). Correctness is
    made structural rather than implicit: rewind by ``n`` days, then RE-SNAP to
    local midnight. The re-snap is what guarantees DST-safety and self-documents
    it — even though ``midnight_local - timedelta(days=n)`` already lands on local
    00:00 today (subtracting a ``timedelta`` from a ``ZoneInfo``-aware datetime is
    wall-clock arithmetic: it shifts the naive Y/M/D fields and lazily re-derives
    the offset, so the wall clock stays 00:00 while the absolute UTC span absorbs
    the 23h/25h DST day), that safety hinges on a subtle tz property. Snapping via
    ``_start_of_local_day`` makes the midnight landing explicit and immune to a
    future refactor to absolute-time subtraction (``now_utc - timedelta``), which
    WOULD drift the start to 01:00/23:00 across a transition.
    """
    return _start_of_local_day(midnight_local - dt.timedelta(days=n), tz)


def _parse_local_day_start(day_iso: str, tz: dt.tzinfo) -> dt.datetime | None:
    """Parse a ``YYYY-MM-DD`` local-day key to that day's local midnight."""
    try:
        day = dt.date.fromisoformat(day_iso)
    except ValueError:
        return None
    return dt.datetime.combine(day, dt.time(0, 0, 0), tzinfo=tz)
