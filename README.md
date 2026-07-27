# E.ON Sweden – Home Assistant Integration

A custom Home Assistant integration that fetches electricity consumption data
from [E.ON Sweden (eon.se)](https://www.eon.se/mitt-e-on/din-forbrukning#/).

## Features

- Logs in with your **personnummer** (Swedish personal identity number) and password
- Fetches **15-minute interval** consumption data from the E.ON portal
- Handles the ~12-hour data delay gracefully (looks back up to 2 days)
- Supports **multiple facilities** (anläggningar) under one account
- Uses `refresh_token` for seamless token renewal — browser only needed **once** at setup
- Exposes the following sensors **per facility**:

| Sensor | Unit | Description |
|--------|------|-------------|
| Energy Yesterday | kWh | Complete consumption the previous calendar day — use this in the Energy Dashboard |
| Energy Last 24 Hours | kWh | Rolling 24-hour consumption sum |
| Latest Average Power | W | Average power of the most recent 15-min slot |
| Latest 15-min Energy | kWh | Energy in the most recent 15-min slot |
| Latest Data Timestamp | — | Start time of the most recent available slot |
| Data Delay | h | Hours since the most recent data point *(disabled by default)* |

> **Why no "Energy Today"?** E.ON data has a ~12 hour delay, so today's sensor
> would read zero all morning and only fill in partially by evening. Use
> **Energy Yesterday** (always complete) for the Energy Dashboard instead.

---

## Installation

### Via HACS (recommended)

1. In Home Assistant open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/YOUR_USERNAME/eon-se-home-assistant` as type **Integration**.
3. Find **E.ON Sweden** in HACS and install it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration** and search for **E.ON Sweden**.
6. Enter your personnummer and password.

### Manual

1. Copy the `custom_components/eon_se` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **E.ON Sweden**.
4. Enter your personnummer and password.

---

## ⚠️ Playwright requirement (initial login only)

The E.ON login uses [Curity HAAPI](https://curity.io/product/token-service/haapi/)
with a browser-based WebAssembly attestation (CAT). A headless Chromium browser
is required **once** for the initial login. After that, the integration uses a
`refresh_token` over plain HTTP — no browser needed for ongoing use.

### Installing Playwright on Home Assistant OS

Via the **SSH & Terminal** add-on:

```bash
pip install playwright
playwright install chromium --with-deps
```

This downloads ~200 MB and only needs to be done once.

---

## Data delay

E.ON meters report data with approximately **12 hours of delay**. This means:

- During the morning you will typically see data up to midnight.
- By early afternoon, the first intervals of today start to appear.
- The **"Latest Data Timestamp"** sensor shows exactly when the most recent data is from.
- The **"Data Delay"** sensor (enable in the HA UI) shows the delay in hours.

There is **no real-time API** — the 15-minute `Quarterly` resolution with its
~12 h delay is the finest granularity E.ON exposes.

---

## Troubleshooting

Enable debug logging in `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.eon_se: debug
```

Check the Home Assistant log (`/config/home-assistant.log`) for messages
tagged `custom_components.eon_se`.

---

## Privacy

Your credentials are stored in the Home Assistant config entry (encrypted in
`.storage/core.config_entries`). They are only used to authenticate with the
E.ON Sweden web portal and are never sent anywhere else.

The E.ON Sweden portal is a JavaScript single-page application. The integration
uses reverse-engineered API endpoints that **may change** when E.ON updates their
website. If the integration fails to connect, you need to verify the current
endpoints using your browser's developer tools.

### How to find the real endpoints

#### 1. Find the login endpoint

1. Open **Chrome / Firefox DevTools** (`F12`) and go to the **Network** tab.
2. Filter by **Fetch/XHR**.
3. Navigate to `https://www.eon.se/login/privat/username#/` and log in.
4. Look for a `POST` request that contains your credentials.
   - Note the **Request URL** (e.g. something like `/api/auth/login`)
   - Note the **Request payload** field names (e.g. `username`, `password`, or
     `personnummer`, `pwd`, etc.)
5. Update `LOGIN_API_URL` in `const.py` and the `payload` dict in `api.py →
   authenticate()` to match.

#### 2. Find the facilities endpoint

1. While still logged in, navigate to
   `https://www.eon.se/mitt-e-on/din-forbrukning#/`
2. In DevTools Network tab, look for a `GET` request that returns a **JSON list
   of facilities / anläggningar**.
3. Update `FACILITIES_API_URL` in `const.py` and adapt the field-name extraction
   helpers (`_facility_id`, `_facility_name`, `_facility_address`) in
   `coordinator.py` to match the real JSON keys.

#### 3. Find the CSV export endpoint

1. On the consumption page, trigger a CSV export (click the export/download
   button for a date range with 15-min resolution).
2. In DevTools Network tab, find the request that triggers the file download.
   - Note the **URL**
   - Note the **query parameters** (date range, facility ID, resolution, etc.)
3. Update `CSV_EXPORT_URL` in `const.py` and the `params` dict in
   `api.py → get_consumption_csv()` to match.

### Helpful tip

In Chrome DevTools Network tab you can right-click any request and select
**"Copy as cURL"** or **"Copy as fetch"** to get a ready-made command you can
test in a terminal before updating the integration code.

---

## CSV format reference

The exported file uses semicolon (`;`) as delimiter and comma (`,`) as decimal
separator. It has two sections separated by a blank line:

```
Anläggnings-id;Tidpunkt för export;Energiprodukt;Starttidpunkt;Sluttidpunkt;Måttenhet
735999114000386913;2026-07-27 11:35:54;Aktiv energi;2026-07-26 00:00;2026-07-27 01:00;kWh

Starttidpunkt;Sluttidpunkt;Energiriktning;Kvalitet;Kvantitet
2026-07-26 00:00;2026-07-26 00:15:00;Förbrukning;Uppmätt;0,052
...
```

The `parse_csv()` method in `api.py` handles this format automatically.

---

## Data delay

E.ON meters report data with approximately **12 hours of delay**. This means:

- During the morning, you will typically see data up to midnight or the early
  hours of the current day.
- The **"Energy Today"** sensor may read `0.0` in the morning until the first
  intervals of today become available.
- The **"Latest Data Timestamp"** sensor shows exactly when the most recent
  available measurement is from.

---

## Troubleshooting

- Enable debug logging in `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.eon_se: debug
```

- Check the Home Assistant log (`/config/home-assistant.log`) for messages
  tagged `custom_components.eon_se`.

---

## Privacy

Your credentials are stored in the Home Assistant config entry (encrypted in
`.storage/core.config_entries`). They are only used to authenticate with the
E.ON Sweden web portal and are never sent anywhere else.
