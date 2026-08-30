"""Shared pure helpers for Entity State Tracker.

Pure functions only — no HA I/O side effects, no storage. Callers (config flow,
engine, coordinator, sensor, card) share these so boundary/label/color logic has
a single source of truth.
"""

from __future__ import annotations

from hashlib import md5

from .const import FRAMES

# Human labels per frame. Plain nouns for calendar frames; the rolling frames
# name their span ("Last 24 hours"/"Last 7 days") without a "(rolling)" tag.
# "30d" is last-30-whole-days (not true rolling past retention — §6.4).
_FRAME_LABELS: dict[str, str] = {
    "today": "Today",
    "yesterday": "Yesterday",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "month": "This month",
    "year": "This year",
}


def tracker_device_name(entity_label: str, mode: str) -> str:
    """Return the tracker's device name: ``Entity State Tracker — <label>`` (§5).

    ``mode`` is accepted so callers pass what they have; the device name is
    mode-independent by design (one device per config entry regardless of mode).
    """
    return f"Entity State Tracker — {entity_label}"


def normalize_state(state: str) -> str:
    """Case-normalize a state string (lower + strip).

    Config flow and engine must agree on this so a user-typed ``Heat`` matches a
    recorded ``heat`` (§4 prefilled states are case-normalized).
    """
    return state.strip().lower()


def state_color(state: str) -> str:
    """Return a deterministic ``#rrggbb`` color for a state string (§5.3).

    Hash the state name to a stable hue, so a state always gets the same color
    across runs and adding a new state never recolors the existing ones. Fixed
    saturation/lightness keep slices legible; only the hue varies.
    """
    digest = md5(state.encode(), usedforsecurity=False).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535.0
    return _hsl_to_hex(hue, 0.55, 0.55)


def _hsl_to_hex(h: float, s: float, lightness: float) -> str:
    """Convert HSL (each 0..1) to a ``#rrggbb`` string."""
    if s == 0:
        r = g = b = lightness
    else:
        q = lightness * (1 + s) if lightness < 0.5 else lightness + s - lightness * s
        p = 2 * lightness - q
        r = _hue_to_rgb(p, q, h + 1 / 3)
        g = _hue_to_rgb(p, q, h)
        b = _hue_to_rgb(p, q, h - 1 / 3)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _hue_to_rgb(p: float, q: float, t: float) -> float:
    """One channel of an HSL→RGB conversion (standard piecewise form)."""
    t %= 1.0
    if t < 1 / 6:
        return p + (q - p) * 6 * t
    if t < 1 / 2:
        return q
    if t < 2 / 3:
        return p + (q - p) * (2 / 3 - t) * 6
    return p


def frame_label(frame_key: str) -> str:
    """Return a human label for a frame (§3).

    Unknown keys fall back to the raw key so a future frame can't crash a label
    lookup — it just renders un-prettified.
    """
    if frame_key in _FRAME_LABELS:
        return _FRAME_LABELS[frame_key]
    return frame_key


def unique_id(entry_id: str, frame: str, metric: str) -> str:
    """Build an entity unique_id: ``<entry_id>_<frame>_<metric>`` (§5)."""
    return f"{entry_id}_{frame}_{metric}"


if __name__ == "__main__":  # pragma: no cover
    # Self-check: state_color is deterministic and every canonical frame labels.
    assert state_color("on") == state_color("on")
    assert state_color("on") != state_color("off")
    assert all(c in "0123456789abcdef" for c in state_color("heat")[1:])
    assert normalize_state("  Heat ") == "heat"
    assert tracker_device_name("Front Door", "all_states") == (
        "Entity State Tracker — Front Door"
    )
    assert frame_label("24h") == "Last 24 hours"
    assert frame_label("today") == "Today"
    assert frame_label("mystery") == "mystery"
    assert unique_id("abc", "today", "duration") == "abc_today_duration"
    assert set(_FRAME_LABELS) == set(FRAMES)
    print("helpers self-check OK")
