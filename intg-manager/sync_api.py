"""
Synchronous API Clients.

This module provides synchronous HTTP clients for use in Flask routes.
Uses the `requests` library instead of aiohttp to avoid async context issues.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
import re
from typing import Any
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth
from packaging.version import Version, InvalidVersion

from const import GITHUB_API_BASE, KNOWN_INTEGRATIONS_URL

_LOG = logging.getLogger(__name__)

# Default timeout for all requests (connect, read)
REQUEST_TIMEOUT = (10, 30)


class SyncAPIError(Exception):
    """Exception raised when API calls fail."""


class SyncRemoteClient:
    """
    Synchronous client for the Unfolded Circle Remote REST API.

    For use in Flask routes where async is problematic.
    """

    def __init__(
        self,
        address: str,
        pin: str | None = None,
        api_key: str | None = None,
        port: int = 80,
    ) -> None:
        """
        Initialize the sync Remote API client.

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

        # Set up session with auth
        self._session = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"
        elif pin:
            self._session.auth = HTTPBasicAuth("web-configurator", pin)

    def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Any:
        """
        Make an HTTP request to the Remote API.

        :param method: HTTP method (GET, POST, etc.)
        :param endpoint: API endpoint (e.g., /intg/instances)
        :param kwargs: Additional arguments for requests
        :return: JSON response data
        :raises SyncAPIError: If the request fails
        """
        url = f"{self._base_url}{endpoint}"
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)

        try:
            response = self._session.request(method, url, **kwargs)

            if response.status_code == 401:
                raise SyncAPIError("Authentication failed. Check PIN or API key.")
            if response.status_code == 403:
                raise SyncAPIError("Access forbidden. PIN may have changed.")
            if response.status_code >= 400:
                raise SyncAPIError(
                    f"API error: {response.status_code} - {response.text}"
                )

            if response.text:
                return response.json()
            return None

        except requests.RequestException as e:
            raise SyncAPIError(f"Request failed: {e}") from e

    def test_connection(self) -> bool:
        """Test connectivity to the remote."""
        try:
            self._request("GET", "/pub/version")
            return True
        except SyncAPIError:
            return False

    def get_integrations(self) -> list[dict[str, Any]]:
        """Get list of installed integration instances."""
        return self._request("GET", "/intg/instances?limit=100") or []

    def get_driver(self, driver_id: str) -> dict[str, Any] | None:
        """Get driver metadata by ID."""
        try:
            return self._request("GET", f"/intg/drivers/{driver_id}")
        except SyncAPIError as e:
            _LOG.warning("Failed to get driver %s: %s", driver_id, e)
            return None

    def is_docked(self) -> bool:
        """Check if the remote is currently docked (connected to power)."""
        try:
            power = self._request("GET", "/system/power")
            if power and isinstance(power, dict):
                # power_supply is a boolean: true = on power, false = on battery
                return power.get("power_supply", False) is True
            return False
        except SyncAPIError:
            return False

    def get_drivers(self) -> list[dict[str, Any]]:
        """Get list of all integration drivers."""
        return self._request("GET", "/intg/drivers?limit=100") or []

    def get_log_services(self) -> list[dict[str, Any]]:
        """
        Get all available log services from the remote.

        Returns a list of service objects with 'service', 'active', and 'name' fields.
        Custom integrations are prefixed with 'custom-intg-' (e.g., 'custom-intg-jvc_projector_driver').

        :return: List of log service dictionaries
        """
        return self._request("GET", "/system/logs/services") or []

    def get_logs(
        self,
        priority: int | None = None,
        service: str | None = None,
        limit: int = 1000,
        as_text: bool = False,
    ) -> list[dict[str, Any]] | str:
        """
        Get log entries from the remote.

        :param priority: Minimum priority of log message (0-7, where 0 is highest)
        :param service: Service ID to filter logs (e.g., 'custom-intg-jvc_projector_driver')
        :param limit: Maximum number of log entries to retrieve (max 10,000, default 1000)
        :param as_text: If True, return logs as text export; if False, return as JSON objects
        :return: List of log dictionaries or text string depending on as_text parameter

        Notes:
        - Log entries are retrieved in reverse order (newest first)
        - Maximum 10,000 entries
        - Not all services use priority logging (many log everything at priority 6/info)
        - Text format: tab-separated fields, message may contain tabs and line breaks
        """
        params = {}
        if priority is not None:
            params["p"] = priority
        if service is not None:
            params["s"] = service
        if limit is not None:
            params["limit"] = min(limit, 10000)  # Enforce max limit

        # Set content type header based on desired format
        headers = {}
        if as_text:
            headers["Content-Type"] = "text/plain"
        else:
            headers["Content-Type"] = "application/json"

        _LOG.debug(
            "Fetching logs: priority=%s, service=%s, limit=%s, as_text=%s",
            priority,
            service,
            limit,
            as_text,
        )

        # For text format, we need to handle the response differently
        if as_text:
            url = f"{self._base_url}/system/logs"
            try:
                response = self._session.get(
                    url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
                )
                if response.status_code == 401:
                    raise SyncAPIError("Authentication failed")
                if response.status_code == 403:
                    raise SyncAPIError("Access forbidden")
                if response.status_code >= 400:
                    raise SyncAPIError(
                        f"Request failed: {response.status_code} - {response.text}"
                    )
                return response.text
            except requests.RequestException as e:
                raise SyncAPIError(f"Request failed: {e}") from e
        else:
            # JSON format uses standard request method
            result = self._request(
                "GET", "/system/logs", params=params, headers=headers
            )
            return result if isinstance(result, list) else []

    def delete_instance(self, instance_id: str) -> bool:
        """
        Delete an integration instance.

        :param instance_id: The instance ID to delete
        :return: True if successful
        :raises SyncAPIError: If deletion fails
        """
        try:
            self._request("DELETE", f"/intg/instances/{instance_id}")
            _LOG.info("Deleted integration instance: %s", instance_id)
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to delete instance %s: %s", instance_id, e)
            raise

    def delete_driver(self, driver_id: str) -> bool:
        """
        Delete an integration driver (and its instances).

        According to docs, deleting the driver should also delete instances.

        :param driver_id: The driver ID to delete
        :return: True if successful
        :raises SyncAPIError: If deletion fails
        """
        try:
            self._request("DELETE", f"/intg/drivers/{driver_id}")
            _LOG.info("Deleted integration driver: %s", driver_id)
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to delete driver %s: %s", driver_id, e)
            raise

    def install_integration(self, archive_data: bytes, filename: str) -> dict[str, Any]:
        """
        Install an integration from a tar.gz archive.

        :param archive_data: The raw bytes of the tar.gz archive
        :param filename: Original filename for the upload
        :return: Installation response data
        :raises SyncAPIError: If installation fails
        """
        url = f"{self._base_url}/intg/install"

        try:
            # Use application/x-gzip to match official UC software
            files = {"file": (filename, archive_data, "application/x-gzip")}
            response = self._session.post(url, files=files, timeout=(30, 120))

            if response.status_code == 401:
                raise SyncAPIError("Authentication failed. Check PIN or API key.")
            if response.status_code == 403:
                raise SyncAPIError("Access forbidden. PIN may have changed.")
            if response.status_code >= 400:
                raise SyncAPIError(
                    f"Install failed: {response.status_code} - {response.text}"
                )

            _LOG.info("Successfully installed integration from %s", filename)
            if response.text:
                return response.json()
            return {"status": "ok"}

        except requests.RequestException as e:
            raise SyncAPIError(f"Install request failed: {e}") from e

    def start_setup(self, driver_id: str, reconfigure: bool = True) -> dict[str, Any]:
        """
        Start the integration setup flow.

        POST /intg/setup with driver_id and reconfigure=true to begin configuration.

        :param driver_id: The driver ID to configure
        :param reconfigure: Whether this is a reconfiguration (default True)
        :return: Confirmation response with driver_id, reconfigure, and state
        :raises SyncAPIError: If setup fails
        """
        payload = {
            "driver_id": driver_id,
            "reconfigure": reconfigure,
            "setup_data": {},
        }
        return self._request("POST", "/intg/setup", json=payload)

    def get_setup(self, driver_id: str) -> dict[str, Any]:
        """
        Get the current setup page for an integration.

        GET /intg/setup/{driver_id} to retrieve the setup form/choices.

        :param driver_id: The driver ID being configured
        :return: Setup response with require_user_action fields
        :raises SyncAPIError: If request fails
        """
        return self._request("GET", f"/intg/setup/{driver_id}")

    def send_setup_input(
        self, driver_id: str, input_values: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Send input values during the setup flow.

        PUT /intg/setup/{driver_id} with input_values.

        :param driver_id: The driver ID being configured
        :param input_values: Dictionary of field IDs to values
        :return: Next setup step response
        :raises SyncAPIError: If request fails
        """
        payload = {"input_values": input_values}
        return self._request("PUT", f"/intg/setup/{driver_id}", json=payload)

    def complete_setup(self, driver_id: str) -> bool:
        """
        Complete and clean up an integration setup flow.

        DELETE /intg/setup/{driver_id} to finish the setup process.

        :param driver_id: The driver ID to complete setup for
        :return: True if successful
        """
        try:
            self._request("DELETE", f"/intg/setup/{driver_id}")
            return True
        except SyncAPIError as e:
            _LOG.warning("Failed to complete setup for %s: %s", driver_id, e)
            return False

    def get_enabled_integrations(self) -> list[dict[str, Any]]:
        """Get enabled integrations (for post-install verification)."""
        try:
            return self._request("GET", "/intg?enabled=true&limit=50&page=1") or []
        except SyncAPIError:
            return []

    def get_instantiable_drivers(self) -> list[dict[str, Any]]:
        """Get instantiable and enabled drivers (for post-install verification)."""
        try:
            return (
                self._request(
                    "GET",
                    "/intg/drivers?instantiable=true&enabled=true&limit=50&page=1",
                )
                or []
            )
        except SyncAPIError:
            return []

    def get_custom_drivers_without_instances(self) -> list[dict[str, Any]]:
        """Get custom drivers without instances (for post-install verification)."""
        try:
            return (
                self._request(
                    "GET",
                    "/intg/drivers?driver_type=CUSTOM&has_instances=false&enabled=true&limit=50&page=1",
                )
                or []
            )
        except SyncAPIError:
            return []

    def get_enabled_instances(self) -> list[dict[str, Any]]:
        """Get enabled integration instances (for post-restore verification)."""
        try:
            return (
                self._request("GET", "/intg/instances?enabled=true&limit=50&page=1")
                or []
            )
        except SyncAPIError:
            return []

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        """Get a single integration instance by ID."""
        return self._request("GET", f"/intg/instances/{instance_id}")

    def get_instance_entities(
        self, instance_id: str, filter_type: str = "NEW"
    ) -> list[dict[str, Any]]:
        """
        Get entities for an integration instance.

        :param instance_id: The integration instance ID
        :param filter_type: Entity filter type (NEW, CONFIGURED, etc.)
        :return: List of entity dictionaries
        """
        try:
            return (
                self._request(
                    "GET",
                    f"/intg/instances/{instance_id}/entities?reload=true&filter={filter_type}&limit=100&page=1",
                )
                or []
            )
        except SyncAPIError:
            return []

    def get_configured_entities(self, instance_id: str) -> list[dict[str, Any]]:
        """
        Get configured entities for an integration instance.

        :param instance_id: The integration instance ID
        :return: List of configured entity dictionaries
        """
        return self.get_instance_entities(instance_id, filter_type="CONFIGURED")

    def register_entities(
        self, integration_id: str, entity_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Register entities for an integration.

        If entity_ids is None, registers all available entities.
        If entity_ids is provided, registers only the specified entities.

        :param integration_id: The integration instance ID
        :param entity_ids: Optional list of entity IDs to register (e.g., ["entity1", "entity2"])
        :return: Response dictionary from the API
        :raises SyncAPIError: If the request fails
        """
        endpoint = f"/intg/instances/{integration_id}/entities"

        _LOG.debug("List of Entities to register: %s", entity_ids)

        if entity_ids:
            _LOG.debug(
                "Registering %d specific entities for integration: %s",
                len(entity_ids),
                integration_id,
            )
            return self._request("POST", endpoint, json=entity_ids)

        _LOG.debug("Registering all entities for integration: %s", integration_id)
        return self._request("POST", endpoint)

    def register_entity(self, integration_id: str, entity_id: str) -> dict[str, Any]:
        """
        Register a specific entity for an integration.

        :param integration_id: The integration instance ID
        :param entity_id: The entity ID to register
        :return: Response dictionary from the API
        :raises SyncAPIError: If the request fails
        """
        _LOG.info(
            "Registering entity %s for integration: %s", entity_id, integration_id
        )
        return self._request(
            "POST", f"/intg/instances/{integration_id}/entities/{entity_id}"
        )

    def delete_all_entities(self, integration_id: str) -> dict[str, Any]:
        """
        Delete all entities for an integration.

        :param integration_id: The integration instance ID
        :return: Response dictionary from the API
        :raises SyncAPIError: If the request fails
        """
        _LOG.info("Deleting all entities for integration: %s", integration_id)
        return self._request(
            "DELETE", "/entities", json={"integration_id": integration_id}
        )

    def delete_entity(self, integration_id: str, entity_id: str) -> dict[str, Any]:
        """
        Delete a specific entity for an integration.

        :param integration_id: The integration instance ID (e.g., "psn_driver.main")
        :param entity_id: The partial entity ID without integration prefix (e.g., "media_player.device1")
        :return: Response dictionary from the API
        :raises SyncAPIError: If the request fails
        """
        # Build full entity_id: integration_id + "." + entity_id
        full_entity_id = f"{integration_id}.{entity_id}"
        _LOG.info(
            "Deleting entity %s for integration: %s", full_entity_id, integration_id
        )
        return self._request("DELETE", f"/entities/{full_entity_id}")


class SyncGitHubClient:
    """
    Synchronous client for the GitHub API.

    For use in Flask routes.
    """

    def __init__(self) -> None:
        """Initialize the GitHub client."""
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "uc-intg-manager",
            }
        )

    @staticmethod
    def parse_github_url(home_page: str) -> tuple[str, str] | None:
        """Parse a GitHub URL to extract owner and repo."""
        patterns = [
            r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$",
            r"github\.com/([^/]+)/([^/]+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, home_page)
            if match:
                return match.group(1), match.group(2).rstrip("/")
        return None

    def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Get the latest release for a repository."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"

        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)

            # Check for rate limiting
            rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
            if response.status_code == 403 and rate_limit_remaining == "0":
                rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                # Convert Unix timestamp to readable time

                reset_time = (
                    datetime.fromtimestamp(int(rate_limit_reset))
                    if rate_limit_reset
                    else None
                )
                reset_str = (
                    reset_time.strftime("%Y-%m-%d %H:%M:%S")
                    if reset_time
                    else "unknown"
                )
                _LOG.warning(
                    "GitHub API rate limit exceeded for %s/%s. Reset at: %s (in %d seconds)",
                    owner,
                    repo,
                    reset_str,
                    int(rate_limit_reset) - int(datetime.now().timestamp())
                    if rate_limit_reset
                    else 0,
                )
                return None

            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                # Try tags if no releases
                return self._get_latest_tag(owner, repo)
            return None
        except requests.RequestException as e:
            _LOG.warning("Failed to get release for %s/%s: %s", owner, repo, e)
            return None

    def get_releases(
        self, owner: str, repo: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get multiple releases for a repository.

        :param owner: Repository owner
        :param repo: Repository name
        :param limit: Maximum number of releases to return (default 10)
        :return: List of release data dictionaries
        """
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases"
        params = {"per_page": limit}

        try:
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT)

            # Check for rate limiting
            rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
            if response.status_code == 403 and rate_limit_remaining == "0":
                _LOG.warning(
                    "GitHub API rate limit exceeded for %s/%s releases", owner, repo
                )
                return []

            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                _LOG.debug("No releases found for %s/%s", owner, repo)
                return []
            return []
        except requests.RequestException as e:
            _LOG.warning("Failed to get releases for %s/%s: %s", owner, repo, e)
            return []

    def get_release_by_tag(
        self, owner: str, repo: str, tag: str
    ) -> dict[str, Any] | None:
        """
        Get a specific release by tag name.

        :param owner: Repository owner
        :param repo: Repository name
        :param tag: Release tag (e.g., 'v1.0.0' or '1.0.0')
        :return: Release data or None if not found
        """
        # GitHub API expects the tag as-is (with or without 'v' prefix)
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"

        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                _LOG.debug("Release not found for %s/%s tag %s", owner, repo, tag)
                return None
            return None
        except requests.RequestException as e:
            _LOG.warning(
                "Failed to get release for %s/%s tag %s: %s", owner, repo, tag, e
            )
            return None

    def download_release_asset(
        self,
        owner: str,
        repo: str,
        asset_pattern: str = ".tar.gz",
        version: str | None = None,
    ) -> tuple[bytes, str] | None:
        """
        Download a release asset (tar.gz file) from a release.

        :param owner: GitHub repository owner
        :param repo: GitHub repository name
        :param asset_pattern: Pattern to match asset filename (default .tar.gz)
        :param version: Specific version tag to download (e.g., 'v1.0.0'). If None, downloads latest.
        :return: Tuple of (file bytes, filename) or None if not found
        """
        # Get the appropriate release
        if version:
            release = self.get_release_by_tag(owner, repo, version)
            if not release:
                _LOG.warning(
                    "No release found for %s/%s version %s", owner, repo, version
                )
                return None
        else:
            release = self.get_latest_release(owner, repo)
            if not release:
                _LOG.warning("No release found for %s/%s", owner, repo)
                return None

        assets = release.get("assets", [])
        if not assets:
            _LOG.warning("No assets in release for %s/%s", owner, repo)
            return None

        # Find the tar.gz asset
        target_asset = None
        for asset in assets:
            name = asset.get("name", "")
            if asset_pattern in name:
                target_asset = asset
                break

        if not target_asset:
            _LOG.warning(
                "No %s asset found in release for %s/%s", asset_pattern, owner, repo
            )
            return None

        download_url = target_asset.get("browser_download_url")
        if not download_url:
            _LOG.warning("No download URL for asset in %s/%s", owner, repo)
            return None

        try:
            _LOG.info("Downloading %s from %s/%s", target_asset["name"], owner, repo)
            response = self._session.get(
                download_url,
                timeout=(30, 300),  # 30s connect, 5min read for large files
                headers={"Accept": "application/octet-stream"},
            )
            if response.status_code == 200:
                return response.content, target_asset["name"]
            _LOG.error(
                "Failed to download asset: %s - %s",
                response.status_code,
                response.text[:200],
            )
            return None
        except requests.RequestException as e:
            _LOG.error("Failed to download release asset: %s", e)
            return None

    def _get_latest_tag(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Get the latest tag if no releases exist."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/tags"

        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)

            # Check for rate limiting
            if response.status_code == 403:
                rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
                rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                if rate_limit_remaining == "0":
                    # Convert Unix timestamp to readable time

                    reset_time = (
                        datetime.fromtimestamp(int(rate_limit_reset))
                        if rate_limit_reset
                        else None
                    )
                    reset_str = (
                        reset_time.strftime("%Y-%m-%d %H:%M:%S")
                        if reset_time
                        else "unknown"
                    )
                    _LOG.warning(
                        "GitHub API rate limit exceeded for %s/%s tags. Reset at: %s (in %d seconds)",
                        owner,
                        repo,
                        reset_str,
                        int(rate_limit_reset) - int(datetime.now().timestamp())
                        if rate_limit_reset
                        else 0,
                    )
                    return None

            if response.status_code == 200:
                tags = response.json()
                if tags:
                    return {"tag_name": tags[0].get("name", "")}
            return None
        except requests.RequestException:
            return None

    @staticmethod
    def compare_versions(current: str, latest: str) -> bool:
        """Check if latest version is newer than current."""
        try:
            # Strip 'v' prefix if present and compare using packaging
            current_clean = re.sub(r"^[vV]", "", current).split("-")[0].split("+")[0]
            latest_clean = re.sub(r"^[vV]", "", latest).split("-")[0].split("+")[0]
            return Version(latest_clean) > Version(current_clean)
        except (InvalidVersion, TypeError, AttributeError):
            return False


def load_registry() -> list[dict[str, Any]]:
    """Load the integrations registry from URL or local file."""
    try:
        # Check if it's a local file path
        if os.path.exists(KNOWN_INTEGRATIONS_URL):
            _LOG.debug("Loading registry from local file: %s", KNOWN_INTEGRATIONS_URL)
            with open(KNOWN_INTEGRATIONS_URL, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "integrations" in data:
                    return data["integrations"]
                if isinstance(data, list):
                    return data
                return []

        # Otherwise treat it as a URL
        _LOG.debug("Loading registry from URL: %s", KNOWN_INTEGRATIONS_URL)
        response = requests.get(KNOWN_INTEGRATIONS_URL, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and "integrations" in data:
                return data["integrations"]
            if isinstance(data, list):
                return data
        return []
    except (requests.RequestException, OSError, json.JSONDecodeError) as e:
        _LOG.warning("Failed to load registry: %s", e)
        return []
