/**
 * Entity State Tracker Card
 * Custom Lovelace card for the Home Assistant Entity State Tracker integration.
 *
 * Renders the per-frame duration / breakdown sensors this integration emits as
 * one of three charts (bars | pie | table). English-only (house rule). One
 * self-contained file, vanilla LitElement via the home-assistant-main prototype.
 */

const CARD_VERSION = "0.1.0";

console.info(
  `%c ENTITY-STATE-TRACKER-CARD %c v${CARD_VERSION} %c — github.com/italo-lombardi `,
  "color: white; background: #7e57c2; font-weight: bold; padding: 2px 6px; border-radius: 3px 0 0 3px;",
  "color: #7e57c2; background: #ede7f6; font-weight: bold; padding: 2px 6px;",
  "color: #9e9e9e; background: #ede7f6; padding: 2px 6px; border-radius: 0 3px 3px 0;"
);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "entity-state-tracker-card",
  name: "Entity State Tracker Card",
  description:
    "Time-in-state and transition metrics per frame — bars, pie, or table.",
  preview: true,
  documentationURL:
    "https://github.com/italo-lombardi/Home-Assistant-EntityStateTracker",
});

// Canonical no-build LitElement bootstrap — matches the Entity Availability card
// and thomasloven/lovelace-card-tools. home-assistant-main / hui-view are in
// HA's initial bundle, so they're always defined before card JS runs.
const LitElement = Object.getPrototypeOf(
  customElements.get("home-assistant-main") || customElements.get("hui-view")
);
const html = LitElement.prototype.html;
const nothing = LitElement.prototype.nothing ?? "";
const css =
  LitElement.prototype.css ||
  (() => {
    class CSSResult {
      constructor(cssText) {
        this.cssText = cssText;
        this._styleSheet = null;
      }
      get styleSheet() {
        if (this._styleSheet === null && window.CSSStyleSheet) {
          try {
            this._styleSheet = new CSSStyleSheet();
            this._styleSheet.replaceSync(this.cssText);
          } catch (e) {
            this._styleSheet = null;
          }
        }
        return this._styleSheet;
      }
      toString() {
        return this.cssText;
      }
    }
    return (strings, ...values) =>
      new CSSResult(
        strings.reduce(
          (acc, str, i) =>
            acc + str + (values[i] != null ? String(values[i]) : ""),
          ""
        )
      );
  })();

// Canonical frame order + labels, mirroring helpers.py `_FRAME_LABELS`.
const FRAME_ORDER = ["today", "yesterday", "24h", "7d", "30d", "month", "year"];
const FRAME_LABELS = {
  today: "Today",
  yesterday: "Yesterday",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  month: "This month",
  year: "This year",
};

// Backend integration domain (device identifier + entity platform key). All of
// one tracker's entities live on a service device with identifier
// (DOMAIN, entry_id); the card discovers them by that device, never by parsing
// the entity_id string — so a user may rename any sensor's id freely.
const DOMAIN = "entity_state_tracker";

// translation_key values (backend TRANSLATION_KEY_*). The card charts the two
// per-frame metrics; the rest (percent/compliance/binary) aren't charted.
const TK_DURATION = "duration";
const TK_BREAKDOWN = "breakdown";
const TK_COMPLIANT = "compliant";
const CHART_METRICS = [TK_DURATION, TK_BREAKDOWN];

// Sort state names alphabetically, but keep the two "no reading" states
// (unavailable/unknown) at the end — they're noise, not a state you tracked on
// purpose. Mirrors the config-flow prefill sort so display and setup agree.
const _STATE_TAIL = { unavailable: 1, unknown: 1 };
function sortStates(states) {
  return [...states].sort((a, b) => {
    const ta = _STATE_TAIL[a] ? 1 : 0;
    const tb = _STATE_TAIL[b] ? 1 : 0;
    return ta - tb || String(a).localeCompare(String(b));
  });
}

// -----------------------------------------------------------------------------
// Deterministic per-state color (§5.3).
//
// The backend (helpers.state_color) hashes the state name with md5 → hue, HSL
// 0.55/0.55. md5 has no sync browser API, and the task brief says md5 in JS is
// overkill: what matters is that a state ALWAYS maps to the same color and a new
// slice never recolors the existing ones. So we use a stable FNV-1a string hash
// → hue with the SAME fixed S=0.55 / L=0.55. Deterministic and documented; the
// exact hue may differ from the backend but is consistent within the card.
// -----------------------------------------------------------------------------
function stateHue(state) {
  let h = 0x811c9dc5;
  const str = String(state);
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  // >>> 0 → unsigned; divide by 2^32 for a stable 0..1 hue.
  return (h >>> 0) / 4294967296;
}

function hueToRgb(p, q, t) {
  t %= 1.0;
  if (t < 0) t += 1.0;
  if (t < 1 / 6) return p + (q - p) * 6 * t;
  if (t < 1 / 2) return q;
  if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
  return p;
}

function stateColor(state) {
  const h = stateHue(state);
  const s = 0.55;
  const l = 0.55;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const r = hueToRgb(p, q, h + 1 / 3);
  const g = hueToRgb(p, q, h);
  const b = hueToRgb(p, q, h - 1 / 3);
  const hex = (x) =>
    Math.round(x * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

// -----------------------------------------------------------------------------
// Formatting helpers
// -----------------------------------------------------------------------------
function fmtDuration(seconds) {
  if (seconds == null || isNaN(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = s / 60;
  if (m < 60) return `${m.toFixed(m < 10 ? 1 : 0)} min`;
  const h = m / 60;
  if (h < 24) return `${h.toFixed(1)} h`;
  const d = h / 24;
  return `${d.toFixed(1)} d`;
}

function fmtPct(pct) {
  if (pct == null || isNaN(pct)) return "—";
  const n = Math.max(0, Math.min(100, Number(pct)));
  // A nonzero-but-tiny slice must not read "0.0%" (mirrors the backend sentinel);
  // display-only string, the numeric attribute stays numeric.
  if (n > 0 && n < 0.1) return "<0.1%";
  return `${n.toFixed(1)}%`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

// Compact "changed 2 h ago" / "since 14:32"-style suffix for the source
// entity's last_changed. Same-day → clock time ("since 14:32"); older → a
// coarse relative age ("changed 3 d ago"). Locale clock, no external deps.
function fmtLastChanged(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = Date.now();
  const diffMs = now - d.getTime();
  if (diffMs < 0) return "";
  const sameDay = new Date().toDateString() === d.toDateString();
  if (sameDay) {
    return `since ${d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    })}`;
  }
  const mins = Math.floor(diffMs / 60000);
  if (mins < 60) return `changed ${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `changed ${hrs} h ago`;
  const days = Math.floor(hrs / 24);
  return `changed ${days} d ago`;
}

// Registry-based discovery (mirrors the Entity Guard card). Every entity of one
// tracker lives on a service device with identifier (DOMAIN, entry_id); we key
// off that device_id and each entity's translation_key, NEVER the entity_id
// string — so a user may rename any sensor's id and the card still finds it.

// The device_id of the tracker whose config-entry id is `trackerId`, or "".
function deviceIdForTracker(hass, trackerId) {
  if (!hass || !trackerId) return "";
  const devices = hass.devices || {};
  for (const devId of Object.keys(devices)) {
    const ids = devices[devId]?.identifiers;
    if (!Array.isArray(ids)) continue;
    for (const pair of ids) {
      if (Array.isArray(pair) && pair[0] === DOMAIN && pair[1] === trackerId) {
        return devId;
      }
    }
  }
  return "";
}

// An entity's translation_key (registry entry, falling back to the live-state
// attribute HA copies onto states). This is the backend metric key
// (duration/breakdown/percent/…), stable across any entity_id rename.
function translationKeyOf(hass, id) {
  const entry = hass?.entities?.[id];
  const st = hass?.states?.[id];
  return entry?.translation_key || st?.attributes?.translation_key || null;
}

// The frame KEY a per-frame sensor reports, read straight off its `frame`
// attribute (backend emits the raw key: today/24h/7d/…). No id parsing.
function frameOf(hass, id) {
  return hass?.states?.[id]?.attributes?.frame ?? null;
}

// The chartable frame sensors of one tracker: sensor entities on the tracker's
// device whose translation_key is a chart metric (duration or breakdown) and
// which carry a frame. Returns [{entity_id, frame, translationKey}].
function trackerFrameSensors(hass, trackerId) {
  const devId = deviceIdForTracker(hass, trackerId);
  if (!devId) return [];
  const entities = hass.entities || {};
  const out = [];
  for (const id of Object.keys(entities)) {
    if (!id.startsWith("sensor.")) continue;
    if (entities[id]?.device_id !== devId) continue;
    const tk = translationKeyOf(hass, id);
    if (!CHART_METRICS.includes(tk)) continue;
    const frame = frameOf(hass, id);
    if (!frame) continue; // cold start before first coordinator refresh
    out.push({ entity_id: id, frame, translationKey: tk });
  }
  return out;
}

// Discover every tracker present in `hass`, each as {trackerId, label} — the
// value the card's `tracker` config expects. Iterates the service devices this
// integration registers (identifier (DOMAIN, entry_id)); label = device name.
function trackerOptions(hass) {
  if (!hass) return [];
  const devices = hass.devices || {};
  const out = [];
  for (const devId of Object.keys(devices)) {
    const ids = devices[devId]?.identifiers;
    if (!Array.isArray(ids)) continue;
    for (const pair of ids) {
      if (Array.isArray(pair) && pair[0] === DOMAIN) {
        const dev = devices[devId];
        out.push({
          trackerId: pair[1],
          label: dev.name_by_user || dev.name || pair[1],
        });
        break;
      }
    }
  }
  return out.sort((a, b) => a.label.localeCompare(b.label));
}

// The tracker's device name, or "".
function deviceNameOf(hass, trackerId) {
  const devId = deviceIdForTracker(hass, trackerId);
  const dev = devId ? hass?.devices?.[devId] : null;
  return (dev && (dev.name_by_user || dev.name)) || "";
}

// -----------------------------------------------------------------------------
const cardStyles = css`
  :host {
    --est-text-primary: var(--primary-text-color, #212121);
    --est-text-secondary: var(--secondary-text-color, #727272);
    --est-divider: var(--divider-color, rgba(0, 0, 0, 0.12));
    --est-bar-bg: var(--disabled-color, #bdbdbd);
    /* Darker grey for the specific-mode pie's "other" (non-tracked time) slice,
       so it reads as distinct from the lighter --est-bar-bg "No data" slice. */
    --est-bar-bg-alt: var(--secondary-text-color, #727272);
    --est-accent: var(--primary-color, #7e57c2);
  }

  ha-card {
    overflow: hidden;
  }

  .card-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 16px 16px 8px;
  }

  .header-icon {
    --mdc-icon-size: 22px;
    color: var(--est-accent);
    flex-shrink: 0;
  }

  .title {
    font-size: 16px;
    font-weight: 500;
    color: var(--est-text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .header-text {
    min-width: 0;
    flex: 1;
  }

  .source-context {
    font-size: 12px;
    color: var(--est-text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 2px;
  }

  .source-context .current {
    color: var(--est-text-primary);
    font-weight: 500;
  }

  .source-context .source-link {
    color: var(--est-text-primary);
    font-weight: 500;
    cursor: pointer;
    border-radius: 3px;
  }

  .source-context .source-link:hover {
    color: var(--est-accent);
    text-decoration: underline;
  }

  .source-context .source-link:focus-visible {
    outline: 2px solid var(--est-accent);
    outline-offset: 1px;
  }

  .body {
    padding: 4px 16px 16px;
  }

  .error-message {
    padding: 16px;
    color: var(--error-color, #f44336);
    font-size: 14px;
  }

  /* Bars */
  .bars-note {
    font-size: 13px;
    font-weight: 500;
    color: var(--est-text-primary);
    margin-bottom: 8px;
  }

  .bar-row {
    margin: 12px 0;
  }

  .clickable {
    cursor: pointer;
    border-radius: 6px;
  }

  .clickable:focus-visible {
    outline: 2px solid var(--est-accent);
    outline-offset: 2px;
  }

  .bar-label {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    margin-bottom: 4px;
    color: var(--est-text-primary);
  }

  .bar-label .frame {
    font-weight: 500;
  }

  .bar-label .value {
    color: var(--est-text-secondary);
  }

  .bar-track {
    position: relative;
    height: 10px;
    border-radius: 5px;
    background: var(--est-bar-bg);
    overflow: hidden;
  }

  .bar-fill {
    position: absolute;
    top: 0;
    left: 0;
    bottom: 0;
    border-radius: 5px;
    background: var(--est-accent);
  }

  /* Compliance status chip: met vs not-met symbol + score/target text, in place
     of the old second bar (a single pass/fail flag reads clearer than a bar). */
  .bar-compliance-status {
    display: flex;
    align-items: baseline;
    gap: 6px;
    font-size: 12px;
    margin-top: 4px;
  }

  .bar-compliance-status .mark {
    font-weight: 700;
    line-height: 1;
  }

  .bar-compliance-status.met .mark {
    color: var(--success-color, #4caf50);
  }

  .bar-compliance-status.unmet .mark {
    color: var(--error-color, #f44336);
  }

  .bar-compliance-status .text {
    color: var(--est-text-secondary);
  }

  .transitions {
    font-size: 12px;
    color: var(--est-text-secondary);
    margin-top: 3px;
  }

  .incomplete {
    background-image: repeating-linear-gradient(
      45deg,
      transparent,
      transparent 4px,
      rgba(255, 255, 255, 0.25) 4px,
      rgba(255, 255, 255, 0.25) 8px
    );
  }

  .since {
    font-size: 11px;
    font-style: italic;
    color: var(--est-text-secondary);
    margin-left: 6px;
  }

  /* Pie / donut */
  .pie-wrap {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    padding-top: 8px;
  }

  .pie-svg {
    flex-shrink: 0;
  }

  .legend {
    flex: 0 1 auto;
    min-width: 140px;
    max-width: 260px;
  }

  .legend-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    margin: 3px 0;
    color: var(--est-text-primary);
  }

  .legend-swatch {
    width: 12px;
    height: 12px;
    border-radius: 3px;
    flex-shrink: 0;
  }

  .legend-value {
    margin-left: 12px;
    color: var(--est-text-secondary);
    white-space: nowrap;
  }

  .frame-picker {
    font-size: 12px;
    color: var(--est-text-secondary);
    padding-bottom: 4px;
  }

  /* Table */
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  th,
  td {
    text-align: right;
    padding: 6px 8px;
    border-bottom: 1px solid var(--est-divider);
  }

  th:first-child,
  td:first-child {
    text-align: left;
  }

  thead th {
    color: var(--est-text-secondary);
    font-weight: 500;
  }

  td.state-cell {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .cell-secondary {
    color: var(--est-text-secondary);
    font-size: 11px;
  }
`;

class EntityStateTrackerCard extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
    };
  }

  static get styles() {
    return cardStyles;
  }

  static getConfigElement() {
    return document.createElement("entity-state-tracker-card-editor");
  }

  static getStubConfig(hass) {
    // Preview: offer the first discovered tracker (by its config-entry id).
    // Harmless literal fallback if none is present.
    const [first] = trackerOptions(hass);
    return {
      tracker_id: first ? first.trackerId : "",
      chart: "bars",
    };
  }

  constructor() {
    super();
    this._config = {};
  }

  setConfig(config) {
    if (!config || !config.tracker_id) {
      throw new Error(
        "You must define a 'tracker_id' in the card configuration."
      );
    }
    const chart = config.chart || "bars";
    if (!["bars", "pie", "table"].includes(chart)) {
      throw new Error(`Unknown chart type '${chart}' (use bars|pie|table).`);
    }
    this._config = { chart, ...config };
  }

  getCardSize() {
    return this._config.chart === "table" ? 4 : 3;
  }

  shouldUpdate(changedProps) {
    if (changedProps.has("_config")) return true;
    if (!this.hass) return false;
    const oldHass = changedProps.get("hass");
    if (!oldHass) return true;
    const ids = this._trackerSensors().map((s) => s.entity_id);
    return ids.some((id) => oldHass.states[id] !== this.hass.states[id]);
  }

  // The tracker's chartable frame sensors, discovered by device_id +
  // translation_key (never by parsing the entity_id — ids are user-renameable).
  // Reads each sensor's frame from its `frame` attribute. Returns the render
  // shape {entity_id, frame, state, attrs} in canonical frame order.
  _trackerSensors() {
    const found = trackerFrameSensors(this.hass, this._config.tracker_id);
    const out = found.map(({ entity_id, frame }) => {
      const st = this.hass.states[entity_id];
      return { entity_id, frame, state: st.state, attrs: st.attributes };
    });
    out.sort((a, b) => FRAME_ORDER.indexOf(a.frame) - FRAME_ORDER.indexOf(b.frame));
    return out;
  }

  _framesToShow(sensors) {
    const filter = this._config.frames;
    if (!Array.isArray(filter) || filter.length === 0) return sensors;
    return sensors.filter((s) => filter.includes(s.frame));
  }

  render() {
    if (!this._config || !this.hass) {
      return html`<ha-card
        ><div class="error-message">Card not configured.</div></ha-card
      >`;
    }
    const all = this._trackerSensors();
    if (all.length === 0) {
      return html`<ha-card>
        <div class="error-message">
          No Entity State Tracker sensors found for this tracker.
        </div>
      </ha-card>`;
    }
    const sensors = this._framesToShow(all);
    if (sensors.length === 0) {
      return html`<ha-card>
        <div class="error-message">No frames match the configured filter.</div>
      </ha-card>`;
    }

    const title = this._config.title ?? this._deriveTitle(sensors);
    let body;
    if (this._config.chart === "pie") body = this._renderPie(sensors);
    else if (this._config.chart === "table") body = this._renderTable(sensors);
    else body = this._renderBars(sensors);

    return html`<ha-card>
      <div class="card-header">
        <ha-icon class="header-icon" icon="mdi:chart-timeline-variant"></ha-icon>
        <div class="header-text">
          <div class="title">${title}</div>
          ${this._renderSourceContext(sensors)}
        </div>
      </div>
      <div class="body">${body}</div>
    </ha-card>`;
  }

  // Per-card context for the tracked SOURCE entity (all frames share one
  // source). Reads the `source_entity` attribute the backend exposes on every
  // frame sensor; resolves its friendly name + live state + last_changed from
  // hass.states. Degrades to nothing when the attribute is absent (older
  // sensor) or the source entity isn't in hass (unloaded/unknown).
  _renderSourceContext(sensors) {
    const sourceId = (sensors[0]?.attrs || {}).source_entity;
    if (!sourceId) return nothing;
    const st = this.hass.states[sourceId];
    const friendly = (st && st.attributes && st.attributes.friendly_name) || sourceId;
    // The source name opens the tracked entity's more-info dialog (EA parity).
    // role/tabindex + Enter/Space keep it keyboard-accessible; the click fires
    // hass-more-info for the SOURCE entity (not the tracker sensor).
    const name = html`<span
      class="source-link"
      role="button"
      tabindex="0"
      @click=${(e) => this._handleEntityClick(e, sourceId)}
      @keydown=${(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          this._handleEntityClick(e, sourceId);
        }
      }}
      >${friendly}</span
    >`;
    if (!st) {
      return html`<div class="source-context">
        Tracking: ${name} · unavailable
      </div>`;
    }
    const changed = fmtLastChanged(st.last_changed);
    return html`<div class="source-context">
      Tracking: ${name} · now
      <span class="current">${st.state}</span>${changed
        ? html` · ${changed}`
        : nothing}
    </div>`;
  }

  // Open the more-info dialog for an entity (EA parity). Stops propagation so a
  // click on the source name doesn't also trigger any card-level handler; fires
  // the standard `hass-more-info` event HA's dialog manager listens for. Guards
  // a missing/non-string id (never dispatches an empty more-info).
  _handleEntityClick(e, entityId) {
    e.stopPropagation();
    if (typeof entityId !== "string" || !entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      })
    );
  }

  // The frame's Compliant binary sensor id (same device, translation_key
  // "compliant", matching frame), or null. Keyed off the bar sensor's OWN
  // device_id so it works no matter how the tracker was configured, and never
  // parses an entity_id string (rename-safe).
  _frameCompliantId(s) {
    const devId = this.hass?.entities?.[s.entity_id]?.device_id;
    if (!devId) return null;
    const entities = this.hass.entities || {};
    // ponytail: O(rows×entities) scan per render; index by device_id if the card
    // ever renders on huge installs. Repaints are user-scale, so it's free today.
    for (const id of Object.keys(entities)) {
      if (!id.startsWith("binary_sensor.")) continue;
      if (entities[id].device_id !== devId) continue;
      if (translationKeyOf(this.hass, id) !== TK_COMPLIANT) continue;
      if (frameOf(this.hass, id) === s.frame) return id;
    }
    return null;
  }

  _deriveTitle(sensors) {
    // The device carries the human name the user configured ("Entity State
    // Tracker — Global, Any Light"); read it from the registry by tracker_id.
    // Fall back to the source entity's friendly name only when the device
    // registry isn't reachable (e.g. card preview without a full hass).
    return (
      deviceNameOf(this.hass, this._config.tracker_id) ||
      (sensors[0]?.attrs || {}).source_entity ||
      "Entity State Tracker"
    );
  }

  // A sensor is a breakdown (all-states) sensor when it carries the breakdown
  // dicts; otherwise it's a specific-mode duration sensor.
  _isBreakdown(s) {
    return s.attrs && s.attrs.breakdown_seconds != null;
  }

  _incomplete(attrs) {
    return (attrs.window_coverage != null && attrs.window_coverage < 1) ||
      attrs.has_gap === true;
  }

  // ---------------------------------------------------------------------------
  // Bars: one row per frame. % fill + "6.2 h · 26%"; compliance pass/fail chip
  // (✓/✗ + score) when a target is set; transition line from counts/avg_duration_seconds.
  // ---------------------------------------------------------------------------
  _renderBars(sensors) {
    // Header rows above the bars: the tracked state(s) and the compliance target
    // (stated once, instead of repeating "(target ≥ N%)" on every row's chip).
    const header = this._metaHeader(sensors[0]?.attrs || {});
    const rows = sensors.map((s) => {
      const a = s.attrs || {};
      let pct;
      let durSecs;
      if (this._isBreakdown(s)) {
        // Dominant state's slice for the bar; state name is s.state.
        durSecs = (a.breakdown_seconds || {})[s.state];
        pct = (a.breakdown_pct || {})[s.state];
      } else {
        // Prefer the raw seconds attr (HA serves s.state already unit-converted
        // to hours, so Number(s.state) here is unit-ambiguous). Fall back only
        // for older backends without duration_seconds.
        durSecs =
          a.duration_seconds != null ? Number(a.duration_seconds) : Number(s.state);
        pct = a.percent;
      }
      const pctNum = pct == null ? 0 : Math.max(0, Math.min(100, Number(pct)));
      const incomplete = this._incomplete(a);
      const label = this._isBreakdown(s) ? `${s.state}` : "";
      const compliantId = this._frameCompliantId(s);
      // Row opens its own duration/breakdown sensor; the compliance chip opens
      // that frame's Compliant binary sensor instead (its click stops bubbling).
      return html`<div
        class="bar-row clickable"
        role="button"
        tabindex="0"
        @click=${(e) => this._handleEntityClick(e, s.entity_id)}
        @keydown=${(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            this._handleEntityClick(e, s.entity_id);
          }
        }}
      >
        <div class="bar-label">
          <span class="frame"
            >${FRAME_LABELS[s.frame] || s.frame}${label
              ? html` <span class="cell-secondary">· ${label}</span>`
              : nothing}</span
          >
          <span class="value"
            >${fmtDuration(durSecs)} · ${fmtPct(pct)}${incomplete && a.data_start
              ? html`<span class="since"
                  >since ${fmtDate(a.data_start)}</span
                >`
              : nothing}</span
          >
        </div>
        <div class="bar-track">
          <div
            class="bar-fill ${incomplete ? "incomplete" : ""}"
            style="width:${pctNum}%;${this._isBreakdown(s)
              ? `background:${stateColor(s.state)};`
              : ""}"
          ></div>
        </div>
        ${a.compliance_percent != null
          ? this._complianceStatus(a, compliantId)
          : nothing}
        ${this._transitionLine(a, this._isBreakdown(s) ? s.state : null)}
      </div>`;
    });
    return html`${header}${rows}`;
  }

  // Compliance status chip: pass/fail mark + score, replacing the old second
  // bar. Met = score ≥ threshold (or no threshold → always met, just informational).
  // ✓ green when met, ✗ red when a threshold exists and is missed. Plain unicode
  // marks (no icon dep). The target itself is stated once in the bars header, not
  // repeated per row. Caller gated on compliance_percent. When compliantId is set
  // the chip opens that frame's Compliant binary sensor (click stops bubbling so
  // it doesn't also trigger the row's duration more-info).
  _complianceStatus(a, compliantId) {
    const pct = Number(a.compliance_percent);
    const hasTarget = a.target_threshold != null;
    const met = !hasTarget || pct >= Number(a.target_threshold);
    const text = hasTarget
      ? `${met ? "Compliant" : "Not compliant"} · ${fmtPct(a.compliance_percent)}`
      : `compliance ${fmtPct(a.compliance_percent)}`;
    const clickable = compliantId
      ? {
          class: `bar-compliance-status clickable ${met ? "met" : "unmet"}`,
          role: "button",
          tabindex: "0",
          click: (e) => this._handleEntityClick(e, compliantId),
          keydown: (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              this._handleEntityClick(e, compliantId);
            }
          },
        }
      : null;
    if (!clickable) {
      return html`<div class="bar-compliance-status ${met ? "met" : "unmet"}">
        <span class="mark">${met ? "✓" : "✗"}</span>
        <span class="text">${text}</span>
      </div>`;
    }
    return html`<div
      class=${clickable.class}
      role="button"
      tabindex="0"
      @click=${clickable.click}
      @keydown=${clickable.keydown}
    >
      <span class="mark">${met ? "✓" : "✗"}</span>
      <span class="text">${text}</span>
    </div>`;
  }

  // Self-explanatory per-state transition line, state name ADJACENT to the
  // count so it reads unambiguously as "this state, entered N times, avg M per
  // visit". Breakdown: stateKey = the row's state. Specific: label from
  // tracked_states (1 → its name; several → "tracked states"). Renders
  // "on — 2 visits · avg 5 min"; drops the "— …" tail when the state is unknown
  // (specific mode with no tracked_states) so it never dangles a bare count.
  _transitionLine(attrs, stateKey) {
    const counts = attrs.counts || {};
    const avg = attrs.avg_duration_seconds || {};
    let count;
    let avgSecs;
    let label;
    if (stateKey != null) {
      count = counts[stateKey];
      avgSecs = avg[stateKey];
      label = stateKey;
    } else {
      const keys = Object.keys(counts);
      count = keys.reduce((n, k) => n + (counts[k] || 0), 0);
      // Weighted-ish: just show avg of the first tracked state if present.
      avgSecs = keys.length ? avg[keys[0]] : null;
      const tracked = Array.isArray(attrs.tracked_states)
        ? attrs.tracked_states
        : null;
      if (tracked && tracked.length === 1) label = tracked[0];
      else if (tracked && tracked.length > 1) label = "tracked states";
      else label = null;
    }
    if (count == null || count === 0) return nothing;
    const visits = `${count} ${count === 1 ? "visit" : "visits"}`;
    const avgTxt = avgSecs != null ? ` · avg ${fmtDuration(avgSecs)} per visit` : "";
    // State name adjacent to the count. No label (specific mode, no tracked
    // states) → plain "N visits", never a dangling bare count.
    return html`<div class="transitions">
      ${label != null ? `${label} — ${visits}` : visits}${avgTxt}
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Shared header: tracked state(s) + compliance target, stated once. Used by
  // the bars and pie views so both carry the same context line(s).
  // ---------------------------------------------------------------------------
  _metaHeader(a) {
    const tracked = a.tracked_states;
    const threshold = a.target_threshold;
    const trackedLine =
      Array.isArray(tracked) && tracked.length
        ? html`<div class="bars-note">
            Tracked ${tracked.length > 1 ? "states" : "state"}:
            ${sortStates(tracked).join(", ")}
          </div>`
        : nothing;
    return html`${trackedLine}${threshold != null
      ? html`<div class="bars-note">Compliance target ≥ ${threshold}%</div>`
      : nothing}`;
  }

  // ---------------------------------------------------------------------------
  // Compliance gauge: a 2-slice donut answering "how much of the frame met the
  // target set" — compliant (green) vs not (red), from the aggregate
  // compliance_percent. Opt-in (config.compliance_pie), only meaningful when a
  // target threshold is set. Never blank: a full green/red ring at 100%/0%.
  // ---------------------------------------------------------------------------
  _renderComplianceGauge(a) {
    const pct = a.compliance_percent;
    if (pct == null) return nothing; // N/A window — nothing to gauge.
    const threshold = a.target_threshold;
    const met = threshold == null || Number(pct) >= Number(threshold);
    const green = "var(--success-color, #4caf50)";
    const red = "var(--error-color, #f44336)";
    // Two proportion-only slices (no seconds — compliance is a ratio). The met
    // slice takes the compliance color; the remainder is neutral grey so a
    // "compliant" gauge reads as green-on-grey, not green-vs-red alarm.
    const p = Math.max(0, Math.min(100, Number(pct)));
    const slices = [
      { state: met ? "Compliant" : "Not compliant", secs: p, pct: p, color: met ? green : red },
      { state: "shortfall", secs: 100 - p, pct: 100 - p, color: "var(--est-bar-bg)" },
    ].filter((s) => s.secs >= 0.05);
    const total = slices.reduce((n, s) => n + s.secs, 0) || 1;
    const cx = 50, cy = 50, r = 40, inner = 24;
    let angle = -Math.PI / 2;
    const single = slices.length === 1;
    const paths = slices.map((s) => {
      const frac = s.secs / total;
      if (single || frac >= 0.9999) {
        return { d: this._ring(cx, cy, r, inner), color: s.color, evenodd: true };
      }
      const a0 = angle;
      const a1 = angle + frac * 2 * Math.PI;
      angle = a1;
      return { d: this._arc(cx, cy, r, inner, a0, a1), color: s.color, evenodd: false };
    });
    return html`
      <div class="frame-picker">
        Compliance${threshold != null ? html` (target ≥ ${threshold}%)` : nothing}
      </div>
      <div class="pie-wrap">
        ${this._pieSvg(paths)}
        <div class="legend">
          ${slices.map(
            (s) => html`<div class="legend-item">
              <span class="legend-swatch" style="background:${s.color}"></span>
              <span>${s.state}</span>
              <span class="legend-value">${fmtPct(s.pct)}</span>
            </div>`
          )}
        </div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Pie/donut: one frame's breakdown. all-states → every state; specific →
  // in-state vs rest. Deterministic per-state color.
  // ---------------------------------------------------------------------------
  _renderPie(sensors) {
    // Pick the configured frame, else the first available.
    const pick =
      sensors.find((s) => s.frame === this._config.frame) || sensors[0];
    const a = pick.attrs || {};
    let slices;
    if (this._isBreakdown(pick)) {
      const bs = a.breakdown_seconds || {};
      slices = Object.keys(bs).map((state) => ({
        state,
        secs: bs[state],
        pct: (a.breakdown_pct || {})[state],
        color: stateColor(state),
      }));
      // Trailing grey slice for window time attributed to no state, so the
      // donut visibly sums to 100. "No data" when the window predates our
      // history, else "In progress" (a transient open-state lag). Guarded on
      // the attr being present (falsy/absent on older backends).
      const gap = Number(a.unaccounted_seconds);
      if (gap > 0) {
        const ws = Number(a.window_seconds) || 0;
        slices.push({
          state: a.has_gap ? "No data" : "In progress",
          secs: gap,
          pct: ws > 0 ? (gap / ws) * 100 : null,
          color: "var(--est-bar-bg)",
        });
      }
    } else {
      // Specific mode: build three REAL-seconds slices from the DurationSensor's
      // attrs — identical shape to all-states breakdown — so the donut is never
      // blank (a full grey ring at 0% in-state) and never mislabels absence of
      // data as time in a non-tracked state:
      //   in-state = duration_seconds
      //   other    = window_seconds - duration_seconds - unaccounted_seconds
      //   no-data  = unaccounted_seconds  ("No data" past window / "In progress")
      // Prefer the raw seconds attr; pick.state is unit-converted to hours by HA
      // and thus unit-ambiguous.
      const inSecs =
        (a.duration_seconds != null ? Number(a.duration_seconds) : Number(pick.state)) || 0;
      const ws = Number(a.window_seconds) || 0;
      const gap = Math.max(0, Number(a.unaccounted_seconds) || 0);
      const other = ws > 0 ? Math.max(0, ws - inSecs - gap) : 0;
      const denom = ws > 0 ? ws : inSecs + other + gap;
      const pctOf = (secs) => (denom > 0 ? (secs / denom) * 100 : null);
      const tracked = (a.tracked_states || []).join(", ") || "tracked";
      slices = [
        { state: tracked, secs: inSecs, pct: pctOf(inSecs), color: stateColor(tracked) },
        { state: "other", secs: other, pct: pctOf(other), color: "var(--est-bar-bg-alt)" },
      ];
      if (gap > 0) {
        slices.push({
          state: a.has_gap ? "No data" : "In progress",
          secs: gap,
          pct: pctOf(gap),
          color: "var(--est-bar-bg)",
        });
      }
    }
    // Drop sub-second slices: the engine counts the current open state up to
    // `now`, so a fully-covered window leaves only floating-point residue in
    // unaccounted/other (e.g. 0.4s) — a dust slice that rounds to "0 s" and
    // clutters the legend. >=1s is the smallest slice worth drawing here.
    slices = slices.filter((s) => s.secs >= 1);
    const total = slices.reduce((n, s) => n + s.secs, 0) || 1;

    // Build a donut with stacked conic-gradient-free SVG arcs.
    const cx = 50;
    const cy = 50;
    const r = 40;
    const inner = 24;
    let angle = -Math.PI / 2;
    const single = slices.length === 1;
    // Compute each slice as a plain {d, color, evenodd} descriptor, then build
    // the whole <svg> IMPERATIVELY with document.createElementNS below. HA's
    // LitElement prototype exposes no `svg` template tag, and a <path>
    // interpolated into an `html` <svg> is parsed standalone in the HTML
    // namespace → an unknown element that never paints (the "legend shows,
    // arcs don't" bug). createElementNS guarantees the SVG namespace, so the
    // geometry renders; Lit renders a returned Node value in place as-is.
    const paths = slices.map((s) => {
      const frac = s.secs / total;
      // A full-circle slice (only slice, or frac≈1) has coincident arc
      // endpoints → a zero-length, INVISIBLE path. Draw a donut RING (outer
      // circle + inner hole punched via even-odd fill) instead.
      if (single || frac >= 0.9999) {
        return { d: this._ring(cx, cy, r, inner), color: s.color, evenodd: true };
      }
      const a0 = angle;
      const a1 = angle + frac * 2 * Math.PI;
      angle = a1;
      return {
        d: this._arc(cx, cy, r, inner, a0, a1),
        color: s.color,
        evenodd: false,
      };
    });

    const incomplete = this._incomplete(a);
    // Opt-in compliance gauge below the state donut — only when the card asks
    // for it AND the frame has a compliance figure to show.
    const gauge =
      this._config.compliance_pie && a.compliance_percent != null
        ? this._renderComplianceGauge(a)
        : nothing;
    return html`
      ${this._metaHeader(a)}
      <div class="frame-picker">
        ${FRAME_LABELS[pick.frame] || pick.frame}${incomplete && a.data_start
          ? html`<span class="since">since ${fmtDate(a.data_start)}</span>`
          : nothing}
      </div>
      <div class="pie-wrap">
        ${this._pieSvg(paths)}
        <div class="legend">
          ${slices.map(
            (s) => html`<div class="legend-item">
              <span class="legend-swatch" style="background:${s.color}"></span>
              <span>${s.state}</span>
              <span class="legend-value"
                >${fmtDuration(s.secs)} · ${fmtPct(s.pct)}</span
              >
            </div>`
          )}
        </div>
      </div>
      ${gauge}
    `;
  }

  // Build the donut <svg> as a real SVG-namespaced DOM node. Every element is
  // created with createElementNS(NS, …) so <path> lands in
  // http://www.w3.org/2000/svg and the browser renders it as geometry. Lit
  // renders a Node child value directly (no re-parsing, no namespace loss),
  // which sidesteps the missing `svg` template tag entirely. Rebuilt each
  // render — cheap (a handful of nodes) and always fresh.
  _pieSvg(paths) {
    const NS = "http://www.w3.org/2000/svg";
    const el = document.createElementNS(NS, "svg");
    el.setAttribute("class", "pie-svg");
    el.setAttribute("width", "100");
    el.setAttribute("height", "100");
    el.setAttribute("viewBox", "0 0 100 100");
    for (const p of paths) {
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", p.d);
      path.setAttribute("fill", p.color);
      if (p.evenodd) path.setAttribute("fill-rule", "evenodd");
      el.appendChild(path);
    }
    return el;
  }

  // Full donut RING for a single 100% slice — outer + inner circle as one
  // even-odd path so the inner hole is punched out (an arc path with coincident
  // endpoints would be zero-length and invisible). Two arcs per circle since SVG
  // arcs can't span a full 360°. Returns the `d` string; the caller emits the
  // <path> inline inside the <svg> template (SVG namespace).
  _ring(cx, cy, r, ri) {
    return (
      `M ${cx - r} ${cy} A ${r} ${r} 0 1 1 ${cx + r} ${cy} ` +
      `A ${r} ${r} 0 1 1 ${cx - r} ${cy} Z ` +
      `M ${cx - ri} ${cy} A ${ri} ${ri} 0 1 0 ${cx + ri} ${cy} ` +
      `A ${ri} ${ri} 0 1 0 ${cx - ri} ${cy} Z`
    );
  }

  // SVG donut-segment path between two angles (radians), outer radius r, inner ri.
  _arc(cx, cy, r, ri, a0, a1) {
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const x0 = cx + r * Math.cos(a0);
    const y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1);
    const y1 = cy + r * Math.sin(a1);
    const xi1 = cx + ri * Math.cos(a1);
    const yi1 = cy + ri * Math.sin(a1);
    const xi0 = cx + ri * Math.cos(a0);
    const yi0 = cy + ri * Math.sin(a0);
    return (
      `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} ` +
      `L ${xi1} ${yi1} A ${ri} ${ri} 0 ${large} 0 ${xi0} ${yi0} Z`
    );
  }

  // ---------------------------------------------------------------------------
  // Table: rows = states, columns = frames (duration + %). Best for all-states.
  // ---------------------------------------------------------------------------
  _renderTable(sensors) {
    // Collect the union of states across frames (breakdown mode). For specific
    // mode, the single "tracked" row.
    const stateSet = new Set();
    let anyGap = false;
    for (const s of sensors) {
      if (this._isBreakdown(s)) {
        Object.keys((s.attrs || {}).breakdown_seconds || {}).forEach((st) =>
          stateSet.add(st)
        );
        if (Number((s.attrs || {}).unaccounted_seconds) > 0) anyGap = true;
      } else {
        stateSet.add(((s.attrs || {}).tracked_states || []).join(", ") || "tracked");
      }
    }
    // Trailing pseudo-row for window time attributed to no state (breakdown
    // mode), mirroring the pie's grey slice so the table columns sum to 100.
    const GAP_ROW = "__gap__"; // sentinel key, never a real state name
    // Order rows by total seconds desc for a stable, readable table.
    const totals = {};
    for (const st of stateSet) totals[st] = 0;
    const secsFor = (s, st) => {
      if (st === GAP_ROW)
        return this._isBreakdown(s) ? Number((s.attrs || {}).unaccounted_seconds) || 0 : 0;
      if (this._isBreakdown(s)) return ((s.attrs || {}).breakdown_seconds || {})[st] || 0;
      const tracked = ((s.attrs || {}).tracked_states || []).join(", ") || "tracked";
      // Prefer raw seconds attr; s.state is HA-unit-converted (hours) and thus
      // ambiguous. Fall back for older backends.
      const secs =
        (s.attrs || {}).duration_seconds != null
          ? Number((s.attrs || {}).duration_seconds)
          : Number(s.state);
      return st === tracked ? secs || 0 : 0;
    };
    const pctFor = (s, st) => {
      if (st === GAP_ROW) {
        if (!this._isBreakdown(s)) return null;
        const ws = Number((s.attrs || {}).window_seconds) || 0;
        const gap = Number((s.attrs || {}).unaccounted_seconds) || 0;
        return ws > 0 ? (gap / ws) * 100 : null;
      }
      if (this._isBreakdown(s)) return ((s.attrs || {}).breakdown_pct || {})[st];
      const tracked = ((s.attrs || {}).tracked_states || []).join(", ") || "tracked";
      return st === tracked ? (s.attrs || {}).percent : null;
    };
    for (const s of sensors)
      for (const st of stateSet) totals[st] += secsFor(s, st);
    const rows = [...stateSet].sort((x, y) => totals[y] - totals[x]);
    // Gap row always last, regardless of magnitude.
    if (anyGap) rows.push(GAP_ROW);
    const rowLabel = (st) => (st === GAP_ROW ? "No data" : st);
    const rowColor = (st) =>
      st === GAP_ROW ? "var(--est-bar-bg)" : stateColor(st);

    return html`<table>
      <thead>
        <tr>
          <th>State</th>
          ${sensors.map(
            (s) =>
              html`<th>
                ${FRAME_LABELS[s.frame] || s.frame}${this._incomplete(s.attrs)
                  ? html`<span class="since">*</span>`
                  : nothing}
              </th>`
          )}
        </tr>
      </thead>
      <tbody>
        ${rows.map(
          (st) => html`<tr>
            <td>
              <span class="state-cell"
                ><span
                  class="legend-swatch"
                  style="background:${rowColor(st)}"
                ></span
                >${rowLabel(st)}</span
              >
            </td>
            ${sensors.map((s) => {
              const secs = secsFor(s, st);
              const pct = pctFor(s, st);
              return html`<td>
                ${fmtDuration(secs)}<br /><span class="cell-secondary"
                  >${fmtPct(pct)}</span
                >
              </td>`;
            })}
          </tr>`
        )}
      </tbody>
    </table>`;
  }
}

customElements.define("entity-state-tracker-card", EntityStateTrackerCard);

// --- Card Editor ---
//
// Mirrors the Entity Availability card editor (its proven, browser-tested
// pattern): a vanilla LitElement with hand-rolled inputs + a `_updateConfig`
// that fires `config-changed`, NOT `ha-form`. Same properties/style bootstrap,
// same `.value`/`@change` bindings, same undefined-key stripping. Adapted to
// EST's config keys: entity (tracker), chart, frame (pie/table only), title.

class EntityStateTrackerCardEditor extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
    };
  }

  static get styles() {
    return css`
      .editor-row {
        margin-bottom: 12px;
      }
      .editor-row label {
        display: block;
        font-weight: 500;
        margin-bottom: 4px;
      }
      .editor-row input[type="text"],
      .editor-row select {
        width: 100%;
        padding: 8px;
        border: 1px solid var(--divider-color, #ccc);
        border-radius: 4px;
        box-sizing: border-box;
        background: var(--card-background-color, var(--primary-background-color, #fafafa));
        color: var(--primary-text-color, #212121);
      }
      .editor-hint {
        font-size: 12px;
        color: var(--secondary-text-color, #727272);
        margin-top: 4px;
      }
    `;
  }

  setConfig(config) {
    this._config = config;
  }

  // Frame options for the pie/table frame picker — only frames the chosen
  // tracker actually publishes, so we never offer a window the coordinator
  // isn't computing. Falls back to the full canonical set when no tracker is
  // resolved yet.
  _frameOptions() {
    const frames = new Set(
      trackerFrameSensors(this.hass, this._config?.tracker_id).map(
        (s) => s.frame
      )
    );
    if (frames.size > 0) return FRAME_ORDER.filter((f) => frames.has(f));
    return FRAME_ORDER;
  }

  // True when the selected tracker has a compliance target — the gauge is
  // meaningless without one, so the checkbox only appears then. Read off any
  // frame sensor's live attrs (target_threshold rides on the duration sensor).
  _hasTarget() {
    return trackerFrameSensors(this.hass, this._config?.tracker_id).some(
      (s) => this.hass?.states?.[s.entity_id]?.attributes?.target_threshold != null
    );
  }

  render() {
    if (!this._config) return html``;

    const chart = this._config.chart || "bars";
    // The single `frame` picker only affects pie (which charts ONE frame).
    // Bars and table render every frame (rows / columns), so the picker is
    // meaningless there — hide it. Multi-frame selection for bars/table is the
    // separate `frames` filter (see _framesToShow), not this picker.
    const showFrame = chart === "pie";
    const options = trackerOptions(this.hass);

    return html`
      <div style="padding: 16px;">
        <div class="editor-row">
          <label>Tracker</label>
          ${options.length > 0
            ? html`<select
                .value=${this._config.tracker_id || ""}
                @change=${(e) => this._updateConfig("tracker_id", e.target.value)}
              >
                ${this._config.tracker_id &&
                !options.some((o) => o.trackerId === this._config.tracker_id)
                  ? html`<option value=${this._config.tracker_id} selected>
                      ${this._config.tracker_id} (not found)
                    </option>`
                  : nothing}
                ${options.map(
                  (o) => html`<option
                    value=${o.trackerId}
                    ?selected=${this._config.tracker_id === o.trackerId}
                  >
                    ${o.label}
                  </option>`
                )}
              </select>`
            : html`<div class="editor-hint">
                No Entity State Tracker trackers found. Add one via
                Settings → Devices & Services first.
              </div>`}
          <div class="editor-hint">
            Pick the tracker to show — the card finds all its frames
            automatically.
          </div>
        </div>
        <div class="editor-row">
          <label>Chart</label>
          <select
            .value=${chart}
            @change=${(e) => this._updateConfig("chart", e.target.value)}
          >
            <option value="bars" ?selected=${chart === "bars"}>Bars (one row per frame)</option>
            <option value="pie" ?selected=${chart === "pie"}>Pie (one frame's breakdown)</option>
            <option value="table" ?selected=${chart === "table"}>Table (states × frames)</option>
          </select>
        </div>
        ${showFrame
          ? html`<div class="editor-row">
              <label>Frame${chart === "pie" ? "" : " (emphasis)"}</label>
              <select
                .value=${this._config.frame || ""}
                @change=${(e) =>
                  this._updateConfig("frame", e.target.value || undefined)}
              >
                <option value="" ?selected=${!this._config.frame}>
                  ${chart === "pie" ? "First available" : "All frames"}
                </option>
                ${this._frameOptions().map(
                  (f) => html`<option
                    value=${f}
                    ?selected=${this._config.frame === f}
                  >
                    ${FRAME_LABELS[f] || f}
                  </option>`
                )}
              </select>
              <div class="editor-hint">
                ${chart === "pie"
                  ? "Which frame the pie breaks down."
                  : "Optional — the table shows every enabled frame as a column."}
              </div>
            </div>`
          : nothing}
        ${chart === "pie" && this._hasTarget()
          ? html`<div class="editor-row">
              <label>
                <input
                  type="checkbox"
                  ?checked=${!!this._config.compliance_pie}
                  @change=${(e) =>
                    this._updateConfig(
                      "compliance_pie",
                      e.target.checked || undefined
                    )}
                />
                Show compliance gauge
              </label>
              <div class="editor-hint">
                Adds a met/unmet donut below the state pie.
              </div>
            </div>`
          : nothing}
        <div class="editor-row">
          <label>Title (optional)</label>
          <input
            type="text"
            .value=${this._config.title || ""}
            @input=${(e) =>
              this._updateConfig("title", e.target.value || undefined)}
            placeholder="Defaults to the tracker's name"
          />
        </div>
      </div>
    `;
  }

  _updateConfig(key, value) {
    if (!this._config) return;
    const newConfig = { ...this._config, [key]: value };
    Object.keys(newConfig).forEach((k) => {
      if (newConfig[k] === undefined) delete newConfig[k];
    });
    this._config = newConfig;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this._config },
        bubbles: true,
        composed: true,
      })
    );
  }
}

customElements.define(
  "entity-state-tracker-card-editor",
  EntityStateTrackerCardEditor
);
