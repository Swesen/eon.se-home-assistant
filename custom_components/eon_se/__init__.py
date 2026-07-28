"""E.ON Sweden integration setup."""
from __future__ import annotations

import logging
import time

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import config_validation as cv

from .api import EonApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_FACILITY_IDS,
    CONF_PASSWORD,
    CONF_PERSONNUMMER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from .coordinator import EonCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_PUSH_TOKENS = "push_tokens"
SERVICE_PUSH_TOKENS_SCHEMA = vol.Schema(
    {
        vol.Required("access_token"): cv.string,
        vol.Required("refresh_token"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up E.ON Sweden from a config entry."""

    personnummer: str = entry.data[CONF_PERSONNUMMER]
    password: str = entry.data[CONF_PASSWORD]
    facility_filter: list[str] = entry.data.get(CONF_FACILITY_IDS, [])

    session = async_create_clientsession(hass)
    client = EonApiClient(personnummer, password, session)

    # Restore saved tokens so we don't need Playwright on every HA restart.
    saved_access = entry.data.get(CONF_ACCESS_TOKEN, "")
    saved_refresh = entry.data.get(CONF_REFRESH_TOKEN, "")
    if saved_access:
        client._bearer_token = saved_access
        client._refresh_token = saved_refresh or None
        client._token_expires_at = time.monotonic()  # force refresh on first call
        _LOGGER.debug("Restored saved tokens for %s", personnummer)

    async def _persist_tokens() -> None:
        """Write refreshed tokens back into the config entry so they survive HA restarts."""
        if client._bearer_token and client._bearer_token != entry.data.get(CONF_ACCESS_TOKEN):
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: client._bearer_token,
                    CONF_REFRESH_TOKEN: client._refresh_token or "",
                },
            )
            _LOGGER.debug("Persisted refreshed tokens to config entry")

    client._on_token_refresh = _persist_tokens

    coordinator = EonCoordinator(hass, client, facility_filter)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the push_tokens service (called by get_tokens.py --push)
    async def handle_push_tokens(call: ServiceCall) -> None:
        """Accept fresh tokens pushed from get_tokens.py running on a PC."""
        new_access = call.data["access_token"]
        new_refresh = call.data["refresh_token"]
        client._bearer_token = new_access
        client._refresh_token = new_refresh
        client._token_expires_at = time.monotonic() + 840  # 14 min
        await _persist_tokens()
        await coordinator.async_request_refresh()
        _LOGGER.info("Tokens updated via push_tokens service call")

    if not hass.services.has_service(DOMAIN, SERVICE_PUSH_TOKENS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PUSH_TOKENS,
            handle_push_tokens,
            schema=SERVICE_PUSH_TOKENS_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
