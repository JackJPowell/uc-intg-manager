"""
Flask Web Server for Integration Manager.

This module provides the web interface for managing integrations
on the Unfolded Circle Remote.

Uses synchronous HTTP clients (requests) to avoid aiohttp async context issues.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from werkzeug.serving import make_server

from backup_service import (
    backup_integration,
    get_all_backups,
    delete_backup,
    backup_all_integrations,
    get_backup,
)
from const import WEB_SERVER_PORT, Settings, API_DELAY
from log_handler import get_log_entries, get_log_handler
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
    can_update: bool = False  # Version meets minimum requirement for updates


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
    categories: list = None
    installed: bool = False  # Has an instance configured
    driver_installed: bool = False  # Driver is installed (may not have instance)
    external: bool = False  # Running externally (Docker/network)
    custom: bool = True
    official: bool = False
    update_available: bool = False
    latest_version: str = ""
    instance_id: str = ""  # Instance ID if configured
    can_update: bool = False  # Version meets minimum requirement for updates

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
                release = _github_client.get_latest_release(owner, repo)
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

        # Map driver_type to our flags
        is_official = driver_type == "LOCAL"
        is_external = driver_type == "EXTERNAL"
        is_custom = driver_type == "CUSTOM"

        # Check registry for supports_backup flag and repository URL fallback
        # Use fuzzy matching since driver_id may not match registry id exactly
        registry_item = find_registry_item(driver_id, driver_name)
        supports_backup = registry_item.get("supports_backup", False)

        if not home_page and registry_item.get("repository"):
            home_page = registry_item.get("repository")
        # Also use registry if driver home_page doesn't have github.com
        elif (
            home_page
            and "github.com" not in home_page
            and registry_item.get("repository")
        ):
            home_page = registry_item.get("repository")

        # Get description from driver, fall back to registry
        description = driver.get("description", {}).get("en", "") if driver else ""
        if not description and registry_item.get("description"):
            description = registry_item.get("description")

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

                # Check if current version meets minimum version requirement for updates
                min_version = registry_item.get("backup_min_version")
                info.can_update = True
                if min_version:
                    try:
                        if Version(info.version) < Version(min_version):
                            info.can_update = False
                            _LOG.debug(
                                "Update button hidden for %s: current version %s is below minimum %s",
                                driver_id,
                                info.version,
                                min_version,
                            )
                    except (InvalidVersion, TypeError):
                        # If version parsing fails, allow update
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

        is_official = driver_type == "LOCAL"
        is_external = driver_type == "EXTERNAL"
        is_custom = driver_type == "CUSTOM"

        # Check registry for supports_backup flag and repository URL fallback
        # Use fuzzy matching since driver_id may not match registry id exactly
        registry_item = find_registry_item(driver_id, driver_name)
        supports_backup = registry_item.get("supports_backup", False)

        # Use registry repository as fallback for home_page
        if not home_page and registry_item.get("repository"):
            home_page = registry_item.get("repository")
        # Also use registry if driver home_page doesn't have github.com
        elif (
            home_page
            and "github.com" not in home_page
            and registry_item.get("repository")
        ):
            home_page = registry_item.get("repository")

        # Get description from driver, fall back to registry
        description = driver.get("description", {}).get("en", "")
        if not description and registry_item.get("description"):
            description = registry_item.get("description")

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

                # Check if current version meets minimum version requirement for updates
                min_version = registry_item.get("backup_min_version")
                info.can_update = True
                if min_version:
                    try:
                        if Version(info.version) < Version(min_version):
                            info.can_update = False
                            _LOG.debug(
                                "Update button hidden for %s: current version %s is below minimum %s",
                                driver_id,
                                info.version,
                                min_version,
                            )
                    except (InvalidVersion, TypeError):
                        # If version parsing fails, allow update
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
        is_official = item.get("official", False) or not item.get("custom", True)
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
        if is_installed and not is_official and not is_external:
            # Use the actual driver_id from the remote (not registry id) for cache lookup
            if actual_driver_id and actual_driver_id in _cached_version_data:
                version_info = _cached_version_data[actual_driver_id]
                if version_info.get("has_update"):
                    # Always mark that an update is available (for badge display)
                    update_available = True
                    latest_version = version_info.get("latest", "")

                    # Check if current version meets minimum version requirement for updates
                    min_version = item.get("backup_min_version")
                    can_update = True
                    if min_version and version:
                        try:
                            if Version(version) < Version(min_version):
                                can_update = False
                                _LOG.debug(
                                    "Update button hidden for %s: current version %s is below minimum %s",
                                    actual_driver_id,
                                    version,
                                    min_version,
                                )
                        except (InvalidVersion, TypeError):
                            # If version parsing fails, allow update
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
    Update an existing integration to the latest version using default settings.

    The register_entities behavior is determined by the user's auto_register_entities setting.
    """
    settings = Settings.load()
    return _perform_update_integration(instance_id, settings.auto_register_entities)


@app.route("/api/integration/<instance_id>/update-alt", methods=["POST"])
def update_integration_alt(instance_id: str):
    """
    Update an existing integration with the opposite entity registration behavior.

    If auto_register_entities is enabled, this will NOT register entities.
    If auto_register_entities is disabled, this WILL register entities.
    """
    settings = Settings.load()
    return _perform_update_integration(instance_id, not settings.auto_register_entities)


def _perform_update_integration(instance_id: str, register_entities: bool):
    """
    Update an existing integration to the latest version.

    Process:
    1. Backup the current configuration
    2. Find the integration's GitHub repo URL
    3. Download the latest release tar.gz
    4. Delete the existing driver (which cascades to delete instance)
    5. Install the new version
    6. Restore configuration
    7. Optionally register entities if register_entities=True

    :param instance_id: The integration instance ID to update
    :param register_entities: Whether to register entities after update
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
                    entity.get("entity_id")
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
        # For integrations that support backup, we REQUIRE a successful backup before proceeding
        if is_configured:
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
                elif integration.supports_backup:
                    # Integration supports backup but backup failed - don't proceed
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
                else:
                    _LOG.warning(
                        "No backup data retrieved for %s - integration may not support backup",
                        integration.driver_id,
                    )
            except Exception as e:
                if integration.supports_backup:
                    # Integration supports backup but backup failed - don't proceed
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
                    _LOG.warning(
                        "Failed to backup %s, continuing with update: %s",
                        integration.driver_id,
                        e,
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

        # Download the latest release
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
                    "message": f"No tar.gz release found for {owner}/{repo}",
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
        _LOG.info("Waiting for driver to be ready: %s", integration.driver_id)
        _remote_client.get_drivers()  # Verify driver is available

        # Additional pause to ensure driver is fully initialized
        time.sleep(API_DELAY * 3)

        # Restore configuration if backup data exists (only for configured instances)
        if backup_data and is_configured:
            try:
                _LOG.info(
                    "Starting configuration restore for %s", integration.driver_id
                )

                # Step 1: POST /intg/setup with reconfigure=false to start restore mode
                _remote_client.start_setup(integration.driver_id, reconfigure=False)
                _LOG.info("Started setup mode for restore")

                # Brief pause between API calls
                # time.sleep(API_DELAY)

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

                # Get entities for the restored instance
                if restored_instance_id:
                    entities = _remote_client.get_instance_entities(
                        restored_instance_id
                    )
                    _LOG.info(
                        "Retrieved %d entities for instance %s",
                        len(entities),
                        restored_instance_id,
                    )

                    # Re-register previously configured entities if requested
                    if register_entities and configured_entity_ids:
                        time.sleep(API_DELAY * 5)
                        _LOG.info(
                            "Re-registering %d previously configured entities for instance %s",
                            len(configured_entity_ids),
                            restored_instance_id,
                        )

                        try:
                            # Register all entities in one API call
                            _remote_client.register_entities(
                                restored_instance_id, configured_entity_ids
                            )

                            _LOG.info(
                                "Successfully re-registered %d entities for instance %s",
                                len(configured_entity_ids),
                                restored_instance_id,
                            )
                        except SyncAPIError as e:
                            _LOG.warning(
                                "Failed to register entities for instance %s: %s",
                                restored_instance_id,
                                e,
                            )

                _LOG.info(
                    "Configuration restored successfully for %s", integration.driver_id
                )

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
    Update an unconfigured driver to the latest version.

    This is used when a driver is installed but not configured (no instance exists).
    Since there's no instance, there's nothing to backup or restore - just download
    and install the new version.
    """
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

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

        # Download the latest release
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

    Process:
    1. Look up the integration in the registry by driver_id
    2. Get the GitHub repo URL
    3. Download the latest release tar.gz
    4. Upload and install on the remote
    """
    if not _remote_client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

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

        # Download the latest release
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
                release = _github_client.get_latest_release(owner, repo)
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

    if not entries:
        return """
        <div class="p-8 text-center text-gray-400">
            <i class="fa-regular fa-file-lines w-12 h-12 mx-auto mb-3 opacity-50"></i>
            <p>No log entries yet</p>
        </div>
        """

    html_parts = []
    for entry in entries:
        level_color = "bg-blue-500"
        bg_class = ""
        if entry.level == "ERROR":
            level_color = "bg-red-500"
            bg_class = "bg-red-900/20"
        elif entry.level == "WARNING":
            level_color = "bg-yellow-500"
            bg_class = "bg-yellow-900/20"

        html_parts.append(f"""
        <div class="p-3 hover:bg-gray-750 {bg_class}">
            <div class="flex items-start gap-3">
                <span class="w-2 h-2 mt-1.5 rounded-full flex-shrink-0 {level_color}"></span>
                <span class="text-gray-500 flex-shrink-0 w-36">{entry.timestamp}</span>
                <span class="text-purple-400 flex-shrink-0 w-32 truncate" title="{entry.logger}">{entry.logger}</span>
                <span class="text-gray-300 break-all">{entry.message}</span>
            </div>
        </div>
        """)

    return "\n".join(html_parts)


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
                        hx-delete="/api/backups/{driver_id}"
                        hx-target="#backup-list"
                        hx-swap="innerHTML"
                        hx-confirm="Delete backup for {driver_id}?">
                    Delete
                </button>
            </div>
            """
        html += "</div>"
        return html

    except Exception as e:
        _LOG.error("Failed to list backups: %s", e)
        return f"<div class='text-red-400'>Error: {e}</div>"


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
    from flask import send_file
    import io
    from const import Settings

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
    from flask import request as flask_request
    from const import Settings, INTEGRATION_BACKUPS_FILE

    try:
        if "file" not in flask_request.files:
            return jsonify({"status": "error", "message": "No file provided"}), 400

        file = flask_request.files["file"]
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
