"""Config flow for Entity State Tracker integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_ENABLE_COMPLIANCE,
    CONF_ENTITY,
    CONF_FRAMES,
    CONF_MIN_STATE_DURATION,
    CONF_MODE,
    CONF_STATES,
    CONF_TARGET,
    CONF_TARGET_THRESHOLD,
    DEFAULT_FRAMES,
    DEFAULT_MIN_STATE_DURATION,
    DOMAIN,
    FRAMES,
    MODE_ALL,
    MODE_SPECIFIC,
)

_LOGGER = logging.getLogger(__name__)

# Cap on the RECORDER-derived distinct seen states offered as prefilled options.
# A numeric entity (e.g. an input_number cycling through hundreds of distinct
# float values) would otherwise flood the SelectSelector. The cap applies ONLY
# to the recorder-derived set — the current live state and the always-offered
# ``unavailable``/``unknown`` are added on top. custom_value=True still lets the
# user type any state omitted by the cap.
_SEEN_PREFILL_CAP = 50

# Transient config-flow keys (stripped before async_create_entry).
_KEY_ENTITY = f"_{CONF_ENTITY}"
_KEY_MODE = f"_{CONF_MODE}"
_KEY_STATES = f"_{CONF_STATES}"
_KEY_NAME = f"_{CONF_NAME}"


def _seen_states_schema(seen: list[str]) -> Any:
    """SelectSelector for tracked states, prefilled with seen states."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=seen,
            multiple=True,
            custom_value=True,
        )
    )


def _target_selector(options: list[str]) -> Any:
    """SelectSelector for the compliance target set."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            multiple=True,
            custom_value=True,
        )
    )


def _threshold_selector() -> Any:
    """Optional 0–100 percentage threshold."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, max=100, step=1, unit_of_measurement="%")
    )


def _min_duration_selector() -> Any:
    """Glitch-filter seconds (BOX so 0 stays 0)."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            step=1,
            unit_of_measurement="seconds",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _frames_schema(
    defaults: dict[str, bool], current: dict[str, Any]
) -> dict[Any, Any]:
    """Shared tail: one BooleanSelector per frame + min_duration.

    ``defaults`` maps each frame name to its on/off default; ``current`` supplies
    stored values (options-flow edit) and falls back to those defaults.
    """
    schema: dict[Any, Any] = {}
    for frame in FRAMES:
        schema[vol.Optional(frame, default=current.get(frame, defaults[frame]))] = (
            selector.BooleanSelector()
        )
    schema[
        vol.Optional(
            CONF_MIN_STATE_DURATION,
            default=current.get(CONF_MIN_STATE_DURATION, DEFAULT_MIN_STATE_DURATION),
        )
    ] = _min_duration_selector()
    return schema


def _split_frames(user_input: dict[str, Any]) -> tuple[dict[str, bool], dict[str, Any]]:
    """Split a frames-step submission into (frame-flag dict, tail settings).

    ``CONF_FRAMES`` is stored as ``{frame: bool}`` — the shape the coordinator
    consumes. ``enabled`` is falsy when no frame is on (guards the tail error).
    """
    flags = {frame: bool(user_input.get(frame)) for frame in FRAMES}
    tail = {
        CONF_MIN_STATE_DURATION: int(
            user_input.get(CONF_MIN_STATE_DURATION, DEFAULT_MIN_STATE_DURATION)
        ),
    }
    return flags, tail


async def _async_seen_states(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Best-effort list of states seen for an entity (current + recorder), lowercased.

    Recorder query runs on the recorder executor and degrades to just the current
    state if the recorder is unavailable. The returned list is:

    ``[real states (live + recorder-distinct) sorted alphabetically,
    unavailable, unknown]``

    — deduped, in that order. The recorder-derived distinct states are capped at
    ``_SEEN_PREFILL_CAP`` (most-recent kept) so a numeric entity with hundreds of
    distinct values can't flood the selector; the current live state is pulled
    out BEFORE the cap so it ALWAYS survives even when the recorder set exceeds
    the cap, then folded back into the alphabetical sort. ``unavailable`` and
    ``unknown`` are ALWAYS offered last (unavailable before unknown), on top of
    the cap, because entities routinely pass through them (startup, source
    outage) and tracking them is a common ask but they are filtered from the
    live/recorder scan. ``custom_value=True`` on the selector lets the user type
    any omitted state.
    """
    live: str | None = None
    state = hass.states.get(entity_id)
    if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        live = state.state.lower()

    from datetime import timedelta

    from homeassistant.components.recorder import get_instance, history
    from homeassistant.util import dt as dt_util

    now = dt_util.utcnow()
    start = now - timedelta(days=10)

    def _distinct() -> list[str]:
        changes = history.state_changes_during_period(
            hass, start, now, entity_id, no_attributes=True
        )
        return [s.state.lower() for s in changes.get(entity_id, [])]

    recorder: list[str] = []
    try:
        instance = get_instance(hass)  # raises if recorder not set up
        recorder = await instance.async_add_executor_job(_distinct)
    except Exception as err:  # noqa: BLE001 - recorder absent/failed: prefill is best-effort
        _LOGGER.debug("State prefill for %s skipped: %s", entity_id, err)

    # Cap the RECORDER-derived distinct set only — never the live state. Recorder
    # history arrives oldest-first, so a straight dedup keeps chronological order;
    # the cap trims from the FRONT (oldest) to keep the most-recent
    # ``_SEEN_PREFILL_CAP`` distinct states. Drop unavailable/unknown (and the
    # live state, re-prepended below) here: unavailable/unknown are ALWAYS
    # re-appended at the tail in a fixed order, so a recorder scan that surfaced
    # them must not pin them mid-list. Excluding the live state before the cap
    # guarantees the CURRENT state survives even for an entity with >cap distinct
    # recorder states (else the front-trim would drop the index-0 live state and
    # violate this function's ``[current live state, ...]`` contract).
    distinct = [
        s
        for s in dict.fromkeys(recorder)
        if s not in (STATE_UNAVAILABLE, STATE_UNKNOWN) and s != live
    ]
    if len(distinct) > _SEEN_PREFILL_CAP:
        distinct = distinct[-_SEEN_PREFILL_CAP:]

    # Offer the real states alphabetically (live + recorder-distinct, sorted),
    # then always offer "unavailable" then "unknown" (in that order) as the LAST
    # two options: entities routinely pass through both and they are filtered from
    # the current-state check above, so seed them explicitly. Sorting the real
    # states — rather than live-first, chronological — matches the card's tracked-
    # states display sort; unavailable/unknown stay pinned last regardless.
    head = [live] if live is not None else []
    real = sorted(dict.fromkeys([*head, *distinct]))
    return [*real, STATE_UNAVAILABLE, STATE_UNKNOWN]


class EntityStateTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Entity State Tracker."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick the entity to track."""
        if user_input is not None:
            self._data[_KEY_ENTITY] = user_input[CONF_ENTITY]
            name = (user_input.get(CONF_NAME) or "").strip()
            if name:
                self._data[_KEY_NAME] = name
            return await self.async_step_mode()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig()
                    ),
                    vol.Optional(CONF_NAME): selector.TextSelector(
                        selector.TextSelectorConfig()
                    ),
                }
            ),
        )

    async def async_step_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: the branch point — specific states or all states."""
        return self.async_show_menu(
            step_id="mode",
            menu_options=[MODE_SPECIFIC, MODE_ALL],
        )

    async def async_step_specific_states(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Specific leg entry — the menu routes here for the specific mode."""
        self._data[_KEY_MODE] = MODE_SPECIFIC
        return await self.async_step_specific(user_input)

    async def async_step_all_states(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """All-states leg — no state pick, no compliance; straight to the tail."""
        self._data[_KEY_MODE] = MODE_ALL
        return await self.async_step_frames()

    async def async_step_specific(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Specific leg: choose states, optionally enable compliance."""
        errors: dict[str, str] = {}

        if user_input is not None:
            states = [s.lower() for s in user_input.get(CONF_STATES, [])]
            if not states:
                errors[CONF_STATES] = "no_states_selected"
            else:
                self._data[_KEY_STATES] = list(dict.fromkeys(states))
                if user_input.get(CONF_ENABLE_COMPLIANCE):
                    return await self.async_step_compliance()
                return await self.async_step_frames()

        seen = await _async_seen_states(self.hass, self._data[_KEY_ENTITY])
        return self.async_show_form(
            step_id="specific",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_STATES, default=(user_input or {}).get(CONF_STATES, seen)
                    ): _seen_states_schema(seen),
                    vol.Optional(
                        CONF_ENABLE_COMPLIANCE,
                        default=(user_input or {}).get(CONF_ENABLE_COMPLIANCE, False),
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_compliance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Specific leg: declare the compliance target set + optional threshold."""
        errors: dict[str, str] = {}

        if user_input is not None:
            target = [s.lower() for s in user_input.get(CONF_TARGET, [])]
            threshold = user_input.get(CONF_TARGET_THRESHOLD)
            # The threshold selector already clamps to 0–100; only presence matters.
            if not target:
                errors[CONF_TARGET] = "no_target_selected"
            else:
                self._data[CONF_TARGET] = list(dict.fromkeys(target))
                if threshold is not None:
                    self._data[CONF_TARGET_THRESHOLD] = threshold
                return await self.async_step_frames()

        # The compliance target is independent of the tracked states: scoring
        # runs against the entity's full state breakdown, so a desired state you
        # do not otherwise track (e.g. `off`) is a valid target. Offer the full
        # seen-states list (recorder + current + `unknown`) unioned with the
        # tracked states — so tracked states always appear even if not in recent
        # history — and prefill with the tracked states as a convenience.
        tracked = self._data.get(_KEY_STATES, [])
        seen = await _async_seen_states(self.hass, self._data[_KEY_ENTITY])
        options = list(dict.fromkeys([*tracked, *seen]))
        return self.async_show_form(
            step_id="compliance",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TARGET, default=tracked): _target_selector(
                        options
                    ),
                    vol.Optional(CONF_TARGET_THRESHOLD): _threshold_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_frames(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Shared tail: enable frames and set the glitch filter."""
        errors: dict[str, str] = {}

        if user_input is not None:
            flags, tail = _split_frames(user_input)
            if not any(flags.values()):
                errors["base"] = "no_frames_selected"
            else:
                mode = self._data[_KEY_MODE]
                entity_id = self._data[_KEY_ENTITY]
                states = self._data.get(_KEY_STATES, [])

                # No unique_id / duplicate guard: multiple trackers on the same
                # entity are allowed (different states, different name, or just a
                # second independent tracker). Each config entry has its own
                # unique entry_id, which is what storage + DeviceInfo key on, so
                # duplicates never collide. HA disambiguates any repeated
                # entity_ids with a numeric suffix automatically.

                data: dict[str, Any] = {
                    CONF_ENTITY: entity_id,
                    CONF_MODE: mode,
                    CONF_FRAMES: flags,
                    **tail,
                }
                if mode == MODE_SPECIFIC:
                    data[CONF_STATES] = states
                    if CONF_TARGET in self._data:
                        data[CONF_ENABLE_COMPLIANCE] = True
                        data[CONF_TARGET] = self._data[CONF_TARGET]
                        if CONF_TARGET_THRESHOLD in self._data:
                            data[CONF_TARGET_THRESHOLD] = self._data[
                                CONF_TARGET_THRESHOLD
                            ]

                # Custom name from step 1 sets the config/device title; falls
                # back to the entity_id prettified + the tracker mode so two
                # trackers on the same entity (specific vs all-states) are
                # distinguishable by title. unique_id + entity_id slugs are
                # unaffected (they derive from entry_id / entity_id / mode).
                custom_name = self._data.get(_KEY_NAME)
                if custom_name:
                    title = custom_name
                else:
                    base = entity_id.split(".", 1)[-1].replace("_", " ").title()
                    kind = "All States" if mode == MODE_ALL else "Specific States"
                    title = f"Entity State Tracker - {base} - {kind}"
                return self.async_create_entry(title=title, data=data)

        defaults = {frame: frame in DEFAULT_FRAMES for frame in FRAMES}
        return self.async_show_form(
            step_id="frames",
            data_schema=vol.Schema(_frames_schema(defaults, user_input or {})),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> EntityStateTrackerOptionsFlow:
        """Get the options flow for this handler."""
        return EntityStateTrackerOptionsFlow()


class EntityStateTrackerOptionsFlow(OptionsFlow):
    """Handle options for Entity State Tracker — within-mode edits only.

    Editable: frames, min_state_duration, and (when compliance is enabled)
    target / target_threshold. Entity, mode and the tracked-states set are
    fixed — changing those means a new tracker.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        has_compliance = bool(current.get(CONF_ENABLE_COMPLIANCE))
        tracked = current.get(CONF_STATES, [])

        if user_input is not None:
            flags, tail = _split_frames(user_input)
            options: dict[str, Any] = {CONF_FRAMES: flags, **tail}

            if not any(flags.values()):
                errors["base"] = "no_frames_selected"

            if has_compliance:
                target = [s.lower() for s in user_input.get(CONF_TARGET, [])]
                threshold = user_input.get(CONF_TARGET_THRESHOLD)
                # The threshold selector already clamps to 0–100.
                if not target:
                    errors[CONF_TARGET] = "no_target_selected"
                else:
                    options[CONF_TARGET] = list(dict.fromkeys(target))
                    if threshold is not None:
                        options[CONF_TARGET_THRESHOLD] = threshold

            if not errors:
                return self.async_create_entry(title="", data=options)

        # Frame booleans default from what the tracker currently tracks; the
        # tail setting comes straight from stored data/options (falling back to
        # the shared default inside _frames_schema when absent).
        stored_frames = current.get(CONF_FRAMES, {})
        frame_current: dict[str, Any] = {
            frame: bool(stored_frames.get(frame)) for frame in FRAMES
        }
        frame_current[CONF_MIN_STATE_DURATION] = current.get(
            CONF_MIN_STATE_DURATION, DEFAULT_MIN_STATE_DURATION
        )
        schema: dict[Any, Any] = dict(
            _frames_schema(
                {frame: frame in DEFAULT_FRAMES for frame in FRAMES}, frame_current
            )
        )

        if has_compliance:
            # The saved target may include states that are not tracked (the
            # target is independent of the tracked set), so offer the union of
            # tracked states + the currently-saved target — guaranteeing every
            # saved target state is a selectable option. custom_value=True still
            # lets the user type any other state.
            target_options = list(
                dict.fromkeys([*tracked, *current.get(CONF_TARGET, [])])
            )
            schema[
                vol.Optional(CONF_TARGET, default=current.get(CONF_TARGET, tracked))
            ] = _target_selector(target_options)
            default_threshold = current.get(CONF_TARGET_THRESHOLD)
            threshold_key = (
                vol.Optional(CONF_TARGET_THRESHOLD, default=default_threshold)
                if default_threshold is not None
                else vol.Optional(CONF_TARGET_THRESHOLD)
            )
            schema[threshold_key] = _threshold_selector()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
