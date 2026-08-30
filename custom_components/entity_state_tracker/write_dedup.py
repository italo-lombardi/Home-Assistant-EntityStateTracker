"""Coordinator-aware entity bases that skip writes when state and attrs are unchanged.

Home Assistant writes a new history row every time ``async_write_ha_state`` is
called. ``CoordinatorEntity`` triggers that call on every coordinator tick
(every ``SCAN_INTERVAL`` seconds), regardless of whether the entity's value
actually changed. For sensors that publish a duration, a percent or a per-state
breakdown dict, the value is identical the vast majority of idle ticks, so each
refresh would produce a redundant recorder row.

The mixin and base classes in this module compare the just-computed
``native_value``/``is_on``, ``extra_state_attributes`` and ``available``
against the previously published triple and short-circuit
``async_write_ha_state`` when all three match. The first write always goes
through (the cache starts empty), so an entity always publishes an initial
state, and a coordinator failure that flips ``available`` from True to False
still propagates even when the cached value would otherwise look unchanged.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EntityStateTrackerCoordinator

_UNSET: Any = object()


class WriteDedupMixin:
    """Cache the last published triple and decide whether to write."""

    _est_last_value: Any = _UNSET
    _est_last_attrs: Any = _UNSET
    _est_last_available: Any = _UNSET

    def _est_current_value(self) -> Any:
        """Return the value subclasses publish (``native_value`` or ``is_on``)."""
        raise NotImplementedError

    def _est_should_write(self) -> bool:
        """Return True when value, attrs, or availability differ from cache."""
        value = self._est_current_value()
        attrs = getattr(self, "extra_state_attributes", None)
        available = getattr(self, "available", True)
        if (
            value == self._est_last_value
            and attrs == self._est_last_attrs
            and available == self._est_last_available
        ):
            return False
        self._est_last_value = value
        self._est_last_attrs = attrs
        self._est_last_available = available
        return True

    def _est_reset_cache(self) -> None:
        """Clear cached publish state so the next call always writes."""
        self._est_last_value = _UNSET
        self._est_last_attrs = _UNSET
        self._est_last_available = _UNSET


class DedupCoordinatorSensor(
    WriteDedupMixin,
    CoordinatorEntity[EntityStateTrackerCoordinator],
    SensorEntity,
):
    """``SensorEntity`` base that skips unchanged coordinator-driven writes."""

    def _est_current_value(self) -> Any:
        return self.native_value

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write only when the published value, attrs, or availability changed."""
        if self._est_should_write():
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clear cache on removal so a re-added instance writes its first state."""
        self._est_reset_cache()
        await super().async_will_remove_from_hass()


class DedupCoordinatorBinarySensor(
    WriteDedupMixin,
    CoordinatorEntity[EntityStateTrackerCoordinator],
    BinarySensorEntity,
):
    """``BinarySensorEntity`` base with the same dedup behavior."""

    def _est_current_value(self) -> Any:
        return self.is_on

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write only when the published value, attrs, or availability changed."""
        if self._est_should_write():
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clear cache on removal so a re-added instance writes its first state."""
        self._est_reset_cache()
        await super().async_will_remove_from_hass()
