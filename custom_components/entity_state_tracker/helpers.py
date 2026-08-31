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
    TRANSLATION_KEY_COMPLIANCE,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
    TRANSLATION_KEY_DURATION,
    TRANSLATION_KEY_PERCENT,
)

# Human labels per frame. Plain nouns for calendar frames; the rolling frames
# name their span ("Last 24 hours"/"Last 7 days") without a "(rolling)" tag.
# "30d" is last-30-whole-days (not true rolling past retention — §6.4).
_FRAME_LABELS: dict[str, str] = {
    "today": "Today",
    "yesterday": "Yesterday",
    "24h": "Last 24 hours",
    "week": "This week",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "month": "This month",
    "year": "This year",
}


def _strip_integration_prefix(label: str) -> str:
    """Drop a leading "Entity State Tracker" from a user/derived label.

    The device name prepends "Entity State Tracker — " and the entity_id prepends
    the ``entity_state_tracker`` DOMAIN, so a label that ALREADY starts with the
    integration name doubles it (``Entity State Tracker — Entity State Tracker …``
    / ``sensor.entity_state_tracker_entity_state_tracker_…``). Strip it once, in a
    single place, so both paths stay clean regardless of what the user types.
    Case/separator-insensitive on the prefix; a bare label ("Entity State Tracker"
    alone) is left intact so we never produce an empty label.
    """
    stripped = label.strip()
    prefix = "entity state tracker"
    low = stripped.lower()
    if low.startswith(prefix):
        rest = stripped[len(prefix) :].lstrip(" -—–:")
        if rest:
            return rest
    return stripped


def tracker_device_name(entity_label: str) -> str:
    """Return the tracker's device name: ``Entity State Tracker — <label>`` (§5).

    The device name is mode-independent by design — one device per config entry
    regardless of mode — so it takes only the entity label. Any leading
    "Entity State Tracker" in the label is stripped first so the prefix is not
    doubled.
    """
    return f"Entity State Tracker — {_strip_integration_prefix(entity_label)}"


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


# Metric token → the English ENTITY NAME (base, without the "({frame})" suffix),
# mirroring translations/en.json. This is the SINGLE SOURCE for the entity_id
# slug: the default entity_id tail is slugify(name), so id == slugify(friendly
# name), every word, same order (see _METRIC_ENTITY_SLUG below). Keep these in
# lockstep with en.json — the __main__ self-check pins the expected slugs so a
# name edit that breaks the convention fails CI.
_METRIC_NAME_EN: dict[str, str] = {
    TRANSLATION_KEY_DURATION: "Duration",
    TRANSLATION_KEY_PERCENT: "In a Tracked State %",
    TRANSLATION_KEY_COMPLIANCE: "Compliance",
    TRANSLATION_KEY_BREAKDOWN: "State Breakdown",
    TRANSLATION_KEY_CURRENTLY_IN_STATE: "In a Tracked State",
    TRANSLATION_KEY_COMPLIANT: "Compliant",
}


# Metric token → the default entity_id slug, DERIVED from the English name so it
# can never silently drift from it (id == slugify(name), every word, same order).
# One transform rule: map "%" → "percent" first (HA slugify would otherwise DROP
# "%" entirely, losing the word), then slugify (lowercase, non-alphanumerics → "_",
# collapse runs). So "In a Tracked State %" → "in_a_tracked_state_percent". The
# entity_id is only the DEFAULT — a user may rename it freely; the card discovers
# by device_id + translation_key (frontend), never by this id string, so renames
# don't break the card. The __main__ self-check pins the expected slugs so a name
# edit that breaks the convention fails CI.
def _metric_slug(name: str) -> str:
    """Slug of an entity name for the default entity_id (id == slugify(name)).

    Maps "%" → "percent" before slugify so the percent sensor's id spells the
    word rather than dropping the glyph (HA ``slugify`` discards "%").
    """
    return slugify(name.replace("%", "percent"))


_METRIC_ENTITY_SLUG: dict[str, str] = {
    key: _metric_slug(name) for key, name in _METRIC_NAME_EN.items()
}


def _frame_label_slug(frame: str) -> str:
    """Slug of a frame's English label, for the default entity_id frame token.

    e.g. ``7d`` → ``last_7_days`` (slug of "Last 7 days"), so the id reads like the
    friendly name ("… (Last 7 days)" → ``…_last_7_days``). slugify: lowercase,
    non-alphanumerics → "_", collapse, trim.
    """
    return slugify(frame_label(frame))


def frame_entity_id(name: str, frame: str, metric: str) -> str:
    """Default entity_id for a per-frame sensor: ``id == slugify(name)``.

    ``sensor.entity_state_tracker_<name_slug>_<metric_slug>_<frame_label_slug>``
    — ``name`` is the tracker's user-visible name (``entry.title``), NOT the
    entry_id: an internal ULID must never leak into a public entity_id. metric_slug
    is slugify(English name) (see ``_METRIC_ENTITY_SLUG``) and the frame token is
    the frame LABEL slug ("Last 7 days" → ``last_7_days``), so the whole id reads
    as the slugified friendly name ("Duration (Last 7 days)" → ``duration_last_7_days``),
    every word, same order, frame last. This is only the DEFAULT — users may rename
    it; the card discovers by device_id + translation_key, not this id string.

    ponytail: two trackers sharing a title collide here; HA's registry auto-suffixes
    ``_2`` on insert, so a colliding pin is deduped safely — no guard needed.
    """
    return (
        f"sensor.{DOMAIN}_{slugify(_strip_integration_prefix(name))}_"
        f"{_METRIC_ENTITY_SLUG[metric]}_{_frame_label_slug(frame)}"
    )


def binary_entity_id(name: str, metric: str) -> str:
    """Default entity_id for a (frameless) binary sensor: ``id == slugify(name)``.

    ``binary_sensor.entity_state_tracker_<name_slug>_<metric_slug>`` — same
    namespacing as ``frame_entity_id`` minus the frame token (the binary sensors
    are not per-frame). ``name`` is ``entry.title`` (never the entry_id ULID). Only
    the DEFAULT; renameable, not depended on by the card.
    """
    return f"binary_sensor.{DOMAIN}_{slugify(_strip_integration_prefix(name))}_{_METRIC_ENTITY_SLUG[metric]}"


def binary_frame_entity_id(name: str, frame: str, metric: str) -> str:
    """Default entity_id for a PER-FRAME binary sensor: ``id == slugify(name)``.

    ``binary_sensor.entity_state_tracker_<name_slug>_<metric_slug>_<frame_label_slug>``
    — the ``frame_entity_id`` shape on the ``binary_sensor`` domain, used by the
    per-frame Compliant sensors (one per enabled frame). frame last, so the id
    reads as "Compliant (This month)" → ``compliant_this_month``.
    """
    return (
        f"binary_sensor.{DOMAIN}_{slugify(_strip_integration_prefix(name))}_"
        f"{_METRIC_ENTITY_SLUG[metric]}_{_frame_label_slug(frame)}"
    )


if __name__ == "__main__":  # pragma: no cover
    # Self-check: normalization, device name, and every canonical frame labels.
    assert normalize_state("  Heat ") == "heat"
    assert tracker_device_name("Front Door") == ("Entity State Tracker — Front Door")
    # Prefix-doubling guard: a label already carrying the integration name must not
    # double it in the device name or the entity_id slug (the reported bug).
    assert (
        _strip_integration_prefix("Entity State Tracker - Italo - All") == "Italo - All"
    )
    assert _strip_integration_prefix("entity state tracker — Foo") == "Foo"
    assert _strip_integration_prefix("Living Room") == "Living Room"
    assert _strip_integration_prefix("Entity State Tracker") == "Entity State Tracker"
    assert (
        tracker_device_name("Entity State Tracker - Italo - All States")
        == "Entity State Tracker — Italo - All States"
    )
    assert (
        frame_entity_id(
            "Entity State Tracker - Italo - All States",
            "7d",
            TRANSLATION_KEY_DURATION,
        )
        == "sensor.entity_state_tracker_italo_all_states_duration_last_7_days"
    )
    assert frame_label("24h") == "Last 24 hours"
    assert frame_label("today") == "Today"
    assert frame_label("mystery") == "mystery"
    assert unique_id("abc", "today", "duration") == "abc_today_duration"
    assert set(_FRAME_LABELS) == set(FRAMES)
    # Convention: the DEFAULT entity_id tail == slugify(name) (with "%"→"percent"),
    # every word, same order. Pin the expected slug per metric so a name edit that
    # breaks id==slug(name) fails here. _METRIC_NAME_EN must mirror en.json.
    assert set(_METRIC_NAME_EN) == set(_METRIC_ENTITY_SLUG)
    assert _METRIC_ENTITY_SLUG == {
        TRANSLATION_KEY_DURATION: "duration",
        TRANSLATION_KEY_PERCENT: "in_a_tracked_state_percent",
        TRANSLATION_KEY_COMPLIANCE: "compliance",
        TRANSLATION_KEY_BREAKDOWN: "state_breakdown",
        TRANSLATION_KEY_CURRENTLY_IN_STATE: "in_a_tracked_state",
        TRANSLATION_KEY_COMPLIANT: "compliant",
    }
    # Every slug is exactly slugify(name-with-%→percent) — the drift guard.
    for _k, _name in _METRIC_NAME_EN.items():
        assert _METRIC_ENTITY_SLUG[_k] == _metric_slug(_name)
    # Pinned entity_ids reproduce EXACTLY the id==slug(name) shape, namespaced by
    # the tracker NAME (entry.title) — never the entry_id ULID.
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
    assert (
        binary_entity_id("Front Door", TRANSLATION_KEY_COMPLIANT)
        == "binary_sensor.entity_state_tracker_front_door_compliant"
    )
    assert (
        binary_entity_id("Front Door", TRANSLATION_KEY_CURRENTLY_IN_STATE)
        == "binary_sensor.entity_state_tracker_front_door_in_a_tracked_state"
    )
    assert (
        binary_frame_entity_id("Front Door", "month", TRANSLATION_KEY_COMPLIANT)
        == "binary_sensor.entity_state_tracker_front_door_compliant_this_month"
    )
    print("helpers self-check OK")
