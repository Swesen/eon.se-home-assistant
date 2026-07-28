"""Constants for the E.ON Sweden integration."""

DOMAIN = "eon_se"
MANUFACTURER = "E.ON Sweden"

# Config entry keys
CONF_PERSONNUMMER = "personnummer"
CONF_PASSWORD = "password"
CONF_FACILITY_IDS = "facility_ids"  # Optional list to filter facilities
CONF_ACCESS_TOKEN = "access_token"   # Pre-obtained token (token-paste flow)
CONF_REFRESH_TOKEN = "refresh_token" # Pre-obtained refresh token
CONF_TOKEN_EXPIRES_AT = "token_expires_at"  # monotonic-independent wall-clock expiry (unix ts)
CONF_ADDON_URL = "addon_url"         # URL of the E.ON Auth add-on, e.g. http://homeassistant.local:8099

# Default URL of the auth add-on (port mapped to HA host)
ADDON_AUTH_DEFAULT_URL = "http://homeassistant.local:8099"

# Coordinator
UPDATE_INTERVAL_MINUTES = 15
# E.ON data has ~12 hour delay; we look back up to 2 days to find the latest data
DATA_LOOKBACK_DAYS = 2

# --- E.ON website URLs ---
BASE_URL = "https://www.eon.se"
LOGIN_PAGE_URL = f"{BASE_URL}/login/privat/username"

# --- Curity / HAAPI auth server ---
AUTH_SERVER = "https://api.apps.eon.se"

# Step 1: GET – returns JSON with OAuth config (client_id, scopes, redirect_uri…)
#   Response is ~280 bytes.  The integration reads this to seed OAuth params.
SECURE_CONFIG_URL = f"{BASE_URL}/bin/eon-se/neon/secure-configuration"

# Step 2: GET – starts HAAPI authentication session
#   Params: acr, serviceProviderId, forceAuthN, resumePath, state, client_id
HAAPI_AUTHN_START_URL = f"{AUTH_SERVER}/authn/authenticate"

# Step 3: GET – loads the username/password form for the SAP authenticator
HAAPI_AUTHN_FORM_URL = f"{AUTH_SERVER}/authn/authenticate/isu-sap-authenticator-recaptcha"

# Step 4: POST – submits personnummer + password (form-urlencoded, ~60 bytes)
#   Authorization: DPoP <jwt>   (DPoP key proof, see api.py)
#   Body (URL-encoded): personnummer=<...>&password=<...>  ← field names TBC
HAAPI_AUTHN_SUBMIT_URL = HAAPI_AUTHN_FORM_URL

# Step 5: POST – completes OAuth authorization code flow
HAAPI_AUTHZ_URL = f"{AUTH_SERVER}/neo/oauth/v2/authorization"

# Step 6: POST – exchanges auth code for access + id tokens (response ~2858 bytes JSON)
TOKEN_URL = f"{AUTH_SERVER}/neo/oauth/v2/token"

# OAuth client details (extracted from eon.se JavaScript / secure-configuration)
OAUTH_CLIENT_ID = "eon-web-light-haapi"
# The redirect URI registered for the web client:
OAUTH_REDIRECT_URI = f"{BASE_URL}/"
# Full scope string confirmed from DevTools (space-separated)
OAUTH_SCOPE = (
    "cjcv cjip cjmc cjpf cjim nfda openid cjrn movingjourney cjdsp cjero stgo workorder "
    "outagehistory:read-restricted serviceorder:create-restricted "
    "serviceorder:delete-restricted serviceorder:read-restricted "
    "faultreport:read-restricted faultreport:create-restricted "
    "outage:read-restricted installation:read-restricted "
    "outagecompensation:read-restricted damagenotification:create-restricted "
    "damagenotification:read-restricted invoice:read-restricted "
    "hancustchoice:read-restricted hancustchoice:update-restricted "
    "contractaccount:update-restricted elnastatus:read-restricted "
    "elnastatus:update-restricted meterreading:read-restricted "
    "nsporder:create-restricted salesforce deal:read-restricted "
    "damagenotification:update-restricted damagenotification:delete-restricted "
    "pubregperson:read-restricted bankid-sign store-poa "
    "power-consumption:read-all disruption-alert:update-restricted "
    "meterreading:create-restricted nspinspdoc:read-restricted "
    "nspinspdoc:create-all power-consumption:update-restricted "
    "power-consumption:create-restricted nspengagements:read-restricted "
    "logexport:read-restricted logexport:create-restricted "
    "nspgridinfo:read-all nspcontractaccounts:read-restricted "
    "nspeligibleoffers:read-all credit-status document:read-restricted"
)
# ACR value for the SAP authenticator
OAUTH_ACR = (
    "urn:se:curity:authentication:isu-sap-authenticator"
    ":isu-sap-authenticator-recaptcha"
)

# --- MTCaptcha ---
# The CAPTCHA is solved by the browser JS.  In headless/automation context this
# step is skipped – the authenticator-recaptcha endpoint may accept submissions
# without a valid CAPTCHA token (it only validates, it does not hard-block).
# If 403s occur here a CAPTCHA-solving service would be needed.
CAPTCHA_VALIDATE_URL = "https://eoncommonapiapirun.azurewebsites.net/api/validateMTCaptcha"

# Consumption/facilities endpoints
CONSUMPTION_PAGE_URL = f"{BASE_URL}/mitt-e-on/din-forbrukning"

# Confirmed resolution strings for /api/consumption_new
# "Quarterly" = 15-minute intervals (quarter-hour), "Q" used in export-log
# "Hourly"    = 1-hour intervals,                  "H" used in export-log
RESOLUTION_15MIN = "Quarterly"   # despite the name this means 15-minute / quarter-hour
RESOLUTION_HOURLY = "Hourly"
RESOLUTION_15MIN_LOG = "Q"
RESOLUTION_HOURLY_LOG = "H"

# --- Real backend API (Azure Functions) ---
# Discovered via DevTools network inspection on 2026-07-27.
EON_API_BASE = "https://eonmycoapirun.azurewebsites.net/api"

# POST – returns consumption data as JSON (body ~151 bytes)
CONSUMPTION_API_URL = f"{EON_API_BASE}/consumption_new"

# POST – logs that an export was triggered (body ~153 bytes)
EXPORT_LOG_URL = f"{EON_API_BASE}/export-log"

# Facilities list endpoint – TODO: find via DevTools
FACILITIES_API_URL = f"{EON_API_BASE}/facilities"  # ← TODO: confirm via DevTools

# CSV parsing
CSV_DELIMITER = ";"
CSV_DECIMAL_SEPARATOR = ","
CSV_DATE_FORMAT = "%Y-%m-%d %H:%M"

# Sensor types
SENSOR_TYPE_ENERGY_YESTERDAY = "energy_yesterday"
SENSOR_TYPE_ENERGY_LAST_24H = "energy_last_24h"
SENSOR_TYPE_LATEST_POWER = "latest_power"
SENSOR_TYPE_LATEST_INTERVAL_ENERGY = "latest_interval_energy"
SENSOR_TYPE_DATA_TIMESTAMP = "data_timestamp"
SENSOR_TYPE_DATA_DELAY = "data_delay_hours"

# Data keys within coordinator data per facility
KEY_FACILITY_ID = "facility_id"
KEY_FACILITY_NAME = "facility_name"
KEY_FACILITY_ADDRESS = "facility_address"
KEY_INTERVALS = "intervals"  # list of parsed 15-min interval dicts
KEY_ENERGY_YESTERDAY = "energy_yesterday"
KEY_ENERGY_LAST_24H = "energy_last_24h"
KEY_LATEST_POWER_W = "latest_power_w"
KEY_LATEST_INTERVAL_KWH = "latest_interval_kwh"
KEY_LATEST_TIMESTAMP = "latest_timestamp"
KEY_DATA_DELAY_HOURS = "data_delay_hours"

# Interval data keys
INTERVAL_START = "start"
INTERVAL_END = "end"
INTERVAL_DIRECTION = "direction"
INTERVAL_QUALITY = "quality"
INTERVAL_KWH = "kwh"
