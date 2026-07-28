ARG BUILD_FROM=debian:bookworm-slim
FROM ${BUILD_FROM}

# Install Python and system dependencies for Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip3 install --break-system-packages \
    playwright \
    flask \
    aiohttp

# Install Chromium and its dependencies via Playwright
RUN playwright install chromium --with-deps

WORKDIR /app
COPY auth_server.py haapi_browser_auth.py ./

# Add-on run script
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
