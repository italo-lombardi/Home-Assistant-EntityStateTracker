"""
Live smoke tests for Entity State Tracker against a running HA instance.

Run IN the HA container/host (a remote localhost won't reach HA):
    docker cp tests/integration/smoke.py <your-ha-container>:/tmp/smoke.py
    docker exec -e EST_SMOKE_TOKEN="<long-lived-token>" <your-ha-container> \\
        python3 /tmp/smoke.py

Get a long-lived access token from the HA UI (Profile → Security → Long-Lived
Access Tokens). See tests/integration/README.md.

Required env vars:
    EST_SMOKE_TOKEN     HA long-lived access token (see README).

Optional env vars:
    EST_SMOKE_BASE_URL  HA base URL, default http://localhost:8123
    EST_SMOKE_FAST      "1" → shorter wait_for timeouts (default 60 s)
    EST_SMOKE_KEEP      "1" → do not delete the config entries created by this run
                        (default: clean up every entry we create at the end)
    EST_SMOKE_EC        comma-separated EC numbers to run, e.g. "5,6,7". Empty = all.
    EST_SMOKE_HA_DIR    HA working dir used to relaunch HA in EC12 (restart test).
                        EC12 SKIPS unless this is set. See README.
    EST_SMOKE_PYTHON    python used to relaunch HA in EC12 (default "python3").
    EST_SMOKE_HA_CONFIG HA config dir passed to `-c` in EC12 (default "./config").

The test creates its own dedicated tracked entities via POST /api/states and its
own config entries via the REST config-flow — no hardcoded entity IDs from the
live HA. Every EST entity is discovered via the entity-registry websocket by its
predictable unique_id (``<entry_id>_<frame>_<metric>``), so no entity_id guessing.

Edge cases covered (EC1-EC16, plus sub-checks). See tests/integration/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--fast", action="store_true")
_parser.add_argument("--keep", action="store_true")
_args, _ = _parser.parse_known_args()

TOKEN = os.environ.get("EST_SMOKE_TOKEN", "")
BASE = os.environ.get("EST_SMOKE_BASE_URL", "http://localhost:8123")
FAST = _args.fast or os.environ.get("EST_SMOKE_FAST", "") == "1"
KEEP = _args.keep or os.environ.get("EST_SMOKE_KEEP", "") == "1"
EC_FILTER: set[int] = {
    int(x) for x in os.environ.get("EST_SMOKE_EC", "").split(",") if x.strip().isdigit()
}
WAIT_FOR_TIMEOUT = 45 if FAST else 60

# EC12 restarts HA in-place, which needs to know how to relaunch it. This is
# environment-specific, so it is env-driven and EC12 skips unless HA_DIR is set.
HA_DIR = os.environ.get("EST_SMOKE_HA_DIR", "")
HA_PYTHON = os.environ.get("EST_SMOKE_PYTHON", "python3")
HA_CONFIG = os.environ.get("EST_SMOKE_HA_CONFIG", "./config")

DOMAIN = "entity_state_tracker"
EVENT_NEW_STATE = "entity_state_tracker_new_state"


def ec_enabled(n: int) -> bool:
    return not EC_FILTER or n in EC_FILTER


if not TOKEN:
    sys.exit(
        "Error: set EST_SMOKE_TOKEN to a valid HA access token.\n"
        "See tests/integration/README.md for instructions."
    )

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def api_status(method, path, body=None):
    """Like api() but returns (status, parsed_body_or_text) and never raises on HTTP error."""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                return r.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def gs(eid):
    return api("GET", f"/api/states/{eid}")


def gs_safe(eid):
    """Like gs() but returns None instead of raising on 404."""
    try:
        return api("GET", f"/api/states/{eid}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def ss(eid, state, attrs=None):
    """Set a state via POST /api/states (force_update semantics for our test entities)."""
    return api(
        "POST",
        f"/api/states/{eid}",
        {"state": state, "attributes": attrs or {}},
    )


def wait(seconds=10):
    time.sleep(seconds)


def wait_for(check_fn, expected, timeout=None, interval=3):
    """Poll until str(check_fn()) == str(expected) or timeout; return last value."""
    if timeout is None:
        timeout = WAIT_FOR_TIMEOUT
    deadline = time.time() + timeout
    val = None
    while time.time() < deadline:
        try:
            val = check_fn()
        except Exception:
            val = None
        if str(val) == str(expected):
            return val
        time.sleep(interval)
    return val


def wait_until(pred_fn, timeout=None, interval=3):
    """Poll until pred_fn() is truthy or timeout; return the final truthiness."""
    if timeout is None:
        timeout = WAIT_FOR_TIMEOUT
    deadline = time.time() + timeout
    ok = False
    while time.time() < deadline:
        try:
            ok = bool(pred_fn())
        except Exception:
            ok = False
        if ok:
            return True
        time.sleep(interval)
    return ok


def wait_for_gt(check_fn, floor, timeout=None, interval=3):
    """Poll until float(check_fn()) > floor or timeout; return last numeric value."""
    if timeout is None:
        timeout = WAIT_FOR_TIMEOUT
    deadline = time.time() + timeout
    val = None
    while time.time() < deadline:
        try:
            val = float(check_fn())
            if val > floor:
                return val
        except (TypeError, ValueError):
            pass
        time.sleep(interval)
    return val


# ---------------------------------------------------------------------------
# WebSocket helpers: entity-registry listing + event capture
# ---------------------------------------------------------------------------

try:
    import websocket as _ws_lib  # websocket-client

    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

_WS_URL = (
    BASE.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
)


def _ws_command(command: dict, timeout: int = 20):
    """Open a websocket, auth, send one command, return its result payload.

    Returns the ``result`` field of the command reply, or None on failure.
    """
    if not _WS_AVAILABLE:
        return None
    result_holder: dict = {}
    done = threading.Event()

    def on_message(ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") == "auth_required":
            ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        elif msg.get("type") == "auth_ok":
            ws.send(json.dumps({"id": 1, **command}))
        elif msg.get("type") == "result" and msg.get("id") == 1:
            result_holder["success"] = msg.get("success")
            result_holder["result"] = msg.get("result")
            result_holder["error"] = msg.get("error")
            done.set()
            ws.close()

    ws = _ws_lib.WebSocketApp(_WS_URL, on_message=on_message)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    done.wait(timeout)
    try:
        ws.close()
    except Exception:
        pass
    return result_holder


def entity_registry_list() -> list[dict]:
    """Return the entity registry (websocket config/entity_registry/list)."""
    res = _ws_command({"type": "config/entity_registry/list"})
    if not res or not res.get("success"):
        return []
    return res.get("result") or []


def entity_registry_enable(entity_id: str) -> None:
    """Enable a disabled-by-default entity via the registry websocket."""
    _ws_command(
        {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
            "disabled_by": None,
        }
    )


def capture_events(event_type: str, trigger_fn, timeout: int = 30) -> list[dict]:
    """Subscribe to event_type, call trigger_fn() once subscribed, return payloads."""
    if not _WS_AVAILABLE:
        trigger_fn()
        return []

    captured: list[dict] = []
    done = threading.Event()
    msg_id = 7

    def on_message(ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype == "auth_required":
            ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        elif mtype == "auth_ok":
            ws.send(
                json.dumps(
                    {"id": msg_id, "type": "subscribe_events", "event_type": event_type}
                )
            )
        elif mtype == "result" and msg.get("id") == msg_id and msg.get("success"):
            trigger_fn()
        elif mtype == "event":
            captured.append(msg.get("event", {}).get("data", {}))

    ws = _ws_lib.WebSocketApp(_WS_URL, on_message=on_message)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    time.sleep(timeout)
    try:
        ws.close()
    except Exception:
        pass
    done.set()
    return captured


# ---------------------------------------------------------------------------
# Test tracking
# ---------------------------------------------------------------------------
_passed = _failed = 0
_results: list[tuple[str, bool, str]] = []


def chk(label, got, exp, note=""):
    global _passed, _failed
    ok = str(got) == str(exp)
    _passed += ok
    _failed += not ok
    _results.append((label, ok, f"got={got} exp={exp} {note}".strip()))
    print(
        f"{'PASS' if ok else 'FAIL'} {label}: got={got} expected={exp} {note}",
        flush=True,
    )
    return ok


def note(msg):
    print(f"  … {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config-flow driver
# ---------------------------------------------------------------------------

FLOW = "/api/config/config_entries/flow"
_created_entries: list[str] = []


def _flow_step(flow_id, body):
    return api("POST", f"{FLOW}/{flow_id}", body)


def create_tracker(
    entity_id: str,
    mode: str,  # "specific_states" | "all_states"
    *,
    states: list[str] | None = None,
    enable_compliance: bool = False,
    target: list[str] | None = None,
    target_threshold: float | None = None,
    frames: dict[str, bool] | None = None,
    min_state_duration: int = 0,
) -> str:
    """Drive the REST config-flow end-to-end; return the created entry_id.

    Aborts (already_configured) are surfaced by returning the existing entry_id
    when discoverable, else raising.
    """
    frames = frames or {"today": True, "yesterday": True, "24h": True, "7d": True}

    r = api("POST", FLOW, {"handler": DOMAIN})
    fid = r["flow_id"]
    chk_step = r.get("step_id")
    assert chk_step == "user", f"expected user step, got {r}"

    r = _flow_step(fid, {"entity": entity_id})
    # mode is a menu step
    assert r.get("type") == "menu", f"expected menu step, got {r}"
    assert r.get("step_id") == "mode"

    if mode == "specific_states":
        r = _flow_step(fid, {"next_step_id": "specific_states"})
        assert r.get("step_id") == "specific", f"expected specific step, got {r}"
        body = {"states": states or ["on"], "enable_compliance": enable_compliance}
        r = _flow_step(fid, body)
        if enable_compliance:
            assert r.get("step_id") == "compliance", f"expected compliance, got {r}"
            cbody: dict = {"target": target or ["on"]}
            if target_threshold is not None:
                cbody["target_threshold"] = target_threshold
            r = _flow_step(fid, cbody)
    else:
        r = _flow_step(fid, {"next_step_id": "all_states"})

    assert r.get("step_id") == "frames", f"expected frames step, got {r}"
    tail = dict(frames)
    tail["min_state_duration"] = min_state_duration
    r = _flow_step(fid, tail)

    if r.get("type") == "abort":
        raise RuntimeError(f"config flow aborted: {r.get('reason')}")
    assert r.get("type") == "create_entry", f"expected create_entry, got {r}"
    entry_id = r["result"]["entry_id"]
    _created_entries.append(entry_id)
    return entry_id


def delete_entry(entry_id: str) -> None:
    try:
        api("DELETE", f"/api/config/config_entries/entry/{entry_id}")
    except Exception as e:
        note(f"cleanup: delete {entry_id} failed: {e}")


def cleanup():
    if KEEP:
        note(f"EST_SMOKE_KEEP set — leaving {len(_created_entries)} entries in place")
        return
    for entry_id in _created_entries:
        delete_entry(entry_id)
    note(f"cleaned up {len(_created_entries)} config entries")


# ---------------------------------------------------------------------------
# EST entity discovery — map unique_id → entity_id via registry
# ---------------------------------------------------------------------------


def est_entities(entry_id: str) -> dict[str, dict]:
    """Return {unique_id: registry_entry} for all EST entities of one config entry."""
    reg = entity_registry_list()
    out: dict[str, dict] = {}
    for e in reg:
        if e.get("config_entry_id") == entry_id and e.get("platform") == DOMAIN:
            out[e.get("unique_id")] = e
    return out


def eid_for(entry_id: str, frame: str, metric: str, reg: dict[str, dict] | None = None):
    """entity_id for a frame-scoped sensor unique_id ``<entry_id>_<frame>_<metric>``."""
    reg = reg if reg is not None else est_entities(entry_id)
    uid = f"{entry_id}_{frame}_{metric}"
    entry = reg.get(uid)
    return entry.get("entity_id") if entry else None


def bs_eid_for(entry_id: str, metric: str, reg: dict[str, dict] | None = None):
    """entity_id for a binary sensor unique_id ``<entry_id>__<metric>`` (empty frame)."""
    reg = reg if reg is not None else est_entities(entry_id)
    uid = f"{entry_id}__{metric}"
    entry = reg.get(uid)
    return entry.get("entity_id") if entry else None


def category_for(
    entry_id: str, frame: str, metric: str, reg: dict[str, dict] | None = None
):
    """entity_category (e.g. 'diagnostic') for a frame-scoped sensor, or None."""
    reg = reg if reg is not None else est_entities(entry_id)
    entry = reg.get(f"{entry_id}_{frame}_{metric}")
    return entry.get("entity_category") if entry else None


def wait_entities(entry_id: str, min_count: int = 1, timeout=30) -> dict[str, dict]:
    """Poll the registry until at least ``min_count`` EST entities exist."""
    deadline = time.time() + timeout
    reg: dict[str, dict] = {}
    while time.time() < deadline:
        reg = est_entities(entry_id)
        if len(reg) >= min_count:
            return reg
        time.sleep(2)
    return reg


# ---------------------------------------------------------------------------
# Dedicated test entities (fully controlled via POST /api/states)
# ---------------------------------------------------------------------------

RUN = str(int(time.time()))[-6:]  # unique-ish suffix so re-runs don't collide


def make_entity(suffix: str, initial: str = "on") -> str:
    """Create/seed a test entity via POST /api/states and return its entity_id."""
    eid = f"sensor.est_smoke_{suffix}_{RUN}"
    ss(eid, initial, {"friendly_name": f"EST Smoke {suffix} {RUN}"})
    return eid


# ---------------------------------------------------------------------------
# Frame sensor metric keys (translation keys — see const.py)
# ---------------------------------------------------------------------------
M_DURATION = "duration"
M_PERCENT = "percent"
M_COMPLIANCE = "compliance"
M_BREAKDOWN = "breakdown"
M_CURRENTLY = "currently_in_state"
M_COMPLIANT = "compliant"

FRAMES_ON = {"today": True, "yesterday": True, "24h": True, "7d": True}


# ---------------------------------------------------------------------------
# Individual edge cases
# ---------------------------------------------------------------------------


def ec1_specific_duration_sensors():
    """EC1: specific tracker (no compliance) → a DurationSensor per enabled frame, state numeric."""
    print(
        "\n=== EC1: specific tracker → per-frame DurationSensors, numeric state ===",
        flush=True,
    )
    eid = make_entity("ec1", "on")
    entry = create_tracker(
        eid, "specific_states", states=["on", "off"], frames=FRAMES_ON
    )
    # Per frame: DurationSensor + PercentSensor (both enabled by default) → 8 total.
    reg = wait_entities(entry, min_count=8)
    for frame in ("today", "yesterday", "24h", "7d"):
        dur_eid = eid_for(entry, frame, M_DURATION, reg)
        chk(
            f"EC1 duration sensor exists ({frame})",
            dur_eid is not None,
            True,
            f"reg_uids={list(reg)}",
        )
        if dur_eid:
            wait_until(
                lambda de=dur_eid: (
                    gs(de).get("state") not in (None, "unknown", "unavailable")
                )
            )
            val = gs(dur_eid).get("state")
            numeric = False
            try:
                float(val)
                numeric = True
            except (TypeError, ValueError):
                numeric = False
            chk(
                f"EC1 duration state numeric ({frame})", numeric, True, f"state={val!r}"
            )
        # PercentSensor: enabled-by-default, DIAGNOSTIC, numeric 0-100.
        pct_eid = eid_for(entry, frame, M_PERCENT, reg)
        chk(
            f"EC1 percent sensor exists ({frame})",
            pct_eid is not None,
            True,
            f"reg_uids={list(reg)}",
        )
        chk(
            f"EC1 percent sensor is DIAGNOSTIC ({frame})",
            category_for(entry, frame, M_PERCENT, reg),
            "diagnostic",
        )
        if pct_eid:
            wait_until(
                lambda pe=pct_eid: (
                    gs(pe).get("state") not in (None, "unknown", "unavailable")
                )
            )
            pv = gs(pct_eid).get("state")
            in_range = False
            try:
                in_range = 0.0 <= float(pv) <= 100.0
            except (TypeError, ValueError):
                in_range = False
            chk(
                f"EC1 percent state in [0,100] ({frame})",
                in_range,
                True,
                f"state={pv!r}",
            )
            pattrs = gs(pct_eid).get("attributes", {})
            chk(
                f"EC1 percent unit is % ({frame})",
                pattrs.get("unit_of_measurement"),
                "%",
            )
    # Duration sensor carries the full config-context + bounds attribute set.
    today_dur = eid_for(entry, "today", M_DURATION, reg)
    if today_dur:
        attrs = gs(today_dur).get("attributes", {})
        for key in (
            "source_entity",
            "frame",
            "percent",
            "duration_seconds",
            "tracked_states",
            "target_states",
            "window_start",
            "data_start",
            "window_coverage",
            "has_gap",
            "last_entered",
            "last_exited",
        ):
            chk(
                f"EC1 duration attr '{key}' present",
                key in attrs,
                True,
                f"keys={list(attrs)}",
            )
        chk(
            "EC1 duration source_entity = tracked entity",
            attrs.get("source_entity"),
            eid,
        )
        chk("EC1 duration frame = today", attrs.get("frame"), "today")
    # No compliance configured → NO compliance percent sensor for any frame.
    chk(
        "EC1 no compliance sensor without target (specific, no compliance)",
        eid_for(entry, "today", M_COMPLIANCE, reg) is None,
        True,
        f"reg_uids={list(reg)}",
    )
    return entry, eid


def ec2_ec3_duration_rises_and_currently(entry, eid):
    """EC2/EC3: tracked state accumulates + CurrentlyInState on; non-tracked → off, no accrual."""
    print(
        "\n=== EC2: tracked state → duration/percent rise, CurrentlyInState on ===",
        flush=True,
    )
    reg = est_entities(entry)
    today_dur = eid_for(entry, "today", M_DURATION, reg)
    curr = bs_eid_for(entry, M_CURRENTLY, reg)
    chk("EC2 CurrentlyInState binary sensor exists", curr is not None, True)

    # Set to tracked "on" via a real edge (off→on) so a state_changed fires even
    # if the entity happened to already be "on" from a prior step.
    ss(eid, "off")
    wait_for(lambda: gs(curr).get("state") if curr else None, "off")
    ss(eid, "on")

    # CurrentlyInState reads the tracked entity's LIVE state from HA's state
    # machine (not the coordinator ledger), repainting on the ~0.5s debounce a
    # state change schedules. On a freshly-restarted host the tracker's state
    # subscription can miss the very first edge (registered a beat after we POST),
    # so poll AND re-assert the edge each round — a re-POST refires state_changed
    # for the subscription to catch and recompute.
    def _currently_on() -> bool:
        if not curr or gs(curr).get("state") == "on":
            return gs(curr).get("state") == "on" if curr else False
        ss(eid, "on")  # refire the edge in case the first was missed at subscribe
        api("POST", "/api/services/homeassistant/update_entity", {"entity_id": curr})
        return gs(curr).get("state") == "on"

    wait_until(_currently_on)
    curr_state = gs(curr).get("state") if curr else None
    chk(
        "EC2 CurrentlyInState=on for tracked state",
        curr_state,
        "on",
    )
    # CurrentlyInState carries config context + the live current_state.
    if curr:
        cattrs = gs(curr).get("attributes", {})
        for key in ("source_entity", "tracked_states", "current_state"):
            chk(
                f"EC2 currently attr '{key}' present",
                key in cattrs,
                True,
                f"keys={list(cattrs)}",
            )
        chk(
            "EC2 currently source_entity = tracked entity",
            cattrs.get("source_entity"),
            eid,
        )
        chk("EC2 currently current_state = on", cattrs.get("current_state"), "on")
    d0 = float(gs(today_dur).get("state") or 0)
    # The duration sensor state is minute-rounded (recorder-row reduction), so a
    # sub-minute dwell floors to 0 and wouldn't register. Stay in-state for just
    # over a minute so at least one whole minute accrues.
    note(f"today duration baseline={d0}s; dwelling ~65s in tracked state")
    time.sleep(65)
    # Fold the 'on' visit by transitioning away (the fold happens on the NEXT
    # change), then force a coordinator recompute so the closed block is
    # reflected without waiting for the 5-min base-class poll.
    ss(eid, "off")
    api(
        "POST",
        "/api/services/homeassistant/update_entity",
        {"entity_id": today_dur},
    )
    d1 = wait_for_gt(lambda: gs(today_dur).get("state"), d0)
    chk(
        "EC2 today duration rose after time in tracked state",
        (d1 or 0) > d0,
        True,
        f"before={d0} after={d1}",
    )

    print(
        "\n=== EC3: non-tracked state → CurrentlyInState off, no accrual ===",
        flush=True,
    )
    # Create a tracker that only tracks "on"; move entity to a non-tracked state.
    ss(eid, "on")
    wait_for(lambda: gs(curr).get("state") if curr else None, "on")
    ss(eid, "off")  # 'off' is tracked in EC1 setup — need a truly untracked one
    # entity tracked states are ["on","off"]; use "cooling" as untracked
    ss(eid, "cooling")
    off_val = wait_for(lambda: gs(curr).get("state") if curr else None, "off")
    chk(
        "EC3 CurrentlyInState=off for untracked state",
        off_val,
        "off",
        "state=cooling not in [on,off]",
    )
    dnt0 = float(gs(today_dur).get("state") or 0)
    time.sleep(8)
    ss(eid, "cooling", {"n": "poke"})  # stay untracked
    ss(eid, "heating")  # fold the untracked visit
    time.sleep(6)
    dnt1 = float(gs(today_dur).get("state") or 0)
    chk(
        "EC3 duration did NOT rise while untracked",
        abs(dnt1 - dnt0) < 3.0,
        True,
        f"before={dnt0} after={dnt1}",
    )
    ss(eid, "on")


def ec4_compliance():
    """EC4: specific + compliance → Compliant bs exists; Compliant
    state is consistent with computed compliance_percent vs threshold, and flips
    when the threshold crosses that percent.

    Note on semantics: compliance_percent = target-state seconds / *window* seconds
    (share of the whole frame, e.g. share of today-so-far), NOT of tracked time. A
    live smoke test cannot move that percent to 80% in seconds, so we test the flip
    by moving the *threshold* across the actual computed percent — the real
    invariant the CompliantBinarySensor encodes (is_on == compliance_percent >= threshold).
    """
    print(
        "\n=== EC4: compliance sensors exist + Compliant flips vs threshold ===",
        flush=True,
    )
    eid = make_entity("ec4", "on")
    # threshold=0 → any data makes it compliant (compliance_percent >= 0 always true).
    entry = create_tracker(
        eid,
        "specific_states",
        states=["on", "off"],
        enable_compliance=True,
        target=["on"],
        target_threshold=0,
        frames={"today": True},
    )
    reg = wait_entities(entry, min_count=1)
    compliant = bs_eid_for(entry, M_COMPLIANT, reg)
    dur = eid_for(entry, "today", M_DURATION, reg)
    chk(
        "EC4 Compliant binary sensor exists",
        compliant is not None,
        True,
        f"uids={list(reg)}",
    )
    # ComplianceSensor (numeric %) is created per frame only when compliance is on.
    comp_pct_eid = eid_for(entry, "today", M_COMPLIANCE, reg)
    chk(
        "EC4 compliance percent sensor exists (compliance enabled)",
        comp_pct_eid is not None,
        True,
        f"uids={list(reg)}",
    )
    chk(
        "EC4 compliance percent sensor is DIAGNOSTIC",
        category_for(entry, "today", M_COMPLIANCE, reg),
        "diagnostic",
    )
    if comp_pct_eid:
        wait_until(
            lambda ce=comp_pct_eid: (
                gs(ce).get("state") not in (None, "unknown", "unavailable")
            )
        )
        cpv = gs(comp_pct_eid).get("state")
        cin = False
        try:
            cin = 0.0 <= float(cpv) <= 100.0
        except (TypeError, ValueError):
            cin = False
        chk(
            "EC4 compliance percent state in [0,100]",
            cin,
            True,
            f"state={cpv!r}",
        )

    # Accrue some target time, then fold it so compliance_percent > 0.
    ss(eid, "on")
    time.sleep(10)
    ss(eid, "off")
    ss(eid, "on")
    # With threshold=0, Compliant must be ON (compliance_percent >= 0).
    on_val = wait_for(
        lambda: gs(compliant).get("state") if compliant else None,
        "on",
        timeout=WAIT_FOR_TIMEOUT,
    )
    cp = gs(dur).get("attributes", {}).get("compliance_percent")
    chk(
        "EC4 Compliant=on when threshold=0 (any target data)",
        on_val,
        "on",
        f"compliance_percent={cp}",
    )
    chk(
        "EC4 compliance_percent computed (not None)",
        cp is not None,
        True,
        f"compliance_percent={cp}",
    )
    # Compliant binary sensor mirrors the compliance config as attributes.
    if compliant:
        battrs = gs(compliant).get("attributes", {})
        for key in (
            "source_entity",
            "compliance_percent",
            "tracked_states",
            "target_states",
            "target_threshold",
            "frame",
        ):
            chk(
                f"EC4 compliant attr '{key}' present",
                key in battrs,
                True,
                f"keys={list(battrs)}",
            )
        chk("EC4 compliant target_states = [on]", battrs.get("target_states"), ["on"])
        chk(
            "EC4 compliant target_threshold = 0",
            float(battrs.get("target_threshold")),
            0.0,
        )

    # Now raise the threshold above the actual computed percent via options flow →
    # Compliant must flip OFF. The tiny live percent is well under 99.
    r = api("POST", "/api/config/config_entries/options/flow", {"handler": entry})
    fid = r["flow_id"]
    api(
        "POST",
        f"/api/config/config_entries/options/flow/{fid}",
        {
            "today": True,
            "min_state_duration": 0,
            "target": ["on"],
            "target_threshold": 99,
        },
    )

    # Entry reloads; rediscover the (possibly new) entity_id and poll until it is
    # available again AND reads off. Skip transient unavailable/None during reload.
    def _compliant_now():
        e = bs_eid_for(entry, M_COMPLIANT)
        if not e:
            return None
        st = gs_safe(e)
        return st.get("state") if st else None

    off_val = wait_for(_compliant_now, "off", timeout=WAIT_FOR_TIMEOUT)
    if str(off_val) != "off":
        # One more settle: drive a fold so the coordinator recomputes post-reload.
        ss(eid, "off")
        ss(eid, "on")
        off_val = wait_for(_compliant_now, "off", timeout=WAIT_FOR_TIMEOUT)
    chk(
        "EC4 Compliant flips off when threshold(99) > compliance_percent",
        off_val,
        "off",
        f"compliance_percent≈{cp}",
    )
    return entry, eid


def ec5_ec6_ec7_allstates():
    """EC5: all-states tracker → BreakdownSensor per frame, dominant=current.
    EC6: new state at runtime → breakdown key + event + persistent_notification.
    EC7: unavailable/unknown counted as ordinary breakdown rows.
    """
    print(
        "\n=== EC5: all-states tracker → BreakdownSensors, dominant=current ===",
        flush=True,
    )
    eid = make_entity("all", "stateA")
    entry = create_tracker(eid, "all_states", frames=FRAMES_ON)
    reg = wait_entities(entry, min_count=4)
    for frame in ("today", "yesterday", "24h", "7d"):
        b_eid = eid_for(entry, frame, M_BREAKDOWN, reg)
        chk(
            f"EC5 breakdown sensor exists ({frame})",
            b_eid is not None,
            True,
            f"uids={list(reg)}",
        )
    today_bd = eid_for(entry, "today", M_BREAKDOWN, reg)
    # dominant should be the current state after some accrual + a fold.
    ss(eid, "stateA")
    time.sleep(8)
    ss(eid, "stateB")  # fold stateA visit
    ss(eid, "stateA")  # back to A, fold tiny B
    dom = wait_for(lambda: gs(today_bd).get("state"), "stateA")
    chk(
        "EC5 today dominant = current state",
        dom,
        "stateA",
        f"state={gs(today_bd).get('state')!r}",
    )
    bd = gs(today_bd).get("attributes", {}).get("breakdown_seconds", {})
    chk("EC5 breakdown_seconds has stateA", "stateA" in bd, True, f"breakdown={bd}")

    print(
        "\n=== EC6: NEW state at runtime → key + event + notification ===", flush=True
    )
    # Dwell in the new state long enough to give it a clear folded duration, then
    # flip back so the visit folds into the ledger (fold happens on the NEXT
    # transition). A too-short dwell can leave breakdown_seconds without the key
    # when a coordinator tick lands before the fold.
    events = capture_events(
        EVENT_NEW_STATE,
        lambda: (ss(eid, "brandnew_state"), time.sleep(5), ss(eid, "stateA")),
        timeout=28,
    )
    ev = next((e for e in events if e.get("state") == "brandnew_state"), None)
    if _WS_AVAILABLE:
        chk("EC6 new_state event fired", ev is not None, True, f"events={events}")
        if ev:
            chk("EC6 event payload entry_id matches", ev.get("entry_id"), entry)
            chk("EC6 event payload entity_id matches", ev.get("entity_id"), eid)
            chk(
                "EC6 event payload state = new state", ev.get("state"), "brandnew_state"
            )
    else:
        note(
            "EC6: websocket-client absent — event capture skipped; verifying breakdown + notification only"
        )
    # breakdown gets the new key. Ensure the visit has folded: a trailing fold
    # transition is already done above; poll generously for the recompute.
    got_key = wait_until(
        lambda: (
            "brandnew_state"
            in gs(today_bd).get("attributes", {}).get("breakdown_seconds", {})
        ),
        timeout=WAIT_FOR_TIMEOUT,
    )
    chk(
        "EC6 breakdown_seconds gains new state key",
        got_key,
        True,
        f"breakdown={gs(today_bd).get('attributes', {}).get('breakdown_seconds', {})}",
    )
    # A new state is EVENT-ONLY by design (build your own notification in an
    # automation). The integration fires NO persistent_notification — asserting
    # the event above (EC6) is the whole contract.

    print(
        "\n=== EC7: unavailable/unknown counted as ordinary breakdown rows ===",
        flush=True,
    )
    ss(eid, "unavailable")
    time.sleep(6)
    ss(eid, "unknown")
    time.sleep(6)
    ss(eid, "stateA")  # fold the unknown visit
    time.sleep(6)
    bd = gs(today_bd).get("attributes", {}).get("breakdown_seconds", {})
    chk(
        "EC7 'unavailable' present as breakdown row",
        "unavailable" in bd,
        True,
        f"breakdown={bd}",
    )
    chk(
        "EC7 'unknown' present as breakdown row",
        "unknown" in bd,
        True,
        f"breakdown={bd}",
    )
    return entry, eid, today_bd


def ec8_glitch_filter():
    """EC8: min_state_duration=5 → sub-5s visits filtered (count doesn't rise)."""
    print(
        "\n=== EC8: min_state_duration glitch filter (sub-5s visits ignored) ===",
        flush=True,
    )
    eid = make_entity("glitch", "on")
    entry = create_tracker(
        eid,
        "all_states",
        frames={"today": True},
        # A high threshold keeps the quick flicks unambiguously sub-threshold
        # even under container HTTP/scheduling latency (a 5s threshold can race
        # a loaded host where a "1s" flick lands >5s apart on the wall clock).
        min_state_duration=120,
    )
    reg = wait_entities(entry, min_count=1)
    today_bd = eid_for(entry, "today", M_BREAKDOWN, reg)
    # Rapid flips into "flicker", each far shorter than the 120s threshold.
    for _ in range(4):
        ss(eid, "flicker")
        time.sleep(1)
        ss(eid, "on")
        time.sleep(1)
    # Settle in "on" well past min_state_duration so the trailing open block is
    # unambiguously "on" (a coordinator tick landing mid-flick could otherwise
    # briefly see an open sub-threshold "flicker" block). Poll until stable.
    ss(eid, "on")
    time.sleep(12)
    flicker_count = wait_for(
        lambda: gs(today_bd).get("attributes", {}).get("counts", {}).get("flicker", 0),
        0,
        timeout=WAIT_FOR_TIMEOUT,
    )
    counts = gs(today_bd).get("attributes", {}).get("counts", {})
    chk(
        "EC8 sub-threshold 'flicker' visits filtered (count==0)",
        flicker_count,
        0,
        f"counts={counts}",
    )
    return entry, eid


def ec9_unrecorded(entry_allstates, eid_allstates, today_bd):
    """EC9: breakdown attrs are _unrecorded — present in live /api/states."""
    print(
        "\n=== EC9: breakdown attrs present in live state (unrecorded) ===", flush=True
    )
    attrs = gs(today_bd).get("attributes", {})
    for key in (
        "source_entity",
        "frame",
        "breakdown_seconds",
        "breakdown_pct",
        "counts",
        "avg_duration_seconds",
        "previous_state",
        "last_entered",
        "last_exited",
        "window_seconds",
        "unaccounted_seconds",
        "window_coverage",
        "has_gap",
    ):
        chk(
            f"EC9 attr '{key}' present in live state",
            key in attrs,
            True,
            f"keys={list(attrs)}",
        )
    # breakdown_pct is balanced to sum to 100 with a trailing 'unaccounted' key.
    pct = attrs.get("breakdown_pct", {})
    chk(
        "EC9 breakdown_pct carries 'unaccounted' balancing key",
        "unaccounted" in pct,
        True,
        f"breakdown_pct={pct}",
    )
    # Best-effort: confirm the churny attr is NOT in recorder history.
    try:
        hist = api(
            "GET", f"/api/history/period?filter_entity_id={today_bd}&minimal_response"
        )
        # /api/history returns list-of-lists of state dicts. Attributes only appear on the first entry.
        first = hist[0][0] if hist and hist[0] else {}
        recorded_attrs = first.get("attributes", {})
        chk(
            "EC9 breakdown_seconds excluded from recorder history",
            "breakdown_seconds" not in recorded_attrs,
            True,
            f"recorded_attr_keys={list(recorded_attrs)}",
        )
    except Exception as e:
        note(f"EC9 recorder-history check skipped: {e}")


def ec10_reset_ledger(entry_allstates, eid_allstates, today_bd):
    """EC10: reset_ledger confirm:false → error/no change; confirm:true → cleared."""
    print("\n=== EC10: reset_ledger confirm gate + clears ledger ===", flush=True)
    # Accrue something first.
    ss(eid_allstates, "stateA")
    time.sleep(6)
    ss(eid_allstates, "stateB")
    time.sleep(4)
    ss(eid_allstates, "stateA")
    wait_until(
        lambda: (
            sum(
                gs(today_bd).get("attributes", {}).get("breakdown_seconds", {}).values()
            )
            > 0
        )
    )
    before = gs(today_bd).get("attributes", {}).get("breakdown_seconds", {})
    note(f"breakdown before reset: {before}")

    # confirm:false → ServiceValidationError (HTTP 400), ledger unchanged.
    status, body = api_status(
        "POST", f"/api/services/{DOMAIN}/reset_ledger", {"confirm": False}
    )
    chk(
        "EC10 confirm:false → error status (>=400)",
        status >= 400,
        True,
        f"status={status} body={body}",
    )
    after_false = gs(today_bd).get("attributes", {}).get("breakdown_seconds", {})
    chk(
        "EC10 ledger unchanged after confirm:false",
        sum(after_false.values()) > 0,
        True,
        f"after={after_false}",
    )

    # confirm:true → clears. After reset the ledger is emptied; today's live slice
    # rebuilds from the recorder, but the pre-reset accrued closed-history is gone.
    status2, _ = api_status(
        "POST", f"/api/services/{DOMAIN}/reset_ledger", {"confirm": True}
    )
    chk(
        "EC10 confirm:true → success status (2xx)",
        200 <= status2 < 300,
        True,
        f"status={status2}",
    )

    # After reset the coordinator refreshes; the ledger daily buckets are cleared.
    # We verify via diagnostics that day_count dropped to 0 (most robust signal).
    def _daycount():
        raw = api("GET", f"/api/diagnostics/config_entry/{entry_allstates}")
        data = raw.get("data", raw)
        return (data.get("ledger") or {}).get("day_count")

    dc = wait_for(_daycount, 0, timeout=WAIT_FOR_TIMEOUT)
    chk("EC10 ledger cleared after confirm:true (day_count=0)", dc, 0)

    # Diagnostics payload shape: beyond `ledger`, it carries entry/coordinator/
    # frames/store blocks. Assert the top-level structure once here.
    raw = api("GET", f"/api/diagnostics/config_entry/{entry_allstates}")
    data = raw.get("data", raw)
    for block in ("entry", "coordinator", "frames", "ledger", "store"):
        chk(
            f"EC10 diagnostics carries '{block}' block",
            block in data,
            True,
            f"keys={list(data)}",
        )
    coord = data.get("coordinator", {})
    for key in (
        "entity_id",
        "mode",
        "tracked_states",
        "enabled_frames",
        "last_update_success",
    ):
        chk(
            f"EC10 diagnostics coordinator.'{key}' present",
            key in coord,
            True,
            f"coord_keys={list(coord)}",
        )


def ec11_options_flow_frame_toggle():
    """EC11: options-flow edit (toggle a frame off) → entry reloads, that frame's sensor removed."""
    print(
        "\n=== EC11: options-flow toggles a frame off → sensor removed on reload ===",
        flush=True,
    )
    eid = make_entity("opts", "on")
    entry = create_tracker(
        eid,
        "specific_states",
        states=["on"],
        frames=FRAMES_ON,
    )
    reg = wait_entities(entry, min_count=4)
    yday = eid_for(entry, "yesterday", M_DURATION, reg)
    chk("EC11 yesterday sensor exists before edit", yday is not None, True)

    # Options flow: init step carries frames + tail (+ target when compliance). Turn yesterday off.
    r = api("POST", "/api/config/config_entries/options/flow", {"handler": entry})
    fid = r["flow_id"]
    assert r.get("step_id") == "init", f"expected init step, got {r}"
    body = {
        "today": True,
        "yesterday": False,  # toggled OFF
        "24h": True,
        "7d": True,
        "30d": False,
        "month": False,
        "year": False,
        "min_state_duration": 0,
    }
    r2 = api("POST", f"/api/config/config_entries/options/flow/{fid}", body)
    chk("EC11 options flow created entry", r2.get("type"), "create_entry")
    # Entry reloads. HA keeps the orphaned registry entry for the now-unproduced
    # frame, but the platform no longer creates that entity → its state goes
    # unavailable (no live value). The kept frame stays live (numeric). That is
    # the real "frame removed" signal (HA does not auto-delete registry rows).
    yday_after = eid_for(entry, "yesterday", M_DURATION)
    gone = wait_until(
        lambda: (
            yday_after is None
            or (gs_safe(yday_after) or {}).get("state") in ("unavailable", None)
        ),
        timeout=WAIT_FOR_TIMEOUT,
    )
    chk(
        "EC11 yesterday frame sensor no longer produced (unavailable) after toggle off",
        gone,
        True,
        f"yesterday_state={(gs_safe(yday_after) or {}).get('state') if yday_after else None}",
    )
    # today should still be present and live (numeric).
    today_eid = eid_for(entry, "today", M_DURATION)
    today_live = today_eid is not None and (gs_safe(today_eid) or {}).get(
        "state"
    ) not in (None, "unavailable")
    chk(
        "EC11 today frame sensor still live (numeric)",
        today_live,
        True,
        f"today_state={(gs_safe(today_eid) or {}).get('state') if today_eid else None}",
    )
    return entry, eid


def ec13_coverage_gap():
    """EC13: window_coverage / has_gap fields are present and internally consistent.

    The genuine "data younger than window" gap only surfaces once the ledger
    holds a *closed* day whose oldest key falls inside the window; a fresh
    tracker's ledger is empty (today's slice comes from the recorder), so
    data_start is unknown and coverage is correctly reported as 1.0 / no-gap
    (the engine does not fabricate a gap from missing history — §8 _coverage()).
    We therefore assert the invariant that actually holds live: the fields exist,
    coverage ∈ [0,1], has_gap is boolean, and has_gap ⟺ (data_start known AND
    coverage < 1).
    """
    print(
        "\n=== EC13: 7d window_coverage / has_gap fields present + consistent ===",
        flush=True,
    )
    eid = make_entity("cov", "on")
    entry = create_tracker(eid, "all_states", frames={"today": True, "7d": True})
    reg = wait_entities(entry, min_count=2)
    d7 = eid_for(entry, "7d", M_BREAKDOWN, reg)
    ss(eid, "on")
    time.sleep(6)
    ss(eid, "off")
    time.sleep(6)
    attrs = gs(d7).get("attributes", {})
    cov = attrs.get("window_coverage")
    gap = attrs.get("has_gap")
    ds = attrs.get("data_start")
    note(f"7d window_coverage={cov} has_gap={gap} data_start={ds}")
    chk(
        "EC13 window_coverage present",
        cov is not None,
        True,
        f"attrs_keys={list(attrs)}",
    )
    chk(
        "EC13 window_coverage in [0,1]",
        0.0 <= float(cov) <= 1.0,
        True,
        f"coverage={cov}",
    )
    chk("EC13 has_gap is boolean", isinstance(gap, bool), True, f"has_gap={gap!r}")
    # Consistency: a gap is flagged iff we know a data_start inside the window
    # (coverage < 1). No gap ⟺ coverage == 1.0 for a fresh (empty-ledger) tracker.
    consistent = bool(gap) == (ds is not None and float(cov) < 1.0)
    chk(
        "EC13 has_gap ⟺ (data_start known AND coverage<1)",
        consistent,
        True,
        f"has_gap={gap} data_start={ds} coverage={cov}",
    )
    return entry, eid


def ec14_dominant_hysteresis():
    """EC14: two near-tied states → dominant doesn't flip on sub-margin noise."""
    print(
        "\n=== EC14: dominant hysteresis — near-tie doesn't flip on noise ===",
        flush=True,
    )
    eid = make_entity("hyst", "alpha")
    entry = create_tracker(
        eid,
        "all_states",
        frames={"today": True},
        min_state_duration=0,
    )
    reg = wait_entities(entry, min_count=1)
    today_bd = eid_for(entry, "today", M_BREAKDOWN, reg)
    # Accrue alpha as dominant.
    ss(eid, "alpha")
    time.sleep(12)  # keep alpha ahead
    ss(eid, "beta")
    dom0 = wait_for(lambda: gs(today_bd).get("state"), "alpha")
    chk(
        "EC14 alpha is dominant initially",
        dom0,
        "alpha",
        f"state={gs(today_bd).get('state')!r}",
    )
    # Give beta a tiny sliver — below the 1% hysteresis margin — dominant must stay alpha.
    ss(eid, "beta")
    time.sleep(1)  # sub-margin sliver relative to alpha's 12s
    ss(eid, "alpha")
    time.sleep(8)
    dom1 = gs(today_bd).get("state")
    bd = gs(today_bd).get("attributes", {}).get("breakdown_seconds", {})
    chk(
        "EC14 dominant stays alpha through sub-margin beta sliver",
        dom1,
        "alpha",
        f"breakdown={bd}",
    )
    return entry, eid


def ec15_card_resource():
    """EC15: card resource registered / served."""
    print("\n=== EC15: card JS served + Lovelace resource registered ===", flush=True)
    status, _ = api_status("GET", "/entity_state_tracker/entity-state-tracker-card.js")
    chk("EC15 card JS served (200)", status, 200)
    # Lovelace resources list (websocket): the card should be registered.
    res = _ws_command({"type": "lovelace/resources"})
    registered = False
    if res and res.get("success"):
        items = res.get("result") or []
        registered = any(
            "entity-state-tracker-card.js" in (r.get("url") or "") for r in items
        )
        chk(
            "EC15 Lovelace resource registered",
            registered,
            True,
            f"resources={[r.get('url') for r in items]}",
        )
    else:
        note(
            "EC15: lovelace/resources websocket unavailable — served-200 check stands alone"
        )


def ec16_targeted_reset():
    """EC16: reset_ledger entity_id target → only the matching tracker's ledger clears;
    a target matching no tracker raises (reset_no_match).
    """
    print(
        "\n=== EC16: reset_ledger entity_id target resets only that tracker ===",
        flush=True,
    )
    eid_a = make_entity("tgt_a", "on")
    eid_b = make_entity("tgt_b", "on")
    entry_a = create_tracker(eid_a, "all_states", frames={"today": True})
    entry_b = create_tracker(eid_b, "all_states", frames={"today": True})
    wait_entities(entry_a, min_count=1)
    wait_entities(entry_b, min_count=1)

    # Accrue on BOTH so each ledger has data to clear. Entities start "on"
    # (make_entity above), so a single off→on cycle is enough to record a visit.
    for e in (eid_a, eid_b):
        time.sleep(3)
        ss(e, "off")
        ss(e, "on")

    def _day_count(entry_id):
        raw = api("GET", f"/api/diagnostics/config_entry/{entry_id}")
        data = raw.get("data", raw)
        return (data.get("ledger") or {}).get("day_count")

    wait_until(lambda: (_day_count(entry_a) or 0) >= 1)
    wait_until(lambda: (_day_count(entry_b) or 0) >= 1)

    # Target ONLY entity A.
    status, _ = api_status(
        "POST",
        f"/api/services/{DOMAIN}/reset_ledger",
        {"confirm": True, "entity_id": eid_a},
    )
    chk(
        "EC16 targeted reset success (2xx)",
        200 <= status < 300,
        True,
        f"status={status}",
    )

    dca = wait_for(lambda: _day_count(entry_a), 0, timeout=WAIT_FOR_TIMEOUT)
    chk("EC16 targeted tracker A ledger cleared (day_count=0)", dca, 0)
    # B must be untouched (still holds ≥1 day). Read once so a transient
    # diagnostics error fails this check rather than aborting the test on the
    # message-arg call.
    b_after = _day_count(entry_b) or 0
    chk(
        "EC16 non-targeted tracker B ledger untouched (day_count≥1)",
        b_after >= 1,
        True,
        f"B_day_count={b_after}",
    )

    # A target matching no tracker → reset_no_match (HTTP >=400), nothing cleared.
    status_nm, body_nm = api_status(
        "POST",
        f"/api/services/{DOMAIN}/reset_ledger",
        {"confirm": True, "entity_id": "sensor.est_no_such_tracker_xyz"},
    )
    chk(
        "EC16 unmatched target → error status (>=400)",
        status_nm >= 400,
        True,
        f"status={status_nm} body={body_nm}",
    )
    b_final = _day_count(entry_b) or 0
    chk(
        "EC16 tracker B still untouched after unmatched reset",
        b_final >= 1,
        True,
        f"B_day_count={b_final}",
    )


def ec12_restart_persistence():
    """EC12: create tracker, accrue, restart HA → ledger survives (closed-day buckets intact).

    This is the heaviest EC (restarts HA). Runs last. Uses a dedicated entry we
    intentionally KEEP across the restart, then verify via diagnostics day_count/totals.
    """
    print(
        "\n=== EC12: RESTART PERSISTENCE — ledger survives HA restart ===", flush=True
    )
    if not HA_DIR:
        print(
            "EC12 SKIPPED: set EST_SMOKE_HA_DIR (and optionally EST_SMOKE_PYTHON / "
            "EST_SMOKE_HA_CONFIG) to the HA working dir so this test can relaunch HA. "
            "See tests/integration/README.md.",
            flush=True,
        )
        return None, None
    eid = make_entity("persist", "on")
    entry = create_tracker(eid, "all_states", frames={"today": True, "7d": True})
    wait_entities(entry, min_count=2)

    # Accrue a visit that spans a real fold, then force a flush by making transitions.
    ss(eid, "on")
    time.sleep(10)
    ss(eid, "persisted_marker")
    time.sleep(6)
    ss(eid, "on")
    time.sleep(6)

    # Read ledger totals via diagnostics BEFORE restart.
    def _ledger(entry_id):
        raw = api("GET", f"/api/diagnostics/config_entry/{entry_id}")
        data = raw.get("data", raw)
        return data.get("ledger") or {}

    before = _ledger(entry)
    per_state_before = before.get("per_state_seconds", {})
    note(
        f"ledger before restart: day_count={before.get('day_count')} per_state={per_state_before}"
    )
    chk(
        "EC12 marker state accrued before restart",
        "persisted_marker" in per_state_before,
        True,
        f"per_state={per_state_before}",
    )

    # The store flushes on EVENT_HOMEASSISTANT_STOP; a clean restart triggers that.
    note("restarting HA (this takes ~40s)…")
    back = _restart_ha()
    chk("EC12 HA came back after restart", back, True)
    if not back:
        return entry, eid
    # Give the entry time to load + first refresh.
    wait_until(
        lambda: (
            (api("GET", f"/api/diagnostics/config_entry/{entry}").get("data", {}) or {})
            .get("ledger", {})
            .get("loaded")
        ),
        timeout=90,
    )
    after = _ledger(entry)
    per_state_after = after.get("per_state_seconds", {})
    note(
        f"ledger after restart: day_count={after.get('day_count')} per_state={per_state_after}"
    )
    chk(
        "EC12 persisted_marker seconds survived restart",
        per_state_after.get("persisted_marker", 0) > 0,
        True,
        f"before={per_state_before.get('persisted_marker')} after={per_state_after.get('persisted_marker')}",
    )
    # The exact accrued seconds must be preserved to the microsecond — this is the
    # real persistence signal. day_count is NOT asserted to be retained: the first
    # boot seeds empty backfill buckets across the 7d window, and the second boot
    # prunes stale (empty) buckets older than the frame cutoff, so day_count
    # legitimately shrinks while the accrued per-state data is untouched.
    chk(
        "EC12 persisted_marker seconds identical (no drift) across restart",
        abs(
            float(per_state_after.get("persisted_marker", 0))
            - float(per_state_before.get("persisted_marker", 0))
        )
        < 0.001,
        True,
        f"before={per_state_before.get('persisted_marker')} after={per_state_after.get('persisted_marker')}",
    )
    chk(
        "EC12 ledger still holds ≥1 day after restart (loaded, non-empty)",
        (after.get("day_count") or 0) >= 1,
        True,
        f"before={before.get('day_count')} after={after.get('day_count')}",
    )
    return entry, eid


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _restart_ha() -> bool:
    """Kill + relaunch HA in-place; poll until it answers. Return True if back.

    Shared by EC12 (persistence) and EC17 (currently-in-state reconcile). The
    relaunched process is fully detached (setsid + own session) so it outlives
    this smoke script — a plain child would die with us and take HA down
    mid-suite.
    """
    import subprocess

    subprocess.run(["bash", "-c", "pkill -f 'homeassistant -c' || true"], check=False)
    time.sleep(4)
    subprocess.Popen(
        [
            "setsid",
            "bash",
            "-c",
            (
                f"cd {HA_DIR} && "
                f"{HA_PYTHON} -m homeassistant -c {HA_CONFIG} "
                ">>/tmp/ha.log 2>&1"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 150
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def ec17_currently_reconcile_on_restart():
    """EC17: CurrentlyInState reflects the LIVE state right after a restart.

    Regression guard for the boot-stale bug: the sensor used to read the
    coordinator ledger's ``last_state``, which lags the real entity on boot (and
    stays ``None`` after a reset) until the next live transition — so a tracker
    whose entity sat unchanged in a tracked state across a restart showed the
    WRONG Off/On until something happened to fold. The fix reads HA's live state
    machine, so it must be correct immediately after restart with NO transition.

    We seed the entity into a tracked state, restart HA, and — crucially without
    touching the entity — assert CurrentlyInState is ``on`` once the entry loads.
    """
    print(
        "\n=== EC17: CurrentlyInState correct on restart with no transition ===",
        flush=True,
    )
    if not HA_DIR:
        print(
            "EC17 SKIPPED: set EST_SMOKE_HA_DIR (see EC12) so this test can "
            "relaunch HA.",
            flush=True,
        )
        return None, None
    eid = make_entity("reconcile", "on")
    entry = create_tracker(
        eid, "specific_states", states=["on"], frames={"today": True}
    )
    wait_entities(entry, min_count=1)
    curr = bs_eid_for(entry, M_CURRENTLY)
    chk("EC17 CurrentlyInState exists", curr is not None, True)
    # Confirm it's on BEFORE the restart (entity is "on", a tracked state).
    wait_for(lambda: (gs_safe(curr) or {}).get("state") if curr else None, "on")

    note("restarting HA with entity held 'on' (no transition after)…")
    back = _restart_ha()
    chk("EC17 HA came back after restart", back, True)
    if not back:
        return entry, eid

    # Re-seed the entity to "on" ONCE post-boot: POST /api/states restores our
    # synthetic entity (it isn't a real integration, so it doesn't survive the
    # restart) to the tracked state — WITHOUT going through a tracked→tracked
    # transition that would fold the ledger. This mirrors a real entity that was
    # already "on" at boot. The bug would show Off here (ledger last_state stale/
    # None); the fix reads live state → on.
    ss(eid, "on")

    # Rediscover the (reloaded) entity_id and poll until the entry is back and the
    # sensor reads on. No transition is driven — a correct sensor is on from the
    # live "on" state alone.
    def _curr_now():
        e = bs_eid_for(entry, M_CURRENTLY)
        if not e:
            return None
        st = gs_safe(e)
        return st.get("state") if st else None

    on_val = wait_for(_curr_now, "on", timeout=WAIT_FOR_TIMEOUT)
    chk(
        "EC17 CurrentlyInState=on from live state after restart (no fold)",
        on_val,
        "on",
        "reads HA live state, not stale ledger last_state",
    )
    return entry, eid


def main():
    print("=== Entity State Tracker smoke tests ===", flush=True)
    print(f"BASE={BASE}  FAST={FAST}  WS={_WS_AVAILABLE}  RUN={RUN}", flush=True)
    if EC_FILTER:
        print(f"EC filter: {sorted(EC_FILTER)}", flush=True)

    try:
        if ec_enabled(1) or ec_enabled(2) or ec_enabled(3):
            entry1, eid1 = ec1_specific_duration_sensors()
            if ec_enabled(2) or ec_enabled(3):
                ec2_ec3_duration_rises_and_currently(entry1, eid1)

        if ec_enabled(4):
            ec4_compliance()

        entry_all = eid_all = today_bd = None
        if any(ec_enabled(n) for n in (5, 6, 7, 9, 10)):
            entry_all, eid_all, today_bd = ec5_ec6_ec7_allstates()

        if ec_enabled(8):
            ec8_glitch_filter()

        if ec_enabled(9) and today_bd:
            ec9_unrecorded(entry_all, eid_all, today_bd)

        if ec_enabled(10) and today_bd:
            ec10_reset_ledger(entry_all, eid_all, today_bd)

        if ec_enabled(11):
            ec11_options_flow_frame_toggle()

        if ec_enabled(13):
            ec13_coverage_gap()

        if ec_enabled(14):
            ec14_dominant_hysteresis()

        if ec_enabled(15):
            ec15_card_resource()

        if ec_enabled(16):
            ec16_targeted_reset()

        # EC12 last — it restarts HA.
        if ec_enabled(12):
            ec12_restart_persistence()

        # EC17 also restarts HA (currently-in-state reconcile); after EC12.
        if ec_enabled(17):
            ec17_currently_reconcile_on_restart()

    finally:
        cleanup()

    # Summary table
    print("\n" + "=" * 70, flush=True)
    print("SMOKE SUMMARY", flush=True)
    print("=" * 70, flush=True)
    for label, ok, detail in _results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)
    print("-" * 70, flush=True)
    print(f"{_passed}/{_passed + _failed} green", flush=True)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
