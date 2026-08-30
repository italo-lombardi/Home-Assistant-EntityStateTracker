"""Data models for Entity State Tracker.

Three concerns live here:

* :class:`StoredData` / :class:`TrackerLedger` — the persisted daily-bucket
  ledger (§6.2). One :class:`TrackerLedger` per config entry, keyed inside
  :class:`StoredData` by ``entry_id``. ``from_dict`` is defensive: malformed
  rows are swallowed rather than raising, so a partially corrupt store still
  loads (mirrors the WashWise store contract).
* :class:`FrameResult` / :class:`TrackerData` — the coordinator's computed
  output (§5.2). Never persisted; rebuilt every tick from ledger + recorder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FrameResult:
    """Computed metrics for a single frame (window) of one tracker.

    ``breakdown_*`` / ``counts`` / ``avg_duration`` are keyed by state name.
    ``dominant`` is the max-duration state. ``window_start`` is the frame
    window's start as a local-time ISO timestamp (§5.1). ``percent`` is the
    tracked-state share (specific mode) and ``compliance_percent`` the target
    share (specific mode with a target set); both are ``None`` when N/A.
    ``unaccounted_seconds`` is the window time not attributed to any state
    (pre-data gap and/or transient open-state lag), so the card can render a
    trailing slice and the donut visibly sums to 100.
    """

    window_seconds: float
    breakdown_seconds: dict[str, float] = field(default_factory=dict)
    breakdown_pct: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    avg_duration: dict[str, float | None] = field(default_factory=dict)
    dominant: str | None = None
    window_start: str | None = None
    data_start: str | None = None
    window_coverage: float = 1.0
    has_gap: bool = False
    percent: float | None = None
    compliance_percent: float | None = None
    unaccounted_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class TrackerData:
    """Coordinator output for one tracker — not persisted.

    ``frames`` maps each enabled frame key to its :class:`FrameResult`.
    ``last_state`` / ``previous_state`` are the live transition context.
    """

    frames: dict[str, FrameResult] = field(default_factory=dict)
    last_state: str | None = None
    previous_state: str | None = None


@dataclass
class TrackerLedger:
    """Persisted daily-bucket ledger for one tracked entity (§6.2).

    ``daily`` maps ``local_day_iso`` → state → ``{"secs": float, "count": int}``.
    ``count`` is entries into the state that day (a midnight-spanning visit is
    counted once, on the day it began). Timestamps are ISO strings.
    """

    entity_id: str
    mode: str
    states: list[str] | None = None
    target: list[str] | None = None
    daily: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    last_state: str | None = None
    last_changed_ts: str | None = None
    last_updated_day: str | None = None
    # The min_state_duration (glitch threshold) the closed-day daily buckets were
    # LAST built with. Persisted so an options edit that changes the threshold can
    # detect stale buckets across a restart and re-backfill them with the new
    # value (H1) — closed-day buckets written under the old threshold would
    # otherwise mix silently with new-threshold folds. ``None`` on a fresh/legacy
    # ledger; the coordinator seeds it on first build.
    built_min_state_duration: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrackerLedger:
        """Build from a raw dict, dropping malformed buckets/rows."""
        daily: dict[str, dict[str, dict[str, Any]]] = {}
        for day, states in (d.get("daily") or {}).items():
            if not isinstance(states, dict):
                continue
            day_bucket: dict[str, dict[str, Any]] = {}
            for state, row in states.items():
                if not isinstance(row, dict):
                    continue
                try:
                    day_bucket[state] = {
                        "secs": float(row.get("secs", 0.0)),
                        "count": int(row.get("count", 0)),
                    }
                except (TypeError, ValueError):
                    continue
            if day_bucket:
                daily[str(day)] = day_bucket
        states_raw = d.get("states")
        target_raw = d.get("target")
        built = d.get("built_min_state_duration")
        try:
            built_min_state_duration = float(built) if built is not None else None
        except (TypeError, ValueError):
            built_min_state_duration = None
        return cls(
            entity_id=str(d.get("entity_id", "")),
            mode=str(d.get("mode", "")),
            states=[str(s) for s in states_raw]
            if isinstance(states_raw, list)
            else None,
            target=[str(s) for s in target_raw]
            if isinstance(target_raw, list)
            else None,
            daily=daily,
            last_state=d.get("last_state"),
            last_changed_ts=d.get("last_changed_ts"),
            last_updated_day=d.get("last_updated_day"),
            built_min_state_duration=built_min_state_duration,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "mode": self.mode,
            "states": list(self.states) if self.states is not None else None,
            "target": list(self.target) if self.target is not None else None,
            "daily": {
                day: {state: dict(row) for state, row in states.items()}
                for day, states in self.daily.items()
            },
            "last_state": self.last_state,
            "last_changed_ts": self.last_changed_ts,
            "last_updated_day": self.last_updated_day,
            "built_min_state_duration": self.built_min_state_duration,
        }


@dataclass
class StoredData:
    """Root persisted document — one ledger per config entry (§6.2)."""

    version: int = 1
    trackers: dict[str, TrackerLedger] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> StoredData:
        return cls(version=1, trackers={})

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> StoredData:
        """Build from a raw dict, dropping malformed tracker rows."""
        if not d:
            return cls.empty()
        trackers_raw = d.get("trackers") or {}
        trackers: dict[str, TrackerLedger] = {}
        for key, row in trackers_raw.items():
            if not isinstance(row, dict):
                continue
            try:
                trackers[str(key)] = TrackerLedger.from_dict(row)
            except (TypeError, ValueError, KeyError):
                continue
        try:
            version = int(d.get("version", 1))
        except (TypeError, ValueError):
            version = 1
        return cls(version=version, trackers=trackers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "trackers": {k: v.to_dict() for k, v in self.trackers.items()},
        }
