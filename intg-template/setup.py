"""
Setup Flow Module.

This module handles the device setup and configuration process. It provides
forms for manual device entry and validation of device connections.

TODO: Customize the setup form fields and validation for your device.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from const import DeviceConfig
from ucapi import IntegrationSetupError, RequestUserInput, SetupError
from ucapi_framework import BaseSetupFlow

_LOG = logging.getLogger(__name__)

# TODO: Customize this form for your device's setup requirements
# This form is displayed when the user chooses manual device entry
_MANUAL_INPUT_SCHEMA = RequestUserInput(
    {"en": "Device Setup"},  # TODO: Update title
    [
        {
            "id": "info",
            "label": {
                "en": "Setup your Device",  # TODO: Update label
            },
            "field": {
                "label": {
                    "value": {
                        "en": (
                            "Please enter the connection details for your device."
                            # TODO: Update help text
                        ),
                    }
                }
            },
        },
        {
            "field": {"text": {"value": ""}},
            "id": "name",
            "label": {
                "en": "Device Name",
            },
        },
        {
            "field": {"text": {"value": ""}},
            "id": "address",
            "label": {
                "en": "IP Address or Hostname",
            },
        },
        # TODO: Add additional fields your device needs
        # {
        #     "field": {"number": {"value": 8080, "min": 1, "max": 65535}},
        #     "id": "port",
        #     "label": {
        #         "en": "Port",
        #     },
        # },
        # {
        #     "field": {"text": {"value": ""}},
        #     "id": "username",
        #     "label": {
        #         "en": "Username",
        #     },
        # },
        # {
        #     "field": {"password": {"value": ""}},
        #     "id": "password",
        #     "label": {
        #         "en": "Password",
        #     },
        # },
    ],
)


class DeviceSetupFlow(BaseSetupFlow[DeviceConfig]):
    """
    Setup flow for device integration.

    Handles device configuration through discovery or manual entry.
    Extend this class to add custom setup logic for your device.
    """

    def get_manual_entry_form(self) -> RequestUserInput:
        """
        Return the manual entry form for device setup.

        Override this method to return a custom form for your device.

        :return: RequestUserInput with form fields for manual configuration
        """
        return _MANUAL_INPUT_SCHEMA

    async def query_device(
        self, input_values: dict[str, Any]
    ) -> DeviceConfig | SetupError | RequestUserInput:
        """
        Create device configuration from user input.

        This method is called after the user submits the setup form.
        It should validate the input, attempt to connect to the device,
        and return a DeviceConfig if successful.

        :param input_values: Dictionary of user input from the form
        :return: DeviceConfig on success, SetupError on failure, or
                 RequestUserInput to re-display the form
        """
        # Extract form values
        name = input_values.get("name", "").strip()
        address = input_values.get("address", "").strip()
        # TODO: Extract additional fields
        # port = input_values.get("port", 8080)
        # username = input_values.get("username", "").strip()
        # password = input_values.get("password", "").strip()

        # Validate required fields
        if not address:
            _LOG.warning("Address is required, re-displaying form")
            return _MANUAL_INPUT_SCHEMA

        # Use a default name if not provided
        if not name:
            name = f"Device ({address})"

        _LOG.debug("Attempting to connect to device at %s", address)

        try:
            # TODO: Implement device connection validation
            # This should:
            # 1. Attempt to connect to the device
            # 2. Verify it's the expected device type
            # 3. Retrieve device info (identifier, model, etc.)
            #
            # Example:
            # client = YourDeviceClient(address, port)
            # try:
            #     await client.connect()
            #     info = await client.get_device_info()
            # finally:
            #     await client.disconnect()
            #
            # identifier = info.get("serial", info.get("mac", address))

            # Placeholder: Generate identifier from address
            # TODO: Replace with actual device identifier retrieval
            identifier = address.replace(".", "_").replace(":", "_")

            return DeviceConfig(
                identifier=identifier,
                name=name,
                address=address,
                # TODO: Add additional config fields
                # port=port,
                # username=username,
                # password=password,
            )

        except ConnectionError as ex:
            _LOG.error("Connection refused to %s: %s", address, ex)
            return SetupError(IntegrationSetupError.CONNECTION_REFUSED)

        except TimeoutError as ex:
            _LOG.error("Connection timeout to %s: %s", address, ex)
            return SetupError(IntegrationSetupError.TIMEOUT)

        except Exception as ex:
            _LOG.error("Failed to connect to %s: %s", address, ex)
            _LOG.info("Please verify the device address and try again")
            return SetupError(IntegrationSetupError.CONNECTION_REFUSED)
