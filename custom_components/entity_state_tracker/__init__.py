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
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .coordinator import EntityStateTrackerCoordinator
from .storage import EntityStateTrackerStore

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
    await _async_purge_orphaned_storage(hass)
    return True


def disable_orphaned_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: str,
    valid_unique_ids: set[str],
) -> None:
    """Disable this entry's registry entities no longer backed by a live entity.

    Platforms only ever ADD entities; when ``enabled_frames`` shrinks (a frame
    toggled off, compliance/threshold removed, or an upgrade narrowing the
    default set), the dropped frame's per-frame sensors keep their entity
    registry entries and HA strands them as ``restored``/``unavailable`` forever.
    Each platform's ``async_setup_entry`` passes the unique_ids it is about to
    add (so this stays in lockstep with setup — no re-derived config predicate).
    MUST be called AFTER ``async_add_entities`` so the just-added entities are
    the valid set. This relies on HA instantiating the entities synchronously
    before control returns here: ``async_add_entities`` schedules an eager task
    that runs sync up to its first real suspension, and with
    ``update_before_add=False`` (the default) the registry write happens before
    any such suspension — so the just-added unique_ids are already registered
    when this runs. If a future HA defers the registry write past a suspension,
    or a platform switches to ``update_before_add=True``, this ordering breaks;
    the live smoke tests (EC41/EC44, asserting ``disabled_by`` against a running
    HA) are the backstop that would catch it.

    We DISABLE the orphan (``disabled_by = INTEGRATION``), never ``async_remove``
    it: removal fires ``entity_registry_updated`` → the recorder purges the
    entity's states AND long-term statistics, so a reversible frame toggle would
    irreversibly destroy the user's history. Disabling clears the stranded
    ``unavailable`` state from the dashboard while preserving history, and the
    entity comes back intact when the frame is re-enabled (setup re-adds it →
    its unique_id is valid again → re-enabled below). An entry already disabled
    BY THE USER is left untouched — that is their explicit choice, not an orphan.
    Conversely, an entity we previously disabled whose frame is enabled again is
    re-enabled so it starts producing without the user hunting through the UI.
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain != platform:
            continue
        is_valid = reg_entry.unique_id in valid_unique_ids
        if not is_valid and reg_entry.disabled_by is None:
            # Newly orphaned → disable (non-destructive; history kept).
            registry.async_update_entity(
                reg_entry.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
        elif is_valid and reg_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            # Frame re-enabled and we were the one who disabled it → restore.
            # NOTE: clearing disabled_by on an entity of an unload-capable entry
            # makes HA schedule a one-shot config-entry reload ~30s later
            # (RELOAD_AFTER_UPDATE_DELAY). That's benign and converges (setup is
            # idempotent) — the entity is back immediately; the delayed reload
            # just re-runs setup once. The disable direction does NOT trigger it.
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)


async def _async_purge_orphaned_storage(hass: HomeAssistant) -> None:
    """Delete .storage ledger files for entry IDs that no longer exist.

    Versions ≤ 0.1.6 lacked async_remove_entry, so deleted trackers left their
    storage files behind. This one-shot scan on integration load cleans them up.
    """
    storage_dir = Path(hass.config.path(".storage"))
    known_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}
    prefix = "entity_state_tracker."

    def _scan() -> list[Path]:
        if not storage_dir.is_dir():
            return []
        return [
            p
            for p in storage_dir.iterdir()
            if p.name.startswith(prefix) and p.name[len(prefix) :] not in known_ids
        ]

    orphans = await hass.async_add_executor_job(_scan)
    for path in orphans:
        try:
            await hass.async_add_executor_job(path.unlink, True)  # missing_ok=True
            _LOGGER.info(
                "Entity State Tracker: removed orphaned storage file %s", path.name
            )
        except OSError as err:
            _LOGGER.warning(
                "Entity State Tracker: could not remove orphaned storage file %s: %s",
                path.name,
                err,
            )


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
    # Clear any frame-fail Repair issues BEFORE dropping the coordinator — an
    # issue active at teardown would otherwise linger with no backing entry.
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        coordinator.clear_all_frame_fail_issues()

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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the ledger file when the config entry is removed."""
    await EntityStateTrackerStore(hass, entry.entry_id).remove()
    _LOGGER.debug("Entity State Tracker: removed storage for entry %s", entry.entry_id)
