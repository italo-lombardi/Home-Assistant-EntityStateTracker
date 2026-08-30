"""Binary sensor platform for Entity State Tracker.

Two binary sensors, both conditional (§5.1):

* **Currently in state** — specific mode only. ``on`` while the entity's live
  state is one of the tracked states. Read straight off the coordinator's
  ``last_state`` so it flips the moment a transition folds into the ledger.
* **Compliant** — only when a compliance ``target_threshold`` is configured.
  ``on`` when today's compliance percentage meets or exceeds that threshold.

Both ride :class:`~.write_dedup.DedupCoordinatorBinarySensor`, so an idle 5-min
coordinator tick that recomputes an unchanged value writes no recorder row.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MODE_SPECIFIC,
    TRANSLATION_KEY_COMPLIANT,
    TRANSLATION_KEY_CURRENTLY_IN_STATE,
)
from .coordinator import EntityStateTrackerCoordinator
from .helpers import binary_entity_id, tracker_device_name, unique_id
from .write_dedup import DedupCoordinatorBinarySensor

# The compliant sensor keys today's frame — the only window whose compliance is
# "now" rather than a historical span. "today" is a default-on frame, but a user
# may disable it; the sensor then falls back to the first enabled frame so it
# never references a frame the coordinator isn't computing.
_TODAY = "today"


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
    # Compliant only exists when a pass/fail threshold was declared.
    if coordinator.target_threshold is not None:
        entities.append(CompliantBinarySensor(coordinator))

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

    def __init__(self, coordinator: EntityStateTrackerCoordinator) -> None:
        """Initialize the currently-in-state binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = unique_id(
            coordinator.entry.entry_id, "", TRANSLATION_KEY_CURRENTLY_IN_STATE
        )
        # Pin entity_id so it shares the tracker's card-discoverable stem (see
        # sensor.py _FrameSensor.__init__ for the rationale + v0.1.0 tradeoff).
        self.entity_id = binary_entity_id(
            coordinator.entry.entry_id, TRANSLATION_KEY_CURRENTLY_IN_STATE
        )
        self._attr_device_info = _device_info(coordinator)

    @property
    def is_on(self) -> bool:
        """Return True when the current state is one of the tracked states."""
        data = self.coordinator.data
        tracked = self.coordinator.tracked_states or ()
        return data is not None and data.last_state in tracked


class CompliantBinarySensor(DedupCoordinatorBinarySensor):
    """ON when today's compliance percentage meets the configured threshold.

    Frame note: this sensor scores the ``today`` frame — the only window whose
    compliance is "now" rather than a historical span. ``today`` is default-on,
    but a user MAY disable it; when it is off, the sensor falls back to the FIRST
    enabled frame (``enabled_frames[0]``, canonical order today→…→year). So with
    ``today`` disabled, "compliant" can silently mean e.g. year-compliance —
    whichever enabled frame comes first in canonical order. The active frame is
    always surfaced in the ``frame`` extra-state attribute so the user can see
    which window is being scored. Keep ``today`` enabled if you want the sensor
    to track live/day compliance.
    """

    _attr_has_entity_name = True
    _attr_translation_key = TRANSLATION_KEY_COMPLIANT

    # compliance_percent churns on essentially every transition on today's frame,
    # so strip it from the recorder (mirrors DurationSensor/BreakdownSensor, §5.3)
    # — it stays queryable as a live attribute; our ledger is the history store.
    # target / target_threshold / frame are config-stable and stay recorded.
    _unrecorded_attributes = frozenset({"compliance_percent"})

    def __init__(self, coordinator: EntityStateTrackerCoordinator) -> None:
        """Initialize the compliant binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = unique_id(
            coordinator.entry.entry_id, "", TRANSLATION_KEY_COMPLIANT
        )
        # Pin entity_id so it shares the tracker's card-discoverable stem (see
        # sensor.py _FrameSensor.__init__ for the rationale + v0.1.0 tradeoff).
        self.entity_id = binary_entity_id(
            coordinator.entry.entry_id, TRANSLATION_KEY_COMPLIANT
        )
        self._attr_device_info = _device_info(coordinator)
        # Prefer today's frame; fall back to whichever frame is first enabled so
        # the sensor always reads a frame the coordinator actually computes.
        self._frame_key = (
            _TODAY
            if _TODAY in coordinator.enabled_frames
            else (
                coordinator.enabled_frames[0] if coordinator.enabled_frames else _TODAY
            )
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when compliance meets the threshold, None when unknown.

        Scores ``self._frame_key`` — ``today`` when enabled, else the first
        enabled frame (see the class docstring): with ``today`` off this may be a
        long window, so the score is that frame's compliance, not the live day.
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
        always sees what bar is being applied to which window.
        """
        data = self.coordinator.data
        frame = data.frames.get(self._frame_key) if data is not None else None
        return {
            "compliance_percent": frame.compliance_percent
            if frame is not None
            else None,
            "target": self.coordinator.target_states,
            "target_threshold": self.coordinator.target_threshold,
            "frame": self._frame_key,
        }
