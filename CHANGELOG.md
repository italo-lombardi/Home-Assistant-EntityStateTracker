# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4]

### Fixed
- **State name case mismatch (critical)** — tracked states were stored lowercase by the config flow but HA zone/person entity states are title-cased (e.g. `"Casa Buonabitacolo"`). The case-sensitive match in `_subset_percent` returned 0% for all tracked states, routing all time to the "other" slice. `accumulate_blocks` now lowercases state at source; `compute_frame` retains a defense-in-depth norm loop for legacy ledger files.
- **Ledger migration** — `last_state`, daily bucket keys, and `last_entered`/`last_exited` keys loaded from disk (written by older versions with raw HA casing) are now lowercased on read, closing post-restart coalesce gaps and stale transition timestamps.
- **State write normalisation** — coordinator lowercases state strings on every write path (`ledger.last_state`, `_fold_visit`, `_live_today_blocks`, `_overlay_open_visit` idempotency guard, `tracked_states`/`target_states` at load).
- **Spurious new-state events on restart** — `_ledger_seen_states` was seeding `_seen` with raw-cased backfill keys; lowercase normalisation fixes false "new state" fires for title-cased zone states after restart.
- **`breakdown_pct["unaccounted"]` overflow** — clamped to `max(0.0, ...)` so a seam overflow never produces a negative unaccounted slice.
- **Config flow whitespace** — state strings from the selector are now stripped before lowercasing, preventing `" home"` ≠ `"home"` mismatches from user-typed entries.

### Changed
- **State labels now title-cased in card** — a `toLabel()` helper converts stored lowercase keys to human-readable labels: `"casa nonna antonietta"` → `"Casa Nonna Antonietta"`, `"not_home"` → `"Not Home"`. Applied to legend, table rows, bar labels, tooltip, and tracked-states header.
- **Config flow state picker shows human-readable labels** — the "pick states to track" selector now displays `"Casa Buonabitacolo"` / `"Not Home"` instead of raw lowercase keys. Labels are auto-generated (`toLabel()`); stored values remain lowercase. Applies to both tracked-states and compliance-target selectors.
- **"No data" slice threshold** — shown whenever `unaccounted_seconds > 10s` (previously only when `has_gap` was True). Catches mid-window recorder outages (DB restart, recorder error) that leave real gaps without setting `has_gap`. The 10s threshold absorbs normal recorder commit lag (~2–5s) on slow hardware.
- **"In progress" slice removed** — open-frame lag (recorder commit latency ~5s) was silently inflated by the case-mismatch bug. Now that state time is correctly attributed, only genuine gaps (> 10s unaccounted) show a "No data" slice.
- **Poll interval reduced to 1 minute** — `SCAN_INTERVAL` changed from 5 min to 1 min so long-running visits on idle entities update the donut at most 1 minute behind. State-change accuracy remains event-driven (< 100 ms).

## [0.1.3]

### Added
- **Multi-frame picker on every card type** — the card editor now has a **Frames** checklist (pick any subset of the tracker's frames), applied to bars, pie, and table alike. Pie draws one donut per selected frame; bars/table limit their rows to the picked frames. A fresh card opens with every frame the tracker publishes pre-checked; unchecking all falls back to showing all frames. Replaces the pie-only single-frame dropdown; a card still carrying the legacy `frame:` key keeps working and migrates on first edit.

### Changed
- **Table/pie/bars states sorted by share, descending** — per-state rows and slices now lead with the biggest state instead of sorting alphabetically, so the table reads top-down by percentage and the table's top-5 cap keeps the states that actually matter.

## [0.1.2]

### Added
- **Editable tracked states** — the tracked-state set can now be changed after creation from the Edit Tracker (options) flow; history recomputes retroactively from the stored ledger, no migration.
- **Per-state breakdown in specific mode** — the duration sensor now exposes a `breakdown_seconds` / `breakdown_pct` map for the tracked states, so the card's pie, bars, and table draw one slice/row per tracked state instead of a single summed slice.
- **Chart tooltips** — hover (or tap) any bar segment, pie slice, or stacked region to see its state, duration, and percentage; edge-aware flipping keeps the tooltip on-screen.
- **Table: frame totals + optional per-state breakdown** — the table now leads with a frame-total row per enabled frame (Frame · Duration · %, plus Compliance when a target is set). A new **Show per-state breakdown** option (off by default) adds a per-state table under each frame; a **Limit to 5 states per frame** option (on by default) folds surplus states into a "… N more" row.

### Changed
- **Stacked all-states bar** — the all-states bar is now stacked per observed state (each with its own tooltip) plus a derived "No data"/"In progress" tail for uncomputed time, instead of one dominant fill over a bare track.
- **Zero-second state discovery** — a tracked state that opens a frame is seeded at zero seconds so it appears immediately, before any duration accrues.

## [0.1.1]

### Added
- **`last_week` and `last_month` frames** — the previous full Monday–Sunday week and the previous full calendar month, both closed windows (fixed start and end, ending at a past local midnight). Off by default; toggle per tracker in the config/options flow and the Lovelace card. Frame order pairs each with its to-date sibling: `week`, `last_week`, … `month`, `last_month`.

## [0.1.0]

Initial release.

### Added
- **Two tracking modes via config flow** — **specific-states** (pick the states to track) and **all-states** (auto-discover every state the entity visits). Selected from a menu; one entity per config entry. Multiple trackers on the same entity are allowed.
- **Multi-frame output from one pick** — duration/breakdown sensors for `today`, `yesterday`, `24h`, `week`, `7d`, `30d`, `month`, and `year`, each toggleable. `today`, `yesterday`, `24h`, `7d` on by default; `week`, `30d`, `month`, `year` off by default (`week` to keep the default set lean, `30d`/`month`/`year` because they exceed recorder retention and fill in over time).
- **Specific-states sensors** — a duration sensor per enabled frame (seconds, `device_class: duration`, `state_class: measurement`, suggested display in hours) with `percent`, `compliance_percent` (when a target is set), `tracked_states`, `window_start`, `data_start`, `window_coverage`, `has_gap`, and transition metrics as attributes.
- **Compliance** (specific mode) — declare a target set of desired states, independent of the tracked states; the percentage becomes a compliance score. An optional 0–100 threshold spawns a `compliant` binary sensor that also exposes `compliance_percent`, `target`, `target_threshold`, and `frame` attributes.
- **"In a Tracked State" binary sensor** — ON (On/Off) while the entity is in one of the tracked states.
- **All-states breakdown** — one breakdown sensor per enabled frame whose state is the dominant state and whose attributes carry `breakdown_seconds`, `breakdown_pct` (balanced to sum to 100 with an `unaccounted` entry), `counts`, `avg_duration_seconds` per state (plus `previous_state`, `window_seconds`, `unaccounted_seconds`, `data_start`, `window_coverage`, `has_gap`). Every state literal — including `unavailable`, `unknown`, `none` — is its own row against a single wall-clock denominator.
- **Runtime new-state handling** — a previously-unseen state becomes a new breakdown key accumulating from first-seen, with no entity created and no restart. Fires an `entity_state_tracker_new_state` event (event-only — build your own notification in an automation).
- **Transition metrics** — per-state entry count, average visit duration, and previous-state, riding the same event stream.
- **Persisted daily-bucket ledger** — closed local days stored via HA `Store`, so long windows survive recorder purge and HA restarts. Backfill on start recomputes days missed while HA was down. `24h`/`7d` are computed from the recorder for accuracy; changing the glitch filter re-backfills the ledger with the new threshold.
- **DST-correct math** — every percentage divides by the real elapsed seconds of the window (never a fixed 86,400), so 23h/25h DST days read correctly.
- **Glitch filter** — optional `min_state_duration` (default 0) merges sub-threshold visits into the preceding block, keeping durations and transition counts clean.
- **Custom Lovelace card** — bars, pie/donut, and table views with a visual editor and deterministic per-state colours; auto-installed as a Lovelace resource, with graceful degradation on YAML-mode dashboards.
- **Diagnostics** — dumps ledger stats, coverage, and gap flags for support.
- **Options flow** — edit states, frames, target, and glitch filter after creation (within-mode only).
- **Real translations across 29 locales** for config, options, entity, and selector strings (Lovelace card English-only).
- Recorder-friendly writes (unrecorded breakdown attributes, rounded/deduplicated sensor states) budgeted at ~250–400 KB/yr per tracker.
