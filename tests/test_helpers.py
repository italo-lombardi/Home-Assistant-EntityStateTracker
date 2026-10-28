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
    match_state,
    normalize_state,
    tracker_device_name,
    unique_id,
    zone_label_map,
)


def test_normalize_state() -> None:
    """Case-fold and strip so user-typed states match recorded ones."""
    assert normalize_state("  Heat ") == "heat"
    assert normalize_state("AUTO") == "auto"
    assert normalize_state("off") == "off"


def test_normalize_state_folds_separators() -> None:
    """Underscore↔space unify and whitespace runs collapse (the mismatch bug)."""
    # Config "city_centre" must match recorded "city centre".
    assert normalize_state("city_centre") == "city centre"
    assert normalize_state("city centre") == "city centre"
    assert normalize_state("not_home") == "not home"
    assert normalize_state("City  Centre") == "city centre"
    # NOT slugify: parens stay, so distinct states never merge.
    assert normalize_state("Shopping Centre (North)") == "shopping centre (north)"
    assert normalize_state("shopping centre (north)") != normalize_state(
        "shopping centre"
    )


def test_match_state_snaps_to_real() -> None:
    """match_state returns the real recorded string for a separator variant."""
    real = ["city centre", "not_home", "shopping centre (north)"]
    assert match_state("city_centre", real) == "city centre"
    assert match_state("NOT HOME", real) == "not_home"
    assert match_state("shopping centre (north)", real) == "shopping centre (north)"
    # No real state corresponds → None (keep the typed value as-is).
    assert match_state("somewhere_else", real) is None


def test_match_state_exact_wins_over_fold() -> None:
    """A candidate equal to a real state resolves to ITSELF, not a fold sibling.

    normalize_state is many-to-one ("not_home" and "not home" fold the same), so
    match_state must prefer an exact (case-insensitive) recorded key over any
    fold-equal one — else two genuinely-distinct recorded states get conflated.
    """
    collide = ["not_home", "not home"]
    assert match_state("not_home", collide) == "not_home"
    assert match_state("not home", collide) == "not home"
    assert match_state("NOT_HOME", collide) == "not_home"  # case-insensitive exact


def test_match_state_ambiguous_fold_returns_none() -> None:
    """A candidate matching 2+ reals only by fold (no exact) resolves to None.

    Honours the heal's "unambiguous only" contract — never silently pick one of
    several distinct recorded states that happen to fold together.
    """
    collide = ["not_home", "not home"]
    # "not__home" folds to "not home" like both reals, matches neither exactly.
    assert match_state("not__home", collide) is None
    # Single fold match still resolves (the real mismatch-fix case).
    assert match_state("city_centre", ["city centre"]) == "city centre"


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


def test_integration_prefix_not_doubled() -> None:
    """A label already carrying the integration name must not double it.

    Guards the reported bug: a title like "Entity State Tracker - Italo - All"
    yielded "Entity State Tracker — Entity State Tracker - …" as the device name
    and "sensor.entity_state_tracker_entity_state_tracker_…" as the entity_id.
    """
    label = "Entity State Tracker - Italo - All States"
    assert tracker_device_name(label) == "Entity State Tracker — Italo - All States"
    assert tracker_device_name("entity state tracker — Foo") == (
        "Entity State Tracker — Foo"
    )
    # A bare "Entity State Tracker" label is left intact (never emptied).
    assert tracker_device_name("Entity State Tracker") == (
        "Entity State Tracker — Entity State Tracker"
    )
    eid = frame_entity_id(label, "7d", TRANSLATION_KEY_DURATION)
    assert eid == "sensor.entity_state_tracker_italo_all_states_duration_last_7_days"
    assert "entity_state_tracker_entity_state_tracker" not in eid
    assert binary_entity_id(label, TRANSLATION_KEY_COMPLIANT) == (
        "binary_sensor.entity_state_tracker_italo_all_states_compliant"
    )


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


def test_zone_label_map_resolves_lowercased_key_to_real_name() -> None:
    """A lowercased stored key maps back to the zone's real friendly name."""
    zmap = zone_label_map(["Zone One (Suffix)", "Office HQ"])
    # Keyed by normalize_state, so the lowercased storage key resolves.
    assert zmap[normalize_state("zone one (suffix)")] == "Zone One (Suffix)"
    assert zmap[normalize_state("office hq")] == "Office HQ"


def test_zone_label_map_drops_ambiguous_fold() -> None:
    """Two+ zones folding to one key are all dropped — never guess."""
    # All three normalize identically; the 3rd exercises the already-seen arc.
    zmap = zone_label_map(["not home", "not_home", "NOT HOME"])
    assert normalize_state("not home") not in zmap
