"""
Device Communication Module.

This module handles all communication with your device. It manages connections,
sends commands, and tracks device state.

TODO: Implement the actual device communication logic for your specific device.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from asyncio import AbstractEventLoop
from enum import StrEnum
from typing import Any

from const import DeviceConfig
from ucapi import EntityTypes
from ucapi.media_player import Attributes as MediaAttr
from ucapi_framework import BaseConfigManager, StatelessHTTPDevice, create_entity_id
from ucapi_framework.device import DeviceEvents

_LOG = logging.getLogger(__name__)


class PowerState(StrEnum):
    """
    Power state enumeration for the device.

    Adjust these states based on what your device supports.
    """

    OFF = "OFF"
    ON = "ON"
    STANDBY = "STANDBY"
    UNKNOWN = "UNKNOWN"


class Device(StatelessHTTPDevice):
    """
    Device class representing your physical device.

    This class handles all communication with the device and maintains
    its current state. Extend this class with methods specific to your device.

    The base class StatelessHTTPDevice is suitable for devices that don't
    maintain a persistent connection. If your device uses a persistent
    connection (like TCP sockets or WebSockets), consider using a different
    base class or implementing your own connection management.
    """

    def __init__(
        self,
        device_config: DeviceConfig,
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
            device_config=device_config, loop=loop, config_manager=config_manager
        )

        # TODO: Initialize your device client/connection here
        # Example:
        # self._client = YourDeviceClient(
        #     host=device_config.address,
        #     port=device_config.port,
        # )

        # Initialize device state tracking
        self._power_state: PowerState = PowerState.UNKNOWN

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
    def state(self) -> PowerState:
        """Return the current power state."""
        return self._power_state

    @property
    def log_id(self) -> str:
        """Return a log identifier for debugging."""
        return self.name if self.name else self.identifier

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def connect(self) -> None:
        """
        Establish connection to the device.

        TODO: Implement your device's connection logic here.
        This might involve opening a TCP connection, authenticating, etc.
        """
        _LOG.debug("[%s] Connecting to device at %s", self.log_id, self.address)
        # TODO: Implement connection logic
        # await self._client.connect()

    async def disconnect(self) -> None:
        """
        Disconnect from the device.

        TODO: Implement your device's disconnection logic here.
        """
        _LOG.debug("[%s] Disconnecting from device", self.log_id)
        # TODO: Implement disconnection logic
        # await self._client.disconnect()

    async def verify_connection(self) -> None:
        """
        Verify connection to the device and emit current state.

        This method is called by the framework to check device connectivity
        and retrieve the current state. State updates are emitted via DeviceEvents.UPDATE.

        :raises: Exception if connection verification fails
        """
        _LOG.debug(
            "[%s] Verifying connection to device at %s", self.log_id, self.address
        )

        try:
            # TODO: Implement connection verification
            # This should:
            # 1. Connect to the device
            # 2. Query the current state
            # 3. Update internal state tracking
            # 4. Emit state update event

            # Example implementation:
            # await self.connect()
            # state = await self._client.get_power_state()
            # self._power_state = PowerState(state)

            _LOG.debug(
                "[%s] Connection verified, state: %s", self.log_id, self._power_state
            )

            # Emit state update to the Remote
            self._emit_state_update()

        except Exception as err:
            _LOG.error("[%s] Connection verification failed: %s", self.log_id, err)
            raise

    # =========================================================================
    # Power Control
    # =========================================================================

    async def power_on(self) -> None:
        """
        Turn on the device.

        TODO: Implement power on command for your device.
        """
        _LOG.debug("[%s] Powering on", self.log_id)
        # TODO: Send power on command
        # await self._client.power_on()

        self._power_state = PowerState.ON
        self._emit_state_update()

    async def power_off(self) -> None:
        """
        Turn off the device.

        TODO: Implement power off command for your device.
        """
        _LOG.debug("[%s] Powering off", self.log_id)
        # TODO: Send power off command
        # await self._client.power_off()

        self._power_state = PowerState.OFF
        self._emit_state_update()

    async def power_toggle(self) -> None:
        """
        Toggle the device power state.

        TODO: Implement power toggle or use power_on/power_off based on state.
        """
        _LOG.debug("[%s] Toggling power", self.log_id)

        if self._power_state == PowerState.ON:
            await self.power_off()
        else:
            await self.power_on()

    # =========================================================================
    # Command Sending
    # =========================================================================

    async def send_command(self, command: str, *args: Any, **kwargs: Any) -> None:
        """
        Send a command to the device.

        This is a generic command method that can be used for various operations.

        :param command: Command to send
        :param args: Positional arguments
        :param kwargs: Keyword arguments
        """
        _LOG.debug("[%s] Sending command: %s", self.log_id, command)
        update: dict[MediaAttr, Any] = {}

        # TODO: Implement command routing
        # Example:
        # match command:
        #     case "volume_up":
        #         await self._client.volume_up()
        #         update = {MediaAttr.VOLUME: self._volume}
        #     case "volume_down":
        #         await self._client.volume_down()
        #         update = {MediaAttr.VOLUME: self._volume}
        #     case _:
        #         _LOG.warning("Unknown command: %s", command)

        self.events.emit(
            DeviceEvents.UPDATE,
            create_entity_id(EntityTypes.MEDIA_PLAYER, self.identifier),
            update,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _emit_state_update(self) -> None:
        """Emit current state to the Remote."""
        attributes = {
            MediaAttr.STATE: self._power_state,
            # TODO: Add additional attributes your device tracks
            # MediaAttr.VOLUME: self._volume,
            # MediaAttr.MUTED: self._muted,
            # MediaAttr.SOURCE: self._source,
        }
        self.events.emit(
            DeviceEvents.UPDATE,
            create_entity_id(EntityTypes.MEDIA_PLAYER, self.identifier),
            attributes,
        )
