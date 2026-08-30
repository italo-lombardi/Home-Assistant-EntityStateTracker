"""Services for Entity State Tracker.

One service: ``entity_state_tracker.reset_ledger`` — clears the persisted
daily-bucket history (§9). It is destructive (long frames rebuild only from
whatever the recorder still retains), so it is guarded by a required ``confirm``
boolean: calling without ``confirm: true`` raises a
:class:`ServiceValidationError` rather than silently wiping history.

Scope (ponytail — simplest correct): the reset applies to every Entity State
Tracker config entry. Each entry owns its own store file, so we walk the loaded
coordinators and reset each one's ledger, then refresh it so the sensors
recompute from the now-empty history in the same tick.
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

RESET_LEDGER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIRM, default=False): cv.boolean,
    }
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Entity State Tracker services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_RESET_LEDGER):
        return

    async def handle_reset_ledger(call: ServiceCall) -> None:
        """Clear the ledger for every Entity State Tracker config entry."""
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="reset_not_confirmed",
            )

        coordinators = [
            coordinator
            for coordinator in hass.data.get(DOMAIN, {}).values()
            if isinstance(coordinator, EntityStateTrackerCoordinator)
        ]
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
