/**
 * Entity State Tracker Card
 * Custom Lovelace card for the Home Assistant Entity State Tracker integration.
 *
 * Renders the per-frame duration / breakdown sensors this integration emits as
 * one of three charts (bars | pie | table). English-only (house rule). One
 * self-contained file, vanilla LitElement via the home-assistant-main prototype.
 */

const CARD_VERSION = "0.1.3";

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
const FRAME_ORDER = ["today", "yesterday", "24h", "week", "last_week", "7d", "30d", "month", "last_month", "year"];
const FRAME_LABELS = {
  today: "Today",
  yesterday: "Yesterday",
  "24h": "Last 24 hours",
  week: "This week",
  last_week: "Last week",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  month: "This month",
  last_month: "Last month",
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

// Effective frame selection for a card config: the `frames` array, or the legacy
// single `frame` string, normalized to FRAME_ORDER. Empty = all frames. Shared by
// the card renderer and the editor so the two never drift.
function selectedFrames(config) {
  const raw = Array.isArray(config.frames)
    ? config.frames
    : config.frame
      ? [config.frame]
      : [];
  return FRAME_ORDER.filter((f) => raw.includes(f));
}

// -----------------------------------------------------------------------------
// Deterministic per-state color.
//
// A state ALWAYS maps to the same color and a new slice never recolors existing
// ones. Common states get a SEMANTIC color (on=green, off=slate, home=green,
// away/off-ish=slate, unavailable/unknown=muted grey) so binary-sensor charts
// read intuitively; every other state hash-indexes into a fixed high-contrast
// categorical PALETTE (colorblind-friendly, no muddy red/blue pairs). Shared by
// pie, table and stacked bars, so all three agree.
// -----------------------------------------------------------------------------
const STATE_PALETTE = [
  "#4c8bf5", // blue
  "#f5a623", // amber
  "#7b61ff", // violet
  "#2ecc71", // green
  "#ff6b6b", // coral
  "#00b8a9", // teal
  "#e056fd", // magenta
  "#f9c80e", // yellow
  "#5f6caf", // indigo
  "#e17055", // burnt orange
];
const SEMANTIC_COLORS = {
  on: "#2ecc71",
  off: "#5c6b7a",
  home: "#2ecc71",
  away: "#5c6b7a",
  open: "#2ecc71",
  closed: "#5c6b7a",
  unavailable: "#9aa4ad",
  unknown: "#c2c8cf",
};

function stateHashIndex(state, mod) {
  let h = 0x811c9dc5;
  const str = String(state);
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) % mod;
}

function stateColor(state) {
  const key = String(state).toLowerCase();
  if (key in SEMANTIC_COLORS) return SEMANTIC_COLORS[key];
  return STATE_PALETTE[stateHashIndex(key, STATE_PALETTE.length)];
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

  /* Stacked track: segments laid side by side — one per state (+ other/no-data),
     for both all-states and specific mode, instead of one absolute fill. */
  .bar-track-stacked {
    display: flex;
  }

  .bar-seg {
    height: 100%;
  }

  /* Positioning context for the hover/tap tooltip (absolute within the card). */
  .card-body-wrap {
    position: relative;
  }

  /* Styled hover/tap tooltip for pie slices + bar segments — themed, instant,
     touch-friendly (matches the HA / apexcharts-card norm, unlike OS title). */
  .est-tooltip {
    position: absolute;
    z-index: 5;
    pointer-events: none;
    min-width: 96px;
    max-width: 220px;
    padding: 6px 9px;
    border-radius: 6px;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.28);
    border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
    font-size: 12px;
    line-height: 1.35;
  }

  .est-tooltip-head {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
  }

  .est-tooltip-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: 0 0 auto;
  }

  .est-tooltip-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .est-tooltip-metric {
    margin-top: 2px;
  }

  .est-tooltip-sub {
    margin-top: 1px;
    color: var(--secondary-text-color);
    font-size: 11px;
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
  .pie-charts {
    display: flex;
    align-items: flex-start;
    gap: 24px;
    flex-wrap: wrap;
    padding-top: 8px;
  }

  /* Multi-frame: frames stack vertically (one pie below the next). Each frame is
     a .pie-frame row holding its donut + optional gauge side by side. */
  .pie-charts:has(.pie-frame) {
    flex-direction: column;
    flex-wrap: nowrap;
  }
  .pie-frame {
    display: flex;
    align-items: flex-start;
    gap: 24px;
    flex-wrap: wrap;
  }
  .pie-frame + .pie-frame {
    border-top: 1px solid var(--divider-color, #e0e0e0);
    padding-top: 16px;
  }

  /* One chart column: caption, then the donut+legend group. */
  .pie-chart {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
  }

  /* A solo (beside) chart left-aligns its column: when the row-legend wraps
     under the donut on a narrow viewport it hugs the left edge instead of
     centering awkwardly beneath. Stacked (two-chart) columns stay centered. */
  .pie-chart:has(.pie-body.beside) {
    align-items: flex-start;
  }

  /* Donut + legend. Default stacks (legend below); .beside is a solo chart
     with the legend to the right of the donut. */
  .pie-body {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
  }

  .pie-body.beside {
    flex-direction: row;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    justify-content: flex-start;
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
    padding: 7px 10px;
    /* tabular-nums keeps digits the same width so columns line up. */
    font-variant-numeric: tabular-nums;
  }

  th:first-child,
  td:first-child {
    text-align: left;
  }

  /* State column takes the slack; duration/% stay tight and right-aligned. */
  .state-col {
    width: 100%;
    text-align: left;
  }

  thead th {
    color: var(--est-text-primary);
    font-weight: 600;
    border-bottom: 2px solid var(--est-divider);
    white-space: nowrap;
  }

  tbody td {
    border-bottom: 1px solid var(--est-divider);
  }

  /* Zebra + hover so the rows stay scannable. */
  tbody tr:nth-child(even) {
    background: color-mix(in srgb, var(--est-text-primary) 4%, transparent);
  }

  tbody tr:hover {
    background: color-mix(in srgb, var(--est-accent) 10%, transparent);
  }

  /* Class was on the span, not the td — the old td.state-cell selector never
     matched. Now it styles the swatch+label group directly. */
  .state-cell {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 500;
  }

  /* Value cell: a per-cell mini-bar (state color, width = share of frame) sits
     behind the number as a low-alpha background, giving the grid a scan anchor
     without a separate column. Theme-safe via color-mix on the state color. */
  td.value-cell {
    background-image: var(--cell-bar, none);
    background-repeat: no-repeat;
    background-position: right center;
  }

  .cell-primary {
    color: var(--est-text-primary);
  }

  .cell-secondary {
    color: var(--est-text-secondary);
    font-size: 11px;
  }

  .compliance-mark {
    font-weight: 700;
    margin-right: 3px;
  }

  /* Frame rows carry the window totals; their state sub-rows sit beneath as a
     drill-down. Weight the frame label and indent + lighten the sub-rows so the
     nesting reads without a separate table. */
  .frame-row td {
    font-weight: 600;
  }

  .state-indent {
    padding-left: 18px;
    font-weight: 400;
  }
`;

class EntityStateTrackerCard extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
      _tip: { state: true },
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
    // Dismiss a pinned (tapped) tooltip when the user taps anywhere else.
    // Bound once so add/removeEventListener pair up.
    this._onDocPointer = (e) => {
      if (!this._tip || !this._tip.pinned) return;
      if (!e.composedPath().includes(this)) this._hideTip();
    };
  }

  connectedCallback() {
    super.connectedCallback();
    document.addEventListener("click", this._onDocPointer);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener("click", this._onDocPointer);
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

  // Frames the card is configured to show (see module-level selectedFrames).
  _selectedFrames() {
    return selectedFrames(this._config);
  }

  _framesToShow(sensors) {
    const filter = this._selectedFrames();
    if (filter.length === 0) return sensors;
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
      <div class="card-body-wrap">
        <div class="body">${body}</div>
        ${this._renderTip()}
      </div>
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

  // ---------------------------------------------------------------------------
  // Shared hover/tap tooltip for pie slices + bar segments (matches the HA /
  // apexcharts-card norm — a styled, instant, themed tooltip, not the OS `title`
  // box which never appears on touch). One reactive `_tip` slot; the tooltip div
  // is rendered once at the card root and positioned to the pointer.
  //
  // Mobile: pointer events fire for mouse AND pen; touch has no hover, so a
  // segment TAP pins the tooltip and stops the row's more-info click from also
  // firing. A document click outside dismisses it. Desktop hover just tracks the
  // pointer and clears on leave.
  // ---------------------------------------------------------------------------
  _showTip(e, info) {
    // Position relative to the card so the tooltip stays put on scroll and is
    // clamped inside the card box (no viewport spill on a narrow phone).
    const card = this.renderRoot?.querySelector(".card-body-wrap");
    const rect = card
      ? card.getBoundingClientRect()
      : { left: 0, top: 0, width: 9999, height: 9999 };
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    this._tip = { ...info, x, y, w: rect.width, h: rect.height };
  }

  _hideTip() {
    if (this._tip) this._tip = null;
  }

  // Tap pins on touch: show + swallow the click so the row more-info doesn't also
  // open. On a subsequent tap of the SAME segment, toggle off.
  _tapTip(e, info) {
    e.stopPropagation();
    if (this._tip && this._tip.label === info.label && this._tip.pinned) {
      this._hideTip();
      return;
    }
    this._showTip(e, info);
    this._tip = { ...this._tip, pinned: true };
  }

  _renderTip() {
    const t = this._tip;
    if (!t) return nothing;
    // Anchor to the pointer and flip on both axes near the card edges so the
    // tooltip is never clipped: past the vertical midline it grows LEFT
    // (translateX -100%), in the bottom band it grows UP (translateY -100%).
    // CSS transforms do the sizing, so no width/height measurement is needed.
    const w = t.w || 9999;
    const flipX = t.x > w / 2;
    const flipY = t.h != null && t.y + 96 > t.h;
    const left = flipX ? t.x - 12 : t.x + 12;
    const top = flipY ? t.y - 12 : t.y + 12;
    const tx = flipX ? "-100%" : "0";
    const ty = flipY ? "-100%" : "0";
    return html`<div
      class="est-tooltip"
      style="left:${left}px;top:${top}px;transform:translate(${tx}, ${ty});"
      role="tooltip"
    >
      <div class="est-tooltip-head">
        <span class="est-tooltip-dot" style="background:${t.color}"></span>
        <span class="est-tooltip-label">${t.label}</span>
      </div>
      <div class="est-tooltip-metric">
        ${fmtDuration(t.secs)} · ${fmtPct(t.pct)}
      </div>
      ${t.count != null
        ? html`<div class="est-tooltip-sub">
            ${t.count} ${t.count === 1 ? "visit" : "visits"}${t.avg != null
              ? html` · avg ${fmtDuration(t.avg)}`
              : nothing}
          </div>`
        : nothing}
    </div>`;
  }
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

  // A sensor is a breakdown (all-states) sensor when it has NO tracked_states
  // set. Specific-mode duration sensors ALWAYS carry tracked_states (and now a
  // tracked-only breakdown_seconds too), so we cannot key off breakdown_seconds
  // presence — that would misroute specific sensors into the all-states branch.
  _isBreakdown(s) {
    return s.attrs && s.attrs.tracked_states == null;
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
        <div class="bar-track bar-track-stacked">
          ${(this._isBreakdown(s) ? this._breakdownSlices(a) : this._specificSlices(a, s))
            .filter((seg) => seg.secs >= 1 || !seg.derived)
            .map((seg) => {
              // Stacked segment per state (+ other / no-data), width = its share
              // of the window, same colors as pie/table. Real states carry
              // count/avg; derived fillers don't.
              const w =
                seg.pct == null ? 0 : Math.max(0, Math.min(100, Number(seg.pct)));
              const info = {
                label: seg.state,
                secs: seg.secs,
                pct: seg.pct,
                color: seg.color,
                count: seg.count != null ? seg.count : null,
                avg: seg.avg != null ? seg.avg : null,
              };
              return html`<div
                class="bar-seg ${incomplete && !seg.derived ? "incomplete" : ""}"
                style="width:${w}%;background:${seg.color};"
                @pointerenter=${(e) => this._showTip(e, info)}
                @pointermove=${(e) => this._showTip(e, info)}
                @pointerleave=${() => this._hideTip()}
                @click=${(e) => this._tapTip(e, info)}
              ></div>`;
            })}
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

  // One donut column: caption on top, then the donut+legend group. With
  // beside=true (solo chart) the legend sits to the RIGHT of the donut; else
  // (two charts side-by-side) it stacks BELOW. legendItems: [{color,label,value}].
  _pieColumn(caption, paths, legendItems, beside = false) {
    return html`
      <div class="pie-chart">
        ${caption}
        <div class="pie-body${beside ? " beside" : ""}">
          ${this._pieSvg(paths)}
          <div class="legend">
            ${legendItems.map(
              (i) => html`<div class="legend-item">
                <span class="legend-swatch" style="background:${i.color}"></span>
                <span>${i.label}</span>
                ${i.value != null
                  ? html`<span class="legend-value">${i.value}</span>`
                  : nothing}
              </div>`
            )}
          </div>
        </div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Compliance gauge: a single-value donut reading the aggregate
  // compliance_percent against the target threshold. Arc length = the score;
  // arc color = green when met, red when under. The REMAINDER is the same hue
  // faded (~0.18 opacity), NOT grey — grey would collide with the state pie's
  // no-data grey, and the remainder is "not-yet-target time", not an alarm.
  // Center prints the % (arc-colored) + a pass/fail glyph; no legend (the
  // number is the legend). Opt-in (config.compliance_pie); shown only with a
  // numeric threshold (met/unmet is undefined without one). Never blank:
  // full solid ring at 100%, full faint ring at 0%.
  // ---------------------------------------------------------------------------
  _renderComplianceGauge(a) {
    const pct = a.compliance_percent;
    const threshold = a.target_threshold;
    // No score, or no threshold to score against → nothing meaningful to gauge.
    // (target_states can be set without a threshold; met/unmet needs the number.)
    if (pct == null || threshold == null) return nothing;
    const met = Number(pct) >= Number(threshold);
    const p = Math.max(0, Math.min(100, Number(pct)));
    const compColor = met
      ? "var(--success-color, #4caf50)"
      : "var(--error-color, #f44336)";
    // Remainder = same hue, faded. color-mix keeps it theme-safe and always the
    // arc's own hue, so a passing gauge never shows a second alarm color.
    const faint = `color-mix(in srgb, ${compColor} 18%, transparent)`;
    const cx = 50, cy = 50, r = 40, inner = 24;
    // A ~0-area slice can't paint an arc, so at the extremes draw ONE full ring:
    // solid arc-color at 100%, faint at 0%. Mid-range draws the two arcs.
    let paths;
    if (p >= 99.95) {
      paths = [{ d: this._ring(cx, cy, r, inner), color: compColor, evenodd: true }];
    } else if (p <= 0.05) {
      paths = [{ d: this._ring(cx, cy, r, inner), color: faint, evenodd: true }];
    } else {
      const a0 = -Math.PI / 2;
      const a1 = a0 + (p / 100) * 2 * Math.PI;
      paths = [
        { d: this._arc(cx, cy, r, inner, a0, a1), color: compColor, evenodd: false },
        { d: this._arc(cx, cy, r, inner, a1, a0 + 2 * Math.PI), color: faint, evenodd: false },
      ];
    }
    // No legend, no target line here — _metaHeader already states the target
    // once for the whole card, and the center number + glyph carry meaning.
    return html`
      <div class="pie-chart">
        <div class="frame-picker">Compliance</div>
        <div class="pie-body">
          ${this._pieSvg(paths, {
            value: `${Math.round(p)}%`,
            glyph: met ? "✓" : "✗",
            color: compColor,
          })}
        </div>
      </div>
    `;
  }

  // Specific mode: one slice per TRACKED STATE from the sensor's tracked-only
  // breakdown_seconds (each its own deterministic color, same as all-states),
  // plus derived filler slices so the set sums to 100:
  //   other   = window_seconds - in-state total - no-data
  //   no-data = unaccounted_seconds  ("No data" past window / "In progress")
  // Never blank at 0% in-state, never mislabels absence of data as a non-tracked
  // state. Falls back to a single summed "in-state" slice when breakdown_seconds
  // is absent (older backend / staged deploy). Shared by the pie and the stacked
  // bar. Returns [{state, secs, pct, color, derived?}]; NOT dust-filtered (each
  // caller filters as it needs).
  // All-states stacked-bar slices: one segment per observed state (each enriched
  // with visit count + avg-duration), plus a derived "No data"/"In progress" tail
  // for uncomputed time. On long frames (last_7_days, last_30_days) that tail is
  // the window portion predating our recorded history ("No data"); on the current
  // frame it's a transient open-state lag ("In progress"). Every state is tracked
  // here, so there is no non-tracked "other" — only states + the gap. Percentages
  // are of window_seconds so the bar sums to 100. Returns [{state, secs, pct,
  // color, count?, avg?, derived?}]; NOT dust-filtered (caller filters).
  _breakdownSlices(a) {
    const bs = a.breakdown_seconds || {};
    const bp = a.breakdown_pct || {};
    const counts = a.counts || {};
    const avgs = a.avg_duration_seconds || {};
    const ws = Number(a.window_seconds) || 0;
    const gap = Math.max(0, Number(a.unaccounted_seconds) || 0);
    const inSecs = Object.values(bs).reduce((n, v) => n + (Number(v) || 0), 0);
    const denom = ws > 0 ? ws : inSecs + gap;
    const pctOf = (secs) => (denom > 0 ? (secs / denom) * 100 : null);
    const slices = Object.keys(bs)
      .map((state) => ({
        state,
        secs: Number(bs[state]) || 0,
        pct: bp[state] != null ? bp[state] : pctOf(Number(bs[state]) || 0),
        color: stateColor(state),
        count: counts[state] != null ? counts[state] : null,
        avg: avgs[state] != null ? avgs[state] : null,
      }))
      // Biggest first, so the table/pie/bars lead with the dominant state and
      // the top-5 cap keeps the states that actually matter.
      .sort((x, y) => y.secs - x.secs);
    if (gap > 0) {
      slices.push({
        state: a.has_gap ? "No data" : "In progress",
        secs: gap,
        pct: pctOf(gap),
        color: "var(--est-bar-bg)",
        derived: true,
      });
    }
    return slices;
  }

  _specificSlices(a, pick) {
    const ws = Number(a.window_seconds) || 0;
    const gap = Math.max(0, Number(a.unaccounted_seconds) || 0);
    const bs = a.breakdown_seconds;
    const tracked = a.tracked_states || [];
    let inSlices;
    let inSecs;
    if (bs != null) {
      inSlices = tracked
        .map((state) => ({
          state,
          secs: Number(bs[state]) || 0,
          color: stateColor(state),
        }))
        // Biggest first (see _breakdownSlices) — % descending in the table.
        .sort((x, y) => y.secs - x.secs);
      inSecs = inSlices.reduce((n, s) => n + s.secs, 0);
    } else {
      // pick.state is unit-converted to hours by HA (unit-ambiguous); prefer the
      // raw seconds attr.
      inSecs =
        (a.duration_seconds != null ? Number(a.duration_seconds) : Number(pick.state)) || 0;
      const label = tracked.join(", ") || "tracked";
      inSlices = [{ state: label, secs: inSecs, color: stateColor(label) }];
    }
    const other = ws > 0 ? Math.max(0, ws - inSecs - gap) : 0;
    const denom = ws > 0 ? ws : inSecs + other + gap;
    const pctOf = (secs) => (denom > 0 ? (secs / denom) * 100 : null);
    const slices = inSlices.map((s) => ({ ...s, pct: pctOf(s.secs) }));
    slices.push({
      state: "other",
      secs: other,
      pct: pctOf(other),
      color: "var(--est-bar-bg-alt)",
      derived: true,
    });
    if (gap > 0) {
      slices.push({
        state: a.has_gap ? "No data" : "In progress",
        secs: gap,
        pct: pctOf(gap),
        color: "var(--est-bar-bg)",
        derived: true, // computed filler, not real occupancy → dust-filterable
      });
    }
    return slices;
  }

  // ---------------------------------------------------------------------------
  // Pie/donut: one donut per selected frame. all-states → every state; specific
  // → in-state vs rest. Deterministic per-state color.
  // ---------------------------------------------------------------------------
  _renderPie(sensors) {
    // One donut per frame. Solo → single donut (legacy legend-beside look);
    // multi → frames stack vertically (one pie below the next), each frame's
    // donut + optional gauge side by side in a .pie-frame row.
    // solo is on the POST-filter count: one frame checked out of many → solo
    // (legend beside, flat), same as a tracker that only has one frame enabled.
    const solo = sensors.length === 1;
    return html`
      ${this._metaHeader(sensors[0].attrs || {})}
      <div class="pie-charts">
        ${sensors.map((s) => this._pieColumnFor(s, solo))}
      </div>
    `;
  }

  // Build one frame's donut column + optional compliance gauge. `solo` puts the
  // legend beside the donut (single-frame look) and returns a flat column;
  // multi-frame stacks the legend below and wraps in a .pie-frame row.
  _pieColumnFor(pick, solo) {
    const a = pick.attrs || {};
    // all-states reuses _breakdownSlices (sorted, gap-tailed); specific its own.
    let slices = this._isBreakdown(pick)
      ? this._breakdownSlices(a)
      : this._specificSlices(a, pick);
    // Drop sub-second DERIVED slices (other / no-data): the engine counts the
    // current open state up to `now`, so a fully-covered window leaves only
    // floating-point residue there (e.g. 0.4s) — a dust slice that rounds to
    // "0 s" and clutters the legend. Real state slices are never dropped.
    slices = slices.filter((s) => s.secs >= 1 || !s.derived);
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
      // Per-slice tooltip meta (real states enriched with count/avg; derived
      // other/no-data slices carry only duration/pct).
      const count = s.derived ? null : (a.counts || {})[s.state];
      const avg = s.derived ? null : (a.avg_duration_seconds || {})[s.state];
      const tip = {
        label: s.state,
        secs: s.secs,
        pct: s.pct,
        color: s.color,
        count: count != null ? count : null,
        avg: avg != null ? avg : null,
      };
      // A full-circle slice (only slice, or frac≈1) has coincident arc
      // endpoints → a zero-length, INVISIBLE path. Draw a donut RING (outer
      // circle + inner hole punched via even-odd fill) instead.
      if (single || frac >= 0.9999) {
        return { d: this._ring(cx, cy, r, inner), color: s.color, evenodd: true, tip };
      }
      const a0 = angle;
      const a1 = angle + frac * 2 * Math.PI;
      angle = a1;
      return {
        d: this._arc(cx, cy, r, inner, a0, a1),
        color: s.color,
        evenodd: false,
        tip,
      };
    });

    const incomplete = this._incomplete(a);
    // Opt-in compliance gauge beside the state donut — only when the card asks
    // for it AND the frame has a compliance figure to show.
    const gauge =
      this._config.compliance_pie && a.compliance_percent != null
        ? this._renderComplianceGauge(a)
        : nothing;
    const stateCaption = html`<div class="frame-picker">
      ${FRAME_LABELS[pick.frame] || pick.frame}${incomplete && a.data_start
        ? html`<span class="since">since ${fmtDate(a.data_start)}</span>`
        : nothing}
    </div>`;
    // Legend beside only for a solo frame with no gauge (preserve single look);
    // otherwise stack it below so multiple donuts stay narrow and aligned.
    const column = html`
      ${this._pieColumn(
        stateCaption,
        paths,
        slices.map((s) => ({
          color: s.color,
          label: s.state,
          value: html`${fmtDuration(s.secs)} · ${fmtPct(s.pct)}`,
        })),
        solo && gauge === nothing
      )}
      ${gauge}
    `;
    // Multi-frame: wrap each frame's donut+gauge in a row so the frames stack
    // vertically (one pie below the next) while a frame's own gauge stays beside
    // its donut. Solo keeps the flat .pie-charts row (unchanged look).
    return solo ? column : html`<div class="pie-frame">${column}</div>`;
  }

  // Build the donut <svg> as a real SVG-namespaced DOM node. Every element is
  // created with createElementNS(NS, …) so <path> lands in
  // http://www.w3.org/2000/svg and the browser renders it as geometry. Lit
  // renders a Node child value directly (no re-parsing, no namespace loss),
  // which sidesteps the missing `svg` template tag entirely. Rebuilt each
  // render — cheap (a handful of nodes) and always fresh.
  _pieSvg(paths, center = null) {
    const NS = "http://www.w3.org/2000/svg";
    const el = document.createElementNS(NS, "svg");
    el.setAttribute("class", "pie-svg");
    el.setAttribute("width", "100");
    el.setAttribute("height", "100");
    el.setAttribute("viewBox", "0 0 100 100");
    let hasTip = false;
    for (const p of paths) {
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", p.d);
      path.setAttribute("fill", p.color);
      if (p.evenodd) path.setAttribute("fill-rule", "evenodd");
      // Hover/tap tooltip: same info + behaviour as the bar segments. Guarded on
      // `p.tip` so the compliance gauge (no tip) stays inert. pointerleave is on
      // the <svg> (below), not per-path — a per-path leave misses when the pointer
      // exits through a transparent gap between slices, leaving the tip stuck.
      if (p.tip) {
        hasTip = true;
        path.style.cursor = "pointer";
        path.addEventListener("pointerenter", (e) => this._showTip(e, p.tip));
        path.addEventListener("pointermove", (e) => this._showTip(e, p.tip));
        path.addEventListener("click", (e) => this._tapTip(e, p.tip));
      }
      el.appendChild(path);
    }
    if (hasTip) el.addEventListener("pointerleave", () => this._hideTip());
    // Optional center label (gauge): a big value + small glyph in the hole.
    if (center) {
      const val = document.createElementNS(NS, "text");
      val.setAttribute("x", "50");
      val.setAttribute("y", center.glyph ? "48" : "50");
      val.setAttribute("text-anchor", "middle");
      val.setAttribute("dominant-baseline", "central");
      val.setAttribute("fill", center.color);
      val.setAttribute("style", "font-size:14px;font-weight:700");
      // Only cap width when the text is long enough to risk spilling past the
      // inner ring ("100%"). textLength on a short string ("0%") stretches it
      // to the full width and wrecks the glyph spacing, so leave those natural.
      if (center.value.length >= 4) {
        val.setAttribute("textLength", "40");
        val.setAttribute("lengthAdjust", "spacingAndGlyphs");
      }
      val.textContent = center.value;
      el.appendChild(val);
      if (center.glyph) {
        const g = document.createElementNS(NS, "text");
        g.setAttribute("x", "50");
        g.setAttribute("y", "64");
        g.setAttribute("text-anchor", "middle");
        g.setAttribute("dominant-baseline", "central");
        g.setAttribute("fill", center.color);
        g.setAttribute("style", "font-size:12px");
        g.textContent = center.glyph;
        el.appendChild(g);
      }
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
  // Table — one narrow table, one row per enabled frame (Frame | Duration | %
  // [| Compliance when a specific target is set]) — the window-level story, same
  // axis as the bars. Duration/% is the tracked total (specific) or the
  // observed-state total = window − gap (all-states).
  // With `show_states` on (OFF by default), each frame row is immediately
  // followed by indented per-state sub-rows in the SAME table — that frame's
  // states drilled down beneath it. States come from the mode's slice builder,
  // capped at 5 per frame (`limit_states`, on unless false) with the rest folded
  // into a "… N more" row.
  // ---------------------------------------------------------------------------
  _renderTable(sensors) {
    const isBreakdown = sensors.some((s) => this._isBreakdown(s));
    const hasCompliance = sensors.some(
      (s) => (s.attrs || {}).compliance_percent != null
    );
    const showStates = !!this._config.show_states;
    return html`
      ${this._metaHeader(sensors[0]?.attrs || {})}
      <table>
        <thead>
          <tr>
            <th class="state-col">Frame</th>
            <th>Duration</th>
            <th>%</th>
            ${hasCompliance ? html`<th>Compliance</th>` : nothing}
          </tr>
        </thead>
        <tbody>
          ${sensors.map((s) => {
            const stateRows = showStates
              ? this._capSlices(
                  (isBreakdown
                    ? this._breakdownSlices(s.attrs || {})
                    : this._specificSlices(s.attrs || {}, s)
                  ).filter((x) => x.secs >= 1 || !x.derived)
                )
              : [];
            return html`${this._frameRow(s, hasCompliance)}${this._stateSubRows(
              stateRows,
              hasCompliance
            )}`;
          })}
        </tbody>
      </table>
    `;
  }

  // One frame's total row. Total duration/% is the tracked total (specific:
  // duration_seconds/percent) or the observed-state total for all-states
  // (window − unaccounted, so "no data" time is excluded).
  _frameRow(s, hasCompliance) {
    const a = s.attrs || {};
    let secs;
    let pct;
    if (this._isBreakdown(s)) {
      const ws = Number(a.window_seconds) || 0;
      const gap = Math.max(0, Number(a.unaccounted_seconds) || 0);
      secs = Math.max(0, ws - gap);
      pct = ws > 0 ? (secs / ws) * 100 : null;
    } else {
      secs =
        (a.duration_seconds != null ? Number(a.duration_seconds) : Number(s.state)) || 0;
      pct = a.percent;
    }
    const compliance = a.compliance_percent;
    const threshold = a.target_threshold;
    const w = pct == null ? 0 : Math.max(0, Math.min(100, Number(pct)));
    const tint = `color-mix(in srgb, var(--est-accent) 22%, transparent)`;
    const bar =
      w > 0
        ? `linear-gradient(90deg, ${tint} 0 ${w}%, transparent ${w}% 100%)`
        : "none";
    return html`<tr class="frame-row">
      <td class="state-col">
        ${FRAME_LABELS[s.frame] || s.frame}${this._incomplete(a) && a.data_start
          ? html`<span class="since">since ${fmtDate(a.data_start)}</span>`
          : nothing}
      </td>
      <td class="cell-primary">${fmtDuration(secs)}</td>
      <td class="value-cell cell-secondary" style="--cell-bar:${bar}">
        ${fmtPct(pct)}
      </td>
      ${hasCompliance
        ? html`<td class="cell-secondary">
            ${compliance != null && threshold != null
              ? html`<span
                    class="compliance-mark"
                    style="color:${Number(compliance) >= Number(threshold)
                      ? "var(--success-color, #4caf50)"
                      : "var(--error-color, #f44336)"}"
                    >${Number(compliance) >= Number(threshold) ? "✓" : "✗"}</span
                  >${fmtPct(compliance)}`
              : fmtPct(compliance)}
          </td>`
        : nothing}
    </tr>`;
  }

  // Cap a frame's slices at 5 (unless limit_states is explicitly false). The
  // derived tail ("other"/"No data"/"In progress") always stays visible — only
  // real states are capped — so the overflow row folds surplus *states* into one
  // "… (N more)" entry carrying their summed secs/pct, inserted before the tail.
  _capSlices(slices) {
    if (this._config.limit_states === false) return slices;
    const CAP = 5;
    const real = slices.filter((s) => !s.derived);
    const tail = slices.filter((s) => s.derived);
    if (real.length <= CAP) return slices;
    const shown = real.slice(0, CAP);
    const hidden = real.slice(CAP);
    const secs = hidden.reduce((n, s) => n + (Number(s.secs) || 0), 0);
    const pct = hidden.reduce((n, s) => n + (Number(s.pct) || 0), 0);
    return [
      ...shown,
      {
        state: `… ${hidden.length} more`,
        secs,
        pct,
        color: "var(--est-bar-bg-alt)",
        derived: true,
      },
      ...tail,
    ];
  }

  // A frame's state slices → indented sub-rows in the SAME table (a drill-down
  // beneath the frame row). Same three/four columns as the frame row; the state
  // name is indented + swatched so it reads as nested. slices already
  // dust-filtered + capped by the caller.
  _stateSubRows(slices, hasCompliance) {
    return slices.map((s) => {
      const { state, secs, pct, color } = s;
      const w = pct == null ? 0 : Math.max(0, Math.min(100, Number(pct)));
      const tint = `color-mix(in srgb, ${color} 22%, transparent)`;
      const bar =
        w > 0
          ? `linear-gradient(90deg, ${tint} 0 ${w}%, transparent ${w}% 100%)`
          : "none";
      return html`<tr>
        <td class="state-col">
          <span class="state-cell state-indent"
            ><span class="legend-swatch" style="background:${color}"></span
            >${state}</span
          >
        </td>
        <td class="cell-primary">${fmtDuration(secs)}</td>
        <td class="value-cell cell-secondary" style="--cell-bar:${bar}">
          ${fmtPct(pct)}
        </td>
        ${hasCompliance ? html`<td></td>` : nothing}
      </tr>`;
    });
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
      .frame-checklist {
        display: flex;
        flex-wrap: wrap;
        gap: 6px 16px;
      }
      .frame-check {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-weight: 400;
        margin-bottom: 0;
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
    // Also surface any already-checked frame whose sensor has since been disabled
    // in the tracker, so the checkbox stays visible (checked-but-unavailable)
    // instead of silently vanishing on next editor open.
    this._selectedFrames().forEach((f) => frames.add(f));
    if (frames.size > 0) return FRAME_ORDER.filter((f) => frames.has(f));
    return FRAME_ORDER;
  }

  // Effective selected frames: the multi-select `frames` array, or the legacy
  // single `frame` string (back-compat), else none = all. FRAME_ORDER-normalized.
  _selectedFrames() {
    return selectedFrames(this._config);
  }

  // Toggle one frame in the selection, write it back as `frames`, and drop the
  // legacy `frame` key so a picked card migrates cleanly. Empty selection clears
  // `frames` (undefined → stripped by _updateConfig) = all frames.
  _toggleFrame(f, on) {
    const next = this._selectedFrames().filter((x) => x !== f);
    if (on) next.push(f);
    const ordered = FRAME_ORDER.filter((x) => next.includes(x));
    // One update: write `frames` and drop the legacy `frame` key together.
    this._updateConfig({ frames: ordered.length ? ordered : undefined, frame: undefined });
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
            <option value="pie" ?selected=${chart === "pie"}>Pie (a donut per frame)</option>
            <option value="table" ?selected=${chart === "table"}>Table (states grouped by frame)</option>
          </select>
        </div>
        <div class="editor-row">
          <label>Frames</label>
          <div class="frame-checklist">
            ${this._frameOptions().map((f) => {
              const on = this._selectedFrames().includes(f);
              return html`<label class="frame-check">
                <input
                  type="checkbox"
                  ?checked=${on}
                  @change=${(e) => this._toggleFrame(f, e.target.checked)}
                />
                ${FRAME_LABELS[f] || f}
              </label>`;
            })}
          </div>
          <div class="editor-hint">
            Which frames to show (pie draws one donut each). None checked = all
            frames.
          </div>
        </div>
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
                Adds a compliance-score gauge (green when the target is met,
                red when not) beside the state pie.
              </div>
            </div>`
          : nothing}
        ${chart === "table"
          ? html`<div class="editor-row">
              <label>
                <input
                  type="checkbox"
                  ?checked=${!!this._config.show_states}
                  @change=${(e) =>
                    this._updateConfig(
                      "show_states",
                      e.target.checked || undefined
                    )}
                />
                Show per-state breakdown
              </label>
              <div class="editor-hint">
                Adds a per-state table under each frame's total. Off by default —
                the table shows frame totals only.
              </div>
            </div>`
          : nothing}
        ${chart === "table" && this._config.show_states
          ? html`<div class="editor-row">
              <label>
                <input
                  type="checkbox"
                  ?checked=${this._config.limit_states !== false}
                  @change=${(e) =>
                    this._updateConfig(
                      "limit_states",
                      e.target.checked ? undefined : false
                    )}
                />
                Limit to 5 states per frame
              </label>
              <div class="editor-hint">
                Shows the top 5 states in each frame; the rest collapse into a
                “… N more” row with their combined %.
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

  // key,value for one field, or a patch object for several at once (undefined
  // values are stripped either way).
  _updateConfig(key, value) {
    if (!this._config) return;
    const patch = typeof key === "object" ? key : { [key]: value };
    const newConfig = { ...this._config, ...patch };
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
