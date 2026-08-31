"""Tests for the sensor platform (§5 output, §7 transitions, §16.2).

Entities are constructed against a fake coordinator whose ``data`` is a
:class:`TrackerData` we build directly, so we assert the exact per-mode entity
set and every attribute shape without spinning up a real coordinator.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime

from custom_components.entity_state_tracker.binary_sensor import (
    _device_info as binary_device_info,
)
from custom_components.entity_state_tracker.const import (
    DOMAIN,
    MODE_ALL,
    MODE_SPECIFIC,
)
from custom_components.entity_state_tracker.models import FrameResult, TrackerData
from custom_components.entity_state_tracker.sensor import (
    BreakdownSensor,
    ComplianceSensor,
    DurationSensor,
    PercentSensor,
    async_setup_entry,
)
from custom_components.entity_state_tracker.sensor import (
    _device_info as sensor_device_info,
)


def _frame_result(**kw: Any) -> FrameResult:
    """Build a FrameResult with sensible defaults, overridable per test."""
    defaults: dict[str, Any] = {
        "window_seconds": 3600.0,
        "breakdown_seconds": {"heat": 1800.0, "auto": 600.0, "off": 1200.0},
        "breakdown_pct": {"heat": 50.0, "auto": 16.7, "off": 33.3, "unaccounted": 10.0},
        "counts": {"heat": 3, "auto": 1, "off": 2},
        "avg_duration": {"heat": 600.0, "auto": 600.0, "off": 600.0},
        "dominant": "heat",
        "window_start": "2026-08-29T00:00:00-07:00",
        "data_start": "2026-08-29T00:00:00+00:00",
        "window_coverage": 0.9,
        "has_gap": True,
        "percent": 66.7,
        "compliance_percent": 50.0,
        "unaccounted_seconds": 360.0,
    }
    defaults.update(kw)
    return FrameResult(**defaults)


def _fake_coordinator(
    *,
    mode: str,
    enabled_frames: list[str],
    tracked_states: list[str] | None,
    target_states: list[str] | None,
    data: TrackerData | None,
    target_threshold: float | None = None,
    title: str = "Living Room — heat/auto",
    entry_id: str = "est_entry",
) -> SimpleNamespace:
    """A duck-typed coordinator carrying only what the sensors read."""
    entry = SimpleNamespace(entry_id=entry_id, title=title)
    return SimpleNamespace(
        mode=mode,
        enabled_frames=enabled_frames,
        tracked_states=tracked_states,
        target_states=target_states,
        target_threshold=target_threshold,
        data=data,
        entry=entry,
        entity_id="climate.living_room",
        last_update_success=True,
        # CoordinatorEntity.__init__ registers a listener.
        async_add_listener=lambda cb, context=None: lambda: None,
    )


# --------------------------------------------------------------------------
# async_setup_entry — per-mode entity count formula
# --------------------------------------------------------------------------


async def _collect(coordinator, hass, entry_id) -> list:
    """Run async_setup_entry against a fake coordinator, return added entities."""
    hass.data.setdefault(DOMAIN, {})[entry_id] = coordinator
    entry = SimpleNamespace(entry_id=entry_id)
    added: list = []
    await async_setup_entry(hass, entry, lambda ents: added.extend(ents))
    return added


@pytest.mark.asyncio
async def test_setup_specific_no_target_counts(hass) -> None:
    """SPECIFIC, no target: one DurationSensor + one PercentSensor per frame.

    No compliance sensor without a target set.
    """
    frames = ["today", "yesterday", "24h", "7d"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=frames,
        tracked_states=["heat", "auto"],
        target_states=None,
        data=data,
        entry_id="e1",
    )
    added = await _collect(coord, hass, "e1")
    durations = [e for e in added if isinstance(e, DurationSensor)]
    percents = [e for e in added if isinstance(e, PercentSensor)]
    assert len(durations) == len(frames)
    assert len(percents) == len(frames)
    assert not [e for e in added if isinstance(e, ComplianceSensor)]
    assert len(added) == 2 * len(frames)


@pytest.mark.asyncio
async def test_setup_specific_with_target_counts(hass) -> None:
    """SPECIFIC + target: duration + percent + compliance per frame."""
    frames = ["today", "yesterday", "24h", "7d"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=frames,
        tracked_states=["heat", "auto"],
        target_states=["heat"],
        data=data,
        entry_id="e2",
    )
    added = await _collect(coord, hass, "e2")
    assert len([e for e in added if isinstance(e, DurationSensor)]) == len(frames)
    assert len([e for e in added if isinstance(e, PercentSensor)]) == len(frames)
    assert len([e for e in added if isinstance(e, ComplianceSensor)]) == len(frames)
    assert len(added) == 3 * len(frames)


@pytest.mark.asyncio
async def test_setup_specific_today_disabled_counts(hass) -> None:
    """SPECIFIC with today disabled: the per-frame set covers only enabled frames."""
    frames = ["yesterday", "24h"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=frames,
        tracked_states=["heat"],
        target_states=["heat"],
        data=data,
        entry_id="e3",
    )
    added = await _collect(coord, hass, "e3")
    assert len([e for e in added if isinstance(e, DurationSensor)]) == len(frames)
    assert len([e for e in added if isinstance(e, PercentSensor)]) == len(frames)
    assert len([e for e in added if isinstance(e, ComplianceSensor)]) == len(frames)
    assert len(added) == 3 * len(frames)


@pytest.mark.asyncio
async def test_setup_all_states_counts(hass) -> None:
    """ALL_STATES: exactly one BreakdownSensor per enabled frame."""
    frames = ["today", "yesterday", "24h", "7d"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_ALL,
        enabled_frames=frames,
        tracked_states=None,
        target_states=None,
        data=data,
        entry_id="e4",
    )
    added = await _collect(coord, hass, "e4")
    breakdowns = [e for e in added if isinstance(e, BreakdownSensor)]
    assert len(breakdowns) == len(frames)
    assert len(added) == len(frames)


@pytest.mark.asyncio
async def test_setup_device_name_falls_back_to_entity_id(hass) -> None:
    """When entry.title is empty, device name uses coordinator.entity_id."""
    frames = ["today"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=frames,
        tracked_states=["heat"],
        target_states=None,
        data=data,
        title="",
        entry_id="e5",
    )
    added = await _collect(coord, hass, "e5")
    duration = next(e for e in added if isinstance(e, DurationSensor))
    assert "climate.living_room" in duration.device_info["name"]


def test_device_info_name_identical_across_platforms() -> None:
    """sensor + binary_sensor build the SAME device name for one entry (§5, S1).

    Both platforms register {(DOMAIN, entry_id)} as the same device; a differing
    name would flap the device's display name on reload by setup order.
    """
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=None,
        data=None,
        title="Living Room — heat/auto",
    )
    assert sensor_device_info(coord)["name"] == binary_device_info(coord)["name"]


def test_device_info_name_identical_across_platforms_title_fallback() -> None:
    """The unified name falls back to entity_id on both platforms when title is empty."""
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=None,
        data=None,
        title="",
    )
    s_name = sensor_device_info(coord)["name"]
    b_name = binary_device_info(coord)["name"]
    assert s_name == b_name
    assert "climate.living_room" in s_name


# --------------------------------------------------------------------------
# DurationSensor
# --------------------------------------------------------------------------


def _duration_coord(
    *, tracked=("heat", "auto"), target=None, result: FrameResult | None = -1
) -> SimpleNamespace:
    frames_data = None
    if result is not None:
        res = _frame_result() if result == -1 else result
        frames_data = TrackerData(frames={"today": res})
    return _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=list(tracked) if tracked is not None else None,
        target_states=list(target) if target else None,
        target_threshold=80.0 if target else None,
        data=frames_data,
    )


def test_duration_native_value_tracked_subset() -> None:
    """native_value sums only the tracked states' seconds (int)."""
    coord = _duration_coord(tracked=("heat", "auto"))
    sensor = DurationSensor(coord, "today")
    # heat 1800 + auto 600 = 2400.
    assert sensor.native_value == 2400
    assert isinstance(sensor.native_value, int)


def test_duration_native_value_all_states_when_tracked_none() -> None:
    """tracked_states None sums every recorded state's seconds."""
    coord = _duration_coord(tracked=None)
    sensor = DurationSensor(coord, "today")
    # 1800 + 600 + 1200 = 3600.
    assert sensor.native_value == 3600


def test_duration_native_value_none_before_data() -> None:
    """No coordinator data → native_value is None."""
    coord = _duration_coord(result=None)
    sensor = DurationSensor(coord, "today")
    assert sensor.native_value is None


def test_duration_native_value_none_when_frame_missing() -> None:
    """Data present but this frame absent → None."""
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=None,
        data=TrackerData(frames={}),
    )
    sensor = DurationSensor(coord, "today")
    assert sensor.native_value is None


def test_duration_entity_descriptors() -> None:
    """Duration sensor carries the §5.1 descriptor contract."""
    coord = _duration_coord()
    sensor = DurationSensor(coord, "today")
    assert sensor.device_class == SensorDeviceClass.DURATION
    assert sensor.native_unit_of_measurement == UnitOfTime.SECONDS
    assert sensor.suggested_unit_of_measurement == UnitOfTime.HOURS
    assert sensor.suggested_display_precision == 1
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.unique_id == "est_entry_today_duration"
    # entity_id is PINNED to the card-discoverable slug (§card parity): metric
    # LABEL slug ("duration") + frame LABEL slug ("today"), NOT the metric/frame
    # keys — so the card's DOMAIN_PREFIX discovery always finds a custom-named
    # tracker.
    assert sensor.entity_id == "sensor.entity_state_tracker_est_entry_duration_today"


def test_frame_sensor_entity_id_pinned_frame_label_slug() -> None:
    """A multi-token frame label slugifies into the pinned entity_id."""
    frames = ["24h"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=frames,
        tracked_states=["heat"],
        target_states=None,
        data=data,
        entry_id="e_multi",
    )
    sensor = DurationSensor(coord, "24h")
    assert (
        sensor.entity_id == "sensor.entity_state_tracker_e_multi_duration_last_24_hours"
    )


def test_breakdown_sensor_entity_id_uses_state_breakdown_metric_slug() -> None:
    """Breakdown sensor pins the "state_breakdown" metric slug (not "breakdown")."""
    coord = _breakdown_coord()
    coord.entry.entry_id = "e_bd"
    sensor = BreakdownSensor(coord, "today")
    assert sensor.entity_id == "sensor.entity_state_tracker_e_bd_state_breakdown_today"


def test_duration_attributes_without_target() -> None:
    """Attributes include percent/coverage/gap + transition metrics; no compliance."""
    coord = _duration_coord(tracked=("heat", "auto"), target=None)
    sensor = DurationSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert attrs["percent"] == 66.7
    # duration_seconds is the RAW tracked seconds (== native_value), independent
    # of HA's native→suggested (seconds→hours) unit conversion on the state, so
    # the card has an unambiguous seconds figure. heat 1800 + auto 600 = 2400.
    assert attrs["duration_seconds"] == 2400
    assert attrs["duration_seconds"] == sensor.native_value
    assert isinstance(attrs["duration_seconds"], int)
    assert attrs["tracked_states"] == ["heat", "auto"]
    # source_entity names the tracked entity so the card can show it (Part A).
    assert attrs["source_entity"] == "climate.living_room"
    # frame is the common-core window key (RECORDED — config-stable).
    assert attrs["frame"] == "today"
    assert attrs["window_coverage"] == 0.9
    assert attrs["has_gap"] is True
    assert attrs["data_start"] == "2026-08-29T00:00:00+00:00"
    # window_start is the frame WINDOW's start (result.window_start), a distinct
    # field from data_start — regression guard for the wiring fix.
    assert attrs["window_start"] == "2026-08-29T00:00:00-07:00"
    assert attrs["window_start"] != attrs["data_start"]
    assert "compliance_percent" not in attrs
    # target_threshold rides along only with a target set — absent here.
    assert "target_threshold" not in attrs
    # Transition metrics scoped to tracked states.
    assert set(attrs["counts"]) == {"heat", "auto"}
    assert attrs["counts"]["heat"] == 3
    assert attrs["avg_duration_seconds"]["heat"] == 600.0
    # last_seen was dropped; last_entered/last_exited replace it (asserted in the
    # dedicated exposure test below).
    assert "last_seen" not in attrs


def test_duration_attributes_with_target_adds_compliance() -> None:
    """A target set surfaces compliance_percent."""
    coord = _duration_coord(tracked=("heat", "auto"), target=("heat",))
    sensor = DurationSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert attrs["compliance_percent"] == 50.0
    assert attrs["target_states"] == ["heat"]
    # target_threshold rides along beside compliance_percent when a target is set.
    assert attrs["target_threshold"] == 80.0


def test_duration_attributes_all_states_transition_keys_when_tracked_none() -> None:
    """tracked_states None → transition metrics span every counted state."""
    coord = _duration_coord(tracked=None)
    sensor = DurationSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert set(attrs["counts"]) == {"heat", "auto", "off"}


def test_duration_attributes_previous_state() -> None:
    """previous_state is threaded from coordinator.data."""
    data = TrackerData(frames={"today": _frame_result()}, previous_state="off")
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=None,
        data=data,
    )
    sensor = DurationSensor(coord, "today")
    assert sensor.extra_state_attributes["previous_state"] == "off"


def test_duration_attributes_none_before_data() -> None:
    """No data → extra_state_attributes is None."""
    coord = _duration_coord(result=None)
    sensor = DurationSensor(coord, "today")
    assert sensor.extra_state_attributes is None


def test_last_entered_exited_exposed_on_both_sensors() -> None:
    """Both sensors expose last_entered/last_exited as flat {state: iso} dicts.

    They are tracker-global (frame-independent) → sourced whole from
    coordinator.data, identical on the duration and breakdown sensors (§7).
    """
    entered = {"heat": "2026-08-31T10:00:00+00:00", "off": "2026-08-31T09:00:00+00:00"}
    exited = {"off": "2026-08-31T10:00:00+00:00"}
    dur_data = TrackerData(
        frames={"today": _frame_result()},
        last_entered=entered,
        last_exited=exited,
    )
    dur_coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=None,
        data=dur_data,
    )
    dur_attrs = DurationSensor(dur_coord, "today").extra_state_attributes
    assert dur_attrs["last_entered"] == entered
    assert dur_attrs["last_exited"] == exited

    brk_data = TrackerData(
        frames={"today": _frame_result()},
        last_entered=entered,
        last_exited=exited,
    )
    brk_coord = _fake_coordinator(
        mode=MODE_ALL,
        enabled_frames=["today"],
        tracked_states=None,
        target_states=None,
        data=brk_data,
    )
    brk_attrs = BreakdownSensor(brk_coord, "today").extra_state_attributes
    assert brk_attrs["last_entered"] == entered
    assert brk_attrs["last_exited"] == exited
    # Both keys are stripped from the recorder (they churn) on both sensors.
    assert {"last_entered", "last_exited"} <= DurationSensor._unrecorded_attributes
    assert {"last_entered", "last_exited"} <= BreakdownSensor._unrecorded_attributes


def test_last_entered_exited_default_empty_when_no_data() -> None:
    """A TrackerData carrying no stamps yet exposes empty dicts, not None/error."""
    coord = _duration_coord(tracked=("heat",))
    attrs = DurationSensor(coord, "today").extra_state_attributes
    assert attrs["last_entered"] == {}
    assert attrs["last_exited"] == {}


def test_duration_unrecorded_attributes_covers_volatile_keys() -> None:
    """DurationSensor strips its churny attributes from the recorder (§5.3)."""
    unrecorded = DurationSensor._unrecorded_attributes
    # The volatile keys must all be excluded from recorded state_attributes.
    assert {
        "counts",
        "avg_duration_seconds",
        "previous_state",
        "last_entered",
        "last_exited",
        "percent",
        "compliance_percent",
        "duration_seconds",
        "window_start",
        "data_start",
        "window_coverage",
        "has_gap",
    } <= unrecorded
    # Config attributes stay recorded — they don't churn.
    assert "tracked_states" not in unrecorded
    assert "target_states" not in unrecorded
    # source_entity is config-stable → RECORDED (not stripped from recorder).
    assert "source_entity" not in unrecorded
    # last_seen was dropped entirely — replaced by last_entered/last_exited.
    assert "last_seen" not in unrecorded


# --------------------------------------------------------------------------
# BreakdownSensor (all-states)
# --------------------------------------------------------------------------


def _breakdown_coord(result: FrameResult | None = -1) -> SimpleNamespace:
    frames_data = None
    if result is not None:
        res = _frame_result() if result == -1 else result
        frames_data = TrackerData(frames={"today": res}, previous_state="idle")
    return _fake_coordinator(
        mode=MODE_ALL,
        enabled_frames=["today"],
        tracked_states=None,
        target_states=None,
        data=frames_data,
    )


def test_breakdown_native_value_is_dominant() -> None:
    """State is the dominant (max-duration) state."""
    coord = _breakdown_coord()
    sensor = BreakdownSensor(coord, "today")
    assert sensor.native_value == "heat"


def test_breakdown_native_value_none_before_data() -> None:
    """No data → state None."""
    coord = _breakdown_coord(result=None)
    sensor = BreakdownSensor(coord, "today")
    assert sensor.native_value is None


def test_breakdown_unrecorded_attributes_exact_set() -> None:
    """_unrecorded_attributes is the EXACT churny-key set (§5.3)."""
    assert BreakdownSensor._unrecorded_attributes == frozenset(
        {
            "breakdown_seconds",
            "breakdown_pct",
            "counts",
            "avg_duration_seconds",
            "previous_state",
            "last_entered",
            "last_exited",
            "window_seconds",
            "data_start",
            "window_coverage",
            "has_gap",
            "unaccounted_seconds",
        }
    )


def test_breakdown_attributes_sorted_by_seconds_desc() -> None:
    """breakdown_seconds keys are sorted by seconds descending."""
    coord = _breakdown_coord()
    sensor = BreakdownSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    # source_entity names the tracked entity so the card can show it (Part A).
    assert attrs["source_entity"] == "climate.living_room"
    # frame is the common-core window key (RECORDED — config-stable).
    assert attrs["frame"] == "today"
    # heat 1800 > off 1200 > auto 600.
    assert list(attrs["breakdown_seconds"]) == ["heat", "off", "auto"]
    assert attrs["breakdown_seconds"]["heat"] == 1800
    assert all(isinstance(v, int) for v in attrs["breakdown_seconds"].values())
    # breakdown_pct shares that per-state ordering, then the additive
    # "unaccounted" key trails last (it has no breakdown_seconds entry).
    assert list(attrs["breakdown_pct"]) == ["heat", "off", "auto", "unaccounted"]
    assert list(attrs["counts"]) == ["heat", "off", "auto"]
    assert list(attrs["avg_duration_seconds"]) == ["heat", "off", "auto"]


def test_breakdown_attributes_carry_window_metrics_and_previous_state() -> None:
    """Window metrics + previous_state are surfaced in attributes."""
    coord = _breakdown_coord()
    sensor = BreakdownSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert attrs["window_seconds"] == 3600.0
    assert attrs["data_start"] == "2026-08-29T00:00:00+00:00"
    assert attrs["window_coverage"] == 0.9
    assert attrs["has_gap"] is True
    assert attrs["previous_state"] == "idle"


def test_breakdown_surfaces_unaccounted_seconds() -> None:
    """unaccounted_seconds rides along as a breakdown attribute (Fix 3b)."""
    coord = _breakdown_coord()
    sensor = BreakdownSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert attrs["unaccounted_seconds"] == 360.0


def test_breakdown_pct_carries_unaccounted_key_but_others_stay_pure() -> None:
    """breakdown_pct gains an "unaccounted" key; the per-state dicts do not.

    The additive key lets a template loop breakdown_pct to ~100, while
    breakdown_seconds/counts/avg_duration_seconds stay pure per-state (it is not a real
    state, so it has no seconds/count/avg).
    """
    coord = _breakdown_coord()
    sensor = BreakdownSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert "unaccounted" in attrs["breakdown_pct"]
    assert attrs["breakdown_pct"]["unaccounted"] == 10.0
    assert "unaccounted" not in attrs["breakdown_seconds"]
    assert "unaccounted" not in attrs["counts"]
    assert "unaccounted" not in attrs["avg_duration_seconds"]


def test_breakdown_special_states_are_ordinary_rows() -> None:
    """unavailable/unknown/none appear as ordinary breakdown rows (§5.2)."""
    result = _frame_result(
        breakdown_seconds={"unavailable": 900.0, "unknown": 300.0, "none": 100.0},
        breakdown_pct={"unavailable": 69.2, "unknown": 23.1, "none": 7.7},
        counts={"unavailable": 2, "unknown": 1, "none": 1},
        avg_duration={"unavailable": 450.0, "unknown": 300.0, "none": 100.0},
        dominant="unavailable",
    )
    coord = _breakdown_coord(result=result)
    sensor = BreakdownSensor(coord, "today")
    attrs = sensor.extra_state_attributes
    assert list(attrs["breakdown_seconds"]) == ["unavailable", "unknown", "none"]
    assert sensor.native_value == "unavailable"


def test_breakdown_attributes_none_before_data() -> None:
    """No data → attributes None."""
    coord = _breakdown_coord(result=None)
    sensor = BreakdownSensor(coord, "today")
    assert sensor.extra_state_attributes is None


def test_breakdown_previous_state_none_passthrough() -> None:
    """A data object whose previous_state is None surfaces None in attributes."""
    result = _frame_result()
    coord = _fake_coordinator(
        mode=MODE_ALL,
        enabled_frames=["today"],
        tracked_states=None,
        target_states=None,
        data=TrackerData(frames={"today": result}, previous_state=None),
    )
    sensor = BreakdownSensor(coord, "today")
    assert sensor.extra_state_attributes["previous_state"] is None


# --------------------------------------------------------------------------
# PercentSensor / ComplianceSensor (specific-mode, standalone % entities §5.1)
# --------------------------------------------------------------------------


def test_percent_sensor_descriptors() -> None:
    """Percent sensor carries the §5.1 HA-correct % measurement contract."""
    coord = _duration_coord(tracked=("heat", "auto"))
    sensor = PercentSensor(coord, "today")
    assert sensor.native_unit_of_measurement == PERCENTAGE
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    # No SensorDeviceClass.PERCENTAGE exists — device_class is deliberately None
    # (no borrowed BATTERY/HUMIDITY class).
    assert sensor.device_class is None
    assert sensor.suggested_display_precision == 1
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    # DIAGNOSTIC, NOT disabled-by-default (§5 decision): a disabled MEASUREMENT
    # sensor accrues no Statistics until manually enabled.
    assert sensor.entity_registry_enabled_default is True


def test_compliance_sensor_descriptors() -> None:
    """Compliance sensor shares the percent descriptor contract."""
    coord = _duration_coord(tracked=("heat", "auto"), target=("heat",))
    sensor = ComplianceSensor(coord, "today")
    assert sensor.native_unit_of_measurement == PERCENTAGE
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.device_class is None
    assert sensor.suggested_display_precision == 1
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is True


def test_percent_native_value_equals_frame_percent() -> None:
    """native_value is the frame's percent (0–100), rounded 1 dp."""
    coord = _duration_coord(tracked=("heat", "auto"))
    sensor = PercentSensor(coord, "today")
    assert sensor.native_value == 66.7


def test_compliance_native_value_equals_frame_compliance() -> None:
    """native_value is the frame's compliance_percent (0–100), rounded 1 dp."""
    coord = _duration_coord(tracked=("heat", "auto"), target=("heat",))
    sensor = ComplianceSensor(coord, "today")
    assert sensor.native_value == 50.0


def test_percent_native_value_rounds_to_one_dp() -> None:
    """A drifting ratio is rounded to 1 dp so idle ticks hash-dedup (§5)."""
    result = _frame_result(percent=66.6666, compliance_percent=33.3333)
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=["heat"],
        target_threshold=80.0,
        data=TrackerData(frames={"today": result}),
    )
    assert PercentSensor(coord, "today").native_value == 66.7
    assert ComplianceSensor(coord, "today").native_value == 33.3


def test_percent_native_value_none_before_data() -> None:
    """No coordinator data → native_value is None."""
    coord = _duration_coord(result=None)
    assert PercentSensor(coord, "today").native_value is None


def test_compliance_native_value_none_before_data() -> None:
    """No coordinator data → native_value is None."""
    coord = _duration_coord(target=("heat",), result=None)
    assert ComplianceSensor(coord, "today").native_value is None


def test_percent_native_value_none_when_result_percent_none() -> None:
    """A frame whose percent is None (never expected in specific mode) → None."""
    result = _frame_result(percent=None, compliance_percent=None)
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=["today"],
        tracked_states=["heat"],
        target_states=["heat"],
        target_threshold=80.0,
        data=TrackerData(frames={"today": result}),
    )
    assert PercentSensor(coord, "today").native_value is None
    assert ComplianceSensor(coord, "today").native_value is None


def test_percent_and_compliance_unique_ids() -> None:
    """unique_id uses the 'percent'/'compliance' metric slug, distinct from duration."""
    coord = _duration_coord(tracked=("heat",), target=("heat",))
    pct = PercentSensor(coord, "today")
    comp = ComplianceSensor(coord, "today")
    dur = DurationSensor(coord, "today")
    assert pct.unique_id == "est_entry_today_percent"
    assert comp.unique_id == "est_entry_today_compliance"
    # No collision with the duration sensor's unique_id.
    assert len({pct.unique_id, comp.unique_id, dur.unique_id}) == 3


def test_percent_and_compliance_entity_ids_pinned() -> None:
    """Pinned entity_ids carry the 'percent'/'compliance' metric label slug."""
    coord = _duration_coord(tracked=("heat",), target=("heat",))
    assert (
        PercentSensor(coord, "today").entity_id
        == "sensor.entity_state_tracker_est_entry_percent_today"
    )
    assert (
        ComplianceSensor(coord, "today").entity_id
        == "sensor.entity_state_tracker_est_entry_compliance_today"
    )


def test_percent_and_compliance_carry_no_extra_attributes() -> None:
    """The % sensors publish their STATE only — no volatile attribute churn (§8)."""
    coord = _duration_coord(tracked=("heat",), target=("heat",))
    assert PercentSensor(coord, "today").extra_state_attributes is None
    assert ComplianceSensor(coord, "today").extra_state_attributes is None


@pytest.mark.asyncio
async def test_percent_absent_in_all_states_mode(hass) -> None:
    """ALL_STATES emits neither PercentSensor nor ComplianceSensor (§5.1)."""
    frames = ["today", "yesterday"]
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_ALL,
        enabled_frames=frames,
        tracked_states=None,
        target_states=None,
        data=data,
        entry_id="e_all",
    )
    added = await _collect(coord, hass, "e_all")
    assert not [e for e in added if isinstance(e, (PercentSensor, ComplianceSensor))]


@pytest.mark.asyncio
async def test_disabling_frame_drops_its_percent_and_compliance(hass) -> None:
    """Disabling a frame removes its percent + compliance sensors (per-frame scope)."""
    frames = ["today"]  # yesterday/24h/7d disabled
    data = TrackerData(frames={f: _frame_result() for f in frames})
    coord = _fake_coordinator(
        mode=MODE_SPECIFIC,
        enabled_frames=frames,
        tracked_states=["heat"],
        target_states=["heat"],
        data=data,
        entry_id="e_drop",
    )
    added = await _collect(coord, hass, "e_drop")
    pct_frames = {e._frame for e in added if isinstance(e, PercentSensor)}
    comp_frames = {e._frame for e in added if isinstance(e, ComplianceSensor)}
    assert pct_frames == {"today"}
    assert comp_frames == {"today"}
    # No sensor for a disabled frame.
    assert "yesterday" not in pct_frames
    assert "yesterday" not in comp_frames
