# Entity State Tracker for Home Assistant

Track how long any entity spends in each of its states — across many time frames at once — and keep those numbers even after the recorder purges its history.

Point it at one entity, pick a mode, and it produces a bundle of duration, percentage, compliance, and transition sensors for `today`, `yesterday`, `24h`, `week`, `last_week`, `7d`, `30d`, `month`, `last_month`, and `year` from a single config-flow pick. A custom Lovelace card renders it as bars, a pie/donut, or a dense multi-frame table.

## Features

- **Two modes** — **specific-states** (pick states → duration + % + optional compliance score) or **all-states** (auto-discovers every state → per-state breakdown). Chosen from a menu; no YAML.
- **Many frames from one pick** — `today`, `yesterday`, `24h`, `week`, `last_week`, `7d`, `30d`, `month`, `last_month`, `year`, each toggleable.
- **Survives recorder purge** — a self-managed daily-bucket ledger keeps `30d` / `month` / `year` windows correct past the recorder's ~10-day retention; partial windows are flagged, never silently wrong.
- **Compliance** — declare a target set of desired states and the percentage becomes a score, with an optional threshold that spawns a `compliant` binary sensor.
- **Transitions** — per-state entry count, average visit duration, last-seen, and previous-state.
- **Auto-discovered breakdown** — a new state seen at runtime just becomes a new key (no restart), and fires an `entity_state_tracker_new_state` event.
- **DST-correct** — percentages divide by real elapsed window seconds, so a 23h/25h DST day still reads 100%.
- **Recorder-friendly** — churny breakdown dicts are unrecorded; only sensor states record, rounded so idle ticks don't create rows.
- **Custom Lovelace card** — bars, pie/donut, or table; deterministic per-state colours; auto-installed.
- **Survives HA restarts** — closed days persist in `.storage`; the open window recomputes from the recorder on start.

## Setup

1. Install via HACS.
2. Go to **Settings → Devices & Services → Add Integration**.
3. Search for **Entity State Tracker**.
4. Pick an entity, choose a mode, then select frames.

> **Note:** `30d`, `month`, and `year` frames are off by default and fill in over time — when first enabled they only show data from that point forward plus whatever the recorder still holds.

> This is an unofficial integration not affiliated with Home Assistant.
