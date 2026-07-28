"""E.ON Sweden integration setup."""
from __future__ import annotations

import logging
import time

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import config_validation as cv

from .api import EonApiClient
from .const import (
    ADDON_AUTH_DEFAULT_URL,
    CONF_ACCESS_TOKEN,
    CONF_ADDON_URL,
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


async def _probe_addon(session: aiohttp.ClientSession, url: str) -> bool:
    """Return True if the E.ON Auth add-on is reachable at the given URL."""
    try:
        async with session.get(
            f"{url.rstrip('/')}/health",
            timeout=aiohttp.ClientTimeout(total=4),
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _discover_addon_url(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
) -> str | None:
    """Discover the add-on URL using HA's hassio helper, then direct probes."""
    # --- Method 1: HA hassio integration (works on HA OS, no token needed) ---
    try:
        from homeassistant.components.hassio import async_get_addon_info, is_hassio
        if is_hassio(hass):
            # Try both the short slug and with common repo prefixes
            for slug in ("eon_se_auth",):
                try:
                    info = await async_get_addon_info(hass, slug)
                    if info and info.get("state") == "started":
                        ip = info.get("ip_address", "")
                        if ip and ip != "0.0.0.0":
                            url = f"http://{ip}:8099"
                            if await _probe_addon(session, url):
                                _LOGGER.debug("Add-on found via hassio helper at %s", url)
                                return url
                except Exception as err:
                    _LOGGER.debug("hassio addon info failed for slug %r: %s", slug, err)
    except ImportError:
        pass

    # --- Method 2: Supervisor REST API via HA's hassio proxy ---
    try:
        from homeassistant.components.hassio import async_create_session
        hassio_session = async_create_session(hass)
        async with hassio_session.get(
            "http://hassio/addons/eon_se_auth/info",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                ip = data.get("data", {}).get("ip_address", "")
                if ip and ip != "0.0.0.0":
                    url = f"http://{ip}:8099"
                    if await _probe_addon(session, url):
                        _LOGGER.debug("Add-on found via hassio proxy at %s", url)
                        return url
    except Exception as err:
        _LOGGER.debug("hassio proxy probe failed: %s", err)

    # --- Method 3: Direct probes at well-known addresses ---
    for candidate in (
        "http://172.30.33.2:8099",       # common HA OS add-on IP
        ADDON_AUTH_DEFAULT_URL,           # http://homeassistant.local:8099
        "http://homeassistant:8099",
    ):
        if await _probe_addon(session, candidate):
            _LOGGER.debug("Add-on found at %s", candidate)
            return candidate

    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up E.ON Sweden from a config entry."""

    personnummer: str = entry.data[CONF_PERSONNUMMER]
    password: str = entry.data[CONF_PASSWORD]
    facility_filter: list[str] = entry.data.get(CONF_FACILITY_IDS, [])

    session = async_create_clientsession(hass)

    # Resolve the auth add-on URL — stored in config, or discover via Supervisor API
    addon_url: str | None = entry.data.get(CONF_ADDON_URL) or None
    if not addon_url:
        addon_url = await _discover_addon_url(hass, async_create_clientsession(hass))
        if addon_url:
            _LOGGER.info("Auto-detected E.ON Auth add-on at %s", addon_url)

    client = EonApiClient(personnummer, password, session, addon_url=addon_url)

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
