"""Tests for Entity State Tracker binary sensors (§5.1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

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


async def test_setup_compliance_adds_currently_plus_one_compliant_per_frame(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Specific mode with a threshold: currently-in-state + one Compliant/frame."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    hass.data.setdefault(DOMAIN, {})[compliance_config_entry.entry_id] = coordinator

    added: list = []
    await async_setup_entry(hass, compliance_config_entry, added.extend)

    assert sum(isinstance(e, CurrentlyInStateBinarySensor) for e in added) == 1
    compliant = [e for e in added if isinstance(e, CompliantBinarySensor)]
    # One Compliant per enabled frame, each keyed to its own frame.
    assert [c._frame_key for c in compliant] == list(coordinator.enabled_frames)
    assert len(coordinator.enabled_frames) > 1  # proves it's not a single sensor


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
    # entity_id PINNED to the id==slugify(name) default, namespaced by the tracker
    # NAME (entry.title) — never the entry_id ULID — with the metric name slug
    # ("In a Tracked State" → in_a_tracked_state). No frame token (frameless).
    assert sensor.entity_id == (
        "binary_sensor.entity_state_tracker_"
        f"{slugify(specific_config_entry.title)}_in_a_tracked_state"
    )
    assert sensor.device_info["identifiers"] == {
        (DOMAIN, specific_config_entry.entry_id)
    }


async def test_currently_in_state_on_when_tracked(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on True when the source's LIVE state is one of the tracked states."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    hass.states.async_set(coordinator.entity_id, "heat")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is True


async def test_currently_in_state_off_when_not_tracked(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on False when the source's live state is outside the tracked set."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    hass.states.async_set(coordinator.entity_id, "off")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_currently_in_state_off_when_no_data(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on False when the source entity is absent from the state machine."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_currently_in_state_off_when_tracked_none(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """is_on False when tracked_states is None (the ``or ()`` fallback)."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    coordinator.tracked_states = None
    hass.states.async_set(coordinator.entity_id, "heat")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.is_on is False


async def test_currently_in_state_extra_attributes(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Attributes expose source entity, tracked states, and the live state."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    hass.states.async_set(coordinator.entity_id, "heat")
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.extra_state_attributes == {
        "source_entity": coordinator.entity_id,
        "tracked_states": coordinator.tracked_states,
        "current_state": "heat",
    }


async def test_currently_in_state_current_state_none_when_no_data(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """current_state is None when the source entity is absent."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    sensor = CurrentlyInStateBinarySensor(coordinator)

    assert sensor.extra_state_attributes["current_state"] is None


async def test_currently_in_state_repaints_on_source_change(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """A source state change writes state immediately, not just on the tick."""
    coordinator = _make_coordinator(hass, specific_config_entry)
    hass.data.setdefault(DOMAIN, {})[specific_config_entry.entry_id] = coordinator

    added: list = []
    await async_setup_entry(hass, specific_config_entry, added.extend)
    sensor = added[0]
    sensor.hass = hass
    sensor.async_write_ha_state = Mock()  # type: ignore[method-assign]
    # No-op the coordinator listener registration so CoordinatorEntity's
    # async_added_to_hass doesn't arm a refresh-interval timer (which would
    # linger past the test); we only exercise the source subscription here.
    coordinator.async_add_listener = Mock(return_value=lambda: None)  # type: ignore[method-assign]
    await sensor.async_added_to_hass()
    sensor.async_write_ha_state.reset_mock()

    # A source edge fires the subscription → an immediate write (line 128).
    hass.states.async_set(coordinator.entity_id, "heat")
    await hass.async_block_till_done()

    sensor.async_write_ha_state.assert_called()


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
    """Translation key, unique_id, entity_id, and device info are per-frame."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.translation_key == TRANSLATION_KEY_COMPLIANT
    assert sensor.unique_id == (
        f"{compliance_config_entry.entry_id}_today_{TRANSLATION_KEY_COMPLIANT}"
    )
    assert sensor.entity_id == (
        "binary_sensor.entity_state_tracker_"
        f"{slugify(compliance_config_entry.title)}_compliant_today"
    )
    assert sensor.device_info["identifiers"] == {
        (DOMAIN, compliance_config_entry.entry_id)
    }
    assert sensor._frame_key == "today"


async def test_compliant_per_frame_ids_distinct(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Each frame's Compliant sensor gets its own unique_id + entity_id."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    today = CompliantBinarySensor(coordinator, "today")
    month = CompliantBinarySensor(coordinator, "7d")

    assert today.unique_id != month.unique_id
    assert today.entity_id != month.entity_id
    assert month.entity_id.endswith("_compliant_last_7_days")


async def test_compliant_unique_id_distinct_from_currently(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """The two binary sensors never share a unique_id."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    currently = CurrentlyInStateBinarySensor(coordinator)
    compliant = CompliantBinarySensor(coordinator, "today")

    assert currently.unique_id != compliant.unique_id


async def test_compliant_on_at_or_above_threshold(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on True when today's compliance meets the threshold (80)."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=80.0)}
    )
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.is_on is True


async def test_compliant_scores_its_own_frame(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """Each per-frame sensor scores ITS frame, not a shared one."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={
            "today": FrameResult(window_seconds=1.0, compliance_percent=90.0),
            "7d": FrameResult(window_seconds=1.0, compliance_percent=50.0),
        }
    )
    assert CompliantBinarySensor(coordinator, "today").is_on is True
    assert CompliantBinarySensor(coordinator, "7d").is_on is False


async def test_compliant_off_below_threshold(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on False when today's compliance is below the threshold."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=79.9)}
    )
    sensor = CompliantBinarySensor(coordinator, "today")

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
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.is_on is None


async def test_compliant_none_when_no_data(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when the coordinator has no data yet."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = None
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.is_on is None


async def test_compliant_none_when_frame_absent(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when the keyed frame is not in the computed output."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(frames={})
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.is_on is None


async def test_compliant_none_when_compliance_percent_none(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """is_on None when the frame's compliance_percent is None (N/A window)."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    coordinator.data = TrackerData(
        frames={"today": FrameResult(window_seconds=1.0, compliance_percent=None)}
    )
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.is_on is None


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
    sensor = CompliantBinarySensor(coordinator, "today")

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
    sensor = CompliantBinarySensor(coordinator, "today")

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
    sensor = CompliantBinarySensor(coordinator, "today")

    assert sensor.extra_state_attributes["compliance_percent"] is None
    assert sensor.extra_state_attributes["frame"] == "today"


async def test_compliant_compliance_percent_unrecorded(
    hass: HomeAssistant, compliance_config_entry: MockConfigEntry
) -> None:
    """The volatile score + coverage trio are stripped; config stays recorded."""
    coordinator = _make_coordinator(hass, compliance_config_entry)
    sensor = CompliantBinarySensor(coordinator, "today")

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
