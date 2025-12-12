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

from const import RemoteConfig
from device import IntegrationManagerDevice
from discover import ManagerDiscovery
from log_handler import setup_log_handler
from setup import RemoteSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path


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

    # Initialize the integration driver
    # This integration doesn't expose entities - it's purely a web UI
    driver = BaseIntegrationDriver(
        device_class=IntegrationManagerDevice,
        entity_classes=[],  # No entities exposed
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
    discovery = ManagerDiscovery("_uc-remote._tcp.local.", timeout=2)
    setup_handler = RemoteSetupFlow.create_handler(driver, discovery=discovery)

    # Initialize the API with the driver configuration
    await driver.api.init("driver.json", setup_handler)

    # Keep the driver running
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
