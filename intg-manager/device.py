"""
Device Communication Module.

This module handles communication with the Unfolded Circle Remote.
It manages connections, polls power status, and controls the web server.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from asyncio import AbstractEventLoop
from datetime import datetime
from typing import Any

from const import (
    RemoteConfig,
    Settings,
    POWER_POLL_INTERVAL,
    VERSION_CHECK_INTERVAL_POLLS,
    WEB_SERVER_PORT,
)
from remote_api import RemoteAPIClient, RemoteAPIError
from web_server import WebServer
from ucapi_framework import BaseConfigManager, PollingDevice

_LOG = logging.getLogger(__name__)


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
        )

        self._device_config: RemoteConfig = device_config

        # Load user settings
        self._settings = Settings.load()

        # Initialize the Remote API client
        self._client = RemoteAPIClient(
            address=device_config.address,
            pin=device_config.pin if device_config.pin else None,
            api_key=device_config.api_key if device_config.api_key else None,
        )

        # Web server instance
        self._web_server: WebServer | None = None

        # Track dock state
        self._is_docked: bool = False
        self._connected: bool = False

        # Poll counter for periodic version checking
        self._poll_count: int = 0

        # Last backup date for scheduling
        self._last_backup_date: str | None = None

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

    @property
    def log_id(self) -> str:
        """Return a log identifier for debugging."""
        return self.name if self.name else self.identifier

    @property
    def is_docked(self) -> bool:
        """Return whether the remote is currently docked."""
        return self._is_docked

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def establish_connection(self) -> None:
        """Establish connection to the remote (required by PollingDevice)."""
        _LOG.debug("[%s] Connecting to remote at %s", self.log_id, self.address)

        try:
            # Test connection
            if await self._client.test_connection():
                self._connected = True
                _LOG.info("[%s] Connected to remote", self.log_id)
            else:
                raise RemoteAPIError("Connection test failed")

        except RemoteAPIError as e:
            _LOG.error("[%s] Failed to connect: %s", self.log_id, e)
            self._connected = False
            raise

    async def disconnect(self) -> None:
        """Disconnect from the remote."""
        _LOG.debug("[%s] Disconnecting from remote", self.log_id)

        # Stop polling
        self._stop_polling()

        # Stop web server if running
        if self._web_server and self._web_server.is_running:
            self._web_server.stop()
            self._web_server = None

        # Close API client
        await self._client.close()
        self._connected = False

    async def verify_connection(self) -> None:
        """
        Verify connection to the remote and emit current state.

        This method is called by the framework to check device connectivity.
        """
        _LOG.debug("[%s] Verifying connection to remote", self.log_id)

        try:
            if await self._client.test_connection():
                self._connected = True
                _LOG.debug("[%s] Connection verified", self.log_id)
            else:
                self._connected = False
                _LOG.warning("[%s] Connection verification failed", self.log_id)
        except RemoteAPIError as err:
            _LOG.error("[%s] Connection verification failed: %s", self.log_id, err)
            self._connected = False
            raise

    # =========================================================================
    # Polling Implementation
    # =========================================================================

    async def poll_device(self) -> None:
        """
        Poll the remote for power/dock status (required by PollingDevice).

        This method is called periodically by the PollingDevice base class.
        It checks if the remote is docked and starts/stops the web server accordingly.
        Also triggers periodic version checks for installed integrations.
        """
        self._poll_count += 1

        try:
            was_docked = self._is_docked
            self._is_docked = await self._client.is_docked()

            # Handle dock state changes
            if self._is_docked and not was_docked:
                # Remote just docked - start web server
                await self._on_docked()
            elif not self._is_docked and was_docked:
                # Remote just undocked - stop web server
                await self._on_undocked()

            # Periodic version check (every VERSION_CHECK_INTERVAL_POLLS polls)
            # Only check when docked and web server is running
            if (
                self._is_docked
                and self._web_server
                and self._web_server.is_running
                and self._poll_count % VERSION_CHECK_INTERVAL_POLLS == 0
            ):
                await self._check_integration_versions()

            # Periodic backup check - only when docked and web server is running
            if self._is_docked and self._web_server and self._web_server.is_running:
                await self._check_scheduled_backup()

        except RemoteAPIError as e:
            _LOG.warning("[%s] Failed to poll power status: %s", self.log_id, e)

    async def _on_docked(self) -> None:
        """Handle remote being docked - start web server."""
        _LOG.info("[%s] Remote docked - starting web server", self.log_id)

        if self._web_server is None:
            self._web_server = WebServer(
                address=self._device_config.address,
                pin=self._device_config.pin if self._device_config.pin else None,
                api_key=self._device_config.api_key
                if self._device_config.api_key
                else None,
            )

        if not self._web_server.is_running:
            self._web_server.start()
            _LOG.info(
                "[%s] Web server started at http://%s:%d", self.log_id, self.address, WEB_SERVER_PORT
            )
            
            # Trigger initial version check on startup
            _LOG.info("[%s] Triggering initial version check...", self.log_id)
            try:
                self._web_server.refresh_integration_versions()
            except Exception as e:
                _LOG.warning("[%s] Initial version check failed: %s", self.log_id, e)

    async def _on_undocked(self) -> None:
        """Handle remote being undocked - conditionally stop web server."""
        if not self._settings.shutdown_on_battery:
            _LOG.info(
                "[%s] Remote undocked - web server remains running (shutdown_on_battery=False)",
                self.log_id,
            )
            return

        _LOG.info("[%s] Remote undocked - stopping web server", self.log_id)

        if self._web_server and self._web_server.is_running:
            self._web_server.stop()
            _LOG.info("[%s] Web server stopped", self.log_id)

    async def _check_integration_versions(self) -> None:
        """
        Check for updates to installed integrations.

        This is called periodically during polling to refresh version info.
        The web server caches this data for display in the UI.
        """
        if not self._web_server:
            return

        _LOG.info("[%s] Checking for integration updates...", self.log_id)
        try:
            # Trigger the web server to refresh version data
            # This updates the cached update availability info
            self._web_server.refresh_integration_versions()
            _LOG.debug("[%s] Integration version check complete", self.log_id)
        except Exception as e:
            _LOG.warning(
                "[%s] Failed to check integration versions: %s", self.log_id, e
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
        if not self._web_server:
            return

        try:
            # Load current settings to check if backups are enabled
            settings = Settings.load()

            if not settings.backup_configs:
                return  # Automatic backups disabled

            if not self._is_backup_time(settings.backup_time):
                return  # Not backup time yet

            _LOG.info(
                "[%s] Starting scheduled backup at %s",
                self.log_id,
                settings.backup_time,
            )

            # Perform the backup via web server (run in executor since it's sync)
            backup_result = await self._loop.run_in_executor(
                None, self._web_server.perform_scheduled_backup
            )

            # Update last backup date on success
            if backup_result:
                self._last_backup_date = datetime.now().strftime("%Y-%m-%d")
                _LOG.info("[%s] Scheduled backup completed successfully", self.log_id)
            else:
                _LOG.warning("[%s] Scheduled backup failed", self.log_id)

        except Exception as e:
            _LOG.error("[%s] Error during scheduled backup: %s", self.log_id, e)

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
