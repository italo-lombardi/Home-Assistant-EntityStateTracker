"""The Entity State Tracker integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS
from .coordinator import EntityStateTrackerCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_FILENAME = "entity-state-tracker-card.js"
CARD_URL = f"/entity_state_tracker/{CARD_FILENAME}"
_CARD_INSTALLED_KEY = "_card_installed"


def _get_version() -> str:
    """Get cache-bust key: version + card JS content hash.

    Including a hash of the card JS ensures the Lovelace resource URL changes
    whenever the JS file is updated, even when the integration version stays the same.
    This forces browsers to fetch the new file instead of serving a cached copy.
    """
    import hashlib

    manifest = Path(__file__).parent / "manifest.json"
    version = "0.0.0"
    with manifest.open() as f:
        version = json.load(f).get("version", "0.0.0")

    card = Path(__file__).parent / "frontend" / CARD_FILENAME
    if card.exists():
        md5 = hashlib.md5(card.read_bytes(), usedforsecurity=False).hexdigest()[:8]
        return f"{version}-{md5}"
    return version


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Entity State Tracker integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Entity State Tracker from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = EntityStateTrackerCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(coordinator.async_shutdown)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    await _async_install_card(hass)

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update - reload the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_install_card(hass: HomeAssistant) -> None:
    """Serve card JS from component dir and register as Lovelace resource."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    version = await hass.async_add_executor_job(_get_version)
    if domain_data.get(_CARD_INSTALLED_KEY) == version:
        return

    # Claim the slot before proceeding so concurrent entry setups skip this
    # block. Two racing callers may both read version before either claims, but
    # static-path registration and Lovelace resource update are both idempotent
    # so a duplicate run is harmless.
    domain_data[_CARD_INSTALLED_KEY] = version

    source = Path(__file__).parent / "frontend" / CARD_FILENAME
    if not source.exists():
        _LOGGER.warning("Card JS not found at %s", source)
        domain_data.pop(_CARD_INSTALLED_KEY, None)
        return

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(source), True)]
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Static path %s already registered", CARD_URL)

    try:
        await _async_register_lovelace_resource(hass, version)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Failed to register Lovelace resource for %s", CARD_URL)
        domain_data.pop(_CARD_INSTALLED_KEY, None)


async def _async_register_lovelace_resource(hass: HomeAssistant, version: str) -> None:
    """Register card as Lovelace resource."""
    resource_url = f"{CARD_URL}?automatically-added&{version}"

    try:
        resources = hass.data["lovelace"].resources
    except (KeyError, AttributeError):
        _LOGGER.info(
            "Could not auto-register Lovelace resource. "
            "Add manually: url: %s?%s, type: module",
            CARD_URL,
            version,
        )
        return

    if not resources.loaded:
        await resources.async_load()

    existing = [r for r in resources.async_items() if CARD_FILENAME in r.get("url", "")]

    if not existing:
        # Only storage-mode resources can be created programmatically; YAML-mode
        # resources are read-only config the user must edit themselves.
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_create_item(
                {"res_type": "module", "url": resource_url}
            )
            _LOGGER.info("Registered %s as Lovelace resource", resource_url)
        else:
            _LOGGER.info(
                "Lovelace in YAML resource mode. "
                "Add manually: url: %s?%s, type: module",
                CARD_URL,
                version,
            )
        return

    # Remove duplicates — keep only the first, update it to current version
    for r in existing[1:]:
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_delete_item(r["id"])
            _LOGGER.info("Removed duplicate Lovelace resource %s", r["url"])

    first = existing[0]
    if first.get("url") != resource_url:
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_update_item(
                first["id"], {"res_type": "module", "url": resource_url}
            )
            _LOGGER.info("Updated Lovelace resource to %s", resource_url)
        else:
            first["url"] = resource_url


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and e.entry_id != entry.entry_id
    ]
    if not remaining:
        hass.data.get(DOMAIN, {}).pop(_CARD_INSTALLED_KEY, None)

    return unload_ok
