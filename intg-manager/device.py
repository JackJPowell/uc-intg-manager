"""
Device Communication Module.

This module handles communication with the Unfolded Circle Remote.
It manages connections, polls power status, and controls the web server.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import json
import logging
import os
from asyncio import AbstractEventLoop
from datetime import datetime
from typing import Any

from const import (
    MANAGER_DATA_FILE,
    POWER_POLL_INTERVAL,
    VERSION_CHECK_INTERVAL_POLLS,
    WEB_SERVER_PORT,
    RemoteConfig,
    Settings,
    is_external_mode,
)
from notification_manager import get_notification_manager as _get_nm
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, PollingDevice
from unfurled.api import CoreAPI
from unfurled.helpers.exceptions import UnfurledError as RemoteAPIError
from web_server import (
    WebServer,
    set_firmware_version,
    set_remote_online,
    set_system_update_info,
)

_LOG = logging.getLogger(__name__)

# Module-level web server coordination
_all_remote_configs: list[RemoteConfig] = []
_web_server_instance: WebServer | None = None


def register_remote_config(config: RemoteConfig) -> None:
    """Register a remote config for multi-remote support."""
    if config not in _all_remote_configs:
        _all_remote_configs.append(config)


async def _async_port_in_use(port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP connect to 127.0.0.1:port succeeds within timeout.

    Non-blocking: uses asyncio.open_connection so the event loop stays
    responsive while waiting for the connect attempt.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


class IntegrationManagerDevice(PollingDevice):
    """
    Device class representing the connection to the Unfolded Circle Remote.

    This class handles:
    - Polling the remote for power/dock status
    - Starting/stopping the web server based on dock status
    - Managing the remote API connection
    """

    def __init__(
        self,
        device_config: RemoteConfig,
        loop: AbstractEventLoop | None,
        config_manager: BaseConfigManager | None = None,
        driver: BaseIntegrationDriver | None = None,
    ) -> None:
        """
        Initialize the device.

        :param device_config: Configuration for this device
        :param loop: Event loop for async operations
        :param config_manager: Configuration manager instance
        """
        super().__init__(
            device_config=device_config,
            loop=loop,
            config_manager=config_manager,
            poll_interval=POWER_POLL_INTERVAL,
            driver=driver,
        )

        self._device_config: RemoteConfig = device_config

        # Keep one unfurled client per configured Remote for polling.
        self._client = CoreAPI(
            f"http://{device_config.address}:80/api/",
            pin=device_config.pin if device_config.pin else None,
            api_key=device_config.api_key if device_config.api_key else None,
        )

        # Web server instance
        self._web_server: WebServer | None = None

        # Track dock state
        self._is_docked: bool = False
        self._connected: bool = False

        # Track if we're running in external/Docker mode
        self._is_external: bool = False

        # Poll counter for periodic version checking
        self._poll_count: int = 0

        # Last backup date for scheduling
        self._last_backup_date: str | None = None

        # Register this remote config for multi-remote support
        register_remote_config(device_config)

        # Ensure this remote exists in manager.json
        self._ensure_remote_in_manager_json()

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def identifier(self) -> str:
        """Return the device identifier."""
        return self._device_config.identifier

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._device_config.name

    @property
    def address(self) -> str | None:
        """Return the device address."""
        return self._device_config.address

    def _is_owner(self) -> bool:
        """
        Check if this remote is the web server owner.

        The owner is simply the first remote in the configuration file.
        This avoids race conditions and complex election logic.
        """
        return (
            len(_all_remote_configs) > 0
            and _all_remote_configs[0].identifier == self.identifier
        )

    @property
    def log_id(self) -> str:
        """Return the device log identifier."""
        return self.name if self.name else self.identifier

    @property
    def is_docked(self) -> bool:
        """Return whether the remote is currently docked."""
        return self._is_docked

    # =========================================================================
    # Remote Registration
    # =========================================================================

    def _ensure_remote_in_manager_json(self) -> None:
        """
        Ensure this remote has an entry in manager.json.

        This is called during device initialization to automatically add
        new remotes to the manager.json file so they appear in the web UI.
        """
        try:
            # Load existing manager.json
            if not os.path.exists(MANAGER_DATA_FILE):
                # No manager.json yet - will be created on first save
                _LOG.debug(
                    "[%s] No manager.json yet, will be created on first save",
                    self.log_id,
                )
                return

            with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Check if this remote already exists
            remotes = data.get("remotes", {})
            if self.identifier in remotes:
                _LOG.debug("[%s] Remote already exists in manager.json", self.log_id)
                # Still trigger reload in case this is a newly added remote from config.json
                self._trigger_web_server_reload()
                return

            # Add this remote to manager.json
            _LOG.info("[%s] Adding new remote to manager.json", self.log_id)

            # Ensure v2.0 structure
            if "version" not in data:
                data["version"] = "2.0"
            if "remotes" not in data:
                data["remotes"] = {}
            if "shared" not in data:
                data["shared"] = {}

            # Add remote entry with empty sections
            data["remotes"][self.identifier] = {
                "settings": {},
                "integrations": {},
                "notification_state": {},
            }

            # Save back to file
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            _LOG.info("[%s] Successfully added remote to manager.json", self.log_id)

            # Trigger web server reload if it's already running
            self._trigger_web_server_reload()

        except (json.JSONDecodeError, OSError) as e:
            _LOG.error(
                "[%s] Failed to ensure remote in manager.json: %s", self.log_id, e
            )

    def _trigger_web_server_reload(self) -> None:
        """
        Trigger web server reload with updated remote configs.

        This is called after adding a new remote to ensure the web UI
        immediately shows the new remote in the dropdown.
        """
        global _web_server_instance

        # Use global web server reference if available
        if _web_server_instance and _web_server_instance.is_running:
            _LOG.info("[%s] Reloading web server with new remote configs", self.log_id)
            _web_server_instance.reload_remotes()
            return

        # Otherwise, remote will appear after web server starts or page refresh
        _LOG.debug(
            "[%s] New remote added - will be available when web server starts",
            self.log_id,
        )

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def _test_remote_connection(self) -> bool:
        """Return whether the Remote responds to its public version endpoint."""
        try:
            await self._client.get_version()
            return True
        except RemoteAPIError:
            return False

    async def _get_dock_state(self) -> bool:
        """Return whether the Remote is docked or wirelessly charging."""
        charger = await self._client.get_charger()
        return bool(
            charger.get("power_supply", False)
            or charger.get("wireless_charging", False)
        )

    async def establish_connection(self) -> None:
        """Establish connection to the remote (required by PollingDevice)."""
        _LOG.debug("[%s] Connecting to remote at %s", self.log_id, self.address)

        try:
            # Test connection
            if await self._test_remote_connection():
                self._connected = True
                set_remote_online(self.identifier, True)
                _LOG.info("[%s] Connected to remote", self.log_id)

                # Fetch and cache firmware version for inplace-update capability checks
                try:
                    fw_version = str(
                        (await self._client.get_version()).get("os", "0.0.0")
                    )
                    set_firmware_version(self.identifier, fw_version)
                    _LOG.info("[%s] Firmware version: %s", self.log_id, fw_version)
                except Exception as e:
                    _LOG.warning(
                        "[%s] Could not fetch firmware version: %s", self.log_id, e
                    )

                # Check if we're running in external mode
                self._is_external = is_external_mode()
                config_home = os.getenv("UC_CONFIG_HOME", "")

                if self._is_external:
                    # Running externally (Docker, PC, Mac, etc.) - always start web server
                    _LOG.info(
                        "[%s] Running in external mode (UC_CONFIG_HOME: %s) - starting web server",
                        self.log_id,
                        config_home if config_home else "not set",
                    )
                    self._is_docked = True  # Treat as always "docked" in external mode
                    await self._on_docked()
                else:
                    # Running on Remote as integration - check dock state and start web server if appropriate
                    _LOG.info(
                        "[%s] Running on remote as integration (UC_CONFIG_HOME: %s)",
                        self.log_id,
                        config_home,
                    )
                    try:
                        self._is_docked = await self._get_dock_state()
                        if self._is_docked:
                            _LOG.info(
                                "[%s] Remote is charging at startup (dock or wireless)",
                                self.log_id,
                            )
                            await self._on_docked()
                        else:
                            _LOG.info(
                                "[%s] Remote is on battery at startup", self.log_id
                            )
                            # Check if web server should run even on battery
                            if not Settings.load(self.identifier).shutdown_on_battery:
                                _LOG.info(
                                    "[%s] Starting web server on battery (shutdown_on_battery=False)",
                                    self.log_id,
                                )
                                await self._on_docked()
                    except RemoteAPIError as e:
                        _LOG.warning(
                            "[%s] Failed to check initial charging status: %s",
                            self.log_id,
                            e,
                        )
                    except Exception as e:
                        # This catches web server startup failures
                        _LOG.error(
                            "[%s] Error during startup initialization: %s",
                            self.log_id,
                            e,
                        )
            else:
                raise RemoteAPIError("Connection test failed")

        except RemoteAPIError as e:
            _LOG.error("[%s] Failed to connect: %s", self.log_id, e)
            self._connected = False
            set_remote_online(self.identifier, False)
            raise
        except asyncio.TimeoutError as e:
            _LOG.error("[%s] Timeout while trying to connect: %s", self.log_id, e)
            self._connected = False
            set_remote_online(self.identifier, False)
            raise RemoteAPIError("Connection test timed out") from e
        except Exception as e:
            _LOG.error("[%s] Unexpected error during connection: %s", self.log_id, e)
            self._connected = False
            set_remote_online(self.identifier, False)
            raise RemoteAPIError(f"Connection test failed: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from the remote."""
        _LOG.debug("[%s] Disconnecting from remote", self.log_id)
        global _web_server_instance
        set_remote_online(self.identifier, False)

        # Only stop web server if:
        # 1. We're running on the remote (not Docker), AND
        # 2. We're the owner
        # In Docker mode, web server should keep running even if this remote disconnects
        if (
            not self._is_external
            and self._is_owner()
            and self._web_server
            and self._web_server.is_running
        ):
            # stop() joins the Hypercorn thread (up to 5s); run off-loop.
            await asyncio.to_thread(self._web_server.stop)
            self._web_server = None
            _web_server_instance = None
            _LOG.info("[%s] Web server stopped", self.log_id)

        # Close API client
        await self._client.close()
        self._connected = False

        # Let base class handle stopping the polling
        await super().disconnect()

    async def verify_connection(self) -> None:
        """
        Verify connection to the remote and emit current state.

        This method is called by the framework to check device connectivity.
        """
        _LOG.debug("[%s] Verifying connection to remote", self.log_id)

        try:
            if await self._test_remote_connection():
                self._connected = True
                set_remote_online(self.identifier, True)
                _LOG.debug("[%s] Connection verified", self.log_id)
            else:
                self._connected = False
                set_remote_online(self.identifier, False)
                _LOG.warning("[%s] Connection verification failed", self.log_id)
        except RemoteAPIError as err:
            _LOG.error("[%s] Connection verification failed: %s", self.log_id, err)
            self._connected = False
            set_remote_online(self.identifier, False)
            raise
        except asyncio.TimeoutError as err:
            _LOG.error("[%s] Connection verification timed out: %s", self.log_id, err)
            self._connected = False
            set_remote_online(self.identifier, False)
            raise
        except Exception as err:
            _LOG.error(
                "[%s] Unexpected error during connection verification: %s",
                self.log_id,
                err,
            )
            self._connected = False
            set_remote_online(self.identifier, False)
            raise

    # =========================================================================
    # Polling Implementation
    # =========================================================================

    async def poll_device(self) -> None:
        """
        Poll the remote for charging status (required by PollingDevice).

        This method is called periodically by the PollingDevice base class.
        It checks if the remote is charging (docked or wireless) and starts/stops
        the web server accordingly. Also triggers periodic version checks for
        installed integrations.

        When running in external/Docker mode, skip charging status checks since
        the web server should always be running.
        """
        self._poll_count += 1

        # Build poll activity summary for consolidated logging
        poll_tasks = []
        periodic_check = self._poll_count % VERSION_CHECK_INTERVAL_POLLS == 0

        if periodic_check:
            poll_tasks.append("versions/orphans/registry")
            poll_tasks.append("system-update")
        if self._is_owner() and periodic_check:
            poll_tasks.append("repo-batch")
        poll_tasks.append("backup-check")
        poll_tasks.append("error-states")
        if self._is_owner() and not self._is_external:
            poll_tasks.append("health-check")

        tasks_str = ", ".join(poll_tasks) if poll_tasks else "dock-status-only"
        _LOG.debug(
            "[%s] Poll #%d: %s | docked=%s owner=%s",
            self.identifier,
            self._poll_count,
            tasks_str,
            self._is_docked,
            self._is_owner(),
        )

        try:
            # Skip dock polling in external/Docker mode - web server always runs
            if not self._is_external:
                was_docked = self._is_docked
                self._is_docked = await self._get_dock_state()

                # Handle dock state changes
                if self._is_docked and not was_docked:
                    # Remote just docked - start web server
                    await self._on_docked()
                elif not self._is_docked and was_docked:
                    # Remote just undocked - stop web server
                    await self._on_undocked()

            # Use global web server instance for polling tasks
            # This allows all remotes to perform their checks even if they're not the owner
            web_server = _web_server_instance

            # Periodic version check (every VERSION_CHECK_INTERVAL_POLLS polls)
            # Only check when docked and web server is running
            if (
                self._is_docked
                and web_server
                and web_server.is_running
                and self._poll_count % VERSION_CHECK_INTERVAL_POLLS == 0
            ):
                # Per-remote task: Check integration versions for this remote
                await self._check_integration_versions()

                # Per-remote task: Check for system firmware updates
                await self._check_system_update()

                # Per-remote task: Check firmware updates for associated docks
                await self._check_dock_updates()

                # Shared task: Fetch repository batch (owner only)
                if self._is_owner():
                    web_server.fetch_repository_batch()

            # Periodic backup check - only when docked and web server is running
            if self._is_docked and web_server and web_server.is_running:
                # Per-remote task: Scheduled backup for this remote
                await self._check_scheduled_backup()

            # Connectivity heartbeat + integration error states — runs every poll
            if web_server and web_server.is_running:
                if self._is_owner():
                    # Owner handles ALL remotes — some (added via setup) have no polling device
                    await web_server.run_on_server_loop(
                        web_server.check_all_remote_connectivity()
                    )
                    await web_server.run_on_server_loop(
                        web_server.check_all_error_states()
                    )
                else:
                    await web_server.run_on_server_loop(
                        web_server.check_connectivity(self.identifier)
                    )
                    await web_server.run_on_server_loop(
                        web_server.check_error_states(self.identifier)
                    )

            # On-remote mode: per-poll restart probe. External mode uses
            # the driver-level watchdog instead (see driver._web_server_watchdog).
            if not self._is_external:
                await self._check_web_server_health()

        except RemoteAPIError as e:
            _LOG.warning("[%s] Failed to poll power status: %s", self.log_id, e)

    async def _on_docked(self) -> None:
        """Handle remote being docked/charging - start web server if owner."""
        _LOG.info("[%s] Remote charging started", self.log_id)

        global _web_server_instance

        try:
            # In external mode, check if web server is already running globally
            if (
                self._is_external
                and _web_server_instance
                and _web_server_instance.is_running
            ):
                _LOG.info(
                    "[%s] Web server already running in external mode - reusing",
                    self.log_id,
                )
                # Set local reference to global instance
                self._web_server = _web_server_instance
                await self._run_initial_integration_checks()
                return

            # In remote mode, only the owner (first in config) starts the web server
            if not self._is_external and not self._is_owner():
                _LOG.info(
                    "[%s] Skipping web server start - not owner (owner is %s)",
                    self.log_id,
                    _all_remote_configs[0].identifier
                    if _all_remote_configs
                    else "unknown",
                )
                return

            if self._is_external:
                _LOG.info("[%s] Starting web server in Docker mode", self.log_id)
            else:
                _LOG.info(
                    "[%s] Starting web server as owner (first remote in config)",
                    self.log_id,
                )

            if self._web_server is None:
                # Pass all registered remote configs to WebServer
                self._web_server = WebServer(
                    remote_configs=_all_remote_configs,
                )
            else:
                # Web server already exists - reload with updated remote configs
                # This happens when a new remote is added through setup
                _LOG.info(
                    "[%s] Reloading web server with updated remote configs", self.log_id
                )
                self._web_server.reload_remotes(_all_remote_configs)

            if not self._web_server.is_running:
                self._web_server.start()

                # Poll for actual port bind. WebServer.start() flips is_running
                # True before Hypercorn binds the listening socket, so a plain
                # flag check can publish a not-yet-listening instance.
                bound = False
                for _ in range(25):
                    await asyncio.sleep(0.2)
                    if not self._web_server.is_running:
                        break
                    if await _async_port_in_use(self._web_server.port):
                        bound = True
                        break

                if bound:
                    _LOG.info("[%s] Web server started successfully", self.log_id)
                    # Publish global reference once the bind is confirmed.
                    _web_server_instance = self._web_server
                    await self._run_initial_integration_checks()
                else:
                    _LOG.error(
                        "[%s] Web server failed to start (check logs for port conflicts)",
                        self.log_id,
                    )
                    try:
                        # stop() joins the Hypercorn thread; run off-loop.
                        await asyncio.to_thread(self._web_server.stop)
                    except Exception as e:
                        _LOG.warning(
                            "[%s] Stop of failed web server raised: %s", self.log_id, e
                        )
                    self._web_server = None
            else:
                # Already running (e.g., reloaded). Ensure global points to it.
                _web_server_instance = self._web_server
        except Exception as e:
            _LOG.error(
                "[%s] Failed to start web server: %s", self.log_id, e, exc_info=True
            )
            self._web_server = None

    async def _run_initial_integration_checks(self) -> None:
        """Trigger per-remote startup checks once the web server is available."""
        if not self._web_server:
            return

        _LOG.info("[%s] Triggering initial integration checks...", self.log_id)
        try:
            # Per-remote: Check for version updates
            await self._web_server.run_on_server_loop(
                self._web_server.refresh_integration_versions(self.identifier)
            )
            # Per-remote: Check for new integrations in registry
            await self._web_server.run_on_server_loop(
                self._web_server.check_new_integrations(self.identifier)
            )
            # Per-remote: Check for orphaned entities in activities
            await self._web_server.run_on_server_loop(
                self._web_server.check_orphaned_entities(self.identifier)
            )
            # Shared (owner only): Check for new system messages from GitHub
            if self._is_owner():
                self._web_server.check_system_messages()
            _LOG.info("[%s] Initial integration checks complete", self.log_id)
        except Exception as e:
            _LOG.warning(
                "[%s] Initial integration checks failed: %s", self.log_id, e
            )

    async def _on_undocked(self) -> None:
        """Handle remote being undocked/unplugged - conditionally stop web server."""
        global _web_server_instance

        # In Docker mode, this should never be called, but add safety check
        if self._is_external:
            _LOG.debug(
                "[%s] Ignoring undock event in Docker mode - web server always runs",
                self.log_id,
            )
            return

        # Load settings for THIS remote
        settings = Settings.load(self.identifier)

        if not settings.shutdown_on_battery:
            _LOG.info(
                "[%s] Remote on battery - web server remains running (shutdown_on_battery=False)",
                self.log_id,
            )
            return

        _LOG.info("[%s] Remote on battery", self.log_id)

        # Only stop web server if we're the owner
        if self._is_owner() and self._web_server and self._web_server.is_running:
            # stop() joins the Hypercorn thread (up to 5s); run off-loop.
            await asyncio.to_thread(self._web_server.stop)
            _LOG.info("[%s] Web server stopped", self.log_id)
            # Clear references so _on_docked() creates a fresh server on reconnect
            self._web_server = None
            _web_server_instance = None

    async def _check_integration_versions(self) -> None:
        """
        Check for updates to installed integrations.

        This is called periodically during polling to refresh version info.
        The web server caches this data for display in the UI.
        """
        web_server = _web_server_instance
        if not web_server:
            return

        _LOG.info("[%s] Checking for integration updates...", self.log_id)
        try:
            # Per-remote: Trigger the web server to refresh version data
            # This updates the cached update availability info and sends update notifications
            await web_server.run_on_server_loop(
                web_server.refresh_integration_versions(self.identifier)
            )

            # Per-remote: Check for new integrations in registry
            await web_server.run_on_server_loop(
                web_server.check_new_integrations(self.identifier)
            )

            # Per-remote: Check for orphaned entities in activities
            await web_server.run_on_server_loop(
                web_server.check_orphaned_entities(self.identifier)
            )

            # Shared (owner only): Check for new system messages from GitHub
            if self._is_owner():
                web_server.check_system_messages()

            _LOG.debug("[%s] Integration checks complete", self.log_id)
        except Exception as e:
            _LOG.warning("[%s] Failed to check integrations: %s", self.log_id, e)

    async def _check_system_update(self) -> None:
        """
        Check for system firmware updates and notify if a new version is available.

        Stores the update info in web_server for the diagnostics page.
        Only sends one notification per available firmware version.
        """
        web_server = _web_server_instance
        try:
            update_info = await self._client.get_system_update()
            if not update_info:
                return

            # Cache for diagnostics page
            if web_server:
                set_system_update_info(self.identifier, update_info)

            installed = update_info.get("installed_version", "")
            available = update_info.get("available", [])

            if not available:
                _LOG.debug(
                    "[%s] System firmware is up to date (%s)", self.log_id, installed
                )
                return

            latest = available[0]
            latest_version = latest.get("version", "")
            latest_title = latest.get("title", f"Firmware {latest_version}")

            _LOG.info(
                "[%s] Firmware update available: %s -> %s",
                self.log_id,
                installed,
                latest_version,
            )

            nm = _get_nm(self.identifier)
            await nm.notify_firmware_update(
                installed_version=installed,
                available_version=latest_version,
                title=latest_title,
            )

        except RemoteAPIError as e:
            _LOG.debug("[%s] Failed to check system update: %s", self.log_id, e)
        except Exception as e:
            _LOG.warning("[%s] Error checking system update: %s", self.log_id, e)

    async def _check_dock_updates(self) -> None:
        """Check associated docks for firmware updates and send notifications."""
        try:
            docks = await self._client.get_docks()
        except RemoteAPIError as e:
            _LOG.debug("[%s] Failed to get associated docks: %s", self.log_id, e)
            return

        for dock in docks:
            dock_id = dock.get("dock_id") if isinstance(dock, dict) else None
            if not dock_id:
                _LOG.debug("[%s] Skipping dock without an ID: %s", self.log_id, dock)
                continue

            try:
                update_info = await self._client.get_dock_update(dock_id)
            except RemoteAPIError as e:
                _LOG.debug(
                    "[%s] Failed to check update for dock %s: %s",
                    self.log_id,
                    dock_id,
                    e,
                )
                continue

            if not update_info.get("update_available"):
                continue

            firmware_update = update_info.get("firmware_update") or {}
            available_version = firmware_update.get("version", "")
            installed_version = update_info.get("version", "")
            if not available_version:
                _LOG.warning(
                    "[%s] Dock %s reports an update without a version",
                    self.log_id,
                    dock_id,
                )
                continue

            model = firmware_update.get("model", "Dock")
            _LOG.info(
                "[%s] %s %s firmware update available: %s -> %s",
                self.log_id,
                model,
                dock_id,
                installed_version,
                available_version,
            )
            await _get_nm(self.identifier).notify_dock_firmware_update(
                dock_id=dock_id,
                installed_version=installed_version,
                available_version=available_version,
                title=model,
            )

    def _is_backup_time(self, backup_time_str: str) -> bool:
        """
        Check if the current time matches the scheduled backup time.

        :param backup_time_str: Time string in "HH:MM" format
        :return: True if it's time to backup
        """

        try:
            now = datetime.now()
            current_date = now.strftime("%Y-%m-%d")

            # Check if we already backed up today
            if self._last_backup_date == current_date:
                return False

            # Parse the backup time
            backup_hour, backup_minute = map(int, backup_time_str.split(":"))

            # Check if current time matches (within the polling window)
            current_hour = now.hour
            current_minute = now.minute

            # Allow a 5-minute window around the scheduled time
            if backup_hour == current_hour:
                time_diff = abs(current_minute - backup_minute)
                return time_diff <= 5

            return False
        except (ValueError, AttributeError) as e:
            _LOG.warning(
                "[%s] Invalid backup time format '%s': %s",
                self.log_id,
                backup_time_str,
                e,
            )
            return False

    async def _check_scheduled_backup(self) -> None:
        """
        Check if it's time for scheduled backup and perform if needed.

        This is called during each poll cycle when docked and web server is running.
        """
        web_server = _web_server_instance
        if not web_server:
            return

        try:
            # Load current settings for THIS remote to check if backups are enabled
            settings = Settings.load(self.identifier)

            if not settings.backup_configs:
                return  # Automatic backups disabled

            if not self._is_backup_time(settings.backup_time):
                return  # Not backup time yet

            _LOG.info(
                "[%s] Starting scheduled backup at %s",
                self.log_id,
                settings.backup_time,
            )

            # Perform the backup via web server
            # Per-remote task: backup for this remote only
            backup_result = await web_server.run_on_server_loop(
                web_server.perform_scheduled_backup(self.identifier)
            )

            # Update last backup date on success
            if backup_result:
                self._last_backup_date = datetime.now().strftime("%Y-%m-%d")
                _LOG.info("[%s] Scheduled backup completed successfully", self.log_id)
            else:
                _LOG.warning("[%s] Scheduled backup failed", self.log_id)

        except Exception as e:
            _LOG.error("[%s] Error during scheduled backup: %s", self.log_id, e)

    async def _check_web_server_health(self) -> None:
        """
        Check if the web server is healthy and restart if needed.

        This verifies that the web server is actually accessible when it should be running.
        If the server is supposed to be running but is not responding, it performs cleanup
        and restarts the server.

        Called during each poll cycle when the web server should be active.
        On-remote mode only — external mode uses the driver-level watchdog.
        """
        global _web_server_instance

        # Use global web server instance
        web_server = _web_server_instance

        # Only the web server owner should perform health checks
        if not self._is_owner() or not web_server or not web_server.is_running:
            return

        # Determine if web server should actually be running based on current conditions
        should_be_running = False

        if self._is_docked:
            # Docked - always running
            should_be_running = True
        elif not Settings.load(self.identifier).shutdown_on_battery:
            # On battery but configured to keep running
            should_be_running = True

        if not should_be_running:
            return  # Server should not be running, skip health check

        # Test if server is actually accessible
        if await _async_port_in_use(WEB_SERVER_PORT, timeout=2.0):
            return

        # Server is not accessible but should be running
        _LOG.warning(
            "[%s] Web server should be running but is not accessible on port %d - attempting restart",
            self.log_id,
            WEB_SERVER_PORT,
        )

        # Server is not healthy - perform cleanup and restart
        try:
            # Stop the current server instance (cleanup)
            if web_server:
                try:
                    # stop() joins the Hypercorn thread (up to 5s); run off-loop.
                    await asyncio.to_thread(web_server.stop)
                except Exception as e:
                    _LOG.warning(
                        "[%s] Error stopping unhealthy web server: %s", self.log_id, e
                    )

            await asyncio.sleep(1)

            # Create new server instance
            new_server = WebServer(
                remote_configs=_all_remote_configs,
            )

            # Start the server
            new_server.start()

            # Poll for actual port bind. _running flips True before Hypercorn
            # binds, so trust only a successful TCP connect.
            bound = False
            for _ in range(25):
                await asyncio.sleep(0.2)
                if not new_server.is_running:
                    break
                if await _async_port_in_use(new_server.port):
                    bound = True
                    break

            if bound:
                _LOG.info(
                    "[%s] Web server successfully restarted after health check failure",
                    self.log_id,
                )
                # Update both global and local references
                _web_server_instance = new_server
                self._web_server = new_server
            else:
                _LOG.error(
                    "[%s] Web server failed to restart after health check failure",
                    self.log_id,
                )
                try:
                    await asyncio.to_thread(new_server.stop)
                except Exception as e:
                    _LOG.warning(
                        "[%s] Stop of failed web server raised: %s", self.log_id, e
                    )
                _web_server_instance = None
                self._web_server = None

        except Exception as e:
            _LOG.error(
                "[%s] Failed to restart web server during health check: %s",
                self.log_id,
                e,
                exc_info=True,
            )
            _web_server_instance = None
            self._web_server = None

    # =========================================================================
    # Command Handling
    # =========================================================================

    async def send_command(self, command: str, *_args: Any, **_kwargs: Any) -> None:
        """
        Send a command to the device.

        This integration doesn't have traditional commands as it's a manager UI.

        :param command: Command to send
        :param _args: Positional arguments (unused)
        :param _kwargs: Keyword arguments (unused)
        """
        _LOG.debug("[%s] Command received: %s (not implemented)", self.log_id, command)
