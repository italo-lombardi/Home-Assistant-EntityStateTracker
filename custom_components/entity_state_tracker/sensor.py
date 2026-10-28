"""Sensor platform for Entity State Tracker.

Two shapes, chosen by ``coordinator.mode`` (§5):

* **specific-states** — one :class:`DurationSensor` per enabled frame (tracked
  seconds, ``MEASUREMENT``). Its ``percent`` and — when a target set is
  configured — ``compliance_percent`` ride along as attributes (queryable via
  templates); they are ALSO promoted to their own :class:`PercentSensor` /
  :class:`ComplianceSensor` per frame (``%``, ``MEASUREMENT``, ``DIAGNOSTIC``)
  so ``numeric_state`` triggers, history graphs and long-term Statistics — none
  of which can target an attribute — can bind to them.
* **all-states** — one :class:`BreakdownSensor` per enabled frame. Its state is
  the dominant (max-duration) state; the per-state dicts live in attributes and
  are stripped from the recorder (``_unrecorded_attributes``) because they churn
  every tick and our ledger is the history store, not HA long-term statistics
  (§5.3).

All entities use ``_attr_has_entity_name`` + a per-metric ``translation_key``,
with the frame surfaced via ``translation_placeholders`` so one string set
covers every frame. They share one service device per config entry and gate
``available`` on ``coordinator.last_update_success`` (the ``CoordinatorEntity``
default).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MODE_SPECIFIC,
    TRANSLATION_KEY_BREAKDOWN,
    TRANSLATION_KEY_COMPLIANCE,
    TRANSLATION_KEY_DURATION,
    TRANSLATION_KEY_PERCENT,
)
from .coordinator import EntityStateTrackerCoordinator
from .helpers import (
    frame_entity_id,
    frame_label,
    humanize_duration,
    match_state,
    normalize_state,
    resolve_wanted,
    tracker_device_name,
    unique_id,
    zone_label_map,
)
from .models import FrameResult
from .write_dedup import DedupCoordinatorSensor

_ZONE_STATE_DOMAINS = ("person", "device_tracker")


def _zone_labels(
    hass: HomeAssistant | None, source_entity: str | None, keys: list[str]
) -> dict[str, str]:
    """Map each breakdown key that names a LIVE zone -> its real friendly name.

    Resolved fresh from ``hass`` on every attribute read, NEVER stored -- so a
    zone rename is reflected the next refresh with no migration. Keys with no
    live-zone match are omitted; a consumer falls back to its own title-casing
    for those (unchanged behaviour). Lets templates / other dashboards show the
    real "Zone ... (Suffix)" instead of the lossy lowercased storage key.

    ONLY person/device_tracker get zone resolution -- their state IS a zone
    name. For any other domain a state that merely collides with a zone's name
    (a climate "away", a vacuum state) must NOT be rewritten (mirrors the config
    flow's ``_ZONE_STATE_DOMAINS`` gate).
    """
    if hass is None or source_entity is None:
        return {}
    if source_entity.split(".", 1)[0] not in _ZONE_STATE_DOMAINS:
        return {}
    zmap = zone_label_map(
        [
            z.attributes.get("friendly_name") or z.entity_id.split(".", 1)[1]
            for z in hass.states.async_all("zone")
        ]
    )
    out: dict[str, str] = {}
    for k in keys:
        real = zmap.get(normalize_state(k))
        if real is not None:
            out[k] = real
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the tracker's per-frame sensors for the config entry."""
    from . import disable_orphaned_entities

    entities: list[Entity] = []

    coordinator: EntityStateTrackerCoordinator = hass.data[DOMAIN][entry.entry_id]

    if coordinator.mode == MODE_SPECIFIC:
        for frame in coordinator.enabled_frames:
            entities.append(DurationSensor(coordinator, frame))
            # Promote percent (and compliance, when a target set is configured)
            # to their own sensors so numeric_state triggers, long-term
            # Statistics and history graphs can bind to them — they exist only as
            # attributes on the duration sensor otherwise (§5.1). Specific mode
            # only: all-states has no single percent.
            entities.append(PercentSensor(coordinator, frame))
            if coordinator.target_states:
                entities.append(ComplianceSensor(coordinator, frame))
    else:
        entities.extend(
            BreakdownSensor(coordinator, frame) for frame in coordinator.enabled_frames
        )

    async_add_entities(entities)
    # AFTER the add: the valid set is the entities just added, so a reload that
    # re-adds a frame can't race its own disable. Do not move above the add.
    disable_orphaned_entities(
        hass, entry, "sensor", {e.unique_id for e in entities if e.unique_id}
    )


def _device_info(coordinator: EntityStateTrackerCoordinator) -> DeviceInfo:
    """Return the shared service device for every entity of one tracker (§5)."""
    label = coordinator.entry.title or coordinator.entity_id
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=tracker_device_name(label),
        manufacturer="Entity State Tracker",
        entry_type=DeviceEntryType.SERVICE,
    )


class _FrameSensor(DedupCoordinatorSensor):
    """Base for a sensor scoped to one frame of a tracker.

    Wires unique_id/device_info/translation once; subclasses set the metric and
    provide ``native_value``/``extra_state_attributes``.
    """

    _attr_has_entity_name = True

    _metric: str
    _translation_key: str

    def __init__(self, coordinator: EntityStateTrackerCoordinator, frame: str) -> None:
        """Initialize the frame-scoped sensor."""
        super().__init__(coordinator)
        self._frame = frame
        self._attr_unique_id = unique_id(
            coordinator.entry.entry_id, frame, self._metric
        )
        # Pin entity_id to the id==slugify(name) default (helpers.frame_entity_id),
        # namespaced by the tracker NAME (entry.title), never the entry_id ULID —
        # an internal id must not leak into a public entity_id.
        # With has_entity_name=True, HA would otherwise slugify the (custom) DEVICE
        # name into the object_id, so a custom-named tracker would get an
        # unpredictable id. Pinning gives a stable, convention-matching default that
        # does not drift when the device is renamed. The card discovers by device_id
        # + translation_key (not this id string), so a user is free to rename it.
        # TRADEOFF: on existing installs this sets the entity_id once (history
        # survives via unique_id in the registry; hardcoded dashboard/template refs
        # to any OLD slugified id break). Accepted at v0.1.0 (see CHANGELOG).
        self.entity_id = frame_entity_id(
            coordinator.entry.title or coordinator.entity_id, frame, self._metric
        )
        self._attr_translation_key = self._translation_key
        self._attr_translation_placeholders = {"frame": frame_label(frame)}
        self._attr_device_info = _device_info(coordinator)

    @property
    def _result(self) -> FrameResult | None:
        """Return this frame's computed result, or ``None`` before first data."""
        data = self.coordinator.data
        if data is None:
            return None
        return data.frames.get(self._frame)


def _tracked_seconds(result: FrameResult, tracked: list[str] | None) -> float:
    """Sum the tracked states' seconds (all recorded states when none declared).

    Returns the RAW float seconds — no minute-flooring. Earlier versions floored
    to whole minutes to coarsen the state for recorder hash-dedup, but that made
    the headline value read up to 59s LOWER than a plain ``history_stats`` sensor
    over the same window (and the ``suggested_display_precision=1`` display
    compounded it) — defeating the integration's purpose of an *accurate* long-run
    duration. Measurement showed the floor saved zero recorder rows anyway: every
    open frame carries ``window_seconds = (now − start)`` as an attribute, so the
    write-dedup mixin already fires a write every tick regardless of the state
    value — the floored state was never the recorder's dedup key. So we now emit
    the exact seconds; the state equals ``duration_seconds`` and the raw
    ``breakdown_seconds`` sum, matching ``history_stats`` to the second. The UI
    shows ``suggested_display_precision=2`` hours (0.01 h ≈ 36 s), matching the
    standard tool's display granularity.
    """
    if tracked is None:
        total = sum(result.breakdown_seconds.values())
    else:
        # Resolve each CONFIG state to the REAL recorded key (match_state folds
        # the underscore/space + case mismatch) and sum each resolved key ONCE —
        # resolve_wanted dedups the many-to-one fold so the headline can't
        # double-count a state and diverge above the set-based percent.
        real_keys = list(result.breakdown_seconds)
        total = sum(
            result.breakdown_seconds.get(real, 0.0)
            for real in resolve_wanted(tracked, real_keys)
        )
    # Round to milliseconds: far finer than history_stats' second-granularity, but
    # tames the float-sum tail (e.g. 32021.287099999463) so the stored state
    # string / history graph / API read clean, not an ugly long float.
    return round(total, 3)


def _transition_metrics(
    result: FrameResult,
    coordinator: EntityStateTrackerCoordinator,
    states: list[str] | None,
) -> dict[str, Any]:
    """Return the §7 transition metrics for ``states`` (all rows when ``None``).

    ``last_entered`` / ``last_exited`` are tracker-global (frame-independent),
    so they are emitted whole from the coordinator data rather than sliced to
    ``states`` — consistent with the flat ``{state: iso_ts}`` shape the plan
    specifies.
    """
    data = coordinator.data
    if states is None:
        # All-states mode: keys ARE the real recorded names — pure identity, same
        # as before the match change. Never route through match_state here: it is
        # many-to-one (not_home / "not home" fold together), so resolving an
        # already-real key could mis-map it onto a different fold-equal key.
        keys = list(result.counts)
        return {
            "counts": {s: result.counts.get(s, 0) for s in keys},
            "avg_duration_seconds": {s: result.avg_duration.get(s) for s in keys},
            "previous_state": data.previous_state if data is not None else None,
            "last_entered": dict(data.last_entered) if data is not None else {},
            "last_exited": dict(data.last_exited) if data is not None else {},
        }
    # Specific mode: resolve each CONFIG key to the REAL recorded key via the
    # canonical form so counts/avg land on a separator-variant state
    # ("city_centre" → recorded "city centre"). Keep the config key so the card
    # (mapping over tracked_states) still finds it. match_state prefers an EXACT
    # recorded key over a fold, so a config value equal to a real state can't be
    # mis-mapped onto a fold-equal sibling.
    real_keys = list(result.counts)
    resolved = {s: (match_state(s, real_keys) or s) for s in states}
    return {
        "counts": {s: result.counts.get(real, 0) for s, real in resolved.items()},
        "avg_duration_seconds": {
            s: result.avg_duration.get(real) for s, real in resolved.items()
        },
        "previous_state": data.previous_state if data is not None else None,
        "last_entered": dict(data.last_entered) if data is not None else {},
        "last_exited": dict(data.last_exited) if data is not None else {},
    }


class DurationSensor(_FrameSensor):
    """Seconds spent in the tracked states over one frame (specific mode, §5.1)."""

    _metric = TRANSLATION_KEY_DURATION
    _translation_key = TRANSLATION_KEY_DURATION
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.HOURS
    # 2 dp of hours (0.01 h ≈ 36 s) matches the display granularity of a plain
    # history_stats sensor; 1 dp (0.1 h = 6 min) read coarser than the standard
    # tool. Pure display hint — never changes the stored value or statistics.
    _attr_suggested_display_precision = 2
    # MEASUREMENT on every frame — our ledger + card are the history store, not
    # HA long-term statistics, so TOTAL/last_reset buys nothing (§5.1).
    _attr_state_class = SensorStateClass.MEASUREMENT

    # Strip the volatile attributes from the recorder (mirrors BreakdownSensor,
    # §5.3): the transition metrics + percent/compliance/coverage churn on
    # essentially every transition on the today frame, so recording them writes
    # a state_attributes row per changed tick and defeats hash-dedup. percent /
    # compliance_percent stay queryable here as attributes (via templates), just
    # not recorded. tracked_states/target_states/source_entity are config and
    # stay recorded. The ledger holds the history; live UI/templates still read
    # attrs.
    _unrecorded_attributes = frozenset(
        {
            "counts",
            "avg_duration_seconds",
            "previous_state",
            "last_entered",
            "last_exited",
            "percent",
            "compliance_percent",
            "duration_seconds",
            "duration_text",
            # Per-state breakdown churns every tick like the all-states
            # BreakdownSensor's dicts — strip from the recorder (§5.3).
            "breakdown_seconds",
            "breakdown_pct",
            "breakdown_text",
            "breakdown_labels",
            "window_start",
            "data_start",
            "window_coverage",
            "has_gap",
            "window_seconds",
            "unaccounted_seconds",
        }
    )

    @property
    def native_value(self) -> float | None:
        """Return tracked-state seconds this frame (``None`` before first data)."""
        result = self._result
        if result is None:
            return None
        return _tracked_seconds(result, self.coordinator.tracked_states)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return source entity, frame, percent, compliance, bounds, transitions."""
        result = self._result
        if result is None:
            return None
        attrs: dict[str, Any] = {
            "source_entity": self.coordinator.entity_id,
            "frame": self._frame,
            "percent": result.percent,
            # Raw tracked seconds, independent of HA's native→suggested unit
            # conversion on the STATE (which serves hours). The card reads this
            # for an unambiguous seconds figure; identical to native_value.
            "duration_seconds": _tracked_seconds(
                result, self.coordinator.tracked_states
            ),
            # Humanized two-unit representations (ms/hm/dh/wd) of the tracked
            # total, for templates/cards that want the pretty "18h 33m" string
            # HA only renders in the frontend. Built from the SAME
            # _tracked_seconds as duration_seconds above (which resolves config→
            # real via match_state) so the two public attrs can never diverge for
            # a separator-drifted config — the earlier raw .get(s) sum here missed
            # the recorded key and showed "0s" while duration_seconds was correct.
            "duration_text": humanize_duration(
                _tracked_seconds(result, self.coordinator.tracked_states)
            ),
            "tracked_states": self.coordinator.tracked_states,
            "target_states": self.coordinator.target_states,
            "window_start": result.window_start,
            "data_start": result.data_start,
            "window_coverage": result.window_coverage,
            "has_gap": result.has_gap,
            # Total frame span + the slice no recorded state covers (partial
            # window / in-progress today). The card builds the specific-mode pie
            # from the tracked-only breakdown_seconds below (one slice per tracked
            # state), plus "other" (non-tracked time) and "no-data"/"in-progress"
            # derived from window_seconds - in-state - unaccounted, instead of
            # deriving a "rest" from percent (§5.1/§5.2).
            "window_seconds": result.window_seconds,
            "unaccounted_seconds": result.unaccounted_seconds,
        }
        # Per-state seconds/pct sliced to the tracked set (the card draws one
        # slice per tracked state). Kept tracked-only — a "specific" sensor must
        # not leak non-tracked states. tracked_states is None only for a
        # never-configured entry (impossible for a created specific tracker), and
        # its presence is also the card's specific-vs-all-states discriminator.
        tracked = self.coordinator.tracked_states
        if tracked is not None:
            # Slice keyed by the CONFIG state, value pulled from the REAL recorded
            # key via the canonical form, so "city_centre" surfaces the recorded
            # "city centre" seconds. Keeping the config key means the card (which
            # maps over tracked_states) finds each value with no JS change; the
            # displayed label is identical either way.
            real_keys = list(result.breakdown_seconds)
            matched = {s: match_state(s, real_keys) for s in tracked}
            attrs["breakdown_seconds"] = {
                s: result.breakdown_seconds.get(real, 0.0)
                for s, real in matched.items()
            }
            attrs["breakdown_pct"] = {
                s: (result.breakdown_pct.get(real) if real is not None else None)
                for s, real in matched.items()
            }
            # Per-state humanized reps, parallel to breakdown_seconds (same keys,
            # same tracked-only slice; a 0s tracked state reads "0m 0s"/… etc).
            attrs["breakdown_text"] = {
                s: humanize_duration(result.breakdown_seconds.get(real, 0.0))
                for s, real in matched.items()
            }
            # Real zone friendly names for any tracked key that names a live zone
            # (resolved fresh — honours a zone rename). Omits non-zone keys.
            labels = _zone_labels(self.hass, self.coordinator.entity_id, list(tracked))
            if labels:
                attrs["breakdown_labels"] = labels
        if self.coordinator.target_states:
            attrs["compliance_percent"] = result.compliance_percent
            attrs["target_threshold"] = self.coordinator.target_threshold
        attrs.update(
            _transition_metrics(
                result, self.coordinator, self.coordinator.tracked_states
            )
        )
        return attrs


class _PercentFrameSensor(_FrameSensor):
    """Base for the standalone percent-scale sensors (§5.1).

    Percent and compliance share one descriptor contract — a ``%`` measurement
    on the 0–100 scale. They are marked ``DIAGNOSTIC`` (fleet convention — the
    siblings' secondary percent sensors are diagnostic) rather than
    disabled-by-default: a disabled ``MEASUREMENT`` sensor accrues no long-term
    Statistics until manually enabled, defeating the reason to promote these off
    the duration sensor's attributes. There is deliberately no
    ``SensorDeviceClass.PERCENTAGE`` (HA has none) and no borrowed
    BATTERY/HUMIDITY class — a bare ``%`` unit + MEASUREMENT is the HA-correct
    shape. The single per-state percent lives ONLY on the duration sensor's
    attributes still (unchanged); these promote the WINDOW percent to a bindable
    entity. Subclasses provide the ``FrameResult`` field they read.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = None
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        """Return the frame's percent (0–100, 1 dp), ``None`` before first data.

        Rounded to 1 dp so idle coordinator ticks hash-dedup and don't spam the
        recorder as a growing-denominator frame drifts the ratio each tick (§5).
        """
        result = self._result
        if result is None:
            return None
        value = self._percent_value(result)
        return round(value, 1) if value is not None else None


class PercentSensor(_PercentFrameSensor):
    """Share of one frame spent in the tracked states, as a % entity (§5.1)."""

    _metric = TRANSLATION_KEY_PERCENT
    _translation_key = TRANSLATION_KEY_PERCENT
    _attr_icon = "mdi:percent-outline"

    def _percent_value(self, result: FrameResult) -> float | None:
        return result.percent


class ComplianceSensor(_PercentFrameSensor):
    """Share of one frame spent in the target set, as a % entity (§5.1).

    Created only when a target set is configured (same gate as the duration
    sensor's ``compliance_percent`` attribute).
    """

    _metric = TRANSLATION_KEY_COMPLIANCE
    _translation_key = TRANSLATION_KEY_COMPLIANCE
    _attr_icon = "mdi:shield-check-outline"

    def _percent_value(self, result: FrameResult) -> float | None:
        return result.compliance_percent


class BreakdownSensor(_FrameSensor):
    """Per-state breakdown for one frame (all-states mode, §5.2).

    State is the dominant (max-duration) state — coordinator applies hysteresis
    so near-ties don't flip it every tick. Every per-state dict lives in
    attributes and is stripped from the recorder: they change ~every minute, so
    recording them would defeat the recorder's hash-dedup and write ~525k
    ``state_attributes`` rows/entity/yr. The ledger holds the history instead;
    live UI/templates still read the attributes (§5.3).
    """

    _metric = TRANSLATION_KEY_BREAKDOWN
    _translation_key = TRANSLATION_KEY_BREAKDOWN
    _attr_icon = "mdi:chart-donut"

    _unrecorded_attributes = frozenset(
        {
            "breakdown_seconds",
            "breakdown_pct",
            "breakdown_text",
            "breakdown_labels",
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

    @property
    def native_value(self) -> str | None:
        """Return the dominant state name (``None`` before first data)."""
        result = self._result
        return result.dominant if result is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return source entity, frame + full per-state breakdown, sorted desc."""
        result = self._result
        if result is None:
            return None
        order = sorted(
            result.breakdown_seconds,
            key=lambda s: result.breakdown_seconds[s],
            reverse=True,
        )
        # Per-state dicts stay pure (real states only, sorted desc). breakdown_pct
        # additionally carries the engine's additive "unaccounted" key so a
        # template looping it sums to ~100 (it has no breakdown_seconds entry, so
        # it is appended last rather than picked up by ``order``).
        breakdown_pct = {s: result.breakdown_pct.get(s) for s in order}
        breakdown_pct["unaccounted"] = result.breakdown_pct.get("unaccounted")
        # Real zone friendly names for any breakdown key that names a live zone
        # (resolved fresh — honours a zone rename). Omits non-zone keys.
        labels = _zone_labels(self.hass, self.coordinator.entity_id, order)
        attrs = {
            "source_entity": self.coordinator.entity_id,
            "frame": self._frame,
            "breakdown_seconds": {s: result.breakdown_seconds[s] for s in order},
            "breakdown_pct": breakdown_pct,
            # Per-state humanized reps (ms/hm/dh/wd), same desc order + keys as
            # breakdown_seconds — the all-states parallel to the DurationSensor's
            # breakdown_text, so both modes expose the pretty string identically.
            "breakdown_text": {
                s: humanize_duration(result.breakdown_seconds[s]) for s in order
            },
            "counts": {s: result.counts.get(s, 0) for s in order},
            "avg_duration_seconds": {s: result.avg_duration.get(s) for s in order},
            "previous_state": self.coordinator.data.previous_state
            if self.coordinator.data is not None
            else None,
            # Tracker-global {state: iso_ts} — same on every frame's sensor (§7).
            "last_entered": dict(self.coordinator.data.last_entered)
            if self.coordinator.data is not None
            else {},
            "last_exited": dict(self.coordinator.data.last_exited)
            if self.coordinator.data is not None
            else {},
            "window_seconds": result.window_seconds,
            "data_start": result.data_start,
            "window_coverage": result.window_coverage,
            "has_gap": result.has_gap,
            "unaccounted_seconds": result.unaccounted_seconds,
        }
        if labels:
            attrs["breakdown_labels"] = labels
        return attrs
