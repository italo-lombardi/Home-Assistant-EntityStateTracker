// Unit tests for the Entity State Tracker card's pure rendering logic.
//
// Harness note: the card is loaded via ./_harness.mjs, which stubs the browser
// globals the card reads at module-load and captures the card class. The three
// methods under test are reached off the prototype — _breakdownSlices and
// _specificSlices use no `this`; _transitionLine needs a stub `this` carrying
// _formatTrackedState and inherits the stubbed html/nothing.
//
// Scope note (issue #39): there is deliberately NO normalizeState↔Python
// normalize_state parity test. The card has no client-side normalize function —
// by design it does exact-key lookups on keys the backend has already
// normalized (engine norm loop + match_state emit canonical keys). The card's
// only folds are display-only (toLabel: `_`→space + title-case; stateColor:
// toLowerCase for a color key), neither implementing Python's
// strip→lower→`_`→space→whitespace-collapse rule. There is nothing on the JS
// side to assert parity against, so the gap is documented here rather than
// guarded by a test against code that does not exist.

import { test } from "node:test";
import assert from "node:assert/strict";
import { Card, NOTHING } from "./_harness.mjs";

const fakeThis = { _formatTrackedState: (_id, s) => s };

test("_transitionLine: sub-second weighted avg renders '0 s', not hidden (PR #36 guard)", () => {
  // Two tracked states, every per-visit avg rounds to 0 s, but there ARE visits.
  // Visit-weighted mean = (0.2*3 + 0.3*2) / 5 = 0.24 → fmtDuration → "0 s".
  // The count>0 guard means this is reported as 0, NOT hidden as "no data".
  const frag = Card.prototype._transitionLine.call(
    fakeThis,
    {
      counts: { on: 3, off: 2 },
      avg_duration_seconds: { on: 0.2, off: 0.3 },
      tracked_states: ["on", "off"],
      source_entity: "sensor.x",
    },
    null
  );
  assert.notEqual(frag, NOTHING, "sub-second avg must not be hidden");
  assert.match(frag.__html, /5 visits/);
  assert.match(
    frag.__html,
    /avg 0 s per visit/,
    "a genuine 0.0 avg is shown as '0 s', not dropped"
  );
});

test("_transitionLine: zero visits is hidden (returns nothing)", () => {
  // The discriminating case: count === 0 → null avg → whole line hidden.
  const frag = Card.prototype._transitionLine.call(
    fakeThis,
    {
      counts: {},
      avg_duration_seconds: {},
      tracked_states: ["on"],
      source_entity: "sensor.x",
    },
    null
  );
  assert.equal(frag, NOTHING);
});

test("_transitionLine: single-state key path reports that state's count + avg", () => {
  const frag = Card.prototype._transitionLine.call(
    fakeThis,
    {
      counts: { active: 4 },
      avg_duration_seconds: { active: 90 },
      source_entity: "sensor.x",
    },
    "active"
  );
  assert.match(frag.__html, /active — 4 visits/);
  assert.match(frag.__html, /avg 1\.5 min per visit/); // 90 s → "1.5 min"
});

test("_breakdownSlices: share-of-window math, sorted secs-descending", () => {
  const slices = Card.prototype._breakdownSlices.call(
    {},
    {
      breakdown_seconds: { on: 300, off: 100 },
      window_seconds: 600,
      unaccounted_seconds: 0,
      counts: { on: 2, off: 1 },
      avg_duration_seconds: { on: 150, off: 100 },
    }
  );
  assert.deepEqual(
    slices.map((s) => s.state),
    ["on", "off"],
    "biggest first"
  );
  assert.equal(Math.round(slices[0].pct), 50); // 300/600
  assert.equal(Math.round(slices[1].pct), 17); // 100/600
});

test("_breakdownSlices: gap > 10 s appends a 'No data' derived slice", () => {
  const slices = Card.prototype._breakdownSlices.call(
    {},
    {
      breakdown_seconds: { on: 500 },
      window_seconds: 600,
      unaccounted_seconds: 100,
    }
  );
  const tail = slices[slices.length - 1];
  assert.equal(tail.state, "No data");
  assert.equal(tail.derived, true);
  assert.equal(tail.secs, 100);
});

test("_breakdownSlices: gap <= 10 s is absorbed (no 'No data' slice)", () => {
  const slices = Card.prototype._breakdownSlices.call(
    {},
    {
      breakdown_seconds: { on: 595 },
      window_seconds: 600,
      unaccounted_seconds: 5,
    }
  );
  assert.ok(!slices.some((s) => s.state === "No data"));
});

test("_specificSlices: tracked share + synthetic 'other' remainder", () => {
  const slices = Card.prototype._specificSlices.call(
    {},
    {
      window_seconds: 1000,
      unaccounted_seconds: 0,
      breakdown_seconds: { active: 400 },
      tracked_states: ["active"],
    },
    null
  );
  const active = slices.find((s) => s.state === "active");
  const other = slices.find((s) => s.state === "other");
  assert.equal(Math.round(active.pct), 40); // 400/1000
  assert.ok(other, "remainder is surfaced as an 'other' slice");
  assert.equal(Math.round(other.pct), 60); // (1000-400)/1000
});

test("zero-duration slices are dropped (no '0 s · 0.0%' legend clutter)", () => {
  // bug_card.png: tracked states never seen in the window surface as "0 s · 0.0%"
  // rows. _renderPie / bar-track filter them with `secs >= 1`; the builders still
  // emit them, so guard the predicate against a real 0-sec tracked state.
  const slices = Card.prototype._breakdownSlices.call(
    {},
    {
      window_seconds: 1000,
      unaccounted_seconds: 0,
      breakdown_seconds: { seen: 600, unseen: 0 },
      tracked_states: ["seen", "unseen"],
    }
  );
  assert.ok(
    slices.some((s) => s.state === "unseen" && s.secs === 0),
    "builder still emits the 0-sec state"
  );
  const kept = slices.filter((s) => s.secs >= 1);
  assert.ok(!kept.some((s) => s.state === "unseen"), "0-sec state is filtered out");
  assert.ok(kept.some((s) => s.state === "seen"), "non-zero state survives");
});


test("_formatTrackedState: zone key resolves to the zone's LIVE friendly name, not re-cased key", () => {
  // The engine lowercases every state key, so a zone named "Casa Maria Teresa
  // (Parma)" is stored as "casa maria teresa (parma)". toLabel() only re-cases
  // word-initial chars → "(parma)" keeps its lowercase p. The card must instead
  // pull the real friendly name from hass (ground truth).
  const stub = {
    hass: {
      states: {
        "zone.cmt": { attributes: { friendly_name: "Casa Maria Teresa (Parma)" } },
        "person.x": { attributes: {} },
      },
      formatEntityState: () => null,
    },
  };
  const out = Card.prototype._formatTrackedState.call(
    stub,
    "person.x",
    "casa maria teresa (parma)"
  );
  assert.equal(out, "Casa Maria Teresa (Parma)");
});

test("_formatTrackedState: non-zone key still title-cases via toLabel", () => {
  const stub = { hass: { states: {}, formatEntityState: () => null } };
  const out = Card.prototype._formatTrackedState.call(stub, "sensor.x", "not_home");
  assert.equal(out, "Not Home");
});

test("_formatTrackedState: non-tracker domain does NOT adopt a colliding zone name", () => {
  // A climate "away" that happens to match a zone named "Away" must stay a
  // climate state — only person/device_tracker report a zone name as state.
  const stub = {
    hass: {
      states: { "zone.away": { attributes: { friendly_name: "Away" } } },
      formatEntityState: () => null,
    },
  };
  const out = Card.prototype._formatTrackedState.call(stub, "climate.hvac", "away");
  assert.equal(out, "Away"); // toLabel title-case, NOT the zone lookup path
  // Prove it's the toLabel path, not the zone path: a lowercase-preserving
  // zone name would leak through if the gate were missing.
  const stub2 = {
    hass: {
      states: { "zone.x": { attributes: { friendly_name: "mixedCase Zone" } } },
      formatEntityState: () => null,
    },
  };
  const out2 = Card.prototype._formatTrackedState.call(
    stub2,
    "climate.hvac",
    "mixedcase zone"
  );
  assert.equal(out2, "Mixedcase Zone"); // toLabel, not the zone's "mixedCase Zone"
});

test("_formatTrackedState: ambiguous zones (case-only diff) resolve to neither", () => {
  // Two zones whose names fold to the same key → never guess; fall back to
  // toLabel (matches helpers.zone_label_map's ambiguity drop).
  const stub = {
    hass: {
      states: {
        "zone.a": { attributes: { friendly_name: "Office" } },
        "zone.b": { attributes: { friendly_name: "OFFICE" } },
      },
      formatEntityState: () => null,
    },
  };
  const out = Card.prototype._formatTrackedState.call(stub, "person.x", "office");
  assert.equal(out, "Office"); // toLabel fallback, not a silent pick of either zone
});

test("_formatTrackedState: zone key folds underscores like normalize_state", () => {
  // A zone "Not Home" folds to "not home"; a stored "not_home" key must match.
  const stub = {
    hass: {
      states: { "zone.nh": { attributes: { friendly_name: "Not Home" } } },
      formatEntityState: () => null,
    },
  };
  const out = Card.prototype._formatTrackedState.call(stub, "person.x", "not_home");
  assert.equal(out, "Not Home");
});
