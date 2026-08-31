"""Binary sensor platform for Entity State Tracker.

Conditional binary sensors (§5.1):

* **Currently in state** — specific mode only. ``on`` while the entity's live
  state is one of the tracked states. Read straight off HA's state machine
  (``hass.states.get``), so it is correct the instant the entry loads — even
  across a restart with no transition — and repaints on the debounce a state
  change schedules. (It deliberately does NOT read the coordinator ledger's
  ``last_state``, which lags the real entity on boot until the next fold.)
* **Compliant** — one per enabled frame, only when a compliance
  ``target_threshold`` is configured. Each is ``on`` when its frame's compliance
  percentage meets or exceeds that threshold.

Both ride :class:`~.write_dedup.DedupCoordinatorBinarySensor`, so an idle 5-min
coordinator tick that recomputes an unchanged value writes no recorder row.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    DOMAIN,
    MODE_SPECIFIC,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
)
from .coordinator import EntityStateTrackerCoordinator
from .helpers import (
    binary_entity_id,
    binary_frame_entity_id,
    frame_label,
    tracker_device_name,
    unique_id,
)
from .write_dedup import DedupCoordinatorBinarySensor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Entity State Tracker binary sensors for a config entry."""
    coordinator: EntityStateTrackerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[DedupCoordinatorBinarySensor] = []
    # Currently-in-state only makes sense when we know which states count.
    if coordinator.mode == MODE_SPECIFIC and coordinator.tracked_states:
        entities.append(CurrentlyInStateBinarySensor(coordinator))
    # One Compliant per enabled frame — only when a pass/fail threshold exists.
    if coordinator.target_threshold is not None:
        entities.extend(
            CompliantBinarySensor(coordinator, frame)
            for frame in coordinator.enabled_frames
        )

    async_add_entities(entities)


def _device_info(coordinator: EntityStateTrackerCoordinator) -> DeviceInfo:
    """Build the shared per-entry service DeviceInfo (§5).

    Derives the label identically to the sensor platform (``entry.title`` with
    the entity_id as fallback): both platforms register the SAME device
    (identifiers ``{(DOMAIN, entry_id)}``), so a differing name would flap the
    device's display name on reload by setup order. ``entry.title`` is stable —
    unlike ``friendly_name``, which is unavailable when the tracked entity is
    unloaded.
    """
    label = coordinator.entry.title or coordinator.entity_id
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=tracker_device_name(label),
        manufacturer="Entity State Tracker",
        entry_type=DeviceEntryType.SERVICE,
    )


class CurrentlyInStateBinarySensor(DedupCoordinatorBinarySensor):
    """ON while the tracked entity's live state is one of the tracked states."""

    _attr_has_entity_name = True
    _attr_translation_key = TRANSLATION_KEY_CURRENTLY_IN_STATE

    # This sensor is frame-agnostic — it reads the live last_state, not a window
    # — so it carries no frame/coverage attributes (they'd be meaningless here).
    # current_state names which state is live; it churns every transition, so
    # strip it from the recorder. source_entity/tracked_states are config-stable
    # and stay recorded.
    _unrecorded_attributes = frozenset({"current_state"})

    def __init__(self, coordinator: EntityStateTrackerCoordinator) -> None:
        """Initialize the currently-in-state binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = unique_id(
            coordinator.entry.entry_id, "", TRANSLATION_KEY_CURRENTLY_IN_STATE
        )
        # Pin entity_id to the id==slugify(name) default, namespaced by the tracker
        # NAME (entry.title), never the entry_id ULID (see sensor.py _FrameSensor
        # for the full rationale + v0.1.0 tradeoff). The card discovers by device_id
        # + translation_key, not this id, so it's renameable.
        self.entity_id = binary_entity_id(
            coordinator.entry.title or coordinator.entity_id,
            TRANSLATION_KEY_CURRENTLY_IN_STATE,
        )
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Repaint immediately on every source-entity state change.

        ``is_on``/``current_state`` read the source's LIVE state, but the base
        class only writes on coordinator ticks (~5 min) — so between the source
        changing and the next tick the published state lags, and right after a
        restart the sensor can publish before the source entity has been
        restored (reading it as unavailable). Subscribing to the source's
        ``state_changed`` and writing on each edge closes both windows: the
        sensor tracks the live state the instant it moves, and refires as soon
        as the source appears post-boot.
        """
        await super().async_added_to_hass()

        @callback
        def _source_changed(_event: Event[EventStateChangedData]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.coordinator.entity_id], _source_changed
            )
        )
        # Publish once now so a source that is already settled at add-time (e.g.
        # unchanged across a restart, so no post-add edge fires) is reflected
        # without waiting for the first coordinator tick.
        self.async_write_ha_state()

    @property
    def _live_state(self) -> str | None:
        """Return the tracked entity's live state, or None if unavailable.

        Reads HA's state machine directly rather than the coordinator's ledger
        ``last_state``. The ledger anchor only advances on a folded transition
        (or the HA-start seed), so on boot — or after a reset that left it
        ``None`` — it lags the real entity until the next transition, which
        surfaced as the sensor showing the wrong Off/On until the entity next
        changed. The live state machine is always current, matching this
        sensor's "live state" contract (class docstring).
        """
        state = self.coordinator.hass.states.get(self.coordinator.entity_id)
        return state.state if state is not None else None

    @property
    def is_on(self) -> bool:
        """Return True when the live state is one of the tracked states."""
        tracked = self.coordinator.tracked_states or ()
        return self._live_state in tracked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the source entity, tracked states, and the live current state."""
        return {
            "source_entity": self.coordinator.entity_id,
            "tracked_states": self.coordinator.tracked_states,
            "current_state": self._live_state,
        }


class CompliantBinarySensor(DedupCoordinatorBinarySensor):
    """ON when ONE frame's compliance percentage meets the configured threshold.

    One instance per enabled frame (setup loops ``coordinator.enabled_frames``),
    so a tracker with a threshold exposes e.g. "Compliant (Today)", "Compliant
    (This month)", … each scoring its own window against the same threshold.
    The scored frame is surfaced in the ``frame`` extra-state attribute.
    """

    _attr_has_entity_name = True
    _attr_translation_key = TRANSLATION_KEY_COMPLIANT

    # compliance_percent churns on essentially every transition on today's frame,
    # so strip it from the recorder (mirrors DurationSensor/BreakdownSensor, §5.3)
    # — it stays queryable as a live attribute; our ledger is the history store.
    # The coverage trio (data_start/window_coverage/has_gap) is likewise volatile
    # and stripped. source_entity/tracked_states/target_states/target_threshold/
    # frame are config-stable and stay recorded.
    _unrecorded_attributes = frozenset(
        {"compliance_percent", "data_start", "window_coverage", "has_gap"}
    )

    def __init__(self, coordinator: EntityStateTrackerCoordinator, frame: str) -> None:
        """Initialize the compliant binary sensor for one frame."""
        super().__init__(coordinator)
        self._frame_key = frame
        self._attr_unique_id = unique_id(
            coordinator.entry.entry_id, frame, TRANSLATION_KEY_COMPLIANT
        )
        # Pin entity_id to the id==slugify(name) default, namespaced by the tracker
        # NAME (entry.title), never the entry_id ULID (see sensor.py _FrameSensor
        # for the full rationale + v0.1.0 tradeoff). The card discovers by device_id
        # + translation_key, not this id, so it's renameable.
        self.entity_id = binary_frame_entity_id(
            coordinator.entry.title or coordinator.entity_id,
            frame,
            TRANSLATION_KEY_COMPLIANT,
        )
        self._attr_translation_placeholders = {"frame": frame_label(frame)}
        self._attr_device_info = _device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        """Return True when this frame's compliance meets the threshold, else None.

        ``None`` when the threshold is cleared, there is no data yet, the frame is
        absent from the computed output, or its ``compliance_percent`` is ``None``
        (N/A window).
        """
        threshold = self.coordinator.target_threshold
        data = self.coordinator.data
        if threshold is None or data is None:
            return None
        frame = data.frames.get(self._frame_key)
        if frame is None or frame.compliance_percent is None:
            return None
        return frame.compliance_percent >= threshold

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why the sensor is (non-)compliant: the score, bar, and frame.

        ``compliance_percent`` is ``None`` before first data or when its frame is
        absent — the target/threshold/frame config surface regardless so the user
        always sees what bar is being applied to which window. The common-core
        source_entity/data_start/window_coverage/has_gap ride along too.
        """
        data = self.coordinator.data
        frame = data.frames.get(self._frame_key) if data is not None else None
        return {
            "source_entity": self.coordinator.entity_id,
            "compliance_percent": frame.compliance_percent
            if frame is not None
            else None,
            "tracked_states": self.coordinator.tracked_states,
            "target_states": self.coordinator.target_states,
            "target_threshold": self.coordinator.target_threshold,
            "frame": self._frame_key,
            "data_start": frame.data_start if frame is not None else None,
            "window_coverage": frame.window_coverage if frame is not None else None,
            "has_gap": frame.has_gap if frame is not None else None,
        }
