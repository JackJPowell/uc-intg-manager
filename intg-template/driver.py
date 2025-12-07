"""
Remote Two/3 Integration Driver.

This is the main entry point for the integration driver. It initializes
the driver, sets up logging, and starts the integration API.

TODO: Replace "YourDevice" references with your actual device name.

:copyright: (c) 2025 by Your Name.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os

from const import DeviceConfig
from device import Device
from discover import DeviceDiscovery
from media_player import DeviceMediaPlayer
from setup import DeviceSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    # Configure logging level from environment variable
    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    logging.getLogger("device").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    # Initialize the integration driver
    # TODO: Add additional entity classes if your device supports them
    # Available entity types: MediaPlayer, Remote, Light, Switch, Climate, etc.
    driver = BaseIntegrationDriver(
        device_class=Device, entity_classes=[DeviceMediaPlayer]
    )

    # Configure the device config manager
    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=DeviceConfig,
    )

    # Register all configured devices from config file
    await driver.register_all_configured_devices()

    # Set up device discovery (optional - remove if not using discovery)
    # TODO: Update the discovery parameters for your device's discovery protocol
    discovery = DeviceDiscovery(timeout=1, service_type="_yourdevice._tcp.local.")
    setup_handler = DeviceSetupFlow.create_handler(driver, discovery=discovery)

    # Initialize the API with the driver configuration
    await driver.api.init("driver.json", setup_handler)

    # Keep the driver running
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
