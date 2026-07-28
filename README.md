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

> **Why no "Energy Today"?** E.ON data has a ~12-hour delay, so today's sensor
> would read zero all morning and only partially fill in by evening. Use
> **Energy Yesterday** (always complete) for the Energy Dashboard instead.

---

## Prerequisites — E.ON Sweden Auth Add-on

The E.ON login uses [Curity HAAPI](https://curity.io/product/token-service/haapi/)
with a browser-based WebAssembly attestation (CAT). A headless Chromium browser
is required **once** for the initial login. After that, the integration uses a
`refresh_token` over plain HTTP — no browser needed for ongoing use.

Because Home Assistant OS uses Alpine/musl Linux, Playwright cannot be installed
directly on it. The solution is the **E.ON Sweden Auth** add-on: a small Docker
container (Debian Bookworm) that runs a headless Chromium and exposes a local
HTTP endpoint the integration calls automatically.

### Installing the add-on

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add `https://github.com/Swesen/eon.se-home-assistant` and click **Add**.
3. Find **E.ON Sweden Auth** in the store and click **Install**.
   > The first install takes a few minutes — it downloads Chromium (~200 MB) and builds the container.
4. Go to the add-on's **Configuration** tab and fill in:
   - `personnummer` — your Swedish personal identity number
   - `password` — your E.ON account password
5. Start the add-on and confirm it shows **Running** in the **Info** tab.

The integration will automatically detect the running add-on during setup.

Once the add-on is running, proceed to install the integration below.

---

## Integration Installation

### Via HACS (recommended)

1. In Home Assistant open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/Swesen/eon.se-home-assistant` as type **Integration**.
3. Find **E.ON Sweden** in HACS and install it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration** and search for **E.ON Sweden**.
6. Enter your personnummer and password, then follow the setup wizard.

### Manual

1. Copy the `custom_components/eon_se` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **E.ON Sweden**.
4. Enter your personnummer and password.

---

## Data delay

E.ON meters report data with approximately **12 hours of delay**. This means:

- During the morning you will typically see data up to midnight.
- By early afternoon, the first intervals of today start to appear.
- The **"Latest Data Timestamp"** sensor shows exactly when the most recent data is from.
- The **"Data Delay"** sensor (enable in the HA UI) shows the delay in hours.

There is **no real-time API** — the 15-minute `Quarterly` resolution with its
~12-hour delay is the finest granularity E.ON exposes.

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

### Add-on not detected

If the config flow reports that the add-on was not found, verify:

- The **E.ON Sweden Auth** add-on is installed and **Running**.
- Port `8099` is not in use by another add-on.
- The add-on **Configuration** tab has `personnummer` and `password` filled in.

You can also enter the add-on URL manually in the integration setup (default:
`http://homeassistant.local:8099`).

### Re-authentication

If the refresh token expires (e.g. after a long HA outage), Home Assistant will
show a **"Re-authentication required"** notification. Click it and follow the
wizard — the add-on will obtain new tokens automatically.

---

## How it works

```
HA Integration ──► E.ON Sweden Auth Add-on (port 8099)
                        │
                        └─► Headless Chromium (Playwright)
                                │
                                └─► Curity HAAPI / WASM attestation
                                        │
                                        └─► OAuth2 code + PKCE verifier
                                                │
HA Integration ◄────────────────────────────────┘
     │
     └─► POST /token → access_token + refresh_token
             │
             ├─► GET /api/facilities  → facility list
             └─► POST /api/consumption_new → 15-min intervals
```

After the first login, the integration renews the `access_token` silently via
the `refresh_token` grant — the add-on and Chromium are only used when a brand
new full login is required.

---

## Privacy

Your credentials are stored in:
- The Home Assistant config entry (encrypted in `.storage/core.config_entries`).
- The add-on's `/data/options.json` on the HA host.

They are only used to authenticate with the E.ON Sweden web portal and are
never sent anywhere else.

The E.ON Sweden portal is a JavaScript single-page application. The integration
uses reverse-engineered API endpoints that **may change** when E.ON updates their
website. If the integration stops working, please open an issue on GitHub.
