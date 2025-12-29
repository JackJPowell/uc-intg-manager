"""
Flask Web Server for Integration Manager.

This module provides the web interface for managing integrations
on the Unfolded Circle Remote.

Uses synchronous HTTP clients (requests) to avoid aiohttp async context issues.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import re

import io
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import markdown
from flask import Flask, render_template, jsonify, request, send_file, Response
from werkzeug.serving import make_server

from backup_service import (
    backup_integration,
    get_all_backups,
    delete_backup,
    backup_all_integrations,
    get_backup,
)
from const import WEB_SERVER_PORT, Settings, API_DELAY, INTEGRATION_BACKUPS_FILE
from log_handler import get_log_entries, get_log_handler
from migration_service import extract_migration_mappings
from sync_api import SyncRemoteClient, SyncGitHubClient, load_registry, SyncAPIError
from packaging.version import Version, InvalidVersion

_LOG = logging.getLogger(__name__)

# Set werkzeug logging to WARNING and above to reduce noise
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Get template and static directories from source
# Handle PyInstaller frozen executables where data is in sys._MEIPASS
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Running as PyInstaller bundle
    BASE_DIR = sys._MEIPASS
else:
    # Running as regular Python script
    BASE_DIR = os.path.dirname(__file__)

TEMPLATE_DIR = os.path.abspath(os.path.join(BASE_DIR, "templates"))
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "static"))

# Create Flask app with cache disabled for read-only filesystems
app = Flask(
    __name__,
    template_folder=TEMPLATE_DIR,
    static_folder=STATIC_DIR,
)
# Disable Jinja2 bytecode cache to avoid writing to read-only filesystem
app.jinja_env.auto_reload = True
app.jinja_env.cache = {}
app.jinja_env.bytecode_cache = None
# Additional config for read-only filesystem
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Will be set by WebServer class
_remote_client: SyncRemoteClient | None = None
_github_client: SyncGitHubClient | None = None

# Cached version data for integrations
_cached_version_data: dict = {}
_version_check_timestamp: str | None = None
_cached_driver_ids: set = set()  # Track installed driver IDs to detect changes

# Operation lock to prevent concurrent installs/upgrades
_operation_in_progress: bool = False
_operation_lock = threading.Lock()


@dataclass
class IntegrationInfo:
    """Integration information for display."""

    instance_id: str
    driver_id: str
    name: str
    version: str
    description: str = ""
    icon: str = ""
    home_page: str = ""
    developer: str = ""
    enabled: bool = True
    state: str = "UNKNOWN"
    update_available: bool = False
    latest_version: str | None = None
    custom: bool = False  # Running on the remote (installed via tar.gz)
    official: bool = False  # Official UC integration (firmware-managed)
    external: bool = False  # Running externally (Docker/network)
    configured_entities: int = 0
    supports_backup: bool = False  # Uses ucapi-framework with backup support
    can_update: bool = False  # Show update button (always true if update available for custom integrations)
    can_auto_update: bool = False  # Can do automated backup/restore (requires supports_backup and min version)


@dataclass
class AvailableIntegration:
    """Available integration from registry."""

    driver_id: str
    name: str
    description: str = ""
    icon: str = ""
    home_page: str = ""
    developer: str = ""
    version: str = ""
    category: str = ""
    categories: list | None = None
    installed: bool = False  # Has an instance configured
    driver_installed: bool = False  # Driver is installed (may not have instance)
    external: bool = False  # Running externally (Docker/network)
    custom: bool = True
    official: bool = False
    update_available: bool = False
    latest_version: str = ""
    instance_id: str = ""  # Instance ID if configured
    can_update: bool = False  # Show update button (always true if update available for custom integrations)
    can_auto_update: bool = False  # Can do automated backup/restore (requires supports_backup and min version)
    supports_backup: bool = False  # Uses ucapi-framework with backup support

    @property
    def install_status(self) -> str:
        """Get installation status for display."""
        if self.official:
            return "official"
        if self.external:
            return "external"
        if self.installed:
            return "configured"
        if self.driver_installed:
            return "installed"
        return "available"

    def __post_init__(self):
        if self.categories is None:
            self.categories = []


def _get_latest_release_for_update(owner: str, repo: str) -> dict[str, Any] | None:
    """
    Get the latest release considering the show_beta_releases setting.

    If show_beta_releases is enabled, returns the latest release (stable or beta).
    If disabled, returns only the latest stable release.

    :param owner: GitHub repository owner
    :param repo: GitHub repository name
    :return: Release data or None if not found
    """
    if not _github_client:
        return None

    settings = Settings.load()

    if settings.show_beta_releases:
        # Get recent releases and pick the first non-draft one (could be beta or stable)
        releases = _github_client.get_releases(owner, repo, limit=5)
        if releases:
            for release in releases:
                if not release.get("draft", False):
                    return release
        return None
    else:
        # Get latest stable release only (GitHub's /releases/latest excludes pre-releases)
        return _github_client.get_latest_release(owner, repo)


def _refresh_version_cache() -> None:
    """
    Refresh the cached version information for all installed integrations.

    This is called after installations/updates to ensure the UI shows
    current version information.
    """
    global _cached_version_data, _version_check_timestamp, _cached_driver_ids

    if not _remote_client or not _github_client:
        return

    try:
        _LOG.info("Refreshing version cache after update...")

        # Get installed integrations
        integrations = _get_installed_integrations()
        version_updates = {}
        current_driver_ids = set()

        for integration in integrations:
            current_driver_ids.add(integration.driver_id)

            if integration.official:
                continue

            if not integration.home_page or "github.com" not in integration.home_page:
                continue

            # Small delay to avoid GitHub rate limiting
            time.sleep(0.1)

            try:
                parsed = SyncGitHubClient.parse_github_url(integration.home_page)
                if not parsed:
                    continue

                owner, repo = parsed
                release = _get_latest_release_for_update(owner, repo)
                if release:
                    latest_version = release.get("tag_name", "")
                    current_version = integration.version or ""
                    has_update = SyncGitHubClient.compare_versions(
                        current_version, latest_version
                    )
                    version_updates[integration.driver_id] = {
                        "current": current_version,
                        "latest": latest_version,
                        "has_update": has_update,
                    }
            except Exception as e:
                _LOG.debug(
                    "Failed to check version for %s: %s", integration.driver_id, e
                )

        _cached_version_data = version_updates
        _version_check_timestamp = datetime.now().isoformat()
        _cached_driver_ids = current_driver_ids

        _LOG.info("Version cache refreshed: %d integrations", len(version_updates))
    except Exception as e:
        _LOG.error("Failed to refresh version cache: %s", e)


def _get_installed_integrations() -> list[IntegrationInfo]:
    """Get list of installed integrations with metadata.

    This includes:
    - Configured instances (drivers with instances)
    - Installed drivers without instances (needs configuration)

    Excludes LOCAL (firmware) drivers unless they have an instance configured.

    driver_type values from API:
    - CUSTOM: installed on the remote via tar.gz
    - EXTERNAL: running in Docker or external server
    - LOCAL: built into firmware
    """
    if not _remote_client:
        return []

    # Load registry to check for supports_backup flag and driver_id mapping
    registry = load_registry()
    # Primary lookup: by driver_id field (matches what remote reports)
    registry_by_driver_id = {
        item.get("driver_id", ""): item for item in registry if item.get("driver_id")
    }
    # Secondary lookup: by registry id (fallback)
    registry_by_id = {item.get("id", ""): item for item in registry}
    # Tertiary lookup: by name for fuzzy matching (last resort)
    registry_by_name = {item.get("name", "").lower(): item for item in registry}

    def find_registry_item(driver_id: str, driver_name: str) -> dict:
        """Find registry item by driver_id, registry id, or fuzzy name match."""
        # Primary: match by driver_id field (what the remote reports)
        if driver_id in registry_by_driver_id:
            return registry_by_driver_id[driver_id]

        # Secondary: match by registry id
        if driver_id in registry_by_id:
            return registry_by_id[driver_id]

        # Tertiary: fuzzy name matching (fallback for integrations not yet updated)
        driver_name_lower = driver_name.lower()
        for reg_name, item in registry_by_name.items():
            if (
                reg_name == driver_name_lower
                or driver_name_lower in reg_name
                or reg_name in driver_name_lower
            ):
                return item
        return {}

    integrations = []
    configured_driver_ids = set()

    # First, get all configured instances
    try:
        instances = _remote_client.get_integrations()
    except SyncAPIError as e:
        _LOG.error("Failed to get integrations: %s", e)
        instances = []

    # Build set of configured driver IDs
    for instance in instances:
        configured_driver_ids.add(instance.get("driver_id", ""))

    # Get all drivers
    try:
        drivers = _remote_client.get_drivers()
    except SyncAPIError as e:
        _LOG.error("Failed to get drivers: %s", e)
        drivers = []

    # Build driver lookup
    driver_lookup = {d.get("driver_id", ""): d for d in drivers}

    # Process configured instances first
    for instance in instances:
        driver_id = instance.get("driver_id", "")
        driver = driver_lookup.get(driver_id, {})

        developer = driver.get("developer", {}).get("name", "")
        home_page = driver.get("developer", {}).get("url", "")
        driver_type = driver.get("driver_type", "CUSTOM")
        driver_name = (
            driver.get("name", {}).get("en", driver_id) if driver else driver_id
        )

        # Map driver_type to our flags (official = LOCAL firmware integrations)
        is_official = driver_type == "LOCAL"
        is_external = driver_type == "EXTERNAL"
        is_custom = driver_type == "CUSTOM"

        # Check registry for supports_backup flag and repository URL fallback
        # Use fuzzy matching since driver_id may not match registry id exactly
        registry_item = find_registry_item(driver_id, driver_name)
        supports_backup = registry_item.get("supports_backup", False)

        if not home_page and registry_item.get("repository"):
            home_page: str = registry_item.get("repository", "")
        # Also use registry if driver home_page doesn't have github.com
        elif (
            home_page
            and "github.com" not in home_page
            and registry_item.get("repository")
        ):
            home_page = registry_item.get("repository", "")

        # Get description from driver, fall back to registry
        description: str = driver.get("description", {}).get("en", "") if driver else ""
        if not description and registry_item.get("description"):
            description = registry_item.get("description", "")

        info = IntegrationInfo(
            instance_id=instance.get("integration_id", ""),
            driver_id=driver_id,
            name=driver_name,
            version=driver.get("version", "0.0.0") if driver else "0.0.0",
            description=description,
            icon=instance.get("icon", ""),
            home_page=home_page,
            developer=developer,
            enabled=instance.get("enabled", True),
            state=instance.get("device_state", "UNKNOWN"),
            custom=is_custom,
            official=is_official,
            external=is_external,
            configured_entities=len(instance.get("configured_entities", [])),
            supports_backup=supports_backup,
        )

        # Check for updates using cached version data from background checks
        # This ensures consistent version info regardless of when page is loaded
        if is_custom and driver_id in _cached_version_data:
            version_info = _cached_version_data[driver_id]
            if version_info.get("has_update"):
                # Always mark that an update is available (for badge display)
                info.update_available = True
                info.latest_version = version_info.get("latest", "")
                _LOG.debug(
                    "Update available for %s: %s -> %s (from cache)",
                    driver_id,
                    info.version,
                    info.latest_version,
                )

                # Show update button for all custom integrations with updates
                info.can_update = True
                _LOG.debug(
                    "Update button enabled for %s (can_update=True, can_auto_update will be determined)",
                    driver_id,
                )

                # Check if automated backup/restore is possible
                # Requires: supports_backup AND version >= backup_min_version (if specified)
                min_version = registry_item.get("backup_min_version")
                info.can_auto_update = supports_backup

                if min_version and supports_backup:
                    try:
                        if Version(info.version) < Version(min_version):
                            info.can_auto_update = False
                            _LOG.debug(
                                "Update available for %s: %s -> %s (requires manual reconfiguration - version %s < minimum %s)",
                                driver_id,
                                info.version,
                                info.latest_version,
                                info.version,
                                min_version,
                            )
                    except (InvalidVersion, TypeError):
                        # If version parsing fails, allow auto update if supports_backup
                        pass

        integrations.append(info)

    # Now add drivers without instances (but NOT LOCAL ones - they're firmware-only)
    for driver in drivers:
        driver_id = driver.get("driver_id", "")
        driver_type = driver.get("driver_type", "CUSTOM")

        # Skip if already processed (has an instance)
        if driver_id in configured_driver_ids:
            continue

        # Skip LOCAL drivers that aren't configured - they're just firmware options
        if driver_type == "LOCAL":
            continue

        developer = driver.get("developer", {}).get("name", "")
        home_page = driver.get("developer", {}).get("url", "")
        driver_name = driver.get("name", {}).get("en", driver_id)

        # Map driver_type to our flags (official = LOCAL firmware integrations)
        is_official = driver_type == "LOCAL"
        is_external = driver_type == "EXTERNAL"
        is_custom = driver_type == "CUSTOM"

        # Check registry for supports_backup flag and repository URL fallback
        # Use fuzzy matching since driver_id may not match registry id exactly
        registry_item = find_registry_item(driver_id, driver_name)
        supports_backup = registry_item.get("supports_backup", False)

        # Use registry repository as fallback for home_page
        if not home_page and registry_item.get("repository"):
            home_page = registry_item.get("repository", "")
        # Also use registry if driver home_page doesn't have github.com
        elif (
            home_page
            and "github.com" not in home_page
            and registry_item.get("repository")
        ):
            home_page = registry_item.get("repository", "")

        # Get description from driver, fall back to registry
        description = driver.get("description", {}).get("en", "")
        if not description and registry_item.get("description"):
            description = registry_item.get("description", "")

        info = IntegrationInfo(
            instance_id="",  # No instance yet
            driver_id=driver_id,
            name=driver_name,
            version=driver.get("version", "0.0.0"),
            description=description,
            icon=driver.get("icon", ""),
            home_page=home_page,
            developer=developer,
            enabled=False,  # Not configured yet
            state="NOT_CONFIGURED",  # Special state for unconfigured drivers
            custom=is_custom,
            official=is_official,
            external=is_external,
            configured_entities=0,
            supports_backup=supports_backup,
        )

        # Check for updates using cached version data (for unconfigured drivers too)
        if is_custom and driver_id in _cached_version_data:
            version_info = _cached_version_data[driver_id]
            if version_info.get("has_update"):
                # Always mark that an update is available (for badge display)
                info.update_available = True
                info.latest_version = version_info.get("latest", "")

                # Show update button for all custom integrations with updates
                info.can_update = True
                _LOG.debug(
                    "Update button enabled for unconfigured %s (can_update=True)",
                    driver_id,
                )

                # Check if automated backup/restore is possible
                # Requires: supports_backup AND version >= backup_min_version (if specified)
                min_version = registry_item.get("backup_min_version")
                info.can_auto_update = supports_backup

                if min_version and supports_backup:
                    try:
                        if Version(info.version) < Version(min_version):
                            info.can_auto_update = False
                            _LOG.debug(
                                "Update available for %s: %s -> %s (requires manual reconfiguration - version %s < minimum %s)",
                                driver_id,
                                info.version,
                                info.latest_version,
                                info.version,
                                min_version,
                            )
                    except (InvalidVersion, TypeError):
                        # If version parsing fails, allow auto update if supports_backup
                        pass

        integrations.append(info)

    return integrations


def _get_available_integrations() -> list[AvailableIntegration]:
    """Get list of available integrations from registry.

    Uses driver_type from API:
    - CUSTOM: installed on the remote via tar.gz
    - EXTERNAL: running in Docker or external server
    - LOCAL: built into firmware
    """
    registry = load_registry()

    # Get installed driver info for comparison
    installed_drivers = {}  # driver_id -> (driver_type, version)
    configured_driver_ids = {}  # driver_id -> instance_id
    driver_names = {}  # Map name -> (driver_id, driver_type, version) for fuzzy matching

    if _remote_client:
        try:
            # Get all drivers (installed)
            drivers = _remote_client.get_drivers()
            for driver in drivers:
                driver_id = driver.get("driver_id", "")
                driver_type = driver.get("driver_type", "CUSTOM")
                version = driver.get("version", "")
                installed_drivers[driver_id] = (driver_type, version)
                # Also store driver name for fuzzy matching
                name = driver.get("name", {}).get("en", "").lower()
                if name:
                    driver_names[name] = (driver_id, driver_type, version)
        except SyncAPIError:
            pass

        try:
            # Get all instances (configured) with their instance IDs
            for instance in _remote_client.get_integrations():
                driver_id = instance.get("driver_id", "")
                instance_id = instance.get("integration_id", "")
                configured_driver_ids[driver_id] = instance_id
        except SyncAPIError:
            pass

    def is_match(
        registry_id: str, registry_name: str
    ) -> tuple[bool, bool, bool, str, str, str]:
        """Check if a registry item matches an installed driver.

        Returns: (is_installed, is_configured, is_external, version, instance_id, actual_driver_id)
        """
        # Direct ID match
        if registry_id in installed_drivers:
            driver_type, version = installed_drivers[registry_id]
            is_external = driver_type == "EXTERNAL"
            is_configured = registry_id in configured_driver_ids
            instance_id = configured_driver_ids.get(registry_id, "")
            return (True, is_configured, is_external, version, instance_id, registry_id)

        # Try fuzzy match by name
        registry_name_lower = registry_name.lower()
        for name, (driver_id, driver_type, version) in driver_names.items():
            # Check if names match closely
            if (
                name == registry_name_lower
                or registry_name_lower in name
                or name in registry_name_lower
            ):
                is_external = driver_type == "EXTERNAL"
                is_configured = driver_id in configured_driver_ids
                instance_id = configured_driver_ids.get(driver_id, "")
                return (
                    True,
                    is_configured,
                    is_external,
                    version,
                    instance_id,
                    driver_id,
                )

        return (False, False, False, "", "", "")

    available = []
    for item in registry:
        # Derive official status from custom field (official = not custom)
        is_official = not item.get("custom", True)
        driver_id = item.get("id", "")
        name = item.get("name", "")
        home_page = item.get("repository", "")

        # Check installation status with fuzzy matching
        (
            is_installed,
            is_configured,
            is_external,
            version,
            instance_id,
            actual_driver_id,
        ) = is_match(driver_id, name)

        # Check for updates for installed custom integrations using cached data
        update_available = False
        latest_version = ""
        can_update = False
        can_auto_update = False
        supports_backup = item.get("supports_backup", False)

        if is_installed and not is_official and not is_external:
            # Use the actual driver_id from the remote (not registry id) for cache lookup
            if actual_driver_id and actual_driver_id in _cached_version_data:
                version_info = _cached_version_data[actual_driver_id]
                if version_info.get("has_update"):
                    # Always mark that an update is available (for badge display)
                    update_available = True
                    latest_version = version_info.get("latest", "")

                    # Show update button for all custom integrations with updates
                    can_update = True

                    # Check if automated backup/restore is possible
                    # Requires: supports_backup AND version >= backup_min_version (if specified)
                    min_version = item.get("backup_min_version")
                    can_auto_update = supports_backup

                    if min_version and supports_backup and version:
                        try:
                            if Version(version) < Version(min_version):
                                can_auto_update = False
                                _LOG.debug(
                                    "Update available for %s: %s -> %s (requires manual reconfiguration - version %s < minimum %s)",
                                    actual_driver_id,
                                    version,
                                    latest_version,
                                    version,
                                    min_version,
                                )
                        except (InvalidVersion, TypeError):
                            # If version parsing fails, allow auto update if supports_backup
                            pass

        categories_list = item.get("categories", [])
        avail = AvailableIntegration(
            driver_id=actual_driver_id if actual_driver_id else driver_id,
            name=name,
            description=item.get("description", ""),
            icon=item.get("icon", "code"),  # FontAwesome icon base name
            home_page=home_page,
            developer=item.get("author", ""),
            version=version,
            category=categories_list[0] if categories_list else "",
            categories=categories_list,
            installed=is_configured,
            driver_installed=is_installed,
            external=is_external,
            custom=not is_official,
            official=is_official,
            update_available=update_available,
            latest_version=latest_version,
            instance_id=instance_id,
            can_update=can_update,
            can_auto_update=can_auto_update,
            supports_backup=supports_backup,
        )
        available.append(avail)

    return available


def _can_backup_integration(
    driver_id: str, current_version: str, registry_item: dict
) -> tuple[bool, str]:
    """
    Check if an integration can be backed up based on version requirements.

    :param driver_id: The driver ID
    :param current_version: Current installed version
    :param registry_item: Registry entry for the integration
    :return: (can_backup, reason)
    """
    if not registry_item.get("supports_backup", False):
        return False, "Integration doesn't support backup"

    min_version = registry_item.get("backup_min_version")
    if not min_version:
        return True, ""  # No minimum version requirement

    try:
        if Version(current_version) < Version(min_version):
            return (
                False,
                f"Requires version {min_version} or higher (current: {current_version})",
            )
    except (InvalidVersion, TypeError):
        # If version parsing fails, assume compatible
        pass

    return True, ""


# =============================================================================
# Routes
# =============================================================================


@app.route("/health")
def health():
    """Simple health check endpoint."""
    return "OK"


@app.route("/api/registry")
def get_registry():
    """Serve the integrations registry (for local development/testing)."""
    registry_path = Path(__file__).parent / "integrations-registry.json"
    if registry_path.exists():
        with open(registry_path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"integrations": []})


@app.route("/")
def index():
    """Render the main dashboard page."""
    return render_template("index.html")


@app.route("/integrations")
def integrations_page():
    """Render the integrations management page."""
    return render_template("integrations.html")


@app.route("/available")
def available_page():
    """Render the available integrations page."""
    return render_template("available.html")


# =============================================================================
# HTMX Partial Routes
# =============================================================================


@app.route("/api/stats/installed-count")
def get_installed_count():
    """Get the count of installed integrations.

    Counts drivers where:
    - driver_type is CUSTOM or EXTERNAL (always count)
    - driver_type is LOCAL only if it has a configured instance
    """
    if not _remote_client:
        return "0"

    try:
        # Get configured instance driver IDs
        instances = _remote_client.get_integrations()
        configured_driver_ids = {i.get("driver_id", "") for i in instances}

        # Get all drivers
        drivers = _remote_client.get_drivers()

        count = 0
        for driver in drivers:
            driver_id = driver.get("driver_id", "")
            driver_type = driver.get("driver_type", "CUSTOM")

            # Count CUSTOM and EXTERNAL drivers always
            if driver_type in ("CUSTOM", "EXTERNAL"):
                count += 1
            # Count LOCAL only if configured
            elif driver_type == "LOCAL" and driver_id in configured_driver_ids:
                count += 1

        return str(count)
    except SyncAPIError as e:
        _LOG.error("Failed to get integrations count: %s", e)
        return "0"


@app.route("/api/stats/updates-count")
def get_updates_count():
    """Get the count of integrations with available updates."""
    if not _remote_client or not _github_client:
        return "0"

    try:
        integrations = _get_installed_integrations()
        count = sum(
            1
            for i in integrations
            if i.update_available and not i.official and not i.external
        )
        return str(count)
    except Exception as e:
        _LOG.error("Failed to get updates count: %s", e)
        return "0"


@app.route("/api/integrations/list")
def get_integrations_list():
    """Get HTML partial with list of installed integrations."""
    if not _remote_client:
        return "<div class='text-red-500'>Service not initialized</div>"

    try:
        integrations = _get_installed_integrations()

        # Check if driver list changed (new/removed drivers) and refresh cache if needed
        current_driver_ids = {i.driver_id for i in integrations}
        if current_driver_ids != _cached_driver_ids:
            _LOG.info("Driver list changed, refreshing version cache...")
            _refresh_version_cache()
            # Re-fetch integrations with updated cache
            integrations = _get_installed_integrations()

        settings = Settings.load()
        remote_ip = _remote_client._address if _remote_client else None
        return render_template(
            "partials/integration_list.html",
            integrations=integrations,
            remote_ip=remote_ip,
            settings=settings,
        )
    except Exception as e:
        _LOG.error("Failed to get integrations: %s", e)
        return f"<div class='text-red-500'>Error: {e}</div>"


@app.route("/api/integrations/available")
def get_available_list():
    """Get HTML partial with list of available integrations."""
    try:
        available = _get_available_integrations()
        remote_ip = _remote_client._address if _remote_client else None
        return render_template(
            "partials/available_list.html",
            integrations=available,
            remote_ip=remote_ip,
        )
    except Exception as e:
        _LOG.error("Failed to get available integrations: %s", e)
        return f"<div class='text-red-500'>Error: {e}</div>"


@app.route("/api/integrations/refresh-versions", methods=["POST"])
def refresh_versions():
    """Manually refresh version cache for all integrations."""
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        _LOG.info("Manual version cache refresh requested")
        _refresh_version_cache()
        return jsonify({"status": "success", "message": "Version cache refreshed"})
    except Exception as e:
        _LOG.error("Failed to refresh version cache: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/integration/<instance_id>")
def get_integration_detail(instance_id: str):
    """Get HTML partial with integration details."""
    if not _remote_client:
        return "<div class='text-red-500'>Service not initialized</div>"

    try:
        # Find the integration in the list
        integrations = _get_installed_integrations()
        integration = next(
            (i for i in integrations if i.instance_id == instance_id), None
        )
        if integration:
            return render_template(
                "partials/integration_detail.html", integration=integration
            )
        return "<div class='text-yellow-500'>Integration not found</div>"
    except Exception as e:
        _LOG.error("Failed to get integration detail: %s", e)
        return f"<div class='text-red-500'>Error: {e}</div>"


@app.route("/api/integration/<instance_id>/update", methods=["POST"])
def update_integration(instance_id: str):
    """
    Update an existing integration to the latest or specified version using default settings.

    Accepts optional 'version' query parameter to update to a specific version.

    The register_entities behavior is determined by the user's auto_register_entities setting.
    """
    settings = Settings.load()
    version = request.args.get("version") or request.form.get("version")
    return _perform_update_integration(
        instance_id, settings.auto_register_entities, version
    )


@app.route("/api/integration/<instance_id>/update-alt", methods=["POST"])
def update_integration_alt(instance_id: str):
    """
    Update an existing integration with the opposite entity registration behavior.

    Accepts optional 'version' query parameter to update to a specific version.

    If auto_register_entities is enabled, this will NOT register entities.
    If auto_register_entities is disabled, this WILL register entities.
    """
    settings = Settings.load()
    version = request.args.get("version") or request.form.get("version")
    return _perform_update_integration(
        instance_id, not settings.auto_register_entities, version
    )


def _perform_update_integration(
    instance_id: str, register_entities: bool, version: str | None = None
):
    """
    Update an existing integration to the latest or specified version.

    Process:
    1. Fetch current version (for migration check)
    2. Validate version against migration boundary if specified
    3. Backup the current configuration
    4. Find the integration's GitHub repo URL
    5. Download the specified or latest release tar.gz
    6. Delete the existing driver (which cascades to delete instance)
    7. Install the new version
    8. Check if migration is required
    9. Restore configuration (with updated entity IDs if migration needed)
    10. Execute migration if required
    11. Optionally register entities if register_entities=True

    :param instance_id: The integration instance ID to update
    :param register_entities: Whether to register entities after update
    :param version: Optional specific version to update to (e.g., 'v1.2.3')
    """
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    # Check if another operation is in progress
    global _operation_in_progress
    with _operation_lock:
        _LOG.info(
            "Lock check for instance %s: _operation_in_progress=%s",
            instance_id,
            _operation_in_progress,
        )
        if _operation_in_progress:
            _LOG.warning("Update blocked for instance %s - lock is held", instance_id)
            return jsonify(
                {"status": "error", "message": "Another install/upgrade is in progress"}
            ), 409
        _operation_in_progress = True
        _LOG.info("Lock acquired for updating instance %s", instance_id)

    backup_data = None
    previous_version = None

    try:
        # Find the integration to get its GitHub URL
        integrations = _get_installed_integrations()
        integration = next(
            (i for i in integrations if i.instance_id == instance_id), None
        )

        if not integration:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - integration %s not found", instance_id)
            return jsonify({"status": "error", "message": "Integration not found"}), 404

        if integration.official:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - integration %s is official", instance_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "Official integrations are managed by firmware updates",
                }
            ), 400

        if not integration.home_page or "github.com" not in integration.home_page:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - integration %s has no GitHub URL", instance_id
                )
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this integration",
                }
            ), 400

        # Determine if this is a configured instance (has backup/restore capability)
        is_configured = bool(instance_id and integration.instance_id)
        _LOG.info(
            "Updating %s: configured=%s, supports_backup=%s",
            integration.driver_id,
            is_configured,
            integration.supports_backup,
        )

        # Load registry to check for migration_required_at
        migration_required_at = None
        try:
            registry = load_registry()
            for entry in registry:
                if entry.get("driver_id") == integration.driver_id:
                    migration_required_at = entry.get("migration_required_at")
                    if migration_required_at:
                        _LOG.info(
                            "Registry indicates migration may be required at version: %s",
                            migration_required_at,
                        )
                    break
        except Exception as e:
            _LOG.warning("Failed to load registry for migration check: %s", e)

        # Validate version against migration boundary if specified
        if version and migration_required_at:
            clean_version = version.lstrip("v")
            try:
                if Version(clean_version) <= Version(migration_required_at):
                    with _operation_lock:
                        _operation_in_progress = False
                    _LOG.warning(
                        "Update blocked for %s - version %s violates migration boundary %s",
                        integration.driver_id,
                        version,
                        migration_required_at,
                    )
                    return jsonify(
                        {
                            "status": "error",
                            "message": f"Cannot downgrade to version {version} - requires version > {migration_required_at}",
                        }
                    ), 400
            except InvalidVersion as e:
                with _operation_lock:
                    _operation_in_progress = False
                _LOG.warning("Invalid version format %s: %s", version, e)
                return jsonify(
                    {"status": "error", "message": f"Invalid version format: {version}"}
                ), 400

        # Step 1: Store current version for migration check
        previous_version = integration.version
        if previous_version:
            _LOG.info(
                "Current version of %s: %s", integration.driver_id, previous_version
            )

        # Capture list of configured entities before update (if user wants to re-register)
        configured_entity_ids = []
        if register_entities and is_configured:
            try:
                _LOG.info(
                    "Capturing configured entities before update: %s", instance_id
                )
                configured_entities = _remote_client.get_configured_entities(
                    instance_id
                )
                configured_entity_ids = [
                    str(entity.get("entity_id"))
                    for entity in configured_entities
                    if entity.get("entity_id")
                ]
                _LOG.info(
                    "Found %d configured entities for %s: %s",
                    len(configured_entity_ids),
                    integration.driver_id,
                    configured_entity_ids,
                )
            except Exception as e:
                _LOG.warning(
                    "Failed to capture configured entities for %s: %s",
                    integration.driver_id,
                    e,
                )

        # Step 1: Backup current configuration before updating (only for configured instances)
        # For integrations that support backup AND meet minimum version, we REQUIRE a successful backup
        # For integrations without backup support or below minimum version, we proceed without backup
        if is_configured:
            # Check if this integration can actually do automated backup/restore
            # It requires supports_backup AND version >= backup_min_version
            can_backup = integration.supports_backup
            if can_backup:
                # Check if current version meets minimum version requirement
                min_version = None
                try:
                    registry = load_registry()
                    for entry in registry:
                        if entry.get("driver_id") == integration.driver_id:
                            min_version = entry.get("backup_min_version")
                            break
                except Exception:
                    pass

                if min_version and integration.version:
                    try:
                        if Version(integration.version) < Version(min_version):
                            can_backup = False
                            _LOG.info(
                                "Backup not available for %s: current version %s is below minimum %s",
                                integration.driver_id,
                                integration.version,
                                min_version,
                            )
                    except (InvalidVersion, TypeError):
                        pass

            if can_backup:
                # This integration SHOULD support backup - require it
                _LOG.info(
                    "Backing up configuration before update: %s", integration.driver_id
                )
                try:
                    backup_data = backup_integration(
                        _remote_client, integration.driver_id, save_to_file=True
                    )
                    if backup_data:
                        _LOG.info(
                            "Successfully backed up configuration for %s",
                            integration.driver_id,
                        )
                    else:
                        # Integration should support backup but backup failed - don't proceed
                        _LOG.error(
                            "Backup required for %s but no data was retrieved",
                            integration.driver_id,
                        )
                        with _operation_lock:
                            _operation_in_progress = False
                            _LOG.info(
                                "Lock released - backup failed for integration %s",
                                instance_id,
                            )
                        return jsonify(
                            {
                                "status": "error",
                                "message": "Backup failed - cannot update without successful backup for this integration",
                            }
                        ), 400
                except Exception as e:
                    # Integration should support backup but backup failed - don't proceed
                    _LOG.error(
                        "Backup required for %s but failed: %s",
                        integration.driver_id,
                        e,
                    )
                    with _operation_lock:
                        _operation_in_progress = False
                        _LOG.info(
                            "Lock released - backup exception for integration %s",
                            instance_id,
                        )
                    return jsonify(
                        {
                            "status": "error",
                            "message": f"Backup failed - cannot update: {e}",
                        }
                    ), 400
            else:
                # Integration doesn't support backup or version too old - proceed without backup
                _LOG.info(
                    "Skipping backup for %s: supports_backup=%s, can_backup=%s",
                    integration.driver_id,
                    integration.supports_backup,
                    can_backup,
                )
                _LOG.info(
                    "Configuration will NOT be preserved - user will need to reconfigure"
                )
        else:
            _LOG.info(
                "Skipping backup for unconfigured driver: %s", integration.driver_id
            )

        # Parse GitHub URL
        parsed = SyncGitHubClient.parse_github_url(integration.home_page)
        if not parsed:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - could not parse GitHub URL for integration %s",
                    instance_id,
                )
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Download the specified or latest release
        if version:
            _LOG.info(
                "Updating integration %s to version %s", integration.driver_id, version
            )
            download_result = _github_client.download_release_asset(
                owner, repo, version=version
            )
        else:
            _LOG.info(
                "Updating integration %s to latest version", integration.driver_id
            )
            download_result = _github_client.download_release_asset(owner, repo)
        if not download_result:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - no release found for integration %s", instance_id
                )
            return jsonify(
                {
                    "status": "error",
                    "message": f"No tar.gz release found for {owner}/{repo}"
                    + (f" version {version}" if version else ""),
                }
            ), 404

        archive_data, filename = download_result
        _LOG.info("Downloaded %s (%d bytes) for update", filename, len(archive_data))

        # Delete the existing driver (cascades to delete instances)
        try:
            _remote_client.delete_driver(integration.driver_id)
            _LOG.info("Deleted existing driver: %s", integration.driver_id)
        except SyncAPIError as e:
            error_str = str(e).lower()
            # Check if this is a connection/network error
            if any(
                x in error_str
                for x in ["connection", "disconnect", "timeout", "network"]
            ):
                _LOG.error(
                    "Connection error while deleting driver %s: %s",
                    integration.driver_id,
                    e,
                )
                with _operation_lock:
                    _operation_in_progress = False
                    _LOG.info(
                        "Lock released due to connection error for instance %s",
                        instance_id,
                    )
                return (
                    f"""
                    <span class="inline-flex items-center gap-1 text-red-400 text-sm" title="Connection error: {str(e).replace('"', "&quot;")}">
                        <i class="fas fa-exclamation-circle"></i>
                        Connection Failed
                    </span>
                """,
                    500,
                )
            # For other errors, log warning and continue
            _LOG.warning("Failed to delete driver, continuing anyway: %s", e)

        # Install the new version
        _remote_client.install_integration(archive_data, filename)
        _LOG.info("Updated integration %s successfully", integration.name)

        # Brief pause to let installation settle
        time.sleep(API_DELAY * 2)

        # Post-installation verification - give the remote time to process the driver
        _LOG.debug("Waiting for driver to be ready: %s", integration.driver_id)
        _remote_client.get_drivers()  # Verify driver is available

        # Additional pause to ensure driver is fully initialized
        time.sleep(API_DELAY * 3)

        # Get current version once after installation for migration use
        current_version = ""
        should_check_migration = False

        if previous_version:
            # Get current version for migration checks
            driver_info = _remote_client.get_driver(integration.driver_id)
            if driver_info:
                current_version = driver_info.get("version", "")
                _LOG.info(
                    "Installed version: %s (upgrading from %s)",
                    current_version,
                    previous_version,
                )

            # Check if migration is needed based on registry's migration_required_at
            if migration_required_at:
                # Compare versions to see if migration is still needed
                try:
                    if Version(previous_version) < Version(migration_required_at):
                        should_check_migration = True
                        _LOG.info(
                            "Previous version %s is less than %s - will check for migration",
                            previous_version,
                            migration_required_at,
                        )
                    else:
                        _LOG.info(
                            "Previous version %s is greater than or equal to %s - migration already completed, skipping",
                            previous_version,
                            migration_required_at,
                        )
                except Exception as e:
                    _LOG.warning(
                        "Failed to compare versions, will check for migration anyway: %s",
                        e,
                    )
                    should_check_migration = True
            else:
                _LOG.info(
                    "No migration_required_at in registry for %s - no migration needed",
                    integration.driver_id,
                )

        # Restore configuration if backup data exists (only for configured instances)
        if backup_data and is_configured:
            try:
                _LOG.info(
                    "Starting configuration restore for %s", integration.driver_id
                )

                # Step 1: Start setup for restore
                _remote_client.start_setup(integration.driver_id, reconfigure=False)
                _LOG.info("Started setup for restore (reconfigure=false)")

                time.sleep(API_DELAY * 4)  # Give more time for setup to initialize

                # Step 1a: Check for migration metadata in the setup response
                setup_response = _remote_client.get_setup(integration.driver_id)
                _LOG.debug("Initial setup response: %s", setup_response)

                # Check if setup is in the right state
                setup_state = setup_response.get("state", "")
                if setup_state != "WAIT_USER_ACTION":
                    _LOG.warning(
                        "Setup not ready yet (state: %s), waiting longer...",
                        setup_state,
                    )
                    time.sleep(API_DELAY * 2)
                    setup_response = _remote_client.get_setup(integration.driver_id)
                    setup_state = setup_response.get("state", "")
                    _LOG.debug("Setup response after wait: %s", setup_response)

                migration_required = (
                    None  # None = unknown, True = required, False = not required
                )
                migration_possible = False
                migration_mappings = []

                # Only check for migration if registry indicates it might be needed
                if previous_version and should_check_migration:
                    _LOG.info(
                        "Checking for migration metadata (previous_version: %s Migration Required: %s)",
                        previous_version,
                        should_check_migration,
                    )

                    # Look for migration_required and migration_possible in settings
                    settings = (
                        setup_response.get("require_user_action", {})
                        .get("input", {})
                        .get("settings", [])
                    )

                    _LOG.debug("Found %d settings in setup response", len(settings))

                    # Log all setting IDs to help debug
                    setting_ids = [s.get("id") for s in settings]
                    _LOG.debug("Setting IDs in response: %s", setting_ids)

                    for setting in settings:
                        setting_id = setting.get("id")

                        if setting_id == "migration_possible":
                            # This indicates the integration supports migration (has get_migration_data override)
                            migration_possible = True
                            _LOG.info(
                                "Migration is possible for %s - integration supports migration",
                                integration.driver_id,
                            )
                        elif setting_id == "migration_required":
                            # Get the previous_version value from the label
                            label_value = (
                                setting.get("field", {})
                                .get("label", {})
                                .get("value", "")
                            )
                            # If there's a value, migration is required
                            migration_required = bool(label_value)
                            _LOG.info(
                                "Migration metadata found: migration_required=%s (previous_version from field: %s)",
                                migration_required,
                                label_value,
                            )

                    if not migration_possible:
                        _LOG.info(
                            "Integration %s does not support migration (no migration_possible field)",
                            integration.driver_id,
                        )
                    elif migration_required is False:
                        _LOG.info(
                            "Migration explicitly not required for %s",
                            integration.driver_id,
                        )
                    elif migration_required is None:
                        _LOG.debug(
                            "Migration requirement unknown for %s (will attempt if possible)",
                            integration.driver_id,
                        )
                    else:
                        _LOG.info(
                            "Migration IS required for %s - will execute after restore",
                            integration.driver_id,
                        )
                elif previous_version and not should_check_migration:
                    _LOG.info(
                        "Skipping migration check - registry indicates migration not needed or already completed"
                    )
                else:
                    _LOG.info("No previous_version provided - skipping migration check")

                # Step 2: PUT /intg/setup/{driver_id} with restore_from_backup="true"
                _remote_client.send_setup_input(
                    integration.driver_id, {"restore_from_backup": "true"}
                )
                _LOG.info("Initiated restore mode")

                # Brief pause between API calls
                time.sleep(API_DELAY * 2)

                # Step 3: PUT /intg/setup/{driver_id} with restore data
                # The backup_data is a JSON string that needs to be properly escaped
                try:
                    # Parse the backup data to ensure it's valid JSON, then re-serialize for proper escaping
                    parsed_backup = json.loads(backup_data)
                    escaped_backup_data = json.dumps(parsed_backup)
                except json.JSONDecodeError as e:
                    _LOG.warning("Backup data is not valid JSON, using as-is: %s", e)
                    escaped_backup_data = backup_data

                _remote_client.send_setup_input(
                    integration.driver_id,
                    {
                        "restore_from_backup": "true",
                        "restore_data": escaped_backup_data,
                    },
                )

                time.sleep(API_DELAY * 6)

                # Post-restore verification calls (like official tool)
                _LOG.info(
                    "Performing post-restore verification for %s", integration.driver_id
                )
                _remote_client.get_enabled_integrations()

                # Get enabled instances and find our restored instance
                enabled_instances = _remote_client.get_enabled_instances()
                restored_instance_id = None
                for instance in enabled_instances:
                    if instance.get("driver_id") == integration.driver_id:
                        restored_instance_id = instance.get("integration_id")
                        _LOG.info(
                            "Found restored instance: %s for driver %s",
                            restored_instance_id,
                            integration.driver_id,
                        )
                        break

                _remote_client.get_instantiable_drivers()
                _remote_client.get_driver(integration.driver_id)

                # Get the specific instance to verify it's CONNECTED
                if restored_instance_id:
                    instance_detail = _remote_client.get_instance(restored_instance_id)
                    device_state = instance_detail.get("device_state", "UNKNOWN")
                    _LOG.info(
                        "Instance %s state: %s", restored_instance_id, device_state
                    )

                # Complete the setup flow twice (like official tool)
                _remote_client.complete_setup(integration.driver_id)

                # Final verification call after DELETE (like official tool)
                _remote_client.get_enabled_instances()

                _LOG.info(
                    "Configuration restored successfully for %s", integration.driver_id
                )

                # Step 4: Register ALL entities before migration (only if migration is possible)
                # Migration needs entities to exist on Remote to update activities
                all_entities = []
                if migration_possible and restored_instance_id:
                    _remote_client.get_enabled_instances()

                    all_entities = _remote_client.get_instance_entities(
                        restored_instance_id
                    )
                    _LOG.info(
                        "Retrieved %d total entities for instance %s",
                        len(all_entities),
                        restored_instance_id,
                    )

                    # Register ALL entities (not just configured ones)
                    # This ensures entities exist when migration runs
                    if all_entities:
                        all_entity_ids: list[str] = [
                            str(e.get("entity_id"))
                            for e in all_entities
                            if e.get("entity_id")
                        ]
                        time.sleep(API_DELAY * 5)
                        _LOG.info(
                            "Registering ALL %d entities before migration for instance %s",
                            len(all_entity_ids),
                            restored_instance_id,
                        )
                        _LOG.debug("All entity IDs: %s", all_entity_ids)

                        try:
                            _remote_client.register_entities(
                                restored_instance_id, all_entity_ids
                            )
                            _LOG.info(
                                "Successfully registered all %d entities",
                                len(all_entity_ids),
                            )
                        except SyncAPIError as e:
                            _LOG.warning(
                                "Failed to register all entities for instance %s: %s",
                                restored_instance_id,
                                e,
                            )

                # Step 5: Execute migration if possible and not explicitly not required
                # migration_required: None (unknown) or True = proceed, False = skip
                if (
                    migration_possible
                    and migration_required is not False
                    and previous_version
                    and restored_instance_id
                ):
                    try:
                        _LOG.info(
                            "Migration flow starting for %s (previous_version: %s, migration_required: %s)",
                            integration.driver_id,
                            previous_version,
                            migration_required
                            if migration_required is not None
                            else "to be determined",
                        )

                        # POST with reconfigure=true to get to the configuration mode screen
                        _remote_client.start_setup(
                            integration.driver_id, reconfigure=True
                        )
                        _LOG.info("Started setup mode for migration")

                        time.sleep(API_DELAY)

                        # GET to read the configuration mode screen
                        setup_response = _remote_client.get_setup(integration.driver_id)
                        _LOG.debug("Setup response for migration: %s", setup_response)

                        # Extract the choice ID (current device)
                        settings = (
                            setup_response.get("require_user_action", {})
                            .get("input", {})
                            .get("settings", [])
                        )
                        choice_id = None
                        for setting in settings:
                            if setting.get("id") == "choice":
                                dropdown = setting.get("field", {}).get("dropdown", {})
                                choice_id = dropdown.get("value")
                                break

                        if not choice_id:
                            _LOG.warning("No choice ID found for migration")
                            raise ValueError("No device choice found")

                        # Step 4a: Select "migrate" action with the choice
                        _remote_client.send_setup_input(
                            integration.driver_id,
                            {"choice": choice_id, "action": "migrate"},
                        )
                        _LOG.debug(
                            "Selected 'migrate' action for device: %s", choice_id
                        )

                        time.sleep(API_DELAY * 2)

                        # Step 4b: GET the next setup page after selecting migrate
                        setup_response = _remote_client.get_setup(integration.driver_id)
                        _LOG.debug(
                            "Setup response after selecting migrate: %s", setup_response
                        )

                        # Check if setup is in the right state
                        setup_state = setup_response.get("state", "")
                        if setup_state != "WAIT_USER_ACTION":
                            _LOG.warning(
                                "Setup not in WAIT_USER_ACTION after selecting migrate (state: %s)",
                                setup_state,
                            )
                            raise ValueError(
                                f"Unexpected setup state after migrate: {setup_state}"
                            )

                        # Step 4c: Send previous_version
                        _remote_client.send_setup_input(
                            integration.driver_id,
                            {"previous_version": previous_version},
                        )
                        _LOG.debug("Sent previous_version: %s", previous_version)

                        time.sleep(API_DELAY * 2)

                        # GET the migration execution screen (asks for remote_url, pin, etc.)
                        setup_response = _remote_client.get_setup(integration.driver_id)
                        _LOG.debug("Migration execution screen: %s", setup_response)

                        # Prepare migration data
                        remote_url = _remote_client._address or "http://localhost"
                        remote_api_key = _remote_client._api_key or ""

                        _LOG.info(
                            "Executing migration for %s (from %s to %s)",
                            integration.driver_id,
                            previous_version,
                            current_version,
                        )

                        # Build migration input - only include fields that have values
                        # The integration will try to fetch current_version from Remote API,
                        # but if that fails, having it provided prevents errors
                        migration_input = {
                            "previous_version": previous_version,
                            "current_version": current_version,
                            "remote_url": remote_url,
                            "pin": "",
                            "automated": "true",
                        }

                        # Only include api_key if it's not empty (avoid sending empty form fields)
                        if remote_api_key:
                            migration_input["api_key"] = remote_api_key

                        _LOG.info(
                            "Sending migration data: remote_url=%s, api_key=%s, previous_version=%s, current_version=%s",
                            remote_url,
                            "****" if remote_api_key else "(not provided)",
                            previous_version,
                            current_version,
                        )

                        _remote_client.send_setup_input(
                            integration.driver_id, migration_input
                        )
                        _LOG.debug("Migration execution data sent successfully")

                        time.sleep(
                            API_DELAY * 4
                        )  # Give more time for migration to process

                        # GET to read the migration mappings response
                        setup_response = _remote_client.get_setup(integration.driver_id)
                        _LOG.debug("Migration mappings response: %s", setup_response)

                        # Check the state of the response
                        setup_state = setup_response.get("state", "")
                        _LOG.debug("Migration response state: %s", setup_state)

                        if setup_state == "ERROR":
                            error_type = setup_response.get("error", "UNKNOWN")
                            _LOG.error(
                                "Migration failed with error state: %s. This could mean:\n"
                                "  - Integration couldn't connect to Remote at %s\n"
                                "  - Invalid PIN provided\n"
                                "  - Integration encountered an error during migration\n"
                                "  - Check integration logs for more details",
                                error_type,
                                remote_url,
                            )
                            # Don't raise - try to extract any mappings that might be present

                        # Extract migration mappings from the response
                        migration_mappings = extract_migration_mappings(setup_response)

                        _LOG.debug(
                            "Found %d entity mappings: %s",
                            len(migration_mappings),
                            migration_mappings,
                        )

                        if migration_mappings:
                            # Update configured_entity_ids list with migrated IDs
                            # DON'T modify backup_data - keep it original for potential rollback
                            _LOG.debug(
                                "Updating configured_entity_ids with migration mappings. Original: %s",
                                configured_entity_ids,
                            )
                            mapping_dict = {
                                m["previous_entity_id"]: m["new_entity_id"]
                                for m in migration_mappings
                            }
                            updated_entity_ids = []
                            for entity_id in configured_entity_ids:
                                if entity_id in mapping_dict:
                                    updated_entity_ids.append(mapping_dict[entity_id])
                                    _LOG.info(
                                        "Mapped entity: %s -> %s",
                                        entity_id,
                                        mapping_dict[entity_id],
                                    )
                                else:
                                    updated_entity_ids.append(entity_id)
                            configured_entity_ids = updated_entity_ids
                            _LOG.info(
                                "Updated configured_entity_ids: %s",
                                configured_entity_ids,
                            )

                            # Check final result - should be SETUP_COMPLETE or show mappings
                            migration_state = setup_response.get("state", "")

                            if migration_state == "SETUP_COMPLETE":
                                _LOG.info(
                                    "Migration completed successfully for %s",
                                    integration.driver_id,
                                )
                            elif migration_state == "SETUP_ERROR":
                                error_msg = setup_response.get("error", "Unknown error")
                                _LOG.error(
                                    "Migration failed for %s: %s",
                                    integration.driver_id,
                                    error_msg,
                                )
                            else:
                                _LOG.info(
                                    "Migration processing complete for %s",
                                    integration.driver_id,
                                )
                        else:
                            _LOG.warning(
                                "No migration mappings found for %s",
                                integration.driver_id,
                            )

                        # Complete migration setup flow
                        _remote_client.complete_setup(integration.driver_id)

                    except Exception as e:
                        _LOG.warning(
                            "Failed to execute migration for %s: %s",
                            integration.driver_id,
                            e,
                        )

                # Step 6: Register configured entities
                if restored_instance_id and register_entities and configured_entity_ids:
                    time.sleep(API_DELAY * 2)

                    # If migration was possible, we registered ALL entities earlier
                    # Now we need to clean up by deleting all and re-registering only configured ones
                    if migration_possible:
                        _LOG.info(
                            "Cleaning up entities for %s - will keep only configured entities",
                            restored_instance_id,
                        )

                        try:
                            # Delete ALL entities for this integration
                            _LOG.info(
                                "Deleting all entities for instance %s",
                                restored_instance_id,
                            )
                            _remote_client.delete_all_entities(restored_instance_id)
                            _LOG.info("All entities deleted")

                            time.sleep(API_DELAY * 2)

                            # Re-register only the configured entities (now with updated IDs from migration)
                            _LOG.info(
                                "Re-registering %d configured entities for instance %s",
                                len(configured_entity_ids),
                                restored_instance_id,
                            )
                            _LOG.info(
                                "Configured entity IDs: %s", configured_entity_ids
                            )

                            _remote_client.register_entities(
                                restored_instance_id, configured_entity_ids
                            )

                            _LOG.info(
                                "Successfully registered %d configured entities",
                                len(configured_entity_ids),
                            )
                        except SyncAPIError as e:
                            _LOG.warning(
                                "Failed to clean up entities for instance %s: %s",
                                restored_instance_id,
                                e,
                            )
                    else:
                        # No migration possible - just register configured entities directly
                        _LOG.info(
                            "Registering %d configured entities for instance %s",
                            len(configured_entity_ids),
                            restored_instance_id,
                        )
                        _LOG.info("Configured entity IDs: %s", configured_entity_ids)

                        try:
                            _remote_client.register_entities(
                                restored_instance_id, configured_entity_ids
                            )

                            _LOG.info(
                                "Successfully registered %d configured entities",
                                len(configured_entity_ids),
                            )
                        except SyncAPIError as e:
                            _LOG.warning(
                                "Failed to register entities for instance %s: %s",
                                restored_instance_id,
                                e,
                            )

                _LOG.info("Update completed successfully for %s", integration.driver_id)

            except SyncAPIError as e:
                _LOG.error(
                    "Failed to restore configuration for %s: %s",
                    integration.driver_id,
                    e,
                )
                # Try to clean up setup flow even on failure (twice like official tool)
                try:
                    _remote_client.complete_setup(integration.driver_id)
                    # Final verification call after double DELETE
                    _remote_client.get_enabled_instances()
                    time.sleep(API_DELAY)  # Brief pause after cleanup
                except SyncAPIError:
                    pass
            except Exception as e:
                _LOG.error(
                    "Unexpected error during restore for %s: %s",
                    integration.driver_id,
                    e,
                )
                # Try to clean up setup flow even on failure (twice like official tool)
                try:
                    _remote_client.complete_setup(integration.driver_id)
                    _remote_client.complete_setup(integration.driver_id)
                    # Final verification call after double DELETE
                    _remote_client.get_enabled_instances()
                    time.sleep(API_DELAY)  # Brief pause after cleanup
                except SyncAPIError:
                    pass

        # Update the cache entry for this driver instead of full refresh
        # This avoids GitHub rate limiting issues
        if integration.driver_id in _cached_version_data:
            _cached_version_data[integration.driver_id]["has_update"] = False
            _cached_version_data[integration.driver_id]["current"] = (
                _cached_version_data[integration.driver_id]["latest"]
            )
            _LOG.debug(
                "Updated cache for %s: marked as current version", integration.driver_id
            )

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after successful update of instance %s", instance_id
            )

        # Brief delay to ensure remote has processed the update
        time.sleep(API_DELAY)

        # Re-fetch the integration info with updated version
        integrations = _get_installed_integrations()
        updated_integration = next(
            (i for i in integrations if i.driver_id == integration.driver_id), None
        )

        if updated_integration:
            # Return the updated card HTML
            settings = Settings.load()
            remote_ip = _remote_client._address if _remote_client else None
            return render_template(
                "partials/integration_card.html",
                integration=updated_integration,
                remote_ip=remote_ip,
                settings=settings,
                just_updated=True,
            )
        else:
            # Fallback: use original integration data with updated flag
            # This shouldn't normally happen, but ensures we return a card
            _LOG.warning(
                "Could not find updated integration %s, using original data",
                integration.driver_id,
            )
            settings = Settings.load()
            remote_ip = _remote_client._address if _remote_client else None
            return render_template(
                "partials/integration_card.html",
                integration=integration,
                remote_ip=remote_ip,
                settings=settings,
                just_updated=True,
            )

    except SyncAPIError as e:
        _LOG.error("Update failed: %s", e)
        error_msg = str(e).replace('"', "&quot;")

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after SyncAPIError in update_integration for instance %s",
                instance_id,
            )

        return (
            f'''
            <span class="inline-flex items-center gap-1 text-red-400 text-sm" title="{error_msg}">
                <i class="fas fa-exclamation-circle"></i>
                Failed
            </span>
        ''',
            500,
        )
    except Exception as e:
        _LOG.error("Unexpected error during update: %s", e)
        error_msg = str(e).replace('"', "&quot;")

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after generic exception in update_integration for instance %s",
                instance_id,
            )

        return (
            f'''
            <span class="inline-flex items-center gap-1 text-red-400 text-sm" title="{error_msg}">
                <i class="fas fa-exclamation-circle"></i>
                Failed
            </span>
        ''',
            500,
        )


@app.route("/api/driver/<driver_id>/update", methods=["POST"])
def update_driver(driver_id: str):
    """
    Update an unconfigured driver to the latest or specified version.

    Accepts optional 'version' query parameter to update to a specific version.

    This is used when a driver is installed but not configured (no instance exists).
    Since there's no instance, there's nothing to backup or restore - just download
    and install the new version.
    """
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    # Get optional version parameter from query string or form data
    version = request.args.get("version") or request.form.get("version")

    # Check if another operation is in progress
    global _operation_in_progress
    with _operation_lock:
        _LOG.info(
            "Lock check for driver %s: _operation_in_progress=%s",
            driver_id,
            _operation_in_progress,
        )
        if _operation_in_progress:
            _LOG.warning("Update blocked for driver %s - lock is held", driver_id)
            return jsonify(
                {"status": "error", "message": "Another install/upgrade is in progress"}
            ), 409
        _operation_in_progress = True
        _LOG.info("Lock acquired for updating driver %s", driver_id)

    try:
        # Find the driver to get its GitHub URL
        integrations = _get_installed_integrations()
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        if not integration:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - driver %s not found", driver_id)
            return jsonify({"status": "error", "message": "Driver not found"}), 404

        if integration.official:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - driver %s is official", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "Official integrations are managed by firmware updates",
                }
            ), 400

        if not integration.home_page or "github.com" not in integration.home_page:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - driver %s has no GitHub URL", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this driver",
                }
            ), 400

        # Check migration boundary if version specified
        if version:
            try:
                registry = load_registry()
                for entry in registry:
                    if (
                        entry.get("id") == driver_id
                        or entry.get("driver_id") == driver_id
                    ):
                        migration_required_at = entry.get("migration_required_at")
                        if migration_required_at:
                            clean_version = version.lstrip("v")
                            if Version(clean_version) <= Version(migration_required_at):
                                with _operation_lock:
                                    _operation_in_progress = False
                                _LOG.warning(
                                    "Update blocked for %s - version %s violates migration boundary %s",
                                    driver_id,
                                    version,
                                    migration_required_at,
                                )
                                return jsonify(
                                    {
                                        "status": "error",
                                        "message": f"Cannot downgrade to version {version} - requires version > {migration_required_at}",
                                    }
                                ), 400
                        break
            except (InvalidVersion, Exception) as e:
                with _operation_lock:
                    _operation_in_progress = False
                _LOG.warning("Version validation failed for %s: %s", version, e)
                return jsonify(
                    {"status": "error", "message": f"Invalid version: {version}"}
                ), 400

        # Parse GitHub URL
        parsed = SyncGitHubClient.parse_github_url(integration.home_page)
        if not parsed:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - could not parse GitHub URL for driver %s",
                    driver_id,
                )
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Download the specified or latest release
        if version:
            _LOG.info("Updating driver %s to version %s", driver_id, version)
            download_result = _github_client.download_release_asset(
                owner, repo, version=version
            )
        else:
            _LOG.info("Updating driver %s to latest version", driver_id)
            download_result = _github_client.download_release_asset(owner, repo)
        if not download_result:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - no release found for driver %s", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": f"No tar.gz release found for {owner}/{repo}",
                }
            ), 404

        archive_data, filename = download_result
        _LOG.info("Downloaded %s (%d bytes) for update", filename, len(archive_data))

        # Delete the existing driver
        try:
            _remote_client.delete_driver(driver_id)
            _LOG.info("Deleted existing driver: %s", driver_id)
        except SyncAPIError as e:
            error_str = str(e).lower()
            # Check if this is a connection/network error
            if any(
                x in error_str
                for x in ["connection", "disconnect", "timeout", "network"]
            ):
                _LOG.error(
                    "Connection error while deleting driver %s: %s", driver_id, e
                )
                with _operation_lock:
                    _operation_in_progress = False
                    _LOG.info(
                        "Lock released due to connection error for driver %s", driver_id
                    )
                return (
                    f"""
                    <span class="inline-flex items-center gap-1 text-red-400 text-sm" title="Connection error: {str(e).replace('"', "&quot;")}">
                        <i class="fas fa-exclamation-circle"></i>
                        Connection Failed
                    </span>
                """,
                    500,
                )
            # For other errors, log warning and continue
            _LOG.warning("Failed to delete driver, continuing anyway: %s", e)

        # Install the new version
        _remote_client.install_integration(archive_data, filename)
        _LOG.info("Updated driver %s successfully", integration.name)

        # Wait for the specific driver to appear in the driver list
        # Poll up to 10 times (5 seconds total) to ensure new driver is registered
        driver_found = False
        for attempt in range(10):
            time.sleep(0.5)
            try:
                drivers = _remote_client.get_drivers()
                if any(d.get("driver_id") == driver_id for d in drivers):
                    driver_found = True
                    _LOG.debug(
                        "Driver %s found after %d attempts", driver_id, attempt + 1
                    )
                    break
            except Exception as e:
                _LOG.debug("Attempt %d to verify driver failed: %s", attempt + 1, e)

        if not driver_found:
            _LOG.warning(
                "Driver %s not found in driver list after update, cache may be stale",
                driver_id,
            )

        # Additional delay to ensure driver info has fully propagated
        time.sleep(1.0)

        # Update just this driver's cache entry instead of refreshing everything
        # This avoids GitHub rate limiting issues from rapid consecutive API calls
        if driver_id in _cached_version_data:
            # Driver was updated to latest version, so no update is available anymore
            _cached_version_data[driver_id]["has_update"] = False
            _cached_version_data[driver_id]["current"] = _cached_version_data[
                driver_id
            ]["latest"]
            _LOG.debug("Updated cache for %s: marked as current version", driver_id)

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info("Lock released after successful update of driver %s", driver_id)

        # Brief delay to ensure remote has processed the update
        time.sleep(API_DELAY)

        # Re-fetch the integration info with updated version from available list
        # Since this is for unconfigured drivers, we use _get_available_integrations
        available = _get_available_integrations()
        updated_integration = next(
            (i for i in available if i.driver_id == driver_id), None
        )

        remote_ip = _remote_client._address if _remote_client else None

        if updated_integration:
            # Return the updated card HTML for available list
            return render_template(
                "partials/available_card.html",
                integration=updated_integration,
                remote_ip=remote_ip,
                just_updated=True,
            )
        else:
            # Fallback: Try to find it in installed integrations list
            # This shouldn't normally happen, but ensures we return a card
            _LOG.warning(
                "Could not find updated driver %s in available list, checking installed",
                driver_id,
            )
            integrations = _get_installed_integrations()
            integration = next(
                (i for i in integrations if i.driver_id == driver_id), None
            )

            if integration:
                settings = Settings.load()
                return render_template(
                    "partials/integration_card.html",
                    integration=integration,
                    remote_ip=remote_ip,
                    settings=settings,
                    just_updated=True,
                )
            else:
                # Last resort: Create a minimal AvailableIntegration from registry
                _LOG.error("Could not find driver %s anywhere after update", driver_id)
                registry = load_registry()
                registry_item = next(
                    (item for item in registry if item.get("id") == driver_id), {}
                )
                categories_list = registry_item.get("categories", [])
                fallback_integration = AvailableIntegration(
                    driver_id=driver_id,
                    name=registry_item.get("name", driver_id),
                    description=registry_item.get("description", ""),
                    icon=registry_item.get("icon", "puzzle-piece"),
                    home_page=registry_item.get("repository", ""),
                    developer=registry_item.get("author", ""),
                    version="",
                    category=categories_list[0] if categories_list else "",
                    categories=categories_list,
                    installed=False,
                    driver_installed=True,
                    external=False,
                    custom=True,
                    official=False,
                    update_available=False,
                    latest_version="",
                    instance_id="",
                    can_update=False,
                )
                return render_template(
                    "partials/available_card.html",
                    integration=fallback_integration,
                    remote_ip=remote_ip,
                    just_updated=True,
                )

    except SyncAPIError as e:
        _LOG.error("Update failed: %s", e)
        error_msg = str(e).replace('"', "&quot;")

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after SyncAPIError in update_driver for driver %s",
                driver_id,
            )

        return (
            f'''
            <span class="inline-flex items-center gap-1 text-red-400 text-sm" title="{error_msg}">
                <i class="fas fa-exclamation-circle"></i>
                Failed
            </span>
        ''',
            500,
        )
    except Exception as e:
        _LOG.error("Unexpected error during update: %s", e)
        error_msg = str(e).replace('"', "&quot;")

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after generic exception in update_driver for driver %s",
                driver_id,
            )

        return (
            f'''
            <span class="inline-flex items-center gap-1 text-red-400 text-sm" title="{error_msg}">
                <i class="fas fa-exclamation-circle"></i>
                Failed
            </span>
        ''',
            500,
        )


@app.route("/api/integration/<driver_id>/update-confirm")
def get_update_confirmation(driver_id: str):
    """
    Get update confirmation modal for integrations without backup support.

    Returns HTML warning that configuration cannot be preserved.
    """
    if not _remote_client:
        return "<p class='text-red-400'>Service not initialized</p>"

    try:
        # Get integration details
        integrations = _get_installed_integrations()
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        if not integration:
            return "<p class='text-red-400'>Integration not found</p>"

        # Load registry to check backup requirements
        registry = load_registry()
        registry_item = None
        for entry in registry:
            if entry.get("driver_id") == driver_id or entry.get("id") == driver_id:
                registry_item = entry
                break

        # Determine the reason for no backup
        reason = "no_backup_support"
        min_version = None
        if registry_item:
            min_version = registry_item.get("backup_min_version")
            if min_version and integration.version:
                try:
                    if Version(integration.version) < Version(min_version):
                        reason = "version_too_old"
                except (InvalidVersion, TypeError):
                    pass

        # Determine update URL based on whether there's an instance
        if integration.instance_id:
            update_url = f"/api/integration/{integration.instance_id}/update?version={integration.latest_version}"
            update_target = f"#card-{driver_id}"
        else:
            update_url = (
                f"/api/driver/{driver_id}/update?version={integration.latest_version}"
            )
            update_target = f"#card-{driver_id}"

        return render_template(
            "partials/modal_update_no_backup.html",
            driver_id=driver_id,
            integration_name=integration.name,
            current_version=integration.version,
            new_version=integration.latest_version,
            min_version=min_version,
            reason=reason,
            update_url=update_url,
            update_target=update_target,
            update_indicator=f"#upgrade-overlay-{driver_id}",
        )
    except Exception as e:
        _LOG.error("Error loading update confirmation for %s: %s", driver_id, e)
        return f"<p class='text-red-400'>Error: {str(e)}</p>"


@app.route("/api/integration/<driver_id>/delete-confirm")
def get_delete_confirmation(driver_id: str):
    """
    Get delete confirmation modal content for an integration.

    Returns HTML to be displayed in the modal with delete options.
    """
    if not _remote_client:
        return "<p class='text-red-400'>Service not initialized</p>"

    try:
        # Get integration name for display
        integrations = _get_installed_integrations()
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        # Also check available list for unconfigured drivers
        if not integration:
            available = _get_available_integrations()
            integration = next((i for i in available if i.driver_id == driver_id), None)

        integration_name = integration.name if integration else driver_id

        # Determine if integration is configured (has an instance)
        is_configured = False
        if integration:
            # For IntegrationInfo, check if it has an instance_id and is not NOT_CONFIGURED
            if hasattr(integration, "instance_id") and hasattr(integration, "state"):
                is_configured = (
                    bool(integration.instance_id)
                    and integration.state != "NOT_CONFIGURED"
                )
            # For AvailableIntegration, check the installed flag
            elif hasattr(integration, "installed"):
                is_configured = integration.installed

        return render_template(
            "partials/modal_delete_confirm.html",
            driver_id=driver_id,
            integration_name=integration_name,
            is_configured=is_configured,
        )
    except Exception as e:
        _LOG.error("Error loading delete confirmation for %s: %s", driver_id, e)
        return f"<p class='text-red-400'>Error: {str(e)}</p>"


@app.route("/api/integration/<driver_id>/delete", methods=["DELETE"])
def delete_integration(driver_id: str):
    """
    Delete an integration - either just the configuration or the entire integration.

    Query parameters:
    - type: 'configuration' or 'full'
    """
    if not _remote_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    delete_type = request.args.get("type", "configuration")
    _LOG.info("Delete request for %s: type=%s", driver_id, delete_type)

    try:
        # Build the instance_id (driver_id + ".main")
        instance_id = f"{driver_id}.main"

        # Delete the instance/configuration
        try:
            _remote_client.delete_instance(instance_id)
            _LOG.info("Deleted instance: %s", instance_id)
        except SyncAPIError as e:
            # If instance doesn't exist, that's okay - might be unconfigured
            _LOG.warning("Failed to delete instance %s: %s", instance_id, e)

        # If full delete, also delete the driver
        if delete_type == "full":
            # Small delay to let instance deletion complete
            time.sleep(API_DELAY * 2)

            try:
                _remote_client.delete_driver(driver_id)
                _LOG.info("Deleted driver: %s", driver_id)
            except SyncAPIError as e:
                _LOG.error("Failed to delete driver %s: %s", driver_id, e)
                return jsonify(
                    {"status": "error", "message": f"Failed to delete driver: {e}"}
                ), 500

        # Small delay to ensure remote has processed
        time.sleep(API_DELAY)

        # Return updated card or empty response
        if delete_type == "full":
            # Full delete - return empty (card will be removed)
            return "", 200
        else:
            # Configuration delete - return updated card showing unconfigured state
            integrations = _get_installed_integrations()
            integration = next(
                (i for i in integrations if i.driver_id == driver_id), None
            )

            if integration:
                settings = Settings.load()
                remote_ip = _remote_client._address if _remote_client else None
                return render_template(
                    "partials/integration_card.html",
                    integration=integration,
                    remote_ip=remote_ip,
                    settings=settings,
                )
            else:
                # Driver might have been removed, return empty
                return "", 200

    except Exception as e:
        _LOG.error("Unexpected error during delete for %s: %s", driver_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


def _build_error_card(driver_id: str, registry: list, error_msg: str) -> str:
    """Build an error card HTML for a failed install."""
    registry_item = next((item for item in registry if item.get("id") == driver_id), {})

    # Convert registry item to AvailableIntegration structure
    categories_list = registry_item.get("categories", [])
    integration = AvailableIntegration(
        driver_id=driver_id,
        name=registry_item.get("name", driver_id),
        description=registry_item.get("description", ""),
        icon=registry_item.get("icon", "puzzle-piece"),
        home_page=registry_item.get("repository", ""),
        developer=registry_item.get("author", ""),
        version="",
        category=categories_list[0] if categories_list else "",
        categories=categories_list,
        installed=False,
        driver_installed=False,
        external=False,
        custom=True,
        official=False,
        update_available=False,
        latest_version="",
        instance_id="",
        can_update=False,
    )

    remote_ip = _remote_client._address if _remote_client else None
    return render_template(
        "partials/available_card.html",
        integration=integration,
        remote_ip=remote_ip,
        install_error=error_msg,
    )


@app.route("/api/integration/<driver_id>/install", methods=["POST"])
def install_integration(driver_id: str):
    """
    Install a new integration from the registry.

    Accepts optional 'version' query parameter to install a specific version.

    Process:
    1. Look up the integration in the registry by driver_id
    2. Get the GitHub repo URL
    3. Download the specified (or latest) release tar.gz
    4. Validate against migration boundary if version specified
    5. Upload and install on the remote
    """
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    # Get optional version parameter from query string or form data
    version = request.args.get("version") or request.form.get("version")

    # Check if another operation is in progress
    global _operation_in_progress
    with _operation_lock:
        _LOG.info(
            "Lock check for install %s: _operation_in_progress=%s",
            driver_id,
            _operation_in_progress,
        )
        if _operation_in_progress:
            _LOG.warning("Install blocked for %s - lock is held", driver_id)
            return jsonify(
                {"status": "error", "message": "Another install/upgrade is in progress"}
            ), 409
        _operation_in_progress = True
        _LOG.info("Lock acquired for installing %s", driver_id)

    try:
        # Find the integration in the registry
        registry = load_registry()
        integration = next(
            (item for item in registry if item.get("id") == driver_id), None
        )

        if not integration:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - integration %s not found in registry", driver_id
                )
            return jsonify(
                {"status": "error", "message": "Integration not found in registry"}
            ), 404

        # Check migration boundary if version specified
        migration_required_at = integration.get("migration_required_at")
        if version and migration_required_at:
            # Clean version string
            clean_version = version.lstrip("v")
            try:
                if Version(clean_version) <= Version(migration_required_at):
                    with _operation_lock:
                        _operation_in_progress = False
                    _LOG.warning(
                        "Install blocked for %s - version %s violates migration boundary %s",
                        driver_id,
                        version,
                        migration_required_at,
                    )
                    return jsonify(
                        {
                            "status": "error",
                            "message": f"Cannot install version {version} - requires version > {migration_required_at}",
                        }
                    ), 400
            except InvalidVersion as e:
                with _operation_lock:
                    _operation_in_progress = False
                _LOG.warning("Invalid version format %s: %s", version, e)
                return jsonify(
                    {"status": "error", "message": f"Invalid version format: {version}"}
                ), 400

        repo_url = integration.get("repository", "")
        if not repo_url or "github.com" not in repo_url:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - no GitHub URL for integration %s", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this integration",
                }
            ), 400

        # Parse GitHub URL
        parsed = SyncGitHubClient.parse_github_url(repo_url)
        if not parsed:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - could not parse GitHub URL for integration %s",
                    driver_id,
                )
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Download the specified or latest release
        if version:
            _LOG.info("Installing %s version %s", driver_id, version)
            download_result = _github_client.download_release_asset(
                owner, repo, version=version
            )
        else:
            _LOG.info("Installing latest version of %s", driver_id)
            download_result = _github_client.download_release_asset(owner, repo)
        if not download_result:
            with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - no release found for integration %s", driver_id
                )
            return jsonify(
                {
                    "status": "error",
                    "message": f"No tar.gz release found for {owner}/{repo}. "
                    "This integration may not have a release available.",
                }
            ), 404

        archive_data, filename = download_result
        _LOG.info("Downloaded %s (%d bytes) for install", filename, len(archive_data))

        # Install the integration
        _remote_client.install_integration(archive_data, filename)
        _LOG.info("Installed integration %s successfully", integration.get("name"))

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info("Lock released after successful install of %s", driver_id)

        # Return a replacement card HTML for HTMX outerHTML swap
        categories_list = integration.get("categories", [])
        integration_obj = AvailableIntegration(
            driver_id=driver_id,
            name=integration.get("name", driver_id),
            description=integration.get("description", ""),
            icon=integration.get("icon", "puzzle-piece"),
            home_page=integration.get("repository", ""),
            developer=integration.get("author", ""),
            version="",
            category=categories_list[0] if categories_list else "",
            categories=categories_list,
            installed=False,
            driver_installed=True,  # Just installed, not configured yet
            external=False,
            custom=True,
            official=False,
            update_available=False,
            latest_version="",
            instance_id="",
            can_update=False,
        )

        remote_ip = _remote_client._address if _remote_client else None
        return render_template(
            "partials/available_card.html",
            integration=integration_obj,
            remote_ip=remote_ip,
            just_installed=True,
        )

    except SyncAPIError as e:
        _LOG.error("Install failed: %s", e)
        error_msg = str(e).replace('"', "&quot;").replace("'", "&#39;")

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after SyncAPIError in install_integration for %s",
                driver_id,
            )

        return _build_error_card(driver_id, registry, error_msg), 200
    except Exception as e:
        _LOG.error("Unexpected error during install: %s", e)
        error_msg = str(e).replace('"', "&quot;").replace("'", "&#39;")

        # Release operation lock
        with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after generic exception in install_integration for %s",
                driver_id,
            )

        return _build_error_card(driver_id, registry, error_msg), 200


@app.route("/api/backup/all", methods=["POST"])
def backup_all():
    """
    Backup all custom integrations' configurations.

    This triggers the backup flow for all CUSTOM driver types.
    """
    if not _remote_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        results = backup_all_integrations(_remote_client)
        successful = sum(1 for v in results.values() if v)
        failed = sum(1 for v in results.values() if not v)

        return jsonify(
            {
                "status": "ok",
                "message": f"Backed up {successful} integrations, {failed} failed",
                "results": results,
            }
        )
    except Exception as e:
        _LOG.error("Backup all failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/backup/<driver_id>", methods=["POST"])
def backup_single(driver_id: str):
    """
    Backup a single integration's configuration.

    :param driver_id: The driver ID to backup
    """
    if not _remote_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        backup_data = backup_integration(_remote_client, driver_id, save_to_file=True)
        if backup_data:
            return jsonify(
                {
                    "status": "ok",
                    "message": f"Successfully backed up {driver_id}",
                    "has_data": True,
                }
            )
        else:
            return jsonify(
                {
                    "status": "warning",
                    "message": f"No backup data retrieved for {driver_id}",
                    "has_data": False,
                }
            )
    except Exception as e:
        _LOG.error("Backup failed for %s: %s", driver_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/backup/<driver_id>", methods=["GET"])
def get_backup_data(driver_id: str):
    """
    Get the stored backup data for an integration.

    :param driver_id: The driver ID
    """
    backup_data = get_backup(driver_id)
    if backup_data:
        return jsonify(
            {
                "status": "ok",
                "driver_id": driver_id,
                "data": backup_data,
            }
        )
    else:
        return jsonify(
            {
                "status": "not_found",
                "message": f"No backup found for {driver_id}",
            }
        ), 404


@app.route("/api/backups", methods=["GET"])
def list_integration_backups():
    """List all stored integration config backups."""
    backups = get_all_backups()
    return jsonify(backups)


@app.route("/api/release-notes/unavailable/<version>")
def get_release_notes_unavailable(version: str):
    """
    Return a user-friendly message when release notes cannot be fetched.

    Used when GitHub URL cannot be parsed or release info is unavailable.
    """
    return render_template(
        "partials/modal_release_notes.html",
        error="Release notes are not available for this integration",
        version=version,
        github_url=None,
    )


@app.route("/api/release-notes/<owner>/<repo>/<version>")
def get_release_notes(owner: str, repo: str, version: str):
    """
    Get release notes for a specific version and return HTML for modal.

    Renders markdown release notes as HTML.
    """
    if not _github_client:
        return render_template(
            "partials/modal_release_notes.html",
            error="GitHub client not available",
            version=version,
        )

    try:
        # Fetch release info from GitHub
        release = _github_client.get_release_by_tag(owner, repo, version)

        if not release:
            return render_template(
                "partials/modal_release_notes.html",
                error=f"Release notes not found for {version}",
                version=version,
                github_url=f"https://github.com/{owner}/{repo}/releases/tag/{version}",
            )

        # Get release body (markdown)
        release_body = release.get("body", "")

        # Convert markdown to HTML with comprehensive extensions
        md = markdown.Markdown(
            extensions=[
                "markdown.extensions.fenced_code",  # ```code blocks```
                "markdown.extensions.tables",  # Tables
                "markdown.extensions.nl2br",  # Newline to <br>
                "markdown.extensions.sane_lists",  # Better list handling
                "markdown.extensions.codehilite",  # Code highlighting
                "markdown.extensions.attr_list",  # Add attributes to elements
                "markdown.extensions.def_list",  # Definition lists
                "markdown.extensions.abbr",  # Abbreviations
                "markdown.extensions.footnotes",  # Footnotes
                "markdown.extensions.md_in_html",  # Markdown in HTML
                "markdown.extensions.toc",  # Table of contents
            ],
            extension_configs={
                "markdown.extensions.codehilite": {
                    "css_class": "highlight",
                    "linenums": False,
                },
            },
        )
        release_notes_html = md.convert(release_body) if release_body else ""

        # Format the published date
        published_at = release.get("published_at", "")
        if published_at:
            try:
                dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                release_date = dt.strftime("%B %d, %Y")
            except (ValueError, AttributeError):
                release_date = published_at
        else:
            release_date = "Unknown"

        # Check if this is a pre-release (beta)
        is_beta = release.get("prerelease", False)

        # Create modal title with Beta prefix if needed
        modal_title = f"{'Beta ' if is_beta else ''}Release Notes - {version}"

        # Render the release notes template
        return render_template(
            "partials/modal_release_notes.html",
            version=version,
            release_date=release_date,
            release_notes=release_notes_html,
            release_name=release.get("name", ""),
            github_url=f"https://github.com/{owner}/{repo}/releases/tag/{version}",
            author=release.get("author", {}).get("login", ""),
            is_beta=is_beta,
            modal_title=modal_title,
        )
    except Exception as e:
        _LOG.error(
            "Error loading release notes for %s/%s %s: %s", owner, repo, version, e
        )
        return render_template(
            "partials/modal_release_notes.html",
            error=f"Error loading release notes: {str(e)}",
            version=version,
            github_url=f"https://github.com/{owner}/{repo}/releases/tag/{version}",
        )


@app.route("/api/version-selector/<owner>/<repo>/<driver_id>")
def get_version_selector(owner: str, repo: str, driver_id: str):
    """
    Get version selector modal content with available releases.

    Fetches recent releases and filters by migration boundary from registry.
    Shows beta releases if enabled in settings.
    """
    if not _github_client:
        return render_template(
            "partials/modal_version_selector.html",
            error="GitHub client not available",
        )

    try:
        # Load settings to check show_beta_releases
        settings = Settings.load()
        show_beta_releases = settings.show_beta_releases

        # Load registry to get migration_required_at
        migration_required_at = None
        is_update = False
        instance_id = None

        try:
            registry = load_registry()
            for entry in registry:
                if entry.get("id") == driver_id or entry.get("driver_id") == driver_id:
                    migration_required_at = entry.get("migration_required_at")
                    break
        except Exception as e:
            _LOG.warning("Failed to load registry for migration check: %s", e)

        # Check if this is an update (driver installed) or fresh install
        integrations = _get_installed_integrations()
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        if integration:
            is_update = True
            instance_id = integration.instance_id

        # Fetch releases from GitHub
        releases_data = _github_client.get_releases(owner, repo, limit=20)

        if not releases_data:
            return render_template(
                "partials/modal_version_selector.html",
                error="No releases found for this integration",
            )

        # Filter and organize releases
        beta_releases = []
        stable_releases = []
        found_first_stable = False

        for release in releases_data:
            tag_name = release.get("tag_name", "")
            if not tag_name:
                continue

            # Skip drafts always
            if release.get("draft", False):
                continue

            # Check if this is a pre-release (beta)
            is_prerelease = release.get("prerelease", False)

            # Parse version for comparison
            clean_version = tag_name.lstrip("v")

            # Check migration boundary
            if migration_required_at:
                try:
                    if Version(clean_version) <= Version(migration_required_at):
                        _LOG.debug(
                            "Filtering out %s (≤ %s migration boundary)",
                            tag_name,
                            migration_required_at,
                        )
                        continue
                except InvalidVersion:
                    _LOG.warning("Invalid version format: %s", tag_name)
                    continue

            # Format published date
            published_at = release.get("published_at", "")
            if published_at:
                try:
                    dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                    formatted_date = dt.strftime("%B %d, %Y")
                except (ValueError, AttributeError):
                    formatted_date = published_at
            else:
                formatted_date = ""

            release_info = {
                "tag_name": tag_name,
                "name": release.get("name", ""),
                "published_at": formatted_date,
                "is_beta": is_prerelease,
            }

            if is_prerelease:
                # Only add beta releases if:
                # 1. User has enabled show_beta_releases setting
                # 2. We haven't found the first stable release yet
                if show_beta_releases and not found_first_stable:
                    beta_releases.append(release_info)
            else:
                # This is a stable release
                found_first_stable = True
                stable_releases.append(release_info)

            # Stop when we have enough stable releases
            if len(stable_releases) >= 4:
                break

        # Combine lists: beta releases first, then stable releases
        # This ensures: beta, beta, latest, previous (good)
        # Not: beta, latest, beta (bad)
        filtered_releases = beta_releases + stable_releases

        # Limit to 4 total
        filtered_releases = filtered_releases[:4]

        # Determine the install/update URL
        if is_update and instance_id:
            install_url = f"/api/integration/{instance_id}/update"
            hx_target = f"#card-{driver_id}"
            hx_indicator = f"#upgrade-overlay-{driver_id}"
        elif is_update:
            # Driver installed but no instance
            install_url = f"/api/driver/{driver_id}/update"
            hx_target = f"#card-{driver_id}"
            hx_indicator = f"#upgrade-overlay-{driver_id}"
        else:
            # Fresh install
            install_url = f"/api/integration/{driver_id}/install"
            hx_target = f"#card-{driver_id}"
            hx_indicator = f"#overlay-{driver_id}"

        return render_template(
            "partials/modal_version_selector.html",
            releases=filtered_releases,
            migration_required_at=migration_required_at,
            install_url=install_url,
            hx_target=hx_target,
            hx_indicator=hx_indicator,
            driver_id=driver_id,
        )

    except Exception as e:
        _LOG.error("Error loading version selector for %s/%s: %s", owner, repo, e)
        return render_template(
            "partials/modal_version_selector.html",
            error=f"Error loading versions: {str(e)}",
        )


@app.route("/api/versions/check", methods=["POST"])
def check_versions():
    """
    Manually trigger a version check for all installed integrations.

    This refreshes the cached version data from GitHub.
    """
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        _LOG.info("Manual version check triggered")

        integrations = _get_installed_integrations()
        version_updates = {}
        checked = 0
        updates_available = 0

        for integration in integrations:
            if integration.official:
                continue

            if not integration.home_page or "github.com" not in integration.home_page:
                continue

            try:
                parsed = SyncGitHubClient.parse_github_url(integration.home_page)
                if not parsed:
                    continue

                owner, repo = parsed
                release = _get_latest_release_for_update(owner, repo)
                if release:
                    latest_version = release.get("tag_name", "")
                    current_version = integration.version or ""
                    has_update = SyncGitHubClient.compare_versions(
                        current_version, latest_version
                    )
                    version_updates[integration.driver_id] = {
                        "name": integration.name,
                        "current": current_version,
                        "latest": latest_version,
                        "has_update": has_update,
                    }
                    checked += 1
                    if has_update:
                        updates_available += 1
            except Exception as e:
                _LOG.debug(
                    "Failed to check version for %s: %s", integration.driver_id, e
                )

        global _cached_version_data, _version_check_timestamp
        _cached_version_data = version_updates
        _version_check_timestamp = datetime.now().isoformat()

        return jsonify(
            {
                "status": "ok",
                "checked": checked,
                "updates_available": updates_available,
                "timestamp": _version_check_timestamp,
                "versions": version_updates,
            }
        )

    except Exception as e:
        _LOG.error("Version check failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/versions", methods=["GET"])
def get_versions():
    """Get cached version data for all integrations."""
    return jsonify(
        {
            "timestamp": _version_check_timestamp,
            "versions": _cached_version_data,
        }
    )


@app.route("/api/status")
def get_status():
    """Get current system status as JSON."""
    if not _remote_client:
        return jsonify({"error": "Service not initialized"})

    try:
        is_docked = _remote_client.is_docked()
        return jsonify({"docked": is_docked, "server": "running"})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/status/html")
def get_status_html():
    """Get current system status as HTML badges."""
    if not _remote_client:
        return '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-500/20 text-red-300">Not Connected</span>'

    try:
        is_docked = _remote_client.is_docked()
        docked_badge = (
            '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-500/20 text-green-300">'
            '<i class="fa-regular fa-charging-station mr-1.5"></i>Docked</span>'
            if is_docked
            else '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-500/20 text-yellow-300">'
            '<i class="fa-regular fa-battery-half mr-1.5"></i>On Battery</span>'
        )
        server_badge = (
            '<span class="hidden sm:inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-500/20 text-green-300">'
            '<span class="w-1.5 h-1.5 mr-1.5 bg-green-400 rounded-full animate-pulse"></span>Running</span>'
        )
        return f"{docked_badge} {server_badge}"
    except Exception as e:
        return f'<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-500/20 text-red-300">Error: {e}</span>'


# =============================================================================
# Settings Routes
# =============================================================================


@app.route("/settings")
def settings_page():
    """Render the settings page."""
    settings = Settings.load()
    return render_template(
        "settings.html",
        settings=settings,
        remote_address=_remote_client._address if _remote_client else None,
        web_server_port=WEB_SERVER_PORT,
    )


@app.route("/api/settings", methods=["POST"])
def save_settings():
    """Save settings from form submission."""
    try:
        settings = Settings.load()

        # Update settings from form data (checkboxes only send value if checked)
        settings.shutdown_on_battery = request.form.get("shutdown_on_battery") == "on"
        settings.auto_update = request.form.get("auto_update") == "on"
        settings.backup_configs = request.form.get("backup_configs") == "on"
        settings.auto_register_entities = (
            request.form.get("auto_register_entities") == "on"
        )
        settings.show_beta_releases = request.form.get("show_beta_releases") == "on"

        backup_time = request.form.get("backup_time")
        if backup_time:
            settings.backup_time = backup_time

        settings.save()

        return """
        <div class="flex items-center gap-2 text-green-400">
            <i class="fa-solid fa-check w-5 h-5"></i>
            Settings saved successfully
        </div>
        """
    except Exception as e:
        _LOG.error("Failed to save settings: %s", e)
        return f"""
        <div class="flex items-center gap-2 text-red-400">
            <i class="fa-solid fa-xmark w-5 h-5"></i>
            Error: {e}
        </div>
        """


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Get current settings as JSON."""
    settings = Settings.load()
    return jsonify(settings.to_dict())


# ============================================================================
# Logs Routes
# ============================================================================


@app.route("/logs")
def logs_page():
    """Render the logs page."""
    entries = get_log_entries()
    return render_template(
        "logs.html",
        entries=entries,
        log_count=len(entries),
    )


@app.route("/api/logs/entries")
def get_logs_entries():
    """Get log entries as HTML partial for HTMX."""
    entries = get_log_entries()
    return render_template("partials/log_entries.html", entries=entries)


@app.route("/api/logs/clear-confirm")
def get_clear_logs_confirm():
    """Get confirmation modal for clearing logs."""
    return render_template("partials/modal_clear_logs.html")


@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    """Clear all log entries."""
    handler = get_log_handler()
    if handler:
        handler.clear()

    return """
    <div class="p-8 text-center text-gray-400">
        <i class="fa-regular fa-circle-check w-12 h-12 mx-auto mb-3 opacity-50"></i>
        <p>Logs cleared</p>
    </div>
    """


# ============================================================================
# Integration Logs Routes (Remote logs)
# ============================================================================


@app.route("/integration-logs")
def integration_logs_page():
    """Render the integration logs page."""
    if not _remote_client:
        return render_template(
            "integration_logs.html",
            services=[],
            entries=[],
            selected_service="",
        )

    try:
        # Get available log services from the remote
        services = _remote_client.get_log_services()

        _LOG.debug("Fetched %d total log services from remote", len(services))

        # Filter to only active services
        active_services = [
            s for s in services if s.get("service") and s.get("active") is True
        ]

        _LOG.debug(
            "Found %d active services out of %d total services",
            len(active_services),
            len(services),
        )

        # Get integrations to match driver names for custom services
        integrations = _remote_client.get_integrations()
        integration_map = {
            intg.get("driver_id"): intg.get("name", {}).get("en", "")
            for intg in integrations
            if intg.get("driver_id")
        }

        # Enrich custom services with driver names
        for service in active_services:
            service_id = service.get("service", "")
            # Check if it's a custom integration (starts with "custom-intg-")
            if service_id.startswith("custom-intg-"):
                # Remove "custom-intg-" prefix to get driver_id
                driver_id = service_id.replace("custom-intg-", "", 1)
                # Look up the driver name from integrations
                if driver_id in integration_map:
                    service["display_name"] = integration_map[driver_id]
                else:
                    service["display_name"] = service.get("name", service_id)
            else:
                # Use the original name for non-custom services
                service["display_name"] = service.get("name", service_id)

        # Sort by display name
        active_services.sort(key=lambda x: x.get("display_name", ""))

        return render_template(
            "integration_logs.html",
            services=active_services,
            entries=[],
            selected_service="",
        )
    except SyncAPIError as e:
        _LOG.error("Failed to fetch log services: %s", e)
        return render_template(
            "integration_logs.html",
            services=[],
            entries=[],
            selected_service="",
        )


@app.route("/api/integration-logs/entries")
def get_integration_logs_entries():
    """Get integration log entries as HTML partial for HTMX."""
    if not _remote_client:
        return render_template("partials/integration_log_entries.html", entries=[])

    service = request.args.get("service", "")
    if not service:
        return render_template("partials/integration_log_entries.html", entries=[])

    # Get priority filter from request, default to 7 (all levels)
    priority_str = request.args.get("priority", "7")
    try:
        priority = int(priority_str)
        # Ensure priority is in valid range (0-7)
        priority = max(0, min(7, priority))
    except (ValueError, TypeError):
        priority = 7  # Default to all levels if invalid

    try:
        # Fetch logs from the remote for the specified service and priority
        logs = _remote_client.get_logs(
            priority=priority,
            service=service,
            limit=1000,
            as_text=False,  # Get as JSON
        )

        return render_template("partials/integration_log_entries.html", entries=logs)
    except SyncAPIError as e:
        _LOG.error("Failed to fetch integration logs: %s", e)
        return render_template("partials/integration_log_entries.html", entries=[])


@app.route("/api/integration-logs/download")
def download_integration_logs():
    """Download integration logs as a text file."""
    if not _remote_client:
        return "Not connected to remote", 500

    service = request.args.get("service", "")
    if not service:
        return "No service specified", 400

    # Get priority filter from request, default to 7 (all levels)
    priority_str = request.args.get("priority", "7")
    try:
        priority = int(priority_str)
        # Ensure priority is in valid range (0-7)
        priority = max(0, min(7, priority))
    except (ValueError, TypeError):
        priority = 7  # Default to all levels if invalid

    try:
        # Fetch logs as text export with specified priority
        log_text = _remote_client.get_logs(
            priority=priority,
            service=service,
            limit=10000,  # Maximum allowed
            as_text=True,  # Get as text export
        )

        # Ensure we got a string response
        if not isinstance(log_text, str):
            return "Failed to retrieve logs as text", 500

        # Create filename from service name and priority level
        # Remove "custom-intg-" prefix for cleaner filename
        base_name = service.replace("custom-intg-", "").replace("intg-", "")

        # Add priority level to filename for clarity
        priority_labels = {
            0: "emergency",
            1: "alert",
            2: "critical",
            3: "error",
            4: "warning",
            5: "notice",
            6: "info",
            7: "debug",
        }
        priority_label = priority_labels.get(priority, "all")
        filename = f"{base_name}_logs_{priority_label}+.txt"

        # Return as downloadable file
        return Response(
            log_text,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except SyncAPIError as e:
        _LOG.error("Failed to download integration logs: %s", e)
        return f"Failed to download logs: {e}", 500


@app.route("/api/backups/create", methods=["POST"])
def create_backup_now():
    """Create a backup of all integration configs that support backup."""
    try:
        if not _remote_client:
            return """<div class="text-red-400">Not connected to remote</div>"""

        # Load registry to check which integrations support backup
        registry = load_registry()
        registry_by_driver_id = {}
        for item in registry:
            if item.get("driver_id"):
                registry_by_driver_id[item["driver_id"]] = item
            registry_by_driver_id[item["id"]] = item

        # Get installed integrations
        integrations = _remote_client.get_integrations()

        backed_up = []
        skipped = []
        failed = []

        for instance in integrations:
            driver_id = instance.get("driver_id", "")
            name = instance.get("name", {})
            if isinstance(name, dict):
                name = name.get("en", driver_id)

            version = instance.get("version", "0.0.0")

            # Check if this integration supports backup and meets version requirements
            reg_item = registry_by_driver_id.get(driver_id)
            if not reg_item:
                skipped.append(f"{name} (not in registry)")
                continue

            can_backup, reason = _can_backup_integration(driver_id, version, reg_item)
            if not can_backup:
                skipped.append(f"{name} ({reason})")
                continue

            # Try to backup
            backup_data = backup_integration(
                _remote_client, driver_id, save_to_file=True
            )
            if backup_data:
                backed_up.append(name)
            else:
                failed.append(name)

        # Build result message
        result_parts = []
        if backed_up:
            result_parts.append(
                f"<span class='text-green-400'>✓ Backed up: {', '.join(backed_up)}</span>"
            )
        if skipped:
            result_parts.append(
                f"<span class='text-gray-400'>Skipped (no backup support): {len(skipped)}</span>"
            )
        if failed:
            result_parts.append(
                f"<span class='text-red-400'>✗ Failed: {', '.join(failed)}</span>"
            )

        if not result_parts:
            return """<div class="text-gray-400">No integrations to backup</div>"""

        return f"""<div class="space-y-1">{"<br>".join(result_parts)}</div>"""

    except Exception as e:
        _LOG.error("Failed to create backup: %s", e)
        return f"""<div class="text-red-400">Error creating backup: {e}</div>"""


@app.route("/api/backups/list")
def list_backups():
    """List available integration backups."""
    try:
        backups_data = get_all_backups()
        backups = backups_data.get("integrations", {})

        if not backups:
            return "<div class='text-gray-400'>No backups found</div>"

        html = "<div class='space-y-2'>"
        for driver_id, backup_info in backups.items():
            timestamp = backup_info.get("timestamp", "Unknown")
            # Format the timestamp nicely
            try:
                dt = datetime.fromisoformat(timestamp)
                formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                formatted_time = timestamp

            html += f"""
            <div class="flex items-center justify-between p-3 bg-gray-700/50 rounded-lg hover:bg-gray-700">
                <button class="flex-1 text-left"
                        hx-get="/api/backups/{driver_id}/view"
                        hx-target="#backup-content"
                        hx-swap="innerHTML"
                        title="View backup data">
                    <div class="text-white font-mono text-sm">{driver_id}</div>
                    <div class="text-xs text-gray-400">{formatted_time}</div>
                </button>
                <button class="text-red-400 hover:text-red-300 text-sm ml-3"
                        hx-get="/api/backups/{driver_id}/delete-confirm"
                        hx-target="#modal-content"
                        hx-swap="innerHTML"
                        hx-on::before-request="openModal('Delete Backup')">
                    Delete
                </button>
            </div>
            """
        html += "</div>"
        return html

    except Exception as e:
        _LOG.error("Failed to list backups: %s", e)
        return f"<div class='text-red-400'>Error: {e}</div>"


@app.route("/api/backups/<driver_id>/delete-confirm")
def get_delete_backup_confirm(driver_id: str):
    """Get confirmation modal for deleting a backup."""
    try:
        backups_data = get_all_backups()
        backup_info = backups_data.get("integrations", {}).get(driver_id, {})
        timestamp = backup_info.get("timestamp", "Unknown")

        # Format the timestamp nicely
        try:
            dt = datetime.fromisoformat(timestamp)
            formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            formatted_time = timestamp

        return render_template(
            "partials/modal_delete_backup.html",
            driver_id=driver_id,
            timestamp=formatted_time,
        )
    except Exception as e:
        _LOG.error("Failed to get backup info: %s", e)
        return render_template(
            "partials/modal_delete_backup.html",
            driver_id=driver_id,
            timestamp="Unknown",
        )


@app.route("/api/backups/<driver_id>/view")
def view_backup(driver_id: str):
    """View backup data for a specific driver."""
    try:
        backup_data = get_backup(driver_id)

        if not backup_data:
            return "<div class='text-gray-400'>No backup data found</div>"

        # Pretty-print JSON data
        try:
            parsed_data = json.loads(backup_data)
            formatted_data = json.dumps(parsed_data, indent=2)
        except json.JSONDecodeError:
            formatted_data = backup_data

        return f"""
        <div class="mt-4 p-4 bg-gray-900 rounded-lg">
            <div class="flex items-center justify-between mb-3">
                <h4 class="text-sm font-medium text-white">Backup Data for {driver_id}</h4>
                <button class="text-gray-400 hover:text-white text-sm"
                        onclick="this.parentElement.parentElement.style.display='none'">
                    ✕ Close
                </button>
            </div>
            <pre class="text-xs text-gray-300 overflow-auto max-h-96 whitespace-pre-wrap"><code>{formatted_data}</code></pre>
        </div>
        """
    except Exception as e:
        _LOG.error("Failed to view backup: %s", e)
        return f"<div class='text-red-400'>Error: {e}</div>"


@app.route("/api/backups/<driver_id>", methods=["DELETE"])
def delete_backup_entry(driver_id: str):
    """Delete a backup for a specific driver."""
    try:
        delete_backup(driver_id)
        return list_backups()  # Return updated list
    except Exception as e:
        _LOG.error("Failed to delete backup: %s", e)
        return f"<div class='text-red-400'>Error: {e}</div>"


@app.route("/api/backups/download")
def download_complete_backup():
    """Download complete backup file (all integrations + settings)."""

    try:
        # Get current settings
        settings = Settings.load()

        # Get all integration backups
        backups_data = get_all_backups()

        # Ensure settings are included
        backups_data["settings"] = settings.to_dict()

        # Create in-memory file for download
        backup_json = json.dumps(backups_data, indent=2)
        backup_bytes = backup_json.encode("utf-8")
        backup_io = io.BytesIO(backup_bytes)

        return send_file(
            backup_io,
            mimetype="application/json",
            as_attachment=True,
            download_name="uc_integration_manager_backup.json",
        )
    except Exception as e:
        _LOG.error("Failed to download complete backup: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/backups/upload", methods=["POST"])
def upload_complete_backup():
    """Upload and restore complete backup file (all integrations + settings)."""
    try:
        if "file" not in request.files:
            return jsonify({"status": "error", "message": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"status": "error", "message": "No file selected"}), 400

        # Read and validate JSON
        try:
            content = file.read().decode("utf-8")
            backup_data = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return jsonify(
                {"status": "error", "message": f"Invalid backup file: {e}"}
            ), 400

        # Validate backup structure
        if "version" not in backup_data:
            return jsonify(
                {
                    "status": "error",
                    "message": "Invalid backup file: missing version field",
                }
            ), 400

        # Restore settings if present
        if "settings" in backup_data and backup_data["settings"]:
            try:
                settings = Settings(**backup_data["settings"])
                settings.save()
                _LOG.info("Restored settings from backup")
            except Exception as e:
                _LOG.warning("Failed to restore settings: %s", e)

        # Save the complete backup file (includes all integrations)
        try:
            with open(INTEGRATION_BACKUPS_FILE, "w", encoding="utf-8") as f:
                json.dump(backup_data, f, indent=2)
            _LOG.info("Restored complete backup file")
        except OSError as e:
            return jsonify(
                {"status": "error", "message": f"Failed to save backup: {e}"}
            ), 500

        integration_count = len(backup_data.get("integrations", {}))
        settings_restored = "settings" in backup_data and backup_data["settings"]

        message = f"Successfully restored {integration_count} integration backup(s)"
        if settings_restored:
            message += " and settings"

        return jsonify({"status": "ok", "message": message})
    except Exception as e:
        _LOG.error("Failed to upload backup: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# =============================================================================
# Web Server Class
# =============================================================================


class WebServer:
    """
    Flask web server manager.

    Handles starting and stopping the web server in a separate thread.
    """

    def __init__(
        self,
        address: str,
        pin: str | None = None,
        api_key: str | None = None,
        host: str = "0.0.0.0",
        port: int = WEB_SERVER_PORT,
    ) -> None:
        """
        Initialize the web server.

        :param address: Remote IP address
        :param pin: Remote PIN for auth
        :param api_key: API key for auth
        :param host: Host to bind to
        :param port: Port to listen on
        """
        global _remote_client, _github_client

        self._host = host
        self._port = port
        self._server_thread: threading.Thread | None = None
        self._running = False

        # Initialize sync API clients
        _remote_client = SyncRemoteClient(
            address=address,
            pin=pin,
            api_key=api_key,
        )
        _github_client = SyncGitHubClient()

        # Ensure template and static directories exist
        self._setup_directories()

    def _setup_directories(self) -> None:
        """Create required directories if they don't exist."""
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        os.makedirs(STATIC_DIR, exist_ok=True)
        os.makedirs(os.path.join(TEMPLATE_DIR, "partials"), exist_ok=True)

    def start(self) -> None:
        """Start the web server in a background thread."""
        if self._running:
            _LOG.warning("Web server already running")
            return

        _LOG.info("Starting web server on %s:%d", self._host, self._port)

        self._running = True
        self._server_thread = threading.Thread(
            target=self._run_server,
            daemon=True,
        )
        self._server_thread.start()

    def _run_server(self) -> None:
        """Run the Flask server (called in background thread)."""
        try:
            # Use werkzeug server for development
            # In production, consider using waitress or gunicorn
            _LOG.info("Creating server on %s:%d", self._host, self._port)

            self._server = make_server(
                self._host,
                self._port,
                app,
                threaded=True,
            )
            _LOG.info("Server created, starting to serve...")
            self._server.serve_forever()
        except OSError as e:
            _LOG.error("Web server OS error (port may be in use): %s", e)
            self._running = False
        except Exception as e:
            _LOG.error("Web server error: %s", e)
            self._running = False

    def stop(self) -> None:
        """Stop the web server."""
        if not self._running:
            return

        _LOG.info("Stopping web server")
        self._running = False

        if hasattr(self, "_server"):
            self._server.shutdown()

        if self._server_thread:
            self._server_thread.join(timeout=5)
            self._server_thread = None

    @property
    def is_running(self) -> bool:
        """Check if the web server is running."""
        return self._running

    def refresh_integration_versions(self) -> None:
        """
        Refresh version information for all installed integrations.

        This checks GitHub for the latest releases and updates the cached
        version data used by the UI.
        """
        _refresh_version_cache()

    def perform_scheduled_backup(self) -> bool:
        """
        Perform scheduled backup of all supported integrations.

        :return: True if backup was successful, False otherwise
        """
        if not _remote_client:
            _LOG.warning("Cannot perform backup - remote client not initialized")
            return False

        try:
            _LOG.info("Starting scheduled backup of integrations...")

            # Load registry to check which integrations support backup
            registry = load_registry()
            registry_by_driver_id = {}
            for item in registry:
                if item.get("driver_id"):
                    registry_by_driver_id[item["driver_id"]] = item
                registry_by_driver_id[item["id"]] = item

            # Get installed integrations
            integrations = _remote_client.get_integrations()

            backed_up_count = 0
            total_attempted = 0

            for instance in integrations:
                driver_id = instance.get("driver_id", "")
                version = instance.get("version", "0.0.0")

                # Check if this integration supports backup and meets version requirements
                reg_item = registry_by_driver_id.get(driver_id)
                if not reg_item:
                    continue

                can_backup, reason = _can_backup_integration(
                    driver_id, version, reg_item
                )
                if not can_backup:
                    continue

                total_attempted += 1

                # Try to backup
                backup_data = backup_integration(
                    _remote_client, driver_id, save_to_file=True
                )
                if backup_data:
                    backed_up_count += 1
                    _LOG.debug("Backed up integration: %s", driver_id)

            _LOG.info(
                "Scheduled backup complete: %d/%d integrations backed up",
                backed_up_count,
                total_attempted,
            )

            return (
                backed_up_count > 0 or total_attempted == 0
            )  # Success if we backed up something or nothing to backup

        except Exception as e:
            _LOG.error("Failed to perform scheduled backup: %s", e)
            return False
