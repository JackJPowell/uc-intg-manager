"""
Setup Flow Module.

This module handles the remote setup and configuration process.
It provides forms for entering the remote IP and PIN.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from const import RemoteConfig
from remote_api import RemoteAPIClient, RemoteAPIError
from ucapi import IntegrationSetupError, RequestUserInput, SetupError
from ucapi_framework import BaseSetupFlow

_LOG = logging.getLogger(__name__)

# Setup form for entering remote connection details
_REMOTE_INPUT_SCHEMA = RequestUserInput(
    {"en": "Remote Connection Setup"},
    [
        {
            "id": "info",
            "label": {
                "en": "Connect to your Remote",
            },
            "field": {
                "label": {
                    "value": {
                        "en": (
                            "Enter the IP address and web configurator PIN for your "
                            "Unfolded Circle Remote. The PIN can be found in the "
                            "Remote UI under Profile settings."
                        ),
                    }
                }
            },
        },
        {
            "field": {"text": {"value": ""}},
            "id": "address",
            "label": {
                "en": "IP Address",
            },
        },
        {
            "field": {"password": {"value": ""}},
            "id": "pin",
            "label": {
                "en": "Web Configurator PIN",
            },
        },
    ],
)


class RemoteSetupFlow(BaseSetupFlow[RemoteConfig]):
    """
    Setup flow for remote connection.

    Handles remote configuration through manual entry.
    """

    def get_manual_entry_form(self) -> RequestUserInput:
        """
        Return the manual entry form for remote setup.

        :return: RequestUserInput with form fields for remote configuration
        """
        return _REMOTE_INPUT_SCHEMA

    def get_additional_discovery_fields(self) -> list[dict]:
        """
        Return additional fields for discovery-based setup.

        :return: List of dictionaries defining additional fields
        """
        return [
            {
                "field": {"password": {"value": ""}},
                "id": "pin",
                "label": {
                    "en": "Web Configurator PIN",
                },
            },
        ]

    async def query_device(
        self, input_values: dict[str, Any]
    ) -> RemoteConfig | SetupError | RequestUserInput:
        """
        Create remote configuration from user input.

        This method is called after the user submits the setup form.
        It validates the input and attempts to connect to the remote.

        :param input_values: Dictionary of user input from the form
        :return: RemoteConfig on success, SetupError on failure
        """
        # Extract form values
        address = input_values.get("address", "").strip()
        pin = input_values.get("pin", "").strip()

        # Validate required fields
        if not address:
            _LOG.warning("Address is required, re-displaying form")
            return _REMOTE_INPUT_SCHEMA

        if not pin:
            _LOG.warning("PIN is required, re-displaying form")
            return _REMOTE_INPUT_SCHEMA

        _LOG.debug("Attempting to connect to remote at %s", address)

        try:
            # Test the connection
            client = RemoteAPIClient(address, pin=pin)
            try:
                # Try to get version info to validate connection
                version_info = await client.get_version()
                _LOG.info(
                    "Connected to remote: %s (firmware %s)",
                    version_info.get("device_name", "Unknown"),
                    version_info.get("version", "Unknown"),
                )
                name: str | None = version_info.get("device_name", None)
                if name is None:
                    name: str = await client.get_device_name() or version_info.get(
                        "model", "UCR Remote"
                    )

                # Try to create an API key for better authentication
                api_key = await client.create_api_key("intg-manager")
                if api_key:
                    _LOG.info("Created API key for persistent authentication")
                else:
                    _LOG.info("Using PIN-based authentication")

                # Get actual IP if user provided loopback address
                # Check for common localhost/loopback values
                is_localhost = (
                    address.startswith("127.") or address.lower() == "localhost"
                )

                actual_address = address  # Default to user-provided address
                if is_localhost:
                    try:
                        wifi_info = await client.get_wifi_info()
                        if wifi_info and isinstance(wifi_info, dict):
                            ip_address = wifi_info.get("ip_address")
                            if ip_address and not ip_address.startswith("127."):
                                actual_address = ip_address
                                _LOG.info(
                                    "Detected loopback address, using actual IP from WiFi: %s",
                                    actual_address,
                                )
                    except RemoteAPIError:
                        _LOG.debug(
                            "Could not retrieve WiFi info, keeping provided address"
                        )

            except RemoteAPIError as e:
                _LOG.error("Failed to connect to remote: %s", e)
                await client.close()
                return SetupError(IntegrationSetupError.CONNECTION_REFUSED)
            finally:
                await client.close()

            # Generate identifier from address
            identifier = version_info.get("address", "").replace(":", "_")

            return RemoteConfig(
                identifier=identifier,
                name=name,
                address=actual_address,  # Use the actual IP from network info
                pin=pin,
                api_key=api_key or "",
            )

        except ConnectionError as ex:
            _LOG.error("Connection refused to %s: %s", address, ex)
            return SetupError(IntegrationSetupError.CONNECTION_REFUSED)

        except TimeoutError as ex:
            _LOG.error("Connection timeout to %s: %s", address, ex)
            return SetupError(IntegrationSetupError.TIMEOUT)

        except Exception as ex:
            _LOG.error("Failed to connect to %s: %s", address, ex)
            return SetupError(IntegrationSetupError.CONNECTION_REFUSED)
