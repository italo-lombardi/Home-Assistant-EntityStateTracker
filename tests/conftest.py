"""Shared fixtures for Entity State Tracker tests."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_state_tracker.const import (
    CONF_ENABLE_COMPLIANCE,
    CONF_ENTITY,
    CONF_FRAMES,
    CONF_MIN_STATE_DURATION,
    CONF_MODE,
    CONF_STATES,
    CONF_TARGET,
    CONF_TARGET_THRESHOLD,
    DEFAULT_FRAMES,
    DOMAIN,
    FRAMES,
    MODE_ALL,
    MODE_SPECIFIC,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    yield


def _frames_flags(*enabled: str) -> dict[str, bool]:
    """Build a frame-flag dict; enabled frames True, rest False (default set if none)."""
    keys = enabled or tuple(DEFAULT_FRAMES)
    return {frame: frame in keys for frame in FRAMES}


@pytest.fixture
def specific_config_data() -> dict[str, Any]:
    """Config data for a specific-states tracker (no compliance)."""
    return {
        CONF_ENTITY: "climate.living_room",
        CONF_MODE: MODE_SPECIFIC,
        CONF_STATES: ["heat", "auto"],
        CONF_ENABLE_COMPLIANCE: False,
        CONF_FRAMES: _frames_flags(),
        CONF_MIN_STATE_DURATION: 0,
    }


@pytest.fixture
def compliance_config_data() -> dict[str, Any]:
    """Config data for a specific-states tracker with compliance + threshold."""
    return {
        CONF_ENTITY: "climate.living_room",
        CONF_MODE: MODE_SPECIFIC,
        CONF_STATES: ["heat", "auto"],
        CONF_ENABLE_COMPLIANCE: True,
        CONF_TARGET: ["heat"],
        CONF_TARGET_THRESHOLD: 80,
        CONF_FRAMES: _frames_flags(),
        CONF_MIN_STATE_DURATION: 0,
    }


@pytest.fixture
def all_states_config_data() -> dict[str, Any]:
    """Config data for an all-states tracker."""
    return {
        CONF_ENTITY: "binary_sensor.front_door",
        CONF_MODE: MODE_ALL,
        CONF_FRAMES: _frames_flags(),
        CONF_MIN_STATE_DURATION: 0,
    }


@pytest.fixture
def specific_config_entry(specific_config_data: dict[str, Any]) -> MockConfigEntry:
    """Mock config entry for the specific-states tracker."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Living Room — heat/auto",
        data=specific_config_data,
        entry_id="est_specific_entry",
        unique_id=f"{DOMAIN}_climate.living_room_{MODE_SPECIFIC}_auto_heat",
    )


@pytest.fixture
def compliance_config_entry(compliance_config_data: dict[str, Any]) -> MockConfigEntry:
    """Mock config entry for the compliance-enabled tracker."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Living Room — compliance",
        data=compliance_config_data,
        entry_id="est_compliance_entry",
        unique_id=f"{DOMAIN}_climate.living_room_{MODE_SPECIFIC}_auto_heat",
    )


@pytest.fixture
def all_states_config_entry(all_states_config_data: dict[str, Any]) -> MockConfigEntry:
    """Mock config entry for the all-states tracker."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Front Door — all states",
        data=all_states_config_data,
        entry_id="est_all_entry",
        unique_id=f"{DOMAIN}_binary_sensor.front_door_{MODE_ALL}",
    )
