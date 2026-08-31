# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

Initial release.

### Added
- **Two tracking modes via config flow** — **specific-states** (pick the states to track) and **all-states** (auto-discover every state the entity visits). Selected from a menu; one entity per config entry. Multiple trackers on the same entity are allowed.
- **Multi-frame output from one pick** — duration/breakdown sensors for `today`, `yesterday`, `24h`, `7d`, `30d`, `month`, and `year`, each toggleable. `today`, `yesterday`, `24h`, `7d` on by default; `30d`, `month`, `year` off by default (they exceed recorder retention and fill in over time).
- **Specific-states sensors** — a duration sensor per enabled frame (seconds, `device_class: duration`, `state_class: measurement`, suggested display in hours) with `percent`, `compliance_percent` (when a target is set), `tracked_states`, `window_start`, `data_start`, `window_coverage`, `has_gap`, and transition metrics as attributes.
- **Compliance** (specific mode) — declare a target set of desired states, independent of the tracked states; the percentage becomes a compliance score. An optional 0–100 threshold spawns a `compliant` binary sensor that also exposes `compliance_percent`, `target`, `target_threshold`, and `frame` attributes.
- **`currently_in_state` binary sensor** — ON (On/Off) while the entity is in one of the tracked states.
- **All-states breakdown** — one breakdown sensor per enabled frame whose state is the dominant state and whose attributes carry `breakdown_seconds`, `breakdown_pct` (balanced to sum to 100 with an `unaccounted` entry), `counts`, `avg_duration_seconds` per state (plus `previous_state`, `window_seconds`, `unaccounted_seconds`, `data_start`, `window_coverage`, `has_gap`). Every state literal — including `unavailable`, `unknown`, `none` — is its own row against a single wall-clock denominator.
- **Runtime new-state handling** — a previously-unseen state becomes a new breakdown key accumulating from first-seen, with no entity created and no restart. Fires an `entity_state_tracker_new_state` event (event-only — build your own notification in an automation).
- **Transition metrics** — per-state entry count, average visit duration, and previous-state, riding the same event stream.
- **Persisted daily-bucket ledger** — closed local days stored via HA `Store`, so long windows survive recorder purge and HA restarts. Backfill on start recomputes days missed while HA was down. `24h`/`7d` are computed from the recorder for accuracy; changing the glitch filter re-backfills the ledger with the new threshold.
- **DST-correct math** — every percentage divides by the real elapsed seconds of the window (never a fixed 86,400), so 23h/25h DST days read correctly.
- **Glitch filter** — optional `min_state_duration` (default 0) merges sub-threshold visits into the preceding block, keeping durations and transition counts clean.
- **Custom Lovelace card** — bars, pie/donut, and table views with a visual editor and deterministic per-state colours; auto-installed as a Lovelace resource, with graceful degradation on YAML-mode dashboards.
- **`entity_state_tracker.reset_ledger` service** — clears a tracker's persisted ledger (requires `confirm: true`); optional `entity_id` targets specific trackers, otherwise resets all.
- **Diagnostics** — dumps ledger stats, coverage, and gap flags for support.
- **Options flow** — edit states, frames, target, and glitch filter after creation (within-mode only).
- **Real translations across 29 locales** for config, options, entity, selector, and service strings (Lovelace card English-only).
- Recorder-friendly writes (unrecorded breakdown attributes, rounded/deduplicated sensor states) budgeted at ~250–400 KB/yr per tracker.
