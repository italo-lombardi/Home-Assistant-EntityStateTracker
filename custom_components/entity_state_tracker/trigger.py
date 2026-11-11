"""Named automation triggers for Entity State Tracker (§5.4).

Surfaces the integration's bus events as UI-discoverable automation triggers so
a user picks "Dominant state changed" / "Compliant turned on" / … in the
automation editor instead of typing a raw ``entity_state_tracker_*`` event
string. Mirrors the sibling WashWise pattern and HA core's ``moon`` example.

Requires the HA class-based Trigger platform (``async_attach_runner`` surface),
which landed in HA Core 2025.11.0 — the integration's enforced min version
(``hacs.json``). HA auto-discovers this module and ``triggers.yaml`` by presence;
there is no registration step in ``__init__.py``.

Each trigger is a thin relay over a bus event the coordinator already fires: it
subscribes, filters by the optional entry/frame scope carried in the event
payload, and forwards the payload verbatim to the automation as trigger
variables. Events always carry ``entry_id`` (and, for the per-frame events, a
``frame``), so scoping is a direct dict compare — no unique_id resolution.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import override

import voluptuous as vol
from homeassistant.const import CONF_OPTIONS
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.automation import move_top_level_schema_fields_to_options
from homeassistant.helpers.trigger import Trigger, TriggerActionRunner, TriggerConfig
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_FRAME,
    DOMAIN,
    EVENT_COMPLIANT_OFF,
    EVENT_COMPLIANT_ON,
    EVENT_DOMINANT_CHANGED,
    EVENT_NEW_STATE,
)

# Scope options. A per-frame trigger accepts both config_entry_id and frame; the
# new_state trigger is entity-level (its event has no frame) so it accepts only
# config_entry_id. Both optional — blank = fire for any entry/frame.
_SCOPED_OPTIONS = {
    vol.Optional(CONF_CONFIG_ENTRY_ID): str,
    vol.Optional(CONF_FRAME): str,
}
_ENTRY_ONLY_OPTIONS = {vol.Optional(CONF_CONFIG_ENTRY_ID): str}


class _EventTrigger(Trigger):
    """Base: relay one bus event to the automation, filtered by entry/frame scope."""

    _event_type: str
    _description: str
    # Frame scoping is only meaningful for the per-frame events; the new_state
    # subclass sets this False so its editor/validation omits the frame field.
    _supports_frame: bool = True

    @classmethod
    def _options_schema(cls) -> dict:
        return _SCOPED_OPTIONS if cls._supports_frame else _ENTRY_ONLY_OPTIONS

    @override
    @classmethod
    async def async_validate_complete_config(
        cls, hass: HomeAssistant, complete_config: ConfigType
    ) -> ConfigType:
        """Move editor-written top-level scope fields under ``options``.

        The automation editor and YAML write ``config_entry_id``/``frame`` at the
        top level, but the class-based base expects them under ``options`` (the
        zwave_js precedent). Normalise before per-field validation.
        """
        complete_config = move_top_level_schema_fields_to_options(
            complete_config, cls._options_schema()
        )
        return await super().async_validate_complete_config(hass, complete_config)

    @override
    @classmethod
    async def async_validate_config(
        cls, hass: HomeAssistant, config: ConfigType
    ) -> ConfigType:
        """Validate the scope options; fail loud on a dead entry reference.

        A ``config_entry_id`` that does not resolve to one of THIS integration's
        entries is rejected rather than silently never firing (a dead scope is
        worse than an error — the user gets no feedback otherwise).
        """
        options = vol.Schema(cls._options_schema())(config.get(CONF_OPTIONS) or {})
        entry_id = options.get(CONF_CONFIG_ENTRY_ID)
        if entry_id is not None:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                raise vol.Invalid(
                    f"'{entry_id}' is not an Entity State Tracker config entry"
                )
        return {CONF_OPTIONS: options} if options else {}

    @override
    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        super().__init__(hass, config)
        options = config.options or {}
        self._entry_id: str | None = options.get(CONF_CONFIG_ENTRY_ID)
        self._frame: str | None = (
            options.get(CONF_FRAME) if self._supports_frame else None
        )

    @override
    async def async_attach_runner(
        self,
        run_action: TriggerActionRunner,
        _did_not_trigger: Callable[..., None] | None = None,
    ) -> CALLBACK_TYPE:
        @callback
        def _handle(event: Event) -> None:
            if self._entry_id and event.data.get("entry_id") != self._entry_id:
                return
            if self._frame and event.data.get("frame") != self._frame:
                return
            run_action(dict(event.data), self._description, event.context)

        return self._hass.bus.async_listen(self._event_type, _handle)


class _NewStateTrigger(_EventTrigger):
    _event_type = EVENT_NEW_STATE
    _description = "new state seen"
    _supports_frame = False


class _DominantChangedTrigger(_EventTrigger):
    _event_type = EVENT_DOMINANT_CHANGED
    _description = "dominant state changed"


class _CompliantOnTrigger(_EventTrigger):
    _event_type = EVENT_COMPLIANT_ON
    _description = "compliant turned on"


class _CompliantOffTrigger(_EventTrigger):
    _event_type = EVENT_COMPLIANT_OFF
    _description = "compliant turned off"


_TRIGGERS: dict[str, type[Trigger]] = {
    "new_state": _NewStateTrigger,
    "dominant_changed": _DominantChangedTrigger,
    "compliant_on": _CompliantOnTrigger,
    "compliant_off": _CompliantOffTrigger,
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return the named triggers provided by Entity State Tracker."""
    return dict(_TRIGGERS)
