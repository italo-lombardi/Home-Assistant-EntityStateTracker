"""Tests for Entity State Tracker binary sensors (§5.1)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_state_tracker.binary_sensor import (
    CompliantBinarySensor,
    CurrentlyInStateBinarySensor,
    async_setup_entry,
)
from custom_components.entity_state_tracker.const import (
    DOMAIN,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
)
from custom_components.entity_state_tracker.coordinator import (
    EntityStateTrackerCoordinator,
)
from custom_components.entity_state_tracker.models import FrameResult, TrackerData


def _make_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry
) -> EntityStateTrackerCoordinator:
    """Build a coordinator without touching disk (store methods stubbed)."""
    entry.add_to_hass(hass)
    coordinator = EntityStateTrackerCoordinator(hass, entry)
    coordinator.store.load = AsyncMock()  # type: ignore[method-assign]
    return coordinator


# --------------------------------------------------------------------------- #
# async_setup_entry — which entities appear
# --------------------------------------------------------------------------- #


async def test_setup_specific_no_compliance_adds_currently_in_state(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Specific mode without a threshold: only currently-in-state is added."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator

    added: list = []
    await async_setup_entry(hass, specific_config_entry, added.extend)

    assert len(added) == 1
    assert isinstance(added[0], CurrentlyInStateBinarySensor)


async def test_setup_compliance_adds_both(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Specific mode with a threshold: both binary sensors are added."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    hass.data.setdefault(DOMAIN, {})[compliance_config_entry.entry_id] = coordinator

    added: list = []
    await async_setup_entry(hass, compliance_config_entry, added.extend)

    kinds = {type(e) for e in added}
    assert kinds == {CurrentlyInStateBinarySensor, CompliantBinarySensor}


async def test_setup_all_states_adds_nothing(
    hass: HomeAssistant, all_states_config_entry: MockConfigEntry
) -> None:
    """All-states mode: no currently-in-state, no compliant (no threshold)."""
    coordinator = _make_coordinator(hass, all_states_config_entry)
    hass.data.setdefault(DOMAIN, {})[all_states_config_entry.entry_id] = coordinator

    added: list = []
    await async_setup_entry(hass, all_states_config_entry, added.extend)

    assert added == []


async def test_setup_specific_without_tracked_states_skips_currently(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Specific mode but empty tracked_states: currently-in-state is skipped."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.tracked_states = None
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator

    added: list = []
    await async_setup_entry(hass, specific_config_entry, added.extend)

    assert added == []


# --------------------------------------------------------------------------- #
# CurrentlyInStateBinarySensor
# --------------------------------------------------------------------------- #


async def test_currently_in_state_attributes(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Device class, translation key, unique_id, and device info are set."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.device_class is None
    assert sensor.translation_key == TRANSLATION_KEY_CURRENTLY_IN_STATE
    assert sensor.unique_id == (
        f"{specific_config_entry.entry_id}__{TRANSLATION_KEY_CURRENTLY_IN_STATE}"
    )
    # entity_id PINNED to the card-discoverable stem (shares the tracker's
    # device prefix; no frame token since binary sensors are frameless).
    assert sensor.entity_id == (
        "binary_sensor.entity_state_tracker_"
        f"{slugify(specific_config_entry.entry_id)}_currently_in_state"
    )
    assert sensor.device_info["identifiers"] == {
        (DOMAIN, specific_config_entry.entry_id)
    }


async def test_currently_in_state_on_when_tracked(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on True when last_state is one of the tracked states."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = TrackerData(frames={}, last_state="heat")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is True


async def test_currently_in_state_off_when_not_tracked(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on False when last_state is outside the tracked set."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = TrackerData(frames={}, last_state="off")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_currently_in_state_off_when_no_data(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on False when the coordinator has no data yet."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = None
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_currently_in_state_off_when_tracked_none(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on False when tracked_states is None (the ``or ()`` fallback)."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.tracked_states = None
    coordinator.data = TrackerData(frames={}, last_state="heat")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_currently_in_state_extra_attributes(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Attributes expose source entity, tracked states, and the live state."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = TrackerData(frames={}, last_state="heat")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.extra_state_attributes == {
        "source_entity": coordinator.entity_id,
        "tracked_states": coordinator.tracked_states,
        "current_state": "heat",
    }


async def test_currently_in_state_current_state_none_when_no_data(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """current_state is None before the coordinator has any data."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.data = None
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.extra_state_attributes["current_state"] is None


async def test_currently_in_state_unrecorded_attributes(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Only the volatile current_state is stripped; config stays recorded."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor._unrecorded_attributes == frozenset({"current_state"})
    assert "source_entity" not in sensor._unrecorded_attributes
    assert "tracked_states" not in sensor._unrecorded_attributes


# --------------------------------------------------------------------------- #


async def test_compliant_attributes(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Translation key, unique_id, and device info are set; frame is today."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.translation_key == TRANSLATION_KEY_COMPLIANT
    assert sensor.unique_id == (
        f"{compliance_config_entry.entry_id}__{TRANSLATION_KEY_COMPLIANT}"
    )
    assert sensor.entity_id == (
        "binary_sensor.entity_state_tracker_"
        f"{slugify(compliance_config_entry.entry_id)}_compliant"
    )
    assert sensor.device_info["identifiers"] == {
        (DOMAIN, compliance_config_entry.entry_id)
    }
    assert sensor._frame_key == "today"


async def test_compliant_unique_id_distinct_from_currently(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """The two binary sensors never share a unique_id."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    currently = CurrentlyInStateBinarySensor(coordinator)
    compliant = CompliantBinarySensor(coordinator)

    assert currently.unique_id != compliant.unique_id


async def test_compliant_on_at_or_above_threshold(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on True when today's compliance meets the threshold (80)."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=80.0)}
    )
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.is_on is True


async def test_compliant_off_below_threshold(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on False when today's compliance is below the threshold."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=79.9)}
    )
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_compliant_none_when_threshold_missing(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when target_threshold has been cleared."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.target_threshold = None
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=99.0)}
    )
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.is_on is None


async def test_compliant_none_when_no_data(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when the coordinator has no data yet."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = None
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.is_on is None


async def test_compliant_none_when_frame_absent(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when the keyed frame is not in the computed output."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(frames={})
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.is_on is None


async def test_compliant_none_when_compliance_percent_none(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when the frame's compliance_percent is None (N/A window)."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=None)}
    )
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.is_on is None


async def test_compliant_falls_back_to_first_enabled_frame(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """When today is disabled, the sensor keys the first enabled frame."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.enabled_frames = ["7d", "30d"]
    sensor = CompliantBinarySensor(coordinator)

    assert sensor._frame_key == "7d"


async def test_compliant_defaults_to_today_when_no_frames_enabled(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """With no enabled frames at all, the fallback lands on today."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.enabled_frames = []
    sensor = CompliantBinarySensor(coordinator)

    assert sensor._frame_key == "today"


async def test_compliant_extra_attributes(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Attributes expose the score, target set, threshold, and scored frame."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={
            "today": FrameResult(
                window_seconds=1.0,
                compliance_percent=88.0,
                data_start="2026-08-29T00:00:00+00:00",
                window_coverage=0.9,
                has_gap=True,
            )
        }
    )
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.extra_state_attributes == {
        "source_entity": coordinator.entity_id,
        "compliance_percent": 88.0,
        "tracked_states": coordinator.tracked_states,
        "target_states": ["heat"],
        "target_threshold": 80,
        "frame": "today",
        "data_start": "2026-08-29T00:00:00+00:00",
        "window_coverage": 0.9,
        "has_gap": True,
    }


async def test_compliant_extra_attributes_none_when_no_data(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """With no data the score is None but the config still surfaces."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = None
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.extra_state_attributes == {
        "source_entity": coordinator.entity_id,
        "compliance_percent": None,
        "tracked_states": coordinator.tracked_states,
        "target_states": ["heat"],
        "target_threshold": 80,
        "frame": "today",
        "data_start": None,
        "window_coverage": None,
        "has_gap": None,
    }


async def test_compliant_extra_attributes_none_when_frame_absent(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """A missing scored frame leaves compliance_percent None, config intact."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(frames={})
    sensor = CompliantBinarySensor(coordinator)

    assert sensor.extra_state_attributes["compliance_percent"] is None
    assert sensor.extra_state_attributes["frame"] == "today"


async def test_compliant_compliance_percent_unrecorded(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """The volatile score + coverage trio are stripped; config stays recorded."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    sensor = CompliantBinarySensor(coordinator)

    assert sensor._unrecorded_attributes == frozenset(
        {"compliance_percent", "data_start", "window_coverage", "has_gap"}
    )
    # Config-stable attributes stay recorded.
    assert "target_states" not in sensor._unrecorded_attributes
    assert "source_entity" not in sensor._unrecorded_attributes
    assert "tracked_states" not in sensor._unrecorded_attributes
    assert "target_threshold" not in sensor._unrecorded_attributes
    assert "frame" not in sensor._unrecorded_attributes
    # The old `target` key is gone — renamed to `target_states`.
    assert "target" not in sensor.extra_state_attributes
