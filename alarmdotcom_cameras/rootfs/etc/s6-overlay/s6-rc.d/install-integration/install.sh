#!/command/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# Auto-install the companion HA integration into /config/custom_components
# ==============================================================================

declare src="/usr/share/alarmdotcom_cameras/custom_components/alarmdotcom_cameras"
declare dst="/config/custom_components/alarmdotcom_cameras"

if [ ! -d "${src}" ]; then
    bashio::log.warning "Companion integration source not found, skipping install"
    exit 0
fi

# Read bundled version
bundled_version=$(python3 -c "import json; print(json.load(open('${src}/manifest.json'))['version'])" 2>/dev/null || echo "0.0.0")

# Check if already installed and up-to-date
if [ -f "${dst}/manifest.json" ]; then
    installed_version=$(python3 -c "import json; print(json.load(open('${dst}/manifest.json'))['version'])" 2>/dev/null || echo "")
    if [ "${installed_version}" = "${bundled_version}" ]; then
        bashio::log.info "Companion integration v${installed_version} already installed"
        exit 0
    fi
    bashio::log.info "Upgrading companion integration: v${installed_version} -> v${bundled_version}"
else
    bashio::log.info "Installing companion integration v${bundled_version}"
fi

# Install/upgrade
mkdir -p /config/custom_components
rm -rf "${dst}"
cp -r "${src}" "${dst}"

bashio::log.info "Companion integration installed to ${dst}"
bashio::log.info "If this is the first install, restart Home Assistant and add the integration via Settings > Devices & Services"
