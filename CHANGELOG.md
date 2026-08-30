# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-29

Initial release.

### Added
- **Two tracking modes via config flow** — **specific-states** (pick the states to track) and **all-states** (auto-discover every state the entity visits). Selected from a menu; one entity per config entry.
- **Multi-frame output from one pick** — duration/breakdown sensors for `today`, `yesterday`, `24h`, `7d`, `30d`, `month`, and `year`, each toggleable. `today`, `yesterday`, `24h`, `7d` on by default; `30d`, `month`, `year` off by default (they exceed recorder retention and fill in over time). Which frames are calendar-aligned vs rolling is noted in the frames table.
- **Specific-states sensors** — a duration sensor per enabled frame (seconds, `device_class: duration`, `state_class: measurement`, suggested display in hours) with `percent`, `compliance_percent` (when a target is set), `tracked_states`, `window_start`, `data_start`, `window_coverage`, `has_gap`, and transition metrics as attributes.
- **Compliance** (specific mode) — declare a target set of desired states (`heat` **or** `auto`); the percentage becomes a compliance score. An optional 0–100 threshold spawns a `compliant` binary sensor.
- **`currently_in_state` binary sensor** — ON while the entity is in one of the tracked states.
- **All-states breakdown** — one breakdown sensor per enabled frame whose state is the dominant state and whose attributes carry `breakdown_seconds`, `breakdown_pct`, `counts`, `avg_duration` per state (plus `previous_state`, `window_seconds`, `unaccounted_seconds`, `data_start`, `window_coverage`, `has_gap`). Every state literal — including `unavailable`, `unknown`, `none` — is its own row against a single wall-clock denominator.
- **Runtime new-state handling** — a previously-unseen state becomes a new breakdown key accumulating from first-seen, with no entity created and no restart. Fires an `entity_state_tracker_new_state` event and a persistent notification the first time a new state appears.
- **Transition metrics** — per-state entry count, average visit duration, and previous-state, riding the same event stream.
- **Persisted daily-bucket ledger** — closed local days stored via HA `Store`, so long windows survive recorder purge and HA restarts. Backfill on start recomputes days missed while HA was down; a `reset_ledger` service clears it.
- **DST-correct math** — every percentage divides by the real elapsed seconds of the window (never a fixed 86,400), so 23h/25h DST days read correctly.
- **Glitch filter** — optional `min_state_duration` (default 0) merges sub-threshold visits into the preceding block, keeping durations and transition counts clean.
- **Custom Lovelace card** — bars, pie/donut, and table views with deterministic per-state colours; auto-installed as a Lovelace resource, with graceful degradation on YAML-mode dashboards.
- **`entity_state_tracker.reset_ledger` service** — clears a tracker's persisted ledger (requires `confirm: true`).
- **Diagnostics** — dumps ledger stats, coverage, and gap flags for support.
- **Options flow** — edit states, frames, target, and glitch filter after creation (within-mode only).
- **29 translation locales** for config, options, entity, selector, and service strings (Lovelace card English-only).
- Recorder-friendly writes (unrecorded breakdown attributes, rounded/deduplicated sensor states) budgeted at ~250–400 KB/yr per tracker.
