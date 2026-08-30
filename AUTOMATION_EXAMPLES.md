# Entity State Tracker — Automation Examples

Ready-to-adapt automations for every feature. The examples use placeholder entity IDs — replace them with the entities Entity State Tracker created for your tracker.

> **Find your exact entity IDs:** every entity carries the tracker's name, so IDs read as `sensor.entity_state_tracker_<your_entity>_<metric>_<frame>`. Open **Settings → Devices & Services → Entity State Tracker → your tracker's device** and copy the IDs from there — don't hand-assemble them.

**How the IDs are built.** Each entity's name is the metric followed by the frame in parentheses — e.g. *Duration (Last 7 days)*, *State Breakdown (Last 24 hours)* — and Home Assistant slugifies that into the entity ID (parentheses dropped, spaces to underscores). So for a tracker on `light.garage`:

| Entity name | Example entity ID |
|-------------|-------------------|
| Duration (Today) | `sensor.entity_state_tracker_garage_light_duration_today` |
| Duration (Last 7 days) | `sensor.entity_state_tracker_garage_light_duration_last_7_days` |
| State Breakdown (Last 24 hours) | `sensor.entity_state_tracker_garage_light_state_breakdown_last_24_hours` |
| Currently in State | `binary_sensor.entity_state_tracker_garage_light_currently_in_state` |
| Compliant | `binary_sensor.entity_state_tracker_garage_light_compliant` |

> **Tip:** the tracked-state **percent** and **compliance percent** are attributes on each duration sensor (not their own entities). Watch them with a `template` trigger — e.g. `state_attr('sensor..._duration_today', 'percent')` — or read them in templates.

---

## What you can trigger on

| Kind | Trigger style | Notes |
|------|---------------|-------|
| Today's percentage (specific mode) | `template` | `state_attr('sensor..._duration_today', 'percent')` |
| Today's compliance (specific mode, target set) | `template` | `state_attr('sensor..._duration_today', 'compliance_percent')` |
| Compliant / not compliant (target threshold set) | `state` | `binary_sensor..._compliant`, `on`/`off` |
| Currently in a tracked state (specific mode) | `state` | `binary_sensor..._currently_in_state`, `on`/`off` |
| A brand-new state appeared (all-states mode) | `event` | `entity_state_tracker_new_state` |
| A per-state breakdown value (all-states mode) | `template` | Reads `breakdown_pct` / `counts` attributes off the breakdown sensor |

---

## Percentage & compliance (specific mode)

Today's percentage and compliance live as the `percent` and `compliance_percent` attributes on the today duration sensor. A `numeric_state` trigger can't watch an attribute, so use a `template` trigger.

---

### Automation 1 — Alert when a light is on too much of the day

Say you track `light.garage` with tracked state `on`. Warn when it has been on for more than 60% of today.

```yaml
automation:
  alias: EST — garage light on too long today
  trigger:
    - platform: template
      value_template: >
        {{ state_attr('sensor.entity_state_tracker_garage_light_duration_today',
           'percent') | float(0) > 60 }}
      for: "00:10:00"
  action:
    - service: notify.mobile_app_my_phone
      data:
        title: "Garage light on too much of today"
        message: >
          The garage light has been on for
          {{ state_attr('sensor.entity_state_tracker_garage_light_duration_today',
             'percent') }}% of today. Left on by mistake?
```

---

### Automation 2 — Thermostat compliance dropped below target

You track `climate.living_room` with a target set of `heat` / `auto` and want to know when the thermostat spent too little of the day in an approved mode.

```yaml
automation:
  alias: EST — thermostat compliance low
  trigger:
    - platform: template
      value_template: >
        {{ state_attr('sensor.entity_state_tracker_living_room_thermostat_duration_today',
           'compliance_percent') | float(100) < 80 }}
  action:
    - service: notify.mobile_app_my_phone
      data:
        title: Thermostat compliance low
        message: >
          Living room thermostat compliance is only
          {{ state_attr('sensor.entity_state_tracker_living_room_thermostat_duration_today',
             'compliance_percent') }}% today (target: heat/auto).
```

---

### Automation 3 — React to the `compliant` binary sensor

When a target threshold is configured, the tracker publishes a `compliant` binary sensor that is `on` while today's compliance is at or above the threshold (it uses the `today` frame, or the first enabled frame if `today` is off). This is the cleanest trigger — no template comparison in the automation.

```yaml
automation:
  alias: EST — fell out of compliance
  trigger:
    - platform: state
      entity_id: binary_sensor.entity_state_tracker_living_room_thermostat_compliant
      from: "on"
      to: "off"
      for: "00:15:00"
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          Thermostat has been out of its target modes for 15 min
          (compliance today:
          {{ state_attr('sensor.entity_state_tracker_living_room_thermostat_duration_today',
             'compliance_percent') }}%).
```

---

### Automation 4 — Currently-in-state binary sensor

`currently_in_state` is `on` while the entity is in one of the tracked states right now — useful for "is it in a tracked state, and has it been for a while" logic. It has no device class, so it simply reads `on`/`off`.

```yaml
automation:
  alias: EST — pump on notice
  trigger:
    - platform: state
      entity_id: binary_sensor.entity_state_tracker_well_pump_currently_in_state
      to: "on"
      for: "01:00:00"
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: The well pump has been in a tracked state continuously for an hour.
```

---

## New-state events (all-states mode)

In all-states mode a previously-unseen state becomes a new breakdown key on the fly — no restart, no config change — and an `entity_state_tracker_new_state` event **always** fires. You don't need to enable anything: the event is emitted every time an all-states tracker records a state it has never seen before.

| Event | Fired when | Data |
|-------|-----------|------|
| `entity_state_tracker_new_state` | An all-states tracker sees a state it has never recorded before | `entry_id`, `entity_id`, `state` (the newly-seen state) |

---

### Automation 5 — Announce a never-before-seen state

Handy for catching a device that has started reporting an unexpected state (a new error code, a firmware-added mode).

```yaml
automation:
  alias: EST — new state discovered
  trigger:
    - platform: event
      event_type: entity_state_tracker_new_state
  action:
    - service: notify.mobile_app_my_phone
      data:
        title: New state seen
        message: >
          {{ trigger.event.data.entity_id }} just reported a state never
          seen before: "{{ trigger.event.data.state }}".
```

---

### Automation 6 — React only to a specific tracker's new states

Filter by `entry_id` (find it in **Settings → Integrations →** the entry's URL) when you only care about one tracker.

```yaml
automation:
  alias: EST — washer new state only
  trigger:
    - platform: event
      event_type: entity_state_tracker_new_state
      event_data:
        entry_id: "your_tracker_entry_id_here"
  action:
    - service: persistent_notification.create
      data:
        title: Washer reported a new state
        message: >
          New state "{{ trigger.event.data.state }}" on
          {{ trigger.event.data.entity_id }} — add it to a dashboard if it matters.
```

---

## Breakdown attributes (all-states mode)

Each `sensor..._state_breakdown_<frame>` carries the full per-state breakdown in its attributes. `breakdown_pct` is a `{state: percent}` dict, `breakdown_seconds` is `{state: seconds}`, `counts` is `{state: entries}`, and `avg_duration` is `{state: seconds}`. The sensor's own state is the dominant (longest-duration) state for that frame.

> **Note:** breakdown attributes are unrecorded (they change roughly every minute), so they don't bloat the recorder — but templates and `template` triggers read them live just fine.

---

### Automation 7 — Trigger on a specific state's share of the window

Fire when the vacuum spent more than 30% of the last 24 hours in `error`.

```yaml
automation:
  alias: EST — vacuum error share high
  trigger:
    - platform: template
      value_template: >
        {{ state_attr('sensor.entity_state_tracker_vacuum_state_breakdown_last_24_hours',
                      'breakdown_pct').get('error', 0) | float(0) > 30 }}
  action:
    - service: notify.mobile_app_my_phone
      data:
        title: Vacuum spent a lot of time in error
        message: >
          Vacuum was in "error" for
          {{ state_attr('sensor.entity_state_tracker_vacuum_state_breakdown_last_24_hours',
                        'breakdown_pct')['error'] }}% of the last 24 h.
```

---

### Automation 8 — Daily state-breakdown report

Post a top-3 breakdown for the day each evening. Sorts the `breakdown_pct` dict by share.

```yaml
automation:
  alias: EST — daily state breakdown
  trigger:
    - platform: time
      at: "22:00:00"
  action:
    - variables:
        pct: >
          {{ state_attr('sensor.entity_state_tracker_boiler_state_breakdown_today',
                        'breakdown_pct') }}
    - service: notify.mobile_app_my_phone
      data:
        title: Boiler — today's state breakdown
        message: >
          {% for state, share in (pct.items() | sort(attribute='1', reverse=True))
             | list | slice(3) | first %}
          {{ state }}: {{ share }}%
          {% endfor %}
```

---

### Automation 9 — Flag frequent flapping via transition counts

`counts` holds the number of entries into each state this frame. Alert when a door has been opened more than 40 times today.

```yaml
automation:
  alias: EST — door opened too often
  trigger:
    - platform: template
      value_template: >
        {{ state_attr('sensor.entity_state_tracker_front_door_state_breakdown_today',
                      'counts').get('open', 0) | int(0) > 40 }}
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          Front door opened
          {{ state_attr('sensor.entity_state_tracker_front_door_state_breakdown_today',
                        'counts')['open'] }} times today
          (avg open
          {{ (state_attr('sensor.entity_state_tracker_front_door_state_breakdown_today',
                         'avg_duration')['open'] | float(0) / 60) | round(1) }} min).
```

---

## Duration sensors & coverage

The duration sensor for a frame reports seconds in the tracked states (displayed in hours by default). Its `data_start`, `window_coverage`, and `has_gap` attributes tell you whether the window is fully backed by data yet — important right after you enable a long frame like `Last 30 days` or `This year`.

---

### Automation 10 — Weekly time-in-state summary

```yaml
automation:
  alias: EST — weekly heating summary
  trigger:
    - platform: time
      at: "09:00:00"
  condition:
    - condition: time
      weekday: [mon]
  action:
    - service: notify.mobile_app_my_phone
      data:
        title: Heating — last 7 days
        message: >
          Boiler ran for
          {{ (states('sensor.entity_state_tracker_boiler_duration_last_7_days') | float(0)
              / 3600) | round(1) }} h in the last 7 days
          ({{ state_attr('sensor.entity_state_tracker_boiler_duration_last_7_days',
                         'percent') }}% of the window).
```

---

### Automation 11 — Warn only once the window is fully covered

Skip the alert while a freshly-enabled long frame is still filling in — guard on `has_gap` (or require `window_coverage` to reach 1.0). This avoids acting on an incomplete window.

```yaml
automation:
  alias: EST — 30d underuse (only when data complete)
  trigger:
    - platform: numeric_state
      entity_id: sensor.entity_state_tracker_solar_pump_duration_last_30_days
      below: 3600          # ran less than 1 h in 30 days
  condition:
    - condition: template
      value_template: >
        {{ not state_attr('sensor.entity_state_tracker_solar_pump_duration_last_30_days',
                          'has_gap') }}
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          Solar pump ran under an hour in the last 30 days —
          window is fully covered since
          {{ state_attr('sensor.entity_state_tracker_solar_pump_duration_last_30_days',
                        'data_start') }}.
```

---

### Automation 12 — Only act when the window is at least 90% covered

`window_coverage` is a 0..1 fraction of the frame that is backed by real data. Use it directly when you want a softer guard than "no gap at all".

```yaml
automation:
  alias: EST — monthly report (skip if too little data)
  trigger:
    - platform: time
      at: "08:00:00"
  condition:
    - condition: template
      value_template: >
        {{ state_attr('sensor.entity_state_tracker_boiler_duration_this_month',
                      'window_coverage') | float(0) >= 0.9 }}
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          Boiler ran
          {{ (states('sensor.entity_state_tracker_boiler_duration_this_month') | float(0)
              / 3600) | round(1) }} h this month so far.
```

---

## Services

### `entity_state_tracker.reset_ledger`

Clears the persisted daily-bucket ledger and starts long-window accumulation fresh. Use it after you deliberately changed how an entity behaves and don't want the old history dragging the long frames. `confirm: true` is **required** — without it the call raises a validation error rather than silently wiping history, because history beyond recorder retention cannot be rebuilt.

> With no `entity_id`, the reset applies to **every** Entity State Tracker config entry (each entry owns its own store, and all loaded trackers are reset together). Pass an optional `entity_id` — the *tracked* entity, not the tracker's own sensor — to reset only the tracker(s) watching that entity. One tracked entity can have several trackers (specific vs all-states, different names); all of them reset. A target that matches no loaded tracker raises a validation error.

```yaml
service: entity_state_tracker.reset_ledger
data:
  confirm: true
```

Reset just one tracked entity's trackers:

```yaml
service: entity_state_tracker.reset_ledger
data:
  confirm: true
  entity_id: climate.living_room
```

---

### Automation 13 — Reset the ledger after a hardware swap

Triggered by a helper toggle you flip when you replace the tracked device.

```yaml
automation:
  alias: EST — reset ledger on device swap
  trigger:
    - platform: state
      entity_id: input_boolean.device_replaced
      to: "on"
  action:
    - service: entity_state_tracker.reset_ledger
      data:
        confirm: true
    - service: input_boolean.turn_off
      target:
        entity_id: input_boolean.device_replaced
```

---

## Notification channels

### Telegram

```yaml
automation:
  alias: EST — new state to Telegram
  trigger:
    - platform: event
      event_type: entity_state_tracker_new_state
  action:
    - service: notify.telegram
      data:
        message: >-
          *{{ trigger.event.data.entity_id }}* reported new state
          `{{ trigger.event.data.state }}`.
```

### TTS announcement on speaker

```yaml
automation:
  alias: EST — announce compliance drop
  trigger:
    - platform: state
      entity_id: binary_sensor.entity_state_tracker_living_room_thermostat_compliant
      to: "off"
  action:
    - service: tts.speak
      target:
        entity_id: tts.piper
      data:
        media_player_entity_id: media_player.living_room
        message: The thermostat has fallen out of its target modes.
```

---

## Advanced patterns

### One notification per 10-minute window

Suppresses repeated new-state alerts using a helper timestamp.

> **Prerequisites:** create `input_datetime.last_est_notification` with "has time" enabled.

```yaml
automation:
  alias: EST — new state with cooldown
  trigger:
    - platform: event
      event_type: entity_state_tracker_new_state
  condition:
    - condition: template
      value_template: >-
        {% set last = states('input_datetime.last_est_notification') %}
        {% set last_dt = last | as_datetime if last not in ['unknown','unavailable']
           else (now() - timedelta(hours=1)) %}
        {{ (now() - last_dt).total_seconds() > 600 }}
  action:
    - service: input_datetime.set_datetime
      target:
        entity_id: input_datetime.last_est_notification
      data:
        datetime: "{{ now().strftime('%Y-%m-%d %H:%M:%S') }}"
    - service: notify.mobile_app_my_phone
      data:
        message: >
          New state "{{ trigger.event.data.state }}" on
          {{ trigger.event.data.entity_id }}.
```

### `mode: queued` guidance

`mode: queued` is only needed when a single automation could re-trigger before the previous run finishes. The `entity_state_tracker_new_state` event fires once per newly-seen state, so with an `entry_id` or `event_data` filter re-entrance is rare — you can usually leave `mode` at its default. Keep `queued` if your automation has no filter and includes a long `delay:` or `wait_template:`.
