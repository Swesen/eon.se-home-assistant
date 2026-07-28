#!/usr/bin/with-contenv bashio

bashio::log.info "Starting E.ON Sweden Auth server..."

# Read options
export EON_PERSONNUMMER="$(bashio::config 'personnummer')"
export EON_PASSWORD="$(bashio::config 'password')"
export LOG_LEVEL="$(bashio::config 'log_level')"

bashio::log.info "Auth server listening on port 8099"
exec python3 /app/auth_server.py
