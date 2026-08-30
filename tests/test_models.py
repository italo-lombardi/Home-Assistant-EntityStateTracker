"""Tests for Entity State Tracker data models (§6.2)."""

from __future__ import annotations

import pytest

from custom_components.entity_state_tracker.models import (
    FrameResult,
    StoredData,
    TrackerData,
    TrackerLedger,
)


def test_stored_data_empty() -> None:
    """empty() yields version 1 with no trackers."""
    data = StoredData.empty()
    assert data.version == 1
    assert data.trackers == {}


def test_stored_data_from_dict_none() -> None:
    """A falsy raw doc deserializes to empty."""
    assert StoredData.from_dict(None) == StoredData.empty()
    assert StoredData.from_dict({}) == StoredData.empty()


def test_tracker_ledger_roundtrip() -> None:
    """to_dict → from_dict is lossless for a fully-populated ledger."""
    ledger = TrackerLedger(
        entity_id="climate.living_room",
        mode="specific_states",
        states=["heat", "auto"],
        target=["heat"],
        mode_changed_on="2026-08-01",
        daily={"2026-08-29": {"heat": {"secs": 12.5, "count": 3}}},
        last_state="heat",
        last_changed_ts="2026-08-29T10:00:00+00:00",
        last_updated_day="2026-08-29",
    )
    restored = TrackerLedger.from_dict(ledger.to_dict())
    assert restored == ledger


def test_tracker_ledger_from_dict_defaults() -> None:
    """Missing optional keys fall back to null/empty; states/target stay None."""
    ledger = TrackerLedger.from_dict({})
    assert ledger.entity_id == ""
    assert ledger.mode == ""
    assert ledger.states is None
    assert ledger.target is None
    assert ledger.mode_changed_on is None
    assert ledger.daily == {}
    assert ledger.last_state is None


def test_tracker_ledger_from_dict_states_target_coerced() -> None:
    """List states/target are stringified element-wise."""
    ledger = TrackerLedger.from_dict({"states": ["heat", 1], "target": [2, "auto"]})
    assert ledger.states == ["heat", "1"]
    assert ledger.target == ["2", "auto"]


def test_tracker_ledger_from_dict_states_target_non_list() -> None:
    """Non-list states/target become None."""
    ledger = TrackerLedger.from_dict({"states": "heat", "target": {"x": 1}})
    assert ledger.states is None
    assert ledger.target is None


def test_tracker_ledger_from_dict_daily_none() -> None:
    """A null daily map yields an empty daily dict."""
    assert TrackerLedger.from_dict({"daily": None}).daily == {}


def test_tracker_ledger_from_dict_bad_day_bucket_dropped() -> None:
    """A non-dict day value is skipped."""
    ledger = TrackerLedger.from_dict({"daily": {"2026-08-29": "not-a-dict"}})
    assert ledger.daily == {}


def test_tracker_ledger_from_dict_bad_row_dropped() -> None:
    """A non-dict state row is skipped; sibling good rows survive."""
    ledger = TrackerLedger.from_dict(
        {
            "daily": {
                "2026-08-29": {
                    "heat": "not-a-dict",
                    "auto": {"secs": 5, "count": 1},
                }
            }
        }
    )
    assert ledger.daily == {"2026-08-29": {"auto": {"secs": 5.0, "count": 1}}}


def test_tracker_ledger_from_dict_bad_numbers_swallowed() -> None:
    """A row whose secs/count cannot be numeric-coerced is dropped."""
    ledger = TrackerLedger.from_dict(
        {"daily": {"2026-08-29": {"heat": {"secs": "abc", "count": 1}}}}
    )
    assert ledger.daily == {}


def test_tracker_ledger_from_dict_empty_bucket_dropped() -> None:
    """A day whose rows all get dropped leaves no empty day key."""
    ledger = TrackerLedger.from_dict(
        {"daily": {"2026-08-29": {"heat": {"secs": None, "count": 1}}}}
    )
    assert ledger.daily == {}


def test_tracker_ledger_from_dict_row_missing_keys_defaults() -> None:
    """Missing secs/count in a row default to 0.0/0."""
    ledger = TrackerLedger.from_dict({"daily": {"2026-08-29": {"heat": {}}}})
    assert ledger.daily == {"2026-08-29": {"heat": {"secs": 0.0, "count": 0}}}


def test_tracker_ledger_from_dict_day_key_stringified() -> None:
    """Non-string day keys are coerced to str."""
    ledger = TrackerLedger.from_dict({"daily": {20260829: {"heat": {"secs": 1}}}})
    assert "20260829" in ledger.daily


def test_stored_data_from_dict_roundtrip() -> None:
    """StoredData survives a to_dict → from_dict roundtrip."""
    data = StoredData(
        version=1,
        trackers={
            "entry_a": TrackerLedger(entity_id="light.x", mode="all_states"),
        },
    )
    restored = StoredData.from_dict(data.to_dict())
    assert restored == data


def test_stored_data_from_dict_trackers_none() -> None:
    """A null trackers map yields no trackers."""
    assert StoredData.from_dict({"version": 1, "trackers": None}).trackers == {}


def test_stored_data_from_dict_non_dict_tracker_dropped() -> None:
    """A non-dict tracker row is skipped."""
    data = StoredData.from_dict({"trackers": {"bad": "not-a-dict"}})
    assert data.trackers == {}


def test_stored_data_from_dict_tracker_key_stringified() -> None:
    """Non-string tracker keys are coerced to str."""
    data = StoredData.from_dict({"trackers": {42: {"entity_id": "light.x"}}})
    assert "42" in data.trackers


def test_stored_data_from_dict_bad_version_defaults() -> None:
    """A non-integer version falls back to 1."""
    assert StoredData.from_dict({"version": "not-int"}).version == 1


def test_stored_data_from_dict_version_none_defaults() -> None:
    """A null version falls back to 1 via the TypeError branch."""
    assert StoredData.from_dict({"version": None, "trackers": {}}).version == 1


def test_stored_data_from_dict_tracker_ledger_raises_swallowed(monkeypatch) -> None:
    """A TrackerLedger.from_dict that raises is caught and the row dropped."""

    def _boom(_row):
        raise KeyError("boom")

    monkeypatch.setattr(
        TrackerLedger, "from_dict", classmethod(lambda cls, r: _boom(r))
    )
    data = StoredData.from_dict({"trackers": {"entry_a": {"entity_id": "x"}}})
    assert data.trackers == {}


def test_frame_result_defaults() -> None:
    """FrameResult constructs with only window_seconds; the rest default."""
    fr = FrameResult(window_seconds=3600.0)
    assert fr.window_seconds == 3600.0
    assert fr.breakdown_seconds == {}
    assert fr.breakdown_pct == {}
    assert fr.counts == {}
    assert fr.avg_duration == {}
    assert fr.dominant is None
    assert fr.data_start is None
    assert fr.window_coverage == 1.0
    assert fr.has_gap is False
    assert fr.percent is None
    assert fr.compliance_percent is None
    assert fr.unaccounted_seconds == 0.0


def test_frame_result_frozen() -> None:
    """FrameResult is immutable."""
    fr = FrameResult(window_seconds=1.0)
    with pytest.raises((AttributeError, TypeError)):
        fr.window_seconds = 2.0  # type: ignore[misc]


def test_tracker_data_defaults() -> None:
    """TrackerData constructs empty."""
    td = TrackerData()
    assert td.frames == {}
    assert td.last_state is None
    assert td.previous_state is None


def test_tracker_data_construction() -> None:
    """TrackerData carries frames + transition context."""
    fr = FrameResult(window_seconds=60.0)
    td = TrackerData(frames={"today": fr}, last_state="on", previous_state="off")
    assert td.frames["today"] is fr
    assert td.last_state == "on"
    assert td.previous_state == "off"


def test_tracker_data_frozen() -> None:
    """TrackerData is immutable."""
    td = TrackerData()
    with pytest.raises((AttributeError, TypeError)):
        td.last_state = "on"  # type: ignore[misc]
