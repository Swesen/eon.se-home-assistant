"""Config flow for E.ON Sweden integration."""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, parse_qs, urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EonApiClient, EonApiError, EonAuthError, _pkce_pair
from .const import (
    ADDON_AUTH_DEFAULT_URL,
    CONF_ACCESS_TOKEN,
    CONF_ADDON_URL,
    CONF_FACILITY_IDS,
    CONF_PASSWORD,
    CONF_PERSONNUMMER,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
    HAAPI_AUTHZ_URL,
    MANUFACTURER,
    OAUTH_ACR,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)


def _build_auth_url(code_challenge: str) -> str:
    params = urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "acr": OAUTH_ACR,
        "prompt": "login",
    })
    return f"{HAAPI_AUTHZ_URL}?{params}"


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
    session: aiohttp.ClientSession,
    hass: Any | None = None,
) -> str | None:
    """Discover the add-on URL using HA's hassio helper, then direct probes."""
    # --- Method 1: HA hassio integration (works on HA OS, no token needed) ---
    if hass is not None:
        try:
            from homeassistant.components.hassio import async_get_addon_info, is_hassio
            if is_hassio(hass):
                try:
                    info = await async_get_addon_info(hass, "eon_se_auth")
                    if info and info.get("state") == "started":
                        ip = info.get("ip_address", "")
                        if ip and ip != "0.0.0.0":
                            url = f"http://{ip}:8099"
                            if await _probe_addon(session, url):
                                _LOGGER.debug("Add-on found via hassio helper at %s", url)
                                return url
                except Exception as err:
                    _LOGGER.debug("hassio addon info failed: %s", err)
        except ImportError:
            pass

    # --- Method 2: Direct probes at well-known addresses ---
    for candidate in (
        "http://172.30.33.2:8099",       # common HA OS add-on IP
        ADDON_AUTH_DEFAULT_URL,           # http://homeassistant.local:8099
        "http://homeassistant:8099",
    ):
        if await _probe_addon(session, candidate):
            _LOGGER.debug("Add-on found at %s", candidate)
            return candidate

    return None


class EonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON Sweden."""

    VERSION = 1

    def __init__(self) -> None:
        self._personnummer: str = ""
        self._password: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._addon_url: str = ""
        self._facilities: list[dict[str, Any]] = []
        self._code_verifier: str = ""
        self._auth_url: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: enter credentials and optional add-on URL."""
        errors: dict[str, str] = {}

        # Discover the add-on URL (Supervisor API → direct probe fallback)
        session = async_create_clientsession(self.hass)
        detected_addon_url = await _discover_addon_url(session, self.hass)
        addon_available = detected_addon_url is not None
        _LOGGER.debug("Config flow: detected_addon_url=%r", detected_addon_url)

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]
            addon_url = user_input.get(CONF_ADDON_URL, "").strip() or detected_addon_url or ""

            await self.async_set_unique_id(personnummer)
            self._abort_if_unique_id_configured()

            client = EonApiClient(personnummer, password, session, addon_url=addon_url or None)

            try:
                await client.authenticate()
                facilities = await client.get_facilities()
            except EonAuthError:
                errors["base"] = "invalid_auth"
            except EonApiError as err:
                _LOGGER.warning(
                    "E.ON API error during setup (addon_url=%r): %s",
                    addon_url, err,
                )
                err_str = str(err).lower()
                if "cannot reach auth add-on" in err_str:
                    # Add-on URL was set but HTTP connection failed
                    errors[CONF_ADDON_URL] = "addon_unreachable"
                elif "add-on returned http" in err_str or "browser auth failed" in err_str or "browser auth timed out" in err_str:
                    # Add-on was reached but Playwright/auth inside it failed
                    errors["base"] = "auth_failed"
                elif "playwright" in err_str or "subprocess" in err_str or "no output" in err_str:
                    # Local Playwright attempted (no add-on) and failed
                    errors["base"] = "playwright_missing" if not addon_url else "auth_failed"
                elif "token exchange" in err_str:
                    errors["base"] = "cannot_connect"
                else:
                    # Catch-all: no add-on was configured/found
                    errors["base"] = "addon_not_running" if not addon_url else "unknown"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                self._personnummer = personnummer
                self._password = password
                self._access_token = client._bearer_token or ""
                self._refresh_token = client._refresh_token or ""
                self._addon_url = addon_url
                self._facilities = facilities

                if len(facilities) <= 1:
                    return self._create_entry()
                return await self.async_step_facilities()

        # Build the schema — show add-on field pre-filled if detected
        schema = vol.Schema({
            vol.Required(CONF_PERSONNUMMER): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(
                CONF_ADDON_URL,
                default=detected_addon_url or "",
            ): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "addon_status": (
                    "✅ E.ON Auth add-on detected and ready."
                    if addon_available
                    else "⚠️ E.ON Auth add-on not detected. Install it for fully automatic login. "
                         "Without it, use the browser login method below."
                ),
            },
        )

    async def async_step_browser_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Fallback: user logs in via their own browser and pastes the code."""
        errors: dict[str, str] = {}

        if not self._code_verifier:
            self._code_verifier, code_challenge = _pkce_pair()
            self._auth_url = _build_auth_url(code_challenge)

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]
            raw = user_input["auth_code"].strip()
            auth_code = _extract_code(raw)

            if not auth_code:
                errors["auth_code"] = "invalid_code"
            else:
                await self.async_set_unique_id(personnummer)
                self._abort_if_unique_id_configured()

                session = async_create_clientsession(self.hass)
                client = EonApiClient(personnummer, password, session)

                try:
                    token_resp = await client._exchange_code(auth_code, self._code_verifier)
                    client._store_token_response(token_resp)
                    facilities = await client.get_facilities()
                except EonAuthError:
                    errors["auth_code"] = "invalid_code"
                except EonApiError as err:
                    _LOGGER.warning("E.ON API error during browser login: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during browser login")
                    errors["base"] = "unknown"
                else:
                    self._personnummer = personnummer
                    self._password = password
                    self._access_token = client._bearer_token or ""
                    self._refresh_token = client._refresh_token or ""
                    self._facilities = facilities

                    if len(facilities) <= 1:
                        return self._create_entry()
                    return await self.async_step_facilities()

        return self.async_show_form(
            step_id="browser_login",
            data_schema=vol.Schema({
                vol.Required(CONF_PERSONNUMMER): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required("auth_code"): str,
            }),
            errors=errors,
            description_placeholders={"auth_url": self._auth_url},
        )

    async def async_step_facilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self._create_entry(facility_ids=user_input.get(CONF_FACILITY_IDS, []))

        facility_options: dict[str, str] = {}
        for f in self._facilities:
            fid = str(
                f.get("id") or f.get("anlaggningsId") or f.get("facilityId")
                or f.get("meteringPointId") or "unknown"
            )
            label = str(f.get("name") or f.get("namn") or f.get("address") or f.get("adress") or fid)
            facility_options[fid] = label

        return self.async_show_form(
            step_id="facilities",
            data_schema=vol.Schema({
                vol.Optional(CONF_FACILITY_IDS, default=list(facility_options.keys())): vol.All(
                    [vol.In(facility_options)],
                ),
            }),
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-authenticate when refresh token expires."""
        self._code_verifier = ""
        return await self.async_step_browser_login()

    def _create_entry(self, facility_ids: list[str] | None = None) -> FlowResult:
        data: dict[str, Any] = {
            CONF_PERSONNUMMER: self._personnummer,
            CONF_PASSWORD: self._password,
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
        }
        if self._addon_url:
            data[CONF_ADDON_URL] = self._addon_url
        if facility_ids:
            data[CONF_FACILITY_IDS] = facility_ids

        return self.async_create_entry(
            title=f"{MANUFACTURER} ({self._personnummer})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> EonOptionsFlow:
        return EonOptionsFlow(config_entry)


class EonOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_facilities: list[str] = self._config_entry.data.get(CONF_FACILITY_IDS, [])
        current_addon_url: str = self._config_entry.data.get(CONF_ADDON_URL, ADDON_AUTH_DEFAULT_URL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_ADDON_URL, default=current_addon_url): str,
                vol.Optional(CONF_FACILITY_IDS, default=current_facilities): vol.All(
                    [str], vol.Length(min=0)
                ),
            }),
        )


def _extract_code(value: str) -> str | None:
    if not value:
        return None
    if value.startswith("http"):
        try:
            qs = parse_qs(urlparse(value).query)
            codes = qs.get("code", [])
            return codes[0] if codes else None
        except Exception:
            return None
    return value if " " not in value else None


import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, parse_qs, urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EonApiClient, EonApiError, EonAuthError, _b64url, _pkce_pair
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_FACILITY_IDS,
    CONF_PASSWORD,
    CONF_PERSONNUMMER,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
    HAAPI_AUTHZ_URL,
    MANUFACTURER,
    OAUTH_ACR,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)


def _build_auth_url(code_challenge: str) -> str:
    """Build the eon.se authorization URL the user opens in their browser."""
    params = urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "acr": OAUTH_ACR,
        "prompt": "login",
    })
    return f"{HAAPI_AUTHZ_URL}?{params}"


class EonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON Sweden."""

    VERSION = 1

    def __init__(self) -> None:
        self._personnummer: str = ""
        self._password: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._facilities: list[dict[str, Any]] = []
        self._code_verifier: str = ""
        self._auth_url: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the two setup options."""
        if user_input is not None:
            if user_input["auth_method"] == "browser":
                return await self.async_step_browser_login()
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("auth_method", default="browser"): vol.In({
                    "browser": "Log in with your browser (recommended – no extra tools needed)",
                    "credentials": "Enter credentials directly (requires Playwright on this HA machine)",
                }),
            }),
        )

    async def async_step_browser_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Generate PKCE, show the auth URL, ask user to paste the code from the redirect URL."""
        errors: dict[str, str] = {}

        # Generate PKCE on first visit
        if not self._code_verifier:
            self._code_verifier, code_challenge = _pkce_pair()
            self._auth_url = _build_auth_url(code_challenge)

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]
            raw_code_input = user_input["auth_code"].strip()

            # Accept either the bare code or the full redirect URL
            auth_code = _extract_code(raw_code_input)
            if not auth_code:
                errors["auth_code"] = "invalid_code"
            else:
                await self.async_set_unique_id(personnummer)
                self._abort_if_unique_id_configured()

                session = async_create_clientsession(self.hass)
                client = EonApiClient(personnummer, password, session)

                try:
                    token_resp = await client._exchange_code(auth_code, self._code_verifier)
                    client._store_token_response(token_resp)
                    facilities = await client.get_facilities()
                except EonAuthError:
                    errors["auth_code"] = "invalid_code"
                except EonApiError as err:
                    _LOGGER.warning("E.ON API error during browser login: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during browser login")
                    errors["base"] = "unknown"
                else:
                    self._personnummer = personnummer
                    self._password = password
                    self._access_token = client._bearer_token or ""
                    self._refresh_token = client._refresh_token or ""
                    self._facilities = facilities

                    if len(facilities) <= 1:
                        return self._create_entry()
                    return await self.async_step_facilities()

        return self.async_show_form(
            step_id="browser_login",
            data_schema=vol.Schema({
                vol.Required(CONF_PERSONNUMMER): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required("auth_code"): str,
            }),
            errors=errors,
            description_placeholders={"auth_url": self._auth_url},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Authenticate via Playwright browser subprocess (requires Playwright on HA machine)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(personnummer)
            self._abort_if_unique_id_configured()

            session = async_create_clientsession(self.hass)
            client = EonApiClient(personnummer, password, session)

            try:
                await client.authenticate()
                facilities = await client.get_facilities()
            except EonAuthError:
                errors["base"] = "invalid_auth"
            except EonApiError as err:
                _LOGGER.warning("E.ON API error during setup: %s", err)
                if "playwright" in str(err).lower() or "subprocess" in str(err).lower():
                    errors["base"] = "playwright_missing"
                else:
                    errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                self._personnummer = personnummer
                self._password = password
                self._access_token = client._bearer_token or ""
                self._refresh_token = client._refresh_token or ""
                self._facilities = facilities

                if len(facilities) <= 1:
                    return self._create_entry()
                return await self.async_step_facilities()

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({
                vol.Required(CONF_PERSONNUMMER): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_facilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user select which facilities to include."""
        if user_input is not None:
            return self._create_entry(facility_ids=user_input.get(CONF_FACILITY_IDS, []))

        facility_options: dict[str, str] = {}
        for f in self._facilities:
            fid = str(
                f.get("id") or f.get("anlaggningsId") or f.get("facilityId")
                or f.get("meteringPointId") or "unknown"
            )
            label = str(f.get("name") or f.get("namn") or f.get("address") or f.get("adress") or fid)
            facility_options[fid] = label

        return self.async_show_form(
            step_id="facilities",
            data_schema=vol.Schema({
                vol.Optional(CONF_FACILITY_IDS, default=list(facility_options.keys())): vol.All(
                    [vol.In(facility_options)],
                ),
            }),
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-authenticate when the refresh token has expired."""
        # Reset PKCE so a fresh URL is generated
        self._code_verifier = ""
        self._personnummer = self._config_entry.data.get(CONF_PERSONNUMMER, "")  # type: ignore[attr-defined]
        self._password = self._config_entry.data.get(CONF_PASSWORD, "")  # type: ignore[attr-defined]
        return await self.async_step_browser_login()

    def _create_entry(self, facility_ids: list[str] | None = None) -> FlowResult:
        data: dict[str, Any] = {
            CONF_PERSONNUMMER: self._personnummer,
            CONF_PASSWORD: self._password,
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
        }
        if facility_ids:
            data[CONF_FACILITY_IDS] = facility_ids

        return self.async_create_entry(
            title=f"{MANUFACTURER} ({self._personnummer})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> EonOptionsFlow:
        return EonOptionsFlow(config_entry)


class EonOptionsFlow(config_entries.OptionsFlow):
    """Handle options after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_facilities: list[str] = self._config_entry.data.get(CONF_FACILITY_IDS, [])
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_FACILITY_IDS, default=current_facilities): vol.All(
                    [str], vol.Length(min=0)
                ),
            }),
        )


def _extract_code(value: str) -> str | None:
    """Extract the auth code from either a bare code string or a full redirect URL."""
    if not value:
        return None
    # Try parsing as URL first (user pasted the full redirect URL)
    if value.startswith("http"):
        try:
            parsed = urlparse(value)
            qs = parse_qs(parsed.query)
            codes = qs.get("code", [])
            return codes[0] if codes else None
        except Exception:
            return None
    # Bare code — sanity check: must be non-empty and not contain spaces
    return value if " " not in value else None


import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EonApiClient, EonApiError, EonAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_FACILITY_IDS,
    CONF_PASSWORD,
    CONF_PERSONNUMMER,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
    MANUFACTURER,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PERSONNUMMER): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_METHOD_SCHEMA = vol.Schema(
    {
        vol.Required("auth_method", default="credentials"): vol.In(
            ["credentials", "paste_tokens"]
        ),
    }
)

STEP_TOKENS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PERSONNUMMER): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_ACCESS_TOKEN): str,
        vol.Required(CONF_REFRESH_TOKEN): str,
    }
)


class EonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON Sweden."""

    VERSION = 1

    def __init__(self) -> None:
        self._personnummer: str = ""
        self._password: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._facilities: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: choose auth method — browser on this machine, or paste tokens from PC."""
        if user_input is not None:
            if user_input["auth_method"] == "paste_tokens":
                return await self.async_step_tokens()
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_METHOD_SCHEMA,
            description_placeholders={},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Authenticate via Playwright browser (requires Playwright installed on this machine)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(personnummer)
            self._abort_if_unique_id_configured()

            session = async_create_clientsession(self.hass)
            client = EonApiClient(personnummer, password, session)

            try:
                await client.authenticate()
                facilities = await client.get_facilities()
            except EonAuthError:
                errors["base"] = "invalid_auth"
            except EonApiError as err:
                _LOGGER.warning("E.ON API error during setup: %s", err)
                if "playwright" in str(err).lower() or "subprocess" in str(err).lower():
                    errors["base"] = "playwright_missing"
                else:
                    errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                self._personnummer = personnummer
                self._password = password
                self._access_token = client._bearer_token or ""
                self._refresh_token = client._refresh_token or ""
                self._facilities = facilities

                if len(facilities) <= 1:
                    return self._create_entry()
                return await self.async_step_facilities()

        return self.async_show_form(
            step_id="credentials",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_tokens(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Accept pre-obtained tokens from the get_tokens.py helper script run on a PC."""
        errors: dict[str, str] = {}

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]
            access_token = user_input[CONF_ACCESS_TOKEN].strip()
            refresh_token = user_input[CONF_REFRESH_TOKEN].strip()

            await self.async_set_unique_id(personnummer)
            self._abort_if_unique_id_configured()

            # Verify the token works by hitting the consumption API
            session = async_create_clientsession(self.hass)
            client = EonApiClient(personnummer, password, session)
            client._bearer_token = access_token
            client._refresh_token = refresh_token
            client._token_expires_at = time.monotonic() + 840  # assume ~14 min left

            try:
                facilities = await client.get_facilities()
            except EonAuthError:
                errors["base"] = "invalid_auth"
            except EonApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating tokens")
                errors["base"] = "unknown"
            else:
                self._personnummer = personnummer
                self._password = password
                self._access_token = access_token
                self._refresh_token = refresh_token
                self._facilities = facilities

                if len(facilities) <= 1:
                    return self._create_entry()
                return await self.async_step_facilities()

        return self.async_show_form(
            step_id="tokens",
            data_schema=STEP_TOKENS_SCHEMA,
            errors=errors,
            description_placeholders={},
        )

    async def async_step_facilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step (optional): let the user select which facilities to include."""
        if user_input is not None:
            selected: list[str] = user_input.get(CONF_FACILITY_IDS, [])
            return self._create_entry(facility_ids=selected)

        facility_options: dict[str, str] = {}
        for f in self._facilities:
            fid = str(
                f.get("id") or f.get("anlaggningsId") or f.get("facilityId")
                or f.get("meteringPointId") or "unknown"
            )
            label = str(
                f.get("name") or f.get("namn") or f.get("address") or f.get("adress") or fid
            )
            facility_options[fid] = label

        schema = vol.Schema(
            {
                vol.Optional(CONF_FACILITY_IDS, default=list(facility_options.keys())): vol.All(
                    [vol.In(facility_options)],
                ),
            }
        )
        return self.async_show_form(step_id="facilities", data_schema=schema)

    def _create_entry(self, facility_ids: list[str] | None = None) -> FlowResult:
        data: dict[str, Any] = {
            CONF_PERSONNUMMER: self._personnummer,
            CONF_PASSWORD: self._password,
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
        }
        if facility_ids:
            data[CONF_FACILITY_IDS] = facility_ids

        return self.async_create_entry(
            title=f"{MANUFACTURER} ({self._personnummer})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EonOptionsFlow:
        return EonOptionsFlow(config_entry)


class EonOptionsFlow(config_entries.OptionsFlow):
    """Handle options after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_facilities: list[str] = self._config_entry.data.get(CONF_FACILITY_IDS, [])
        schema = vol.Schema(
            {
                vol.Optional(CONF_FACILITY_IDS, default=current_facilities): vol.All(
                    [str], vol.Length(min=0)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EonApiClient, EonApiError, EonAuthError
from .const import (
    CONF_FACILITY_IDS,
    CONF_PASSWORD,
    CONF_PERSONNUMMER,
    DOMAIN,
    MANUFACTURER,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PERSONNUMMER): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class EonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON Sweden."""

    VERSION = 1

    def __init__(self) -> None:
        self._personnummer: str = ""
        self._password: str = ""
        self._facilities: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: ask for personnummer + password and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            personnummer = user_input[CONF_PERSONNUMMER].strip()
            password = user_input[CONF_PASSWORD]

            # Prevent duplicate entries for the same account
            await self.async_set_unique_id(personnummer)
            self._abort_if_unique_id_configured()

            session = async_create_clientsession(self.hass)
            client = EonApiClient(personnummer, password, session)

            try:
                await client.authenticate()
                facilities = await client.get_facilities()
            except EonAuthError:
                errors["base"] = "invalid_auth"
            except EonApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                self._personnummer = personnummer
                self._password = password
                self._facilities = facilities

                if len(facilities) <= 1:
                    # Only one (or zero) facility – skip the selection step
                    return self._create_entry()

                # Multiple facilities – let the user choose which ones to include
                return await self.async_step_facilities()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_facilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2 (optional): let the user select which facilities to include."""
        if user_input is not None:
            selected: list[str] = user_input.get(CONF_FACILITY_IDS, [])
            return self._create_entry(facility_ids=selected)

        # Build a dict of {facility_id: display_label} for the multi-select
        facility_options: dict[str, str] = {}
        for f in self._facilities:
            fid = str(
                f.get("id")
                or f.get("anlaggningsId")
                or f.get("facilityId")
                or f.get("meteringPointId")
                or "unknown"
            )
            label = str(
                f.get("name") or f.get("namn") or f.get("address") or f.get("adress") or fid
            )
            facility_options[fid] = label

        schema = vol.Schema(
            {
                vol.Optional(CONF_FACILITY_IDS, default=list(facility_options.keys())): vol.All(
                    [vol.In(facility_options)],
                ),
            }
        )
        return self.async_show_form(
            step_id="facilities",
            data_schema=schema,
        )

    def _create_entry(self, facility_ids: list[str] | None = None) -> FlowResult:
        data: dict[str, Any] = {
            CONF_PERSONNUMMER: self._personnummer,
            CONF_PASSWORD: self._password,
        }
        if facility_ids:
            data[CONF_FACILITY_IDS] = facility_ids

        return self.async_create_entry(
            title=f"{MANUFACTURER} ({self._personnummer})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EonOptionsFlow:
        return EonOptionsFlow(config_entry)


class EonOptionsFlow(config_entries.OptionsFlow):
    """Handle options (e.g. change facility filter) after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_facilities: list[str] = self._config_entry.data.get(CONF_FACILITY_IDS, [])

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FACILITY_IDS,
                    default=current_facilities,
                ): vol.All(
                    [str],
                    vol.Length(min=0),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
