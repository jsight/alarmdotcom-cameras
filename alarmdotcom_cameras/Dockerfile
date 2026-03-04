ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH

# --- Stage: System dependencies ---
# Using Debian base for glibc compatibility with Chromium/Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    # Playwright Chromium runtime dependencies (subset, rest via install-deps)
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

# --- Stage: Python environment ---
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies (cached layer - only rebuilds when requirements.txt changes)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# --- Stage: Playwright + Chromium ---
# Install Chromium only (not Firefox/WebKit) and its remaining OS deps
RUN playwright install chromium \
    && playwright install-deps chromium \
    # Clean up Playwright cache of unnecessary files
    && find /root/.cache/ms-playwright -name "*.pak" -not -name "en-US.pak" -not -name "resources.pak" -delete 2>/dev/null || true \
    && rm -rf /root/.cache/ms-playwright/*/chrome-linux*/MEIPreload 2>/dev/null || true \
    # Clean up apt cache from install-deps
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# --- Stage: Application ---
# Copy application code
COPY rootfs /

# Bundle companion HA integration for auto-install
COPY custom_components /usr/share/alarmdotcom_cameras/custom_components

# Make the Python package importable
ENV PYTHONPATH="/usr/share:${PYTHONPATH}"

# Chromium performance/compatibility flags (applied via Playwright args too,
# but these env vars ensure consistent behavior)
ENV PLAYWRIGHT_BROWSERS_PATH="/root/.cache/ms-playwright"

# Shared memory: Chromium needs /dev/shm to be larger than default 64MB
# HA add-on Supervisor handles this via tmpfs, but set the env hint
ENV DBUS_SESSION_BUS_ADDRESS="autolaunch:"

# Make S6 scripts executable
RUN chmod +x /etc/s6-overlay/s6-rc.d/alarmdotcom-cameras/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/alarmdotcom-cameras/finish \
    && chmod +x /etc/s6-overlay/s6-rc.d/install-integration/install.sh

LABEL \
    io.hass.name="Alarm.com Cameras" \
    io.hass.description="Alarm.com camera snapshots and live view via headless browser" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="0.1.0"
