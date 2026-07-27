"""E.ON Sweden integration setup."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EonApiClient
from .const import CONF_FACILITY_IDS, CONF_PASSWORD, CONF_PERSONNUMMER, DOMAIN
from .coordinator import EonCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up E.ON Sweden from a config entry."""
    personnummer: str = entry.data[CONF_PERSONNUMMER]
    password: str = entry.data[CONF_PASSWORD]
    facility_filter: list[str] = entry.data.get(CONF_FACILITY_IDS, [])

    session = async_create_clientsession(hass)
    client = EonApiClient(personnummer, password, session)

    coordinator = EonCoordinator(hass, client, facility_filter)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
