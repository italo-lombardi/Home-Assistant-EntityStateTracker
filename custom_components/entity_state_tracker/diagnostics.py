"""Diagnostics support for Entity State Tracker (§9).

Dumps the config entry, a per-frame summary of the coordinator's last computed
output, aggregate ledger stats (day span + per-state second/count totals), and
the store's disk-read counter. Nothing here is sensitive (entity ids and state
names only), so ``TO_REDACT`` is empty — kept for shape parity with the sibling
repos and as the obvious hook if a future field ever needs redaction.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EntityStateTrackerCoordinator
from .models import TrackerLedger

TO_REDACT: set[str] = set()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EntityStateTrackerCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if not isinstance(coordinator, EntityStateTrackerCoordinator):
        return {"error": "coordinator not loaded"}

    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": {
                "entity_id": coordinator.entity_id,
                "mode": coordinator.mode,
                "tracked_states": coordinator.tracked_states,
                "target_states": coordinator.target_states,
                "target_threshold": coordinator.target_threshold,
                "min_state_duration": coordinator.min_state_duration,
                "enabled_frames": coordinator.enabled_frames,
                "last_update_success": coordinator.last_update_success,
            },
            "frames": _frame_summary(coordinator),
            "ledger": _ledger_stats(coordinator),
            "store": {"disk_read_count": coordinator.store.disk_read_count},
        },
        TO_REDACT,
    )


def _frame_summary(coordinator: EntityStateTrackerCoordinator) -> dict[str, Any]:
    """Summarize each computed frame (percent/dominant/gap/coverage)."""
    data = coordinator.data
    if data is None:
        return {}
    return {
        frame_key: {
            "window_seconds": result.window_seconds,
            "percent": result.percent,
            "compliance_percent": result.compliance_percent,
            "dominant": result.dominant,
            "window_coverage": result.window_coverage,
            "has_gap": result.has_gap,
            "data_start": result.data_start,
            "states": len(result.breakdown_seconds),
        }
        for frame_key, result in data.frames.items()
    }


def _ledger_stats(coordinator: EntityStateTrackerCoordinator) -> dict[str, Any]:
    """Aggregate day span and per-state totals from the in-memory ledger."""
    ledger = getattr(coordinator, "_ledger", None)
    if not isinstance(ledger, TrackerLedger):
        return {"loaded": False}

    days = sorted(ledger.daily)
    per_state_secs: dict[str, float] = {}
    per_state_count: dict[str, int] = {}
    for day_bucket in ledger.daily.values():
        for state, row in day_bucket.items():
            per_state_secs[state] = per_state_secs.get(state, 0.0) + float(
                row.get("secs", 0.0)
            )
            per_state_count[state] = per_state_count.get(state, 0) + int(
                row.get("count", 0)
            )

    return {
        "loaded": True,
        "day_count": len(days),
        "oldest_day": days[0] if days else None,
        "newest_day": days[-1] if days else None,
        "last_state": ledger.last_state,
        "last_changed_ts": ledger.last_changed_ts,
        "last_updated_day": ledger.last_updated_day,
        "per_state_seconds": per_state_secs,
        "per_state_count": per_state_count,
    }
