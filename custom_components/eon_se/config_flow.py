"""Config flow for E.ON Sweden integration."""
from __future__ import annotations

import logging
import time
from typing import Any

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
