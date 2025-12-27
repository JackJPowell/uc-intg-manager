"""
Remote API Client.

This module handles communication with the Unfolded Circle Remote REST API.
It provides methods to query integrations, drivers, and system status.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import aiohttp

_LOG = logging.getLogger(__name__)


class RemoteAPIError(Exception):
    """Exception raised when Remote API calls fail."""


class RemoteAPIClient:
    """
    Client for interacting with the Unfolded Circle Remote REST API.

    Handles authentication and provides methods for querying:
    - Integration instances
    - Driver metadata
    - System power status
    """

    def __init__(
        self,
        address: str,
        pin: str | None = None,
        api_key: str | None = None,
        port: int = 80,
    ) -> None:
        """
        Initialize the Remote API client.

        :param address: IP address or hostname of the remote
        :param pin: Web configurator PIN for Basic Auth
        :param api_key: API key for Bearer token auth (preferred)
        :param port: HTTP port (default 80)
        """
        self._address = address
        self._pin = pin
        self._api_key = api_key
        self._port = port
        self._base_url = f"http://{address}:{port}/api"
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {}
            auth = None

            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            elif self._pin:
                auth = aiohttp.BasicAuth("web-configurator", self._pin)

            # Create timeout object explicitly to avoid context manager issues
            # when running from non-async context via run_coroutine_threadsafe
            timeout = aiohttp.ClientTimeout(total=30)

            self._session = aiohttp.ClientSession(
                headers=headers,
                auth=auth,
                timeout=timeout,
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Any:
        """
        Make an HTTP request to the Remote API.

        :param method: HTTP method (GET, POST, etc.)
        :param endpoint: API endpoint (e.g., /intg/instances)
        :param kwargs: Additional arguments for aiohttp request
        :return: JSON response data
        :raises RemoteAPIError: If the request fails
        """
        session = await self._get_session()
        url = f"{self._base_url}{endpoint}"

        try:
            async with session.request(method, url, **kwargs) as response:
                if response.status == 401:
                    raise RemoteAPIError("Authentication failed. Check PIN or API key.")
                if response.status == 403:
                    raise RemoteAPIError("Access forbidden. PIN may have changed.")
                if response.status >= 400:
                    text = await response.text()
                    raise RemoteAPIError(f"API error {response.status}: {text}")

                if response.content_type == "application/json":
                    return await response.json()
                return await response.text()
        except aiohttp.ClientError as e:
            raise RemoteAPIError(f"Connection error: {e}") from e

    async def get_integration_instances(self) -> list[dict[str, Any]]:
        """
        Get list of installed integration instances.

        :return: List of integration instance dictionaries
        """
        _LOG.debug("Fetching integration instances")
        return await self._request("GET", "/intg/instances")

    async def get_driver(self, driver_id: str) -> dict[str, Any]:
        """
        Get driver metadata by driver ID.

        :param driver_id: The driver identifier
        :return: Driver metadata dictionary
        """
        _LOG.debug("Fetching driver metadata for: %s", driver_id)
        return await self._request("GET", f"/intg/drivers/{driver_id}")

    async def get_all_drivers(self) -> list[dict[str, Any]]:
        """
        Get all registered drivers.

        :return: List of driver dictionaries
        """
        _LOG.debug("Fetching all drivers")
        return await self._request("GET", "/intg/drivers?limit=100")

    async def get_power_status(self) -> dict[str, Any]:
        """
        Get the current power/charger status of the remote.

        :return: Charger status dictionary with power_supply and wireless_charging flags
        """

        return await self._request("GET", "/system/power/charger")

    async def is_docked(self) -> bool:
        """
        Check if the remote is currently charging (docked or wireless).

        For R3 remotes, this checks both dock charging (power_supply) and wireless charging.
        The remote is considered "docked" if either charging method is active.

        :return: True if the remote is on dock or wireless charging
        """
        try:
            status = await self.get_power_status()
            # Check if either dock charging or wireless charging is active
            power_supply = status.get("power_supply", False)
            wireless_charging = status.get("wireless_charging", False)
            return power_supply or wireless_charging
        except RemoteAPIError as e:
            _LOG.warning("Failed to check charging status: %s", e)
            return False

    async def get_version(self) -> dict[str, Any]:
        """
        Get remote version information.

        :return: Version information dictionary
        """
        return await self._request("GET", "/pub/version")

    async def test_connection(self) -> bool:
        """
        Test if the connection to the remote is working.

        :return: True if connection successful
        """
        try:
            await self.get_version()
            return True
        except RemoteAPIError:
            return False

    async def get_device_name(self) -> str | None:
        """
        Get the remote device name.

        :return: Device name or None if request failed
        """
        try:
            device_info = await self._request("GET", "/cfg/device")
            return device_info.get("name")
        except RemoteAPIError as e:
            _LOG.error("Failed to get device name: %s", e)
            return None

    async def get_wifi_info(self) -> dict[str, Any] | None:
        """
        Get the remote's WiFi information including IP address.

        Requires authentication (PIN or API key).

        :return: WiFi info dictionary with ip_address field, or None if request failed
        """
        try:
            wifi_info = await self._request("GET", "/system/wifi")
            return wifi_info
        except RemoteAPIError as e:
            _LOG.error("Failed to get WiFi info: %s", e)
            return None

    async def create_api_key(self, name: str = "intg-manager") -> str | None:
        """
        Create an API key for persistent authentication.

        If a key with the same name already exists, it will be deleted first
        to ensure we always have a fresh key.

        :param name: Name for the API key
        :return: API key string or None if creation failed
        """
        try:
            # First, check if a key with this name already exists
            existing_keys = await self._request("GET", "/auth/api_keys")

            # Find and delete any existing key with the same name
            if isinstance(existing_keys, list):
                for key_info in existing_keys:
                    if key_info.get("name") == name:
                        key_id = key_info.get("key_id")
                        if key_id:
                            _LOG.info(
                                "Found existing API key '%s', deleting it first", name
                            )
                            try:
                                await self._request(
                                    "DELETE", f"/auth/api_keys/{key_id}"
                                )
                                _LOG.debug(
                                    "Successfully deleted existing API key with id: %s",
                                    key_id,
                                )
                            except RemoteAPIError as delete_error:
                                _LOG.warning(
                                    "Failed to delete existing API key: %s",
                                    delete_error,
                                )
                                # Continue anyway and try to create the new key

            # Now create the new API key
            response = await self._request(
                "POST",
                "/auth/api_keys",
                json={
                    "name": name,
                    "scopes": ["admin"],
                },
            )
            _LOG.info("Successfully created API key '%s'", name)
            return response.get("api_key")
        except RemoteAPIError as e:
            _LOG.error("Failed to create API key: %s", e)
            return None
