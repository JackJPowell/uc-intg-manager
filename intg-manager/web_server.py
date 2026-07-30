"""Quart/Hypercorn API and SPA host for Integration Manager.

The React UI is served as static assets and communicates exclusively through
JSON endpoints. Remote communication is provided by unfurled and must run on
the Hypercorn event loop that owns its aiohttp sessions.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import inspect
import io
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, cast

import aiohttp
from backup_capabilities import backup_support_status
from backup_service import (
    backup_all_integrations,
    backup_integration,
    delete_backup,
    get_all_backups,
    get_backup,
)
from const import (
    API_DELAY,
    LEGACY_WEB_SERVER_PORT,
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
from log_handler import get_log_entries, get_log_handler
from notification_manager import (
    get_notification_manager as _nm_get_notification_manager,
)
from notification_service import NotificationService, _get_ssl_context
from notification_settings import (
    DiscordNotificationConfig,
    HomeAssistantNotificationConfig,
    NotificationSettings,
    NtfyNotificationConfig,
    PushoverNotificationConfig,
    WebhookNotificationConfig,
)
from packaging.version import InvalidVersion, Version
from quart import (
    Quart,
    Response,
    jsonify,
    redirect,
    request,
    send_file,
    session,
)
from sync_api import (
    GitHubClient,
    _SyncGitHubClient,
    get_cached_repo_info,
    load_registry,
    load_registry_data,
    load_repo_cache,
    save_repo_cache,
)
from system_messages import get_system_messages_service
from unfurled import Remote
from unfurled.helpers.exceptions import UnfurledError

_LOG = logging.getLogger(__name__)

# Set werkzeug logging to WARNING and above to reduce noise
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Get the static directory from source.
# Handle PyInstaller frozen executables where data is in sys._MEIPASS
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Running as PyInstaller bundle
    BASE_DIR = sys._MEIPASS
else:
    # Running as regular Python script
    BASE_DIR = os.path.dirname(__file__)

STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "static"))
_SESSION_SECRET_FILE = os.path.join(os.path.dirname(MANAGER_DATA_FILE), ".session-key")


def _load_session_secret() -> str:
    """Load a stable session key without placing it in exported manager data."""
    if configured_secret := os.environ.get("FLASK_SECRET_KEY"):
        return configured_secret
    try:
        with open(_SESSION_SECRET_FILE, encoding="utf-8") as secret_file:
            if secret := secret_file.read().strip():
                return secret
    except FileNotFoundError:
        pass
    except OSError as error:
        _LOG.warning("Unable to read persisted session key: %s", error)

    secret = secrets.token_hex(32)
    try:
        descriptor = os.open(
            _SESSION_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(secret)
    except FileExistsError:
        try:
            with open(_SESSION_SECRET_FILE, encoding="utf-8") as secret_file:
                if existing_secret := secret_file.read().strip():
                    return existing_secret
        except OSError as error:
            _LOG.warning("Unable to read concurrently created session key: %s", error)
    except OSError as error:
        _LOG.warning(
            "Unable to persist session key; sessions will reset on restart: %s", error
        )
    return secret


# Create the API/static application with cache disabled for read-only filesystems.
app = Quart(
    __name__,
    static_folder=STATIC_DIR,
)
# Additional config for read-only filesystem
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Session configuration for multi-remote support
app.secret_key = _load_session_secret()
app.config["SESSION_TYPE"] = "filesystem"
app.config["PERMANENT_SESSION_LIFETIME"] = 7776000  # 90 days

# Multi-remote support: unfurled owns each Remote and its CoreAPI session.
_remote_clients: dict[str, Remote] = {}
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
            localization = await first_client.api.get_localization_settings()
            if localization and localization.get("language_code"):
                _user_language_code = localization["language_code"]
                _LOG.info("User language set to: %s", _user_language_code)
        except Exception as e:
            _LOG.warning("Failed to fetch localization settings at startup: %s", e)


@app.before_request
async def _redirect_legacy_port() -> Response | None:
    """When accessed on the legacy port 8088, redirect to the port-moved notice page."""
    host = request.host  # e.g. "192.168.1.100:8088"
    if host.endswith(f":{LEGACY_WEB_SERVER_PORT}"):
        # Allow through: the notice page itself and any static assets it needs
        if request.path not in ("/manager/port-moved",) and not request.path.startswith(
            "/static/"
        ):
            return cast(Response, redirect("/manager/port-moved", 302))
    return None


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


def _get_active_remote_client() -> Remote | None:
    """Get the unfurled Remote for the currently active remote."""
    remote_id = get_active_remote_id()
    if remote_id:
        return _remote_clients.get(remote_id)
    return None


# ---------------------------------------------------------------------------
# Remote online status — updated by device.py via set_remote_online()
# ---------------------------------------------------------------------------
_remote_online: dict[str, bool] = {}


@dataclass
class _ConnectivityProbeState:
    """Failure/backoff state for a remote's inexpensive liveness probe."""

    failure_count: int = 0
    next_probe_at: float = 0.0


# A heartbeat must never make managing several sleeping remotes feel serial.
_CONNECTIVITY_TIMEOUT = aiohttp.ClientTimeout(total=2, connect=1)
_OFFLINE_HEARTBEAT_MAX_INTERVAL = 300.0
_remote_connectivity: dict[str, _ConnectivityProbeState] = {}


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


# Cached version data for integrations, keyed by remote_id.
_cached_version_data: dict[str, dict] = {}
_version_check_timestamp: dict[str, str] = {}
_cached_driver_ids: dict[str, set] = {}  # remote_id -> installed driver IDs


def _get_version_cache(remote_id: str | None) -> dict:
    """Return version cache dict for given remote (empty dict if none)."""
    if not remote_id:
        return {}
    return _cached_version_data.get(remote_id, {})


# System update info cache keyed by remote_id
_system_update_cache: dict[str, dict] = {}


def set_system_update_info(remote_id: str, update_info: dict) -> None:
    """Cache system firmware update info for a remote (called from device.py)."""
    _system_update_cache[remote_id] = update_info


def _available_firmware_updates(update_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the firmware-update list returned by different Remote releases."""
    candidates = update_info.get("available") or update_info.get("updates") or []
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict)]


def _firmware_update_api_model(
    update_info: dict[str, Any],
    client: Remote | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize firmware availability and live update progress for the SPA."""
    available = _available_firmware_updates(update_info)
    latest = available[0] if available else {}
    status = status or {}
    raw_progress = status.get("progress")
    progress: dict[str, Any] = (
        cast(dict[str, Any], raw_progress) if isinstance(raw_progress, dict) else {}
    )
    state = str(status.get("state") or "").upper()
    remote_progress = client.system.update_info if client else None
    active_states = {"START", "RUN", "PROGRESS", "DOWNLOAD", "UPDATING", "INSTALLING"}
    in_progress = bool(
        state in active_states or (remote_progress and remote_progress.in_progress)
    )
    update_percent = int(
        progress.get("current_percent", 0)
        or (remote_progress.update_percent if remote_progress else 0)
    )
    download_percent = int(
        progress.get("download_percent", 0)
        or (remote_progress.download_percent if remote_progress else 0)
    )
    return {
        "installedVersion": update_info.get("installed_version", "Unknown"),
        "updateAvailable": bool(available),
        "availableVersion": latest.get("version"),
        "title": latest.get("title"),
        "releaseNotesUrl": latest.get("release_notes_url"),
        "inProgress": in_progress,
        "state": state or ("UPDATING" if in_progress else "IDLE"),
        "updatePercent": max(0, min(100, update_percent)),
        "downloadPercent": max(0, min(100, download_percent)),
    }


# Firmware progress is the sole use of a Remote WebSocket.  Keeping this
# separate from the normal HTTP heartbeat avoids persistent connections to
# battery-powered remotes that are likely to be asleep.
_firmware_update_websockets: set[str] = set()
_FIRMWARE_UPDATE_TERMINAL_STATES = {"DONE", "SUCCESS", "ERROR", "FAILED"}


async def _start_firmware_update_websocket(remote_id: str, client: Remote) -> None:
    """Open the short-lived progress WebSocket for an active firmware update."""
    if remote_id in _firmware_update_websockets:
        return
    config = _remote_configs.get(remote_id)
    if not config or not config.api_key:
        return
    try:
        await client.connect_websocket()
        _firmware_update_websockets.add(remote_id)
    except Exception as error:
        # The update API still provides polling status, so this is non-fatal.
        _LOG.warning(
            "[%s] Firmware progress WebSocket unavailable: %s", remote_id, error
        )


async def _stop_firmware_update_websocket(remote_id: str, client: Remote) -> None:
    """Release a firmware-progress WebSocket after its update is complete."""
    if remote_id not in _firmware_update_websockets:
        return
    _firmware_update_websockets.discard(remote_id)
    try:
        await client.disconnect_websocket()
    except Exception as error:
        _LOG.debug(
            "[%s] Failed to close firmware progress WebSocket: %s", remote_id, error
        )


# Firmware version cache keyed by remote_id (populated at connect time)
_remote_firmware_versions: dict[str, str] = {}


def set_firmware_version(remote_id: str, version: str) -> None:
    """Store the installed firmware version for a remote (called from device.py)."""
    _remote_firmware_versions[remote_id] = version
    _LOG.info("[%s] Firmware version set to %s", remote_id, version)


def _supports_inplace_update(remote_id: str | None = None) -> bool:
    """Return True if the active remote supports in-place integration updates.

    Requires firmware >= 2.9.3 which introduced POST /intg/install?update=true.
    """
    if remote_id is None:
        remote_id = get_active_remote_id()
    if not remote_id:
        return False
    version_str = _remote_firmware_versions.get(remote_id, "0.0.0")
    try:
        return Version(version_str) >= Version("2.9.3")
    except InvalidVersion:
        return False


# Per-Remote operation state prevents concurrent mutations of one Remote while
# allowing independent configured Remotes to be managed in parallel.
_OPERATION_LOCK_TIMEOUT = 15 * 60  # 15 minutes — force-release stale lock


@dataclass
class _OperationState:
    in_progress: bool = False
    acquired_at: float | None = None


_operation_states: dict[str, _OperationState] = {}
_operation_locks: dict[str, asyncio.Lock] = {}


def _operation_key(remote_id: str | None) -> str:
    """Return a stable lock key even while setup has no active Remote."""
    return remote_id or "__unconfigured__"


def _operation_lock_for(remote_id: str | None) -> asyncio.Lock:
    key = _operation_key(remote_id)
    return _operation_locks.setdefault(key, asyncio.Lock())


def _operation_state_for(remote_id: str | None) -> _OperationState:
    key = _operation_key(remote_id)
    return _operation_states.setdefault(key, _OperationState())


# Set to True while a self-update-inplace is in flight so /health returns
# "UPDATING" (not "OK"), preventing /updating from redirecting prematurely.
_self_update_pending: bool = False


async def _try_acquire_operation_lock(operation_name: str, remote_id: str | None):
    """Acquire the operation lock.

    Returns ``None`` when the lock is acquired successfully.  Returns a
    ``(response, 409)`` tuple when another operation is already running and
    has not timed out yet, so callers can do::

        if conflict := await _try_acquire_operation_lock("install foo", remote_id):
            return conflict
    """
    state = _operation_state_for(remote_id)
    async with _operation_lock_for(remote_id):
        _LOG.debug(
            "Lock check [%s/%s]: in_progress=%s",
            remote_id,
            operation_name,
            state.in_progress,
        )
        if state.in_progress:
            elapsed = (
                time.monotonic() - state.acquired_at
                if state.acquired_at is not None
                else 0
            )
            if elapsed > _OPERATION_LOCK_TIMEOUT:
                _LOG.warning(
                    "Operation lock held for %.0f seconds - force releasing stale lock",
                    elapsed,
                )
                state.in_progress = False
                state.acquired_at = None
            else:
                _LOG.warning("Blocked [%s] - lock already held", operation_name)
                return jsonify(
                    {
                        "status": "error",
                        "message": "Another install/upgrade is in progress",
                    }
                ), 409
        state.in_progress = True
        state.acquired_at = time.monotonic()
        _LOG.info("Lock acquired [%s/%s]", remote_id, operation_name)
    return None


async def _release_operation_lock(remote_id: str | None, operation_name: str) -> None:
    """Release one Remote's operation state; safe to call from ``finally``."""
    state = _operation_state_for(remote_id)
    async with _operation_lock_for(remote_id):
        state.in_progress = False
        state.acquired_at = None
    _LOG.info("Lock released [%s/%s]", remote_id, operation_name)


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
    can_update: bool = False  # Show update button (always true if update available for custom integrations)
    backup_available: bool = False


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
    backup_available: bool = False
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


def _api_error(code: str, message: str, status: int = 400):
    """Return the single JSON error shape used by the React client."""
    return jsonify({"error": {"code": code, "message": message}}), status


def _developer_api_model(developer: str) -> dict[str, Any] | None:
    """Expose optional developer and donation links as catalog data."""
    if not developer:
        return None
    try:
        registry_data = load_registry_data()
        developers = (
            registry_data.get("developers", [])
            if isinstance(registry_data, dict)
            else []
        )
        for entry in developers:
            if isinstance(entry, dict) and entry.get("name") == developer:
                url_templates = {
                    "github": "https://github.com/sponsors/{}",
                    "buy_me_a_coffee": "https://www.buymeacoffee.com/{}",
                    "paypal": "https://www.paypal.com/paypalme/{}",
                    "patreon": "https://www.patreon.com/{}",
                    "ko-fi": "https://ko-fi.com/{}",
                    "venmo": "https://venmo.com/{}",
                    "cashapp": "https://cash.app/${}",
                }
                raw_links = entry.get("sponsorship_links", entry.get("links", {}))
                links = (
                    {
                        platform: value
                        if value.startswith("http")
                        else url_templates[platform].format(value)
                        for platform, value in raw_links.items()
                        if isinstance(value, str)
                        and value
                        and (value.startswith("http") or platform in url_templates)
                    }
                    if isinstance(raw_links, dict)
                    else {}
                )
                return {
                    "homepage": entry.get("homepage") or None,
                    "supportLinks": [
                        {"platform": platform, "url": url}
                        for platform, url in links.items()
                    ]
                    if isinstance(links, dict)
                    else [],
                }
    except Exception as e:
        _LOG.debug("Unable to load developer links for %s: %s", developer, e)
    return None


def _plain_text(value: str) -> str:
    """Expose user-facing text through JSON without rendered HTML."""
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _integration_api_model(integration: IntegrationInfo | AvailableIntegration) -> dict:
    """Serialize an integration as data and capabilities, never as UI markup."""
    is_available = isinstance(integration, AvailableIntegration)
    management = (
        "official"
        if integration.official
        else "external"
        if integration.external
        else "self_managed"
        if integration.self_managed
        else "custom"
    )
    if isinstance(integration, AvailableIntegration):
        install_state = integration.install_status
        connection_state = "unknown"
        installed = integration.installed
        driver_installed = integration.driver_installed
        categories = list(integration.categories or [])
        repository = {
            "stars": integration.stars,
            "downloads": integration.downloads,
            "createdAt": integration.created_at or None,
            "updatedAt": integration.pushed_at or None,
        }
        original_index = integration.original_index
    else:
        install_state = "configured" if integration.instance_id else "installed"
        connection_state = integration.state.lower()
        # A driver can be installed without an instance having completed setup.
        # It has no configuration to snapshot until the Remote reports it as
        # configured, so do not expose backup/delete-configuration capabilities.
        installed = integration.state.upper() != "NOT_CONFIGURED"
        driver_installed = True
        categories = []
        repository = {
            "stars": 0,
            "downloads": 0,
            "createdAt": None,
            "updatedAt": None,
        }
        original_index = 0

    can_mutate = management == "custom"
    developer = _developer_api_model(integration.developer)
    return {
        "id": integration.driver_id,
        "instanceId": integration.instance_id or None,
        "source": "catalog" if is_available else "installed",
        "name": integration.name,
        "description": integration.description,
        "version": integration.version or None,
        "latestVersion": integration.latest_version or None,
        "developer": integration.developer or None,
        "developerHomepage": developer.get("homepage") if developer else None,
        "supportLinks": developer.get("supportLinks", []) if developer else [],
        "homepage": integration.home_page or None,
        "categories": categories,
        "repository": repository,
        "originalIndex": original_index,
        "management": management,
        "installState": install_state,
        "connectionState": connection_state,
        "updateAvailable": integration.update_available,
        "installed": installed,
        "driverInstalled": driver_installed,
        "configuredEntities": getattr(integration, "configured_entities", 0),
        "capabilities": {
            "install": is_available and can_mutate and not driver_installed,
            "update": can_mutate and integration.can_update,
            "backup": can_mutate and installed and integration.backup_available,
            "deleteConfiguration": can_mutate and installed,
            "deleteDriver": can_mutate and driver_installed,
            "selectVersion": can_mutate and bool(integration.home_page),
        },
    }


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
    if remote_id is None:
        remote_id = get_active_remote_id()

    client = _remote_clients.get(remote_id) if remote_id else None
    if not client or not _github_client or not remote_id:
        return

    try:
        _LOG.info("[%s] Refreshing version cache after update...", remote_id)

        # Get installed integrations
        integrations = await _get_installed_integrations(remote_id)
        version_updates = {}
        current_driver_ids = set()
        settings = Settings.load(remote_id=remote_id)

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
                        if (
                            release.get("prerelease", False)
                            and not settings.show_beta_releases
                        ):
                            _LOG.debug(
                                "Skipping notification for prerelease %s (show_beta_releases disabled)",
                                latest_version,
                            )
                        else:
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
                                    "Failed to send update notification: %s",
                                    notify_error,
                                )
            except Exception as e:
                _LOG.debug(
                    "Failed to check version for %s: %s", integration.driver_id, e
                )

        _cached_version_data[remote_id] = version_updates
        _version_check_timestamp[remote_id] = datetime.now().isoformat()
        _cached_driver_ids[remote_id] = current_driver_ids

        _LOG.info(
            "[%s] Version cache refreshed: %d integrations",
            remote_id,
            len(version_updates),
        )
    except Exception as e:
        _LOG.error("Failed to refresh version cache: %s", e)


async def _apply_automatic_inplace_update(
    integration: IntegrationInfo, remote_id: str
) -> None:
    """Download and apply the current release through the firmware update API."""
    client = _remote_clients.get(remote_id)
    if not client or not _github_client:
        raise RuntimeError("Remote or GitHub service is unavailable")

    parsed = GitHubClient.parse_github_url(integration.home_page)
    if not parsed:
        raise ValueError("No GitHub repository found")
    owner, repo = parsed

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
    download_result = await _github_client.download_release_asset(
        owner, repo, asset_pattern=asset_pattern
    )
    if not download_result:
        raise RuntimeError(f"No release found for {owner}/{repo}")

    archive_data, filename = download_result
    _LOG.info(
        "[%s] Automatically updating %s with %s",
        remote_id,
        integration.driver_id,
        filename,
    )
    await client.api.post_integration_install(archive_data, filename, update=True)


async def _automatic_update_is_safe(remote_id: str) -> bool:
    """Require charging power and no running activities before an update."""
    client = _remote_clients.get(remote_id)
    if not client:
        return False
    try:
        charger = await client.api.get_charger()
    except UnfurledError as error:
        _LOG.warning(
            "[%s] Skipping automatic updates; charger state is unavailable: %s",
            remote_id,
            error,
        )
        return False
    if not charger.get("power_supply", False) and not charger.get(
        "wireless_charging", False
    ):
        _LOG.info("[%s] Skipping automatic updates; Remote is not charging", remote_id)
        return False
    try:
        activities = await client.api.get_activities()
    except UnfurledError as error:
        _LOG.warning(
            "[%s] Skipping automatic updates; activity state is unavailable: %s",
            remote_id,
            error,
        )
        return False

    if not isinstance(activities, list):
        _LOG.warning(
            "[%s] Skipping automatic updates; activity response was not a list",
            remote_id,
        )
        return False
    running = []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        attributes = activity.get("attributes")
        if (
            isinstance(attributes, dict)
            and str(attributes.get("state", "")).upper() == "ON"
        ):
            running.append(activity)
    if running:
        names = [
            _get_localized_name(
                activity.get("name"), activity.get("entity_id", "activity")
            )
            for activity in running
        ]
        _LOG.info(
            "[%s] Skipping automatic updates; active activities: %s",
            remote_id,
            ", ".join(names),
        )
        return False
    return True


async def _run_automatic_updates(remote_id: str) -> None:
    """Update eligible integrations sequentially after a version refresh.

    Firmware in-place updates preserve configuration and entity registrations,
    so backup capability is deliberately not part of eligibility.  The shared
    operation lock protects the Remote from concurrent install/update requests;
    this runner holds it for one batch and processes each integration in order.
    A failure is logged and does not prevent the remaining integrations from
    being attempted on the same scheduled pass.
    """
    settings = Settings.load(remote_id=remote_id)
    if not settings.auto_update or not is_remote_online(remote_id):
        return

    state = _operation_state_for(remote_id)
    async with _operation_lock_for(remote_id):
        if state.in_progress:
            elapsed = (
                time.monotonic() - state.acquired_at
                if state.acquired_at is not None
                else 0
            )
            if elapsed <= _OPERATION_LOCK_TIMEOUT:
                _LOG.info(
                    "[%s] Skipping automatic updates; another operation is in progress",
                    remote_id,
                )
                return
            _LOG.warning(
                "[%s] Releasing stale operation lock before automatic updates",
                remote_id,
            )
        state.in_progress = True
        state.acquired_at = time.monotonic()

    candidates: list[IntegrationInfo] = []
    try:
        integrations = await _get_installed_integrations(remote_id)
        candidates = [
            integration
            for integration in integrations
            if integration.update_available
            and integration.can_update
            and not integration.official
            and not integration.external
            and not integration.self_managed
        ]
        if not candidates:
            _LOG.debug("[%s] No eligible automatic updates", remote_id)
            return
        if not await _automatic_update_is_safe(remote_id):
            return

        _LOG.info(
            "[%s] Processing %d automatic integration update(s) sequentially",
            remote_id,
            len(candidates),
        )
        for integration in candidates:
            # A queued update can take long enough for an activity to start or
            # the Remote to be removed from its charger. Re-check each item.
            if not await _automatic_update_is_safe(remote_id):
                return
            try:
                await _apply_automatic_inplace_update(integration, remote_id)
                _LOG.info(
                    "[%s] Automatically updated %s",
                    remote_id,
                    integration.driver_id,
                )
            except Exception as error:
                _LOG.error(
                    "[%s] Automatic update failed for %s: %s",
                    remote_id,
                    integration.driver_id,
                    error,
                )
    finally:
        await _release_operation_lock(remote_id, "automatic updates")
        # Refresh once after the whole batch so the cache reflects every result.
        if candidates:
            await _refresh_version_cache(remote_id)


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
        instances = await client.api.get_integrations()
    except UnfurledError as e:
        _LOG.error("Failed to get integrations: %s", e)
        instances = []

    # Build set of configured driver IDs
    for instance in instances:
        configured_driver_ids.add(instance.get("driver_id", ""))

    # Get all drivers
    try:
        drivers = await client.api.get_drivers()
    except UnfurledError as e:
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

        # Prefer registry author so sponsor lookup (keyed by developers[].name) matches.
        # Driver metadata may report a different name format than the registry.
        if registry_item.get("author"):
            developer = registry_item["author"]

        if (
            not home_page
            and registry_item.get("repository")
            or (
                home_page
                and "github.com" not in home_page
                and registry_item.get("repository")
            )
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
            backup_available=backup_support_status(
                driver.get("version", "0.0.0") if driver else "0.0.0", registry_item
            )[0],
        )

        # Check for updates using cached version data from background checks
        # This ensures consistent version info regardless of when page is loaded
        _remote_cache = _get_version_cache(remote_id)
        if is_custom and driver_id in _remote_cache:
            version_info = _remote_cache[driver_id]
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
                #     "Update button enabled for %s (can_update=True)",
                #     driver_id,
                # )

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

        # Prefer registry author so sponsor lookup (keyed by developers[].name) matches.
        if registry_item.get("author"):
            developer = registry_item["author"]

        # Use registry repository as fallback for home_page
        if (
            not home_page
            and registry_item.get("repository")
            or (
                home_page
                and "github.com" not in home_page
                and registry_item.get("repository")
            )
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
            backup_available=backup_support_status(
                driver.get("version", "0.0.0"), registry_item
            )[0],
        )

        # Check for updates using cached version data (for unconfigured drivers too)
        _remote_cache = _get_version_cache(remote_id)
        if is_custom and driver_id in _remote_cache:
            version_info = _remote_cache[driver_id]
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
            drivers = await client.api.get_drivers()
            for driver in drivers:
                driver_id = driver.get("driver_id", "")
                driver_type = driver.get("driver_type", "CUSTOM")
                version = driver.get("version", "")
                installed_drivers[driver_id] = (driver_type, version)
                # Also store driver name for fuzzy matching
                name = driver.get("name", {}).get("en", "").lower()
                if name:
                    driver_names[name] = (driver_id, driver_type, version)
        except UnfurledError:
            pass

        try:
            # Get all instances (configured) with their instance IDs
            for instance in await client.api.get_integrations():
                driver_id = instance.get("driver_id", "")
                instance_id = instance.get("integration_id", "")
                configured_driver_ids[driver_id] = instance_id
        except UnfurledError:
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

    available: list[AvailableIntegration] = []
    _remote_cache = _get_version_cache(remote_id)
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
        supports_backup = item.get("supports_backup", False)
        self_managed = item.get("self_managed", False)

        if is_installed and not is_official and not is_external:
            # Use the actual driver_id from the remote (not registry id) for cache lookup
            if actual_driver_id and actual_driver_id in _remote_cache:
                version_info = _remote_cache[actual_driver_id]
                if version_info.get("has_update"):
                    # Always mark that an update is available (for badge display)
                    update_available = True
                    latest_version = version_info.get("latest", "")

                    # Show update button for custom integrations (but not self_managed ones)
                    can_update = not self_managed

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
        if actual_driver_id and actual_driver_id in _remote_cache:
            downloads = _remote_cache[actual_driver_id].get("downloads", 0)

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
            backup_available=is_installed and backup_support_status(version, item)[0],
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


# =============================================================================
# Routes
# =============================================================================


@app.route("/health")
async def health():
    """Simple health check endpoint."""
    if _self_update_pending:
        return "UPDATING"
    return "OK"


@app.route("/api/v1/registry")
async def get_registry():
    """Serve the integrations registry (for local development/testing)."""
    registry_path = Path(__file__).parent / "integrations-registry.json"
    if registry_path.exists():
        with open(registry_path, encoding="utf-8") as f:
            return jsonify({"data": json.load(f)})
    return jsonify({"data": {"integrations": []}})


@app.route("/")
async def index():
    """Keep the root URL convenient while the manager lives under /manager."""
    return redirect("/manager", 302)


@app.route("/manager")
@app.route("/manager/")
@app.route("/manager/<path:client_path>")
async def manager_spa(client_path: str = ""):
    """Serve the pre-built React entry point for every client-side route."""
    entrypoint = os.path.join(STATIC_DIR, "app", "index.html")
    if not os.path.exists(entrypoint):
        _LOG.error("React UI asset is missing: %s", entrypoint)
        return "Integration Manager UI has not been built.", 503
    return await send_file(entrypoint, mimetype="text/html")


# =============================================================================
# React JSON API (v1)
# =============================================================================


@app.route("/api/v1/bootstrap")
async def api_v1_bootstrap():
    """Return app-shell state required to render the SPA."""
    active_id = get_active_remote_id()
    remotes = [
        {
            "id": remote_id,
            "name": config.name,
            "address": config.address,
            "active": remote_id == active_id,
            "online": is_remote_online(remote_id),
        }
        for remote_id, config in _remote_configs.items()
    ]
    client = _get_active_remote_client()
    active_config = _remote_configs.get(active_id) if active_id else None
    remote_configurator_url = (
        f"http://{active_config.address}" if active_config else None
    )
    return jsonify(
        {
            "data": {
                "activeRemoteId": active_id,
                "remotes": remotes,
                "remoteConfiguratorUrl": remote_configurator_url,
            }
        }
    )


@app.route("/api/v1/status")
async def api_v1_status():
    """Return the active remote's connectivity state without HTML badges."""
    client = _get_active_remote_client()
    remote_id = get_active_remote_id()
    if not client or not is_remote_online(remote_id):
        return jsonify({"data": {"online": False, "docked": None}})
    try:
        charger = await client.api.get_charger()
        docked = bool(
            charger.get("power_supply", False)
            or charger.get("wireless_charging", False)
        )
        return jsonify({"data": {"online": True, "docked": docked}})
    except Exception as e:
        _LOG.warning("Failed to get remote status: %s", e)
        return jsonify({"data": {"online": False, "docked": None}})


@app.route("/api/v1/remotes/active", methods=["POST"])
async def api_v1_set_active_remote():
    """Select an active remote without forcing a browser reload."""
    data = await request.get_json(silent=True) or {}
    remote_id = data.get("remoteId")
    if not remote_id:
        return _api_error("remote_id_required", "remoteId is required")
    if remote_id not in _remote_clients:
        return _api_error("invalid_remote", "The selected remote is not configured")
    session["active_remote_id"] = remote_id
    session.permanent = True
    return jsonify({"data": {"activeRemoteId": remote_id}})


@app.route("/api/v1/integrations")
async def api_v1_integrations():
    """Return installed integration card data for the SPA."""
    remote_id = get_active_remote_id()
    if not _get_active_remote_client():
        return _api_error(
            "service_unavailable", "Integration service is not initialized", 503
        )
    if not is_remote_online(remote_id):
        return _api_error("remote_offline", "The active remote is offline", 503)
    try:
        integrations = await _get_installed_integrations(remote_id)
        return jsonify(
            {"data": [_integration_api_model(item) for item in integrations]}
        )
    except Exception as e:
        _LOG.exception("Failed to load installed integrations")
        return _api_error("integrations_unavailable", str(e), 502)


@app.route("/api/v1/catalog/integrations")
async def api_v1_catalog_integrations():
    """Return catalog integration card data for the SPA."""
    remote_id = get_active_remote_id()
    if remote_id and not is_remote_online(remote_id):
        return _api_error("remote_offline", "The active remote is offline", 503)
    try:
        integrations = await _get_available_integrations(remote_id)
        return jsonify(
            {"data": [_integration_api_model(item) for item in integrations]}
        )
    except Exception as e:
        _LOG.exception("Failed to load available integrations")
        return _api_error("catalog_unavailable", str(e), 502)


@app.route("/api/v1/integrations/refresh", methods=["POST"])
async def api_v1_refresh_integrations():
    """Refresh the version cache used by installed and catalog lists."""
    client = _get_active_remote_client()
    if not client or not _github_client:
        return _api_error(
            "service_unavailable", "Integration service is not initialized", 503
        )
    try:
        await _refresh_version_cache(get_active_remote_id())
        return jsonify({"data": {"refreshed": True}})
    except Exception as e:
        _LOG.exception("Failed to refresh integration versions")
        return _api_error("refresh_failed", str(e), 502)


# =============================================================================
# API summary routes
# =============================================================================


@app.route("/api/v1/stats/installed-count")
async def get_installed_count():
    """Get the count of installed integrations.

    Counts drivers where:
    - driver_type is CUSTOM or EXTERNAL (always count)
    - driver_type is LOCAL only if it has a configured instance
    """
    remote_id = get_active_remote_id()
    if not _get_active_remote_client() or not is_remote_online(remote_id):
        return jsonify({"data": {"count": 0}})

    try:
        # Get all installed integrations (includes configured and unconfigured)
        integrations = await _get_installed_integrations(remote_id)

        count = len(integrations)

        return jsonify({"data": {"count": count}})
    except UnfurledError as e:
        _LOG.error("Failed to get integrations count: %s", e)
        return jsonify({"data": {"count": 0}})


@app.route("/api/v1/stats/updates-count")
async def get_updates_count():
    """Get the count of integrations with available updates."""
    remote_id = get_active_remote_id()
    if (
        not _get_active_remote_client()
        or not _github_client
        or not is_remote_online(remote_id)
    ):
        return jsonify({"data": {"count": 0}})

    try:
        integrations = await _get_installed_integrations(remote_id)
        count = sum(
            1
            for i in integrations
            if i.update_available and not i.official and not i.external
        )
        return jsonify({"data": {"count": count}})
    except Exception as e:
        _LOG.error("Failed to get updates count: %s", e)
        return jsonify({"data": {"count": 0}})


@app.route("/api/v1/integration-instances/<instance_id>")
async def get_integration_detail(instance_id: str):
    """Get one installed integration by instance ID as JSON."""
    if not _get_active_remote_client():
        return _api_error(
            "service_unavailable", "Integration service is not initialized", 503
        )

    try:
        # Find the integration in the list
        integrations = await _get_installed_integrations(get_active_remote_id())
        integration = next(
            (i for i in integrations if i.instance_id == instance_id), None
        )
        if integration:
            return jsonify({"data": _integration_api_model(integration)})
        return _api_error("integration_not_found", "Integration was not found", 404)
    except Exception as e:
        _LOG.error("Failed to get integration detail: %s", e)
        return _api_error("integration_unavailable", str(e), 502)


@app.route("/api/v1/integrations/<driver_id>")
async def get_integration_card(driver_id: str):
    """Return one integration's data for client-controlled reconnect polling."""
    remote_id = get_active_remote_id()
    integrations = await _get_installed_integrations(remote_id)
    integration = next(
        (
            i
            for i in integrations
            if i.driver_id == driver_id or i.instance_id == driver_id
        ),
        None,
    )
    if not integration:
        return _api_error("integration_not_found", "Integration was not found", 404)
    return jsonify({"data": _integration_api_model(integration)})


@app.route("/api/v1/integrations/<driver_id>/update", methods=["POST"])
async def update_integration_inplace(driver_id: str):
    """
    Update an integration in-place using the firmware 2.9.3+ update flag.

    Accepts optional 'version' query parameter to install a specific version.

    This is the simplified update path for firmware >= 2.9.3. It calls
    POST /intg/install?update=true which updates the driver without the
    backup/delete/restore cycle, preserving all configuration automatically.

    Works for both configured instances (with instance_id) and unconfigured
    drivers (driver_id only).
    """
    client = _get_active_remote_client()
    if not client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    remote_id = get_active_remote_id()
    _form = await request.form
    version = request.args.get("version") or _form.get("version")

    if conflict := await _try_acquire_operation_lock(
        f"update-inplace {driver_id}", remote_id
    ):
        return conflict

    try:
        integrations = await _get_installed_integrations(remote_id)
        integration = next(
            (
                i
                for i in integrations
                if i.driver_id == driver_id or i.instance_id == driver_id
            ),
            None,
        )

        # Also search by instance_id (driver_id arg may actually be an instance_id)
        if not integration:
            integration = next(
                (i for i in integrations if i.instance_id == driver_id), None
            )

        if not integration:
            return jsonify({"status": "error", "message": "Integration not found"}), 404

        if integration.official:
            return jsonify(
                {
                    "status": "error",
                    "message": "Official integrations are managed by firmware updates",
                }
            ), 400

        if not integration.home_page or "github.com" not in integration.home_page:
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this integration",
                }
            ), 400

        parsed = GitHubClient.parse_github_url(integration.home_page)
        if not parsed:
            return jsonify(
                {"status": "error", "message": "Could not parse GitHub URL"}
            ), 400

        owner, repo = parsed

        # Check registry for asset_pattern
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

        if version:
            _LOG.info(
                "In-place updating %s to version %s", integration.driver_id, version
            )
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern, version=version
            )
        else:
            _LOG.info("In-place updating %s to latest version", integration.driver_id)
            download_result = await _github_client.download_release_asset(
                owner, repo, asset_pattern=asset_pattern
            )

        if not download_result:
            return jsonify(
                {"status": "error", "message": f"No release found for {owner}/{repo}"}
            ), 404

        archive_data, filename = download_result
        _LOG.info(
            "Downloaded %s (%d bytes) for in-place update", filename, len(archive_data)
        )

        await client.api.post_integration_install(archive_data, filename, update=True)  # ty:ignore[unresolved-attribute]
        _LOG.info("In-place update of %s completed successfully", integration.driver_id)

        # Kick off version cache refresh in the background
        asyncio.create_task(_refresh_version_cache(remote_id))

        return jsonify(
            {
                "data": {
                    "integration": _integration_api_model(integration),
                    "reconnecting": True,
                }
            }
        )

    except UnfurledError as e:
        _LOG.error("In-place update failed for %s: %s", driver_id, e)
        return _api_error("update_failed", str(e), 502)
    except Exception as e:
        _LOG.error("Unexpected error during in-place update of %s: %s", driver_id, e)
        return _api_error("update_failed", str(e), 500)
    finally:
        await _release_operation_lock(remote_id, f"update-inplace {driver_id}")


@app.route("/api/v1/integrations/<driver_id>", methods=["DELETE"])
async def delete_integration(driver_id: str):
    """
    Delete an integration - either just the configuration or the entire integration.

    Query parameters:
    - type: 'configuration' or 'full'
    """
    client = _get_active_remote_client()
    if not client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    remote_id = get_active_remote_id()
    if conflict := await _try_acquire_operation_lock(f"delete {driver_id}", remote_id):
        return conflict

    payload = await request.get_json(silent=True) or {}
    delete_type = payload.get("scope")
    delete_type = delete_type or "configuration"
    if delete_type not in ("configuration", "full"):
        return _api_error("invalid_delete_scope", "scope must be configuration or full")
    _LOG.info("Delete request for %s: type=%s", driver_id, delete_type)

    try:
        # Check if integration is configured by checking for instances
        is_configured = False
        instance_id = f"{driver_id}.main"

        try:
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
                await client.api.delete_integration(instance_id)
                _LOG.info("Deleted instance: %s", instance_id)
            except UnfurledError as e:
                _LOG.warning("Failed to delete instance %s: %s", instance_id, e)

        # If full delete, also delete the driver
        if delete_type == "full":
            # Small delay to let instance deletion complete
            await asyncio.sleep(API_DELAY * 2)

            try:
                await client.api.delete_driver(driver_id)
                _LOG.info("Deleted driver: %s", driver_id)
            except UnfurledError as e:
                _LOG.error("Failed to delete driver %s: %s", driver_id, e)
                return jsonify(
                    {"status": "error", "message": f"Failed to delete driver: {e}"}
                ), 500

        # Small delay to ensure remote has processed
        await asyncio.sleep(API_DELAY)

        # Return updated card or empty response
        if delete_type == "full":
            # Full delete - remove the card entirely
            return jsonify({"data": {"driverId": driver_id, "removed": True}})
        else:
            # Configuration delete - return updated card showing unconfigured state
            integrations = await _get_installed_integrations(remote_id)
            integration = next(
                (i for i in integrations if i.driver_id == driver_id), None
            )

            if integration:
                return jsonify(
                    {
                        "data": {
                            "integration": _integration_api_model(integration),
                            "removed": False,
                        }
                    }
                )
            else:
                # Driver might have been removed, return empty
                return jsonify({"data": {"driverId": driver_id, "removed": True}})

    except Exception as e:
        _LOG.error("Unexpected error during delete for %s: %s", driver_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        await _release_operation_lock(remote_id, f"delete {driver_id}")


@app.route("/api/v1/integrations/<driver_id>/install", methods=["POST"])
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
    client = _get_active_remote_client()
    if not client or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    # Get optional version parameter from query string or form data
    _form = await request.form
    version = request.args.get("version") or _form.get("version")

    remote_id = get_active_remote_id()
    if conflict := await _try_acquire_operation_lock(f"install {driver_id}", remote_id):
        return conflict

    try:
        # Find the integration in the registry
        registry = load_registry()
        integration = next(
            (item for item in registry if item.get("id") == driver_id), None
        )

        if not integration:
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
                _LOG.warning("Invalid version format %s: %s", version, e)
                return jsonify(
                    {"status": "error", "message": f"Invalid version format: {version}"}
                ), 400

        repo_url = integration.get("repository", "")
        if not repo_url or "github.com" not in repo_url:
            return jsonify(
                {
                    "status": "error",
                    "message": "No GitHub repository found for this integration",
                }
            ), 400

        # Parse GitHub URL
        parsed = GitHubClient.parse_github_url(repo_url)
        if not parsed:
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
        await client.api.post_integration_install(archive_data, filename)  # ty:ignore[unresolved-attribute]
        _LOG.info("Installed integration %s successfully", integration.get("name"))

        # Return the updated integration data for the SPA.
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

        return jsonify(
            {
                "data": {
                    "integration": _integration_api_model(integration_obj),
                    "message": f"Installed {integration_obj.name}",
                }
            }
        )

    except UnfurledError as e:
        _LOG.error("Install failed: %s", e)
        return _api_error("install_failed", str(e), 502)
    except Exception as e:
        _LOG.error("Unexpected error during install: %s", e)
        return _api_error("install_failed", str(e), 500)
    finally:
        await _release_operation_lock(remote_id, f"install {driver_id}")


@app.route("/api/v1/operations/lock")
async def get_operation_lock_status():
    """Return whether an install/update operation is currently in progress."""
    remote_id = get_active_remote_id()
    state = _operation_state_for(remote_id)
    elapsed: float | None = None
    if state.in_progress and state.acquired_at is not None:
        elapsed = round(time.monotonic() - state.acquired_at)
    return jsonify({"data": {"locked": state.in_progress, "elapsedSeconds": elapsed}})


@app.route("/api/v1/operations/lock/release", methods=["POST"])
async def release_operation_lock():
    """Manually release a stuck operation lock."""
    remote_id = get_active_remote_id()
    state = _operation_state_for(remote_id)
    async with _operation_lock_for(remote_id):
        was_locked = state.in_progress
        elapsed = (
            round(time.monotonic() - state.acquired_at)
            if state.in_progress and state.acquired_at is not None
            else None
        )
        state.in_progress = False
        state.acquired_at = None
    if was_locked:
        _LOG.warning(
            "Operation lock manually released by user (was held for %s seconds)",
            elapsed,
        )
    return jsonify({"data": {"wasLocked": was_locked, "elapsedSeconds": elapsed}})


@app.route("/api/v1/backups", methods=["POST"])
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
            client.api, remote_id=get_active_remote_id()
        )
        successful = sum(1 for v in results.values() if v)
        failed = sum(1 for v in results.values() if not v)

        return jsonify(
            {
                "data": {
                    "successful": successful,
                    "failed": failed,
                    "results": results,
                }
            }
        )
    except Exception as e:
        _LOG.error("Backup all failed: %s", e)
        return _api_error("backup_failed", str(e), 500)


@app.route("/api/v1/integrations/<driver_id>/backup", methods=["POST"])
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
            client.api,
            driver_id,
            save_to_file=True,
            remote_id=get_active_remote_id(),
        )
        if backup_data:
            _LOG.info("Backup completed successfully for integration: %s", driver_id)
            return jsonify({"data": {"driverId": driver_id, "hasData": True}})
        else:
            _LOG.warning("No backup data retrieved for integration: %s", driver_id)
            return _api_error(
                "backup_unavailable",
                "The integration did not return configuration backup data. "
                "Verify that this installed version supports backups and try again.",
                422,
            )
    except Exception as e:
        _LOG.error("Backup failed for %s: %s", driver_id, e)
        return _api_error("backup_failed", str(e), 500)


@app.route("/api/v1/release-notes/unavailable/<version>")
async def get_release_notes_unavailable(version: str):
    """
    Return a user-friendly message when release notes cannot be fetched.

    Used when GitHub URL cannot be parsed or release info is unavailable.
    """
    return _api_error(
        "release_notes_unavailable",
        f"Release notes are not available for {version}",
        404,
    )


@app.route("/api/v1/release-notes/<owner>/<repo>/<version>")
async def get_release_notes(owner: str, repo: str, version: str):
    """
    Get release notes for a specific version as raw Markdown and metadata.
    """
    if not _github_client:
        return _api_error("github_unavailable", "GitHub client is not available", 503)

    try:
        # Fetch release info from GitHub
        release = await _github_client.get_release_by_tag(owner, repo, version)

        if not release:
            return _api_error(
                "release_notes_not_found", f"Release notes not found for {version}", 404
            )

        # Get release body (markdown)
        release_body = release.get("body", "")

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

        return jsonify(
            {
                "data": {
                    "version": version,
                    "publishedAt": release_date,
                    "notes": release_body,
                    "name": release.get("name", ""),
                    "url": f"https://github.com/{owner}/{repo}/releases/tag/{version}",
                    "author": release.get("author", {}).get("login", ""),
                    "isPrerelease": is_beta,
                }
            }
        )
    except Exception as e:
        _LOG.error(
            "Error loading release notes for %s/%s %s: %s", owner, repo, version, e
        )
        return _api_error("release_notes_unavailable", str(e), 502)


@app.route("/api/v1/version-selector/<owner>/<repo>/<driver_id>")
async def get_version_selector(owner: str, repo: str, driver_id: str):
    """
    Get version selector modal content with available releases.

    Fetches recent releases and filters by migration boundary from registry.
    Shows beta releases if enabled in settings.

    Pass ?self_update=true to use the Integration Manager in-place update
    flow. In that mode the version filter uses min_compatible_version instead
    of migration_required_at.
    """
    if not _github_client:
        return _api_error("github_unavailable", "GitHub client is not available", 503)

    is_self_update = request.args.get("self_update", "").lower() in ("true", "1", "yes")

    try:
        # Load settings to check show_beta_releases
        settings = Settings.load(remote_id=get_active_remote_id())
        show_beta_releases = settings.show_beta_releases

        # Load registry to get version floor values:
        #   migration_required_at   – normal updates: versions <= this are excluded
        #   min_compatible_version  – self-update picker: versions < this are excluded
        #                             (separate from backup_min_version for config backups)
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

        # The compact card picker needs a few releases; the full selector can
        # explicitly request the complete compatible list.
        show_all = request.args.get("all", "").lower() in ("true", "1", "yes")
        releases_data = await _github_client.get_releases(
            owner, repo, limit=100 if show_all else 20
        )

        if not releases_data:
            return _api_error(
                "versions_not_found", "No releases found for this integration", 404
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

            # Keep the card menu deliberately short. The full selector has no
            # artificial release cap.
            if not show_all and len(stable_releases) >= 4:
                break

        # Combine lists: beta releases first, then stable releases
        filtered_releases = beta_releases + stable_releases
        if not show_all:
            filtered_releases = filtered_releases[:4]

        return jsonify(
            {
                "data": {
                    "driverId": driver_id,
                    "releases": filtered_releases,
                    "versionFloor": version_floor,
                    "isUpdate": is_update,
                    "isSelfUpdate": is_self_update,
                }
            }
        )

    except Exception as e:
        _LOG.error("Error loading version selector for %s/%s: %s", owner, repo, e)
        return _api_error("versions_unavailable", str(e), 502)


async def _do_self_update_inplace(
    remote_id: str, im_owner: str, im_repo: str, asset_pattern: str, version: str
) -> None:
    """Background task: download the IM release asset and install it in-place.

    Sets _self_update_pending = True for the duration so /health returns
    "UPDATING".  On success the flag stays True until the IM process is
    killed by the remote restart (the new process starts with it False).
    On failure the flag is cleared so /health resumes returning "OK".
    """
    global _self_update_pending
    try:
        _LOG.info(
            "Self-update-inplace background: downloading IM %s from %s/%s",
            version,
            im_owner,
            im_repo,
        )
        github_client = _github_client
        if not github_client:
            raise RuntimeError("GitHub client is no longer initialized")
        download_result = await github_client.download_release_asset(
            im_owner, im_repo, asset_pattern=asset_pattern, version=version
        )
        if not download_result:
            _LOG.error(
                "Self-update-inplace: no asset found for %s in %s/%s",
                version,
                im_owner,
                im_repo,
            )
            _self_update_pending = False
            return

        archive_data, filename = download_result
        _LOG.info(
            "Self-update-inplace: downloaded %s (%d bytes) — sending install",
            filename,
            len(archive_data),
        )

        client = _remote_clients.get(remote_id)
        if not client:
            raise RuntimeError("Active Remote is no longer configured")
        await client.api.post_integration_install(archive_data, filename, update=True)  # ty:ignore[unresolved-attribute]
        # Install accepted — remote will restart IM.  Keep _self_update_pending
        # True so /health returns "UPDATING" until the new IM process takes over.
        _LOG.info(
            "Self-update-inplace: install sent for %s, awaiting IM restart", version
        )
    except Exception as e:
        _LOG.error("Self-update-inplace background task failed: %s", e)
        # Clear flag so /health goes back to "OK" and /updating can navigate away
        _self_update_pending = False
    finally:
        await _release_operation_lock(remote_id, "self-update")


@app.route("/api/v1/self-update/inplace", methods=["POST"])
async def self_update_inplace():
    """
    Update Integration Manager in-place using the firmware 2.9.3+ update flag.

    The Remote's POST /intg/install?update=true preserves the integration's
    UC_CONFIG_HOME directory, so manager.json and config.json are kept
    automatically.

    Queues the background install and returns JSON immediately. The SPA owns
    its pending-state presentation while the Remote restarts the manager.
    """
    global _self_update_pending

    if not _get_active_remote_client() or not _github_client:
        return jsonify({"status": "error", "message": "Service not initialized"}), 500

    payload = await request.get_json(silent=True) or {}
    version = payload.get("version") or request.args.get("version")
    if not version:
        return jsonify(
            {"status": "error", "message": "version parameter is required"}
        ), 400

    if not version.startswith("v"):
        version = f"v{version}"

    remote_id = get_active_remote_id()
    if conflict := await _try_acquire_operation_lock("self-update", remote_id):
        return conflict

    registry = load_registry()
    manager_entry = next(
        (item for item in registry if item.get("self_managed", False)),
        None,
    )
    if not manager_entry:
        await _release_operation_lock(remote_id, "self-update")
        return jsonify(
            {
                "status": "error",
                "message": "Self-managed IM entry not found in registry",
            }
        ), 404

    manager_repo_url = manager_entry.get("repository", "")
    parsed = GitHubClient.parse_github_url(manager_repo_url)
    if not parsed:
        await _release_operation_lock(remote_id, "self-update")
        return jsonify(
            {
                "status": "error",
                "message": "Could not parse Integration Manager GitHub URL",
            }
        ), 400

    im_owner, im_repo = parsed
    asset_pattern = manager_entry.get("asset_pattern")

    # Derive the manager release asset pattern when the registry does not
    # provide one explicitly.
    if not asset_pattern:
        asset_pattern = f"{im_repo}.*\\.tar\\.gz"

    # Signal to /health that an update is in flight before we hand off to the
    # background task, so the browser lands on /updating and waits correctly.
    _self_update_pending = True
    _LOG.info("Self-update-inplace: queuing background task for %s", version)
    asyncio.create_task(
        _do_self_update_inplace(remote_id, im_owner, im_repo, asset_pattern, version)  # ty:ignore[invalid-argument-type]
    )

    return jsonify({"data": {"started": True, "targetVersion": version}})


@app.route("/api/v1/versions/check", methods=["POST"])
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
        settings = Settings.load(remote_id=remote_id)

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
                        if (
                            release.get("prerelease", False)
                            and not settings.show_beta_releases
                        ):
                            _LOG.debug(
                                "Skipping notification for prerelease %s (show_beta_releases disabled)",
                                latest_version,
                            )
                        else:
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
                                    "Failed to send update notification: %s",
                                    notify_error,
                                )
            except Exception as e:
                _LOG.debug(
                    "Failed to check version for %s: %s", integration.driver_id, e
                )

        if remote_id:
            _cached_version_data[remote_id] = version_updates
            _version_check_timestamp[remote_id] = datetime.now().isoformat()
            ts = _version_check_timestamp[remote_id]
        else:
            ts = datetime.now().isoformat()

        return jsonify(
            {
                "data": {
                    "checked": checked,
                    "updatesAvailable": updates_available,
                    "timestamp": ts,
                    "versions": version_updates,
                }
            }
        )

    except Exception as e:
        _LOG.error("Version check failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/v1/versions", methods=["GET"])
async def get_versions():
    """Get cached version data for all integrations on the active remote."""
    remote_id = get_active_remote_id()
    return jsonify(
        {
            "data": {
                "timestamp": _version_check_timestamp.get(remote_id)
                if remote_id
                else None,
                "versions": _get_version_cache(remote_id),
            }
        }
    )


# =============================================================================
# Settings Routes
# =============================================================================


def _is_external_runtime() -> bool:
    """Match the device's distinction between an external host and a Remote bundle."""
    config_home = os.getenv("UC_CONFIG_HOME", "")
    return not config_home or config_home.startswith("/config")


@app.route("/api/v1/settings", methods=["GET"])
async def api_v1_get_settings():
    """Return settings and UI preferences for the SPA settings form."""
    return jsonify(
        {
            "data": {
                "settings": Settings.load(remote_id=get_active_remote_id()).to_dict(),
                "preferences": UIPreferences.load().to_dict(),
                "runtime": {
                    "remoteAddress": _remote_configs.get(get_active_remote_id()).address  # ty:ignore[unresolved-attribute]
                    if get_active_remote_id() in _remote_configs
                    else None,
                    "webServerPort": WEB_SERVER_PORT,
                    "external": _is_external_runtime(),
                },
            }
        }
    )


@app.route("/api/v1/settings", methods=["PUT"])
async def api_v1_save_settings():
    """Persist typed JSON settings without accepting form submissions."""
    payload = await request.get_json(silent=True) or {}
    values = payload.get("settings", {})
    preferences = payload.get("preferences", {})
    if not isinstance(values, dict) or not isinstance(preferences, dict):
        return _api_error(
            "invalid_settings", "settings and preferences must be objects"
        )

    settings = Settings.load(remote_id=get_active_remote_id())
    for field in (
        "shutdown_on_battery",
        "auto_update",
        "backup_configs",
        "show_beta_releases",
        "backup_time",
    ):
        if field in values:
            setattr(settings, field, values[field])
    if not isinstance(settings.backup_time, str):
        return _api_error("invalid_backup_time", "backup_time must be a string")
    settings.save(remote_id=get_active_remote_id())

    ui_prefs = UIPreferences.load()
    if "sort_by" in preferences:
        ui_prefs.sort_by = str(preferences["sort_by"])
    if "sort_reverse" in preferences:
        ui_prefs.sort_reverse = bool(preferences["sort_reverse"])
    ui_prefs.save()
    return jsonify(
        {
            "data": {
                "settings": settings.to_dict(),
                "preferences": ui_prefs.to_dict(),
                "runtime": {
                    "remoteAddress": _remote_configs.get(get_active_remote_id()).address  # ty:ignore[unresolved-attribute]
                    if get_active_remote_id() in _remote_configs
                    else None,
                    "webServerPort": WEB_SERVER_PORT,
                    "external": _is_external_runtime(),
                },
            }
        }
    )


@app.route("/api/v1/notifications", methods=["GET"])
async def api_v1_get_notifications():
    """Expose all notification configuration through one JSON resource."""
    return jsonify(
        {"data": NotificationSettings.load(remote_id=get_active_remote_id()).to_dict()}
    )


@app.route("/api/v1/notifications", methods=["PUT"])
async def api_v1_save_notifications():
    """Save notification providers and triggers from the SPA."""
    payload = await request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _api_error(
            "invalid_notifications", "Notification settings must be an object"
        )
    try:
        settings = NotificationSettings._parse_settings_data(payload)
        settings.save(remote_id=get_active_remote_id())
        return jsonify({"data": settings.to_dict()})
    except (TypeError, ValueError) as e:
        return _api_error("invalid_notifications", str(e))


@app.route("/api/v1/notifications/home-assistant/test", methods=["POST"])
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


@app.route("/api/v1/notifications/home-assistant/services", methods=["GET"])
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
                    async with session.get(url, headers=headers, timeout=10) as resp:  # ty:ignore[invalid-argument-type]
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


@app.route("/api/v1/notifications/webhook/test", methods=["POST"])
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


@app.route("/api/v1/notifications/pushover/test", methods=["POST"])
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


@app.route("/api/v1/notifications/ntfy/test", methods=["POST"])
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


@app.route("/api/v1/notifications/discord/test", methods=["POST"])
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


# ============================================================================
# Logs Routes
# ============================================================================


@app.route("/api/v1/logs")
async def api_v1_logs():
    """Return manager log records as JSON."""
    return jsonify({"data": [entry.to_dict() for entry in get_log_entries()]})


@app.route("/api/v1/logs", methods=["DELETE"])
async def api_v1_clear_logs():
    """Clear manager logs without an HTML confirmation fragment."""
    handler = get_log_handler()
    if handler:
        handler.clear()
    return jsonify({"data": {"cleared": True}})


# ============================================================================
# Integration Logs Routes (Remote logs)
# ============================================================================


_REMOTE_LOG_LINE = re.compile(
    r"^\[?(?P<timestamp>\d{4}-\d{2}-\d{2}(?:T|\s)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\s?[+-]\d{2}:?\d{2})?)\s+(?:(?P<source>[a-z0-9_.-]+)\s+)?(?:(?P<level>[A-Z]+)\s+)?(?P<message>.*)$"
)


def _normalize_integration_log_entry(entry: Any) -> dict[str, Any]:
    """Give Remote log lines a stable JSON shape for the SPA.

    Some Remote services return structured log records while others return the
    timestamp and severity as a prefix inside ``message``. Preserve both forms
    but promote those prefix values so the client can consistently render and
    sort the timestamp column. This includes the space-separated UTC timestamp
    emitted by current Remote journal output.
    """
    normalized = dict(entry) if isinstance(entry, dict) else {"message": str(entry)}
    message = str(
        normalized.get("message")
        or normalized.get("msg")
        or normalized.get("m")
        or normalized.get("text")
        or ""
    )
    match = _REMOTE_LOG_LINE.match(message)
    if match:
        if not normalized.get("timestamp"):
            normalized["timestamp"] = match.group("timestamp")
        source = match.group("source")
        if level := match.group("level"):
            # Journal records can carry a default debug priority in their
            # structure while the rendered line gives the real service level.
            normalized["level"] = level.lower()
        normalized["message"] = (
            f"{source}  {match.group('message')}" if source else match.group("message")
        )
    else:
        if not normalized.get("timestamp"):
            normalized["timestamp"] = (
                normalized.get("ts")
                or normalized.get("time")
                or normalized.get("datetime")
                or ""
            )
        if not normalized.get("message"):
            normalized["message"] = message
    if not normalized.get("priority") and normalized.get("prio") is not None:
        normalized["priority"] = normalized["prio"]
    return normalized


def _normalize_integration_log_payload(
    payload: list[Any] | str,
) -> list[dict[str, Any]]:
    """Normalize either Remote log response format into individual records.

    Recent Remote firmware returns ``/system/logs`` as ``text/plain`` even
    when requesting the normal log view. Older releases may still return a
    structured JSON list, so retain support for both forms.
    """
    if isinstance(payload, str):
        return [
            _normalize_integration_log_entry(line)
            for line in payload.splitlines()
            if line.strip()
        ]
    return [_normalize_integration_log_entry(entry) for entry in payload]


@app.route("/api/v1/integration-logs/services")
async def api_v1_integration_log_services():
    """List active Remote log services as JSON."""
    client = _get_active_remote_client()
    if not client or not is_remote_online(get_active_remote_id()):
        return jsonify({"data": []})
    try:
        services = await client.api.get_log_services()
        return jsonify(
            {
                "data": [
                    {
                        "id": item.get("service"),
                        "name": item.get("name") or item.get("service"),
                    }
                    for item in services
                    if item.get("service") and item.get("active")
                ]
            }
        )
    except UnfurledError as e:
        return _api_error("log_services_unavailable", str(e), 502)


@app.route("/api/v1/integration-logs")
async def api_v1_integration_logs():
    """Read selected Remote log services as JSON; React renders the log stream."""
    client = _get_active_remote_client()
    services = [item for item in request.args.get("services", "").split(",") if item]
    if not client or not is_remote_online(get_active_remote_id()):
        return jsonify({"data": []})
    if not services:
        return jsonify({"data": []})
    try:
        priority = max(0, min(7, int(request.args.get("priority", "7"))))
    except ValueError:
        priority = 7
    try:
        entries = []
        for service in services:
            logs = await client.api.get_logs(
                priority=priority,
                service=service,
                limit=max(200, 1000 // len(services)),
                as_text=True,
            )
            entries.extend(_normalize_integration_log_payload(logs))
        entries.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return jsonify({"data": entries[:1000]})
    except UnfurledError as e:
        return _api_error("logs_unavailable", str(e), 502)


@app.route("/api/v1/integration-logs/export")
async def download_integration_logs():
    """Download integration logs as a text file."""
    client = _get_active_remote_client()
    if not client:
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
            log_text = await _get_active_remote_client().api.get_logs(  # ty:ignore[unresolved-attribute]
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
                svc_text = await _get_active_remote_client().api.get_logs(  # ty:ignore[unresolved-attribute]
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
    except UnfurledError as e:
        _LOG.error("Failed to download integration logs: %s", e)
        return f"Failed to download logs: {e}", 500


# ============================================================================
# Diagnostics Routes
# ============================================================================


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


@app.route("/api/v1/system-messages")
async def api_v1_system_messages():
    """Return messages as text data; React owns the message presentation."""
    try:
        service = get_system_messages_service()
        unread = service.get_unread_messages()
        read = service.get_read_messages()
        if unread:
            service.mark_messages_as_read([message.id for message in unread])

        def serialize(message):
            return {
                "id": message.id,
                "date": message.date,
                "title": message.title,
                "content": _plain_text(message.content),
                "priority": message.priority,
            }

        return jsonify(
            {
                "data": {
                    "unread": [serialize(item) for item in unread],
                    "read": [serialize(item) for item in read],
                }
            }
        )
    except Exception as e:
        _LOG.error("Failed to load system messages: %s", e)
        return _api_error("messages_unavailable", str(e), 500)


@app.route("/api/v1/system-messages/refresh", methods=["POST"])
async def api_v1_refresh_system_messages():
    """Refresh message data without instructing the browser to reload."""
    try:
        if not get_system_messages_service().fetch_from_github():
            return _api_error(
                "message_refresh_failed", "Failed to fetch system messages", 502
            )
        return jsonify({"data": {"refreshed": True}})
    except Exception as e:
        return _api_error("message_refresh_failed", str(e), 500)


@app.route("/api/v1/diagnostics/system-update", methods=["POST"])
async def api_v1_system_update_check():
    """Check firmware update status with the v1 JSON envelope."""
    client = _get_active_remote_client()
    if not client:
        return _api_error("remote_unavailable", "No remote connected", 503)
    try:
        update_info = await client.api.put_system_update()
        set_system_update_info(get_active_remote_id() or "", update_info)
        return jsonify({"data": _firmware_update_api_model(update_info, client)})
    except UnfurledError as e:
        return _api_error("firmware_check_failed", str(e), 502)


@app.route("/api/v1/diagnostics/system-update/status")
async def api_v1_system_update_status():
    """Return firmware update progress without exposing the Remote WebSocket."""
    remote_id = get_active_remote_id()
    client = _get_active_remote_client()
    if not remote_id or not client:
        return _api_error("remote_unavailable", "No remote connected", 503)
    update_info = _system_update_cache.get(remote_id, {})
    try:
        status = await client.system.get_update_status()
        if not update_info:
            update_info = await client.api.get_system_update()
            set_system_update_info(remote_id, update_info)
        model = _firmware_update_api_model(update_info, client, status)
        if model["state"] in _FIRMWARE_UPDATE_TERMINAL_STATES:
            await _stop_firmware_update_websocket(remote_id, client)
        return jsonify({"data": model})
    except UnfurledError as e:
        await _stop_firmware_update_websocket(remote_id, client)
        return _api_error("firmware_status_failed", str(e), 502)


@app.route("/api/v1/diagnostics/system-update/install", methods=["POST"])
async def api_v1_system_update_install():
    """Start installation of the latest available Remote firmware."""
    remote_id = get_active_remote_id()
    client = _get_active_remote_client()
    if not remote_id or not client:
        return _api_error("remote_unavailable", "No remote connected", 503)
    if not is_remote_online(remote_id):
        return _api_error("remote_offline", "The active remote is offline", 503)
    try:
        update_info = await client.api.get_system_update()
        if not _available_firmware_updates(update_info):
            return _api_error(
                "firmware_up_to_date", "No firmware update is available", 409
            )
        set_system_update_info(remote_id, update_info)
        await _start_firmware_update_websocket(remote_id, client)
        state = await client.system.update_firmware()
        _LOG.info("[%s] Firmware update requested: %s", remote_id, state)
        return jsonify(
            {
                "data": {
                    **_firmware_update_api_model(
                        update_info, client, {"state": state or "UPDATING"}
                    ),
                    "inProgress": True,
                }
            }
        )
    except UnfurledError as e:
        await _stop_firmware_update_websocket(remote_id, client)
        return _api_error("firmware_update_failed", str(e), 502)


@app.route("/api/v1/diagnostics/orphaned-entities")
async def get_orphaned_entities():
    """Return orphaned entities grouped by activity as JSON."""
    if not _get_active_remote_client():
        return jsonify({"data": {"activities": {}}})
    if not is_remote_online(get_active_remote_id()):
        return _api_error("remote_offline", "The active remote is offline", 503)

    try:
        orphaned_entities = (
            await _get_active_remote_client().helpers.find_orphaned_entities()  # ty:ignore[unresolved-attribute]
        )  # ty:ignore[unresolved-attribute]
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

            activities[activity_id]["entities"].append(entity_copy)  # ty:ignore[unresolved-attribute]

        return jsonify({"data": {"activities": activities}})
    except UnfurledError as e:
        _LOG.error("Failed to fetch orphaned entities: %s", e)
        return _api_error("orphaned_entities_unavailable", str(e), 502)


@app.route("/api/v1/diagnostics/unused-activity-entities")
async def get_unused_activity_entities():
    """Return unused activity entities grouped by activity as JSON."""
    if not _get_active_remote_client():
        return jsonify({"data": {"activities": {}}})

    try:
        unused = (
            await _get_active_remote_client().helpers.find_unused_activity_entities()  # ty:ignore[unresolved-attribute]
        )  # ty:ignore[unresolved-attribute]
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
            activities[activity_id]["entities"].append(entity_copy)  # ty:ignore[unresolved-attribute]

        return jsonify({"data": {"activities": activities}})
    except UnfurledError as e:
        _LOG.error("Failed to fetch unused activity entities: %s", e)
        return _api_error("unused_entities_unavailable", str(e), 502)


@app.route("/api/v1/diagnostics/orphaned-ir-codesets")
async def get_orphaned_ir_codesets():
    """Return orphaned custom IR codesets as JSON."""
    client = _get_active_remote_client()
    if not client:
        return jsonify({"data": []})
    if not is_remote_online(get_active_remote_id()):
        return _api_error("remote_offline", "The active remote is offline", 503)

    try:
        orphaned_codesets = await client.helpers.find_orphaned_ir_codesets()
        _LOG.debug("Found %d orphaned IR codesets", len(orphaned_codesets))

        return jsonify({"data": orphaned_codesets})
    except UnfurledError as e:
        _LOG.error("Failed to fetch orphaned IR codesets: %s", e)
        return _api_error("orphaned_codesets_unavailable", str(e), 502)


@app.route("/api/v1/diagnostics/ir-codesets/<device_id>", methods=["DELETE"])
async def delete_ir_codeset(device_id: str):
    """Delete a custom IR codeset."""
    client = _get_active_remote_client()
    if not client:
        return _api_error("remote_unavailable", "Not connected to remote", 503)

    try:
        await client.api.delete_ir_custom_code(device_id)
        _LOG.info("Deleted IR codeset: %s", device_id)
        return jsonify({"data": {"deleted": True, "deviceId": device_id}})
    except UnfurledError as e:
        _LOG.error("Failed to delete IR codeset %s: %s", device_id, e)
        return _api_error("codeset_delete_failed", str(e), 502)


@app.route("/api/v1/diagnostics/ir-codesets/reassociate", methods=["POST"])
async def reassociate_ir_codeset():
    """Create a new remote associated with a custom IR codeset."""
    if not _get_active_remote_client():
        return _api_error("remote_unavailable", "Not connected to remote", 503)

    try:
        # Handle both JSON and form data
        if request.is_json:
            data = await request.get_json()
        else:
            data = (await request.form).to_dict()

        device_id = data.get("device_id")
        remote_name = data.get("remote_name")

        if not device_id or not remote_name:
            return _api_error(
                "invalid_request", "Missing device_id or remote_name", 400
            )

        # Create remote with custom codeset ID
        await _get_active_remote_client().api.post_remote(  # ty:ignore[unresolved-attribute]
            {"name": {"en": remote_name}, "codeset_id": device_id}
        )

        _LOG.info("Created remote '%s' for codeset %s", remote_name, device_id)
        return jsonify(
            {
                "data": {
                    "created": True,
                    "deviceId": device_id,
                    "remoteName": remote_name,
                }
            }
        )
    except UnfurledError as e:
        _LOG.error("Failed to reassociate IR codeset: %s", e)
        return _api_error("codeset_reassociation_failed", str(e), 502)


@app.route("/api/v1/diagnostics/reboot", methods=["POST"])
async def system_reboot():
    """Reboot the remote."""
    if not _get_active_remote_client():
        return _api_error("remote_unavailable", "Not connected to remote", 503)

    try:
        await _get_active_remote_client().api.post_system_command("REBOOT")  # ty:ignore[unresolved-attribute]
        return jsonify({"data": {"sent": True, "message": "Reboot command sent"}})
    except UnfurledError as e:
        _LOG.error("Failed to reboot remote: %s", e)
        return _api_error("reboot_failed", str(e), 502)


@app.route("/api/v1/diagnostics/power-off", methods=["POST"])
async def system_power_off():
    """Power off the remote."""
    if not _get_active_remote_client():
        return _api_error("remote_unavailable", "Not connected to remote", 503)

    try:
        await _get_active_remote_client().api.post_system_command("POWER_OFF")  # ty:ignore[unresolved-attribute]
        return jsonify({"data": {"sent": True, "message": "Power off command sent"}})
    except UnfurledError as e:
        _LOG.error("Failed to power off remote: %s", e)
        return _api_error("power_off_failed", str(e), 502)


@app.route("/api/v1/backups")
async def api_v1_backups():
    """List active-remote backups as data rather than a rendered list."""
    remote_id = get_active_remote_id()
    backups = (
        get_all_backups().get("remotes", {}).get(remote_id, {}).get("integrations", {})
    )
    return jsonify(
        {
            "data": [
                {
                    "driverId": driver_id,
                    "timestamp": info.get("timestamp"),
                    "hasData": bool(info.get("data")),
                }
                for driver_id, info in backups.items()
            ]
        }
    )


@app.route("/api/v1/backups/<driver_id>")
async def api_v1_backup(driver_id: str):
    """Return raw stored configuration as JSON data."""
    data = get_backup(driver_id, remote_id=get_active_remote_id())
    if data is None:
        return _api_error("backup_not_found", "No backup data found", 404)
    try:
        value = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        value = data
    return jsonify({"data": {"driverId": driver_id, "content": value}})


@app.route("/api/v1/backups/<driver_id>", methods=["DELETE"])
async def api_v1_delete_backup(driver_id: str):
    delete_backup(driver_id, remote_id=get_active_remote_id())
    return jsonify({"data": {"driverId": driver_id, "deleted": True}})


@app.route("/api/v1/backups/export")
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


@app.route("/api/v1/backups/import", methods=["POST"])
async def upload_complete_backup():
    """Upload and restore complete backup file (all integrations + settings)."""
    try:
        files = await request.files
        if "file" not in files:
            return _api_error("backup_file_required", "No file provided")

        file = files["file"]
        if file.filename == "":
            return _api_error("backup_file_required", "No file selected")

        # Read and validate JSON
        try:
            content = file.read().decode("utf-8")
            backup_data = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return _api_error("invalid_backup_file", f"Invalid backup file: {e}")

        # Validate backup structure
        if "version" not in backup_data:
            return _api_error(
                "invalid_backup_file", "Invalid backup file: missing version field"
            )

        # Save uploaded backup temporarily and migrate if needed
        active_remote_id = get_active_remote_id()
        if active_remote_id is None:
            return _api_error("remote_required", "No active remote selected")

        # If v1.0 format, save it and run migration
        if backup_data.get("version") == "1.0":
            _LOG.info("Uploaded backup is v1.0 format, will migrate to v2.0")

            # Save the v1.0 backup temporarily
            try:
                with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, indent=2)
            except OSError as e:
                return _api_error(
                    "backup_save_failed", f"Failed to save backup: {e}", 500
                )

            # Run the migration with the active remote ID
            if not migrate_v1_to_v2(target_remote_id=active_remote_id):
                return _api_error(
                    "backup_migration_failed",
                    "Failed to migrate v1.0 backup to v2.0 format",
                    500,
                )

            # Reload the migrated data
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    backup_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                return _api_error(
                    "backup_reload_failed",
                    f"Failed to reload migrated backup: {e}",
                    500,
                )

            _LOG.info("Successfully migrated v1.0 backup to v2.0 format")

        # Restore settings if present in v2.0 format
        settings_restored = False
        remotes_data = backup_data.get("remotes")
        if isinstance(remotes_data, dict) and active_remote_id in remotes_data:
            remote_data = remotes_data[active_remote_id]

            if isinstance(remote_data, dict):
                settings_data = remote_data.get("settings")
                if isinstance(settings_data, dict) and settings_data:
                    try:
                        settings = Settings(**settings_data)
                        settings.save(remote_id=active_remote_id)
                        settings_restored = True
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
            return _api_error("backup_save_failed", f"Failed to save backup: {e}", 500)

        # Calculate integration count from v2.0 structure
        integration_count = 0
        if isinstance(remotes_data, dict) and active_remote_id in remotes_data:
            remote_data = remotes_data[active_remote_id]
            if isinstance(remote_data, dict):
                integrations = remote_data.get("integrations", {})
                if isinstance(integrations, dict):
                    integration_count = len(integrations)

        message = f"Successfully restored {integration_count} integration backup(s)"
        return jsonify(
            {
                "data": {
                    "message": message,
                    "integrationCount": integration_count,
                    "settingsRestored": settings_restored,
                    "restartRequired": False,
                }
            }
        )
    except Exception as e:
        _LOG.error("Failed to upload backup: %s", e)
        return _api_error("backup_import_failed", str(e), 500)


# =============================================================================
# Web Server Class
# =============================================================================


class WebServer:
    """Manage the Quart/Hypercorn server on its dedicated event-loop thread."""

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

        # No Remote session has been opened at construction time. All future
        # replacement and closure is performed on Hypercorn's event loop.
        self._replace_remote_references(remote_configs)
        _LOG.info(
            "WebServer.__init__: loaded %d remote(s): %s",
            len(_remote_clients),
            list(_remote_clients.keys()),
        )

        _github_client = GitHubClient()
        _sync_github_client = _SyncGitHubClient()

        # Ensure the static bundle directory exists.
        self._setup_directories()

    @staticmethod
    def _make_remote(config: RemoteConfig) -> Remote:
        return Remote(
            f"http://{config.address}:80/api/",
            pin=config.pin,
            api_key=config.api_key,
        )

    def _replace_remote_references(
        self, remote_configs: list[RemoteConfig]
    ) -> list[Remote]:
        """Replace the global client map and return clients that need closing."""
        previous_clients = list(_remote_clients.values())
        _remote_clients.clear()
        _remote_configs.clear()
        _remote_connectivity.clear()
        # Previous Remote instances are closed below, which also closes any
        # short-lived firmware-progress socket they owned.
        _firmware_update_websockets.clear()
        for config in remote_configs:
            _remote_configs[config.identifier] = config
            _remote_clients[config.identifier] = self._make_remote(config)
            if config.identifier not in _remote_online:
                _remote_online[config.identifier] = True
        return previous_clients

    async def _replace_remotes_on_server_loop(
        self, remote_configs: list[RemoteConfig]
    ) -> None:
        """Atomically replace Remote clients and close prior aiohttp sessions."""
        previous_clients = self._replace_remote_references(remote_configs)
        if previous_clients:
            results = await asyncio.gather(
                *(client.close() for client in previous_clients),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    _LOG.warning("Failed to close replaced Remote client: %s", result)

    async def _close_remote_clients_on_server_loop(self) -> None:
        """Close unfurled resources before Hypercorn's owning loop exits."""
        clients = list(_remote_clients.values())
        if not clients:
            return
        results = await asyncio.gather(
            *(client.close() for client in clients), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                _LOG.warning(
                    "Failed to close Remote client during shutdown: %s", result
                )

    def _setup_directories(self) -> None:
        """Create required directories if they don't exist."""
        os.makedirs(STATIC_DIR, exist_ok=True)

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

        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        self._loop = loop
        self._shutdown_event = _asyncio.Event()
        try:
            is_docker = os.environ.get("UC_CONFIG_HOME", "").startswith("/config")
            _LOG.info(
                "Creating Hypercorn server on %s:%d%s",
                self._host,
                self._port,
                "" if is_docker else f" (legacy notice on :{LEGACY_WEB_SERVER_PORT})",
            )
            config = HypercornConfig()
            bindings = [f"{self._host}:{self._port}"]
            if not is_docker:
                bindings.append(f"{self._host}:{LEGACY_WEB_SERVER_PORT}")
            config.bind = bindings
            config.loglevel = "WARNING"
            _LOG.info("Server configured, starting to serve...")
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
                _asyncio.set_event_loop(None)

    async def _shutdown_trigger(self) -> None:
        """Awaitable trigger used by hypercorn to initiate graceful shutdown."""
        while self._running:
            await asyncio.sleep(0.5)

    def stop(self) -> None:
        """Stop the web server."""
        if not self._running:
            return

        _LOG.info("Stopping web server")
        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._close_remote_clients_on_server_loop(), loop
                ).result(timeout=5)
            except Exception as error:
                _LOG.warning(
                    "Failed to close Remote clients before shutdown: %s", error
                )
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

        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._replace_remotes_on_server_loop(remote_configs), loop
            ).result(timeout=10)
        else:
            # During initial setup no client session can exist yet.
            self._replace_remote_references(remote_configs)

        for config in remote_configs:
            _LOG.info("Loaded remote: %s (%s)", config.name, config.identifier)

        _LOG.info(
            "Remote reload complete - %d remotes configured: %s",
            len(_remote_clients),
            list(_remote_clients.keys()),
        )

    @property
    def is_running(self) -> bool:
        """Check if the web server is running."""
        return self._running

    async def run_on_server_loop(self, operation: Coroutine[Any, Any, Any]) -> Any:
        """Run a web-server operation on Hypercorn's event loop.

        Polling devices live on the integration driver's loop, while unfurled's
        Remote sessions belong to Hypercorn's loop.  Crossing that boundary
        through ``run_coroutine_threadsafe`` prevents aiohttp futures from
        being reused by the wrong event loop.
        """
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            operation.close()
            raise RuntimeError("Web server event loop is not running")
        if asyncio.get_running_loop() is loop:
            return await operation
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        return await asyncio.wrap_future(future)

    async def refresh_integration_versions(self, remote_id: str) -> None:
        """
        Refresh version information for all installed integrations.

        This checks GitHub for the latest releases and updates the cached
        version data used by the UI.

        :param remote_id: Remote identifier to refresh versions for
        """
        await _refresh_version_cache(remote_id)
        await _run_automatic_updates(remote_id)

    async def check_connectivity(self, remote_id: str, *, force: bool = False) -> None:
        """
        Test whether a remote is reachable and update its online status.

        This is the single owner of set_remote_online for periodic heartbeat
        checks. On-demand status changes (connect/disconnect lifecycle events)
        are handled directly in device.py.

        :param remote_id: Remote identifier to test
        :param force: Bypass offline backoff after an explicit reconnect event
        """
        client = _remote_clients.get(remote_id)
        if not client:
            _LOG.warning(
                "[%s] check_connectivity: no client in _remote_clients (known: %s)",
                remote_id,
                list(_remote_clients.keys()),
            )
            return

        probe = _remote_connectivity.setdefault(remote_id, _ConnectivityProbeState())
        now = time.monotonic()
        if not force and not is_remote_online(remote_id) and now < probe.next_probe_at:
            _LOG.debug(
                "[%s] Skipping offline heartbeat for %.0fs",
                remote_id,
                probe.next_probe_at - now,
            )
            return

        was_online = is_remote_online(remote_id)
        try:
            # This public endpoint is deliberately a small, separately timed
            # liveness check rather than a full authenticated API operation.
            await client.api.request(
                "GET", "pub/version", timeout=_CONNECTIVITY_TIMEOUT
            )
            set_remote_online(remote_id, True)
            probe.failure_count = 0
            probe.next_probe_at = 0.0
            if not was_online:
                _LOG.info("[%s] Remote connectivity restored", remote_id)
        except Exception as e:
            probe.failure_count += 1
            delay = min(
                30.0 * (2 ** (probe.failure_count - 1)),
                _OFFLINE_HEARTBEAT_MAX_INTERVAL,
            )
            probe.next_probe_at = now + delay
            if was_online:
                _LOG.warning("[%s] Connectivity check failed: %s", remote_id, e)
            else:
                _LOG.debug("[%s] Offline heartbeat failed: %s", remote_id, e)
            set_remote_online(remote_id, False)

    async def check_all_remote_connectivity(self, *, force: bool = False) -> None:
        """Test connectivity for every configured remote and update online status."""
        remote_ids = list(_remote_clients.keys())
        if remote_ids:
            await asyncio.gather(
                *(
                    self.check_connectivity(remote_id, force=force)
                    for remote_id in remote_ids
                )
            )

    async def check_error_states(self, remote_id: str) -> None:
        """
        Check all integrations for error/disconnected states and send notifications.

        Skipped when the remote is offline — connectivity is managed separately
        by check_connectivity / check_all_remote_connectivity.

        :param remote_id: Remote identifier to check
        """
        if not is_remote_online(remote_id):
            return

        client = _remote_clients.get(remote_id)
        if not client:
            return

        try:
            # Triggers error-state notifications automatically via _get_installed_integrations
            await _get_installed_integrations(remote_id)
        except Exception as e:
            _LOG.warning(
                "[%s] Failed to check integration error states: %s", remote_id, e
            )

    async def check_all_error_states(self) -> None:
        """Check integration error states for every configured remote."""
        for remote_id in list(_remote_clients.keys()):
            await self.check_error_states(remote_id)

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
            orphaned_entities = await client.helpers.find_orphaned_entities()
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

        except UnfurledError as e:
            _LOG.warning("[%s] Failed to check for orphaned entities: %s", remote_id, e)
        except Exception as e:
            _LOG.error(
                "[%s] Unexpected error checking orphaned entities: %s", remote_id, e
            )

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

                can_backup, reason = backup_support_status(version, reg_item)
                if not can_backup:
                    continue

                total_attempted += 1

                # Try to backup (with remote_id for namespacing)
                backup_data = await backup_integration(
                    client.api, driver_id, save_to_file=True, remote_id=remote_id
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
