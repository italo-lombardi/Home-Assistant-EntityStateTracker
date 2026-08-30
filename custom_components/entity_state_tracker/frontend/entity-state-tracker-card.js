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

const DOMAIN_PREFIX = "sensor.entity_state_tracker_";

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

// The sensors use `_attr_has_entity_name = True`, so HA slugifies the DEVICE
// name + the human ENTITY label (e.g. "Duration (Last 24 hours)") into the
// object_id — the frame key ("24h"/"7d"/…) NEVER appears in the entity_id.
// We must therefore match on the slugified LABEL, not the raw frame key.
//
// FRAME_LABEL_SLUGS maps each frame's label-slug → frame key. Sorted
// longest-first so a longest-suffix match wins ("this_month" before "month",
// "last_24_hours" before nothing shorter it contains). Mirrors HA's slugify:
// lowercase, non-alphanumerics → "_", collapse runs, trim.
function _slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

const FRAME_LABEL_SLUGS = Object.entries(FRAME_LABELS)
  .map(([key, label]) => ({ key, slug: _slugify(label) }))
  .sort((a, b) => b.slug.length - a.slug.length);

// Slugified metric segments (from strings.json entity names: "Duration",
// "State Breakdown") that sit between the device stem and the frame label-slug.
// Only Duration (specific mode) and State Breakdown (all-states mode) entities
// exist. Longest-first so "state_breakdown" wins over any shorter contained
// token.
const METRIC_SLUGS = ["state_breakdown", "duration"];

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

// Match a tracker sensor's object_id against the frame-label slugs. Entity ids
// look like `sensor.entity_state_tracker_<device>_<metric>_<label_slug>`, e.g.
// `..._sun_state_breakdown_last_24_hours`. We find which label-slug the id ENDS
// WITH (longest-match, so "this_month" beats a bare "month" and "last_7_days"
// isn't confused with anything shorter). Returns {frame, slug} or null.
function _matchFrame(entityId) {
  for (const { key, slug } of FRAME_LABEL_SLUGS) {
    if (entityId === slug || entityId.endsWith(`_${slug}`)) {
      return { frame: key, slug };
    }
  }
  return null;
}

// Parse the frame key out of a tracker sensor's object_id — the frame key
// mapped from the slugified human label the id ends with.
function frameFromEntityId(entityId) {
  const m = _matchFrame(entityId);
  return m ? m.frame : null;
}

// -----------------------------------------------------------------------------
const cardStyles = css`
  :host {
    --est-text-primary: var(--primary-text-color, #212121);
    --est-text-secondary: var(--secondary-text-color, #727272);
    --est-divider: var(--divider-color, rgba(0, 0, 0, 0.12));
    --est-bar-bg: var(--disabled-color, #bdbdbd);
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

  .body {
    padding: 4px 16px 16px;
  }

  .error-message {
    padding: 16px;
    color: var(--error-color, #f44336);
    font-size: 14px;
  }

  /* Bars */
  .bar-row {
    margin: 12px 0;
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

  .bar-compliance {
    height: 6px;
    border-radius: 3px;
    background: var(--est-bar-bg);
    margin-top: 3px;
    position: relative;
    overflow: hidden;
  }

  .bar-compliance .fill {
    position: absolute;
    top: 0;
    left: 0;
    bottom: 0;
    border-radius: 3px;
    background: var(--success-color, #4caf50);
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
    flex: 1;
    min-width: 140px;
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
    margin-left: auto;
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

  static getStubConfig(hass) {
    // Preview: find any tracker sensor and offer its stem as the entity.
    const match = Object.keys(hass.states).find((id) =>
      id.startsWith(DOMAIN_PREFIX)
    );
    return {
      entity: match || "sensor.entity_state_tracker_example_breakdown_today",
      chart: "bars",
    };
  }

  constructor() {
    super();
    this._config = {};
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("You must define an 'entity' in the card configuration.");
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

  // Discover every frame sensor belonging to the configured tracker. The config
  // `entity` is the device or one of its sensors; all frame sensors of one
  // tracker share the same object_id stem up to the frame token. We match by
  // that stem so the card works whether the user pointed us at the breakdown
  // sensor, a duration sensor, or the device's default entity.
  _trackerSensors() {
    if (!this.hass || !this._config.entity) return [];
    const configured = this._config.entity;
    const stem = this._stemOf(configured);
    const out = [];
    for (const id of Object.keys(this.hass.states)) {
      if (!id.startsWith(DOMAIN_PREFIX)) continue;
      const frame = frameFromEntityId(id);
      if (!frame) continue;
      if (this._stemOf(id) !== stem) continue;
      const st = this.hass.states[id];
      out.push({ entity_id: id, frame, state: st.state, attrs: st.attributes });
    }
    // Stable frame order for deterministic rendering.
    out.sort((a, b) => FRAME_ORDER.indexOf(a.frame) - FRAME_ORDER.indexOf(b.frame));
    return out;
  }

  // The object_id with the trailing `_<metric>_<label_slug>` stripped, so every
  // frame of one tracker — and both its metrics (duration for specific mode,
  // state_breakdown for all-states) — collapse to a common stem.
  // Strip the full matched label-slug first, then a known trailing metric
  // segment, leaving `sensor.entity_state_tracker_<device>`.
  _stemOf(entityId) {
    const m = _matchFrame(entityId);
    if (!m) return entityId;
    // Drop the label-slug (+ its leading "_").
    let stem = entityId.slice(0, entityId.length - m.slug.length - 1);
    // Drop the metric segment if present, so duration and state_breakdown both
    // reduce to the same device stem.
    for (const metric of METRIC_SLUGS) {
      if (stem.endsWith(`_${metric}`)) {
        stem = stem.slice(0, stem.length - metric.length - 1);
        break;
      }
    }
    return stem;
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
          No Entity State Tracker sensors found for "${this._config.entity}".
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
        <div class="title">${title}</div>
      </div>
      <div class="body">${body}</div>
    </ha-card>`;
  }

  _deriveTitle(sensors) {
    // Never reverse-engineer a title from friendly_name (labels like "Last 24
    // hours" have variable token counts, so the old 2-token strip mangled
    // them). Prettify the tracker stem's device segment instead — stable
    // regardless of metric or frame label.
    const stem = this._stemOf(sensors[0]?.entity_id || "");
    const device = stem.slice(DOMAIN_PREFIX.length);
    if (device) {
      return device
        .split("_")
        .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
        .join(" ");
    }
    return "Entity State Tracker";
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
  // Bars: one row per frame. % fill + "6.2 h · 26%"; compliance second bar when
  // a target is set; transition line from counts/avg_duration.
  // ---------------------------------------------------------------------------
  _renderBars(sensors) {
    return sensors.map((s) => {
      const a = s.attrs || {};
      let pct;
      let durSecs;
      if (this._isBreakdown(s)) {
        // Dominant state's slice for the bar; state name is s.state.
        durSecs = (a.breakdown_seconds || {})[s.state];
        pct = (a.breakdown_pct || {})[s.state];
      } else {
        durSecs = Number(s.state); // duration sensor native value = seconds
        pct = a.percent;
      }
      const pctNum = pct == null ? 0 : Math.max(0, Math.min(100, Number(pct)));
      const incomplete = this._incomplete(a);
      const label = this._isBreakdown(s) ? `${s.state}` : "";
      return html`<div class="bar-row">
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
          ? html`<div class="bar-compliance">
              <div
                class="fill"
                style="width:${Math.max(
                  0,
                  Math.min(100, Number(a.compliance_percent))
                )}%"
              ></div>
            </div>`
          : nothing}
        ${this._transitionLine(a, this._isBreakdown(s) ? s.state : null)}
      </div>`;
    });
  }

  // "entered 12× · avg 5 min" from counts/avg_duration. For a specific-mode
  // sensor with a single tracked state we sum; for breakdown we show the
  // dominant state's row. "entered" (not "opened") because states are arbitrary.
  _transitionLine(attrs, stateKey) {
    const counts = attrs.counts || {};
    const avg = attrs.avg_duration || {};
    let count;
    let avgSecs;
    if (stateKey != null) {
      count = counts[stateKey];
      avgSecs = avg[stateKey];
    } else {
      const keys = Object.keys(counts);
      count = keys.reduce((n, k) => n + (counts[k] || 0), 0);
      // Weighted-ish: just show avg of the first tracked state if present.
      avgSecs = keys.length ? avg[keys[0]] : null;
    }
    if (count == null || count === 0) return nothing;
    const avgTxt = avgSecs != null ? ` · avg ${fmtDuration(avgSecs)}` : "";
    return html`<div class="transitions">
      entered ${count}×${avgTxt}
    </div>`;
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
      // Specific mode: DurationSensor emits `percent` (in-state %) but NOT
      // window_seconds. Derive the "rest" slice from the percentage so the pie
      // is always two slices (in-state vs rest), scaled off the in-state secs.
      const inSecs = Number(pick.state) || 0;
      const inPct = a.percent != null ? Number(a.percent) : null;
      const restPct = inPct != null ? Math.max(0, 100 - inPct) : null;
      // Recover rest seconds proportionally: inSecs / inPct == total / 100.
      const restSecs =
        inPct != null && inPct > 0 ? inSecs * (restPct / inPct) : null;
      const tracked = (a.tracked_states || []).join(", ") || "tracked";
      slices = [
        {
          state: tracked,
          secs: inSecs,
          pct: inPct,
          color: stateColor(tracked),
        },
      ];
      if (restPct != null) {
        slices.push({
          state: "other",
          secs: restSecs != null ? restSecs : 0,
          pct: restPct,
          color: "var(--est-bar-bg)",
        });
      }
    }
    slices = slices.filter((s) => s.secs > 0);
    const total = slices.reduce((n, s) => n + s.secs, 0) || 1;

    // Build a donut with stacked conic-gradient-free SVG arcs.
    const cx = 50;
    const cy = 50;
    const r = 40;
    const inner = 24;
    let angle = -Math.PI / 2;
    const single = slices.length === 1;
    const paths = slices.map((s) => {
      const frac = s.secs / total;
      // A full-circle slice (only slice, or frac≈1) has coincident arc
      // endpoints → a zero-length, INVISIBLE path. Draw a donut RING (outer
      // circle + inner hole punched via even-odd fill) instead.
      if (single || frac >= 0.9999) {
        return this._ring(cx, cy, r, inner, s.color);
      }
      const a0 = angle;
      const a1 = angle + frac * 2 * Math.PI;
      angle = a1;
      return html`<path
        d="${this._arc(cx, cy, r, inner, a0, a1)}"
        fill="${s.color}"
      ></path>`;
    });

    const incomplete = this._incomplete(a);
    return html`
      <div class="frame-picker">
        ${FRAME_LABELS[pick.frame] || pick.frame}${incomplete && a.data_start
          ? html`<span class="since">since ${fmtDate(a.data_start)}</span>`
          : nothing}
      </div>
      <div class="pie-wrap">
        <svg class="pie-svg" width="100" height="100" viewBox="0 0 100 100">
          ${paths}
        </svg>
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
    `;
  }

  // Full donut RING for a single 100% slice — outer + inner circle as one
  // even-odd path so the inner hole is punched out (an arc path with coincident
  // endpoints would be zero-length and invisible). Two arcs per circle since SVG
  // arcs can't span a full 360°.
  _ring(cx, cy, r, ri, color) {
    const d =
      `M ${cx - r} ${cy} A ${r} ${r} 0 1 1 ${cx + r} ${cy} ` +
      `A ${r} ${r} 0 1 1 ${cx - r} ${cy} Z ` +
      `M ${cx - ri} ${cy} A ${ri} ${ri} 0 1 0 ${cx + ri} ${cy} ` +
      `A ${ri} ${ri} 0 1 0 ${cx - ri} ${cy} Z`;
    return html`<path d="${d}" fill="${color}" fill-rule="evenodd"></path>`;
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
    const GAP_ROW = " gap"; // sentinel key, never a real state name
    // Order rows by total seconds desc for a stable, readable table.
    const totals = {};
    for (const st of stateSet) totals[st] = 0;
    const secsFor = (s, st) => {
      if (st === GAP_ROW)
        return this._isBreakdown(s) ? Number((s.attrs || {}).unaccounted_seconds) || 0 : 0;
      if (this._isBreakdown(s)) return ((s.attrs || {}).breakdown_seconds || {})[st] || 0;
      const tracked = ((s.attrs || {}).tracked_states || []).join(", ") || "tracked";
      return st === tracked ? Number(s.state) || 0 : 0;
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
