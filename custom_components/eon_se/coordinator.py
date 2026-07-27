"""DataUpdateCoordinator for E.ON Sweden."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EonApiClient, EonApiError, EonAuthError
from .const import (
    CONF_FACILITY_IDS,
    DATA_LOOKBACK_DAYS,
    DOMAIN,
    INTERVAL_DIRECTION,
    INTERVAL_KWH,
    INTERVAL_START,
    KEY_DATA_DELAY_HOURS,
    KEY_ENERGY_LAST_24H,
    KEY_ENERGY_YESTERDAY,
    KEY_FACILITY_ADDRESS,
    KEY_FACILITY_ID,
    KEY_FACILITY_NAME,
    KEY_INTERVALS,
    KEY_LATEST_INTERVAL_KWH,
    KEY_LATEST_POWER_W,
    KEY_LATEST_TIMESTAMP,
    UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


class EonCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches consumption data for all facilities.

    coordinator.data is a dict keyed by facility_id:
    {
        "735999114000386913": {
            "facility_id": "735999114000386913",
            "facility_name": "Mitt hem",
            "facility_address": "Exempelgatan 1",
            "intervals": [...],            # all parsed interval dicts
            "energy_yesterday": 5.87,      # kWh
            "energy_last_24h": 3.20,       # kWh
            "latest_power_w": 248.0,       # W  (derived from last 15-min slot)
            "latest_interval_kwh": 0.062,  # kWh of last slot
            "latest_timestamp": datetime,  # start of last slot
        },
        ...
    }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: EonApiClient,
        facility_filter: list[str] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._client = client
        self._facility_filter = facility_filter or []

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch updated data from E.ON Sweden."""
        try:
            # ensure_token() uses a cached token and only hits the browser for
            # the very first auth or after a refresh_token failure.
            await self._client.ensure_token()
            facilities = await self._client.get_facilities()
        except EonAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except EonApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

        # Apply optional facility filter
        if self._facility_filter:
            facilities = [
                f for f in facilities
                if _facility_id(f) in self._facility_filter
            ]
            if not facilities:
                _LOGGER.warning(
                    "No facilities matched the configured filter %s",
                    self._facility_filter,
                )

        result: dict[str, Any] = {}

        for facility in facilities:
            fid = _facility_id(facility)
            if not fid:
                _LOGGER.warning("Facility missing ID, skipping: %s", facility)
                continue

            try:
                facility_data = await self._fetch_facility_data(facility)
                result[fid] = facility_data
            except EonApiError as err:
                _LOGGER.error("Failed to fetch data for facility %s: %s", fid, err)
                # Keep previous data if available
                if self.data and fid in self.data:
                    result[fid] = self.data[fid]

        return result

    async def _fetch_facility_data(self, facility: dict[str, Any]) -> dict[str, Any]:
        """Download and process consumption data for a single facility."""
        fid = _facility_id(facility)
        fname = _facility_name(facility)
        faddr = _facility_address(facility)

        # E.ON data has ~12 h delay; fetch the last DATA_LOOKBACK_DAYS days
        # to ensure we get at least some recent data.
        today = date.today()
        from_date = today - timedelta(days=DATA_LOOKBACK_DAYS)

        intervals = await self._client.get_consumption(fid, from_date, today)
        # Mirror the browser behaviour of also posting to export-log (hourly view)
        await self._client.post_export_log(fid, from_date, today)

        # Only consider "Förbrukning" (consumption) direction
        consumption = [i for i in intervals if "förbrukning" in i[INTERVAL_DIRECTION].lower()]

        energy_yesterday = _sum_day(consumption, today - timedelta(days=1))
        energy_last_24h = _sum_last_n_hours(consumption, 24)

        latest = consumption[-1] if consumption else None
        latest_kwh = latest[INTERVAL_KWH] if latest else None
        # Convert kWh per 15-min slot → average power in Watts: P = E / t = kWh / 0.25h * 1000
        latest_power = round(latest_kwh * 4 * 1000, 1) if latest_kwh is not None else None
        latest_ts = latest[INTERVAL_START] if latest else None

        # How many hours ago is the latest data point? Useful for diagnostics.
        if latest_ts is not None:
            from datetime import timezone
            now = datetime.now(tz=latest_ts.tzinfo) if latest_ts.tzinfo else datetime.now()
            data_delay_hours = round((now - latest_ts).total_seconds() / 3600, 1)
        else:
            data_delay_hours = None

        return {
            KEY_FACILITY_ID: fid,
            KEY_FACILITY_NAME: fname,
            KEY_FACILITY_ADDRESS: faddr,
            KEY_INTERVALS: consumption,
            KEY_ENERGY_YESTERDAY: round(energy_yesterday, 4),
            KEY_ENERGY_LAST_24H: round(energy_last_24h, 4),
            KEY_LATEST_POWER_W: latest_power,
            KEY_LATEST_INTERVAL_KWH: latest_kwh,
            KEY_LATEST_TIMESTAMP: latest_ts,
            KEY_DATA_DELAY_HOURS: data_delay_hours,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _facility_id(f: dict[str, Any]) -> str:
    """Extract facility ID from a facility dict (adapt field names as needed)."""
    return str(
        f.get("id")
        or f.get("anlaggningsId")
        or f.get("facilityId")
        or f.get("meteringPointId")
        or ""
    )


def _facility_name(f: dict[str, Any]) -> str:
    return str(
        f.get("name")
        or f.get("namn")
        or f.get("alias")
        or f.get("id")
        or "Unknown facility"
    )


def _facility_address(f: dict[str, Any]) -> str:
    return str(
        f.get("address")
        or f.get("adress")
        or f.get("street")
        or ""
    )


def _sum_day(intervals: list[dict], target_date: date) -> float:
    return sum(
        i[INTERVAL_KWH]
        for i in intervals
        if i[INTERVAL_START].date() == target_date
    )


def _sum_last_n_hours(intervals: list[dict], hours: int) -> float:
    cutoff = datetime.now() - timedelta(hours=hours)
    return sum(
        i[INTERVAL_KWH]
        for i in intervals
        if i[INTERVAL_START] >= cutoff
    )
