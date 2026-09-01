"""Tests for Entity State Tracker integration setup, unload, and card install."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import entity_state_tracker as _init_module
from custom_components.entity_state_tracker import (
    _CARD_INSTALLED_KEY,
    CARD_URL,
    PLATFORMS,
    _async_install_card,
    _async_register_lovelace_resource,
    _async_update_options,
    _get_version,
    async_unload_entry,
)
from custom_components.entity_state_tracker.const import DOMAIN

CARD_FILENAME = "entity-state-tracker-card.js"

# Read the version straight from the manifest so a version bump never breaks
# these tests (the point is to check _get_version reflects the manifest, not to
# pin a literal). Same source _get_version reads.
_MANIFEST = Path(_init_module.__file__).parent / "manifest.json"
MANIFEST_VERSION = json.loads(_MANIFEST.read_text())["version"]


@pytest.fixture(autouse=True)
def _offline_recorder():
    """Patch the recorder query so first_refresh works offline (returns empty)."""
    with patch(
        "custom_components.entity_state_tracker.coordinator.query_recorder",
        new=AsyncMock(return_value=[]),
    ):
        yield


# --------------------------------------------------------------------------- #
# Full setup / unload lifecycle
# --------------------------------------------------------------------------- #


async def test_full_setup_entry(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """A full async_setup drives coordinator, platforms, services, and card."""
    specific_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.entity_state_tracker._async_install_card",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ) as mock_forward,
    ):
        assert await hass.config_entries.async_setup(specific_config_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][specific_config_entry.entry_id]
    assert coordinator.entity_id == "climate.living_room"
    mock_forward.assert_called_once_with(specific_config_entry, PLATFORMS)


async def test_async_update_options_reloads(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """_async_update_options reloads the config entry."""
    specific_config_entry.add_to_hass(hass)

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        await _async_update_options(hass, specific_config_entry)

    mock_reload.assert_awaited_once_with(specific_config_entry.entry_id)


async def test_unload_last_entry_removes_card_key(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """Unloading the only entry removes the card-installed key."""
    specific_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.entity_state_tracker._async_install_card",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        await hass.config_entries.async_setup(specific_config_entry.entry_id)
        await hass.async_block_till_done()

    hass.data[DOMAIN][_CARD_INSTALLED_KEY] = "0.1.0-abc"

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        result = await async_unload_entry(hass, specific_config_entry)

    assert result is True
    assert specific_config_entry.entry_id not in hass.data[DOMAIN]
    assert _CARD_INSTALLED_KEY not in hass.data[DOMAIN]


async def test_unload_with_second_loaded_entry_keeps_card_key(
    hass: HomeAssistant,
    specific_config_entry: MockConfigEntry,
    all_states_config_entry: MockConfigEntry,
) -> None:
    """A second LOADED entry keeps the card key after one unloads."""
    specific_config_entry.add_to_hass(hass)
    all_states_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.entity_state_tracker._async_install_card",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        await hass.config_entries.async_setup(specific_config_entry.entry_id)
        await hass.async_block_till_done()
    # Mark the sibling entry LOADED so the remaining-entries check finds it,
    # without driving a second full setup (which would need its own teardown).
    all_states_config_entry.mock_state(hass, ConfigEntryState.LOADED)

    hass.data[DOMAIN][_CARD_INSTALLED_KEY] = "0.1.0-abc"

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        result = await async_unload_entry(hass, specific_config_entry)

    assert result is True
    # The other entry is still LOADED, so the card key survives.
    assert _CARD_INSTALLED_KEY in hass.data[DOMAIN]


async def test_unload_failure_keeps_entry(
    hass: HomeAssistant, specific_config_entry: MockConfigEntry
) -> None:
    """A failed platform unload leaves the coordinator in hass.data."""
    specific_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.entity_state_tracker._async_install_card",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        await hass.config_entries.async_setup(specific_config_entry.entry_id)
        await hass.async_block_till_done()

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        result = await async_unload_entry(hass, specific_config_entry)

    assert result is False
    assert specific_config_entry.entry_id in hass.data[DOMAIN]


async def test_async_setup_component_returns_true(hass: HomeAssistant) -> None:
    """The YAML-level async_setup is a no-op returning True."""
    assert await async_setup_component(hass, DOMAIN, {})


def test_platforms_defined() -> None:
    """SENSOR and BINARY_SENSOR are the declared platforms."""
    from homeassistant.const import Platform

    assert Platform.SENSOR in PLATFORMS
    assert Platform.BINARY_SENSOR in PLATFORMS
    assert len(PLATFORMS) == 2


# --------------------------------------------------------------------------- #
# _get_version
# --------------------------------------------------------------------------- #


async def test_get_version_with_card_appends_md5(hass: HomeAssistant) -> None:
    """When the card JS exists, the version gets an 8-char md5 suffix."""
    version = await hass.async_add_executor_job(_get_version)
    base, _, digest = version.partition("-")
    assert base == MANIFEST_VERSION
    # The frontend card ships with the integration, so a hash is appended.
    assert len(digest) == 8
    assert all(c in "0123456789abcdef" for c in digest)


async def test_get_version_without_card(hass: HomeAssistant) -> None:
    """When the card JS is missing, the bare manifest version is returned."""
    real_exists = __import__("pathlib").Path.exists

    def _fake_exists(self):
        if self.name == CARD_FILENAME:
            return False
        return real_exists(self)

    with patch("pathlib.Path.exists", _fake_exists):
        version = await hass.async_add_executor_job(_get_version)

    assert version == MANIFEST_VERSION


# --------------------------------------------------------------------------- #
# _async_install_card
# --------------------------------------------------------------------------- #


async def test_install_card_skips_when_same_version(hass: HomeAssistant) -> None:
    """Card install short-circuits when the installed version matches."""
    hass.data.setdefault(DOMAIN, {})[_CARD_INSTALLED_KEY] = "1.2.3"

    with (
        patch(
            "custom_components.entity_state_tracker._get_version",
            return_value="1.2.3",
        ),
        patch(
            "custom_components.entity_state_tracker._async_register_lovelace_resource",
            new=AsyncMock(),
        ) as mock_register,
    ):
        await _async_install_card(hass)

    mock_register.assert_not_called()


async def test_install_card_warns_and_pops_when_js_missing(
    hass: HomeAssistant, caplog
) -> None:
    """A missing JS source warns, skips, and clears the claimed key."""
    hass.data.setdefault(DOMAIN, {})

    with (
        patch(
            "custom_components.entity_state_tracker._get_version",
            return_value="9.9.9",
        ),
        patch("pathlib.Path.exists", return_value=False),
        caplog.at_level(logging.WARNING),
    ):
        await _async_install_card(hass)

    assert "Card JS not found" in caplog.text
    assert _CARD_INSTALLED_KEY not in hass.data[DOMAIN]


async def test_install_card_registers_static_path(hass: HomeAssistant) -> None:
    """Happy path registers the static path and the Lovelace resource."""
    hass.data.setdefault(DOMAIN, {})
    mock_http = MagicMock()
    mock_http.async_register_static_paths = AsyncMock()
    hass.http = mock_http

    with (
        patch(
            "custom_components.entity_state_tracker._get_version",
            return_value="1.0.0",
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "custom_components.entity_state_tracker._async_register_lovelace_resource",
            new=AsyncMock(),
        ) as mock_register,
    ):
        await _async_install_card(hass)

    mock_http.async_register_static_paths.assert_awaited_once()
    mock_register.assert_awaited_once()
    assert hass.data[DOMAIN][_CARD_INSTALLED_KEY] == "1.0.0"


async def test_install_card_static_path_already_registered(hass: HomeAssistant) -> None:
    """A duplicate static-path registration is swallowed (debug log)."""
    hass.data.setdefault(DOMAIN, {})
    mock_http = MagicMock()
    mock_http.async_register_static_paths = AsyncMock(
        side_effect=RuntimeError("already registered")
    )
    hass.http = mock_http

    with (
        patch(
            "custom_components.entity_state_tracker._get_version",
            return_value="1.0.0",
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "custom_components.entity_state_tracker._async_register_lovelace_resource",
            new=AsyncMock(),
        ) as mock_register,
    ):
        await _async_install_card(hass)

    # Static-path failure is non-fatal; resource registration still runs.
    mock_register.assert_awaited_once()
    assert hass.data[DOMAIN][_CARD_INSTALLED_KEY] == "1.0.0"


async def test_install_card_pops_key_on_register_failure(
    hass: HomeAssistant, caplog
) -> None:
    """A Lovelace-resource failure warns and clears the claimed key."""
    hass.data.setdefault(DOMAIN, {})
    mock_http = MagicMock()
    mock_http.async_register_static_paths = AsyncMock()
    hass.http = mock_http

    with (
        patch(
            "custom_components.entity_state_tracker._get_version",
            return_value="1.0.0",
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "custom_components.entity_state_tracker._async_register_lovelace_resource",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        caplog.at_level(logging.WARNING),
    ):
        await _async_install_card(hass)

    assert "Failed to register Lovelace resource" in caplog.text
    assert _CARD_INSTALLED_KEY not in hass.data[DOMAIN]


# --------------------------------------------------------------------------- #
# _async_register_lovelace_resource
# --------------------------------------------------------------------------- #


async def test_register_resource_creates_when_empty(hass: HomeAssistant) -> None:
    """Loads the collection and creates a new resource when none exists."""
    from homeassistant.components.lovelace.resources import (
        ResourceStorageCollection,
    )

    resources = MagicMock(spec=ResourceStorageCollection)
    resources.loaded = False
    resources.async_load = AsyncMock()
    resources.async_items.return_value = []
    resources.async_create_item = AsyncMock()

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, "1.2.3")

    resources.async_load.assert_awaited_once()
    resources.async_create_item.assert_awaited_once()


async def test_register_resource_already_loaded_skips_load(
    hass: HomeAssistant,
) -> None:
    """A pre-loaded collection is not reloaded."""
    from homeassistant.components.lovelace.resources import (
        ResourceStorageCollection,
    )

    resources = MagicMock(spec=ResourceStorageCollection)
    resources.loaded = True
    resources.async_load = AsyncMock()
    resources.async_items.return_value = []
    resources.async_create_item = AsyncMock()

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, "1.2.3")

    resources.async_load.assert_not_called()
    resources.async_create_item.assert_awaited_once()


async def test_register_resource_yaml_mode_logs_manual_add(
    hass: HomeAssistant, caplog
) -> None:
    """A non-storage (YAML) collection cannot be created → INFO manual-add log."""
    resources = MagicMock()  # not spec=ResourceStorageCollection → isinstance False
    resources.loaded = True
    resources.async_load = AsyncMock()
    resources.async_items.return_value = []
    resources.async_create_item = AsyncMock()

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, "1.0.0")

    resources.async_create_item.assert_not_called()
    assert "YAML resource mode" in caplog.text


async def test_register_resource_updates_existing_version_bump(
    hass: HomeAssistant,
) -> None:
    """An existing resource with an old URL is updated to the current version."""
    from homeassistant.components.lovelace.resources import (
        ResourceStorageCollection,
    )

    version = "2.0.0"
    expected = f"{CARD_URL}?automatically-added&{version}"
    old_url = f"{CARD_URL}?automatically-added&1.0.0"

    resources = MagicMock(spec=ResourceStorageCollection)
    resources.loaded = True
    resources.async_load = AsyncMock()
    resources.async_items.return_value = [{"id": "res_1", "url": old_url}]
    resources.async_update_item = AsyncMock()
    resources.async_delete_item = AsyncMock()

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, version)

    resources.async_update_item.assert_awaited_once_with(
        "res_1", {"res_type": "module", "url": expected}
    )
    resources.async_delete_item.assert_not_called()


async def test_register_resource_removes_duplicates(hass: HomeAssistant) -> None:
    """Duplicate resources are deleted, keeping only the first."""
    from homeassistant.components.lovelace.resources import (
        ResourceStorageCollection,
    )

    version = "3.0.0"
    url = f"{CARD_URL}?automatically-added&{version}"

    resources = MagicMock(spec=ResourceStorageCollection)
    resources.loaded = True
    resources.async_load = AsyncMock()
    resources.async_items.return_value = [
        {"id": "res_1", "url": url},
        {"id": "res_2", "url": url},
    ]
    resources.async_delete_item = AsyncMock()
    resources.async_update_item = AsyncMock()

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, version)

    resources.async_delete_item.assert_awaited_once_with("res_2")
    # First already current — no update.
    resources.async_update_item.assert_not_called()


async def test_register_resource_non_storage_inplace_url_update(
    hass: HomeAssistant,
) -> None:
    """Non-storage collection updates the existing dict URL in place."""
    version = "4.0.0"
    old_url = f"{CARD_URL}?automatically-added&1.0.0"
    expected = f"{CARD_URL}?automatically-added&{version}"

    resources = MagicMock()  # not spec=ResourceStorageCollection → isinstance False
    resources.loaded = True
    resources.async_load = AsyncMock()
    existing = {"id": "res_1", "url": old_url}
    resources.async_items.return_value = [existing]

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, version)

    assert existing["url"] == expected


async def test_register_resource_non_storage_duplicates_not_deleted(
    hass: HomeAssistant,
) -> None:
    """Non-storage collection with duplicates: loop runs but isinstance is False.

    Covers the ``for r in existing[1:]`` loop body executing while the
    ResourceStorageCollection isinstance guard is False, so no delete fires.
    """
    version = "5.0.0"
    url = f"{CARD_URL}?automatically-added&{version}"

    resources = MagicMock()  # not spec=ResourceStorageCollection → isinstance False
    resources.loaded = True
    resources.async_load = AsyncMock()
    resources.async_items.return_value = [
        {"id": "res_1", "url": url},
        {"id": "res_2", "url": url},
    ]
    resources.async_delete_item = AsyncMock()

    hass.data["lovelace"] = MagicMock()
    hass.data["lovelace"].resources = resources

    await _async_register_lovelace_resource(hass, version)

    resources.async_delete_item.assert_not_called()


async def test_register_resource_yaml_mode_guard(hass: HomeAssistant, caplog) -> None:
    """YAML-mode: hass.data['lovelace'].resources missing → INFO log, no raise."""
    hass.data.pop("lovelace", None)

    with caplog.at_level(logging.INFO):
        await _async_register_lovelace_resource(hass, "1.0.0")

    assert "Could not auto-register Lovelace resource" in caplog.text


async def test_register_resource_yaml_mode_attribute_error(
    hass: HomeAssistant, caplog
) -> None:
    """A lovelace object without .resources also hits the guard (AttributeError)."""

    class _NoResources:
        pass

    hass.data["lovelace"] = _NoResources()

    with caplog.at_level(logging.INFO):
        await _async_register_lovelace_resource(hass, "1.0.0")

    assert "Could not auto-register Lovelace resource" in caplog.text
