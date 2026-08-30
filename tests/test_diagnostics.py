"""Tests for Entity State Tracker diagnostics (§9)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_state_tracker.const import DOMAIN
from custom_components.entity_state_tracker.coordinator import (
    EntityStateTrackerCoordinator,
)
from custom_components.entity_state_tracker.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.entity_state_tracker.models import (
    FrameResult,
    TrackerData,
    TrackerLedger,
)


def _make_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> EntityStateTrackerCoordinator:
    """Build a coordinator with disk I/O stubbed."""
    entry.add_to_hass(hass)
    coordinator = EntityStateTrackerCoordinator(hass, entry)
    coordinator.store.load = AsyncMock()  # type: ignore[method-assign]
    return coordinator


async def test_diagnostics_coordinator_not_loaded(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Returns an error dict when the coordinator is absent from hass.data."""
    specific_config_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})

    result = await async_get_config_entry_diagnostics(hass, specific_config_entry)

    assert result == {"error": "coordinator not loaded"}


async def test_diagnostics_wrong_type_in_data(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """A non-coordinator value under the entry_id is treated as not-loaded."""
    specific_config_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = "not-a-coord"

    result = await async_get_config_entry_diagnostics(hass, specific_config_entry)

    assert result["error"] == "coordinator not loaded"


async def test_diagnostics_full_dump(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Dumps entry/coordinator/frames/ledger/store with a populated ledger."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator._ledger = TrackerLedger(
        entity_id="climate.living_room",
        mode="specific_states",
        daily={
            "2026-08-28": {"heat": {"secs": 3600.0, "count": 2}},
            "2026-08-29": {
                "heat": {"secs": 1800.0, "count": 1},
                "off": {"secs": 600.0, "count": 3},
            },
        },
        last_state="heat",
        last_changed_ts="2026-08-29T10:00:00+00:00",
        last_updated_day="2026-08-28",
    )
    coordinator.data = TrackerData(
        frames={
            "today": FrameResult(
                window_seconds=86400.0,
                breakdown_seconds={"heat": 1800.0, "off": 600.0},
                percent=75.0,
                compliance_percent=75.0,
                dominant="heat",
                window_coverage=0.9,
                has_gap=False,
                data_start="2026-08-29",
            )
        },
        last_state="heat",
    )
    hass.data.setdefault(DOMAIN, {})[compliance_config_entry.entry_id] = coordinator

    result = await async_get_config_entry_diagnostics(hass, compliance_config_entry)

    assert set(result) == {"entry", "coordinator", "frames", "ledger", "store"}

    assert result["entry"]["title"] == compliance_config_entry.title
    assert result["entry"]["data"]["entity"] == "climate.living_room"

    coord = result["coordinator"]
    assert coord["entity_id"] == "climate.living_room"
    assert coord["mode"] == "specific_states"
    assert coord["target_threshold"] == 80

    frames = result["frames"]
    assert frames["today"]["percent"] == 75.0
    assert frames["today"]["dominant"] == "heat"
    assert frames["today"]["states"] == 2

    ledger = result["ledger"]
    assert ledger["loaded"] is True
    assert ledger["day_count"] == 2
    assert ledger["oldest_day"] == "2026-08-28"
    assert ledger["newest_day"] == "2026-08-29"
    assert ledger["last_state"] == "heat"
    assert ledger["per_state_seconds"]["heat"] == 5400.0
    assert ledger["per_state_count"]["heat"] == 3
    assert ledger["per_state_count"]["off"] == 3

    assert "disk_read_count" in result["store"]


async def test_diagnostics_no_data_empty_frames(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """frames is {} when the coordinator has not computed anything yet."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = None
    coordinator._ledger = TrackerLedger(entity_id="x", mode="specific_states")
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator

    result = await async_get_config_entry_diagnostics(hass, specific_config_entry)

    assert result["frames"] == {}


async def test_diagnostics_ledger_not_loaded(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """ledger reports loaded False when _ledger is not a TrackerLedger."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = None
    coordinator._ledger = None
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator

    result = await async_get_config_entry_diagnostics(hass, specific_config_entry)

    assert result["ledger"] == {"loaded": False}


async def test_diagnostics_empty_ledger_days(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """An empty daily map yields null oldest/newest and zero day_count."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = None
    coordinator._ledger = TrackerLedger(entity_id="x", mode="specific_states")
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator

    result = await async_get_config_entry_diagnostics(hass, specific_config_entry)

    ledger = result["ledger"]
    assert ledger["day_count"] == 0
    assert ledger["oldest_day"] is None
    assert ledger["newest_day"] is None
    assert ledger["per_state_seconds"] == {}
