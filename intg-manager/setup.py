"""
Setup Flow Module.

This module handles the remote setup and configuration process.
It provides forms for entering the remote IP and PIN.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
from typing import Any

from const import MANAGER_DATA_FILE, RemoteConfig
from remote_api import RemoteAPIClient, RemoteAPIError
from ucapi import (
    IntegrationSetupError,
    RequestUserInput,
    SetupComplete,
    SetupError,
    UserDataResponse,
)
from ucapi_framework import BaseSetupFlow

_LOG = logging.getLogger(__name__)


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
        return RequestUserInput(
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
            return self.get_manual_entry_form()  # Re-display form with warning

        if not pin:
            _LOG.warning("PIN is required, re-displaying form")
            return self.get_manual_entry_form()  # Re-display form with warning

        _LOG.debug("Attempting to connect to remote at %s", address)

        try:
            # Test the connection with authentication
            client = RemoteAPIClient(address, pin=pin)

            # First, test if we can connect at all
            if not await client.test_connection():
                _LOG.error("Connection test failed for %s", address)
                await client.close()
                return SetupError(IntegrationSetupError.CONNECTION_REFUSED)

            try:
                # Get version info to validate PIN - this requires authentication
                # If the PIN is wrong, this will raise RemoteAPIError
                version_info = await client.get_version()

                # If we got here, PIN is valid
                _LOG.info(
                    "Connected to remote: %s (firmware %s)",
                    version_info.get("device_name", "Unknown"),
                    version_info.get("os", "Unknown"),
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
                _LOG.error("Failed to retrieve remote details (invalid PIN?): %s", e)
                await client.close()
                # If authentication failed, re-display form for user to correct PIN
                if (
                    "401" in str(e)
                    or "Unauthorized" in str(e)
                    or "authentication" in str(e).lower()
                ):
                    _LOG.warning("Authentication failed - invalid PIN")
                    return self.get_manual_entry_form()
                return SetupError(IntegrationSetupError.CONNECTION_REFUSED)
            except Exception as e:
                _LOG.error("Unexpected error during remote connection: %s", e)
                await client.close()
                return SetupError(IntegrationSetupError.OTHER)
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

    async def _handle_restore_response(
        self, msg: UserDataResponse
    ) -> SetupComplete | SetupError | RequestUserInput:
        """
        Extended restore handler that also accepts ``manager_data``.

        When called by the bootstrapper during a self-update the
        ``send_setup_input`` payload contains two fields:

        - ``restore_data``  — serialised ``config.json`` (list of remote configs);
          processed by the base implementation to reconnect IM to its remotes.
        - ``manager_data``  — serialised ``manager.json`` (integration settings &
          backups); written to ``MANAGER_DATA_FILE`` so that all previous
          integration configurations survive the upgrade.

        Both are optional from the bootstrapper's perspective, but
        ``restore_data`` is required by the base ``_handle_restore_response``
        to succeed.  If ``manager_data`` is absent or empty the method falls
        back to the base behaviour (normal user-driven restore with no
        manager-data handoff).
        """
        manager_data = msg.input_values.get("manager_data", "").strip()

        if manager_data:
            _LOG.info(
                "Setup restore: received manager_data (%d bytes) — will write to %s",
                len(manager_data),
                MANAGER_DATA_FILE,
            )
            try:
                # Validate it is parseable JSON before the base restore commits
                json.loads(manager_data)
            except json.JSONDecodeError as exc:
                _LOG.error(
                    "Setup restore: manager_data is not valid JSON (%s) — ignoring",
                    exc,
                )
                manager_data = ""

        # Delegate config.json restore to the base class
        result = await super()._handle_restore_response(msg)

        # Only persist manager.json if the base restore succeeded
        if manager_data and isinstance(result, SetupComplete):
            try:
                os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)
                with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as fh:
                    fh.write(manager_data)
                _LOG.info(
                    "Setup restore: manager.json written (%d bytes)",
                    len(manager_data),
                )
            except OSError as exc:
                # Log but don't fail — the remote connection is restored; the user
                # can re-import integration data from a backup if needed.
                _LOG.error("Setup restore: could not write manager.json: %s", exc)

        return result
