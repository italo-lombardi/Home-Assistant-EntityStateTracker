"""Tests for the pure helpers (§5.3, §16.2)."""

from __future__ import annotations

from custom_components.entity_state_tracker.const import FRAMES
from custom_components.entity_state_tracker.helpers import (
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
