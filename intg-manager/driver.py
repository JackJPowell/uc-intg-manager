"""
Integration Manager Driver.

This is the main entry point for the integration manager. It initializes
the driver, sets up logging, and starts the integration API.

:copyright: (c) 2025.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os

import device as _device_module
from const import RemoteConfig
from data_migration import migrate
from device import IntegrationManagerDevice, _all_remote_configs
from discover import ManagerDiscovery
from log_handler import setup_log_handler
from setup import RemoteSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path

_LOG = logging.getLogger(__name__)


class IntegrationManagerDriver(BaseIntegrationDriver):
    """
    Custom driver that handles multi-remote disconnect correctly.

    In external/multi-remote mode, only the owner remote (first in config) has
    a UC API WebSocket connection to the integration. When the owner remote goes
    offline and sends a disconnect command, only that device should be disconnected.
    Other remotes have independent HTTP connections and should keep polling.
    """

    def _disconnect_owner_only(self, reason: str) -> None:
        """Disconnect only the owner device (first in config), leaving others running."""
        owner_id = _all_remote_configs[0].identifier if _all_remote_configs else None
        _LOG.debug("%s: disconnecting owner device only (%s)", reason, owner_id)
        for device_id, device in self._device_instances.items():
            if device_id == owner_id:
                self._loop.create_task(device.disconnect())
                break

    async def on_r2_connect_cmd(self) -> None:
        """Connect all devices, then immediately recheck all remote connectivity."""
        await super().on_r2_connect_cmd()
        self._loop.create_task(self._recheck_all_connectivity(delay=3))

    async def on_r2_exit_standby(self) -> None:
        """Reconnect all devices, then immediately recheck all remote connectivity."""
        await super().on_r2_exit_standby()
        self._loop.create_task(self._recheck_all_connectivity(delay=3))

    async def _recheck_all_connectivity(self, delay: float = 3) -> None:
        """Wait briefly for connections to settle, then update all remote online statuses."""
        await asyncio.sleep(delay)
        ws = _device_module._web_server_instance
        if ws and ws.is_running:
            _LOG.debug(
                "Rechecking connectivity for all remotes after connect/exit-standby"
            )
            await ws.run_on_server_loop(ws.check_all_remote_connectivity(force=True))

    async def on_r2_disconnect_cmd(self) -> None:
        """Disconnect only the owner device, not all remotes."""
        self._disconnect_owner_only("Client disconnect command")

    async def on_r2_enter_standby(self) -> None:
        """Disconnect only the owner device when it enters standby."""
        self._disconnect_owner_only("Enter standby event")


async def main():
    """Start the Integration Manager driver."""
    logging.basicConfig()

    # Set up the ring buffer log handler to capture logs for the web UI
    setup_log_handler()

    # Configure logging level from environment variable
    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("device").setLevel(level)
    logging.getLogger("setup").setLevel(level)
    logging.getLogger("web_server").setLevel(level)
    logging.getLogger("unfurled").setLevel(level)
    logging.getLogger("github_api").setLevel(level)
    logging.getLogger("integration_service").setLevel(level)
    logging.getLogger("data_migration").setLevel(level)
    logging.getLogger("backup_service").setLevel(level)

    # Force migration to v2.0 format if needed
    # This ensures all subsequent code can assume v2.0 structure
    migrate()

    # Initialize the integration driver
    # This integration doesn't expose entities - it's purely a web UI
    driver = IntegrationManagerDriver(
        device_class=IntegrationManagerDevice,
        entity_classes=[],  # No entities exposed
        driver_id="intg_manager_driver",
    )

    # Configure the device config manager
    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=RemoteConfig,
    )

    # Register all configured devices from config file
    await driver.register_all_configured_devices()

    # Set up the setup handler
    discovery = ManagerDiscovery("_uc-remote._tcp.local.", timeout=3)
    setup_handler = RemoteSetupFlow.create_handler(driver, discovery=discovery)

    # Initialize the API with the driver configuration
    await driver.api.init("driver.json", setup_handler)

    # Keep the driver running
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
