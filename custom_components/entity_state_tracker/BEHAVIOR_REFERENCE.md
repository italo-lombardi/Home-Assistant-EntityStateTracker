# Entity State Tracker — Behavior Reference

How a tracker turns one entity's state history into time-in-state across eleven
time frames — and exactly when each of the four automation events fires, with
what payload. This is the runtime truth behind the events in the trigger picker
and the numbers shown on the card. For ready-made automations that consume these
events see [`AUTOMATION_EXAMPLES.md`](../../AUTOMATION_EXAMPLES.md).

Source of truth: `custom_components/entity_state_tracker/coordinator.py` (poll
loop, edge emission, ledger folds, restart/backfill, frame-failure and
recorder-off handling), `engine.py` (frame bounds, block accumulation, per-frame
math, dominant/compliance/coverage), `sensor.py` and `binary_sensor.py` (what
each entity exposes), and `trigger.py` (the named triggers). Event names, frame
set, and thresholds: `const.py`.

---

## 1. What a tracker does

A tracker watches **one** entity and measures how its state is distributed over
time. Each tracker runs in one of two mutually exclusive **modes**, chosen when
the tracker is created and fixed thereafter:

- **Specific-states** (`specific_states`) — you name the states you care about.
  Per frame the tracker reports how long (and what share of the window) the
  entity spent in those states, and — if you also set a compliance target — how
  that share compares to a threshold. This is the "how long was the light on
  today" shape.
- **All-states** (`all_states`) — you name nothing. Per frame the tracker breaks
  the window down across **every** state the entity visited and reports the
  **dominant** state (the one with the most time). This is the "what was this
  room mostly used for" shape.

**Frames.** Every frame is one window of time, and both modes compute the same
metrics once per enabled frame. The eleven frames, in their canonical order
(which is also the display order), split into two kinds:

- **Calendar frames** snap to a **local** boundary — midnight, Monday, the 1st,
  Jan 1 — and **reset to a disjoint window** when that boundary passes.
  - **Open** (end at *now*): `today` (since local midnight), `week` (since local
    Monday 00:00 — week-**to-date**, not a rolling 7 days), `month` (since the
    1st), `year` (since Jan 1).
  - **Closed** (end at an earlier local midnight, not *now*): `yesterday`,
    `last_week` (the previous full Monday-anchored week), `last_month` (the
    previous full calendar month).
- **Rolling frames** are `now − delta → now` and slide their start a tick each
  poll; they never reset to a disjoint window and always include the current
  moment: `24h`, `7d`, `30d`, `365d`. (`365d` is the rolling sibling of the
  calendar `year`.)

Two pairs look alike but are not: `week` is week-**to-date** (Monday 00:00 →
now), distinct from the rolling `7d`; and calendar `year` (Jan 1 → now) is
distinct from rolling `365d`.

**Which frames are on.** Enabled frames are built in canonical order as the
frames whose flag is set, so display order is always the order above. The
default selection is `today`, `yesterday`, `24h`, `7d`. The rest
(`week`, `last_week`, `30d`, `month`, `last_month`, `year`, `365d`) are off by
default because they reach past the HA recorder's retention and **fill in over
time** from the ledger's whole-day buckets (`365d` is the deepest). Turning one
on does not retroactively invent history it never recorded; it begins
accumulating whole days from that point and surfaces a coverage gap until the
window is filled (see §7).

Every event says which tracker it came from, in an `entry_id` field — a unique
ID Home Assistant gives each config entry that never changes, even if the
tracker is renamed. The per-frame events additionally carry a `frame` field. An
automation can match on either to react to just one tracker or one window; in the
editor, the named trigger's dropdowns fill them in for you.

**One poll can fire more than one event.** Each enabled frame is evaluated
independently every tick, so a single poll can fire a `dominant_changed` on
`today` and another on `7d`, plus a compliance edge, all at once.

---

## 2. The four events

The integration fires four bus events, each surfaced as a UI-pickable named
automation trigger. A trigger is a thin relay: it subscribes to its one bus
event, filters by the optional scope you set, and forwards the payload
**verbatim** to the automation. So every payload field below appears at the
trigger's top level.

| Event | Trigger name | Fires when | Payload fields |
|---|---|---|---|
| **new_state** | "New state seen" | An all-states tracker sees a state name it has not seen this session. | `entry_id`, `entity_id`, `state` |
| **dominant_changed** | "Dominant state changed" | A frame's dominant state flips from one real state to another (all-states mode). | `entry_id`, `entity_id`, `frame`, `dominant`, `previous_dominant`, `dominant_pct` |
| **compliant_on** | "Compliant turned on" | A frame's compliance verdict crosses from not-compliant to compliant. | `entry_id`, `entity_id`, `frame`, `compliant` (`true`), `compliance_percent`, `threshold` |
| **compliant_off** | "Compliant turned off" | A frame's compliance verdict crosses from compliant to not-compliant. | `entry_id`, `entity_id`, `frame`, `compliant` (`false`), `compliance_percent`, `threshold` |

`dominant_pct` is the dominant state's share of the window, taken from the
frame's `breakdown_pct`. `new_state` carries no `frame` — it is entity-level, not
per-frame — so its trigger exposes only the tracker scope, not a frame scope.

**Scoping.** Every trigger accepts an optional `config_entry_id` to limit it to
one tracker; the three per-frame triggers additionally accept a `frame` to limit
them to one window. Both blank means fire for any entry / any frame. A
`config_entry_id` that does not resolve to one of this integration's entries is
**rejected at validation time** (a hard error), not silently dead — a dead scope
gives the user no feedback otherwise.

> **Named triggers expose the payload at the top level.** Consumed as a named
> trigger, the fields are `trigger.dominant`, `trigger.frame`, `trigger.state`,
> etc. — not `trigger.event.data.*`. Same data, flatter path.

The exact conditions behind `dominant_changed` and the compliance edges — and
the guard that suppresses phantom calendar-frame edges — are §6.

---

## 3. The sensors and binary sensors

All entities for one tracker live under **one** service device (HA device
registry, manufacturer "Entity State Tracker", an entry-type SERVICE device
named after the tracker). Every entity gates its **availability** on the
coordinator's last update succeeding, and returns `None` for its state and
attributes before the coordinator's first data arrives. The card discovers
entities by device and translation key, not by entity-id string, so you may
rename them freely — history survives via the registry.

**Which entities exist depends on the mode and which options are set.**

### Specific-states mode — per enabled frame

- **Duration sensor** (always). State = seconds spent in the tracked states,
  as a `duration` device class (native unit seconds, displayed in hours to two
  decimals, which matches a plain `history_stats` sensor). When no explicit
  tracked set is configured it sums every state; otherwise each configured state
  is resolved to the one real recorded state name it denotes and summed once
  (so a config value and a recorded value that differ only in case or separators
  are counted once, never doubled). Rounded to millisecond precision to tame the
  float-sum tail.
- **Percent sensor** (always). State = the tracked-state share of the window,
  0–100, one decimal, unit `%`, marked a diagnostic entity.
- **Compliance sensor** (only when a compliance target is configured). State =
  the target-set share of the window, 0–100, one decimal, same `%`/diagnostic
  shape. Percent and compliance are promoted to their own entities (rather than
  living as attributes of the duration sensor) so numeric-state triggers,
  history graphs, and long-term Statistics — none of which can bind to an
  attribute — can target them. They are marked diagnostic rather than
  disabled-by-default so they accrue Statistics without the user enabling them
  by hand.

### All-states mode — per enabled frame

- **Breakdown sensor**. State = the frame's **dominant** state (the
  max-duration state, with hysteresis applied — see §5). Never `None` once data
  exists, and never the synthetic value `"unaccounted"`. There is no single
  percent, compliance, or duration sensor in all-states mode.

### Binary sensors

- **Currently-in-state** (specific mode, only when a tracked set is configured;
  one per tracker, frame-agnostic). `is_on` reads the **source entity's live
  state** straight from Home Assistant — not the ledger's last recorded state,
  which can lag on boot — and compares it to the tracked set with case and
  separator folding, so a live `City Centre` matches a configured `city_centre`.
  It subscribes to the source's state changes and repaints on every edge, and
  publishes once at add time, so it is correct the instant the tracker loads
  (even across a restart with no transition) without waiting for the next
  one-minute tick. `False` when the live state is missing. Note that its
  **availability** still follows the coordinator (like every entity), but its
  **on/off value** is read live from the source entity and repainted on each
  source edge and once at add time — the value does not wait on the coordinator
  tick.

- **Compliant** (only when a compliance threshold is set; **one per enabled
  frame**). `is_on` is a **bare threshold compare** of the frame's compliance
  percent against the threshold — `None` (unknown) when the threshold is
  cleared, there is no data, the frame is absent, or the compliance percent is
  `None`. It has **no hysteresis** by design, so it can flap tick-to-tick if the
  percentage oscillates around the bar. That is intentional: the binary sensor
  and the `compliant_on`/`compliant_off` triggers read the **same** verdict
  function, so the sensor's on/off value and the trigger's compliant/
  not-compliant verdict are always computed identically for a given window. (The
  trigger additionally applies the edge guards of §6, so it may legitimately not
  *fire* on a verdict flip the sensor *shows* — that is edge suppression, not a
  verdict disagreement.)

Both binary sensors ride the coordinator: a recompute that yields a
byte-identical state writes no recorder row (HA reports it without recording),
and a changed value writes one.

### Attributes

The **duration sensor** carries: `source_entity`, `frame`, `percent`,
`duration_seconds` (the raw seconds behind the hours-unit state),
`duration_text` (the same seconds humanized, so it can never diverge from
`duration_seconds`), `tracked_states`, `target_states`, `window_start` (local
ISO), `data_start`, `window_coverage`, `has_gap`, `window_seconds`, and
`unaccounted_seconds`. When a tracked set is configured it adds
`breakdown_seconds` / `breakdown_pct` / `breakdown_text` **sliced to the tracked
states only** (a "specific" sensor must not leak non-tracked states), keyed by
the configured state but valued from the real recorded key. If a tracked key
names a live zone (only when the source is a `person` or `device_tracker`) it
adds `breakdown_labels` mapping the key to the zone's current friendly name,
resolved fresh on every read so a rename is honored. When a target is set it adds
`compliance_percent` and `target_threshold`. It also merges transition metrics:
`counts`, `avg_duration_seconds`, `previous_state`, and the tracker-global
`last_entered` / `last_exited` (`{state: iso}` dicts, identical on every frame's
sensor).

The **breakdown sensor** carries: `source_entity`, `frame`, and
`breakdown_seconds` / `breakdown_pct` / `breakdown_text` / `counts` /
`avg_duration_seconds` — all keyed by the **real** recorded state names
(identity, never folded, because the fold is many-to-one), sorted by seconds
descending. `breakdown_pct` additionally carries an appended `"unaccounted"` key
so a template summing it reaches ~100.00; that key has no `breakdown_seconds`
entry and so is not in the sort. It also carries `previous_state`,
`last_entered`, `last_exited`, `window_seconds`, `data_start`,
`window_coverage`, `has_gap`, `unaccounted_seconds`, and `breakdown_labels` when
a live-zone key resolves.

The **Currently-in-state binary sensor** carries: `source_entity`,
`tracked_states`, and `current_state` (the source entity's live state). Of
these, `current_state` is stripped from HA's recorder (it changes with the
source entity); the config fields stay recorded.

The **Compliant binary sensor** (one per enabled frame) carries:
`source_entity`, `compliance_percent`, `tracked_states`, `target_states`,
`target_threshold`, `frame`, `data_start`, `window_coverage`, and `has_gap`. Of
these, `compliance_percent`, `data_start`, `window_coverage`, and `has_gap` are
stripped from the recorder (they churn every tick); `source_entity`,
`tracked_states`, `target_states`, `target_threshold`, and `frame` are
config-stable and stay recorded. Templating off a compliant sensor therefore
reads e.g. `state_attr('binary_sensor..._compliant_today', 'compliance_percent')`
live, even though that value is not recorded for history.

**What is not recorded.** The churning per-tick attributes — every breakdown
dict, `counts`, `avg_duration_seconds`, `percent`, `compliance_percent`, the
window/coverage fields, `previous_state`, `last_entered`, `last_exited`,
`current_state` — are stripped from HA's own recorder. They change nearly every
tick, and the ledger, not HA long-term statistics, is this integration's history
store; recording them would defeat recorder deduplication and write hundreds of
thousands of attribute rows per entity per year. `percent` and
`compliance_percent` stay live-queryable (as attributes and in templates), just
not recorded — which is why they are also promoted to their own recordable
sensors. Config-stable attributes (`tracked_states`, `target_states`,
`source_entity`) stay recorded.

**`duration_text` and the humanized form.** The sensor *state* is a raw number,
so a template reading it gets hours after unit conversion, never a human string.
`duration_text` exposes the human form server-side as a fixed dict ordered
finest to coarsest — e.g. `{"s": "66816s", "ms": "1113m 36s", "hm": "18h 33m",
"dh": "0d 18h", "wd": "0w 0d"}`. Every value **truncates** (never rounds up), so
the text never overstates elapsed time; negative input is clamped to zero.

---

## 4. How time-in-state is computed

A frame's numbers are rebuilt from scratch every tick. Nothing about the
computed output is persisted — only the ledger of whole-day buckets (§8) is.

**One boundary source.** Every window — for the sensors, for the ledger fold,
for the prune cutoff — becomes a `(start_utc, end_utc)` pair through the single
helper that resolves a frame key to its bounds. `now` is normalized into the
configured timezone first, so every calendar boundary lands on a real **local**
midnight / 1st / Jan 1 regardless of `now`'s own zone, and the result is
converted to UTC. This single source closes the UTC-vs-local seam that would
otherwise let the fold and the read disagree on where a day begins. An unknown
frame key raises an error rather than silently yielding a zero window.

**Real seconds, always — DST-safe.** Every duration is a wall-clock delta
between two instants, never a hard-coded 86400 or a count of days. The percent
denominator is the real `(end − start)` seconds of the window. So a 23-hour
spring-forward `today` has a window of 82800 seconds and a 25-hour fall-back
`today` has 90000 — and a state held continuously still reads ~100% across
either. When a calendar frame rewinds to a prior local midnight it subtracts the
days from a local midnight and then re-snaps to local midnight, so the start
always lands on 00:00 local even across a transition. A visit that straddles a
DST change is split at local midnights and attributes the real elapsed seconds to
each day — 23 hours for the short day, not 24.

**The ledger seam — no double-count.** A frame's window is assembled from two
non-overlapping sources: the **ledger** owns closed **whole local days**, and the
**recorder** owns the recent/open portion. The seam is the local day the ledger
fills up to. For open calendar frames the window ends at *now*, so the seam is
today: the ledger owns every closed day and the recorder recomputes the open
day. `yesterday` also closes at today's local midnight (its end is today 00:00),
so its seam is today too. Only `last_week` and `last_month` close on an
**earlier** local midnight — so for those the ledger owns only days below that
earlier midnight and never days after the window closed. That earlier-midnight
seam is what eliminated the historical `last_week` over-100% over-count. (The
rolling frames, including `30d`, are a separate case with their own seam — see
the next paragraph.)

**Rolling frames start mid-day — the far-edge guard.** A rolling frame (`24h`,
`7d`, `30d`, `365d`) runs `now − delta → now`, so its window ends at *now* and
its **oldest day is partial** (its start is some time-of-day, not 00:00). The
recorder is anchored at the later of the window start and the recorder's own
retention start, and the ledger is told to fill only whole days strictly
**below** that floor's local day. In the common case (the recorder keeps more
than the window spans) the recorder covers the whole rolling window at real
seconds, partial oldest day included, and the ledger adds nothing — so the seam
is simply the recorder floor's local day, with the single partial oldest day
owned by the recorder. When the window is longer than the recorder keeps (always
for `365d`; for `30d` only if retention is shorter than 30 days), the purged head
falls to the ledger as whole days, and the single partial oldest day is
**dropped** from the ledger rather than summed as a whole 86400 bucket — a
one-directional guard that can only under-count the far edge, never over-count it
(the mirror of the `last_week` seam fix).

**Block accumulation and the glitch filter (R6).** The recorder result is
queried so its first row is the state in force at the window start, and
consecutive same-state rows merge into one contiguous block. If a minimum
state-duration is configured (**default 0, which disables the filter**), any
block shorter than it is treated as a **glitch**: a glitch with a predecessor
has its seconds merged into the **preceding** surviving block and is dropped from
both the seconds and the entry count; a leading glitch with no predecessor is
dropped outright as unattributable; and if the block after an absorbed glitch
shares the predecessor's state, it coalesces into one visit rather than two. With
the default of 0, nothing is a glitch and every block survives.

**Counting a straddling visit once.** The entry **count** for each state is the
number of qualifying entries into it within the window — **except** the leading
in-force block when its own last-changed time predates the window. That leading
block is a continuation of a visit that began earlier; it contributes seconds
but a count of zero, so a visit straddling midnight is not counted twice across
the ledger-day / today-slice seam. It was already counted on its true start day.

**Carry-forward across an HA-down gap (R4).** While HA is down the recorder holds
no rows, so the last-known state is carried forward across the gap (matching
`history_stats` — correct for steady entities like a light left on). The one
correction: HA writes an `unavailable` row on a clean shutdown; if that is the
**last** row the recorder returns and there is more than one row, it is dropped so
the gap is attributed to the preceding real state, not to `unavailable`. An
`unavailable` row that is not last (a genuine transient) is left alone, and a
sole trailing shutdown marker is kept (dropping it would erase all history).

**The open-visit midnight carry.** An entity held in one state across local
midnight has recorded no transition yet, so the just-closed day's tail sits in
neither the recorder slice (which starts at today's midnight) nor the ledger (the
fold only writes on the *next* transition). Without correction a cumulative
frame would briefly dip at midnight. At **read time only**, the open visit's real
per-day seconds are attributed to each spanned closed day inside the window and
below the seam — unless that day is already **owned** by the ledger, which
requires **both** that the ledger's watermark covers the day **and** that the
day's bucket is non-empty. Both conditions are needed: the watermark alone can
advance over a day the recorder returned nothing for (HA down all day), and a
non-empty check alone can be fooled by an earlier closed visit of the same state.
This carry is never written to the ledger and is idempotent per tick, so the
eventual real fold cannot double-count it.

**Unaccounted time.** `unaccounted_seconds` is the window time attributed to no
state — a pre-data gap and/or the lag of a just-discovered open state. It is kept
**out** of the per-state breakdown, counts, and averages, so per-state numbers
stay pure and the dominant can never be `"unaccounted"`. In all-states mode
`breakdown_pct` carries the extra `"unaccounted"` key and is balanced to sum to
**exactly 100.00**: a genuine gap takes `100 − Σreals`, and when the window is
fully covered the tiny rounding drift is folded into the largest real slice. A
state that holds real time but rounds to 0.0% is floored to a 0.01% sentinel so a
tiny-but-present state never renders as exactly zero.

**Coverage (R7).** When a window reaches further back than the oldest data held,
the frame reports a `data_start` (so the card can say "since &lt;date&gt;"), a
`window_coverage` fraction below 1.0, and `has_gap` true. A fully covered window
reports no `data_start`, coverage 1.0, and no gap.

**Dominant and its hysteresis (R10).** The dominant is the max-seconds state,
but it only flips **away** from the previous dominant when the new leader beats
it by more than **1.0% of the window**. Near-ties therefore keep the incumbent
and don't flap tick to tick. The dominant is picked only from real states (never
the unaccounted remainder). This hysteresis lives in the frame math, so by the
time an edge reaches the trigger emitter it is already debounced — no second
debounce is applied there.

**Compliance.** The tracked-state share and the target-set share each resolve
every configured state to the one real recorded key it denotes, sum exactly on
those keys, round, and clamp to 0–100. The 100 cap guards against sub-second
seam/rounding noise rendering as a nonsensical "137% compliance"; it is not a
substitute for the overflow diagnostic, which still surfaces a real over-count
via `unaccounted_seconds` and a coordinator warning.

**Prune cutoff (R9).** The ledger keeps buckets back to the earliest enabled
frame's start (resolved through the same single boundary helper) minus a 2-day
margin, floored at **400 days** back. The floor guarantees an always-on `year`
frame near a leap-year boundary keeps its Jan 1 bucket. Days older than the
cutoff are dropped.

---

## 5. Dominant hysteresis across a restart

The dominant's hysteresis incumbent is persisted per frame so it survives a
restart. On the first tick after a restart there is no in-memory prior, so the
incumbent is re-seeded from the ledger. Without that re-seed the first tick would
pick the raw max-seconds leader, silently flipping a near-tie that hysteresis had
pinned — and because there is no prior, that flip would fire **no edge** (see
Invariant A below), so it would be an invisible, permanent flip. After each
successful frame compute the reported dominant is written back; a `None` dominant
(a window with no data) deletes any stale stored value so a later boot cannot
resurrect it, and a stored dominant for a frame that is no longer enabled is
dropped.

---

## 6. Edge emission — Invariants A, B, C, D

Once per enabled frame per tick, after that frame is computed, the coordinator
decides whether to fire `dominant_changed` and/or a compliance edge. It compares
this tick's result against the previous tick's result for the same frame, and
runs four guards in order.

**Invariant A — no prior, no edge.** If there is no previous result for the
frame, return without firing. The previous result is absent on the very first
refresh (and for a frame enabled mid-session), so this prevents a spurious
startup edge and an edge the moment a new frame comes online. (This is the guard
that, together with the restart re-seed in §5, makes a cross-restart near-tie
flip not happen at all rather than fire.)

**Invariant D — the calendar-frame guard (the 0.2.1 fix).** For a **calendar**
frame only, suppress the edge when **either**:

- **(a) the window has not yet aged past the settle period** (`window_settled`
  is false), **or**
- **(b) the previous and current results describe different windows** (the
  frame's `window_start` moved).

The settle period is **60 seconds** — equal to the one-minute poll interval — and
`window_settled` is computed per frame as "(now − this frame's start) ≥ 60s",
from aware-UTC bounds, so it is relative to each frame's own reset and holds even
where local midnight is not 00:00 UTC. Rolling frames **never enter this guard** —
their start slides every tick but never resets to a disjoint window, so their
dominant is continuous and real flips always fire.

The two conditions cover two distinct phantom modes, and neither subsumes the
other:

- **(a) Fresh-window noise** on open calendar frames (`today`, `week`, `month`,
  `year`). When a frame rolls its lower bound at a local boundary, the first
  poll or two see a window only seconds wide, so whatever state the entity
  happens to sit in at the boundary trivially "dominates" a near-empty window.
  That is the first sample, not a real dominant — hysteresis can't damp it (its
  margin is a percent of a tiny window), and which state leads is decided by
  sub-second refresh-vs-transition timing. If the reset tick captures a transient
  loser and the true leader overtakes a *later* tick in the **same** window,
  condition (b) alone would miss it (same `window_start`), so the settle age is
  needed to blanket the whole fresh period.
- **(b) Disjoint-window roll** on closed calendar frames (`yesterday`,
  `last_week`, `last_month`) and the reset tick of open frames. A freshly-rolled
  closed window is a complete, old period (old enough to pass condition (a)), but
  the previous result describes the *previous* period and the current result the
  *new* one — two unrelated windows. Comparing their dominants — e.g. `yesterday`
  rolling from a day whose dominant was `off` to one whose dominant was `on` —
  would fire a phantom for an entity that never changed state. The moved
  `window_start` catches this at every calendar roll.

**Consequence:** closed calendar frames (`yesterday`, `last_week`, `last_month`)
**never** emit `dominant_changed` or compliance edges. Their dominant and
compliance are fixed historical facts; the daily/weekly/monthly roll is not a
real state change. **Tradeoff** (mode (a) only): a genuine flip within the first
60 seconds of a fresh open-frame window is delayed by at most one tick — it fires
on the next tick once the window has aged past settle, if the flip still holds, or
is dropped if it was a startup transient. This is the correct bias: a window's
dominant is not meaningful seconds after it opens.

**Invariant B — dominant real → real (all-states only).** `dominant_changed`
fires only when all hold: the tracker is in all-states mode; both the previous
and current dominant are real states (not `None`, which happens at count zero);
and they differ. Firing into or out of `None` is not a meaningful flip. It is
gated to all-states mode because the dominant is only surfaced as an entity (the
breakdown sensor) in that mode; in specific mode the user has no dominant entity.
No extra hysteresis is applied — it already lives upstream in the frame math.

**Invariant C — compliance known → known.** A compliance edge fires only when the
previous and current verdicts are both known (not `None`) and differ; it then
fires `compliant_on` if now compliant, else `compliant_off`. An "unknown" verdict
is deliberately not an edge. The verdict comes from the same single function the
compliant binary sensor uses — the shared function guarantees the same verdict
**value**; the trigger additionally applies the A/C/D edge guards, so it may
legitimately *not* fire on a verdict flip the sensor shows (a fresh or closed
calendar window), which is edge suppression, not a verdict disagreement. Because
that function is a bare threshold compare with no hysteresis, compliance edges
can flap tick to tick if the percentage oscillates around the bar. That flap is
accepted on purpose, to keep the trigger's and the sensor's verdict identical.

Because B and C both run after the A/D guards, one tick on one frame can fire
both a `dominant_changed` and a `compliant_on`/`compliant_off`.

**`new_state`** is separate from the per-frame emitter. It fires from the live
transition path, only in all-states mode, and only the first time a state name is
seen this session. The "seen" set is seeded from the ledger at startup so an
already-recorded state never re-announces, but it is **per-session, not
persisted** — so a restart may re-announce a long-dormant state. The set is
capped at 10,000 distinct states; past the cap, new-state tracking and
announcing stop and a single warning is logged. The event fires the bus event
only — it deliberately raises no persistent notification; build one in an
automation off the event if you want it.

---

## 7. Coverage and fill-in

A frame that reaches past the data the tracker holds reports a partial window:
`has_gap` true, a `window_coverage` fraction below 1.0, and a `data_start`
marking the oldest data. This is normal and expected for a newly enabled long
frame (`30d`, `month`, `year`, `365d`) — it has no history before you turned it
on and fills in as whole days accrue. The coverage fields let the card render
"since &lt;date&gt;" rather than pretend to a full window.

---

## 8. Persistence, restart, and write cadence

Source of truth: `storage.py` (the ledger document and its mutators) and the
load/fold/flush wiring in `coordinator.py`.

**What is persisted.** One JSON document per config entry, under storage key
`entity_state_tracker.{entry_id}` at storage version 1. Its heart is the
**daily ledger**: `local_day_iso → state → {"secs", "count"}`, where `secs` is
real elapsed wall-clock seconds in that state that local day and `count` is the
number of entries into it (a midnight-spanning visit counted once, on the day it
began). State keys are stored lowercased. Alongside the ledger it persists the
tracked/target config, the last state and its timestamp, the last fully-built
day, the per-state `last_entered` / `last_exited` stamps, the per-frame
`last_dominant` incumbents (§5), and the minimum state-duration the closed
buckets were last built with.

**What is not persisted.** The entire computed output — every `FrameResult`, all
breakdowns, percents, and dominants — is rebuilt every tick from ledger plus
recorder and never stored. Per-session-only state (lost on restart) includes the
"seen" set behind `new_state` (so a long-dormant state may be re-announced after
a restart) and the transient previous-state / failure-streak / overflow-warning
flags.

**Poll cadence.** The coordinator polls on a **one-minute** interval. Each tick
re-resolves every enabled frame's bounds, runs a single recorder query covering
the union of today's midnight and all rolling floors, computes every frame from
that shared result, fires any edges, and then flushes once.

**Open vs closed days, and the write path.** Backfill writes closed days
**wholesale**: on setup it recomputes each closed day from the last fully-built
day forward (or, on a first run, from the prune cutoff) up to today, and replaces
each day's bucket outright. The last-built marker advances only **after** a day's
write flushes, so a crash mid-backfill re-runs the same days rather than
double-counting them — each bucket equals ground truth exactly once. A day with
no recorded activity writes no bucket but still advances the marker. During live
running, each transition folds the previous state's elapsed seconds into the
correct day bucket(s), split at local midnights and counted once on the start
day; a fold drops any segment on a day the backfill already owns, so a mid-visit
restart cannot add pre-midnight seconds twice. The **open** current day is never
written to the ledger — its pre-boundary tail is carried into closed days at read
time only (§4), so the eventual real fold can't double-count it, and the open day
is recomputed fresh from the recorder every tick.

**Flush discipline.** The held ledger is the same object the store caches and
mutates, so live folds mutate in memory and only an explicit flush writes to
disk. The flush is a no-op unless the ledger is dirty, and it clears the dirty
flag before awaiting the save so a fold during the save re-dirties for the next
flush. It runs once per tick, on HA stop, and on shutdown. A failed disk write
during a per-tick flush is logged and swallowed — the in-memory frames are kept,
not discarded.

**Pruning.** Pruning runs in memory each tick (buckets older than the §4 cutoff),
and the single per-tick flush persists it — avoiding a second stacked disk write.

**Minimum-state-duration change.** The glitch threshold the closed buckets were
built with is persisted. On setup, if it differs from the configured value the
closed buckets are stale, so the daily ledger is cleared and the last-built
marker reset, forcing a full re-backfill with the new threshold; days older than
the recorder's retention cannot be rebuilt and refill over time. A fresh or
legacy ledger with the field unset is seeded without wiping, so introducing the
field never discards existing history.

**Fresh-install seed.** On a brand-new ledger the open visit is seeded by
anchoring the last state from HA's current state and its timestamp at **now** (not
the state's older last-changed), to avoid double-counting recorder-covered time,
so the first live transition does not drop the time held from start to that
transition. It only seeds when unset — it never clobbers a restored ledger.

**Resilience.** The loader is defensive at every level: malformed day buckets,
rows, and tracker entries are dropped rather than raised, and a corrupt or
unreadable store resets to empty rather than crashing. The fields added over the
integration's life (`last_entered`, `last_exited`, `last_dominant`, the built
threshold) default empty on an older store written before they existed —
explicitly without a storage-version bump.

---

## 9. When something goes wrong

Two runtime degradations surface to the user instead of failing silently.

**A single frame's compute error is isolated.** Each enabled frame is computed
independently; if one frame's compute raises a data anomaly
(`ValueError` / `ArithmeticError` / `LookupError`), that one frame **keeps its
previous tick's value** (the sensor freezes on its last-good number rather than
going unavailable), and a warning is logged. Because the frames dict is shared
entry-wide, this isolation is what stops one bad frame from flipping the whole
tracker's `last_update_success` to false and stranding **every** sensor. A
persistent failure must still surface: after **10 consecutive failures** (~10
minutes at the one-minute poll) a Repair issue (`frame_compute_failed`, severity
ERROR, naming the entity and the frame) is raised. It clears automatically on the
next successful compute of that frame, or when the frame is disabled. (A
programming error of any other kind is not swallowed — it propagates and fails
the tick visibly.)

**The recorder is required for accurate history.** This integration reconstructs
time-in-state from Home Assistant's recorder. If the recorder is disabled or
absent, the recorder query returns nothing: closed-day backfill stops, and each
frame degrades to a **live-only estimate** — the current open state carried from
its last-changed timestamp to now, with no intra-day transition reconstruction
(today's timeline cannot be rebuilt without recorded rows). A Repair issue
(`recorder_off`, severity WARNING) is raised **once** while the recorder is down
and cleared the moment a recorder query succeeds again, so a later outage can
raise it afresh.
