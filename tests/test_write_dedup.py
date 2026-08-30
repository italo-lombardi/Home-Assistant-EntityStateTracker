"""Tests for the write-dedup coordinator entity bases (§5, §16.2).

The dedup logic is pure comparison over a cached (value, attrs, available)
triple. We drive it with a fake coordinator and stub ``async_write_ha_state`` /
``super().async_will_remove_from_hass`` so no HA wiring is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.entity_state_tracker.write_dedup import (
    DedupCoordinatorBinarySensor,
    DedupCoordinatorSensor,
    WriteDedupMixin,
)


class _FakeCoordinator:
    """Minimal stand-in for the coordinator CoordinatorEntity expects."""

    def __init__(self) -> None:
        self.last_update_success = True
        self.data: Any = None
        self._listeners: dict = {}

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


class _Sensor(DedupCoordinatorSensor):
    """Sensor whose value/attrs/available are set by the test directly."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._value: Any = None
        self._attrs: Any = None
        self._available = True
        self.writes = 0

    @property
    def native_value(self) -> Any:
        return self._value

    @property
    def extra_state_attributes(self) -> Any:
        return self._attrs

    @property
    def available(self) -> bool:
        return self._available

    def async_write_ha_state(self) -> None:
        self.writes += 1


class _BinarySensor(DedupCoordinatorBinarySensor):
    """Binary sensor whose is_on/attrs/available are set by the test."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._on: Any = None
        self._attrs: Any = None
        self._available = True
        self.writes = 0

    @property
    def is_on(self) -> Any:
        return self._on

    @property
    def extra_state_attributes(self) -> Any:
        return self._attrs

    @property
    def available(self) -> bool:
        return self._available

    def async_write_ha_state(self) -> None:
        self.writes += 1


@pytest.fixture
def coordinator() -> _FakeCoordinator:
    return _FakeCoordinator()


def test_mixin_current_value_is_abstract() -> None:
    """The base mixin's _est_current_value must be overridden by subclasses."""
    with pytest.raises(NotImplementedError):
        WriteDedupMixin()._est_current_value()


def test_sensor_first_write_always_through(coordinator) -> None:
    """Cache starts empty, so the first update always publishes."""
    s = _Sensor(coordinator)
    s._value = 10
    s._handle_coordinator_update()
    assert s.writes == 1


def test_sensor_unchanged_skips(coordinator) -> None:
    """Identical value + attrs + available on the next tick skips the write."""
    s = _Sensor(coordinator)
    s._value = 10
    s._attrs = {"percent": 50.0}
    s._handle_coordinator_update()
    assert s.writes == 1
    s._handle_coordinator_update()
    assert s.writes == 1


def test_sensor_changed_value_writes(coordinator) -> None:
    """A changed value writes again."""
    s = _Sensor(coordinator)
    s._value = 10
    s._handle_coordinator_update()
    s._value = 11
    s._handle_coordinator_update()
    assert s.writes == 2


def test_sensor_changed_attrs_writes(coordinator) -> None:
    """Same value but changed attributes writes again."""
    s = _Sensor(coordinator)
    s._value = 10
    s._attrs = {"percent": 50.0}
    s._handle_coordinator_update()
    s._attrs = {"percent": 60.0}
    s._handle_coordinator_update()
    assert s.writes == 2


def test_sensor_available_flip_true_to_false_writes(coordinator) -> None:
    """A True→False availability flip propagates even with an unchanged value."""
    s = _Sensor(coordinator)
    s._value = 10
    s._handle_coordinator_update()
    assert s.writes == 1
    s._available = False
    s._handle_coordinator_update()
    assert s.writes == 2


@pytest.mark.asyncio
async def test_sensor_reset_cache_on_remove_writes_again(
    coordinator, monkeypatch
) -> None:
    """Removal clears the cache so the next update writes its first state again."""
    s = _Sensor(coordinator)
    s._value = 10
    s._handle_coordinator_update()
    s._handle_coordinator_update()
    assert s.writes == 1

    async def _noop_super() -> None:
        return None

    # Stub the CoordinatorEntity.async_will_remove_from_hass base call.
    monkeypatch.setattr(
        type(s).__mro__[2],
        "async_will_remove_from_hass",
        lambda self: _noop_super(),
        raising=False,
    )
    await s.async_will_remove_from_hass()
    # After reset, the same value writes through again.
    s._handle_coordinator_update()
    assert s.writes == 2


def test_binary_first_write_always_through(coordinator) -> None:
    """Binary sensor first update always publishes."""
    b = _BinarySensor(coordinator)
    b._on = True
    b._handle_coordinator_update()
    assert b.writes == 1


def test_binary_unchanged_skips(coordinator) -> None:
    """Binary sensor skips when is_on + attrs + available are unchanged."""
    b = _BinarySensor(coordinator)
    b._on = True
    b._handle_coordinator_update()
    b._handle_coordinator_update()
    assert b.writes == 1


def test_binary_changed_writes(coordinator) -> None:
    """A flipped is_on writes again."""
    b = _BinarySensor(coordinator)
    b._on = True
    b._handle_coordinator_update()
    b._on = False
    b._handle_coordinator_update()
    assert b.writes == 2


def test_binary_available_flip_writes(coordinator) -> None:
    """Binary sensor True→False availability flip propagates."""
    b = _BinarySensor(coordinator)
    b._on = True
    b._handle_coordinator_update()
    b._available = False
    b._handle_coordinator_update()
    assert b.writes == 2


@pytest.mark.asyncio
async def test_binary_reset_cache_on_remove(coordinator, monkeypatch) -> None:
    """Binary sensor removal resets the cache; next update writes again."""
    b = _BinarySensor(coordinator)
    b._on = True
    b._handle_coordinator_update()
    b._handle_coordinator_update()
    assert b.writes == 1

    async def _noop_super() -> None:
        return None

    monkeypatch.setattr(
        type(b).__mro__[2],
        "async_will_remove_from_hass",
        lambda self: _noop_super(),
        raising=False,
    )
    await b.async_will_remove_from_hass()
    b._handle_coordinator_update()
    assert b.writes == 2
