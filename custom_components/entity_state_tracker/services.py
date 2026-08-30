"""Services for Entity State Tracker.

One service: ``entity_state_tracker.reset_ledger`` — clears the persisted
daily-bucket history (§9). It is destructive (long frames rebuild only from
whatever the recorder still retains), so it is guarded by a required ``confirm``
boolean: calling without ``confirm: true`` raises a
:class:`ServiceValidationError` rather than silently wiping history.

Targeting: an optional ``entity_id`` narrows the reset to the tracker(s) of that
tracked entity — so a multi-tracker user can wipe ONE tracker's history without
nuking the rest. ``entity_id`` here is the TRACKED entity (the ``entity`` a
tracker watches), not the tracker's own sensor: one tracked entity can have
several trackers (specific vs all-states, different names), and all of them
reset. Omit ``entity_id`` to reset EVERY loaded tracker (the original,
back-compatible behavior). Each entry owns its own store file, so the reset
walks the matching coordinators, resets each one's ledger, and refreshes it so
the sensors recompute from the now-empty history in the same tick.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_RESET_LEDGER
from .coordinator import EntityStateTrackerCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_CONFIRM = "confirm"
ATTR_ENTITY_ID = "entity_id"

RESET_LEDGER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM, default=False): cv.boolean,
        vol.Optional(ATTR_ENTITY_ID): vol.All(cv.ensure_list, [cv.entity_id]),
    }
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Entity State Tracker services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_LEDGER):
        return

    async def handle_reset_ledger(call: ServiceCall) -> None:
        """Clear the ledger for the targeted (or all) config entries."""
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="reset_not_confirmed",
            )

        # Optional target: the TRACKED entity_id(s). When omitted, every loaded
        # tracker resets (back-compat). When given, only coordinators watching
        # one of those entities reset — a multi-tracker user can wipe one
        # tracker's history without touching the others.
        targets: list[str] | None = call.data.get(ATTR_ENTITY_ID)
        coordinators = [
            coordinator
            for coordinator in hass.data.get(DOMAIN, {}).values()
            if isinstance(coordinator, EntityStateTrackerCoordinator)
            and (targets is None or coordinator.entity_id in targets)
        ]
        if targets is not None and not coordinators:
            # A target that matches no loaded tracker is a user error, not a
            # silent no-op — surface it so the caller notices the typo/wrong id.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="reset_no_match",
                translation_placeholders={"entity_id": ", ".join(targets)},
            )
        for coordinator in coordinators:
            await coordinator.async_reset_ledger()
            _LOGGER.info(
                "Reset ledger for %s (entry %s)",
                coordinator.entity_id,
                coordinator.entry.entry_id,
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_LEDGER,
        handle_reset_ledger,
        schema=RESET_LEDGER_SCHEMA,
    )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove services when the last config entry is unloaded."""
    hass.services.async_remove(DOMAIN, SERVICE_RESET_LEDGER)
