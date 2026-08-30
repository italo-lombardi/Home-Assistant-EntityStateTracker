"""Tests for the pure helpers (§5.3, §16.2)."""

from __future__ import annotations

from custom_components.entity_state_tracker.const import (
    FRAMES,
    TRANSLATION_KEY_BREAKDOWN,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
    TRANSLATION_KEY_DURATION,
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
# Pinned entity_ids — must reproduce EXACTLY what the card discovers.
#
# The card (frontend/entity-state-tracker-card.js) discovers sensors by:
#   DOMAIN_PREFIX  = "sensor.entity_state_tracker_"
#   METRIC_SLUGS   = ["state_breakdown", "duration"]   (slugified ENTITY NAMES)
#   FRAME_LABELS   slugified (e.g. "Last 7 days" -> "last_7_days")
# _matchFrame finds the trailing frame-label slug, stemOf strips
# _<metric>_<label_slug>, prettifyStem title-cases the device segment.
# The mirrors below port that exact JS so a drift in either side fails here.
# --------------------------------------------------------------------------- #

_DOMAIN_PREFIX = "sensor.entity_state_tracker_"
_CARD_FRAME_LABELS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "month": "This month",
    "year": "This year",
}
_CARD_METRIC_SLUGS = ["state_breakdown", "duration"]


def _card_slugify(text: str) -> str:
    """Port of the card's _slugify (lowercase, non-alnum -> _, trim)."""
    import re

    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", str(text).lower()))


# Longest-first, mirroring FRAME_LABEL_SLUGS in the card.
_CARD_FRAME_SLUGS = sorted(
    ((k, _card_slugify(v)) for k, v in _CARD_FRAME_LABELS.items()),
    key=lambda kv: len(kv[1]),
    reverse=True,
)


def _card_match_frame(object_id: str) -> tuple[str, str] | None:
    for key, slug in _CARD_FRAME_SLUGS:
        if object_id == slug or object_id.endswith(f"_{slug}"):
            return key, slug
    return None


def _card_stem_of(entity_id: str) -> str:
    m = _card_match_frame(entity_id)
    if not m:
        return entity_id
    _, slug = m
    stem = entity_id[: len(entity_id) - len(slug) - 1]
    for metric in _CARD_METRIC_SLUGS:
        if stem.endswith(f"_{metric}"):
            stem = stem[: len(stem) - len(metric) - 1]
            break
    return stem


def _card_prettify(stem: str) -> str:
    device = stem[len(_DOMAIN_PREFIX) :]
    if not device:
        return "Entity State Tracker"
    return " ".join(w[:1].upper() + w[1:] if w else w for w in device.split("_"))


def test_frame_entity_id_exact_shape() -> None:
    """Pinned frame entity_id is the exact slug the card keys on."""
    assert (
        frame_entity_id("01ABC", "7d", TRANSLATION_KEY_DURATION)
        == "sensor.entity_state_tracker_01abc_duration_last_7_days"
    )
    assert (
        frame_entity_id("01ABC", "month", TRANSLATION_KEY_BREAKDOWN)
        == "sensor.entity_state_tracker_01abc_state_breakdown_this_month"
    )


def test_binary_entity_id_exact_shape() -> None:
    """Pinned binary entity_id shares the tracker stem, no frame token."""
    assert (
        binary_entity_id("01ABC", TRANSLATION_KEY_CURRENTLY_IN_STATE)
        == "binary_sensor.entity_state_tracker_01abc_currently_in_state"
    )
    assert (
        binary_entity_id("01ABC", TRANSLATION_KEY_COMPLIANT)
        == "binary_sensor.entity_state_tracker_01abc_compliant"
    )


def test_pinned_frame_ids_round_trip_through_card_discovery() -> None:
    """Every frame/metric pin is discoverable + labelled by the card logic.

    Ports the card's _matchFrame/stemOf/prettifyStem and asserts: the DOMAIN
    prefix matches, the frame reverse-maps to the right key, all frames of one
    tracker collapse to ONE stem, and both metrics share that stem.
    """
    entry = "01ARZ3NDEKTSV4RRFFQ69G5FAV"  # a real-looking ULID entry_id
    stems: set[str] = set()
    for metric in (TRANSLATION_KEY_DURATION, TRANSLATION_KEY_BREAKDOWN):
        for frame in FRAMES:
            eid = frame_entity_id(entry, frame, metric)
            assert eid.startswith(_DOMAIN_PREFIX)
            m = _card_match_frame(eid)
            assert m is not None, eid
            assert m[0] == frame  # reverse-maps to the right frame KEY
            stems.add(_card_stem_of(eid))
    # All frames + both metrics of ONE tracker collapse to a single stem.
    assert len(stems) == 1
    # And the stem prettifies to a stable, non-empty device label.
    assert _card_prettify(next(iter(stems)))


def test_pinned_ids_distinct_per_entry_no_collision() -> None:
    """Two trackers (distinct entry_ids) yield distinct card stems (multi-tracker)."""
    a = _card_stem_of(frame_entity_id("entryAAA", "today", TRANSLATION_KEY_DURATION))
    b = _card_stem_of(frame_entity_id("entryBBB", "today", TRANSLATION_KEY_DURATION))
    assert a != b
