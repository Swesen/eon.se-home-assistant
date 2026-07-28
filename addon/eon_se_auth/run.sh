#!/bin/bash
set -e

echo "[eon_se_auth] Starting E.ON Sweden Auth server..."

# HA Supervisor writes add-on options to /data/options.json
# auth_server.py reads this file directly, so no bashio needed.

exec python3 /app/auth_server.py
