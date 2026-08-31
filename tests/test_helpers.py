"""Tests for the pure helpers (§5.3, §16.2)."""

from __future__ import annotations

from custom_components.entity_state_tracker.const import (
    FRAMES,
    TRANSLATION_KEY_BREAKDOWN,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
    TRANSLATION_KEY_DURATION,
    TRANSLATION_KEY_PERCENT,
)
from custom_components.entity_state_tracker.helpers import (
    binary_entity_id,
    frame_entity_id,
    frame_label,
    normalize_state,
    tracker_device_name,
    unique_id,
)


def test_normalize_state() -> None:
    """Case-fold and strip so user-typed states match recorded ones."""
    assert normalize_state("  Heat ") == "heat"
    assert normalize_state("AUTO") == "auto"
    assert normalize_state("off") == "off"


def test_frame_label_known_calendar() -> None:
    """Calendar frames read as plain nouns."""
    assert frame_label("today") == "Today"
    assert frame_label("yesterday") == "Yesterday"
    assert frame_label("month") == "This month"
    assert frame_label("year") == "This year"
    assert frame_label("30d") == "Last 30 days"


def test_frame_label_rolling_spans() -> None:
    """Rolling frames name their span without a (rolling) tag."""
    assert frame_label("24h") == "Last 24 hours"
    assert frame_label("7d") == "Last 7 days"


def test_frame_label_all_frames_covered() -> None:
    """Every canonical frame has a label."""
    for frame in FRAMES:
        assert frame_label(frame)


def test_frame_label_unknown_raw() -> None:
    """Unknown key falls back to the raw key, un-prettified."""
    assert frame_label("mystery") == "mystery"


def test_unique_id() -> None:
    """unique_id is <entry_id>_<frame>_<metric>."""
    assert unique_id("abc", "today", "duration") == "abc_today_duration"
    assert unique_id("e1", "24h", "breakdown") == "e1_24h_breakdown"


def test_tracker_device_name() -> None:
    """Device name is mode-independent: Entity State Tracker — <label>."""
    assert tracker_device_name("Front Door") == ("Entity State Tracker — Front Door")


# --------------------------------------------------------------------------- #
# Pinned entity_ids — id == slugify(name), namespaced by the tracker NAME
# (entry.title), never the entry_id ULID. Frame token is the frame-label slug,
# last. The card discovers by device_id + translation_key (not the id string),
# so these ids are only the shipped DEFAULT and are freely renameable.
# --------------------------------------------------------------------------- #


def test_frame_entity_id_exact_shape() -> None:
    """Pinned frame entity_id is id==slug(name), metric slug, frame last."""
    assert (
        frame_entity_id("Front Door", "7d", TRANSLATION_KEY_DURATION)
        == "sensor.entity_state_tracker_front_door_duration_last_7_days"
    )
    assert (
        frame_entity_id("Front Door", "today", TRANSLATION_KEY_PERCENT)
        == "sensor.entity_state_tracker_front_door_in_a_tracked_state_percent_today"
    )
    assert (
        frame_entity_id("Front Door", "month", TRANSLATION_KEY_BREAKDOWN)
        == "sensor.entity_state_tracker_front_door_state_breakdown_this_month"
    )


def test_binary_entity_id_exact_shape() -> None:
    """Pinned binary entity_id shares the name stem, no frame token."""
    assert (
        binary_entity_id("Front Door", TRANSLATION_KEY_CURRENTLY_IN_STATE)
        == "binary_sensor.entity_state_tracker_front_door_in_a_tracked_state"
    )
    assert (
        binary_entity_id("Front Door", TRANSLATION_KEY_COMPLIANT)
        == "binary_sensor.entity_state_tracker_front_door_compliant"
    )


def test_entity_id_uses_name_not_entry_id() -> None:
    """Regression: an internal ULID must never leak into a public entity_id."""
    ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert ulid.lower() not in frame_entity_id(
        "Front Door", "today", TRANSLATION_KEY_DURATION
    )
    assert ulid.lower() not in binary_entity_id("Front Door", TRANSLATION_KEY_COMPLIANT)
