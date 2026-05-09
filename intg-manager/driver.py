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

import ucapi

from const import RemoteConfig
from data_migration import migrate
from device import IntegrationManagerDevice, _all_remote_configs
from discover import ManagerDiscovery
from log_handler import setup_log_handler
from setup import RemoteSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path

_LOG = logging.getLogger(__name__)


def _remote_id_from_ws(websocket) -> str | None:
    if not websocket or not getattr(websocket, "remote_address", None):
        return None
    host = websocket.remote_address[0]
    for cfg in _all_remote_configs:
        if cfg.address == host:
            return cfg.identifier
    return None


class IntegrationManagerDriver(BaseIntegrationDriver):
    """
    Custom driver that handles multi-remote disconnect correctly.

    In external/multi-remote mode, only the owner remote (first in config) has
    a UC API WebSocket connection to the integration. When the owner remote goes
    offline and sends a disconnect command, only that device should be disconnected.
    Other remotes have independent HTTP connections and should keep polling.
    """

    async def on_r2_connect_cmd(self, websocket=None) -> None:
        await self.api.set_device_state(ucapi.DeviceStates.CONNECTED)
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Connect command from %s", rid)
            self._loop.create_task(device.connect())
        else:
            _LOG.debug("Connect command without identifiable source - connecting all")
            for d in self._device_instances.values():
                self._loop.create_task(d.connect())

    async def on_r2_disconnect_cmd(self, websocket=None) -> None:
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Disconnect command from %s", rid)
            self._loop.create_task(device.disconnect())
        else:
            _LOG.debug("Disconnect command without identifiable source - ignoring")

    async def on_r2_enter_standby(self, websocket=None) -> None:
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Enter standby from %s", rid)
            self._loop.create_task(device.disconnect())
        else:
            _LOG.debug("Enter standby without identifiable source - ignoring")

    async def on_r2_exit_standby(self, websocket=None) -> None:
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Exit standby from %s", rid)
            self._loop.create_task(device.connect())
        else:
            _LOG.debug("Exit standby without identifiable source - reconnecting all")
            for d in self._device_instances.values():
                self._loop.create_task(d.connect())


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
    logging.getLogger("remote_api").setLevel(level)
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
