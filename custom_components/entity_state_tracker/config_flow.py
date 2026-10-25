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
from .helpers import match_state, normalize_state

_LOGGER = logging.getLogger(__name__)

# Domains whose state, while the entity is inside a zone, IS that zone's friendly
# name (plus the "home"/"not_home" literals). Only these get zone-aware resolution
# of a typed state — a climate/vacuum state ("cleaning") has no zone meaning.
_ZONE_STATE_DOMAINS = ("person", "device_tracker")

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


def _to_label(state: str) -> str:
    """Human-readable label for a raw state key — mirrors card toLabel().

    Capitalises the first letter of each whitespace-separated word only,
    so apostrophes ("st. john's") don't produce mid-word capitals.
    """
    return " ".join(w.capitalize() for w in state.replace("_", " ").split())


def _state_options(states: list[str]) -> list[dict[str, str]]:
    """Convert lowercase state list to [{value, label}] selector options."""
    return [{"value": s, "label": _to_label(s)} for s in states]


def _seen_states_schema(seen: list[str]) -> Any:
    """SelectSelector for tracked states, prefilled with seen states."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_state_options(seen),
            multiple=True,
            custom_value=True,
        )
    )


def _target_selector(options: list[str]) -> Any:
    """SelectSelector for the compliance target set."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_state_options(options),
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
    # For a person/device_tracker, "home" and "not_home" are the zone specials HA
    # emits (in zone.home / in no zone). They may not appear in recent history, so
    # always offer them — like the unavailable/unknown tail — deduped against the
    # real set. Zone friendly names the user wants are typed/resolved via zones.
    specials: list[str] = []
    if entity_id.split(".", 1)[0] in _ZONE_STATE_DOMAINS:
        specials = [s for s in ("home", "not_home") if s not in real]
    return [*real, *specials, STATE_UNAVAILABLE, STATE_UNKNOWN]


def _zone_state_map(hass: HomeAssistant) -> dict[str, str]:
    """Map every zone's identifiers → the state a tracker reports inside it.

    A ``person``/``device_tracker`` entity, while inside a zone, reports that
    zone's FRIENDLY NAME as its state (and the literal ``home`` for ``zone.home``,
    ``not_home`` when in no zone). So to turn a user-typed zone reference into the
    string the entity will actually emit, we map each zone's friendly name, its
    ``entity_id``, and its object_id (all canonicalised) → the emitted state.

    ``zone.home`` is special-cased to the ``home`` literal, not its friendly name
    ("Home"), because HA reports ``home`` (lowercase literal) for the home zone.
    """
    out: dict[str, str] = {}
    for state in hass.states.async_all("zone"):
        entity_id = state.entity_id  # "zone.dublin_city_center_usc"
        object_id = entity_id.split(".", 1)[1]
        friendly = state.attributes.get("friendly_name") or object_id
        emitted = "home" if entity_id == "zone.home" else friendly
        emitted_stored = normalize_state(emitted)
        # All the ways a user might refer to this zone → the emitted state.
        for key in (friendly, entity_id, object_id):
            out[normalize_state(key)] = emitted_stored
    return out


def _resolve_state(
    raw: str,
    seen: list[str],
    zone_map: dict[str, str],
) -> str:
    """Snap a user-selected/typed state to the string the entity actually emits.

    Order (ground truth first):

    1. ``match_state`` against ``seen`` — the recorder/live states the entity has
       really reported. A real recorded state always wins (its exact separators
       and case are authoritative).
    2. Zone map — only reached when the entity has never yet been in that zone
       (not in history). A typed zone friendly_name / ``zone.x`` id / object_id
       resolves to the state the entity WILL emit there.
    3. Fallback — the canonicalised typed value as-is. The runtime match (Layer 2)
       still catches it once the entity emits the matching form; the first-refresh
       heal re-snaps it later if a real match appears.

    Zone resolution is best-effort config-time convenience: if the zone is absent
    at config time, or its friendly_name is later renamed, the stored value can
    drift. Case/separator drift is covered by the runtime normalize + the heal;
    a full rename is not (same limitation as any label-based config).
    """
    real = match_state(raw, seen)
    if real is not None:
        return real
    zoned = zone_map.get(normalize_state(raw))
    if zoned is not None:
        return zoned
    return normalize_state(raw)


def _resolve_states(
    hass: HomeAssistant,
    source_entity_id: str,
    raw_states: list[str],
    seen: list[str],
) -> list[str]:
    """Resolve + dedup a list of user states against seen + (gated) zones.

    Zone resolution applies ONLY when the tracked entity is a ``person`` or
    ``device_tracker`` (the domains whose state is a zone name); every other
    domain just snaps to the seen-set / canonical form. Dedup is on the resolved
    values so two inputs that resolve to the same real state collapse to one. A
    value that canonicalises to empty (e.g. a stray ``"___"``) is dropped — a
    blank state name is never a real tracked state.
    """
    domain = source_entity_id.split(".", 1)[0]
    zone_map = _zone_state_map(hass) if domain in _ZONE_STATE_DOMAINS else {}
    resolved = [_resolve_state(s, seen, zone_map) for s in raw_states]
    return list(dict.fromkeys(s for s in resolved if s))


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
            raw = [s for s in user_input.get(CONF_STATES, []) if s.strip()]
            if not raw:
                errors[CONF_STATES] = "no_states_selected"
            else:
                # Snap each selected/typed state to the string the entity really
                # emits: a recorded state wins; else a typed zone reference
                # resolves to its emitted state (person/device_tracker only).
                entity_id = self._data[_KEY_ENTITY]
                seen = await _async_seen_states(self.hass, entity_id)
                self._data[_KEY_STATES] = _resolve_states(
                    self.hass, entity_id, raw, seen
                )
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
            raw = [s for s in user_input.get(CONF_TARGET, []) if s.strip()]
            threshold = user_input.get(CONF_TARGET_THRESHOLD)
            # The threshold selector already clamps to 0–100; only presence matters.
            if not raw:
                errors[CONF_TARGET] = "no_target_selected"
            else:
                # Target states have the same separator-mismatch class as tracked
                # states — snap them to real emitted states too.
                entity_id = self._data[_KEY_ENTITY]
                seen = await _async_seen_states(self.hass, entity_id)
                self._data[CONF_TARGET] = _resolve_states(
                    self.hass, entity_id, raw, seen
                )
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
                # distinguishable by title. No "Entity State Tracker" prefix —
                # the device already sits under that integration, so the prefix
                # is redundant noise. unique_id + entity_id slugs are unaffected
                # (they derive from entry_id / entity_id / mode).
                custom_name = self._data.get(_KEY_NAME)
                if custom_name:
                    title = custom_name
                else:
                    base = entity_id.split(".", 1)[-1].replace("_", " ").title()
                    kind = "All States" if mode == MODE_ALL else "Specific States"
                    title = f"{base} ({kind})"
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

    Editable: frames, min_state_duration, the tracked-states set (specific mode),
    and (when compliance is enabled) target / target_threshold. Entity and mode
    are fixed — changing those means a new tracker. Editing tracked states
    recomputes history retroactively (the ledger stores every state's duration;
    tracked_states is only a read-time filter).
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

            # Resolve edited states/target to real emitted states, same as the
            # create flow. Fetch the seen-set once if either block needs it.
            source_entity = current[CONF_ENTITY]
            seen = (
                await _async_seen_states(self.hass, source_entity)
                if (tracked or has_compliance)
                else []
            )

            if tracked:  # specific mode: tracked states are editable + required
                raw = [s for s in user_input.get(CONF_STATES, []) if s.strip()]
                if not raw:
                    errors[CONF_STATES] = "no_states_selected"
                else:
                    options[CONF_STATES] = _resolve_states(
                        self.hass, source_entity, raw, seen
                    )

            if has_compliance:
                raw_target = [s for s in user_input.get(CONF_TARGET, []) if s.strip()]
                threshold = user_input.get(CONF_TARGET_THRESHOLD)
                # The threshold selector already clamps to 0–100.
                if not raw_target:
                    errors[CONF_TARGET] = "no_target_selected"
                else:
                    options[CONF_TARGET] = _resolve_states(
                        self.hass, source_entity, raw_target, seen
                    )
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

        if tracked:  # specific mode — all-states entries have no tracked set.
            # Offer the current tracked set as options; custom_value=True still
            # lets the user type any state not currently tracked. Editing this
            # recomputes history retroactively (the ledger stores every state;
            # tracked_states is only a read-time filter).
            schema[vol.Optional(CONF_STATES, default=tracked)] = _seen_states_schema(
                tracked
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
