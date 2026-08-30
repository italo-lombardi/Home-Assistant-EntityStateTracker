"""Tests for the pure helpers (§5.3, §16.2)."""

from __future__ import annotations

from custom_components.entity_state_tracker.const import FRAMES
from custom_components.entity_state_tracker.helpers import (
    _hsl_to_hex,
    frame_label,
    normalize_state,
    state_color,
    tracker_device_name,
    unique_id,
)


def test_normalize_state() -> None:
    """Case-fold and strip so user-typed states match recorded ones."""
    assert normalize_state("  Heat ") == "heat"
    assert normalize_state("AUTO") == "auto"
    assert normalize_state("off") == "off"


def test_state_color_deterministic() -> None:
    """Same state → same color across calls."""
    assert state_color("on") == state_color("on")
    assert state_color("heat") == state_color("heat")


def test_state_color_differs_between_states() -> None:
    """Different states get different colors."""
    assert state_color("on") != state_color("off")


def test_state_color_stable_when_new_state_added() -> None:
    """Adding a new state never recolors an existing one (hash-based, order-free)."""
    before = state_color("on")
    _ = state_color("some_brand_new_state")
    assert state_color("on") == before


def test_state_color_hex_format() -> None:
    """Output is a valid #rrggbb hex string."""
    color = state_color("heat")
    assert color.startswith("#")
    assert len(color) == 7
    assert all(c in "0123456789abcdef" for c in color[1:])


def test_state_color_grey_branch() -> None:
    """A state hashing near hue 0 still yields a legible non-crashing color.

    Exercises the general HSL path; both branches of _hsl_to_hex are reachable
    only via the internal s==0 guard, so we assert the public contract holds for
    a spread of inputs.
    """
    for state in ("a", "b", "c", "off", "on", "heat", "cool", "idle", "unavailable"):
        color = state_color(state)
        assert len(color) == 7


def test_hsl_to_hex_zero_saturation_is_grey() -> None:
    """The s==0 achromatic branch yields an r==g==b grey (not reachable via
    state_color, which fixes s=0.55, so call the converter directly)."""
    assert _hsl_to_hex(0.5, 0.0, 0.5) == "#808080"
    assert _hsl_to_hex(0.0, 0.0, 0.0) == "#000000"


def test_hsl_to_hex_lightness_gt_half_branch() -> None:
    """Chromatic branch with lightness >= 0.5 exercises the second q formula."""
    color = _hsl_to_hex(0.5, 0.55, 0.7)
    assert color.startswith("#")
    assert len(color) == 7


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
    assert tracker_device_name("Front Door", "all_states") == (
        "Entity State Tracker — Front Door"
    )
    assert tracker_device_name("Front Door", "specific_states") == (
        "Entity State Tracker — Front Door"
    )
