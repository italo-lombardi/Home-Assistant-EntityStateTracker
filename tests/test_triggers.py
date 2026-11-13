"""Tests for the named automation triggers (§5.4).

Two surfaces:

* ``_fire_frame_edges`` — the coordinator's per-frame verdict edge emitter. The
  A/B/C suppression invariants (no edge when the frame is absent from prior, no
  dominant edge into/out of None, no compliance edge on an unknown verdict) are
  the whole point, so they get the most assertions.
* ``trigger.py`` — the platform: slug registry, entry/frame scope filtering, and
  config validation (a dead config_entry_id must fail loud).

State names here are neutral placeholders — no real device/zone state names.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.const import CONF_OPTIONS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.trigger import TriggerConfig
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.entity_state_tracker import trigger as trigger_mod
from custom_components.entity_state_tracker.const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_FRAME,
    DOMAIN,
    EVENT_COMPLIANT_OFF,
    EVENT_COMPLIANT_ON,
    EVENT_DOMINANT_CHANGED,
    EVENT_NEW_STATE,
)
from custom_components.entity_state_tracker.coordinator import (
    EntityStateTrackerCoordinator,
)
from custom_components.entity_state_tracker.models import FrameResult


def _frame(dominant=None, compliance=None, pct=None, window_start=None) -> FrameResult:
    """Build a FrameResult carrying just the fields the edge emitter reads."""
    return FrameResult(
        window_seconds=3600.0,
        dominant=dominant,
        compliance_percent=compliance,
        breakdown_pct=pct or {},
        window_start=window_start,
    )


@pytest.fixture
async def coord(
    hass: HomeAssistant, compliance_config_data
) -> EntityStateTrackerCoordinator:
    """A specific-states coordinator with a compliance threshold set (threshold=80)."""
    entry = MockConfigEntry(domain=DOMAIN, data=compliance_config_data, title="Tracker")
    entry.add_to_hass(hass)
    return EntityStateTrackerCoordinator(hass, entry)


@pytest.fixture
async def all_states_coord(
    hass: HomeAssistant, all_states_config_data
) -> EntityStateTrackerCoordinator:
    """An all-states coordinator — the mode where `dominant` is surfaced/fires."""
    entry = MockConfigEntry(domain=DOMAIN, data=all_states_config_data, title="Tracker")
    entry.add_to_hass(hass)
    return EntityStateTrackerCoordinator(hass, entry)


# --- _fire_frame_edges: A/B/C invariants ---------------------------------


async def test_no_edge_when_frame_absent_from_prior(hass: HomeAssistant, coord) -> None:
    """Invariant A: prior is None (first tick / frame enabled mid-session) → silent."""
    dom = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    off = async_capture_events(hass, EVENT_COMPLIANT_OFF)

    coord._fire_frame_edges("today", None, _frame(dominant="alpha", compliance=90.0))
    await hass.async_block_till_done()

    assert dom == [] and on == [] and off == []


async def test_dominant_changed_fires_on_flip(
    hass: HomeAssistant, all_states_coord
) -> None:
    """A real alpha→beta flip fires exactly one dominant_changed with payload."""
    coord = all_states_coord
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)

    prior = _frame(dominant="alpha")
    new = _frame(dominant="beta", pct={"beta": 55.0})
    coord._fire_frame_edges("today", prior, new)
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["frame"] == "today"
    assert data["dominant"] == "beta"
    assert data["previous_dominant"] == "alpha"
    assert data["dominant_pct"] == 55.0
    assert data["entry_id"] == coord._entry_id


async def test_dominant_no_edge_when_unchanged(
    hass: HomeAssistant, all_states_coord
) -> None:
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    all_states_coord._fire_frame_edges(
        "today", _frame(dominant="alpha"), _frame(dominant="alpha")
    )
    await hass.async_block_till_done()
    assert events == []


async def test_dominant_no_edge_into_or_out_of_none(
    hass: HomeAssistant, all_states_coord
) -> None:
    """Invariant B: None dominant (count==0) is not a meaningful flip either way."""
    coord = all_states_coord
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    # prior had a value, new has none (no data this tick) — suppress.
    coord._fire_frame_edges("today", _frame(dominant="alpha"), _frame(dominant=None))
    # prior had none, new gained one — also suppress (first real data isn't a flip).
    coord._fire_frame_edges("today", _frame(dominant=None), _frame(dominant="alpha"))
    await hass.async_block_till_done()
    assert events == []


async def test_dominant_no_edge_in_specific_mode(hass: HomeAssistant, coord) -> None:
    """Dominant edge is gated to all-states mode — a specific-mode tracker never
    surfaces `dominant` as an entity, so a real max-duration flip must NOT fire.

    `coord` is a specific-states tracker; a clean alpha→beta flip that WOULD fire
    in all-states mode is suppressed here by the mode gate.
    """
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    coord._fire_frame_edges(
        "today", _frame(dominant="alpha"), _frame(dominant="beta", pct={"beta": 55.0})
    )
    await hass.async_block_till_done()
    assert events == []


async def test_compliant_on_fires_on_cross_up(hass: HomeAssistant, coord) -> None:
    """Below→at/above threshold fires compliant_on (threshold=80 from fixture)."""
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    off = async_capture_events(hass, EVENT_COMPLIANT_OFF)

    coord._fire_frame_edges("today", _frame(compliance=70.0), _frame(compliance=85.0))
    await hass.async_block_till_done()

    assert len(on) == 1 and off == []
    assert on[0].data["compliant"] is True
    assert on[0].data["compliance_percent"] == 85.0
    assert on[0].data["threshold"] == coord.target_threshold
    assert on[0].data["frame"] == "today"


async def test_compliant_off_fires_on_cross_down(hass: HomeAssistant, coord) -> None:
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    off = async_capture_events(hass, EVENT_COMPLIANT_OFF)
    coord._fire_frame_edges("today", _frame(compliance=90.0), _frame(compliance=50.0))
    await hass.async_block_till_done()
    assert off and not on
    assert off[0].data["compliant"] is False


async def test_compliant_no_edge_on_unknown_verdict(hass: HomeAssistant, coord) -> None:
    """Invariant C: a None compliance_percent is 'unknown', not an edge."""
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    off = async_capture_events(hass, EVENT_COMPLIANT_OFF)
    # prior known-compliant, new unknown (N/A window) → no edge.
    coord._fire_frame_edges("today", _frame(compliance=90.0), _frame(compliance=None))
    # prior unknown, new known → no edge.
    coord._fire_frame_edges("today", _frame(compliance=None), _frame(compliance=85.0))
    await hass.async_block_till_done()
    assert on == [] and off == []


async def test_compliant_no_edge_when_stays_same_side(
    hass: HomeAssistant, coord
) -> None:
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    off = async_capture_events(hass, EVENT_COMPLIANT_OFF)
    coord._fire_frame_edges("today", _frame(compliance=85.0), _frame(compliance=95.0))
    await hass.async_block_till_done()
    assert on == [] and off == []


async def test_no_compliant_edge_without_threshold(
    hass: HomeAssistant, specific_config_data
) -> None:
    """With no threshold configured, compliance is always unknown → no edge."""
    entry = MockConfigEntry(domain=DOMAIN, data=specific_config_data, title="T")
    entry.add_to_hass(hass)
    c = EntityStateTrackerCoordinator(hass, entry)
    assert c.target_threshold is None
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    c._fire_frame_edges("today", _frame(compliance=10.0), _frame(compliance=99.0))
    await hass.async_block_till_done()
    assert on == []


# --- _fire_frame_edges: Invariant D (calendar window reset) ---------------
# Regression: a `today` frame resets its window at local midnight. An entity
# whose open visit straddles midnight carries into the new day as its dominant,
# which — compared against the OLD day's dominant — fired a phantom
# dominant_changed for an entity that never physically changed state. Observed
# 2026-11-13 00:00 on a prod PIR switch (off→on phantom). Invariant D suppresses
# EVERY edge when window_start advances between ticks (disjoint windows).


async def test_no_dominant_edge_when_calendar_window_reset(
    hass: HomeAssistant, all_states_coord
) -> None:
    """Phantom dominant flip across a `today` midnight reset is suppressed.

    prior (old day) dominant=off, result (new day, open visit carried) dominant=on
    with a DIFFERENT window_start → the apparent off→on is a window reset, not a
    real flip. No dominant_changed must fire.
    """
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    prior = _frame(dominant="off", window_start="2026-11-12T00:00:00+00:00")
    new = _frame(
        dominant="on",
        pct={"on": 100.0},
        window_start="2026-11-13T00:00:00+00:00",
    )
    all_states_coord._fire_frame_edges("today", prior, new)
    await hass.async_block_till_done()
    assert events == []


async def test_no_compliant_edge_when_calendar_window_reset(
    hass: HomeAssistant, coord
) -> None:
    """A compliance verdict flip across a calendar reset is also suppressed."""
    on = async_capture_events(hass, EVENT_COMPLIANT_ON)
    off = async_capture_events(hass, EVENT_COMPLIANT_OFF)
    # prior (old day) below threshold, new (reset day) above — not a real cross.
    coord._fire_frame_edges(
        "today",
        _frame(compliance=50.0, window_start="2026-11-12T00:00:00+00:00"),
        _frame(compliance=95.0, window_start="2026-11-13T00:00:00+00:00"),
    )
    await hass.async_block_till_done()
    assert on == [] and off == []


async def test_dominant_edge_fires_within_same_calendar_window(
    hass: HomeAssistant, all_states_coord
) -> None:
    """Invariant D must NOT swallow a real flip when window_start is unchanged.

    Same window_start on both ticks = same day → a genuine off→on dominant flip
    still fires. Guards against the suppression being too broad.
    """
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    ws = "2026-11-13T00:00:00+00:00"
    all_states_coord._fire_frame_edges(
        "today",
        _frame(dominant="off", window_start=ws),
        _frame(dominant="on", pct={"on": 60.0}, window_start=ws),
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["dominant"] == "on"
    assert events[0].data["previous_dominant"] == "off"


async def test_rolling_frame_edge_fires_despite_window_start_drift(
    hass: HomeAssistant, all_states_coord
) -> None:
    """A rolling frame (7d) slides window_start every tick but never resets to a
    disjoint window — its real flips must still fire (guard keys on calendar
    frames only, so a per-tick start drift does not suppress 7d).
    """
    events = async_capture_events(hass, EVENT_DOMINANT_CHANGED)
    all_states_coord._fire_frame_edges(
        "7d",
        _frame(dominant="off", window_start="2026-11-06T09:00:00+00:00"),
        _frame(
            dominant="on",
            pct={"on": 51.0},
            window_start="2026-11-06T09:01:00+00:00",
        ),
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["dominant"] == "on"


# --- trigger.py platform --------------------------------------------------


async def test_async_get_triggers_returns_all_slugs(hass: HomeAssistant) -> None:
    triggers = await trigger_mod.async_get_triggers(hass)
    assert set(triggers) == {
        "new_state",
        "dominant_changed",
        "compliant_on",
        "compliant_off",
    }


async def test_new_state_trigger_has_no_frame_scope() -> None:
    assert trigger_mod._NewStateTrigger._supports_frame is False
    assert trigger_mod._DominantChangedTrigger._supports_frame is True


async def test_validate_config_rejects_unknown_entry(hass: HomeAssistant) -> None:
    with pytest.raises(vol.Invalid):
        await trigger_mod._DominantChangedTrigger.async_validate_config(
            hass, {CONF_OPTIONS: {CONF_CONFIG_ENTRY_ID: "does-not-exist"}}
        )


async def test_validate_config_accepts_own_entry(
    hass: HomeAssistant, compliance_config_data
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=compliance_config_data, title="T")
    entry.add_to_hass(hass)
    out = await trigger_mod._DominantChangedTrigger.async_validate_config(
        hass, {CONF_OPTIONS: {CONF_CONFIG_ENTRY_ID: entry.entry_id}}
    )
    assert out[CONF_OPTIONS][CONF_CONFIG_ENTRY_ID] == entry.entry_id


async def test_validate_config_frame_only_no_entry(hass: HomeAssistant) -> None:
    """Options with only a frame (no entry_id) validate without an entry lookup."""
    out = await trigger_mod._DominantChangedTrigger.async_validate_config(
        hass, {CONF_OPTIONS: {CONF_FRAME: "today"}}
    )
    assert out[CONF_OPTIONS][CONF_FRAME] == "today"


async def test_validate_config_empty_options(hass: HomeAssistant) -> None:
    """No scope at all → empty validated config (options key omitted)."""
    out = await trigger_mod._NewStateTrigger.async_validate_config(hass, {})
    assert out == {}


async def test_validate_complete_config_moves_top_level_fields(
    hass: HomeAssistant, compliance_config_data
) -> None:
    """Editor writes config_entry_id at top level; it must land under options."""
    entry = MockConfigEntry(domain=DOMAIN, data=compliance_config_data, title="T")
    entry.add_to_hass(hass)
    out = await trigger_mod._DominantChangedTrigger.async_validate_complete_config(
        hass,
        {
            "platform": f"{DOMAIN}.dominant_changed",
            CONF_CONFIG_ENTRY_ID: entry.entry_id,
            CONF_FRAME: "today",
        },
    )
    assert out[CONF_OPTIONS][CONF_CONFIG_ENTRY_ID] == entry.entry_id
    assert out[CONF_OPTIONS][CONF_FRAME] == "today"


async def test_attached_trigger_relays_and_scopes_by_frame(
    hass: HomeAssistant,
) -> None:
    """A frame-scoped trigger relays a matching event and drops a mismatched one."""
    fired: list[dict] = []

    def run_action(run_vars, description, context=None):
        fired.append(run_vars)

    cfg = TriggerConfig(
        key="dominant_changed", target=None, options={CONF_FRAME: "today"}
    )
    trig = trigger_mod._DominantChangedTrigger(hass, cfg)
    unsub = await trig.async_attach_runner(run_action)

    hass.bus.async_fire(EVENT_DOMINANT_CHANGED, {"entry_id": "x", "frame": "week"})
    await hass.async_block_till_done()
    assert fired == []  # wrong frame dropped

    hass.bus.async_fire(EVENT_DOMINANT_CHANGED, {"entry_id": "x", "frame": "today"})
    await hass.async_block_till_done()
    assert len(fired) == 1 and fired[0]["frame"] == "today"

    unsub()


async def test_attached_trigger_scopes_by_entry(hass: HomeAssistant) -> None:
    fired: list[dict] = []
    cfg = TriggerConfig(
        key="new_state", target=None, options={CONF_CONFIG_ENTRY_ID: "wanted"}
    )
    trig = trigger_mod._NewStateTrigger(hass, cfg)
    unsub = await trig.async_attach_runner(lambda rv, d, context=None: fired.append(rv))

    hass.bus.async_fire(EVENT_NEW_STATE, {"entry_id": "other", "state": "s"})
    await hass.async_block_till_done()
    assert fired == []

    hass.bus.async_fire(EVENT_NEW_STATE, {"entry_id": "wanted", "state": "s"})
    await hass.async_block_till_done()
    assert len(fired) == 1

    unsub()


async def test_attached_trigger_blank_scope_fires_for_any(hass: HomeAssistant) -> None:
    """Default (no scope) relays every event — the empty-selector match-all path."""
    fired: list[dict] = []
    cfg = TriggerConfig(key="dominant_changed", target=None, options=None)
    trig = trigger_mod._DominantChangedTrigger(hass, cfg)
    assert trig._entry_id is None and trig._frame is None
    unsub = await trig.async_attach_runner(lambda rv, d, context=None: fired.append(rv))

    hass.bus.async_fire(EVENT_DOMINANT_CHANGED, {"entry_id": "a", "frame": "today"})
    hass.bus.async_fire(EVENT_DOMINANT_CHANGED, {"entry_id": "b", "frame": "week"})
    await hass.async_block_till_done()
    assert len(fired) == 2  # neither entry nor frame filtered

    unsub()
