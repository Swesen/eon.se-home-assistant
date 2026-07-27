"""Config flow for E.ON Sweden integration."""
from __future__ import annotations

import logging
from typing import Any

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
