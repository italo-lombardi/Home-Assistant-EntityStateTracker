"""Constants for the Entity State Tracker integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from homeassistant.const import Platform

DOMAIN = "entity_state_tracker"

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Modes (config-flow branch point — §4)
MODE_SPECIFIC = "specific_states"
MODE_ALL = "all_states"
Mode = Literal["specific_states", "all_states"]

# Frames (canonical set — §3). Each frame is either calendar-aligned
# (bounded by local midnight/1st/Jan 1) or rolling (now − delta → now).
FrameKind = Literal["calendar", "rolling"]

FRAMES: dict[str, FrameKind] = {
    "today": "calendar",
    "yesterday": "calendar",
    "24h": "rolling",
    "week": "calendar",
    "last_week": "calendar",
    "7d": "rolling",
    "30d": "rolling",
    "month": "calendar",
    "last_month": "calendar",
    "year": "calendar",
}

# Frames on by default; week/30d/month/year exceed retention and fill in over time.
DEFAULT_FRAMES = ["today", "yesterday", "24h", "7d"]

# Config flow keys
CONF_ENTITY = "entity"
CONF_MODE = "mode"
CONF_STATES = "states"
CONF_ENABLE_COMPLIANCE = "enable_compliance"
CONF_TARGET = "target"
CONF_TARGET_THRESHOLD = "target_threshold"
CONF_FRAMES = "frames"
CONF_MIN_STATE_DURATION = "min_state_duration"

# Defaults
DEFAULT_MIN_STATE_DURATION = 0  # seconds — glitch filter opt-in

# Storage (§8)
STORAGE_VERSION = 1
STORAGE_KEY_FMT = "entity_state_tracker.{entry_id}"

# Coordinator base-class poll cadence (§6.6) — advances open blocks.
SCAN_INTERVAL = timedelta(minutes=5)

# Prune ceiling: buckets older than this many days are dropped (§6.2).
LEDGER_MAX_DAYS = 400

# Dominant-state hysteresis margin in percent of window (§6.6, guards R10).
DOMINANT_HYSTERESIS_PCT = 1.0

# Bus event fired when an all-states tracker sees a previously unseen state (§5.2).
EVENT_NEW_STATE = "entity_state_tracker_new_state"

# Translation keys per metric (§5, §11) — one per emitted entity kind.
TRANSLATION_KEY_DURATION = "duration"
TRANSLATION_KEY_PERCENT = "percent"
TRANSLATION_KEY_COMPLIANCE = "compliance"
TRANSLATION_KEY_BREAKDOWN = "breakdown"
TRANSLATION_KEY_CURRENTLY_IN_STATE = "currently_in_state"
TRANSLATION_KEY_COMPLIANT = "compliant"
