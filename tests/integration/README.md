# Integration smoke tests

Live tests against a running Home Assistant instance. Run after deploying
changes to `custom_components/entity_state_tracker/`.

Replace `<container>` with your HA container name and `<ha-config-checkout>`
with the working directory of your HA config throughout.

## Why run against the HA host

HA is reachable at `http://localhost:8123` **on the machine/container running
HA**; a remote host's `localhost` does not reach it. Run the smoke script where
HA runs (e.g. inside the container, as shown below). Set `EST_SMOKE_BASE_URL`
if HA is reachable at a different URL.

## Quick run

```bash
# 1. Deploy the integration to HA's config/custom_components
docker cp custom_components/entity_state_tracker/. \
  <container>:<ha-config-checkout>/config/custom_components/entity_state_tracker/

# 2. Copy the smoke script in
docker cp tests/integration/smoke.py <container>:/tmp/smoke.py

# 3. Run with a long-lived access token (see "Get a token" below)
docker exec -e EST_SMOKE_TOKEN="<long-lived-token>" <container> \
  python3 /tmp/smoke.py

# Fast mode (45 s wait_for timeouts instead of 60 s):
docker exec -e EST_SMOKE_TOKEN="<long-lived-token>" -e EST_SMOKE_FAST=1 <container> \
  python3 /tmp/smoke.py

# Run a subset of edge cases (comma-separated EC numbers):
docker exec -e EST_SMOKE_TOKEN="<long-lived-token>" -e EST_SMOKE_EC="5,6,7" <container> \
  python3 /tmp/smoke.py

# Keep the config entries this run creates (default: delete them at the end):
docker exec -e EST_SMOKE_TOKEN="<long-lived-token>" -e EST_SMOKE_KEEP=1 <container> \
  python3 /tmp/smoke.py
```

If `python3` is not on `PATH` in your setup, substitute the full path to the
Python interpreter that runs HA (e.g. your HA virtualenv's `bin/python3`).

## Get a token

Create a long-lived access token from the HA UI:

1. Click your user (bottom-left) to open your profile.
2. Go to the **Security** tab.
3. Under **Long-Lived Access Tokens**, click **Create Token**, name it, and
   copy the value — it is shown only once.

Pass it as `EST_SMOKE_TOKEN`.

## EC12 (restart persistence) configuration

EC12 restarts HA in-place to prove the ledger survives a restart, so it needs
to know how to relaunch HA. It is **skipped** unless you point it at your HA
working directory via env vars:

```bash
docker exec \
  -e EST_SMOKE_TOKEN="<long-lived-token>" \
  -e EST_SMOKE_HA_DIR="<ha-config-checkout>" \
  <container> python3 /tmp/smoke.py
```

- `EST_SMOKE_HA_DIR` — HA working directory to `cd` into before relaunch
  (required for EC12; unset → EC12 skips with a notice).
- `EST_SMOKE_PYTHON` — Python interpreter used to relaunch HA (default
  `python3`; set to your HA venv's `bin/python` if needed).
- `EST_SMOKE_HA_CONFIG` — config dir passed to HA's `-c` flag (default
  `./config`, relative to `EST_SMOKE_HA_DIR`).

## How it works — no hardcoded IDs

The test creates its own dedicated tracked entities (via `POST /api/states`,
namespaced `sensor.est_smoke_*_<run>`) and its own config entries (via the REST
config-flow), then discovers each emitted EST entity through the
**entity-registry websocket** by its predictable `unique_id`
(`<entry_id>_<frame>_<metric>` for frame sensors, `<entry_id>__<metric>` for the
binary sensors). No entity_id from the live HA is assumed. Every entry it creates
is deleted at the end unless `EST_SMOKE_KEEP=1`.

## Dependencies

`websocket-client` (present in a standard HA environment) is required for the
entity-registry discovery, the `entity_state_tracker_new_state` event capture
(EC6), the Lovelace-resource check (EC15) and the persistent-notification check
(EC6). If it is absent, discovery-dependent ECs cannot run.

## What is covered

| EC | Scenario |
|----|----------|
| EC1 | Specific tracker (no compliance) → a DurationSensor per enabled frame; state numeric |
| EC2 | Time in a tracked state → today duration rises; CurrentlyInState = on |
| EC3 | Non-tracked state → CurrentlyInState = off; duration does not accrue |
| EC4 | Compliance enabled → Compliant binary sensor exists; Compliant flips as threshold crosses the computed compliance_percent (read off the duration sensor's `compliance_percent` attribute) |
| EC5 | All-states tracker → a BreakdownSensor per frame; dominant = current state |
| EC6 | New state at runtime → breakdown gains the key, `entity_state_tracker_new_state` event fires with `{entry_id, entity_id, state}`, persistent_notification created |
| EC7 | `unavailable` / `unknown` counted as ordinary breakdown rows |
| EC8 | `min_state_duration=5` → sub-5 s "flicker" visits filtered (count stays 0) |
| EC9 | Breakdown attrs are `_unrecorded` — present in live `/api/states`, excluded from recorder history |
| EC10 | `reset_ledger` confirm gate: `confirm:false` errors + no change; `confirm:true` clears the ledger (day_count → 0) |
| EC11 | Options-flow toggles a frame off → entry reloads; that frame's sensor stops being produced (unavailable) while a kept frame stays live |
| EC12 | Restart persistence: accrue, restart HA, ledger survives (per-state seconds + day_count retained) |
| EC13 | `window_coverage` / `has_gap` fields present + internally consistent (`has_gap ⟺ data_start known AND coverage<1`) |
| EC14 | Dominant hysteresis: a sub-margin near-tie does not flip the dominant state |
| EC15 | Card JS served (200) and registered as a Lovelace resource |

## Notes on semantics that shape the tests

- **percent / compliance_percent are shares of the whole window**, not of tracked
  time: `matched_secs / window_seconds`. A live test cannot move "today" percent
  to 80 % in seconds, so EC4 tests the Compliant flip by moving the *threshold*
  across the computed percent (the real invariant `is_on == percent >= threshold`).
- **Duration sensors display in hours** (`suggested_unit_of_measurement`), so 8 s
  reads as ~0.002; native unit is seconds.
- **The glitch filter lives in the recorder-backed accumulation** (`accumulate_blocks`);
  the "today" frame recomputes from the recorder each tick, so sub-threshold visits
  are filtered there. EC8 settles firmly in one state before asserting to avoid a
  tick landing mid-flick.
- **HA keeps orphaned registry entries** when an integration stops producing an
  entity; EC11 asserts the sensor goes `unavailable`, not that the registry row is
  deleted.
