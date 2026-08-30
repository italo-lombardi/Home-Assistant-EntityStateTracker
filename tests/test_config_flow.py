"""Tests for the Entity State Tracker config + options flows — 100% line + branch."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_state_tracker.config_flow import (
    EntityStateTrackerConfigFlow,
    EntityStateTrackerOptionsFlow,
    _async_seen_states,
)
from custom_components.entity_state_tracker.const import (
    CONF_ENABLE_COMPLIANCE,
    CONF_ENTITY,
    CONF_FRAMES,
    CONF_MIN_STATE_DURATION,
    CONF_MODE,
    CONF_STATES,
    CONF_TARGET,
    CONF_TARGET_THRESHOLD,
    DOMAIN,
    FRAMES,
    MODE_ALL,
    MODE_SPECIFIC,
)

CF = "custom_components.entity_state_tracker.config_flow"
ENTITY = "climate.living_room"


# ---------------------------------------------------------------------------
# _async_seen_states — recorder prefill (the real function, not patched)
# ---------------------------------------------------------------------------


async def _run_sync(func: Any) -> Any:
    """Await helper: run the sync recorder job inline."""
    return func()


class _State:
    """Minimal state stand-in for recorder history rows."""

    def __init__(self, state: str) -> None:
        self.state = state


async def test_seen_states_current_plus_recorder(hass: HomeAssistant) -> None:
    """Current state (deduped, lowercased) merges with recorder distinct states."""
    hass.states.async_set(ENTITY, "Heat")

    def _fake_changes(*_a: Any, **_k: Any) -> dict[str, list[_State]]:
        return {ENTITY: [_State("heat"), _State("Auto"), _State("off")]}

    with (
        patch("homeassistant.components.recorder.get_instance") as mock_get_instance,
        patch(
            "homeassistant.components.recorder.history.state_changes_during_period",
            side_effect=_fake_changes,
        ),
    ):
        mock_get_instance.return_value.async_add_executor_job = _run_sync
        seen = await _async_seen_states(hass, ENTITY)

    # Current "Heat" first, then recorder states; deduped + lowercased.
    # "unknown" is always appended as a prefilled option.
    assert seen == ["heat", "auto", "off", "unknown"]


async def test_seen_states_skips_unavailable_current(hass: HomeAssistant) -> None:
    """An unavailable current state is not prefilled."""
    hass.states.async_set(ENTITY, STATE_UNAVAILABLE)

    with (
        patch("homeassistant.components.recorder.get_instance") as mock_get_instance,
        patch(
            "homeassistant.components.recorder.history.state_changes_during_period",
            return_value={ENTITY: [_State("on")]},
        ),
    ):
        mock_get_instance.return_value.async_add_executor_job = _run_sync
        seen = await _async_seen_states(hass, ENTITY)

    assert seen == ["on", "unknown"]


async def test_seen_states_recorder_off_degrades(hass: HomeAssistant) -> None:
    """Recorder absent → only the current state is returned (no crash)."""
    hass.states.async_set(ENTITY, "cool")

    with patch(
        "homeassistant.components.recorder.get_instance",
        side_effect=KeyError("recorder"),
    ):
        seen = await _async_seen_states(hass, ENTITY)

    assert seen == ["cool", "unknown"]


async def test_seen_states_no_current_state(hass: HomeAssistant) -> None:
    """No current state and recorder off → only the always-offered "unknown"."""
    with patch(
        "homeassistant.components.recorder.get_instance",
        side_effect=KeyError("recorder"),
    ):
        seen = await _async_seen_states(hass, "sensor.missing")

    assert seen == ["unknown"]


# ---------------------------------------------------------------------------
# Flow-driving helpers
# ---------------------------------------------------------------------------


def _patch_seen(states: list[str] | None = None) -> Any:
    """Patch state prefill so flow tests never touch the recorder."""
    return patch(
        f"{CF}._async_seen_states",
        return_value=states if states is not None else ["heat", "auto", "off"],
    )


async def _start_to_mode(
    hass: HomeAssistant, entity: str = ENTITY, name: str | None = None
) -> str:
    """Drive user → mode; return the mode-step flow id.

    ``name`` (when not None) is submitted in the optional step-1 name field.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    user_input: dict[str, Any] = {CONF_ENTITY: entity}
    if name is not None:
        user_input[CONF_NAME] = name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "mode"
    assert set(result["menu_options"]) == {MODE_SPECIFIC, MODE_ALL}
    return result["flow_id"]


def _frames_input(*enabled: str, **extra: Any) -> dict[str, Any]:
    """Frames-step submission: enabled frames True + optional tail overrides."""
    payload: dict[str, Any] = {frame: frame in enabled for frame in FRAMES}
    payload.setdefault(CONF_MIN_STATE_DURATION, 0)
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Specific-states leg
# ---------------------------------------------------------------------------


async def test_specific_no_compliance_creates_entry(hass: HomeAssistant) -> None:
    """Specific leg without compliance produces a frame-flag-dict entry."""
    flow_id = await _start_to_mode(hass)

    with _patch_seen():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
    assert result["step_id"] == "specific"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_STATES: ["Heat", "auto"], CONF_ENABLE_COMPLIANCE: False},
    )
    assert result["step_id"] == "frames"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _frames_input("today", "24h", **{CONF_MIN_STATE_DURATION: 30}),
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Entity State Tracker - Living Room - Specific States"
    data = result["data"]
    assert data[CONF_ENTITY] == ENTITY
    assert data[CONF_MODE] == MODE_SPECIFIC
    assert data[CONF_STATES] == ["heat", "auto"]  # lowercased, deduped
    # Frames stored as a {frame: bool} dict, only today+24h on.
    assert data[CONF_FRAMES]["today"] is True
    assert data[CONF_FRAMES]["24h"] is True
    assert data[CONF_FRAMES]["7d"] is False
    assert set(data[CONF_FRAMES]) == set(FRAMES)
    assert data[CONF_MIN_STATE_DURATION] == 30
    assert CONF_ENABLE_COMPLIANCE not in data  # no compliance configured
    # Transient _-keys stripped.
    assert not any(k.startswith("_") for k in data)
    # No derived unique_id — entries rely on their auto-generated entry_id.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id is None


async def test_specific_empty_states_error(hass: HomeAssistant) -> None:
    """Selecting no states re-shows the specific step with an error."""
    flow_id = await _start_to_mode(hass)
    with _patch_seen():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STATES: [], CONF_ENABLE_COMPLIANCE: False}
        )
    assert result["step_id"] == "specific"
    assert result["errors"] == {CONF_STATES: "no_states_selected"}


async def test_custom_name_sets_title_only(hass: HomeAssistant) -> None:
    """A custom step-1 name becomes the entry title but is not persisted to data.

    The name never touches entry data or the (absent) unique_id.
    """
    flow_id = await _start_to_mode(hass, name="My Heater")

    with _patch_seen():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STATES: ["heat", "auto"], CONF_ENABLE_COMPLIANCE: False},
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _frames_input("today")
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    # Custom name is the title, overriding the "Living Room" derived fallback.
    assert result["title"] == "My Heater"
    # Name is transient — only the title carries it; never lands in entry.data.
    assert CONF_NAME not in result["data"]
    assert "name" not in result["data"]
    assert not any(k.startswith("_") for k in result["data"])
    # No unique_id is set on the entry regardless of the custom name.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id is None


async def test_blank_name_falls_back_to_derived_title(hass: HomeAssistant) -> None:
    """A whitespace-only name is treated as unset → derived title fallback.

    All-states mode → the fallback carries the "All States" mode suffix.
    """
    flow_id = await _start_to_mode(hass, name="  ")

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": MODE_ALL}
    )
    assert result["step_id"] == "frames"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _frames_input("today")
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    # Blank name stripped to empty → falls back to prettified entity + mode.
    assert result["title"] == "Entity State Tracker - Living Room - All States"
    assert CONF_NAME not in result["data"]


async def test_specific_with_compliance_and_threshold(hass: HomeAssistant) -> None:
    """Compliance leg with a threshold writes target + threshold + flag."""
    flow_id = await _start_to_mode(hass)
    with _patch_seen():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STATES: ["heat", "auto"], CONF_ENABLE_COMPLIANCE: True},
        )
    assert result["step_id"] == "compliance"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TARGET: ["Heat"], CONF_TARGET_THRESHOLD: 80}
    )
    assert result["step_id"] == "frames"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _frames_input("today")
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_ENABLE_COMPLIANCE] is True
    assert data[CONF_TARGET] == ["heat"]  # lowercased
    assert data[CONF_TARGET_THRESHOLD] == 80


async def test_compliance_target_offers_seen_union(hass: HomeAssistant) -> None:
    """The compliance target selector offers the full seen-states union.

    Tracked states are unioned with the seen-states list (which may add states
    the tracker doesn't cover, plus ``unknown``) so the target can be ANY state,
    not just a subset of the tracked set. Order: tracked first, then seen.
    """
    flow_id = await _start_to_mode(hass)
    # Track only "heat"; seen-states additionally surfaces "off" and "unknown".
    with _patch_seen(["heat", "off", "unknown"]):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STATES: ["heat"], CONF_ENABLE_COMPLIANCE: True},
        )
    assert result["step_id"] == "compliance"

    schema = result["data_schema"].schema
    target_key = next(k for k in schema if str(k.schema) == CONF_TARGET)
    options = schema[target_key].config["options"]
    # Union of tracked (["heat"]) + seen (["heat", "off", "unknown"]), deduped.
    assert options == ["heat", "off", "unknown"]
    # Prefilled default is the tracked set only.
    assert target_key.default() == ["heat"]


async def test_compliance_non_tracked_target_lands_in_entry(
    hass: HomeAssistant,
) -> None:
    """A NON-tracked state can be chosen as the compliance target.

    Track ``on`` but score compliance on ``off`` — the target is independent of
    the tracked set and must land verbatim in the created entry's data.
    """
    flow_id = await _start_to_mode(hass)
    with _patch_seen(["on", "off", "unknown"]):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STATES: ["on"], CONF_ENABLE_COMPLIANCE: True},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TARGET: ["off"]}
        )
    assert result["step_id"] == "frames"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _frames_input("today")
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_STATES] == ["on"]  # tracked
    assert data[CONF_ENABLE_COMPLIANCE] is True
    assert data[CONF_TARGET] == ["off"]  # non-tracked target, independent


async def test_compliance_without_threshold(hass: HomeAssistant) -> None:
    """Compliance target with no threshold omits the threshold key."""
    flow_id = await _start_to_mode(hass)
    with _patch_seen():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STATES: ["heat"], CONF_ENABLE_COMPLIANCE: True},
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TARGET: ["heat"]}
    )
    assert result["step_id"] == "frames"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _frames_input("today")
    )
    data = result["data"]
    assert data[CONF_ENABLE_COMPLIANCE] is True
    assert data[CONF_TARGET] == ["heat"]
    assert CONF_TARGET_THRESHOLD not in data


async def test_compliance_empty_target_error(hass: HomeAssistant) -> None:
    """Empty target re-shows the compliance step with an error."""
    flow_id = await _start_to_mode(hass)
    with _patch_seen():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": MODE_SPECIFIC}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_STATES: ["heat"], CONF_ENABLE_COMPLIANCE: True},
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TARGET: []}
    )
    assert result["step_id"] == "compliance"
    assert result["errors"] == {CONF_TARGET: "no_target_selected"}


# ---------------------------------------------------------------------------
# All-states leg
# ---------------------------------------------------------------------------


async def test_all_states_leg_creates_entry(hass: HomeAssistant) -> None:
    """All-states leg skips state pick + compliance and goes straight to frames."""
    flow_id = await _start_to_mode(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": MODE_ALL}
    )
    assert result["step_id"] == "frames"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _frames_input("today", "7d")
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_MODE] == MODE_ALL
    assert CONF_STATES not in data
    assert CONF_ENABLE_COMPLIANCE not in data
    assert data[CONF_FRAMES]["7d"] is True
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    # No derived unique_id on an all-states entry either.
    assert entry.unique_id is None


async def test_frames_none_selected_error(hass: HomeAssistant) -> None:
    """Enabling zero frames re-shows the frames step with a base error."""
    flow_id = await _start_to_mode(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": MODE_ALL}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _frames_input(),  # all frames False
    )
    assert result["step_id"] == "frames"
    assert result["errors"] == {"base": "no_frames_selected"}


async def test_duplicate_entities_allowed(hass: HomeAssistant) -> None:
    """Two identical trackers on the same entity both create — no abort.

    Same entity + same mode + same states + blank name: the duplicate/unique_id
    guard is gone, so each reaches CREATE_ENTRY and both entries coexist.
    """

    async def _create_one() -> Any:
        flow_id = await _start_to_mode(hass)
        with _patch_seen():
            result = await hass.config_entries.flow.async_configure(
                flow_id, {"next_step_id": MODE_SPECIFIC}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_STATES: ["heat", "auto"], CONF_ENABLE_COMPLIANCE: False},
            )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], _frames_input("today")
        )

    first = await _create_one()
    second = await _create_one()

    assert first["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert second["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    # Both live side by side; neither has a derived unique_id.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert all(e.unique_id is None for e in entries)


# ---------------------------------------------------------------------------
# OptionsFlow
# ---------------------------------------------------------------------------


def _entry(**overrides: Any) -> MockConfigEntry:
    """Build a specific-mode entry with frames stored as a {frame: bool} dict."""
    data: dict[str, Any] = {
        CONF_ENTITY: ENTITY,
        CONF_MODE: MODE_SPECIFIC,
        CONF_STATES: ["heat", "auto"],
        CONF_FRAMES: {frame: frame in ("today", "24h") for frame in FRAMES},
        CONF_MIN_STATE_DURATION: 5,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, unique_id="opt_test")


def _all_states_entry() -> MockConfigEntry:
    """Build an all-states entry (no states, no compliance)."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTITY: "binary_sensor.front_door",
            CONF_MODE: MODE_ALL,
            CONF_FRAMES: {frame: frame == "today" for frame in FRAMES},
            CONF_MIN_STATE_DURATION: 0,
        },
        unique_id="opt_all",
    )


async def test_options_no_compliance_edit(hass: HomeAssistant) -> None:
    """Without compliance, options edits only frames + tail (no target field)."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    keys = {str(k.schema) for k in result["data_schema"].schema}
    assert CONF_TARGET not in keys
    assert CONF_MIN_STATE_DURATION in keys
    # notify_on_new_state was removed as an option (forced on) — never in the schema.
    assert "notify_on_new_state" not in keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _frames_input("today", "7d", **{CONF_MIN_STATE_DURATION: 30}),
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    opts = result["data"]
    assert opts[CONF_FRAMES]["7d"] is True
    assert opts[CONF_FRAMES]["24h"] is False
    assert opts[CONF_MIN_STATE_DURATION] == 30
    assert CONF_TARGET not in opts


async def test_options_all_states_no_compliance_fields(hass: HomeAssistant) -> None:
    """An all-states entry never exposes compliance fields and saves cleanly."""
    entry = _all_states_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k.schema) for k in result["data_schema"].schema}
    assert CONF_TARGET not in keys
    assert CONF_TARGET_THRESHOLD not in keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _frames_input("today")
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FRAMES]["today"] is True
    assert CONF_TARGET not in result["data"]


async def test_options_no_frames_error(hass: HomeAssistant) -> None:
    """Options submit with no frames re-shows init with a base error."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _frames_input(),  # all frames False
    )
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "no_frames_selected"}


async def test_options_compliance_fields_and_edit(hass: HomeAssistant) -> None:
    """With compliance enabled, target + threshold appear and are editable.

    The saved target (``off``) is not in the tracked set (``heat``/``auto``) yet
    must be a selectable option: the selector offers the union of tracked states
    and the currently-saved target, so a non-tracked target stays re-selectable.
    """
    entry = _entry(
        **{
            CONF_ENABLE_COMPLIANCE: True,
            CONF_TARGET: ["off"],
            CONF_TARGET_THRESHOLD: 80,
        }
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    keys = {str(k.schema) for k in schema}
    assert CONF_TARGET in keys
    assert CONF_TARGET_THRESHOLD in keys
    # Selector offers tracked (heat, auto) unioned with the saved target (off).
    target_key = next(k for k in schema if str(k.schema) == CONF_TARGET)
    assert schema[target_key].config["options"] == ["heat", "auto", "off"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _frames_input("today", **{CONF_TARGET: ["Auto"], CONF_TARGET_THRESHOLD: 90}),
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET] == ["auto"]  # lowercased
    assert result["data"][CONF_TARGET_THRESHOLD] == 90


async def test_options_compliance_no_threshold_default(hass: HomeAssistant) -> None:
    """Compliance entry without a stored threshold still renders + saves."""
    entry = _entry(**{CONF_ENABLE_COMPLIANCE: True, CONF_TARGET: ["heat"]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k.schema) for k in result["data_schema"].schema}
    assert CONF_TARGET_THRESHOLD in keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _frames_input("today", **{CONF_TARGET: ["heat"]})
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARGET] == ["heat"]
    assert CONF_TARGET_THRESHOLD not in result["data"]


async def test_options_compliance_empty_target_error(hass: HomeAssistant) -> None:
    """Empty target in options re-shows init with an error."""
    entry = _entry(**{CONF_ENABLE_COMPLIANCE: True, CONF_TARGET: ["heat"]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _frames_input("today", **{CONF_TARGET: []})
    )
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_TARGET: "no_target_selected"}


async def test_options_no_frames_and_no_target_both_error(
    hass: HomeAssistant,
) -> None:
    """No frames AND no target report both errors together."""
    entry = _entry(**{CONF_ENABLE_COMPLIANCE: True, CONF_TARGET: ["heat"]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _frames_input(**{CONF_TARGET: []}),  # no frames, no target
    )
    assert result["step_id"] == "init"
    assert result["errors"]["base"] == "no_frames_selected"
    assert result["errors"][CONF_TARGET] == "no_target_selected"


async def test_get_options_flow_returns_handler() -> None:
    """The static accessor returns an options-flow instance."""
    handler = EntityStateTrackerConfigFlow.async_get_options_flow(_entry())
    assert isinstance(handler, EntityStateTrackerOptionsFlow)
