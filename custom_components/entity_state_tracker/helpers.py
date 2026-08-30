"""Shared pure helpers for Entity State Tracker.

Pure functions only — no HA I/O side effects, no storage. Callers (config flow,
engine, coordinator, sensor, card) share these so boundary/label/color logic has
a single source of truth.
"""

from __future__ import annotations

from .const import FRAMES

# Human labels per frame. Plain nouns for calendar frames; the rolling frames
# name their span ("Last 24 hours"/"Last 7 days") without a "(rolling)" tag.
# "30d" is last-30-whole-days (not true rolling past retention — §6.4).
_FRAME_LABELS: dict[str, str] = {
    "today": "Today",
    "yesterday": "Yesterday",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "month": "This month",
    "year": "This year",
}


def tracker_device_name(entity_label: str) -> str:
    """Return the tracker's device name: ``Entity State Tracker — <label>`` (§5).

    The device name is mode-independent by design — one device per config entry
    regardless of mode — so it takes only the entity label.
    """
    return f"Entity State Tracker — {entity_label}"


def normalize_state(state: str) -> str:
    """Case-normalize a state string (lower + strip).

    Config flow and engine must agree on this so a user-typed ``Heat`` matches a
    recorded ``heat`` (§4 prefilled states are case-normalized).
    """
    return state.strip().lower()


def frame_label(frame_key: str) -> str:
    """Return a human label for a frame (§3).

    Unknown keys fall back to the raw key so a future frame can't crash a label
    lookup — it just renders un-prettified.
    """
    if frame_key in _FRAME_LABELS:
        return _FRAME_LABELS[frame_key]
    return frame_key


def unique_id(entry_id: str, frame: str, metric: str) -> str:
    """Build an entity unique_id: ``<entry_id>_<frame>_<metric>`` (§5)."""
    return f"{entry_id}_{frame}_{metric}"


if __name__ == "__main__":  # pragma: no cover
    # Self-check: normalization, device name, and every canonical frame labels.
    assert normalize_state("  Heat ") == "heat"
    assert tracker_device_name("Front Door") == ("Entity State Tracker — Front Door")
    assert frame_label("24h") == "Last 24 hours"
    assert frame_label("today") == "Today"
    assert frame_label("mystery") == "mystery"
    assert unique_id("abc", "today", "duration") == "abc_today_duration"
    assert set(_FRAME_LABELS) == set(FRAMES)
    print("helpers self-check OK")
