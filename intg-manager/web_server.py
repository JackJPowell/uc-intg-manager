"""
Flask Web Server for Integration Manager.

This module provides the web interface for managing integrations
on the Unfolded Circle Remote.

Uses synchronous HTTP clients (requests) to avoid aiohttp async context issues.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import inspect
import io
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import aiohttp
import markdown
from backup_service import (
    backup_all_integrations,
    backup_integration,
    delete_backup,
    get_all_backups,
    get_backup,
)
from const import (
    API_DELAY,
    DATA_DIR,
    MANAGER_DATA_FILE,
    REPO_CACHE_VALIDITY,
    REPO_FETCH_BATCH_INTERVAL,
    REPO_FETCH_BATCH_SIZE,
    WEB_SERVER_PORT,
    RemoteConfig,
    Settings,
    UIPreferences,
)
from data_migration import migrate as migrate_v1_to_v2
from quart import Quart, Response, jsonify, render_template, request, send_file, session
from log_handler import get_log_entries, get_log_handler
from migration_service import extract_migration_mappings
from notification_manager import (
    get_notification_manager as _nm_get_notification_manager,
)
from notification_service import NotificationService, _get_ssl_context
from notification_settings import (
    DiscordNotificationConfig,
    HomeAssistantNotificationConfig,
    NotificationSettings,
    NotificationTriggers,
    NtfyNotificationConfig,
    PushoverNotificationConfig,
    WebhookNotificationConfig,
)
from packaging.version import InvalidVersion, Version
from sync_api import (
    GitHubClient,
    RemoteClient,
    SyncAPIError,
    _SyncGitHubClient,
    find_orphaned_ir_codesets,
    get_cached_repo_info,
    load_registry,
    load_registry_data,
    load_repo_cache,
    save_repo_cache,
)
from system_messages import get_system_messages_service

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
app = Quart(
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

# Session configuration for multi-remote support
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())
app.config["SESSION_TYPE"] = "filesystem"
app.config["PERMANENT_SESSION_LIFETIME"] = 7776000  # 90 days

# Multi-remote support: dict of remote_id -> RemoteClient
_remote_clients: dict[str, RemoteClient] = {}
_remote_configs: dict[str, RemoteConfig] = {}

# GitHub client (shared across all remotes)
_github_client: GitHubClient | None = None
# Sync-only GitHub client for fetch_repository_batch (runs in thread, no event loop)
_sync_github_client: _SyncGitHubClient | None = None

# User's language preference from remote localization settings
_user_language_code: str = "en_GB"  # Default to remote's default


@app.before_serving
async def _startup_fetch_localization() -> None:
    """Fetch user language preference from the first configured remote at startup."""
    global _user_language_code
    if _remote_clients:
        first_client = next(iter(_remote_clients.values()))
        try:
            localization = await first_client.get_localization()
            if localization and localization.get("language_code"):
                _user_language_code = localization["language_code"]
                _LOG.info("User language set to: %s", _user_language_code)
        except Exception as e:
            _LOG.warning("Failed to fetch localization settings at startup: %s", e)


def get_active_remote_id() -> str | None:
    """
    Get the active remote ID from session or localStorage.

    Returns the first configured remote if no session is set.
    """
    sid = session.get("active_remote_id")
    if sid and sid in _remote_configs:
        return sid

    # Fallback to first configured remote
    if _remote_configs:
        return next(iter(_remote_configs.keys()))

    return None


def _get_active_remote_client() -> RemoteClient | None:
    """Get the RemoteClient for the currently active remote."""
    remote_id = get_active_remote_id()
    if remote_id:
        return _remote_clients.get(remote_id)
    return None


# ---------------------------------------------------------------------------
# Remote online status — updated by device.py via set_remote_online()
# ---------------------------------------------------------------------------
_remote_online: dict[str, bool] = {}


def set_remote_online(remote_id: str, online: bool) -> None:
    """Called by device.py to push connectivity changes into the web server."""
    caller = inspect.stack()[1].function
    _LOG.debug(
        "[%s] set_remote_online(%s) called from %s",
        remote_id,
        online,
        caller,
    )
    _remote_online[remote_id] = online


def is_remote_online(remote_id: str | None) -> bool:
    """Return True if the named remote is currently considered online."""
    if not remote_id:
        return False
    result = _remote_online.get(remote_id, False)
    caller = inspect.stack()[1].function
    _LOG.debug(
        "[%s] is_remote_online -> %s, called from %s",
        remote_id,
        result,
        caller,
    )
    return result


def _render_offline_partial() -> str:
    """Return a small HTML partial used by HTMX endpoints when the active remote is offline."""
    return (
        "<div class='p-6 text-center text-gray-500 dark:text-gray-400'>"
        "<i class='fa-solid fa-plug-circle-xmark mr-2'></i>"
        "Remote is offline. When it comes back online, refresh the page to load data."
        "</div>"
    )


def get_notification_manager(remote_id: str | None = None):
    """Get the notification manager for a remote, injecting the friendly name from config."""
    rid = remote_id or get_active_remote_id()
    # Only prefix notifications with the remote name when multiple remotes are configured,
    # since a prefix is redundant when there's only one remote.
    if len(_remote_configs) > 1 and rid and rid in _remote_configs:
        name = _remote_configs[rid].name
    else:
        name = ""
    return _nm_get_notification_manager(rid, remote_name=name)


def _load_settings() -> Settings:
    """Load settings for the currently active remote."""
    return Settings.load(remote_id=get_active_remote_id())


def _save_settings(settings: Settings) -> None:
    """Save settings for the currently active remote."""
    settings.save(remote_id=get_active_remote_id())


def _load_notification_settings() -> NotificationSettings:
    """Load notification settings (shared across all remotes)."""
    return NotificationSettings.load(remote_id=get_active_remote_id())


def _save_notification_settings(settings: NotificationSettings) -> None:
    """Save notification settings (shared across all remotes)."""
    settings.save(remote_id=get_active_remote_id())


def _get_remote_name(remote_id: str) -> str:
    """Get the display name for a remote."""
    config = _remote_configs.get(remote_id)
    return config.name if config else remote_id


def _get_localized_name(
    name_dict: dict[str, str] | None, fallback: str = "Unknown"
) -> str:
    """
    Extract a localized name from a multi-language dictionary.

    Tries user's language first (both full code and base language),
    then common fallbacks (en, en_US, en_GB), then any available language.

    :param name_dict: Dictionary with language codes as keys (e.g., {"en": "Name", "en_US": "Name"})
    :param fallback: Default value if no name found
    :return: Localized name string
    """
    if not name_dict or not isinstance(name_dict, dict):
        return fallback

    # Try user's preferred language first (e.g., "en_US")
    if _user_language_code and _user_language_code in name_dict:
        return name_dict[_user_language_code]

    # Try just the language part without country code (e.g., "en" from "en_US")
    if _user_language_code and "_" in _user_language_code:
        base_language = _user_language_code.split("_")[0]
        if base_language in name_dict:
            return name_dict[base_language]

    # Try common English variants as fallback
    for lang_code in ["en", "en_US", "en_GB"]:
        if lang_code in name_dict:
            return name_dict[lang_code]

    # Return first available language
    if name_dict:
        return next(iter(name_dict.values()))

    return fallback


# Cached version data for integrations
_cached_version_data: dict = {}
_version_check_timestamp: str | None = None
_cached_driver_ids: set = set()  # Track installed driver IDs to detect changes

# System update info cache keyed by remote_id
_system_update_cache: dict[str, dict] = {}


def set_system_update_info(remote_id: str, update_info: dict) -> None:
    """Cache system firmware update info for a remote (called from device.py)."""
    _system_update_cache[remote_id] = update_info


# Operation lock to prevent concurrent installs/upgrades
_OPERATION_LOCK_TIMEOUT = 15 * 60  # 15 minutes — force-release stale lock
_operation_in_progress: bool = False
_operation_lock_acquired_at: float | None = None
_operation_lock = asyncio.Lock()


async def _try_acquire_operation_lock(operation_name: str):
    """Acquire the operation lock.

    Returns ``None`` when the lock is acquired successfully.  Returns a
    ``(response, 409)`` tuple when another operation is already running and
    has not timed out yet, so callers can do::

        if conflict := await _try_acquire_operation_lock("install foo"):
            return conflict
    """
    global _operation_in_progress, _operation_lock_acquired_at
    async with _operation_lock:
        _LOG.debug(
            "Lock check [%s]: in_progress=%s", operation_name, _operation_in_progress
        )
        if _operation_in_progress:
            elapsed = (
                time.monotonic() - _operation_lock_acquired_at
                if _operation_lock_acquired_at is not None
                else 0
            )
            if elapsed > _OPERATION_LOCK_TIMEOUT:
                _LOG.warning(
                    "Operation lock held for %.0f seconds - force releasing stale lock",
                    elapsed,
                )
                _operation_in_progress = False
                _operation_lock_acquired_at = None
            else:
                _LOG.warning("Blocked [%s] - lock already held", operation_name)
                return jsonify(
                    {
                        "status": "error",
                        "message": "Another install/upgrade is in progress",
                    }
                ), 409
        _operation_in_progress = True
        _operation_lock_acquired_at = time.monotonic()
        _LOG.info("Lock acquired [%s]", operation_name)
    return None


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
    self_managed: bool = (
        False  # Integration manages its own updates (like Integration Manager itself)
    )
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
    self_managed: bool = (
        False  # Integration manages its own updates (like Integration Manager itself)
    )
    custom: bool = True
    official: bool = False
    update_available: bool = False
    latest_version: str = ""
    instance_id: str = ""  # Instance ID if configured
    can_update: bool = False  # Show update button (always true if update available for custom integrations)
    can_auto_update: bool = False  # Can do automated backup/restore (requires supports_backup and min version)
    supports_backup: bool = False  # Uses ucapi-framework with backup support
    # Repository stats (from GitHub API)
    stars: int = 0
    created_at: str = ""
    pushed_at: str = ""
    downloads: int = 0
    original_index: int = 0  # Original position in registry

    @property
    def install_status(self) -> str:
        """Get installation status for display."""
        if self.official:
            return "official"
        if self.external:
            return "external"
        if self.self_managed:
            return "self_managed"
        if self.installed:
            return "configured"
        if self.driver_installed:
            return "installed"
        return "available"

    def __post_init__(self):
        if self.categories is None:
            self.categories = []


async def _get_latest_release_for_update(
    owner: str, repo: str, remote_id: str | None = None
) -> dict[str, Any] | None:
    """
    Get the latest release considering the show_beta_releases setting.

    If show_beta_releases is enabled, returns the latest release (stable or beta).
    If disabled, returns only the latest stable release.

    :param owner: GitHub repository owner
    :param repo: GitHub repository name
    :param remote_id: Remote identifier for loading settings
    :return: Release data or None if not found
    """
    if not _github_client:
        return None

    settings = Settings.load(remote_id=remote_id)

    if settings.show_beta_releases:
        # Get recent releases and pick the first non-draft one (could be beta or stable)
        releases = await _github_client.get_releases(owner, repo, limit=5)
        if releases:
            for release in releases:
                if not release.get("draft", False):
                    return release
        return None
    else:
        # Get latest stable release only (GitHub's /releases/latest excludes pre-releases)
        return await _github_client.get_latest_release(owner, repo)


async def _refresh_version_cache(remote_id: str | None = None) -> None:
    """
    Refresh the cached version information for all installed integrations.

    This is called after installations/updates to ensure the UI shows
    current version information.

    :param remote_id: Remote identifier to refresh cache for (uses active if None)
    """
    global _cached_version_data, _version_check_timestamp, _cached_driver_ids

    if remote_id is None:
        remote_id = get_active_remote_id()

    client = _remote_clients.get(remote_id) if remote_id else None
    if not client or not _github_client:
        return

    try:
        _LOG.info("[%s] Refreshing version cache after update...", remote_id)

        # Get installed integrations
        integrations = await _get_installed_integrations(remote_id)
        version_updates = {}
        current_driver_ids = set()

        for integration in integrations:
            current_driver_ids.add(integration.driver_id)

            if integration.official:
                continue

            if not integration.home_page or "github.com" not in integration.home_page:
                continue

            # Small delay to avoid GitHub rate limiting
            await asyncio.sleep(0.1)

            try:
                parsed = GitHubClient.parse_github_url(integration.home_page)
                if not parsed:
                    continue

                owner, repo = parsed
                release = await _get_latest_release_for_update(owner, repo, remote_id)
                if release:
                    latest_version = release.get("tag_name", "")
                    current_version = integration.version or ""
                    has_update = GitHubClient.compare_versions(
                        current_version, latest_version
                    )

                    # Calculate total downloads from all release assets
                    total_downloads = 0
                    assets = release.get("assets", [])
                    for asset in assets:
                        total_downloads += asset.get("download_count", 0)

                    version_updates[integration.driver_id] = {
                        "current": current_version,
                        "latest": latest_version,
                        "has_update": has_update,
                        "downloads": total_downloads,
                    }

                    # Send notification for update available
                    if has_update:
                        # _LOG.info(
                        #     "Update available for %s: %s -> %s (cache refresh)",
                        #     integration.name,
                        #     current_version,
                        #     latest_version,
                        # )
                        try:
                            nm = get_notification_manager(remote_id)
                            _LOG.debug(
                                "Sending notification for %s",
                                integration.name,
                            )
                            await nm.notify_integration_update_available(
                                integration.driver_id,
                                integration.name,
                                current_version,
                                latest_version,
                            )
                            _LOG.debug(
                                "send_notification_sync completed for %s",
                                integration.name,
                            )
                        except Exception as notify_error:
                            _LOG.error(
                                "Failed to send update notification: %s", notify_error
                            )
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


async def _get_installed_integrations(
    remote_id: str | None = None,
) -> list[IntegrationInfo]:
    """Get list of installed integrations with metadata.

    This includes:
    - Configured instances (drivers with instances)
    - Installed drivers without instances (needs configuration)

    Excludes LOCAL (firmware) drivers unless they have an instance configured.

    driver_type values from API:
    - CUSTOM: installed on the remote via tar.gz
    - EXTERNAL: running in Docker or external server
    - LOCAL: built into firmware

    :param remote_id: Remote identifier to get integrations from (uses active if None)
    """
    if remote_id is None:
        remote_id = get_active_remote_id()

    client = _remote_clients.get(remote_id) if remote_id else None
    if not client:
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
        instances = await client.get_integrations()
    except SyncAPIError as e:
        _LOG.error("Failed to get integrations: %s", e)
        instances = []

    # Build set of configured driver IDs
    for instance in instances:
        configured_driver_ids.add(instance.get("driver_id", ""))

    # Get all drivers
    try:
        drivers = await client.get_drivers()
    except SyncAPIError as e:
        _LOG.error("Failed to get drivers: %s", e)
        drivers = []

    # Build driver lookup
    driver_lookup = {d.get("driver_id", ""): d for d in drivers}

    # Process configured instances first
    for instance in instances:
        driver_id = instance.get("driver_id", "")
        driver = driver_lookup.get(driver_id, {})

        developer = driver.get("developer", {}).get("name", "") or driver.get(
            "developer_name", ""
        )
        home_page = driver.get("developer", {}).get("url", "")
        driver_type = driver.get("driver_type", "CUSTOM")
        driver_name = (
            driver.get("name", {}).get("en", driver_id) if driver else driver_id
        )

        # Map driver_type to our flags (official = LOCAL firmware integrations)
        is_official = driver_type == "LOCAL"
        is_external = driver_type == "EXTERNAL"
        is_custom = driver_type == "CUSTOM"

        # Check registry for supports_backup flag, self_managed flag, and repository URL fallback
        # Use fuzzy matching since driver_id may not match registry id exactly
        registry_item = find_registry_item(driver_id, driver_name)
        supports_backup = registry_item.get("supports_backup", False)
        self_managed = registry_item.get("self_managed", False)

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
            self_managed=self_managed,
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
                # _LOG.debug(
                #     "Update available for %s: %s -> %s (from cache)",
                #     driver_id,
                #     info.version,
                #     info.latest_version,
                # )

                # Show update button for custom integrations (but not self_managed ones)
                info.can_update = not self_managed
                # _LOG.debug(
                #     "Update button enabled for %s (can_update=True, can_auto_update will be determined)",
                #     driver_id,
                # )

                # Check if automated backup/restore is possible
                # Requires: supports_backup AND version >= backup_min_version (if specified)
                min_version = registry_item.get("backup_min_version")
                info.can_auto_update = supports_backup

                if min_version and supports_backup:
                    try:
                        if Version(info.version) < Version(min_version):
                            info.can_auto_update = False
                            # _LOG.debug(
                            #     "Update available for %s: %s -> %s (requires manual reconfiguration - version %s < minimum %s)",
                            #     driver_id,
                            #     info.version,
                            #     info.latest_version,
                            #     info.version,
                            #     min_version,
                            # )
                    except (InvalidVersion, TypeError):
                        # If version parsing fails, allow auto update if supports_backup
                        pass

        integrations.append(info)

        # Check for error states and send notification
        # Notify for ERROR or DISCONNECTED states (both indicate problems)
        state_upper = info.state.upper() if info.state else ""
        if state_upper and ("ERROR" in state_upper or state_upper == "DISCONNECTED"):
            _LOG.info("Integration %s in problem state: %s", info.name, info.state)
            try:
                nm = get_notification_manager(remote_id)
                await nm.notify_integration_error_state(
                    driver_id, info.name, info.state
                )
            except Exception as notify_error:
                _LOG.error("Failed to send error state notification: %s", notify_error)
        elif state_upper in ("CONNECTED", "OK"):
            # Integration is in healthy state - clear any previous error notification
            # Only clear when truly healthy (CONNECTED/OK), not for intermediate states
            try:
                nm = get_notification_manager(remote_id)
                nm.clear_error_state(driver_id)
            except Exception as notify_error:
                _LOG.debug("Failed to clear error state: %s", notify_error)

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

        developer = driver.get("developer", {}).get("name", "") or driver.get(
            "developer_name", ""
        )
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
                # _LOG.debug(
                #     "Update button enabled for unconfigured %s (can_update=True)",
                #     driver_id,
                # )

                # Check if automated backup/restore is possible
                # Requires: supports_backup AND not self_managed AND version >= backup_min_version (if specified)
                min_version = registry_item.get("backup_min_version")
                info.can_auto_update = supports_backup and not self_managed

                if min_version and supports_backup and not self_managed:
                    try:
                        if Version(info.version) < Version(min_version):
                            info.can_auto_update = False
                            # _LOG.debug(
                            #     "Update available for %s: %s -> %s (requires manual reconfiguration - version %s < minimum %s)",
                            #     driver_id,
                            #     info.version,
                            #     info.latest_version,
                            #     info.version,
                            #     min_version,
                            # )
                    except (InvalidVersion, TypeError):
                        # If version parsing fails, allow auto update if supports_backup
                        pass

        integrations.append(info)

    return integrations


async def _get_available_integrations(
    remote_id: str | None = None,
) -> list[AvailableIntegration]:
    """
    Get list of available integrations from the registry.

    Returns a list of AvailableIntegration objects representing integrations
    that can be installed. Includes installed status checking.

    Also checks for new integrations in registry and sends notifications.

    :param remote_id: Remote identifier to check installed status against (uses active if None)
    """
    if remote_id is None:
        remote_id = get_active_remote_id()

    client = _remote_clients.get(remote_id) if remote_id else None

    registry = load_registry()

    # Get installed driver info for comparison
    installed_drivers = {}  # driver_id -> (driver_type, version)
    configured_driver_ids = {}  # driver_id -> instance_id
    driver_names = {}  # Map name -> (driver_id, driver_type, version) for fuzzy matching

    if client:
        try:
            # Get all drivers (installed)
            drivers = await client.get_drivers()
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
            for instance in await client.get_integrations():
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
        self_managed = item.get("self_managed", False)

        if is_installed and not is_official and not is_external:
            # Use the actual driver_id from the remote (not registry id) for cache lookup
            if actual_driver_id and actual_driver_id in _cached_version_data:
                version_info = _cached_version_data[actual_driver_id]
                if version_info.get("has_update"):
                    # Always mark that an update is available (for badge display)
                    update_available = True
                    latest_version = version_info.get("latest", "")

                    # Show update button for custom integrations (but not self_managed ones)
                    can_update = not self_managed

                    # Check if automated backup/restore is possible
                    # Requires: supports_backup AND version >= backup_min_version (if specified)
                    min_version = item.get("backup_min_version")
                    can_auto_update = supports_backup

                    if min_version and supports_backup and version:
                        try:
                            if Version(version) < Version(min_version):
                                can_auto_update = False
                                # _LOG.debug(
                                #     "Update available for %s: %s -> %s (requires manual reconfiguration - version %s < minimum %s)",
                                #     actual_driver_id,
                                #     version,
                                #     latest_version,
                                #     version,
                                #     min_version,
                                # )
                        except (InvalidVersion, TypeError):
                            # If version parsing fails, allow auto update if supports_backup
                            pass

        # Fetch repository stats from GitHub (cached)
        stars = 0
        created_at = ""
        pushed_at = ""
        downloads = 0

        if _github_client and home_page and "github.com" in home_page:
            try:
                parsed = GitHubClient.parse_github_url(home_page)
                if parsed:
                    owner, repo = parsed
                    repo_info = get_cached_repo_info(owner, repo, _github_client)
                    if repo_info:
                        stars = repo_info.get("stargazers_count", 0)
                        created_at = repo_info.get("created_at", "")
                        pushed_at = repo_info.get("pushed_at", "")
            except Exception as e:
                _LOG.debug("Failed to get repo info for %s: %s", name, e)

        # Get download count from version cache (populated during version checks)
        if actual_driver_id and actual_driver_id in _cached_version_data:
            downloads = _cached_version_data[actual_driver_id].get("downloads", 0)

        _cat_map = _get_category_name_map()
        categories_list = [_cat_map.get(c, c) for c in item.get("categories", [])]
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
            self_managed=self_managed,
            custom=not is_official,
            official=is_official,
            update_available=update_available,
            latest_version=latest_version,
            instance_id=instance_id,
            can_update=can_update,
            can_auto_update=can_auto_update,
            supports_backup=supports_backup,
            stars=stars,
            created_at=created_at,
            pushed_at=pushed_at,
            downloads=downloads,
            original_index=len(available),
        )
        available.append(avail)

    # Apply sorting based on settings
    ui_prefs = UIPreferences.load()
    sort_by = ui_prefs.sort_by
    sort_reverse = ui_prefs.sort_reverse

    if sort_by == "stars":
        available.sort(key=lambda x: x.stars, reverse=not sort_reverse)
    elif sort_by == "created":
        available.sort(key=lambda x: x.created_at or "", reverse=not sort_reverse)
    elif sort_by == "updated":
        available.sort(key=lambda x: x.pushed_at or "", reverse=not sort_reverse)
    elif sort_by == "name":
        available.sort(key=lambda x: x.name.lower(), reverse=sort_reverse)
    elif sort_by == "downloads":
        available.sort(key=lambda x: x.downloads, reverse=not sort_reverse)
    elif sort_by == "developer":
        available.sort(
            key=lambda x: x.developer.lower() if x.developer else "",
            reverse=sort_reverse,
        )
    # "original" or any other value = keep original registry order (no sorting needed)

    # Check for new integrations in registry and send notification
    try:
        nm = get_notification_manager(remote_id)
        # Use registry IDs (not actual_driver_ids) for tracking to avoid false positives
        # when installing integrations (actual_driver_id can differ from registry id)
        integration_data = [
            (item.get("id", ""), item.get("name", "")) for item in registry
        ]
        new_integrations = nm.update_registry_count(integration_data)
        if new_integrations:
            await nm.notify_new_integration_in_registry(new_integrations)
    except Exception as notify_error:
        _LOG.debug(
            "Failed to check/send new integration notification: %s", notify_error
        )

    return available


async def _can_backup_integration(
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
async def health():
    """Simple health check endpoint."""
    return "OK"


@app.route("/api/registry")
async def get_registry():
    """Serve the integrations registry (for local development/testing)."""
    registry_path = Path(__file__).parent / "integrations-registry.json"
    if registry_path.exists():
        with open(registry_path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"integrations": []})


@app.route("/")
async def index():
    """Render the main dashboard page."""
    return await render_template("index.html")


@app.route("/integrations")
async def integrations_page():
    """Render the integrations management page."""
    return await render_template("integrations.html")


@app.route("/available")
async def available_page():
    """Render the available integrations page."""
    return await render_template("available.html")


@app.route("/updating")
async def updating_page():
    """Render the self-update waiting page."""
    target_version = request.args.get("version", "")
    return await render_template("updating.html", target_version=target_version)


# =============================================================================
# HTMX Partial Routes
# =============================================================================


@app.route("/api/stats/installed-count")
async def get_installed_count():
    """Get the count of installed integrations.

    Counts drivers where:
    - driver_type is CUSTOM or EXTERNAL (always count)
    - driver_type is LOCAL only if it has a configured instance
    """
    remote_id = get_active_remote_id()
    if not _get_active_remote_client() or not is_remote_online(remote_id):
        return "0"

    try:
        # Get all installed integrations (includes configured and unconfigured)
        integrations = await _get_installed_integrations(remote_id)

        count = len(integrations)

        return str(count)
    except SyncAPIError as e:
        _LOG.error("Failed to get integrations count: %s", e)
        return "0"


@app.route("/api/stats/updates-count")
async def get_updates_count():
    """Get the count of integrations with available updates."""
    remote_id = get_active_remote_id()
    if (
        not _get_active_remote_client()
        or not _github_client
        or not is_remote_online(remote_id)
    ):
        return "0"

    try:
        integrations = await _get_installed_integrations(remote_id)
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
async def get_integrations_list():
    """Get HTML partial with list of installed integrations."""
    if not _get_active_remote_client():
        return (
            "<div class='text-red-600 dark:text-red-400'>Service not initialized</div>"
        )
    if not is_remote_online(get_active_remote_id()):
        return _render_offline_partial()

    try:
        remote_id = get_active_remote_id()
        integrations = await _get_installed_integrations(remote_id)

        # Check if driver list changed (new/removed drivers) and refresh cache if needed
        current_driver_ids = {i.driver_id for i in integrations}
        if current_driver_ids != _cached_driver_ids:
            _LOG.info("Driver list changed, refreshing version cache...")
            await _refresh_version_cache(remote_id)
            # Re-fetch integrations with updated cache
            integrations = await _get_installed_integrations(remote_id)

        settings = Settings.load(remote_id=get_active_remote_id())
        remote_ip = (
            _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
            if _get_active_remote_client()
            else None
        )
        current_url = request.headers.get("HX-Current-URL", "")
        parsed_path = urlparse(current_url).path.rstrip("/")
        dashboard = parsed_path in ("", "/")
        return await render_template(
            "partials/integration_list.html",
            integrations=integrations,
            remote_ip=remote_ip,
            settings=settings,
            dashboard=dashboard,
        )
    except Exception as e:
        _LOG.error("Failed to get integrations: %s", e)
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>"


@app.route("/api/integrations/available")
async def get_available_list():
    """Get HTML partial with list of available integrations."""
    remote_id = get_active_remote_id()
    if remote_id and not is_remote_online(remote_id):
        return _render_offline_partial()
    try:
        available = await _get_available_integrations(remote_id)
        remote_ip = (
            _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
            if _get_active_remote_client()
            else None
        )
        return await render_template(
            "partials/available_list.html",
            integrations=available,
            remote_ip=remote_ip,
        )
    except Exception as e:
        _LOG.error("Failed to get available integrations: %s", e)
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>"


@app.route("/api/integrations/refresh-versions", methods=["POST"])
async def refresh_versions():
    """Manually refresh version cache for all integrations."""
    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        _LOG.info("Manual version cache refresh requested")
        await _refresh_version_cache(get_active_remote_id())
        return jsonify({"status": "success", "message": "Version cache refreshed"})
    except Exception as e:
        _LOG.error("Failed to refresh version cache: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/integration/<instance_id>")
async def get_integration_detail(instance_id: str):
    """Get HTML partial with integration details."""
    if not _get_active_remote_client():
        return (
            "<div class='text-red-600 dark:text-red-400'>Service not initialized</div>"
        )

    try:
        # Find the integration in the list
        integrations = await _get_installed_integrations(get_active_remote_id())
        integration = next(
            (i for i in integrations if i.instance_id == instance_id), None
        )
        if integration:
            return await render_template(
                "partials/integration_detail.html", integration=integration
            )
        return "<div class='text-yellow-700 dark:text-yellow-400'>Integration not found</div>"
    except Exception as e:
        _LOG.error("Failed to get integration detail: %s", e)
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>"


@app.route("/api/integration/<instance_id>/update", methods=["POST"])
async def update_integration(instance_id: str):
    """
    Update an existing integration to the latest or specified version using default settings.

    Accepts optional 'version' query parameter to update to a specific version.

    The register_entities behavior is determined by the user's auto_register_entities setting.
    """
    settings = Settings.load(remote_id=get_active_remote_id())
    form = await request.form
    version = request.args.get("version") or form.get("version")
    return await _perform_update_integration(
        instance_id, settings.auto_register_entities, version
    )


@app.route("/api/integration/<instance_id>/update-alt", methods=["POST"])
async def update_integration_alt(instance_id: str):
    """
    Update an existing integration with the opposite entity registration behavior.

    Accepts optional 'version' query parameter to update to a specific version.

    If auto_register_entities is enabled, this will NOT register entities.
    If auto_register_entities is disabled, this WILL register entities.
    """
    settings = Settings.load(remote_id=get_active_remote_id())
    form = await request.form
    version = request.args.get("version") or form.get("version")
    return await _perform_update_integration(
        instance_id, not settings.auto_register_entities, version
    )


async def _perform_update_integration(
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
    global _operation_in_progress, _operation_lock_acquired_at

    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    if conflict := await _try_acquire_operation_lock(f"update instance {instance_id}"):
        return conflict

    backup_data = None
    previous_version = None

    try:
        # Find the integration to get its GitHub URL
        remote_id = get_active_remote_id()
        integrations = await _get_installed_integrations(remote_id)
        integration = next(
            (i for i in integrations if i.instance_id == instance_id), None
        )

        if not integration:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - integration %s not found", instance_id)
            return jsonify({"status": "error", "message": "Integration not found"}), 404

        if integration.official:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - integration %s is official", instance_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "Official integrations are managed by firmware updates",
                }
            ), 400

        if not integration.home_page or "github.com" not in integration.home_page:
            async with _operation_lock:
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
        # Only block downgrade if current version > migration_required_at and target version < migration_required_at
        if version and migration_required_at and integration.version:
            clean_version = version.lstrip("v")
            clean_current_version = integration.version.lstrip("v")
            try:
                current_ver = Version(clean_current_version)
                target_ver = Version(clean_version)
                migration_ver = Version(migration_required_at)

                # Block only if: current > migration_required_at AND target < migration_required_at
                # Version at migration_required_at and above are safe (they have the new entity format)
                if current_ver >= migration_ver and target_ver < migration_ver:
                    async with _operation_lock:
                        _operation_in_progress = False
                    _LOG.warning(
                        "Downgrade blocked for %s - current version %s > migration boundary %s, cannot downgrade to %s",
                        integration.driver_id,
                        integration.version,
                        migration_required_at,
                        version,
                    )
                    return jsonify(
                        {
                            "status": "error",
                            "message": f"Cannot downgrade from {integration.version} to {version} - migration boundary at {migration_required_at} prevents this downgrade",
                        }
                    ), 400
            except InvalidVersion as e:
                async with _operation_lock:
                    _operation_in_progress = False
                _LOG.warning(
                    "Invalid version format %s or %s: %s",
                    version,
                    integration.version,
                    e,
                )
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
                configured_entities = (
                    await _get_active_remote_client().get_configured_entities(  # ty:ignore[unresolved-attribute]
                        instance_id
                    )
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
                    client = _get_active_remote_client()
                    if not client:
                        async with _operation_lock:
                            _operation_in_progress = False
                        return jsonify(
                            {"status": "error", "message": "Service not initialized"}
                        ), 500

                    backup_data = await backup_integration(
                        client,
                        integration.driver_id,
                        save_to_file=True,
                        remote_id=get_active_remote_id(),
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
                        async with _operation_lock:
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
                    async with _operation_lock:
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
        parsed = GitHubClient.parse_github_url(integration.home_page)
        if not parsed:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - could not parse GitHub URL for integration %s",
                    instance_id,
                )
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Check if registry has an asset_pattern for this integration
        registry = load_registry()
        asset_pattern = next(
            (
                item.get("asset_pattern")
                for item in registry
                if item.get("driver_id") == integration.driver_id
                or item.get("id") == integration.driver_id
            ),
            None,
        )

        # Download the specified or latest release
        if version:
            _LOG.info(
                "Updating integration %s to version %s", integration.driver_id, version
            )
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern, version=version
            )
        else:
            _LOG.info(
                "Updating integration %s to latest version", integration.driver_id
            )
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern
            )
        if not download_result:
            async with _operation_lock:
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
            await _get_active_remote_client().delete_driver(integration.driver_id)  # ty:ignore[unresolved-attribute]
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
                async with _operation_lock:
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
        await _get_active_remote_client().install_integration(archive_data, filename)  # ty:ignore[unresolved-attribute]

        # Brief pause to let installation settle
        await asyncio.sleep(API_DELAY * 2)

        # Post-installation verification - give the remote time to process the driver
        _LOG.debug("Waiting for driver to be ready: %s", integration.driver_id)
        await (
            _get_active_remote_client().get_drivers()  # ty:ignore[unresolved-attribute]
        )  # Verify driver is available

        # Additional pause to ensure driver is fully initialized
        await asyncio.sleep(API_DELAY * 3)

        # Get current version once after installation for migration use
        current_version = ""
        should_check_migration = False

        if previous_version:
            # Get current version for migration checks
            driver_info = await _get_active_remote_client().get_driver(  # ty:ignore[unresolved-attribute]
                integration.driver_id
            )
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
            # Check if the new version supports backup/restore before attempting restore
            can_restore = True

            # Reuse registry loaded earlier to get backup_min_version
            min_backup_version = next(
                (
                    entry.get("backup_min_version")
                    for entry in registry
                    if entry.get("driver_id") == integration.driver_id
                ),
                None,
            )

            if min_backup_version and current_version:
                try:
                    if Version(current_version) < Version(min_backup_version):
                        can_restore = False
                        _LOG.warning(
                            "Cannot restore configuration for %s: installed version %s is below minimum backup version %s",
                            integration.driver_id,
                            current_version,
                            min_backup_version,
                        )
                        _LOG.info(
                            "User will need to manually reconfigure %s",
                            integration.driver_id,
                        )
                except (InvalidVersion, TypeError) as e:
                    _LOG.warning("Failed to compare versions for restore check: %s", e)

            if not can_restore:
                _LOG.info(
                    "Skipping restore for %s - version too old to support backup/restore",
                    integration.driver_id,
                )
                # Don't attempt restore, user will need to reconfigure
                backup_data = None

        if backup_data and is_configured:
            try:
                _LOG.info(
                    "Starting configuration restore for %s", integration.driver_id
                )

                # Step 1: Start setup for restore
                await _get_active_remote_client().start_setup(  # ty:ignore[unresolved-attribute]
                    integration.driver_id, reconfigure=False
                )
                _LOG.info("Started setup for restore (reconfigure=false)")

                await asyncio.sleep(
                    API_DELAY * 4
                )  # Give more time for setup to initialize

                # Step 1a: Check for migration metadata in the setup response
                setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                    integration.driver_id
                )
                _LOG.debug("Initial setup response: %s", setup_response)

                # Check if setup is in the right state
                setup_state = setup_response.get("state", "")
                if setup_state != "WAIT_USER_ACTION":
                    _LOG.warning(
                        "Setup not ready yet (state: %s), waiting longer...",
                        setup_state,
                    )
                    await asyncio.sleep(API_DELAY * 2)
                    setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                        integration.driver_id
                    )
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
                await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
                    integration.driver_id, {"restore_from_backup": "true"}
                )
                _LOG.info("Initiated restore mode")

                # Brief pause between API calls
                await asyncio.sleep(API_DELAY * 2)

                # Step 3: PUT /intg/setup/{driver_id} with restore data
                # The backup_data is a JSON string that needs to be properly escaped
                try:
                    # Parse the backup data to ensure it's valid JSON, then re-serialize for proper escaping
                    parsed_backup = json.loads(backup_data)
                    escaped_backup_data = json.dumps(parsed_backup)
                except json.JSONDecodeError as e:
                    _LOG.warning("Backup data is not valid JSON, using as-is: %s", e)
                    escaped_backup_data = backup_data

                await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
                    integration.driver_id,
                    {
                        "restore_from_backup": "true",
                        "restore_data": escaped_backup_data,
                    },
                )

                await asyncio.sleep(API_DELAY * 6)

                # Post-restore verification calls (like official tool)
                _LOG.info(
                    "Performing post-restore verification for %s", integration.driver_id
                )
                await _get_active_remote_client().get_enabled_integrations()  # ty:ignore[unresolved-attribute]

                # Get enabled instances and find our restored instance
                enabled_instances = (
                    await _get_active_remote_client().get_enabled_instances()  # ty:ignore[unresolved-attribute]
                )
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

                await _get_active_remote_client().get_instantiable_drivers()  # ty:ignore[unresolved-attribute]
                await _get_active_remote_client().get_driver(integration.driver_id)  # ty:ignore[unresolved-attribute]

                # Complete the setup flow (like official tool)
                await _get_active_remote_client().complete_setup(integration.driver_id)  # ty:ignore[unresolved-attribute]

                # Final verification call after DELETE (like official tool)
                await _get_active_remote_client().get_enabled_instances()  # ty:ignore[unresolved-attribute]

                _LOG.info(
                    "Configuration restored successfully for %s", integration.driver_id
                )

                # Step 4: Register ALL entities before migration (only if migration is possible)
                # Migration needs entities to exist on Remote to update activities
                all_entities = []
                if migration_possible and restored_instance_id:
                    await _get_active_remote_client().get_enabled_instances()  # ty:ignore[unresolved-attribute]

                    all_entities = (
                        await _get_active_remote_client().get_instance_entities(  # ty:ignore[unresolved-attribute]
                            restored_instance_id
                        )
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
                        await asyncio.sleep(API_DELAY * 5)
                        _LOG.info(
                            "Registering ALL %d entities before migration for instance %s",
                            len(all_entity_ids),
                            restored_instance_id,
                        )
                        _LOG.debug("All entity IDs: %s", all_entity_ids)

                        try:
                            await _get_active_remote_client().register_entities(  # ty:ignore[unresolved-attribute]
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
                        await _get_active_remote_client().start_setup(  # ty:ignore[unresolved-attribute]
                            integration.driver_id, reconfigure=True
                        )
                        _LOG.info("Started setup mode for migration")

                        await asyncio.sleep(API_DELAY)

                        # GET to read the configuration mode screen
                        setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                            integration.driver_id
                        )
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
                        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
                            integration.driver_id,
                            {"choice": choice_id, "action": "migrate"},
                        )
                        _LOG.debug(
                            "Selected 'migrate' action for device: %s", choice_id
                        )

                        await asyncio.sleep(API_DELAY * 2)

                        # Step 4b: GET the next setup page after selecting migrate
                        setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                            integration.driver_id
                        )
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
                        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
                            integration.driver_id,
                            {"previous_version": previous_version},
                        )
                        _LOG.debug("Sent previous_version: %s", previous_version)

                        await asyncio.sleep(API_DELAY * 2)

                        # GET the migration execution screen (asks for remote_url, pin, etc.)
                        setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                            integration.driver_id
                        )
                        _LOG.debug("Migration execution screen: %s", setup_response)

                        # Prepare migration data
                        remote_url = (
                            _get_active_remote_client()._address or "http://localhost"  # ty:ignore[unresolved-attribute]
                        )
                        remote_api_key = _get_active_remote_client()._api_key or ""  # ty:ignore[unresolved-attribute]

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

                        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
                            integration.driver_id, migration_input
                        )
                        _LOG.debug("Migration execution data sent successfully")

                        await asyncio.sleep(
                            API_DELAY * 4
                        )  # Give more time for migration to process

                        # GET to read the migration mappings response
                        setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                            integration.driver_id
                        )
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
                        await _get_active_remote_client().complete_setup(  # ty:ignore[unresolved-attribute]
                            integration.driver_id
                        )

                    except Exception as e:
                        _LOG.warning(
                            "Failed to execute migration for %s: %s",
                            integration.driver_id,
                            e,
                        )

                # Step 6: Register configured entities
                if restored_instance_id and register_entities and configured_entity_ids:
                    # Wait for instance to be CONNECTED before registering
                    device_state_step6 = "UNKNOWN"
                    for attempt in range(12):  # up to ~18 seconds
                        instance_detail_step6 = (
                            await _get_active_remote_client().get_instance(  # ty:ignore[unresolved-attribute]
                                restored_instance_id
                            )
                        )
                        device_state_step6 = instance_detail_step6.get(
                            "device_state", "UNKNOWN"
                        )
                        _LOG.info(
                            "Step 6 pre-register: instance %s state=%s (attempt %d/12)",
                            restored_instance_id,
                            device_state_step6,
                            attempt + 1,
                        )
                        if device_state_step6 == "CONNECTED":
                            break
                        await asyncio.sleep(API_DELAY * 2)
                    if device_state_step6 != "CONNECTED":
                        _LOG.warning(
                            "Instance %s not CONNECTED before entity registration (state: %s)",
                            restored_instance_id,
                            device_state_step6,
                        )
                    else:
                        # Brief settle time after connection before registering entities
                        await asyncio.sleep(2)

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
                            await _get_active_remote_client().delete_all_entities(  # ty:ignore[unresolved-attribute]
                                restored_instance_id
                            )
                            _LOG.info("All entities deleted")

                            await asyncio.sleep(API_DELAY * 2)

                            # Re-register only the configured entities (now with updated IDs from migration)
                            _LOG.info(
                                "Re-registering %d configured entities for instance %s",
                                len(configured_entity_ids),
                                restored_instance_id,
                            )
                            _LOG.info(
                                "Configured entity IDs: %s", configured_entity_ids
                            )

                            await _get_active_remote_client().register_entities(  # ty:ignore[unresolved-attribute]
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

                        for reg_attempt in range(4):
                            try:
                                await _get_active_remote_client().register_entities(  # ty:ignore[unresolved-attribute]
                                    restored_instance_id, configured_entity_ids
                                )
                                _LOG.info(
                                    "Registered %d configured entities (attempt %d)",
                                    len(configured_entity_ids),
                                    reg_attempt + 1,
                                )
                            except SyncAPIError as e:
                                _LOG.warning(
                                    "Failed to register entities for instance %s (attempt %d): %s",
                                    restored_instance_id,
                                    reg_attempt + 1,
                                    e,
                                )
                                break

                            # Verify registration stuck
                            await asyncio.sleep(3)
                            verified = await _get_active_remote_client().get_configured_entities(  # ty:ignore[unresolved-attribute]
                                restored_instance_id
                            )
                            verified_ids = [
                                str(e.get("entity_id", "")) for e in verified
                            ]
                            _LOG.info(
                                "Post-register verification (attempt %d): configured=%s",
                                reg_attempt + 1,
                                verified_ids,
                            )
                            if verified_ids:
                                _LOG.info("Entity registration confirmed")
                                break
                            _LOG.warning(
                                "Entities not confirmed after registration (attempt %d/4) - retrying",
                                reg_attempt + 1,
                            )
                            await asyncio.sleep(API_DELAY * 4)

                _LOG.info("Update completed successfully for %s", integration.driver_id)

            except SyncAPIError as e:
                _LOG.error(
                    "Failed to restore configuration for %s: %s",
                    integration.driver_id,
                    e,
                )
                # Try to clean up setup flow even on failure (twice like official tool)
                try:
                    await _get_active_remote_client().complete_setup(  # ty:ignore[unresolved-attribute]
                        integration.driver_id
                    )
                    # Final verification call after double DELETE
                    await _get_active_remote_client().get_enabled_instances()  # ty:ignore[unresolved-attribute]
                    await asyncio.sleep(API_DELAY)  # Brief pause after cleanup
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
                    await _get_active_remote_client().complete_setup(  # ty:ignore[unresolved-attribute]
                        integration.driver_id
                    )
                    await _get_active_remote_client().complete_setup(  # ty:ignore[unresolved-attribute]
                        integration.driver_id
                    )
                    # Final verification call after double DELETE
                    await _get_active_remote_client().get_enabled_instances()  # ty:ignore[unresolved-attribute]
                    await asyncio.sleep(API_DELAY)  # Brief pause after cleanup
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

            # Clear the notified update state since user has updated
            try:
                nm = get_notification_manager(get_active_remote_id())
                nm.clear_update_notification(
                    integration.driver_id,
                    _cached_version_data[integration.driver_id]["latest"],
                )
            except Exception as notify_error:
                _LOG.debug(
                    "Failed to clear update notification state: %s", notify_error
                )

        # Release operation lock
        async with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after successful update of instance %s", instance_id
            )

        # Brief delay to ensure remote has processed the update
        await asyncio.sleep(API_DELAY)

        # Re-fetch the integration info with updated version
        integrations = await _get_installed_integrations(remote_id)
        updated_integration = next(
            (i for i in integrations if i.driver_id == integration.driver_id), None
        )

        if updated_integration:
            # Return the updated card HTML
            settings = Settings.load(remote_id=remote_id)
            remote_ip = (
                _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
                if _get_active_remote_client()
                else None
            )
            return await render_template(
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
            settings = Settings.load(remote_id=get_active_remote_id())
            remote_ip = (
                _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
                if _get_active_remote_client()
                else None
            )
            return await render_template(
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
        async with _operation_lock:
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
        async with _operation_lock:
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
    finally:
        # Safety net: ensure lock is released even on asyncio.CancelledError
        # (CancelledError is BaseException, not Exception, so won't be caught above)
        _operation_in_progress = False
        _operation_lock_acquired_at = None


@app.route("/api/driver/<driver_id>/update", methods=["POST"])
async def update_driver(driver_id: str):
    """
    Update an unconfigured driver to the latest or specified version.

    Accepts optional 'version' query parameter to update to a specific version.

    This is used when a driver is installed but not configured (no instance exists).
    Since there's no instance, there's nothing to backup or restore - just download
    and install the new version.
    """
    global _operation_in_progress, _operation_lock_acquired_at

    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    # Get optional version parameter from query string or form data
    _form = await request.form
    version = request.args.get("version") or _form.get("version")

    if conflict := await _try_acquire_operation_lock(f"update driver {driver_id}"):
        return conflict

    try:
        # Find the driver to get its GitHub URL
        remote_id = get_active_remote_id()
        integrations = await _get_installed_integrations(remote_id)
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        if not integration:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - driver %s not found", driver_id)
            return jsonify({"status": "error", "message": "Driver not found"}), 404

        if integration.official:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - driver %s is official", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "Official integrations are managed by firmware updates",
                }
            ), 400

        if not integration.home_page or "github.com" not in integration.home_page:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - driver %s has no GitHub URL", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this driver",
                }
            ), 400

        # Check migration boundary if version specified and integration already installed
        # Only block downgrade if current version > migration_required_at and target version < migration_required_at
        if version and integration.version:
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
                            clean_current_version = integration.version.lstrip("v")
                            current_ver = Version(clean_current_version)
                            target_ver = Version(clean_version)
                            migration_ver = Version(migration_required_at)

                            # Block only if: current > migration_required_at AND target < migration_required_at
                            # Version at migration_required_at and above are safe (they have the new entity format)
                            if (
                                current_ver >= migration_ver
                                and target_ver < migration_ver
                            ):
                                async with _operation_lock:
                                    _operation_in_progress = False
                                _LOG.warning(
                                    "Downgrade blocked for %s - current version %s > migration boundary %s, cannot downgrade to %s",
                                    driver_id,
                                    integration.version,
                                    migration_required_at,
                                    version,
                                )
                                return jsonify(
                                    {
                                        "status": "error",
                                        "message": f"Cannot downgrade from {integration.version} to {version} - migration boundary at {migration_required_at} prevents this downgrade",
                                    }
                                ), 400
                        break
            except (InvalidVersion, Exception) as e:
                async with _operation_lock:
                    _operation_in_progress = False
                _LOG.warning("Version validation failed for %s: %s", version, e)
                return jsonify(
                    {"status": "error", "message": f"Invalid version: {version}"}
                ), 400

        # Parse GitHub URL
        parsed = GitHubClient.parse_github_url(integration.home_page)
        if not parsed:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - could not parse GitHub URL for driver %s",
                    driver_id,
                )
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Check if registry has an asset_pattern for this integration
        registry = load_registry()
        asset_pattern = next(
            (
                item.get("asset_pattern")
                for item in registry
                if item.get("driver_id") == driver_id or item.get("id") == driver_id
            ),
            None,
        )

        # Download the specified or latest release
        if version:
            _LOG.info("Updating driver %s to version %s", driver_id, version)
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern, version=version
            )
        else:
            _LOG.info("Updating driver %s to latest version", driver_id)
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern
            )
        if not download_result:
            async with _operation_lock:
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
            await _get_active_remote_client().delete_driver(driver_id)  # ty:ignore[unresolved-attribute]
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
                async with _operation_lock:
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
        await _get_active_remote_client().install_integration(archive_data, filename)  # ty:ignore[unresolved-attribute]
        _LOG.info("Updated driver %s successfully", integration.name)

        # Wait for the specific driver to appear in the driver list
        # Poll up to 10 times (5 seconds total) to ensure new driver is registered
        driver_found = False
        for attempt in range(10):
            await asyncio.sleep(0.5)
            try:
                drivers = await _get_active_remote_client().get_drivers()  # ty:ignore[unresolved-attribute]
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
        await asyncio.sleep(1.0)

        # Update just this driver's cache entry instead of refreshing everything
        # This avoids GitHub rate limiting issues from rapid consecutive API calls
        if driver_id in _cached_version_data:
            # Driver was updated to latest version, so no update is available anymore
            _cached_version_data[driver_id]["has_update"] = False
            _cached_version_data[driver_id]["current"] = _cached_version_data[
                driver_id
            ]["latest"]
            _LOG.debug("Updated cache for %s: marked as current version", driver_id)

            # Clear the notified update state since user has updated
            try:
                nm = get_notification_manager(get_active_remote_id())
                nm.clear_update_notification(
                    driver_id, _cached_version_data[driver_id]["latest"]
                )
            except Exception as notify_error:
                _LOG.debug(
                    "Failed to clear update notification state: %s", notify_error
                )

        # Release operation lock
        async with _operation_lock:
            _operation_in_progress = False
            _LOG.info("Lock released after successful update of driver %s", driver_id)

        # Brief delay to ensure remote has processed the update
        await asyncio.sleep(API_DELAY)

        # Re-fetch the integration info with updated version from available list
        # Since this is for unconfigured drivers, we use _get_available_integrations
        available = await _get_available_integrations(remote_id)
        updated_integration = next(
            (i for i in available if i.driver_id == driver_id), None
        )

        remote_ip = (
            _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
            if _get_active_remote_client()
            else None
        )

        if updated_integration:
            # Return the updated card HTML for available list
            return await render_template(
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
            integrations = await _get_installed_integrations(remote_id)
            integration = next(
                (i for i in integrations if i.driver_id == driver_id), None
            )

            if integration:
                settings = Settings.load(remote_id=get_active_remote_id())
                return await render_template(
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
                _cat_map = _get_category_name_map()
                categories_list = [
                    _cat_map.get(c, c) for c in registry_item.get("categories", [])
                ]
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
                return await render_template(
                    "partials/available_card.html",
                    integration=fallback_integration,
                    remote_ip=remote_ip,
                    just_updated=True,
                )

    except SyncAPIError as e:
        _LOG.error("Update failed: %s", e)
        error_msg = str(e).replace('"', "&quot;")

        # Release operation lock
        async with _operation_lock:
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
        async with _operation_lock:
            _operation_in_progress = False
            _operation_lock_acquired_at = None
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
    finally:
        # Safety net: ensure lock is released even on asyncio.CancelledError
        _operation_in_progress = False
        _operation_lock_acquired_at = None


@app.route("/api/integration/<driver_id>/update-confirm")
async def get_update_confirmation(driver_id: str):
    """
    Get update confirmation modal for integrations without backup support.

    Returns HTML warning that configuration cannot be preserved.
    """
    if not _get_active_remote_client():
        return "<p class='text-red-600 dark:text-red-400'>Service not initialized</p>"

    try:
        # Get integration details
        integrations = await _get_installed_integrations(get_active_remote_id())
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        if not integration:
            return "<p class='text-red-600 dark:text-red-400'>Integration not found</p>"

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

        return await render_template(
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
        return f"<p class='text-red-600 dark:text-red-400'>Error: {str(e)}</p>"


@app.route("/api/integration/<driver_id>/delete-confirm")
async def get_delete_confirmation(driver_id: str):
    """
    Get delete confirmation modal content for an integration.

    Returns HTML to be displayed in the modal with delete options.
    """
    if not _get_active_remote_client():
        return "<p class='text-red-600 dark:text-red-400'>Service not initialized</p>"

    try:
        # Get integration name for display
        remote_id = get_active_remote_id()
        integrations = await _get_installed_integrations(remote_id)
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        # Also check available list for unconfigured drivers
        if not integration:
            available = await _get_available_integrations(remote_id)
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

        return await render_template(
            "partials/modal_delete_confirm.html",
            driver_id=driver_id,
            integration_name=integration_name,
            is_configured=is_configured,
        )
    except Exception as e:
        _LOG.error("Error loading delete confirmation for %s: %s", driver_id, e)
        return f"<p class='text-red-600 dark:text-red-400'>Error: {str(e)}</p>"


@app.route("/api/integration/<driver_id>/delete", methods=["DELETE"])
async def delete_integration(driver_id: str):
    """
    Delete an integration - either just the configuration or the entire integration.

    Query parameters:
    - type: 'configuration' or 'full'
    """
    global _operation_in_progress, _operation_lock_acquired_at

    if not _get_active_remote_client():
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    if conflict := await _try_acquire_operation_lock(f"delete {driver_id}"):
        return conflict

    remote_id = get_active_remote_id()
    delete_type = request.args.get("type", "configuration")
    _LOG.info("Delete request for %s: type=%s", driver_id, delete_type)

    try:
        # Check if integration is configured by checking for instances
        is_configured = False
        instance_id = f"{driver_id}.main"

        try:
            remote_id = get_active_remote_id()
            integrations = await _get_installed_integrations(remote_id)
            # Check if any integration has this instance_id and is not NOT_CONFIGURED
            is_configured = any(
                i.instance_id == instance_id and i.state != "NOT_CONFIGURED"
                for i in integrations
            )
        except Exception:
            pass

        # Only delete instance if it's actually configured
        if is_configured:
            try:
                await _get_active_remote_client().delete_instance(instance_id)  # ty:ignore[unresolved-attribute]
                _LOG.info("Deleted instance: %s", instance_id)
            except SyncAPIError as e:
                _LOG.warning("Failed to delete instance %s: %s", instance_id, e)

        # If full delete, also delete the driver
        if delete_type == "full":
            # Small delay to let instance deletion complete
            await asyncio.sleep(API_DELAY * 2)

            try:
                await _get_active_remote_client().delete_driver(driver_id)  # ty:ignore[unresolved-attribute]
                _LOG.info("Deleted driver: %s", driver_id)
            except SyncAPIError as e:
                _LOG.error("Failed to delete driver %s: %s", driver_id, e)
                return jsonify(
                    {"status": "error", "message": f"Failed to delete driver: {e}"}
                ), 500

        # Small delay to ensure remote has processed
        await asyncio.sleep(API_DELAY)

        # Return updated card or empty response
        if delete_type == "full":
            # Full delete - remove the card entirely
            return "", 200
        else:
            # Configuration delete - return updated card showing unconfigured state
            integrations = await _get_installed_integrations(remote_id)
            integration = next(
                (i for i in integrations if i.driver_id == driver_id), None
            )

            if integration:
                settings = Settings.load(remote_id=remote_id)
                remote_ip = (
                    _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
                    if _get_active_remote_client()
                    else None
                )
                return await render_template(
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
    finally:
        async with _operation_lock:
            _operation_in_progress = False
            _operation_lock_acquired_at = None
            _LOG.info("Lock released after delete %s", driver_id)


async def _build_error_card(driver_id: str, registry: list, error_msg: str) -> str:
    """Build an error card HTML for a failed install."""
    registry_item = next((item for item in registry if item.get("id") == driver_id), {})

    # Convert registry item to AvailableIntegration structure
    _cat_map = _get_category_name_map()
    categories_list = [_cat_map.get(c, c) for c in registry_item.get("categories", [])]
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

    remote_ip = (
        _get_active_remote_client()._address if _get_active_remote_client() else None  # ty:ignore[unresolved-attribute]
    )
    return await render_template(
        "partials/available_card.html",
        integration=integration,
        remote_ip=remote_ip,
        install_error=error_msg,
    )


@app.route("/api/integration/<driver_id>/install", methods=["POST"])
async def install_integration(driver_id: str):
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
    global _operation_in_progress, _operation_lock_acquired_at

    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    # Get optional version parameter from query string or form data
    _form = await request.form
    version = request.args.get("version") or _form.get("version")

    if conflict := await _try_acquire_operation_lock(f"install {driver_id}"):
        return conflict

    try:
        # Find the integration in the registry
        registry = load_registry()
        integration = next(
            (item for item in registry if item.get("id") == driver_id), None
        )

        if not integration:
            async with _operation_lock:
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
                    async with _operation_lock:
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
                async with _operation_lock:
                    _operation_in_progress = False
                _LOG.warning("Invalid version format %s: %s", version, e)
                return jsonify(
                    {"status": "error", "message": f"Invalid version format: {version}"}
                ), 400

        repo_url = integration.get("repository", "")
        if not repo_url or "github.com" not in repo_url:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info("Lock released - no GitHub URL for integration %s", driver_id)
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this integration",
                }
            ), 400

        # Parse GitHub URL
        parsed = GitHubClient.parse_github_url(repo_url)
        if not parsed:
            async with _operation_lock:
                _operation_in_progress = False
                _LOG.info(
                    "Lock released - could not parse GitHub URL for integration %s",
                    driver_id,
                )
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Check if registry has an asset_pattern for this integration
        registry = load_registry()
        asset_pattern = next(
            (
                item.get("asset_pattern")
                for item in registry
                if item.get("driver_id") == driver_id or item.get("id") == driver_id
            ),
            None,
        )

        # Download the specified or latest release
        if version:
            _LOG.info("Installing %s version %s", driver_id, version)
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern, version=version
            )
        else:
            _LOG.info("Installing latest version of %s", driver_id)
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern
            )
        if not download_result:
            async with _operation_lock:
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
        await _get_active_remote_client().install_integration(archive_data, filename)  # ty:ignore[unresolved-attribute]
        _LOG.info("Installed integration %s successfully", integration.get("name"))

        # Release operation lock
        async with _operation_lock:
            _operation_in_progress = False
            _LOG.info("Lock released after successful install of %s", driver_id)

        # Return a replacement card HTML for HTMX outerHTML swap
        _cat_map = _get_category_name_map()
        categories_list = [
            _cat_map.get(c, c) for c in integration.get("categories", [])
        ]
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

        remote_ip = (
            _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
            if _get_active_remote_client()
            else None
        )
        return await render_template(
            "partials/available_card.html",
            integration=integration_obj,
            remote_ip=remote_ip,
            just_installed=True,
        )

    except SyncAPIError as e:
        _LOG.error("Install failed: %s", e)
        error_msg = str(e).replace('"', "&quot;").replace("'", "&#39;")

        # Release operation lock
        async with _operation_lock:
            _operation_in_progress = False
            _LOG.info(
                "Lock released after SyncAPIError in install_integration for %s",
                driver_id,
            )

        return await _build_error_card(driver_id, registry, error_msg), 200
    except Exception as e:
        _LOG.error("Unexpected error during install: %s", e)
        error_msg = str(e).replace('"', "&quot;").replace("'", "&#39;")

        # Release operation lock
        async with _operation_lock:
            _operation_in_progress = False
            _operation_lock_acquired_at = None
            _LOG.info(
                "Lock released after generic exception in install_integration for %s",
                driver_id,
            )

        return await _build_error_card(driver_id, registry, error_msg), 200
    finally:
        # Safety net: ensure lock is released even on asyncio.CancelledError
        _operation_in_progress = False
        _operation_lock_acquired_at = None


@app.route("/api/operation-lock/status")
async def get_operation_lock_status():
    """Return whether an install/update operation is currently in progress."""
    elapsed: float | None = None
    if _operation_in_progress and _operation_lock_acquired_at is not None:
        elapsed = round(time.monotonic() - _operation_lock_acquired_at)
    return jsonify({"locked": _operation_in_progress, "elapsed_seconds": elapsed})


@app.route("/api/operation-lock/release", methods=["POST"])
async def release_operation_lock():
    """Manually release a stuck operation lock."""
    global _operation_in_progress, _operation_lock_acquired_at
    async with _operation_lock:
        was_locked = _operation_in_progress
        elapsed = (
            round(time.monotonic() - _operation_lock_acquired_at)
            if _operation_in_progress and _operation_lock_acquired_at is not None
            else None
        )
        _operation_in_progress = False
        _operation_lock_acquired_at = None
    if was_locked:
        _LOG.warning(
            "Operation lock manually released by user (was held for %s seconds)",
            elapsed,
        )
    return jsonify(
        {"status": "ok", "was_locked": was_locked, "elapsed_seconds": elapsed}
    )


@app.route("/api/backup/all", methods=["POST"])
async def backup_all():
    """
    Backup all custom integrations' configurations.

    This triggers the backup flow for all CUSTOM driver types.
    """
    client = _get_active_remote_client()
    if not client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        results = await backup_all_integrations(
            client, remote_id=get_active_remote_id()
        )
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
async def backup_single(driver_id: str):
    """
    Backup a single integration's configuration.

    :param driver_id: The driver ID to backup
    """
    client = _get_active_remote_client()
    if not client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    _LOG.info("Starting backup for integration: %s", driver_id)

    try:
        backup_data = await backup_integration(
            client,
            driver_id,
            save_to_file=True,
            remote_id=get_active_remote_id(),
        )
        if backup_data:
            _LOG.info("Backup completed successfully for integration: %s", driver_id)
            return jsonify(
                {
                    "status": "ok",
                    "message": f"Successfully backed up {driver_id}",
                    "has_data": True,
                }
            )
        else:
            _LOG.warning("No backup data retrieved for integration: %s", driver_id)
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
async def get_backup_data(driver_id: str):
    """
    Get the stored backup data for an integration.

    :param driver_id: The driver ID
    """
    backup_data = get_backup(driver_id, remote_id=get_active_remote_id())
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
async def list_integration_backups():
    """List all stored integration config backups."""
    backups = get_all_backups()
    return jsonify(backups)


@app.route("/api/release-notes/unavailable/<version>")
async def get_release_notes_unavailable(version: str):
    """
    Return a user-friendly message when release notes cannot be fetched.

    Used when GitHub URL cannot be parsed or release info is unavailable.
    """
    return await render_template(
        "partials/modal_release_notes.html",
        error="Release notes are not available for this integration",
        version=version,
        github_url=None,
    )


@app.route("/api/release-notes/<owner>/<repo>/<version>")
async def get_release_notes(owner: str, repo: str, version: str):
    """
    Get release notes for a specific version and return HTML for modal.

    Renders markdown release notes as HTML.
    """
    if not _github_client:
        return await render_template(
            "partials/modal_release_notes.html",
            error="GitHub client not available",
            version=version,
        )

    try:
        # Fetch release info from GitHub
        release = await _github_client.get_release_by_tag(owner, repo, version)

        if not release:
            return await render_template(
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
        return await render_template(
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
        return await render_template(
            "partials/modal_release_notes.html",
            error=f"Error loading release notes: {str(e)}",
            version=version,
            github_url=f"https://github.com/{owner}/{repo}/releases/tag/{version}",
        )


@app.route("/api/version-selector/<owner>/<repo>/<driver_id>")
async def get_version_selector(owner: str, repo: str, driver_id: str):
    """
    Get version selector modal content with available releases.

    Fetches recent releases and filters by migration boundary from registry.
    Shows beta releases if enabled in settings.

    Pass ?self_update=true to use the self-update flow (Integration Manager
    updating itself via the bootstrapper). In that mode the install URL is
    /api/self-update and the version filter uses min_compatible_version instead
    of migration_required_at.
    """
    if not _github_client:
        return await render_template(
            "partials/modal_version_selector.html",
            error="GitHub client not available",
        )

    is_self_update = request.args.get("self_update", "").lower() in ("true", "1", "yes")

    try:
        # Load settings to check show_beta_releases
        settings = Settings.load(remote_id=get_active_remote_id())
        show_beta_releases = settings.show_beta_releases

        # Load registry to get version floor values:
        #   migration_required_at   – normal updates: versions <= this are excluded
        #   min_compatible_version  – self-update picker: versions < this are excluded
        #                             (separate from backup_min_version which gates can_auto_update)
        migration_required_at = None
        min_compatible_version = None
        is_update = False
        instance_id = None

        try:
            registry = load_registry()
            for entry in registry:
                if entry.get("id") == driver_id or entry.get("driver_id") == driver_id:
                    migration_required_at = entry.get("migration_required_at")
                    min_compatible_version = entry.get("min_compatible_version")
                    break
        except Exception as e:
            _LOG.warning("Failed to load registry for migration check: %s", e)

        # Version floor: for self-update use min_compatible_version (< filter),
        # for normal updates use migration_required_at (<= filter).
        version_floor = (
            min_compatible_version if is_self_update else migration_required_at
        )

        # Check if this is an update (driver installed) or fresh install
        integrations = await _get_installed_integrations(get_active_remote_id())
        integration = next((i for i in integrations if i.driver_id == driver_id), None)

        if integration:
            is_update = True
            instance_id = integration.instance_id

        # Fetch releases from GitHub
        releases_data = await _github_client.get_releases(owner, repo, limit=20)

        if not releases_data:
            return await render_template(
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

            # Check version floor
            if version_floor:
                try:
                    v = Version(clean_version)
                    floor = Version(version_floor)
                    # self-update: strict lower bound (< floor is excluded)
                    # normal update: migration boundary (<= floor is excluded)
                    if (is_self_update and v < floor) or (
                        not is_self_update and v <= floor
                    ):
                        _LOG.debug(
                            "Filtering out %s (floor=%s, self_update=%s)",
                            tag_name,
                            version_floor,
                            is_self_update,
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
        filtered_releases = (beta_releases + stable_releases)[:4]

        # Determine the install/update URL
        if is_self_update:
            install_url = "/api/self-update"
            hx_target = "body"
            hx_indicator = ""
        elif is_update and instance_id:
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

        return await render_template(
            "partials/modal_version_selector.html",
            releases=filtered_releases,
            migration_required_at=version_floor,
            install_url=install_url,
            hx_target=hx_target,
            hx_indicator=hx_indicator,
            driver_id=driver_id,
            is_self_update=is_self_update,
        )

    except Exception as e:
        _LOG.error("Error loading version selector for %s/%s: %s", owner, repo, e)
        return await render_template(
            "partials/modal_version_selector.html",
            error=f"Error loading versions: {str(e)}",
        )


@app.route("/api/self-update", methods=["POST"])
async def self_update():
    """
    Trigger a self-update of Integration Manager via the bootstrapper integration.

    When the remote deletes the IM driver it also wipes the entire UC_CONFIG_HOME
    directory for that integration — taking manager.json (integration backups,
    notification settings, UI preferences) and config.json (remote connection
    credentials) with it.  Both files must therefore be serialised and handed to
    the bootstrapper *before* deletion so it can restore them after the new
    version is installed.

    Process:
    1. Validate and normalise the target version tag
    2. Locate the bootstrapper entry in the registry (type: "bootstrapper")
    3. Read manager.json → manager_data  (integration configs, settings)
       Read config.json  → config_data   (remote connection credentials)
    4. Download the latest bootstrapper tar.gz from its GitHub release
    5. Remove any stale bootstrapper installation, then install the fresh copy
    6. Run the bootstrapper setup flow:
       a. start_setup()          - initiates setup on the remote
       b. poll get_setup()       - wait for WAIT_USER_ACTION state
       c. send_setup_input() with:
            target_version    - the IM release tag to install (e.g. "v2.1.0")
            manager_driver_id - our driver_id so bootstrapper knows what to replace
            manager_data      - serialised manager.json (restored after reinstall)
            config_data       - serialised config.json  (restored after reinstall)
          The framework returns SetupComplete after query_device validates the input,
          saves the config, and schedules the upgrade via verify_connection().
          No explicit complete_setup() call is needed — the setup state machine
          is already done by the time send_setup_input returns.
    7. Release the operation lock (IM is about to be replaced)
    8. Return HX-Redirect to /updating?version=X so the browser switches to the
       waiting page, which polls /health every 2 s and auto-redirects to / once
       the new IM instance is back up.
    """
    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    form = await request.form
    version = request.args.get("version") or form.get("version")
    if not version:
        return jsonify(
            {"status": "error", "message": "version parameter is required"}
        ), 400

    # Normalise to tag format (bootstrapper will strip the v itself if needed)
    if not version.startswith("v"):
        version = f"v{version}"

    # Check that there is a free integration slot for the bootstrapper.
    # The Remote enforces a limit of 10 active custom integrations; the upgrade
    # temporarily installs the bootstrapper as an extra custom driver, so we
    # need at least one open slot (current count must be < 10).
    _INTEGRATION_SLOT_LIMIT = 10
    active_count = await _get_active_remote_client().get_custom_active_drivers_count()  # ty:ignore[unresolved-attribute]
    if active_count >= _INTEGRATION_SLOT_LIMIT:
        _LOG.warning(
            "Self-update aborted: %d/%d integration slots in use — no room for bootstrapper",
            active_count,
            _INTEGRATION_SLOT_LIMIT,
        )
        return jsonify(
            {
                "status": "error",
                "message": (
                    f"Cannot upgrade: all {_INTEGRATION_SLOT_LIMIT} integration slots are in use. "
                    "Please remove a custom integration before updating Integration Manager."
                ),
            }
        ), 409

    # Check operation lock
    global _operation_in_progress
    async with _operation_lock:
        if _operation_in_progress:
            return jsonify(
                {"status": "error", "message": "Another install/upgrade is in progress"}
            ), 409
        _operation_in_progress = True
        _LOG.info("Lock acquired for self-update to %s", version)

    try:
        # --- Step 1: Find the self_managed (IM) registry entry ---
        # The bootstrapper is now bundled as a release asset in the same repo as IM.
        registry = load_registry()
        manager_entry = next(
            (item for item in registry if item.get("self_managed", False)),
            None,
        )
        if not manager_entry:
            async with _operation_lock:
                _operation_in_progress = False
            return jsonify(
                {
                    "status": "error",
                    "message": "Self-managed IM entry not found in registry",
                }
            ), 404

        manager_repo_url = manager_entry.get("repository", "")
        if not manager_repo_url or "github.com" not in manager_repo_url:
            async with _operation_lock:
                _operation_in_progress = False
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for Integration Manager",
                }
            ), 400

        parsed = GitHubClient.parse_github_url(manager_repo_url)
        if not parsed:
            async with _operation_lock:
                _operation_in_progress = False
            return jsonify(
                {
                    "status": "error",
                    "message": "Could not parse Integration Manager GitHub URL",
                }
            ), 400

        im_owner, im_repo = parsed
        # Read our own driver_id from driver.json (the live value, not the registry)
        # so it works correctly with dev IDs like intg_manager_driver_dev.
        try:
            _driver_json_path = os.path.join(
                os.path.dirname(__file__), "..", "driver.json"
            )
            with open(_driver_json_path, "r", encoding="utf-8") as _f:
                manager_driver_id = json.load(_f).get(
                    "driver_id", "intg_manager_driver"
                )
        except Exception:
            manager_driver_id = manager_entry.get("driver_id", "intg_manager_driver")
        _LOG.info("Self-update: using manager_driver_id=%s", manager_driver_id)
        bootstrapper_driver_id = manager_entry.get(
            "bootstrapper_driver_id", "intg_bootstrapper_driver"
        )
        asset_pattern = manager_entry.get(
            "bootstrapper_asset_pattern", r"uc-intg-bootstrapper.*\.tar\.gz"
        )

        # --- Step 2: Serialise data files for bootstrapper handoff ---
        # Deleting the IM driver on the remote wipes its entire UC_CONFIG_HOME, so
        # both files must be captured now and passed to the bootstrapper which will
        # write them back after the new version is installed.

        # manager.json – integration backups, notification settings, UI preferences
        manager_data_str = "{}"
        try:
            if os.path.exists(MANAGER_DATA_FILE):
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    manager_data_str = f.read()
                _LOG.info(
                    "Read manager.json (%d bytes) for bootstrapper handoff",
                    len(manager_data_str),
                )
        except OSError as e:
            _LOG.warning("Could not read manager.json for bootstrapper: %s", e)

        # config.json – remote connection credentials (IP, auth tokens, etc.)
        config_file = os.path.join(DATA_DIR, "config.json")
        config_data_str = "[]"
        try:
            if os.path.exists(config_file):
                with open(config_file, "r", encoding="utf-8") as f:
                    config_data_str = f.read()
                _LOG.info(
                    "Read config.json (%d bytes) for bootstrapper handoff",
                    len(config_data_str),
                )
        except OSError as e:
            _LOG.warning("Could not read config.json for bootstrapper: %s", e)

        # --- Step 3: Download bootstrapper ---
        # Dev override: UC_DEV_BOOTSTRAPPER_URL skips GitHub and fetches from a local URL.
        dev_bootstrapper_url = os.getenv("UC_DEV_BOOTSTRAPPER_URL", "")
        if dev_bootstrapper_url:
            _LOG.warning(
                "UC_DEV_BOOTSTRAPPER_URL set — downloading bootstrapper from %s",
                dev_bootstrapper_url,
            )
            try:
                async with aiohttp.ClientSession() as _dev_session:
                    async with _dev_session.get(
                        dev_bootstrapper_url,
                        timeout=aiohttp.ClientTimeout(connect=10, total=120),
                    ) as _dev_resp:
                        if _dev_resp.status != 200:
                            raise SyncAPIError(
                                f"Dev bootstrapper download failed: HTTP {_dev_resp.status}"
                            )
                        archive_data = await _dev_resp.read()
                filename = (
                    dev_bootstrapper_url.rstrip("/").split("/")[-1]
                    or "bootstrapper.tar.gz"
                )
                _LOG.info(
                    "Dev-downloaded bootstrapper %s (%d bytes)",
                    filename,
                    len(archive_data),
                )
            except aiohttp.ClientError as exc:
                async with _operation_lock:
                    _operation_in_progress = False
                return jsonify(
                    {
                        "status": "error",
                        "message": f"Dev bootstrapper download error: {exc}",
                    }
                ), 500
        else:
            # The bootstrapper is bundled as an asset in the same GitHub release as IM.
            _LOG.info(
                "Downloading bootstrapper from %s/%s @ %s", im_owner, im_repo, version
            )
            download_result = await _github_client.download_release_asset(
                im_owner, im_repo, asset_pattern=asset_pattern, version=version
            )
            if not download_result:
                async with _operation_lock:
                    _operation_in_progress = False
                return jsonify(
                    {
                        "status": "error",
                        "message": f"No bootstrapper asset found in release {version} at {im_owner}/{im_repo}",
                    }
                ), 404
            archive_data, filename = download_result

        _LOG.info("Downloaded bootstrapper %s (%d bytes)", filename, len(archive_data))

        # --- Step 4: Install bootstrapper ---
        # Delete any stale bootstrapper first (ignore errors)
        try:
            await _get_active_remote_client().delete_driver(bootstrapper_driver_id)  # ty:ignore[unresolved-attribute]
            await asyncio.sleep(API_DELAY)
        except SyncAPIError:
            pass  # Not installed yet — fine

        await _get_active_remote_client().install_integration(archive_data, filename)  # ty:ignore[unresolved-attribute]
        _LOG.info("Bootstrapper installed")
        await asyncio.sleep(API_DELAY * 3)

        # --- Step 5: Run bootstrapper setup flow ---
        # Start setup (new install, reconfigure=False)
        await _get_active_remote_client().start_setup(  # ty:ignore[unresolved-attribute]
            bootstrapper_driver_id, reconfigure=False
        )
        _LOG.info("Bootstrapper setup started")
        await asyncio.sleep(API_DELAY * 3)

        # Step 5a: Wait for first WAIT_USER_ACTION (restore_from_backup screen)
        # BaseSetupFlow always injects this screen first.
        setup_state = ""
        for _ in range(6):
            setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                bootstrapper_driver_id
            )
            setup_state = setup_response.get("state", "")
            if setup_state == "WAIT_USER_ACTION":
                break
            await asyncio.sleep(API_DELAY * 3)

        if setup_state != "WAIT_USER_ACTION":
            raise SyncAPIError(
                f"Bootstrapper did not reach WAIT_USER_ACTION (state: {setup_state!r})"
            )

        # Step 5b: Answer restore_from_backup = true
        _LOG.info("Bootstrapper setup: answering restore_from_backup=true")
        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
            bootstrapper_driver_id, {"restore_from_backup": "true"}
        )
        await asyncio.sleep(API_DELAY * 3)

        # Step 5c: Wait for second WAIT_USER_ACTION (manual entry form)
        setup_state = ""
        for _ in range(6):
            setup_response = await _get_active_remote_client().get_setup(  # ty:ignore[unresolved-attribute]
                bootstrapper_driver_id
            )
            setup_state = setup_response.get("state", "")
            if setup_state == "WAIT_USER_ACTION":
                break
            await asyncio.sleep(API_DELAY * 3)

        if setup_state != "WAIT_USER_ACTION":
            raise SyncAPIError(
                f"Bootstrapper did not reach second WAIT_USER_ACTION (state: {setup_state!r})"
            )

        # Step 5d: Send the actual payload packed into restore_data
        # The framework's _handle_restore_response reads only restore_data;
        # we pack all four fields as a JSON object inside it so the bootstrapper
        # can unpack them in its _handle_restore_response override.
        restore_payload = json.dumps(
            {
                "target_version": version,
                "manager_driver_id": manager_driver_id,
                "manager_data": manager_data_str,
                "config_data": config_data_str,
            }
        )
        _LOG.info(
            "Sending bootstrapper setup payload: target_version=%s, manager_driver_id=%s",
            version,
            manager_driver_id,
        )
        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
            bootstrapper_driver_id, {"restore_data": restore_payload}
        )
        await asyncio.sleep(API_DELAY * 2)
        _LOG.info(
            "Bootstrapper setup input sent — self-update underway for %s", version
        )

        # Release lock — IM is about to be replaced, no point holding it
        async with _operation_lock:
            _operation_in_progress = False

        # Redirect the browser to the waiting page
        clean_version = version.lstrip("v")
        response = await render_template("updating.html", target_version=version)
        return Response(
            response,
            headers={"HX-Redirect": f"/updating?version={clean_version}"},
        )

    except SyncAPIError as e:
        _LOG.error("Self-update failed (SyncAPIError): %s", e)
        async with _operation_lock:
            _operation_in_progress = False
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        _LOG.error("Self-update failed: %s", e)
        async with _operation_lock:
            _operation_in_progress = False
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/dev/test-bootstrapper-setup", methods=["POST"])
async def dev_test_bootstrapper_setup():
    """
    DEV-ONLY: Drive the bootstrapper setup flow without going through the full
    self_update() path (no registry lookup, no GitHub download, no bootstrapper
    reinstall).

    Requires the env var ``UC_DEV_MODE=true`` — returns 404 otherwise so it
    cannot be hit in production.

    Assumptions:
    - The bootstrapper is already installed on the remote and ready to receive
      setup (start_setup has NOT been called yet, or the previous setup was
      aborted).
    - manager.json and config.json are present on disk at their normal paths.

    Query / form parameters (all optional — sensible defaults are derived):
        bootstrapper_driver_id  default: "intg_bootstrapper_driver"
        manager_driver_id       default: read from MANAGER_DATA_FILE or falls
                                         back to the first self_managed registry entry
        target_version          default: "v0.0.0-dev" (bootstrapper ignores the
                                         actual value during a restore-only test)
        skip_upgrade            default: "true"  — when "true" the bootstrapper's
                                         _run_upgrade is short-circuited by passing
                                         a sentinel target_version so it exits after
                                         the restore step (see device.py TODO below)

    Returns JSON progress log so you can watch each step.
    """
    if os.getenv("UC_DEV_MODE", "").lower() not in ("true", "1", "yes"):
        return jsonify({"status": "error", "message": "Not found"}), 404

    if not _get_active_remote_client():
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    form = await request.form
    args = request.args

    def _param(name: str, default: str = "") -> str:
        return (args.get(name) or form.get(name) or default).strip()

    bootstrapper_driver_id = _param(
        "bootstrapper_driver_id", "intg_bootstrapper_driver"
    )
    target_version = _param("target_version", "v0.0.0-dev")

    # Derive manager_driver_id: prefer explicit param, then read from registry
    manager_driver_id = _param("manager_driver_id")
    if not manager_driver_id:
        try:
            registry = load_registry()
            entry = next((r for r in registry if r.get("self_managed")), None)
            if entry:
                manager_driver_id = entry.get("actual_driver_id") or entry.get("id", "")
        except Exception:  # pylint: disable=broad-except
            pass
    if not manager_driver_id:
        return jsonify(
            {
                "status": "error",
                "message": "Could not determine manager_driver_id — pass it explicitly",
            }
        ), 400

    steps: list[str] = []

    def log_step(msg: str) -> None:
        _LOG.info("[DEV test-bootstrapper-setup] %s", msg)
        steps.append(msg)

    try:
        log_step(
            f"Parameters: bootstrapper={bootstrapper_driver_id!r}  manager={manager_driver_id!r}  version={target_version!r}"
        )

        # --- Read config files from disk (same as self_update does) ---
        manager_data_str = "{}"
        try:
            if os.path.exists(MANAGER_DATA_FILE):
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    manager_data_str = f.read()
            log_step(f"Read manager.json ({len(manager_data_str)} bytes)")
        except OSError as e:
            log_step(f"WARNING: could not read manager.json: {e}")

        config_data_str = "[]"
        config_json_path = os.path.join(DATA_DIR, "config.json")
        try:
            if os.path.exists(config_json_path):
                with open(config_json_path, "r", encoding="utf-8") as f:
                    config_data_str = f.read()
            log_step(f"Read config.json ({len(config_data_str)} bytes)")
        except OSError as e:
            log_step(f"WARNING: could not read config.json: {e}")

        # --- Step 1: start_setup ---
        log_step(f"Calling start_setup({bootstrapper_driver_id!r}) …")
        await _get_active_remote_client().start_setup(  # ty:ignore[unresolved-attribute]
            bootstrapper_driver_id, reconfigure=False
        )
        await asyncio.sleep(API_DELAY * 3)

        # --- Step 2: poll for first WAIT_USER_ACTION (restore_from_backup screen) ---
        setup_state = ""
        for attempt in range(5):
            resp = await _get_active_remote_client().get_setup(bootstrapper_driver_id)  # ty:ignore[unresolved-attribute]
            setup_state = resp.get("state", "")
            log_step(f"  attempt {attempt + 1}: state={setup_state!r}")
            if setup_state == "WAIT_USER_ACTION":
                break
            await asyncio.sleep(API_DELAY * 3)

        if setup_state != "WAIT_USER_ACTION":
            return jsonify(
                {
                    "status": "error",
                    "message": f"Bootstrapper did not reach WAIT_USER_ACTION (last state: {setup_state!r})",
                    "steps": steps,
                }
            ), 500

        # --- Step 3: answer restore_from_backup = true ---
        log_step("Answering restore_from_backup=true …")
        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
            bootstrapper_driver_id, {"restore_from_backup": "true"}
        )
        await asyncio.sleep(API_DELAY * 3)

        # --- Step 4: poll for second WAIT_USER_ACTION (manual entry form) ---
        setup_state = ""
        for attempt in range(5):
            resp = await _get_active_remote_client().get_setup(bootstrapper_driver_id)  # ty:ignore[unresolved-attribute]
            setup_state = resp.get("state", "")
            log_step(
                f"  attempt {attempt + 1} (post-restore toggle): state={setup_state!r}"
            )
            if setup_state == "WAIT_USER_ACTION":
                break
            await asyncio.sleep(API_DELAY * 3)

        if setup_state != "WAIT_USER_ACTION":
            return jsonify(
                {
                    "status": "error",
                    "message": f"Bootstrapper did not reach second WAIT_USER_ACTION (last state: {setup_state!r})",
                    "steps": steps,
                }
            ), 500

        log_step("Bootstrapper ready — sending setup payload …")

        # --- Step 5: send the actual data packed into restore_data ---
        restore_payload = json.dumps(
            {
                "target_version": target_version,
                "manager_driver_id": manager_driver_id,
                "manager_data": manager_data_str,
                "config_data": config_data_str,
            }
        )
        await _get_active_remote_client().send_setup_input(  # ty:ignore[unresolved-attribute]
            bootstrapper_driver_id, {"restore_data": restore_payload}
        )
        await asyncio.sleep(API_DELAY * 2)

        log_step(
            "setup_input sent — bootstrapper should now be running verify_connection / _run_upgrade"
        )
        log_step("Watch the bootstrapper logs on the remote for further progress.")

        return jsonify({"status": "ok", "steps": steps})

    except Exception as e:  # pylint: disable=broad-except
        _LOG.error("[DEV test-bootstrapper-setup] failed: %s", e)
        return jsonify({"status": "error", "message": str(e), "steps": steps}), 500


@app.route("/api/versions/check", methods=["POST"])
async def check_versions():
    """
    Manually trigger a version check for all installed integrations.

    This refreshes the cached version data from GitHub.
    """
    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    try:
        _LOG.info("Manual version check triggered")

        remote_id = get_active_remote_id()
        integrations = await _get_installed_integrations(remote_id)
        version_updates = {}
        checked = 0
        updates_available = 0

        for integration in integrations:
            if integration.official:
                continue

            if not integration.home_page or "github.com" not in integration.home_page:
                continue

            try:
                parsed = GitHubClient.parse_github_url(integration.home_page)
                if not parsed:
                    continue

                owner, repo = parsed
                release = await _get_latest_release_for_update(
                    owner, repo, get_active_remote_id()
                )
                if release:
                    latest_version = release.get("tag_name", "")
                    current_version = integration.version or ""
                    has_update = GitHubClient.compare_versions(
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
                        # Send notification for update available
                        # _LOG.info(
                        #     "Update available for %s: %s -> %s",
                        #     integration.name,
                        #     current_version,
                        #     latest_version,
                        # )
                        try:
                            nm = get_notification_manager(get_active_remote_id())
                            _LOG.info(
                                "Calling send_notification_sync for %s",
                                integration.name,
                            )
                            await nm.notify_integration_update_available(
                                integration.driver_id,
                                integration.name,
                                current_version,
                                latest_version,
                            )
                            _LOG.info(
                                "send_notification_sync completed for %s",
                                integration.name,
                            )
                        except Exception as notify_error:
                            _LOG.error(
                                "Failed to send update notification: %s", notify_error
                            )
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
async def get_versions():
    """Get cached version data for all integrations."""
    return jsonify(
        {
            "timestamp": _version_check_timestamp,
            "versions": _cached_version_data,
        }
    )


@app.route("/api/status")
async def get_status():
    """Get current system status as JSON."""
    if not _get_active_remote_client():
        return jsonify(
            {
                "online": False,
                "docked": None,
                "server": None,
                "error": "Service not initialized",
            }
        )
    if not is_remote_online(get_active_remote_id()):
        return jsonify({"online": False, "docked": None, "server": None})

    try:
        is_docked = await _get_active_remote_client().is_docked()  # ty:ignore[unresolved-attribute]
        return jsonify({"online": True, "docked": is_docked, "server": "running"})
    except Exception as e:
        return jsonify(
            {"online": False, "docked": None, "server": None, "error": str(e)}
        )


@app.route("/api/status/html")
async def get_status_html():
    """Get current system status as HTML badges."""
    if not _get_active_remote_client():
        return '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-600 dark:bg-red-500/20 text-white dark:text-red-300">Not Connected</span>'
    if not is_remote_online(get_active_remote_id()):
        return '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-700 dark:bg-yellow-500/20 text-white dark:text-yellow-300"><i class="fa-solid fa-plug-circle-xmark mr-1.5"></i>Remote Offline</span>'

    try:
        is_docked = await _get_active_remote_client().is_docked()  # ty:ignore[unresolved-attribute]
        docked_badge = (
            '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-700 dark:bg-green-500/20 text-white dark:text-green-300">'
            '<i class="fa-regular fa-charging-station mr-1.5"></i>Docked</span>'
            if is_docked
            else '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-700 dark:bg-yellow-500/20 text-white dark:text-yellow-300">'
            '<i class="fa-regular fa-battery-half mr-1.5"></i>On Battery</span>'
        )
        server_badge = (
            '<span class="hidden sm:inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-700 dark:bg-green-500/20 text-white dark:text-green-300">'
            '<span class="w-1.5 h-1.5 mr-1.5 bg-white dark:bg-green-400 rounded-full animate-pulse"></span>Running</span>'
        )
        return f"{docked_badge} {server_badge}"
    except Exception as e:
        return f'<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-600 dark:bg-red-500/20 text-white dark:text-red-300">Error: {e}</span>'


# =============================================================================
# Settings Routes
# =============================================================================


@app.route("/settings")
async def settings_page():
    """Render the settings page."""
    settings = Settings.load(remote_id=get_active_remote_id())
    ui_prefs = UIPreferences.load()
    # Detect if running in Docker/external mode
    uc_config_home = os.getenv("UC_CONFIG_HOME", "")
    is_external = uc_config_home.startswith("/config")
    _LOG.info(
        f"Settings page: UC_CONFIG_HOME='{uc_config_home}', is_external={is_external}"
    )
    return await render_template(
        "settings.html",
        settings=settings,
        ui_prefs=ui_prefs,
        remote_address=_get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
        if _get_active_remote_client()
        else None,
        web_server_port=WEB_SERVER_PORT,
        is_external=is_external,
    )


@app.route("/api/settings", methods=["POST"])
async def save_settings():
    """Save settings from form submission."""
    try:
        settings = Settings.load(remote_id=get_active_remote_id())
        ui_prefs = UIPreferences.load()

        # Update settings from form data (checkboxes only send value if checked)
        form = await request.form
        settings.shutdown_on_battery = form.get("shutdown_on_battery") == "on"
        settings.auto_update = form.get("auto_update") == "on"
        settings.backup_configs = form.get("backup_configs") == "on"
        settings.auto_register_entities = form.get("auto_register_entities") == "on"
        settings.show_beta_releases = form.get("show_beta_releases") == "on"

        backup_time = form.get("backup_time")
        if backup_time:
            settings.backup_time = backup_time

        settings.save(remote_id=get_active_remote_id())
        ui_prefs.save()

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
async def get_settings():
    """Get current settings as JSON."""
    settings = Settings.load(remote_id=get_active_remote_id())
    return jsonify(settings.to_dict())


@app.route("/api/settings/sort", methods=["GET"])
async def get_sort_settings():
    """Get current sort settings as JSON."""
    ui_prefs = UIPreferences.load()
    return jsonify({"sort_by": ui_prefs.sort_by, "sort_reverse": ui_prefs.sort_reverse})


@app.route("/api/settings/sort", methods=["POST"])
async def update_sort_settings():
    """Update sort settings and return refreshed available integrations list."""
    try:
        ui_prefs = UIPreferences.load()
        form = await request.form
        ui_prefs.sort_by = form.get("sort_by", "stars")
        # Convert string 'true'/'false' to boolean
        sort_reverse_str = form.get("sort_reverse", "false")
        ui_prefs.sort_reverse = sort_reverse_str == "true"
        ui_prefs.save()

        # Return refreshed available integrations with new sort
        available = await _get_available_integrations(get_active_remote_id())
        client = _get_active_remote_client()
        remote_ip = client._address if client else None
        return await render_template(
            "partials/available_list.html",
            integrations=available,
            remote_ip=remote_ip,
        )
    except Exception as e:
        _LOG.error("Failed to update sort settings: %s", e)
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>", 500


# ============================================================================
# Remote Switching Routes
# ============================================================================


@app.route("/api/active-remote", methods=["GET"])
async def get_active_remote():
    """Get current active remote information."""
    remote_id = get_active_remote_id()
    if not remote_id or remote_id not in _remote_configs:
        return jsonify({"error": "No active remote"}), 404

    config = _remote_configs[remote_id]

    return jsonify(
        {
            "id": remote_id,
            "name": config.name,
            "address": config.address,
            "connected": is_remote_online(remote_id),
        }
    )


@app.route("/api/active-remote", methods=["POST"])
async def set_active_remote():
    """Switch active remote via session."""
    try:
        data = await request.get_json()
        remote_id = data.get("remote_id")

        if not remote_id:
            return jsonify({"error": "remote_id required"}), 400

        if remote_id not in _remote_clients:
            return jsonify({"error": "Invalid remote_id"}), 400

        # Update session
        session["active_remote_id"] = remote_id
        session.permanent = True

        _LOG.info(
            "Switched active remote to: %s (%s)", remote_id, _get_remote_name(remote_id)
        )

        return jsonify(
            {
                "status": "ok",
                "active_remote_id": remote_id,
                "remote_name": _get_remote_name(remote_id),
            }
        )
    except Exception as e:
        _LOG.error("Failed to switch active remote: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/remotes/list")
async def get_remotes_list():
    """Get list of all configured remotes with connection status."""
    active_id = get_active_remote_id()

    remotes = []
    for remote_id, config in _remote_configs.items():
        remotes.append(
            {
                "id": remote_id,
                "name": config.name,
                "address": config.address,
                "active": remote_id == active_id,
                "connected": is_remote_online(remote_id),
            }
        )

    return await render_template(
        "partials/remote_selector_dropdown.html", remotes=remotes
    )


# ============================================================================
# Notification Routes
# ============================================================================


@app.route("/notifications")
async def notifications_page():
    """Render the notifications settings page."""

    notification_settings = NotificationSettings.load(remote_id=get_active_remote_id())
    return await render_template(
        "notifications.html",
        notification_settings=notification_settings,
    )


@app.route("/api/notifications/home-assistant", methods=["POST"])
async def save_home_assistant_settings():
    """Save Home Assistant notification settings."""

    try:
        data = await request.get_json()
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        settings.home_assistant = HomeAssistantNotificationConfig(
            enabled=data.get("enabled", False),
            url=data.get("url", ""),
            token=data.get("token", ""),
            service=data.get("service", "notify"),
        )

        settings.save(remote_id=get_active_remote_id())
        _LOG.info("Home Assistant notification settings saved")
        return jsonify({"success": True})
    except Exception as e:
        _LOG.error("Failed to save Home Assistant settings: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/notifications/home-assistant/test", methods=["POST"])
async def test_home_assistant_notification():
    """Send a test notification to Home Assistant."""

    try:
        data = await request.get_json() or {}
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        # Use values from request if provided, otherwise fall back to saved settings
        test_config = HomeAssistantNotificationConfig(
            enabled=True,
            url=data.get("url") or settings.home_assistant.url,
            token=data.get("token") or settings.home_assistant.token,
            service=data.get("service") or settings.home_assistant.service,
        )

        async def send_test():
            return await NotificationService.send_home_assistant(
                test_config,
                "Integration Manager",
                "Test notification from Integration Manager",
            )

        success = await send_test()

        if success:
            return jsonify({"success": True})
        return jsonify(
            {
                "success": False,
                "error": "Failed to send notification. Check logs for details.",
            }
        ), 400
    except Exception as e:
        _LOG.error("Failed to send test notification: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/notifications/home-assistant/services", methods=["GET"])
async def get_home_assistant_services():
    """Get available Home Assistant notify services."""

    try:
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        if not settings.home_assistant.url or not settings.home_assistant.token:
            return jsonify(
                {"success": False, "error": "Home Assistant URL and token required"}
            ), 400

        async def fetch_services():
            url = f"{settings.home_assistant.url.rstrip('/')}/api/services"
            headers = {
                "Authorization": f"Bearer {settings.home_assistant.token}",
                "Content-Type": "application/json",
            }

            try:
                ssl_context = _get_ssl_context()
                connector = aiohttp.TCPConnector(ssl=ssl_context)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(url, headers=headers, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Find notify domain
                            for domain in data:
                                if domain.get("domain") == "notify":
                                    services = domain.get("services", [])
                                    # Filter out the generic 'notify' from the list for clarity
                                    # Users can still manually type it
                                    specific_services = [
                                        s for s in services if s != "notify"
                                    ]
                                    return {
                                        "success": True,
                                        "services": specific_services,
                                        "all_services": services,
                                    }
                            return {
                                "success": False,
                                "error": "No notify services found",
                            }
                        return {
                            "success": False,
                            "error": f"Failed to fetch services: {resp.status}",
                        }
            except Exception as e:
                return {"success": False, "error": str(e)}

        result = await fetch_services()

        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400

    except Exception as e:
        _LOG.error("Failed to fetch HA services: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/notifications/webhook", methods=["POST"])
async def save_webhook_settings():
    """Save webhook notification settings."""

    try:
        data = await request.get_json()
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        settings.webhook = WebhookNotificationConfig(
            enabled=data.get("enabled", False),
            url=data.get("url", ""),
            headers=data.get("headers", {}),
        )

        settings.save(remote_id=get_active_remote_id())
        _LOG.info("Webhook notification settings saved")
        return jsonify({"success": True})
    except Exception as e:
        _LOG.error("Failed to save webhook settings: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/notifications/webhook/test", methods=["POST"])
async def test_webhook_notification():
    """Send a test notification via webhook."""

    try:
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        # Temporarily enable for testing
        test_config = WebhookNotificationConfig(
            enabled=True,
            url=settings.webhook.url,
            headers=settings.webhook.headers,
        )

        async def send_test():
            return await NotificationService.send_webhook(
                test_config,
                "Integration Manager",
                "Test notification from Integration Manager",
                {"source": "test"},
            )

        success = await send_test()

        if success:
            return jsonify({"success": True})
        return jsonify(
            {
                "success": False,
                "error": "Failed to send notification. Check logs for details.",
            }
        ), 400
    except Exception as e:
        _LOG.error("Failed to send test notification: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/notifications/pushover", methods=["POST"])
async def save_pushover_settings():
    """Save Pushover notification settings."""

    try:
        data = await request.get_json()
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        settings.pushover = PushoverNotificationConfig(
            enabled=data.get("enabled", False),
            user_key=data.get("user_key", ""),
            app_token=data.get("app_token", ""),
        )

        settings.save(remote_id=get_active_remote_id())
        _LOG.info("Pushover notification settings saved")
        return jsonify({"success": True})
    except Exception as e:
        _LOG.error("Failed to save Pushover settings: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/notifications/pushover/test", methods=["POST"])
async def test_pushover_notification():
    """Send a test notification via Pushover."""

    try:
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        # Temporarily enable for testing
        test_config = PushoverNotificationConfig(
            enabled=True,
            user_key=settings.pushover.user_key,
            app_token=settings.pushover.app_token,
        )

        async def send_test():
            return await NotificationService.send_pushover(
                test_config,
                "Integration Manager",
                "Test notification from Integration Manager",
            )

        success = await send_test()

        if success:
            return jsonify({"success": True})
        return jsonify(
            {
                "success": False,
                "error": "Failed to send notification. Check logs for details.",
            }
        ), 400
    except Exception as e:
        _LOG.error("Failed to send test notification: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/notifications/ntfy", methods=["POST"])
async def save_ntfy_settings():
    """Save ntfy notification settings."""

    try:
        data = await request.get_json()
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        settings.ntfy = NtfyNotificationConfig(
            enabled=data.get("enabled", False),
            server=data.get("server", "https://ntfy.sh"),
            topic=data.get("topic", ""),
            token=data.get("token", ""),
        )

        settings.save(remote_id=get_active_remote_id())
        _LOG.info("ntfy notification settings saved")
        return jsonify({"success": True})
    except Exception as e:
        _LOG.error("Failed to save ntfy settings: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/notifications/ntfy/test", methods=["POST"])
async def test_ntfy_notification():
    """Send a test notification via ntfy."""

    try:
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        # Temporarily enable for testing
        test_config = NtfyNotificationConfig(
            enabled=True,
            server=settings.ntfy.server,
            topic=settings.ntfy.topic,
            token=settings.ntfy.token,
        )

        async def send_test():
            return await NotificationService.send_ntfy(
                test_config,
                "Integration Manager",
                "Test notification from Integration Manager",
                tags=["white_check_mark"],
            )

        success = await send_test()

        if success:
            return jsonify({"success": True})
        return jsonify(
            {
                "success": False,
                "error": "Failed to send notification. Check logs for details.",
            }
        ), 400
    except Exception as e:
        _LOG.error("Failed to send test notification: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/notifications/discord", methods=["POST"])
async def save_discord_settings():
    """Save Discord notification settings."""

    try:
        data = await request.get_json()
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        settings.discord = DiscordNotificationConfig(
            enabled=data.get("enabled", False),
            webhook_url=data.get("webhook_url", ""),
        )

        settings.save(remote_id=get_active_remote_id())
        _LOG.info("Discord notification settings saved")
        return jsonify({"success": True})
    except Exception as e:
        _LOG.error("Failed to save Discord settings: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/notifications/discord/test", methods=["POST"])
async def test_discord_notification():
    """Send a test notification via Discord."""

    try:
        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        # Temporarily enable for testing
        test_config = DiscordNotificationConfig(
            enabled=True,
            webhook_url=settings.discord.webhook_url,
        )

        async def send_test():
            return await NotificationService.send_discord(
                test_config,
                "Integration Manager",
                "Test notification from Integration Manager",
            )

        success = await send_test()

        if success:
            return jsonify({"success": True})
        return jsonify(
            {
                "success": False,
                "error": "Failed to send notification. Check logs for details.",
            }
        ), 400
    except Exception as e:
        _LOG.error("Failed to send test notification: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/notifications/triggers", methods=["POST"])
async def save_notification_triggers():
    """Save notification trigger preferences."""

    try:
        data = await request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        settings = NotificationSettings.load(remote_id=get_active_remote_id())

        # Update trigger settings
        settings.triggers = NotificationTriggers(
            integration_update_available=data.get("integration_update_available", True),
            new_integration_in_registry=data.get("new_integration_in_registry", False),
            integration_error_state=data.get("integration_error_state", True),
            orphaned_entities_detected=data.get("orphaned_entities_detected", True),
        )

        settings.save(remote_id=get_active_remote_id())

        _LOG.info("Notification trigger preferences saved")
        return jsonify({"success": True})
    except Exception as e:
        _LOG.error("Failed to save notification triggers: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


# ============================================================================
# Logs Routes
# ============================================================================


@app.route("/logs")
async def logs_page():
    """Render the logs page."""
    entries = get_log_entries()
    return await render_template(
        "logs.html",
        entries=entries,
        log_count=len(entries),
    )


@app.route("/api/logs/entries")
async def get_logs_entries():
    """Get log entries as HTML partial for HTMX."""
    entries = get_log_entries()
    return await render_template("partials/log_entries.html", entries=entries)


@app.route("/api/logs/clear-confirm")
async def get_clear_logs_confirm():
    """Get confirmation modal for clearing logs."""
    return await render_template("partials/modal_clear_logs.html")


@app.route("/api/logs/clear", methods=["POST"])
async def clear_logs():
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
async def integration_logs_page():
    """Render the integration logs page."""
    if not _get_active_remote_client() or not is_remote_online(get_active_remote_id()):
        return await render_template(
            "integration_logs.html",
            services=[],
            entries=[],
            selected_service="",
        )

    try:
        # Get available log services from the remote
        services = await _get_active_remote_client().get_log_services()  # ty:ignore[unresolved-attribute]

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
        remote_id = get_active_remote_id()
        integrations = await _get_installed_integrations(remote_id)
        integration_map = {
            intg.driver_id: intg.name for intg in integrations if intg.driver_id
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

        return await render_template(
            "integration_logs.html",
            services=active_services,
            entries=[],
            selected_service="",
        )
    except SyncAPIError as e:
        _LOG.error("Failed to fetch log services: %s", e)
        return await render_template(
            "integration_logs.html",
            services=[],
            entries=[],
            selected_service="",
        )


@app.route("/api/integration-logs/entries")
async def get_integration_logs_entries():
    """Get integration log entries as HTML partial for HTMX."""
    if not _get_active_remote_client() or not is_remote_online(get_active_remote_id()):
        return await render_template(
            "partials/integration_log_entries.html", entries=[]
        )

    service_param = request.args.get("service", "")
    if not service_param:
        return await render_template(
            "partials/integration_log_entries.html", entries=[]
        )

    # Get priority filter from request, default to 7 (all levels)
    priority_str = request.args.get("priority", "7")
    try:
        priority = int(priority_str)
        # Ensure priority is in valid range (0-7)
        priority = max(0, min(7, priority))
    except (ValueError, TypeError):
        priority = 7  # Default to all levels if invalid

    # Support comma-separated service list
    services = [s.strip() for s in service_param.split(",") if s.strip()]

    try:
        if len(services) == 1:
            logs = await _get_active_remote_client().get_logs(  # ty:ignore[unresolved-attribute]
                priority=priority,
                service=services[0],
                limit=1000,
                as_text=False,
            )
        else:
            # Fetch logs for each service and merge, sorted newest-first
            all_logs = []
            per_service_limit = max(200, 1000 // len(services))
            for svc in services:
                svc_logs = await _get_active_remote_client().get_logs(  # ty:ignore[unresolved-attribute]
                    priority=priority,
                    service=svc,
                    limit=per_service_limit,
                    as_text=False,
                )
                if isinstance(svc_logs, list):
                    all_logs.extend(svc_logs)
            # Sort merged results by timestamp descending
            all_logs.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
            logs = all_logs[:1000]

        return await render_template(
            "partials/integration_log_entries.html", entries=logs
        )
    except SyncAPIError as e:
        _LOG.error("Failed to fetch integration logs: %s", e)
        return await render_template(
            "partials/integration_log_entries.html", entries=[]
        )


@app.route("/api/integration-logs/download")
async def download_integration_logs():
    """Download integration logs as a text file."""
    if not _get_active_remote_client():
        return "Not connected to remote", 500

    service_param = request.args.get("service", "")
    if not service_param:
        return "No service specified", 400

    # Get priority filter from request, default to 7 (all levels)
    priority_str = request.args.get("priority", "7")
    try:
        priority = int(priority_str)
        # Ensure priority is in valid range (0-7)
        priority = max(0, min(7, priority))
    except (ValueError, TypeError):
        priority = 7  # Default to all levels if invalid

    services = [s.strip() for s in service_param.split(",") if s.strip()]

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

    try:
        if len(services) == 1:
            log_text = await _get_active_remote_client().get_logs(  # ty:ignore[unresolved-attribute]
                priority=priority,
                service=services[0],
                limit=10000,
                as_text=True,
            )
            if not isinstance(log_text, str):
                return "Failed to retrieve logs as text", 500
            base_name = services[0].replace("custom-intg-", "").replace("intg-", "")
            filename = f"{base_name}_logs_{priority_label}+.txt"
        else:
            # Fetch each service as text and concatenate
            parts = []
            for svc in services:
                svc_text = await _get_active_remote_client().get_logs(  # ty:ignore[unresolved-attribute]
                    priority=priority,
                    service=svc,
                    limit=10000,
                    as_text=True,
                )
                if isinstance(svc_text, str) and svc_text.strip():
                    parts.append(f"=== {svc} ===\n{svc_text}")
            log_text = "\n\n".join(parts)
            filename = f"integration_logs_{priority_label}+.txt"

        return Response(
            log_text,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except SyncAPIError as e:
        _LOG.error("Failed to download integration logs: %s", e)
        return f"Failed to download logs: {e}", 500


# ============================================================================
# Diagnostics Routes
# ============================================================================


@app.context_processor
async def inject_remote_configurator_url():
    """Inject the active remote's web configurator URL into all templates."""
    client = _get_active_remote_client()
    if client and client._address:
        return {"remote_configurator_url": f"http://{client._address}"}
    return {"remote_configurator_url": None}


@app.context_processor
async def inject_system_messages_count():
    """Inject unread system messages count into all templates."""
    try:
        messages_service = get_system_messages_service()
        return {"unread_messages_count": messages_service.get_unread_count()}
    except Exception as e:
        _LOG.error("Failed to get unread messages count: %s", e)
        return {"unread_messages_count": 0}


@app.context_processor
async def inject_orphaned_entities_count():
    """Inject orphaned entities + IR codesets count into all templates.

    Skipped entirely when the active remote is offline so page renders never
    block on a remote that is in standby/unreachable.
    """
    client = _get_active_remote_client()
    remote_id = get_active_remote_id()
    if not client or not is_remote_online(remote_id):
        return {"orphaned_entities_count": 0}

    try:
        orphaned_entities = await client.find_orphan_entities()
        activity_ids = set()
        for entity in orphaned_entities:
            activity_id = entity.get("activity_id")
            if activity_id:
                activity_ids.add(activity_id)

        orphaned_codesets = await find_orphaned_ir_codesets(client)

        total_count = len(activity_ids) + len(orphaned_codesets)
        return {"orphaned_entities_count": total_count}
    except Exception as e:
        _LOG.debug("Failed to get orphaned entities count: %s", e)
        return {"orphaned_entities_count": 0}


@app.context_processor
async def inject_active_remote_status():
    """Inject the active remote's online status into all templates.

    Used to render an offline banner and to gate UI elements that depend on
    a reachable remote.
    """
    remote_id = get_active_remote_id()
    return {
        "active_remote_id": remote_id,
        "active_remote_online": is_remote_online(remote_id),
        "active_remote_name": (
            _remote_configs[remote_id].name
            if remote_id and remote_id in _remote_configs
            else None
        ),
    }


_SPONSOR_URL_TEMPLATES: dict[str, str] = {
    "github": "https://github.com/sponsors/{}",
    "buy_me_a_coffee": "https://www.buymeacoffee.com/{}",
    "paypal": "https://www.paypal.com/paypalme/{}",
    "patreon": "https://www.patreon.com/{}",
    "ko-fi": "https://ko-fi.com/{}",
    "venmo": "https://venmo.com/{}",
    "cashapp": "https://cash.app/${}",
}


def _get_sponsors() -> dict[str, dict]:
    """Load and normalise developers from registry.json, keyed by developer name."""
    try:
        data = load_registry_data()
        developers_list = data.get("developers", []) if isinstance(data, dict) else []
        result: dict[str, dict] = {}
        for developer in developers_list:
            name = developer.get("name", "")
            if not name:
                continue
            links: dict[str, str] = {}
            for platform, value in developer.get("sponsorship_links", {}).items():
                if value.startswith("http"):
                    links[platform] = value
                elif platform in _SPONSOR_URL_TEMPLATES:
                    links[platform] = _SPONSOR_URL_TEMPLATES[platform].format(value)
            result[name] = {
                "description": developer.get("description", ""),
                "homepage": developer.get("homepage", ""),
                "links": links,
            }
        return result
    except Exception as e:
        _LOG.debug("Failed to load developers: %s", e)
        return {}


def _get_category_name_map() -> dict[str, str]:
    """Build id → display name lookup from registry categories."""
    try:
        data = load_registry_data()
        if isinstance(data, dict):
            return {
                c["id"]: c["name"]
                for c in data.get("categories", [])
                if "id" in c and "name" in c
            }
    except Exception:
        pass
    return {}


@app.context_processor
async def inject_sponsors():
    """Inject sponsors lookup dict into all templates."""
    return {"sponsors": _get_sponsors()}


@app.route("/system-messages")
async def system_messages_page():
    """Render the system messages page and mark displayed messages as read."""
    try:
        messages_service = get_system_messages_service()

        # Get unread and read messages
        unread_messages = messages_service.get_unread_messages()
        read_messages = messages_service.get_read_messages()

        # Mark all currently displayed unread messages as read
        if unread_messages:
            message_ids = [msg.id for msg in unread_messages]
            messages_service.mark_messages_as_read(message_ids)

        return await render_template(
            "system_messages.html",
            unread_messages=unread_messages,
            read_messages=read_messages,
        )
    except Exception as e:
        _LOG.error("Failed to load system messages: %s", e)
        return await render_template(
            "system_messages.html",
            unread_messages=[],
            read_messages=[],
        )


@app.route("/api/system-messages/refresh", methods=["POST"])
async def refresh_system_messages():
    """Fetch latest system messages from GitHub and reload the page."""
    try:
        messages_service = get_system_messages_service()
        success = messages_service.fetch_from_github()

        if success:
            _LOG.info("System messages refreshed from GitHub")
            # Return success response that triggers page reload
            return jsonify(
                {"success": True, "message": "Messages refreshed successfully"}
            )
        else:
            _LOG.warning("Failed to refresh system messages from GitHub")
            return jsonify(
                {"success": False, "message": "Failed to fetch from GitHub"}
            ), 500

    except Exception as e:
        _LOG.error("Error refreshing system messages: %s", e)
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/diagnostics")
async def diagnostics_page():
    """Render the diagnostics page."""
    remote_id = get_active_remote_id()
    system_update = _system_update_cache.get(remote_id, {}) if remote_id else {}
    return await render_template(
        "diagnostics.html",
        remote_address=_get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
        if _get_active_remote_client()
        else "localhost",
        system_update=system_update,
    )


@app.route("/api/diagnostics/system-update-check", methods=["POST"])
async def system_update_check():
    """Trigger an immediate firmware update check on the remote and return updated status."""
    client = _get_active_remote_client()
    if not client:
        return jsonify({"success": False, "message": "No remote connected"}), 503
    try:
        update_info = await client.check_system_update()
        remote_id = get_active_remote_id()
        if remote_id:
            _system_update_cache[remote_id] = update_info
        installed = update_info.get("installed_version", "Unknown")
        available = update_info.get("available", [])
        if available:
            latest = available[0]
            return jsonify(
                {
                    "success": True,
                    "installed_version": installed,
                    "update_available": True,
                    "available_version": latest.get("version", ""),
                    "title": latest.get("title", ""),
                    "release_notes_url": latest.get("release_notes_url", ""),
                }
            )
        return jsonify(
            {
                "success": True,
                "installed_version": installed,
                "update_available": False,
            }
        )
    except SyncAPIError as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/diagnostics/orphaned-entities")
async def get_orphaned_entities():
    """Get orphaned entities data as HTML partial for HTMX."""
    if not _get_active_remote_client():
        return await render_template(
            "partials/orphaned_entities.html",
            orphaned_entities=[],
        )
    if not is_remote_online(get_active_remote_id()):
        return _render_offline_partial()

    try:
        orphaned_entities = await _get_active_remote_client().find_orphan_entities()  # ty:ignore[unresolved-attribute]
        _LOG.debug("Orphaned entities data: %s", orphaned_entities)

        # Group entities by activity for display
        activities = {}
        for entity in orphaned_entities:
            activity_id = entity.get("activity_id")
            if not activity_id:
                continue

            if activity_id not in activities:
                activity_name = entity.get("activity_name", {})
                name = _get_localized_name(activity_name, "Unknown Activity")
                activities[activity_id] = {"name": name, "entities": []}

            # Add localized names for entity and integration
            entity_copy = entity.copy()
            entity_copy["localized_name"] = _get_localized_name(
                entity.get("name"), "Unknown Entity"
            )

            # Process integration name if present
            integration = entity.get("integration")
            if integration and isinstance(integration, dict):
                integration_copy = integration.copy()
                integration_copy["localized_name"] = _get_localized_name(
                    integration.get("name"), "Unknown Integration"
                )
                entity_copy["integration"] = integration_copy

            activities[activity_id]["entities"].append(entity_copy)

        remote_ip = (
            _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
            if _get_active_remote_client()
            else None
        )
        return await render_template(
            "partials/orphaned_entities.html",
            activities=activities,
            remote_ip=remote_ip,
        )
    except SyncAPIError as e:
        _LOG.error("Failed to fetch orphaned entities: %s", e)
        # Return error message
        return f"""
        <div class="bg-red-50 dark:bg-red-900/20 border border-red-400 dark:border-red-500/30 rounded-lg p-6">
            <div class="flex items-start gap-3">
                <i class="fa-solid fa-triangle-exclamation text-red-600 dark:text-red-400 text-xl"></i>
                <div>
                    <h3 class="text-gray-900 dark:text-white font-medium mb-1">Error Loading Diagnostics</h3>
                    <p class="text-sm text-gray-700 dark:text-gray-300">{e}</p>
                </div>
            </div>
        </div>
        """


@app.route("/api/diagnostics/unused-activity-entities")
async def get_unused_activity_entities():
    """Get unused activity entities data as HTML partial for HTMX."""
    if not _get_active_remote_client():
        return await render_template(
            "partials/unused_activity_entities.html",
            activities={},
        )

    try:
        unused = await _get_active_remote_client().find_unused_entities()  # ty:ignore[unresolved-attribute]
        _LOG.debug("Unused activity entities data: %s", unused)

        # Group by activity for display
        activities = {}
        for entity in unused:
            activity_id = entity.get("activity_id")
            if not activity_id:
                continue
            if activity_id not in activities:
                activity_name = entity.get("activity_name", {})
                activities[activity_id] = {
                    "name": _get_localized_name(activity_name, "Unknown Activity"),
                    "entities": [],
                }
            entity_copy = entity.copy()
            entity_copy["localized_name"] = _get_localized_name(
                entity.get("name"), "Unknown Entity"
            )
            integration = entity.get("integration")
            if integration and isinstance(integration, dict):
                integration_copy = integration.copy()
                integration_copy["localized_name"] = _get_localized_name(
                    integration.get("name"), "Unknown Integration"
                )
                entity_copy["integration"] = integration_copy
            activities[activity_id]["entities"].append(entity_copy)

        remote_ip = (
            _get_active_remote_client()._address  # ty:ignore[unresolved-attribute]
            if _get_active_remote_client()
            else None
        )
        return await render_template(
            "partials/unused_activity_entities.html",
            activities=activities,
            remote_ip=remote_ip,
        )
    except SyncAPIError as e:
        _LOG.error("Failed to fetch unused activity entities: %s", e)
        return f"""
        <div class="bg-red-50 dark:bg-red-900/20 border border-red-400 dark:border-red-500/30 rounded-lg p-6">
            <div class="flex items-start gap-3">
                <i class="fa-solid fa-triangle-exclamation text-red-600 dark:text-red-400 text-xl"></i>
                <div>
                    <h3 class="text-gray-900 dark:text-white font-medium mb-1">Error Loading Diagnostics</h3>
                    <p class="text-sm text-gray-700 dark:text-gray-300">{e}</p>
                </div>
            </div>
        </div>
        """


@app.route("/api/diagnostics/orphaned-ir-codesets")
async def get_orphaned_ir_codesets():
    """Get orphaned IR codesets data as HTML partial for HTMX."""
    client = _get_active_remote_client()
    if not client:
        return await render_template(
            "partials/orphaned_ir_codesets.html",
            orphaned_codesets=[],
        )
    if not is_remote_online(get_active_remote_id()):
        return _render_offline_partial()

    try:
        orphaned_codesets = await find_orphaned_ir_codesets(client)
        _LOG.debug("Found %d orphaned IR codesets", len(orphaned_codesets))

        return await render_template(
            "partials/orphaned_ir_codesets.html",
            orphaned_codesets=orphaned_codesets,
        )
    except SyncAPIError as e:
        _LOG.error("Failed to fetch orphaned IR codesets: %s", e)
        return f"""
        <div class="bg-red-50 dark:bg-red-900/20 border border-red-400 dark:border-red-500/30 rounded-lg p-6">
            <div class="flex items-start gap-3">
                <i class="fa-solid fa-triangle-exclamation text-red-600 dark:text-red-400 text-xl"></i>
                <div>
                    <h3 class="text-gray-900 dark:text-white font-medium mb-1">Error Loading IR Codesets</h3>
                    <p class="text-sm text-gray-700 dark:text-gray-300">{e}</p>
                </div>
            </div>
        </div>
        """


@app.route("/api/ir/codesets/<device_id>/delete-confirm")
async def ir_codeset_delete_confirm(device_id: str):
    """Render delete confirmation modal for IR codeset."""
    # Get codeset info from query params or find it
    device_name = request.args.get("device_name", device_id)

    return await render_template(
        "partials/modal_delete_ir_codeset.html",
        device_id=device_id,
        device_name=device_name,
    )


@app.route("/api/ir/codesets/<device_id>", methods=["DELETE"])
async def delete_ir_codeset(device_id: str):
    """Delete a custom IR codeset."""
    if not _get_active_remote_client():
        return jsonify({"error": "Not connected to remote"}), 500

    try:
        await _get_active_remote_client().delete_custom_ir_codeset(device_id)  # ty:ignore[unresolved-attribute]
        _LOG.info("Deleted IR codeset: %s", device_id)
        # Return empty response to remove the element from DOM
        return "", 200
    except SyncAPIError as e:
        _LOG.error("Failed to delete IR codeset %s: %s", device_id, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ir/codesets/reassociate", methods=["POST"])
async def reassociate_ir_codeset():
    """Create a new remote associated with a custom IR codeset."""
    if not _get_active_remote_client():
        return jsonify({"error": "Not connected to remote"}), 500

    try:
        # Handle both JSON and form data
        if request.is_json:
            data = await request.get_json()
        else:
            data = (await request.form).to_dict()

        device_id = data.get("device_id")
        remote_name = data.get("remote_name")

        if not device_id or not remote_name:
            return jsonify({"error": "Missing device_id or remote_name"}), 400

        # Create remote with custom codeset ID
        await _get_active_remote_client().create_remote(remote_name, device_id)  # ty:ignore[unresolved-attribute]

        _LOG.info("Created remote '%s' for codeset %s", remote_name, device_id)
        # Return empty response to remove the element from DOM
        return "", 200
    except SyncAPIError as e:
        _LOG.error("Failed to reassociate IR codeset: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/reboot", methods=["POST"])
async def system_reboot():
    """Reboot the remote."""
    if not _get_active_remote_client():
        return jsonify({"error": "Not connected to remote"}), 500

    try:
        await _get_active_remote_client().reboot_remote()  # ty:ignore[unresolved-attribute]
        return jsonify({"success": True, "message": "Reboot command sent"}), 200
    except SyncAPIError as e:
        _LOG.error("Failed to reboot remote: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/system/power-off", methods=["POST"])
async def system_power_off():
    """Power off the remote."""
    if not _get_active_remote_client():
        return jsonify({"error": "Not connected to remote"}), 500

    try:
        await _get_active_remote_client().power_off_remote()  # ty:ignore[unresolved-attribute]
        return jsonify({"success": True, "message": "Power off command sent"}), 200
    except SyncAPIError as e:
        _LOG.error("Failed to power off remote: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/create", methods=["POST"])
async def create_backup_now():
    """Create a backup of all integration configs that support backup."""
    try:
        remote_id = get_active_remote_id()
        client = _get_active_remote_client()

        if not client:
            return """<div class="text-red-600 dark:text-red-400">Not connected to remote</div>"""

        # Load registry to check which integrations support backup
        registry = load_registry()
        registry_by_driver_id = {}
        for item in registry:
            if item.get("driver_id"):
                registry_by_driver_id[item["driver_id"]] = item
            registry_by_driver_id[item["id"]] = item

        # Get installed integrations using the helper function
        integrations = await _get_installed_integrations(remote_id)

        backed_up = []
        skipped = []
        failed = []

        for integration in integrations:
            driver_id = integration.driver_id
            name = integration.name
            version = integration.version

            # Skip unconfigured integrations
            if integration.state == "NOT_CONFIGURED":
                continue

            # Check if this integration supports backup and meets version requirements
            reg_item = registry_by_driver_id.get(driver_id)
            if not reg_item:
                skipped.append(f"{name} (not in registry)")
                continue

            can_backup, reason = await _can_backup_integration(
                driver_id, version, reg_item
            )
            if not can_backup:
                skipped.append(f"{name} ({reason})")
                continue

            # Perform the backup
            backup_data = await backup_integration(
                client,
                driver_id,
                save_to_file=True,
                remote_id=remote_id,
            )
            if backup_data:
                backed_up.append(name)
            else:
                failed.append(name)

        # Build result message
        result_parts = []
        if backed_up:
            result_parts.append(
                f"<span class='text-green-600 dark:text-green-400'>✓ Backed up: {', '.join(backed_up)}</span>"
            )
        if skipped:
            result_parts.append(
                f"<span class='text-gray-600 dark:text-gray-400'>Skipped (integration does not support backup): {len(skipped)}</span>"
            )
        if failed:
            result_parts.append(
                f"<span class='text-red-600 dark:text-red-400'>✗ Failed: {', '.join(failed)}</span>"
            )

        if not result_parts:
            return """<div class="text-gray-600 dark:text-gray-400">No integrations to backup</div>"""

        return f"""<div class="space-y-1">{"<br>".join(result_parts)}</div>"""

    except Exception as e:
        _LOG.error("Failed to create backup: %s", e)
        return f"""<div class="text-red-600 dark:text-red-400">Error creating backup: {e}</div>"""


@app.route("/api/backups/list")
async def list_backups():
    """List available integration backups."""
    try:
        remote_id = get_active_remote_id()
        backups_data = get_all_backups()
        # Get backups for the active remote
        backups = (
            backups_data.get("remotes", {}).get(remote_id, {}).get("integrations", {})
        )

        if not backups:
            return (
                "<div class='text-gray-600 dark:text-gray-400'>No backups found</div>"
            )

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
            <div class="flex items-center justify-between p-3 bg-uc-light-card dark:bg-gray-700/50 rounded-lg border border-uc-light-border dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-700">
                <button class="flex-1 text-left"
                        hx-get="/api/backups/{driver_id}/view"
                        hx-target="#backup-content"
                        hx-swap="innerHTML"
                        title="View backup data">
                    <div class="text-gray-900 dark:text-white font-mono text-sm">{driver_id}</div>
                    <div class="text-xs text-gray-600 dark:text-gray-400">{formatted_time}</div>
                </button>
                <button class="text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300 text-sm ml-3"
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
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>"


@app.route("/api/backups/<driver_id>/delete-confirm")
async def get_delete_backup_confirm(driver_id: str):
    """Get confirmation modal for deleting a backup."""
    try:
        remote_id = get_active_remote_id()
        backups_data = get_all_backups()
        backup_info = (
            backups_data.get("remotes", {})
            .get(remote_id, {})
            .get("integrations", {})
            .get(driver_id, {})
        )
        timestamp = backup_info.get("timestamp", "Unknown")

        # Format the timestamp nicely
        try:
            dt = datetime.fromisoformat(timestamp)
            formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            formatted_time = timestamp

        return await render_template(
            "partials/modal_delete_backup.html",
            driver_id=driver_id,
            timestamp=formatted_time,
        )
    except Exception as e:
        _LOG.error("Failed to get backup info: %s", e)
        return await render_template(
            "partials/modal_delete_backup.html",
            driver_id=driver_id,
            timestamp="Unknown",
        )


@app.route("/api/backups/<driver_id>/view")
async def view_backup(driver_id: str):
    """View backup data for a specific driver."""
    try:
        backup_data = get_backup(driver_id, remote_id=get_active_remote_id())

        if not backup_data:
            return "<div class='text-gray-600 dark:text-gray-400'>No backup data found</div>"

        # Pretty-print JSON data
        try:
            parsed_data = json.loads(backup_data)
            formatted_data = json.dumps(parsed_data, indent=2)
        except json.JSONDecodeError:
            formatted_data = backup_data

        return f"""
        <div class="mt-4 p-4 bg-uc-light-card dark:bg-gray-900 rounded-lg border border-uc-light-border dark:border-gray-700">
            <div class="flex items-center justify-between mb-3">
                <h4 class="text-sm font-medium text-gray-900 dark:text-white">Backup Data for {driver_id}</h4>
                <button class="text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white text-sm"
                        onclick="this.parentElement.parentElement.style.display='none'">
                    ✕ Close
                </button>
            </div>
            <pre class="text-xs text-gray-900 dark:text-gray-300 overflow-auto max-h-96 whitespace-pre-wrap"><code>{formatted_data}</code></pre>
        </div>
        """
    except Exception as e:
        _LOG.error("Failed to view backup: %s", e)
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>"


@app.route("/api/backups/<driver_id>", methods=["DELETE"])
async def delete_backup_entry(driver_id: str):
    """Delete a backup for a specific driver."""
    try:
        delete_backup(driver_id, remote_id=get_active_remote_id())
        return list_backups()  # Return updated list
    except Exception as e:
        _LOG.error("Failed to delete backup: %s", e)
        return f"<div class='text-red-600 dark:text-red-400'>Error: {e}</div>"


@app.route("/api/backups/download")
async def download_complete_backup():
    """Download complete backup file (all integrations + settings)."""

    try:
        # Get current settings
        settings = Settings.load(remote_id=get_active_remote_id())

        # Get all integration backups
        backups_data = get_all_backups()

        # Ensure settings are included
        backups_data["settings"] = settings.to_dict()

        notification_settings = NotificationSettings.load(
            remote_id=get_active_remote_id()
        )
        backups_data["notification_settings"] = notification_settings.to_dict()

        # Create in-memory file for download
        backup_json = json.dumps(backups_data, indent=2)
        backup_bytes = backup_json.encode("utf-8")
        backup_io = io.BytesIO(backup_bytes)

        return await send_file(
            backup_io,
            mimetype="application/json",
            as_attachment=True,
            attachment_filename="uc_integration_manager_backup.json",
        )
    except Exception as e:
        _LOG.error("Failed to download complete backup: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/backups/upload", methods=["POST"])
async def upload_complete_backup():
    """Upload and restore complete backup file (all integrations + settings)."""
    try:
        files = await request.files
        if "file" not in files:
            return jsonify({"status": "error", "message": "No file provided"}), 400

        file = files["file"]
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

        # Save uploaded backup temporarily and migrate if needed
        active_remote_id = get_active_remote_id()
        if active_remote_id is None:
            return jsonify(
                {"status": "error", "message": "No active remote selected"}
            ), 400

        # If v1.0 format, save it and run migration
        if backup_data.get("version") == "1.0":
            _LOG.info("Uploaded backup is v1.0 format, will migrate to v2.0")

            # Save the v1.0 backup temporarily
            try:
                with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, indent=2)
            except OSError as e:
                return jsonify(
                    {"status": "error", "message": f"Failed to save backup: {e}"}
                ), 500

            # Run the migration with the active remote ID
            if not migrate_v1_to_v2(target_remote_id=active_remote_id):
                return jsonify(
                    {
                        "status": "error",
                        "message": "Failed to migrate v1.0 backup to v2.0 format",
                    }
                ), 500

            # Reload the migrated data
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    backup_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                return jsonify(
                    {
                        "status": "error",
                        "message": f"Failed to reload migrated backup: {e}",
                    }
                ), 500

            _LOG.info("Successfully migrated v1.0 backup to v2.0 format")

        # Restore settings if present in v2.0 format
        remotes_data = backup_data.get("remotes")
        if isinstance(remotes_data, dict) and active_remote_id in remotes_data:
            remote_data = remotes_data[active_remote_id]

            if isinstance(remote_data, dict):
                settings_data = remote_data.get("settings")
                if isinstance(settings_data, dict) and settings_data:
                    try:
                        settings = Settings(**settings_data)
                        settings.save(remote_id=active_remote_id)
                        _LOG.info("Restored settings from backup")
                    except Exception as e:
                        _LOG.warning("Failed to restore settings: %s", e)

                # Restore notification settings if present
                notification_settings_data = remote_data.get("notification_settings")
                if (
                    isinstance(notification_settings_data, dict)
                    and notification_settings_data
                ):
                    try:
                        notification_settings = NotificationSettings.load(
                            remote_id=active_remote_id
                        )

                        # Update from backup data
                        if "home_assistant" in notification_settings_data:
                            ha_data = notification_settings_data["home_assistant"]
                            if isinstance(ha_data, dict):
                                notification_settings.home_assistant = (
                                    HomeAssistantNotificationConfig(**ha_data)
                                )
                        if "webhook" in notification_settings_data:
                            webhook_data = notification_settings_data["webhook"]
                            if isinstance(webhook_data, dict):
                                notification_settings.webhook = (
                                    WebhookNotificationConfig(**webhook_data)
                                )
                        if "pushover" in notification_settings_data:
                            pushover_data = notification_settings_data["pushover"]
                            if isinstance(pushover_data, dict):
                                notification_settings.pushover = (
                                    PushoverNotificationConfig(**pushover_data)
                                )
                        if "ntfy" in notification_settings_data:
                            ntfy_data = notification_settings_data["ntfy"]
                            if isinstance(ntfy_data, dict):
                                notification_settings.ntfy = NtfyNotificationConfig(
                                    **ntfy_data
                                )
                        if "discord" in notification_settings_data:
                            discord_data = notification_settings_data["discord"]
                            if isinstance(discord_data, dict):
                                notification_settings.discord = (
                                    DiscordNotificationConfig(**discord_data)
                                )

                        notification_settings.save(remote_id=active_remote_id)
                        _LOG.info("Restored notification settings from backup")
                    except Exception as e:
                        _LOG.warning("Failed to restore notification settings: %s", e)

        # Save the complete backup file (now in v2.0 format)
        try:
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(backup_data, f, indent=2)
            _LOG.info("Restored complete backup file")
        except OSError as e:
            return jsonify(
                {"status": "error", "message": f"Failed to save backup: {e}"}
            ), 500

        # Calculate integration count from v2.0 structure
        integration_count = 0
        if isinstance(remotes_data, dict) and active_remote_id in remotes_data:
            remote_data = remotes_data[active_remote_id]
            if isinstance(remote_data, dict):
                integrations = remote_data.get("integrations", {})
                if isinstance(integrations, dict):
                    integration_count = len(integrations)

        message = f"Successfully restored {integration_count} integration backup(s)"
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
        remote_configs: list[RemoteConfig],
        host: str = "0.0.0.0",
        port: int = WEB_SERVER_PORT,
    ) -> None:
        """
        Initialize the web server.

        :param remote_configs: List of remote configurations to manage
        :param host: Host to bind to
        :param port: Port to listen on
        """
        global \
            _remote_clients, \
            _remote_configs, \
            _github_client, \
            _sync_github_client, \
            _user_language_code

        self._host = host
        self._port = port
        self._server_thread: threading.Thread | None = None
        self._running = False

        # Initialize remote clients and configs
        _remote_clients.clear()
        _remote_configs.clear()
        for config in remote_configs:
            _remote_configs[config.identifier] = config
            _remote_clients[config.identifier] = RemoteClient(
                address=config.address,
                pin=config.pin,
                api_key=config.api_key,
            )

        _github_client = GitHubClient()
        _sync_github_client = _SyncGitHubClient()

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
        """Run the Quart/Hypercorn server (called in background thread)."""
        from hypercorn.config import Config as HypercornConfig
        from hypercorn.asyncio import serve as hypercorn_serve
        import asyncio as _asyncio

        self._shutdown_event = _asyncio.Event()
        try:
            _LOG.info("Creating Hypercorn server on %s:%d", self._host, self._port)
            config = HypercornConfig()
            config.bind = [f"{self._host}:{self._port}"]
            config.loglevel = "WARNING"
            _LOG.info("Server configured, starting to serve...")
            loop = _asyncio.new_event_loop()
            self._loop = loop
            loop.run_until_complete(
                hypercorn_serve(app, config, shutdown_trigger=self._shutdown_trigger)
            )
        except OSError as e:
            _LOG.error("Web server OS error (port may be in use): %s", e)
            self._running = False
        except Exception as e:
            _LOG.error("Web server error: %s", e)
            self._running = False
        finally:
            if hasattr(self, "_loop"):
                self._loop.close()

    async def _shutdown_trigger(self) -> None:
        """Awaitable trigger used by hypercorn to initiate graceful shutdown."""
        while self._running:
            await asyncio.sleep(0.5)

    def stop(self) -> None:
        """Stop the web server."""
        if not self._running:
            return

        _LOG.info("Stopping web server")
        self._running = False

        if self._server_thread:
            self._server_thread.join(timeout=5)
            self._server_thread = None

    def reload_remotes(self, remote_configs: list[RemoteConfig] | None = None) -> None:
        """
        Reload remote configurations dynamically without restarting the server.

        This allows new remotes to be added through the setup flow or config.json
        without requiring a full integration restart.

        :param remote_configs: Updated list of all remote configurations.
                              If None, will import and use device._all_remote_configs
        """
        global _remote_clients, _remote_configs

        # If no configs provided, get them from the device module
        if remote_configs is None:
            try:
                from device import _all_remote_configs as device_configs

                remote_configs = device_configs
                _LOG.info("Reloading remotes from device module")
            except ImportError:
                _LOG.error("Failed to import remote configs from device module")
                return

        _LOG.info(
            "Reloading remote configurations (current: %d, new: %d)",
            len(_remote_configs),
            len(remote_configs),
        )

        # Clear and rebuild remote clients and configs
        _remote_clients.clear()
        _remote_configs.clear()

        for config in remote_configs:
            _remote_configs[config.identifier] = config
            _remote_clients[config.identifier] = RemoteClient(
                address=config.address,
                pin=config.pin,
                api_key=config.api_key,
            )
            _LOG.info("Loaded remote: %s (%s)", config.name, config.identifier)

        _LOG.info(
            "Remote reload complete - %d remotes configured", len(_remote_clients)
        )

    @property
    def is_running(self) -> bool:
        """Check if the web server is running."""
        return self._running

    async def refresh_integration_versions(self, remote_id: str) -> None:
        """
        Refresh version information for all installed integrations.

        This checks GitHub for the latest releases and updates the cached
        version data used by the UI.

        :param remote_id: Remote identifier to refresh versions for
        """
        await _refresh_version_cache(remote_id)

    async def check_error_states(self, remote_id: str) -> None:
        """
        Check all integrations for error states and send notifications.

        This is called periodically to detect integrations that have entered
        error or disconnected states. Also updates the remote's online status
        so the UI banner reflects actual connectivity, not just the last
        WebSocket lifecycle event.

        :param remote_id: Remote identifier to check error states for
        """
        client = _remote_clients.get(remote_id)
        if not client:
            return

        try:
            is_online = await client.test_connection()
            set_remote_online(remote_id, is_online)
            if not is_online:
                return
            # This will trigger error state notifications automatically
            await _get_installed_integrations(remote_id)
            # _LOG.debug("[%s] Error state check complete", remote_id)
        except Exception as e:
            _LOG.warning("[%s] Failed to check error states: %s", remote_id, e)
            set_remote_online(remote_id, False)

    async def check_new_integrations(self, remote_id: str) -> None:
        """
        Check registry for new integrations and send notifications.

        This is called periodically to detect when new integrations are
        added to the registry.

        :param remote_id: Remote identifier to check for new integrations
        """
        try:
            # This will trigger new integration notifications automatically
            await _get_available_integrations(remote_id)
            _LOG.debug("[%s] New integration check complete", remote_id)
        except Exception as e:
            _LOG.warning("[%s] Failed to check for new integrations: %s", remote_id, e)

    def fetch_repository_batch(self) -> None:
        """
        Fetch a batch of repository data from GitHub if batch window is open.

        This is called periodically (e.g., during polling) to gradually populate
        the repository cache without overwhelming GitHub's API rate limits.
        Only fetches if the 1-hour batch interval has elapsed.
        """
        _LOG.debug("fetch_repository_batch: Called (runs every 15 minutes)")

        if not _github_client or not _sync_github_client:
            _LOG.warning(
                "fetch_repository_batch: GitHub client not initialized, skipping"
            )
            return

        try:
            cache = load_repo_cache()
            last_batch_time = cache.get("last_batch_time", 0)
            now = datetime.now().timestamp()

            # Check if we can start a new batch (1 hour has passed)
            can_fetch_batch = (now - last_batch_time) >= REPO_FETCH_BATCH_INTERVAL

            if not can_fetch_batch:
                time_until_next = REPO_FETCH_BATCH_INTERVAL - (now - last_batch_time)
                _LOG.debug(
                    "Repository batch fetch: waiting %.1f minutes until next batch window (last batch: %.1f min ago)",
                    time_until_next / 60,
                    (now - last_batch_time) / 60,
                )
                return

            _LOG.info(
                "Repository batch fetch: Batch window open, checking for repos to update"
            )

            # Get list of all integrations from registry
            registry = load_registry()
            repos_cache = cache.get("repos", {})
            repos_to_fetch = []

            # Count fresh vs stale cached repos for better logging
            fresh_count = 0
            stale_count = 0
            valid_github_repos = (
                0  # Count of repos with valid GitHub URLs (owner + repo)
            )

            # Collect repos that need updating (expired or missing)
            for item in registry:
                home_page = item.get("repository", "")
                if home_page and "github.com" in home_page:
                    parsed = GitHubClient.parse_github_url(home_page)
                    if parsed:
                        valid_github_repos += 1
                        owner, repo = parsed
                        cache_key = f"{owner}/{repo}"

                        # Check if missing or expired
                        if cache_key not in repos_cache:
                            repos_to_fetch.append((owner, repo, cache_key))
                        else:
                            cached_time = repos_cache[cache_key].get("cached_at", 0)
                            if now - cached_time >= REPO_CACHE_VALIDITY:
                                repos_to_fetch.append((owner, repo, cache_key))
                                stale_count += 1
                            else:
                                fresh_count += 1

            _LOG.info(
                "Repository batch fetch: Found %d repos needing updates (fresh: %d, stale: %d, missing: %d, valid GitHub repos: %d)",
                len(repos_to_fetch),
                fresh_count,
                stale_count,
                len(repos_to_fetch)
                - stale_count,  # Missing = total needing fetch - stale
                valid_github_repos,
            )

            if not repos_to_fetch:
                _LOG.info(
                    "Repository batch fetch: all repos up to date, no fetch needed"
                )
                return

            # Fetch up to BATCH_SIZE repos
            _LOG.debug(
                "Repository batch fetch: Starting batch of up to %d repos",
                min(REPO_FETCH_BATCH_SIZE, len(repos_to_fetch)),
            )

            fetch_count = 0
            for owner, repo, cache_key in repos_to_fetch[:REPO_FETCH_BATCH_SIZE]:
                _LOG.debug(
                    "Fetching repo info for %s/%s (%d/%d in batch)",
                    owner,
                    repo,
                    fetch_count + 1,
                    min(REPO_FETCH_BATCH_SIZE, len(repos_to_fetch)),
                )

                repo_info = _sync_github_client.get_repository_info(owner, repo)
                if repo_info:
                    repos_cache[cache_key] = {"cached_at": now, "data": repo_info}
                    fetch_count += 1
                    _LOG.debug("Successfully fetched %s/%s", owner, repo)
                else:
                    _LOG.warning("Failed to fetch repo info for %s/%s", owner, repo)

            # Save updated cache
            if fetch_count > 0:
                # Always update last_batch_time after fetching to enforce 1-hour rate limit
                # This ensures we only fetch max 10 repos per hour (REPO_FETCH_BATCH_SIZE)
                cache["last_batch_time"] = now
                cache["repos"] = repos_cache
                save_repo_cache(cache)

                remaining_count = len(repos_to_fetch) - fetch_count
                if remaining_count == 0:
                    _LOG.info(
                        "Repository batch fetch: Successfully fetched %d/%d repos - ALL REPOS CACHED (%d total)",
                        fetch_count,
                        len(repos_to_fetch),
                        len(repos_cache),
                    )
                else:
                    _LOG.info(
                        "Repository batch fetch: Successfully fetched %d/%d repos (total cached: %d, remaining: %d) - next batch in 1 hour",
                        fetch_count,
                        len(repos_to_fetch),
                        len(repos_cache),
                        remaining_count,
                    )
            else:
                _LOG.warning("Repository batch fetch: No repos successfully fetched")

        except Exception as e:
            _LOG.error("Failed to fetch repository batch: %s", e, exc_info=True)

    async def check_orphaned_entities(self, remote_id: str) -> None:
        """
        Check for orphaned entities in activities and send notifications.

        This is called periodically to detect orphaned entities that may
        prevent activities from functioning correctly.

        :param remote_id: Remote identifier to check orphaned entities for
        """
        client = _remote_clients.get(remote_id)
        if not client:
            return

        try:
            orphaned_entities = await client.find_orphan_entities()
            _LOG.debug(
                "[%s] Found %d orphaned entities",
                remote_id,
                len(orphaned_entities) if orphaned_entities else 0,
            )

            if orphaned_entities:
                activities = {}
                for entity in orphaned_entities:
                    activity_id = entity.get("activity_id")
                    if not activity_id:
                        continue

                    if activity_id not in activities:
                        activity_name = entity.get("activity_name", {})
                        name = _get_localized_name(activity_name, "Unknown Activity")
                        activities[activity_id] = name

                if activities:
                    activity_names = list(activities.values())
                    activity_ids = list(activities.keys())

                    _LOG.info(
                        "[%s] Found %d activities with orphaned entities: %s",
                        remote_id,
                        len(activity_names),
                        ", ".join(activity_names),
                    )

                    # Send notification (per-remote)
                    notification_manager = get_notification_manager(remote_id)
                    await notification_manager.notify_orphaned_entities(
                        activity_names,
                        activity_ids,
                    )
                    _LOG.debug("[%s] Orphaned entities notification sent", remote_id)
                else:
                    _LOG.debug("[%s] No activities with orphaned entities", remote_id)
                    # Clear any previously notified activities if they're now resolved
                    notification_manager = get_notification_manager(remote_id)
                    if notification_manager._notified_orphaned_activities:
                        notification_manager.clear_orphaned_activities(
                            list(notification_manager._notified_orphaned_activities)
                        )
            else:
                _LOG.debug("[%s] No orphaned entities detected", remote_id)
                # Clear any previously notified activities
                notification_manager = get_notification_manager(remote_id)
                if notification_manager._notified_orphaned_activities:
                    notification_manager.clear_orphaned_activities(
                        list(notification_manager._notified_orphaned_activities)
                    )

        except SyncAPIError as e:
            _LOG.warning("[%s] Failed to check for orphaned entities: %s", remote_id, e)
        except Exception as e:
            _LOG.error(
                "[%s] Unexpected error checking orphaned entities: %s", remote_id, e
            )

    async def check_orphaned_entities_async(self, remote_id: str) -> None:
        """
        Check for orphaned entities in activities and send notifications (async version).

        This is called from async contexts like startup to detect orphaned entities
        that may prevent activities from functioning correctly.

        :param remote_id: Remote identifier to check orphaned entities for
        """
        client = _remote_clients.get(remote_id)
        if not client:
            _LOG.debug(
                "[%s] Skipping orphaned entities check - no remote client", remote_id
            )
            return

        try:
            _LOG.debug("[%s] Checking for orphaned entities...", remote_id)
            orphaned_entities = await client.find_orphan_entities()
            _LOG.debug(
                "[%s] Found %d orphaned entities",
                remote_id,
                len(orphaned_entities) if orphaned_entities else 0,
            )

            if orphaned_entities:
                # Group by activity to get unique activities with orphaned entities
                activities = {}
                for entity in orphaned_entities:
                    activity_id = entity.get("activity_id")
                    if not activity_id:
                        continue

                    if activity_id not in activities:
                        activity_name = entity.get("activity_name", {})
                        name = _get_localized_name(activity_name, "Unknown Activity")
                        activities[activity_id] = name

                if activities:
                    activity_names = list(activities.values())
                    activity_ids = list(activities.keys())

                    _LOG.info(
                        "Found %d activities with orphaned entities: %s",
                        len(activity_names),
                        ", ".join(activity_names),
                    )

                    # Send notification
                    _LOG.debug("Attempting to send orphaned entities notification...")
                    notification_manager = get_notification_manager(remote_id)
                    await notification_manager.notify_orphaned_entities(
                        activity_names,
                        activity_ids,
                    )
                    _LOG.debug("Orphaned entities notification sent")
                else:
                    _LOG.debug("No activities with orphaned entities")
                    # Clear any previously notified activities if they're now resolved
                    notification_manager = get_notification_manager(remote_id)
                    if notification_manager._notified_orphaned_activities:
                        notification_manager.clear_orphaned_activities(
                            list(notification_manager._notified_orphaned_activities)
                        )
            else:
                _LOG.debug("No orphaned entities detected")
                # Clear any previously notified activities
                notification_manager = get_notification_manager(remote_id)
                if notification_manager._notified_orphaned_activities:
                    notification_manager.clear_orphaned_activities(
                        list(notification_manager._notified_orphaned_activities)
                    )

        except SyncAPIError as e:
            _LOG.warning("Failed to check for orphaned entities: %s", e)
        except Exception as e:
            _LOG.warning("Error checking orphaned entities: %s", e)

    def check_system_messages(self) -> None:
        """
        Check for new system messages from GitHub.

        This is called periodically to fetch the latest messages.
        """
        try:
            _LOG.debug("Checking for new system messages from GitHub...")
            messages_service = get_system_messages_service()
            success = messages_service.fetch_from_github()

            if success:
                _LOG.info("System messages updated from GitHub")
            else:
                _LOG.debug("No new system messages or fetch failed")

        except Exception as e:
            _LOG.warning("Failed to check system messages: %s", e)

    async def perform_scheduled_backup(self, remote_id: str) -> bool:
        """
        Perform scheduled backup of all supported integrations.

        :param remote_id: Remote identifier to backup integrations for
        :return: True if backup was successful, False otherwise
        """
        client = _remote_clients.get(remote_id)
        if not client:
            _LOG.warning(
                "[%s] Cannot perform backup - remote client not initialized", remote_id
            )
            return False

        try:
            _LOG.info("[%s] Starting scheduled backup of integrations...", remote_id)

            # Load registry to check which integrations support backup
            registry = load_registry()
            registry_by_driver_id = {}
            for item in registry:
                if item.get("driver_id"):
                    registry_by_driver_id[item["driver_id"]] = item
                registry_by_driver_id[item["id"]] = item

            # Get installed integrations for this remote
            integrations = await _get_installed_integrations(remote_id)

            backed_up_count = 0
            total_attempted = 0

            for integration in integrations:
                driver_id = integration.driver_id
                version = integration.version

                # Skip unconfigured integrations
                if integration.state == "NOT_CONFIGURED":
                    continue

                # Check if this integration supports backup and meets version requirements
                reg_item = registry_by_driver_id.get(driver_id)
                if not reg_item:
                    continue

                can_backup, reason = await _can_backup_integration(
                    driver_id, version, reg_item
                )
                if not can_backup:
                    continue

                total_attempted += 1

                # Try to backup (with remote_id for namespacing)
                backup_data = await backup_integration(
                    client, driver_id, save_to_file=True, remote_id=remote_id
                )
                if backup_data:
                    backed_up_count += 1
                    _LOG.debug("[%s] Backed up integration: %s", remote_id, driver_id)

            _LOG.info(
                "[%s] Scheduled backup complete: %d/%d integrations backed up",
                remote_id,
                backed_up_count,
                total_attempted,
            )

            return (
                backed_up_count > 0 or total_attempted == 0
            )  # Success if we backed up something or nothing to backup

        except Exception as e:
            _LOG.error("Failed to perform scheduled backup: %s", e)
            return False
