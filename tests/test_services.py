"""Tests for Entity State Tracker services (§9)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_state_tracker.const import DOMAIN, SERVICE_RESET_LEDGER
from custom_components.entity_state_tracker.coordinator import (
    EntityStateTrackerCoordinator,
)
from custom_components.entity_state_tracker.services import (
    ATTR_CONFIRM,
    ATTR_ENTITY_ID,
    async_setup_services,
    async_unload_services,
)


def _make_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> EntityStateTrackerCoordinator:
    """Build a coordinator with its ledger-reset entry point stubbed.

    The service delegates the whole reset (store wipe + in-memory ledger rebuild
    + refresh) to ``coordinator.async_reset_ledger``; stub that single seam.
    """
    entry.add_to_hass(hass)
    coordinator = EntityStateTrackerCoordinator(hass, entry)
    coordinator.async_reset_ledger = AsyncMock()  # type: ignore[method-assign]
    return coordinator


@pytest.fixture
async def setup_services(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> tuple[HomeAssistant, EntityStateTrackerCoordinator]:
    """Register the service with one coordinator loaded in hass.data."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator
    await async_setup_services(hass)
    return hass, coordinator


async def test_reset_ledger_without_confirm_raises(setup_services) -> None:
    """Calling without confirm raises ServiceValidationError and resets nothing."""
    hass, coordinator = setup_services

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_LEDGER,
            {ATTR_CONFIRM: False},
            blocking=True,
        )

    coordinator.async_reset_ledger.assert_not_awaited()


async def test_reset_ledger_default_confirm_false_raises(setup_services) -> None:
    """Omitting confirm defaults to False and raises (schema default path)."""
    hass, coordinator = setup_services

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_LEDGER,
            {},
            blocking=True,
        )

    coordinator.async_reset_ledger.assert_not_awaited()


async def test_reset_ledger_confirm_resets_and_refreshes(setup_services) -> None:
    """confirm=True resets each coordinator's ledger and requests a refresh."""
    hass, coordinator = setup_services

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_LEDGER,
        {ATTR_CONFIRM: True},
        blocking=True,
    )

    coordinator.async_reset_ledger.assert_awaited_once()


async def test_reset_ledger_iterates_all_coordinators(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """The reset walks every loaded coordinator, skipping non-coordinator values."""
    coord_a = _make_coordinator(hass, specific_config_entry)
    coord_b = _make_coordinator(hass, all_states_config_entry)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][specific_config_entry.entry_id] = coord_a
    hass.data[DOMAIN][all_states_config_entry.entry_id] = coord_b
    # A non-coordinator sentinel (e.g. the card-installed flag) must be skipped.
    hass.data[DOMAIN]["_card_installed"] = "0.1.0"

    await async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_LEDGER,
        {ATTR_CONFIRM: True},
        blocking=True,
    )

    coord_a.async_reset_ledger.assert_awaited_once()
    coord_b.async_reset_ledger.assert_awaited_once()


async def test_reset_ledger_targets_one_entity(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """A targeted reset resets only the tracker(s) watching that entity."""
    coord_a = _make_coordinator(hass, specific_config_entry)  # climate.living_room
    coord_b = _make_coordinator(
        hass, all_states_config_entry
    )  # binary_sensor.front_door
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][specific_config_entry.entry_id] = coord_a
    hass.data[DOMAIN][all_states_config_entry.entry_id] = coord_b

    await async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_LEDGER,
        {ATTR_CONFIRM: True, ATTR_ENTITY_ID: "climate.living_room"},
        blocking=True,
    )

    coord_a.async_reset_ledger.assert_awaited_once()
    coord_b.async_reset_ledger.assert_not_awaited()


async def test_reset_ledger_targets_multiple_entities(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """A multi-entity target resets every matching tracker (list target)."""
    coord_a = _make_coordinator(hass, specific_config_entry)
    coord_b = _make_coordinator(hass, all_states_config_entry)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][specific_config_entry.entry_id] = coord_a
    hass.data[DOMAIN][all_states_config_entry.entry_id] = coord_b

    await async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_LEDGER,
        {
            ATTR_CONFIRM: True,
            ATTR_ENTITY_ID: ["climate.living_room", "binary_sensor.front_door"],
        },
        blocking=True,
    )

    coord_a.async_reset_ledger.assert_awaited_once()
    coord_b.async_reset_ledger.assert_awaited_once()


async def test_reset_ledger_target_no_match_raises(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
) -> None:
    """A target that matches no loaded tracker raises, resetting nothing."""
    coord = _make_coordinator(hass, specific_config_entry)  # climate.living_room
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coord

    await async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_LEDGER,
            {ATTR_CONFIRM: True, ATTR_ENTITY_ID: "sensor.does_not_exist"},
            blocking=True,
        )

    coord.async_reset_ledger.assert_not_awaited()


async def test_setup_services_idempotent(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """A second setup hits the has_service early-return without re-registering."""
    _make_coordinator(hass, specific_config_entry)

    await async_setup_services(hass)
    handler = hass.services.async_services_for_domain(DOMAIN)[SERVICE_RESET_LEDGER]

    # Second call must short-circuit; the registered handler object is unchanged.
    await async_setup_services(hass)
    handler_again = hass.services.async_services_for_domain(DOMAIN)[
        SERVICE_RESET_LEDGER
    ]

    assert handler_again is handler
    assert hass.services.has_service(DOMAIN, SERVICE_RESET_LEDGER)


async def test_unload_services_removes(setup_services) -> None:
    """async_unload_services removes the registered service."""
    hass, _ = setup_services
    assert hass.services.has_service(DOMAIN, SERVICE_RESET_LEDGER)

    async_unload_services(hass)

    assert not hass.services.has_service(DOMAIN, SERVICE_RESET_LEDGER)
