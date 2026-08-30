"""Shared pure helpers for Entity State Tracker.

Pure functions only — no HA I/O side effects, no storage. Callers (config flow,
engine, coordinator, sensor, card) share these so boundary/label/color logic has
a single source of truth.
"""

from __future__ import annotations

from homeassistant.util import slugify

from .const import (
    DOMAIN,
    FRAMES,
    TRANSLATION_KEY_BREAKDOWN,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
    TRANSLATION_KEY_DURATION,
)

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


# Metric token → the slug the CARD discovers on. The card (frontend
# entity-state-tracker-card.js) keys its DOMAIN_PREFIX discovery on the slugified
# English ENTITY NAME ("Duration"→"duration", "State Breakdown"→"state_breakdown",
# …), NOT on the backend metric KEY (TRANSLATION_KEY_BREAKDOWN is "breakdown").
# We pin entity_id to reproduce those exact tokens so the card's stemOf /
# _matchFrame / prettifyStem discover + label every tracker whatever custom name
# the user gave the device (with has_entity_name=True HA would otherwise slugify
# the device name into the object_id, dropping the "entity_state_tracker_" prefix
# the card matches on — mirrors Entity Availability's explicit self.entity_id).
_METRIC_ENTITY_SLUG: dict[str, str] = {
    TRANSLATION_KEY_DURATION: "duration",
    TRANSLATION_KEY_BREAKDOWN: "state_breakdown",
    TRANSLATION_KEY_CURRENTLY_IN_STATE: "currently_in_state",
    TRANSLATION_KEY_COMPLIANT: "compliant",
}


def _frame_label_slug(frame: str) -> str:
    """Slug of a frame's English label — the card matches on this, not the key.

    e.g. ``7d`` → ``last_7_days`` (slug of "Last 7 days"). Must equal the card's
    ``_slugify(FRAME_LABELS[frame])``; HA ``slugify`` and the card's slugify agree
    (lowercase, non-alphanumerics → "_", collapse, trim).
    """
    return slugify(frame_label(frame))


def frame_entity_id(entry_id: str, frame: str, metric: str) -> str:
    """Pinned entity_id for a per-frame sensor the card can discover.

    ``sensor.entity_state_tracker_<entry_slug>_<metric_slug>_<frame_label_slug>``
    — the EXACT shape the card expects (DOMAIN_PREFIX + metric label slug + frame
    label slug). ``entry_id`` is slugified so multiple trackers get distinct stems
    (mirrors ``unique_id``, which already namespaces by entry_id). See
    ``_METRIC_ENTITY_SLUG``.
    """
    return (
        f"sensor.{DOMAIN}_{slugify(entry_id)}_"
        f"{_METRIC_ENTITY_SLUG[metric]}_{_frame_label_slug(frame)}"
    )


def binary_entity_id(entry_id: str, metric: str) -> str:
    """Pinned entity_id for a (frameless) binary sensor.

    ``binary_sensor.entity_state_tracker_<entry_slug>_<metric_slug>`` — same
    namespacing as ``frame_entity_id`` minus the frame token (the binary sensors
    are not per-frame). Kept prefixed so it shares the tracker's device stem.
    """
    return f"binary_sensor.{DOMAIN}_{slugify(entry_id)}_{_METRIC_ENTITY_SLUG[metric]}"


if __name__ == "__main__":  # pragma: no cover
    # Self-check: normalization, device name, and every canonical frame labels.
    assert normalize_state("  Heat ") == "heat"
    assert tracker_device_name("Front Door") == ("Entity State Tracker — Front Door")
    assert frame_label("24h") == "Last 24 hours"
    assert frame_label("today") == "Today"
    assert frame_label("mystery") == "mystery"
    assert unique_id("abc", "today", "duration") == "abc_today_duration"
    assert set(_FRAME_LABELS) == set(FRAMES)
    # Pinned entity_ids reproduce EXACTLY the card's expected slug shape.
    assert (
        frame_entity_id("01ABC", "7d", TRANSLATION_KEY_DURATION)
        == "sensor.entity_state_tracker_01abc_duration_last_7_days"
    )
    assert (
        frame_entity_id("01ABC", "month", TRANSLATION_KEY_BREAKDOWN)
        == "sensor.entity_state_tracker_01abc_state_breakdown_this_month"
    )
    assert (
        binary_entity_id("01ABC", TRANSLATION_KEY_COMPLIANT)
        == "binary_sensor.entity_state_tracker_01abc_compliant"
    )
    print("helpers self-check OK")
