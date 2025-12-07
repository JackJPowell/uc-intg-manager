"""
Media Player Entity.

This module defines the media player entity for your device. The media player
entity handles playback controls, source selection, and volume management.

TODO: Customize this for your specific device's capabilities.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import ucapi
from ucapi import MediaPlayer, media_player, EntityTypes
from ucapi.media_player import DeviceClasses, Attributes

import device
from const import DeviceConfig, SimpleCommands
from ucapi_framework import create_entity_id

_LOG = logging.getLogger(__name__)

# TODO: Define the features your device supports
# Available features:
#   ON_OFF, TOGGLE, VOLUME, VOLUME_UP_DOWN, MUTE, MUTE_TOGGLE,
#   PLAY_PAUSE, STOP, NEXT, PREVIOUS, FAST_FORWARD, REWIND,
#   REPEAT, SHUFFLE, SEEK, MEDIA_DURATION, MEDIA_POSITION,
#   MEDIA_TITLE, MEDIA_ARTIST, MEDIA_ALBUM, MEDIA_IMAGE_URL,
#   MEDIA_TYPE, DPAD, NUMPAD, HOME, MENU, CONTEXT_MENU,
#   GUIDE, INFO, COLOR_BUTTONS, CHANNEL_SWITCHER, SELECT_SOURCE,
#   SELECT_SOUND_MODE, EJECT, OPEN_CLOSE, AUDIO_TRACK,
#   SUBTITLE, RECORD, SETTINGS
FEATURES = [
    media_player.Features.ON_OFF,
    media_player.Features.TOGGLE,
    # TODO: Add features your device supports
    # media_player.Features.VOLUME,
    # media_player.Features.VOLUME_UP_DOWN,
    # media_player.Features.MUTE,
    # media_player.Features.DPAD,
    # media_player.Features.SELECT_SOURCE,
    # media_player.Features.MENU,
]


class DeviceMediaPlayer(MediaPlayer):
    """
    Media Player entity for your device.

    This class handles all media player commands and maintains the entity state.
    """

    def __init__(self, config_device: DeviceConfig, device_instance: device.Device):
        """
        Initialize the media player entity.

        :param config_device: Device configuration from setup
        :param device_instance: The device instance to control
        """
        self._device = device_instance
        entity_id = create_entity_id(EntityTypes.MEDIA_PLAYER, config_device.identifier)

        _LOG.debug("Initializing media player entity: %s", entity_id)

        # TODO: Update device_class to match your device type
        # Available device classes: RECEIVER, SET_TOP_BOX, SPEAKER, STREAMING_BOX, TV
        super().__init__(
            entity_id,
            config_device.name,
            FEATURES,
            attributes={
                Attributes.STATE: device_instance.state,
                # TODO: Add additional attributes your device supports
                # Attributes.VOLUME: 50,
                # Attributes.MUTED: False,
                # Attributes.SOURCE: "",
                # Attributes.SOURCE_LIST: [],
            },
            device_class=DeviceClasses.RECEIVER,
            options={
                # TODO: Add simple commands if your device supports them
                # media_player.Options.SIMPLE_COMMANDS: [
                #     member.value for member in SimpleCommands
                # ]
            },
            cmd_handler=self.handle_command,
        )

    async def handle_command(
        self, entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None
    ) -> ucapi.StatusCodes:
        """
        Handle media player commands from the remote.

        This method is called by the integration API when a command is sent
        to this media player entity.

        :param entity: The media player entity receiving the command
        :param cmd_id: The command identifier
        :param params: Optional command parameters
        :return: Status code indicating success or failure
        """
        _LOG.info("Received command: %s %s", cmd_id, params if params else "")

        try:
            # TODO: Implement command handling for your device
            match cmd_id:
                case media_player.Commands.ON:
                    await self._device.power_on()

                case media_player.Commands.OFF:
                    await self._device.power_off()

                case media_player.Commands.TOGGLE:
                    await self._device.power_toggle()

                # TODO: Add more command handlers as needed
                # case media_player.Commands.VOLUME_UP:
                #     await self._device.volume_up()
                #
                # case media_player.Commands.VOLUME_DOWN:
                #     await self._device.volume_down()
                #
                # case media_player.Commands.MUTE_TOGGLE:
                #     await self._device.mute_toggle()
                #
                # case media_player.Commands.SELECT_SOURCE:
                #     source = params.get("source") if params else None
                #     await self._device.select_source(source)

                case _:
                    _LOG.warning("Unhandled command: %s", cmd_id)
                    return ucapi.StatusCodes.NOT_IMPLEMENTED

            return ucapi.StatusCodes.OK

        except Exception as ex:
            _LOG.error("Error executing command %s: %s", cmd_id, ex)
            return ucapi.StatusCodes.BAD_REQUEST
