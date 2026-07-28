"""
API Clients.

This module provides async HTTP clients for use in Quart routes.
Uses aiohttp for non-blocking I/O.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
import re
import shutil
import ssl
from datetime import datetime
from typing import Any

import aiohttp
import certifi
import requests
from const import (
    GITHUB_API_BASE,
    KNOWN_INTEGRATIONS_URL,
    REPO_CACHE_VALIDITY,
    MANAGER_DATA_FILE,
)
from github_api import compare_versions_for_update
from ucapi_framework import find_orphaned_entities
from ucapi_framework.helpers import find_unused_activity_entities

_LOG = logging.getLogger(__name__)

# Default timeout for all requests (connect, read) in seconds
REQUEST_TIMEOUT = aiohttp.ClientTimeout(connect=10, total=40)
# Shorter timeout for connection tests
CONNECT_TIMEOUT = aiohttp.ClientTimeout(connect=2, total=7)
# Longer timeout for file downloads (30s connect, 5min total)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(connect=30, total=330)
# Sync timeout tuple kept for requests-based uses (fetch_repository_batch)
_SYNC_REQUEST_TIMEOUT = (10, 30)

# In-memory registry cache to avoid blocking the event loop on every request
_registry_cache: dict[str, Any] | list | None = None
_registry_cache_time: float = 0.0
_REGISTRY_CACHE_TTL = 1800  # 30 minutes


class SyncAPIError(Exception):
    """Exception raised when API calls fail."""


class RemoteClient:
    """
    Async client for the Unfolded Circle Remote REST API.

    Uses aiohttp for non-blocking HTTP requests.
    """

    def __init__(
        self,
        address: str,
        pin: str | None = None,
        api_key: str | None = None,
        port: int = 80,
    ) -> None:
        self._address = address
        self._pin = pin
        self._api_key = api_key
        self._port = port
        self._base_url = f"http://{address}:{port}/api"
        self._ssl_context = False  # No SSL for local HTTP

        # Build auth headers
        self._headers: dict[str, str] = {}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

        self._auth = (
            aiohttp.BasicAuth("web-configurator", pin)
            if (pin and not api_key)
            else None
        )

    def _make_session(
        self, timeout: aiohttp.ClientTimeout | None = None
    ) -> aiohttp.ClientSession:
        """Create a new aiohttp session with configured auth/headers."""
        return aiohttp.ClientSession(
            headers=self._headers,
            auth=self._auth,
            timeout=timeout or REQUEST_TIMEOUT,
            connector=aiohttp.TCPConnector(ssl=self._ssl_context),
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        timeout: aiohttp.ClientTimeout | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Make an async HTTP request to the Remote API.

        :raises SyncAPIError: If the request fails
        """
        url = f"{self._base_url}{endpoint}"
        async with self._make_session(timeout) as session:
            try:
                async with session.request(method, url, **kwargs) as response:
                    if response.status == 401:
                        raise SyncAPIError(
                            "Authentication failed. Check PIN or API key."
                        )
                    if response.status == 403:
                        raise SyncAPIError("Access forbidden. PIN may have changed.")
                    if response.status >= 400:
                        text = await response.text()
                        raise SyncAPIError(f"API error: {response.status} - {text}")
                    text = await response.text()
                    if text:
                        return await response.json(content_type=None)
                    return None
            except aiohttp.ClientError as e:
                raise SyncAPIError(f"Request failed: {e}") from e

    async def test_connection(self) -> bool:
        """Test connectivity to the remote."""
        try:
            await self._request("GET", "/pub/version", timeout=CONNECT_TIMEOUT)
            return True
        except SyncAPIError:
            return False

    async def get_integrations(self) -> list[dict[str, Any]]:
        """Get list of installed integration instances."""
        return await self._request("GET", "/intg/instances?limit=100") or []

    async def get_activities(self) -> list[dict[str, Any]]:
        """Get Remote activities and their current ON/OFF state."""
        return await self._request("GET", "/activities?limit=100") or []

    async def get_driver(self, driver_id: str) -> dict[str, Any] | None:
        """Get driver metadata by ID."""
        try:
            return await self._request("GET", f"/intg/drivers/{driver_id}")
        except SyncAPIError as e:
            _LOG.warning("Failed to get driver %s: %s", driver_id, e)
            return None

    async def is_docked(self) -> bool:
        """Check if the remote is currently charging (docked or wireless)."""
        try:
            status = await self._request("GET", "/system/power/charger")
            if status and isinstance(status, dict):
                return status.get("power_supply", False) or status.get(
                    "wireless_charging", False
                )
            return False
        except SyncAPIError:
            return False

    async def get_system_update(self) -> dict[str, Any]:
        """Get system firmware update information."""
        return await self._request("GET", "/system/update") or {}

    async def check_system_update(self) -> dict[str, Any]:
        """Trigger an immediate firmware update check on the remote."""
        return await self._request("PUT", "/system/update") or {}

    async def reboot_remote(self) -> bool:
        """Reboot the remote."""
        try:
            await self._request("POST", "/system?cmd=REBOOT")
            _LOG.info("Remote reboot command sent")
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to reboot remote: %s", e)
            raise

    async def power_off_remote(self) -> bool:
        """Power off the remote."""
        try:
            await self._request("POST", "/system?cmd=POWER_OFF")
            _LOG.info("Remote power off command sent")
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to power off remote: %s", e)
            raise

    async def get_drivers(self) -> list[dict[str, Any]]:
        """Get list of all integration drivers."""
        return await self._request("GET", "/intg/drivers?limit=100") or []

    async def get_log_services(self) -> list[dict[str, Any]]:
        """Get all available log services from the remote."""
        return await self._request("GET", "/system/logs/services") or []

    async def get_logs(
        self,
        priority: int | None = None,
        service: str | None = None,
        limit: int = 1000,
        as_text: bool = False,
    ) -> list[dict[str, Any]] | str:
        """Get log entries from the remote."""
        params: dict[str, Any] = {}
        if priority is not None:
            params["p"] = priority
        if service is not None:
            params["s"] = service
        if limit is not None:
            params["limit"] = min(limit, 10000)

        url = f"{self._base_url}/system/logs"
        async with self._make_session() as session:
            try:
                headers = {
                    "Accept": "text/plain" if as_text else "application/json",
                    "Content-Type": "text/plain" if as_text else "application/json",
                }
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status == 401:
                        raise SyncAPIError("Authentication failed")
                    if response.status == 403:
                        raise SyncAPIError("Access forbidden")
                    if response.status >= 400:
                        text = await response.text()
                        raise SyncAPIError(
                            f"Request failed: {response.status} - {text}"
                        )
                    if as_text:
                        return await response.text()
                    text = await response.text()
                    if not text:
                        return []
                    _LOG.debug(
                        "get_logs raw response (first 200 chars): %s", text[:200]
                    )
                    # Try standard JSON array first, fall back to NDJSON
                    try:
                        data = json.loads(text)
                        return data if isinstance(data, list) else []
                    except json.JSONDecodeError:
                        pass
                    # Remote returned NDJSON (one JSON object per line)
                    entries = []
                    for line in text.splitlines():
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                    return entries
            except aiohttp.ClientError as e:
                raise SyncAPIError(f"Request failed: {e}") from e

    async def get_localization(self) -> dict[str, Any]:
        """Get the remote's localization settings."""
        try:
            result = await self._request("GET", "/cfg/localization")
            _LOG.debug("Localization settings: %s", result)
            return result if isinstance(result, dict) else {}
        except SyncAPIError as e:
            _LOG.warning("Failed to get localization settings: %s", e)
            return {}

    async def find_orphan_entities(self) -> list[dict[str, Any]]:
        """Find orphaned entities across all activities."""
        try:
            remote_url = f"http://{self._address}:{self._port}"
            result = await find_orphaned_entities(
                remote_url=remote_url,
                api_key=self._api_key,
            )
            _LOG.debug("Found %d orphan entities", len(result))
            return result if isinstance(result, list) else []
        except Exception as e:
            _LOG.error("Failed to get orphan entities: %s", e)
            raise SyncAPIError(f"Failed to get orphan entities: {e}") from e

    async def find_unused_entities(self) -> list[dict[str, Any]]:
        """Find entities included in activities but never actually used."""
        try:
            remote_url = f"http://{self._address}:{self._port}"
            result = await find_unused_activity_entities(
                remote_url=remote_url,
                api_key=self._api_key,
            )
            _LOG.debug("Found %d unused activity entities", len(result))
            return result if isinstance(result, list) else []
        except Exception as e:
            _LOG.error("Failed to get unused activity entities: %s", e)
            raise SyncAPIError(f"Failed to get unused activity entities: {e}") from e

    async def get_ir_remotes(
        self, page: int = 1, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get list of IR remotes."""
        try:
            params = {"kind": "IR", "page": page, "limit": limit}
            result = await self._request("GET", "/remotes", params=params)
            return result if isinstance(result, list) else []
        except Exception as e:
            raise SyncAPIError(f"Failed to get IR remotes: {e}") from e

    async def get_remote_detail(self, entity_id: str) -> dict[str, Any]:
        """Get detailed information for a specific remote."""
        try:
            result = await self._request("GET", f"/remotes/{entity_id}")
            return result if isinstance(result, dict) else {}
        except Exception as e:
            raise SyncAPIError(f"Failed to get remote detail: {e}") from e

    async def get_custom_ir_codesets(
        self, page: int = 1, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get list of custom IR codesets."""
        try:
            params = {"page": page, "limit": limit}
            result = await self._request("GET", "/ir/codes/custom", params=params)
            return result if isinstance(result, list) else []
        except Exception as e:
            raise SyncAPIError(f"Failed to get custom IR codesets: {e}") from e

    async def delete_custom_ir_codeset(self, device_id: str) -> bool:
        """Delete a custom IR codeset."""
        try:
            await self._request("DELETE", f"/ir/codes/custom/{device_id}")
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to delete custom IR codeset %s: %s", device_id, e)
            raise

    async def create_remote(self, remote_name: str, codeset_id: str) -> dict[str, Any]:
        """Create a new remote with a custom codeset."""
        try:
            payload = {"name": {"en": remote_name}, "codeset_id": codeset_id}
            result = await self._request("POST", "/remotes", json=payload)
            return result if isinstance(result, dict) else {}
        except Exception as e:
            raise SyncAPIError(f"Failed to create remote: {e}") from e

    async def delete_instance(self, instance_id: str) -> bool:
        """Delete an integration instance."""
        try:
            await self._request("DELETE", f"/intg/instances/{instance_id}")
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to delete instance %s: %s", instance_id, e)
            raise

    async def delete_driver(self, driver_id: str) -> bool:
        """Delete an integration driver (and its instances)."""
        try:
            await self._request("DELETE", f"/intg/drivers/{driver_id}")
            return True
        except SyncAPIError as e:
            _LOG.error("Failed to delete driver %s: %s", driver_id, e)
            raise

    async def get_firmware_version(self) -> str:
        """Return the installed firmware version string, or '0.0.0' on failure."""
        try:
            result = await self._request("GET", "/pub/version", timeout=CONNECT_TIMEOUT)
            if result and isinstance(result, dict):
                return result.get("os", "0.0.0")
        except SyncAPIError:
            pass
        return "0.0.0"

    async def install_integration(
        self, archive_data: bytes, filename: str
    ) -> dict[str, Any]:
        """Install an integration from a tar.gz archive."""
        url = f"{self._base_url}/intg/install"
        install_timeout = aiohttp.ClientTimeout(connect=30, total=150)
        async with self._make_session(install_timeout) as session:
            try:
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    archive_data,
                    filename=filename,
                    content_type="application/x-gzip",
                )
                async with session.post(url, data=form) as response:
                    if response.status == 401:
                        raise SyncAPIError(
                            "Authentication failed. Check PIN or API key."
                        )
                    if response.status == 403:
                        raise SyncAPIError("Access forbidden. PIN may have changed.")
                    if response.status >= 400:
                        text = await response.text()
                        raise SyncAPIError(
                            f"Install failed: {response.status} - {text}"
                        )
                    text = await response.text()
                    if text:
                        return await response.json(content_type=None)
                    return {"status": "ok"}
            except aiohttp.ClientError as e:
                raise SyncAPIError(f"Install request failed: {e}") from e

    async def install_integration_inplace(
        self, archive_data: bytes, filename: str
    ) -> dict[str, Any]:
        """Update an integration in-place using the firmware 2.9.3+ update flag.

        Calls POST /intg/install?update=true which updates the driver without
        the backup/delete/restore cycle, preserving all configuration.
        """
        url = f"{self._base_url}/intg/install"
        install_timeout = aiohttp.ClientTimeout(connect=30, total=150)
        async with self._make_session(install_timeout) as session:
            try:
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    archive_data,
                    filename=filename,
                    content_type="application/x-gzip",
                )
                async with session.post(
                    url, params={"update": "true"}, data=form
                ) as response:
                    if response.status == 401:
                        raise SyncAPIError(
                            "Authentication failed. Check PIN or API key."
                        )
                    if response.status == 403:
                        raise SyncAPIError("Access forbidden. PIN may have changed.")
                    if response.status >= 400:
                        text = await response.text()
                        raise SyncAPIError(
                            f"In-place update failed: {response.status} - {text}"
                        )
                    text = await response.text()
                    if text:
                        return await response.json(content_type=None)
                    return {"status": "ok"}
            except aiohttp.ClientError as e:
                raise SyncAPIError(f"In-place update request failed: {e}") from e

    async def start_setup(
        self, driver_id: str, reconfigure: bool = True
    ) -> dict[str, Any]:
        """Start the integration setup flow."""
        payload = {"driver_id": driver_id, "reconfigure": reconfigure, "setup_data": {}}
        return await self._request("POST", "/intg/setup", json=payload)

    async def get_setup(self, driver_id: str) -> dict[str, Any]:
        """Get the current setup page for an integration."""
        return await self._request("GET", f"/intg/setup/{driver_id}")

    async def send_setup_input(
        self, driver_id: str, input_values: dict[str, Any]
    ) -> dict[str, Any]:
        """Send input values during the setup flow."""
        return await self._request(
            "PUT", f"/intg/setup/{driver_id}", json={"input_values": input_values}
        )

    async def complete_setup(self, driver_id: str) -> bool:
        """Complete and clean up an integration setup flow."""
        try:
            await self._request("DELETE", f"/intg/setup/{driver_id}")
            return True
        except SyncAPIError as e:
            _LOG.warning("Failed to complete setup for %s: %s", driver_id, e)
            return False

    async def get_enabled_integrations(self) -> list[dict[str, Any]]:
        """Get enabled integrations (for post-install verification)."""
        try:
            return (
                await self._request("GET", "/intg?enabled=true&limit=50&page=1") or []
            )
        except SyncAPIError:
            return []

    async def get_instantiable_drivers(self) -> list[dict[str, Any]]:
        """Get instantiable and enabled drivers (for post-install verification)."""
        try:
            return (
                await self._request(
                    "GET",
                    "/intg/drivers?instantiable=true&enabled=true&limit=50&page=1",
                )
                or []
            )
        except SyncAPIError:
            return []

    async def get_custom_drivers_without_instances(self) -> list[dict[str, Any]]:
        """Get custom drivers without instances (for post-install verification)."""
        try:
            return (
                await self._request(
                    "GET",
                    "/intg/drivers?driver_type=CUSTOM&has_instances=false&enabled=true&limit=50&page=1",
                )
                or []
            )
        except SyncAPIError:
            return []

    async def get_custom_active_drivers_count(self) -> int:
        """Return the number of active custom integration drivers.

        Uses the same query the Remote uses to enforce its 10-integration limit:
        ``driver_type=CUSTOM&enabled=true&has_instances=true``.

        :return: Count of active custom drivers, or -1 on error.
        """
        try:
            drivers = (
                await self._request(
                    "GET",
                    "/intg/drivers?driver_type=CUSTOM&enabled=true&has_instances=true&page=1&limit=100",
                )
                or []
            )
            return len(drivers)
        except SyncAPIError as e:
            _LOG.warning("Failed to fetch custom driver count: %s", e)
            return -1

    async def get_enabled_instances(self) -> list[dict[str, Any]]:
        """Get enabled integration instances (for post-restore verification)."""
        try:
            return (
                await self._request(
                    "GET", "/intg/instances?enabled=true&limit=50&page=1"
                )
                or []
            )
        except SyncAPIError:
            return []

    async def get_instance(self, instance_id: str) -> dict[str, Any]:
        """Get a single integration instance by ID."""
        return await self._request("GET", f"/intg/instances/{instance_id}")

    async def get_instance_entities(
        self, instance_id: str, filter_type: str = "NEW", reload: bool = True
    ) -> list[dict[str, Any]]:
        """Get entities for an integration instance."""
        try:
            reload_param = "true" if reload else "false"
            return (
                await self._request(
                    "GET",
                    f"/intg/instances/{instance_id}/entities?reload={reload_param}&filter={filter_type}&limit=100&page=1",
                )
                or []
            )
        except SyncAPIError:
            return []

    async def get_configured_entities(self, instance_id: str) -> list[dict[str, Any]]:
        """Get configured (registered) entities for an integration instance.

        Uses reload=false so the Remote returns its own stored configured-entity
        list rather than re-fetching from the driver (which would clear it).
        """
        return await self.get_instance_entities(
            instance_id, filter_type="CONFIGURED", reload=False
        )

    async def register_entities(
        self, integration_id: str, entity_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Register entities for an integration."""
        endpoint = f"/intg/instances/{integration_id}/entities"
        if entity_ids:
            return await self._request("POST", endpoint, json=entity_ids)
        return await self._request("POST", endpoint)

    async def register_entity(
        self, integration_id: str, entity_id: str
    ) -> dict[str, Any]:
        """Register a specific entity for an integration."""
        return await self._request(
            "POST", f"/intg/instances/{integration_id}/entities/{entity_id}"
        )

    async def delete_all_entities(self, integration_id: str) -> dict[str, Any]:
        """Delete all entities for an integration."""
        return await self._request(
            "DELETE", "/entities", json={"integration_id": integration_id}
        )

    async def delete_entity(
        self, integration_id: str, entity_id: str
    ) -> dict[str, Any]:
        """Delete a specific entity for an integration."""
        full_entity_id = f"{integration_id}.{entity_id}"
        return await self._request("DELETE", f"/entities/{full_entity_id}")


# Backward-compat alias
SyncRemoteClient = RemoteClient


async def find_orphaned_ir_codesets(api_client: RemoteClient) -> list[dict[str, Any]]:
    """
    Find custom IR codesets that are not associated with any remote.

    :param api_client: RemoteClient instance for API calls
    :return: List of orphaned codesets with device_id and device_name
    """
    try:
        associated_codeset_ids: set[str] = set()
        page = 1
        while True:
            remotes = await api_client.get_ir_remotes(page=page, limit=100)
            if not remotes:
                break
            for remote in remotes:
                entity_id = remote.get("entity_id")
                if entity_id:
                    detail = await api_client.get_remote_detail(entity_id)
                    ir = detail.get("options", {}).get("ir", {})
                    ir_codeset = ir.get("codeset")
                    if ir_codeset and isinstance(ir_codeset, dict):
                        codeset_id = ir_codeset.get("id")
                        if codeset_id:
                            associated_codeset_ids.add(codeset_id)
            if len(remotes) < 100:
                break
            page += 1

        all_codesets: list[dict[str, Any]] = []
        page = 1
        while True:
            codesets = await api_client.get_custom_ir_codesets(page=page, limit=100)
            if not codesets:
                break
            all_codesets.extend(codesets)
            if len(codesets) < 100:
                break
            page += 1

        orphaned = []
        for codeset in all_codesets:
            device_id = codeset.get("device_id")
            device_name = codeset.get("device", device_id)
            if device_id and device_id not in associated_codeset_ids:
                orphaned.append({"device_id": device_id, "device_name": device_name})

        _LOG.info("Found %d orphaned IR codesets", len(orphaned))
        return orphaned

    except Exception as e:
        _LOG.error("Failed to find orphaned IR codesets: %s", e)
        return []


class GitHubClient:
    """
    Async client for the GitHub API. Uses aiohttp.
    """

    def __init__(self) -> None:
        self._default_headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "uc-intg-manager",
        }

    def _make_session(
        self, timeout: aiohttp.ClientTimeout | None = None
    ) -> aiohttp.ClientSession:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        return aiohttp.ClientSession(
            headers=self._default_headers,
            timeout=timeout or REQUEST_TIMEOUT,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
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

    def _check_rate_limit(
        self, headers: Any, owner: str, repo: str, context: str = ""
    ) -> bool:
        """Log rate limit warning. Returns True if rate limited."""
        remaining = headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            reset = headers.get("X-RateLimit-Reset")
            reset_str = "unknown"
            countdown = 0
            if reset:
                try:
                    reset_time = datetime.fromtimestamp(int(reset))
                    reset_str = reset_time.strftime("%Y-%m-%d %H:%M:%S")
                    countdown = int(reset) - int(datetime.now().timestamp())
                except (ValueError, OSError):
                    pass
            _LOG.warning(
                "GitHub API rate limit exceeded for %s/%s%s. Reset at: %s (in %d seconds)",
                owner,
                repo,
                f" {context}" if context else "",
                reset_str,
                countdown,
            )
            return True
        return False

    async def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Get the latest release for a repository."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"
        async with self._make_session() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 403 and self._check_rate_limit(
                        response.headers, owner, repo
                    ):
                        return None
                    if response.status == 200:
                        return await response.json()
                    if response.status == 404:
                        return await self._get_latest_tag(owner, repo)
                    return None
            except aiohttp.ClientError as e:
                _LOG.warning("Failed to get release for %s/%s: %s", owner, repo, e)
                return None

    async def get_releases(
        self, owner: str, repo: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get multiple releases for a repository."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases"
        async with self._make_session() as session:
            try:
                async with session.get(url, params={"per_page": limit}) as response:
                    if response.status == 403 and self._check_rate_limit(
                        response.headers, owner, repo, "releases"
                    ):
                        return []
                    if response.status == 200:
                        return await response.json()
                    return []
            except aiohttp.ClientError as e:
                _LOG.warning("Failed to get releases for %s/%s: %s", owner, repo, e)
                return []

    async def get_release_by_tag(
        self, owner: str, repo: str, tag: str
    ) -> dict[str, Any] | None:
        """Get a specific release by tag name."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"
        async with self._make_session() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    return None
            except aiohttp.ClientError as e:
                _LOG.warning(
                    "Failed to get release for %s/%s tag %s: %s", owner, repo, tag, e
                )
                return None

    async def download_release_asset(
        self,
        owner: str,
        repo: str,
        asset_pattern: str | None = None,
        version: str | None = None,
    ) -> tuple[bytes, str] | None:
        """Download a release asset (tar.gz file) from a release."""
        if version:
            release = await self.get_release_by_tag(owner, repo, version)
            if not release:
                _LOG.warning(
                    "No release found for %s/%s version %s", owner, repo, version
                )
                return None
        else:
            release = await self.get_latest_release(owner, repo)
            if not release:
                _LOG.warning("No release found for %s/%s", owner, repo)
                return None

        assets = release.get("assets", [])
        if not assets:
            _LOG.warning("No assets in release for %s/%s", owner, repo)
            return None

        target_asset = None
        if asset_pattern:
            try:
                pattern = re.compile(asset_pattern)
                for asset in assets:
                    if pattern.search(asset.get("name", "")):
                        target_asset = asset
                        break
            except re.error as e:
                _LOG.error("Invalid regex pattern '%s': %s", asset_pattern, e)
                return None
        else:
            for asset in assets:
                if ".tar.gz" in asset.get("name", ""):
                    target_asset = asset
                    break

        if not target_asset:
            _LOG.warning("No matching asset found in release for %s/%s", owner, repo)
            return None

        download_url = target_asset.get("browser_download_url")
        if not download_url:
            return None

        _LOG.info("Downloading %s from %s/%s", target_asset["name"], owner, repo)
        async with self._make_session(DOWNLOAD_TIMEOUT) as session:
            try:
                async with session.get(
                    download_url, headers={"Accept": "application/octet-stream"}
                ) as response:
                    if response.status == 200:
                        return await response.read(), target_asset["name"]
                    _LOG.error("Failed to download asset: %s", response.status)
                    return None
            except aiohttp.ClientError as e:
                _LOG.error("Failed to download release asset: %s", e)
                return None

    async def _get_latest_tag(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Get the latest tag if no releases exist."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/tags"
        async with self._make_session() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 403 and self._check_rate_limit(
                        response.headers, owner, repo, "tags"
                    ):
                        return None
                    if response.status == 200:
                        tags = await response.json()
                        if tags:
                            return {"tag_name": tags[0].get("name", "")}
                return None
            except aiohttp.ClientError:
                return None

    async def get_repository_info(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Get repository information including stars, forks, and dates."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
        async with self._make_session() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 403 and self._check_rate_limit(
                        response.headers, owner, repo
                    ):
                        return None
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "stargazers_count": data.get("stargazers_count", 0),
                            "forks_count": data.get("forks_count", 0),
                            "watchers_count": data.get("watchers_count", 0),
                            "created_at": data.get("created_at", ""),
                            "updated_at": data.get("updated_at", ""),
                            "pushed_at": data.get("pushed_at", ""),
                            "open_issues_count": data.get("open_issues_count", 0),
                        }
                    return None
            except aiohttp.ClientError as e:
                _LOG.warning(
                    "Failed to get repository info for %s/%s: %s", owner, repo, e
                )
                return None

    @staticmethod
    def compare_versions(current: str, latest: str) -> bool:
        """Check if latest version is newer than current."""
        return compare_versions_for_update(current, latest)


class _SyncGitHubClient:
    """
    Synchronous (requests-based) GitHub client.

    Used only by fetch_repository_batch() which runs in a background thread
    and cannot use async/await.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.verify = certifi.where()  # ty:ignore[invalid-assignment]
        self._session.headers.update(
            {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "uc-intg-manager",
            }
        )

    @staticmethod
    def parse_github_url(home_page: str) -> tuple[str, str] | None:
        """Parse a GitHub URL to extract owner and repo."""
        return GitHubClient.parse_github_url(home_page)

    def get_repository_info(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Get repository info synchronously."""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
        try:
            response = self._session.get(url, timeout=_SYNC_REQUEST_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                return {
                    "stargazers_count": data.get("stargazers_count", 0),
                    "forks_count": data.get("forks_count", 0),
                    "watchers_count": data.get("watchers_count", 0),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "pushed_at": data.get("pushed_at", ""),
                    "open_issues_count": data.get("open_issues_count", 0),
                }
            return None
        except requests.RequestException as e:
            _LOG.warning("Failed to get repository info for %s/%s: %s", owner, repo, e)
            return None


# Backward-compat alias (SyncGitHubClient kept as async GitHubClient)
SyncGitHubClient = GitHubClient


def load_repo_cache() -> dict[str, Any]:
    """Load repository cache from manager.json."""
    if not os.path.exists(MANAGER_DATA_FILE):
        return {"last_batch_time": 0, "repos": {}}
    try:
        with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            cache = data.get("shared", {}).get("repo_cache", {})
            if "repos" not in cache:
                return {
                    "last_batch_time": 0,
                    "repos": cache if isinstance(cache, dict) else {},
                }
            return cache
    except (OSError, json.JSONDecodeError) as e:
        _LOG.warning("Failed to load repo cache: %s", e)
        return {"last_batch_time": 0, "repos": {}}


def save_repo_cache(cache: dict[str, Any]) -> None:
    """Save repository cache to manager.json."""
    try:
        os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)
        existing_data: dict[str, Any] = {}
        if os.path.exists(MANAGER_DATA_FILE):
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        if existing_data.get("version") != "2.0":
            _LOG.error(
                "manager.json is not v2.0 format - migration should have run at startup"
            )
            existing_data["version"] = "2.0"
            if "remotes" not in existing_data:
                existing_data["remotes"] = {}
            if "shared" not in existing_data:
                existing_data["shared"] = {}
        existing_data["shared"]["repo_cache"] = cache
        with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2)
    except OSError as e:
        _LOG.warning("Failed to save repo cache: %s", e)


def get_cached_repo_info(
    owner: str, repo: str, github_client: "GitHubClient | _SyncGitHubClient"
) -> dict[str, Any]:
    """
    Get repository info from cache (returns cached data without fetching).

    Background batching in web_server.py populates this via fetch_repository_batch.
    """
    cache = load_repo_cache()
    repos = cache.get("repos", {})
    cache_key = f"{owner}/{repo}"
    now = datetime.now().timestamp()

    if cache_key in repos:
        cached_entry = repos[cache_key]
        cached_time = cached_entry.get("cached_at", 0)
        if now - cached_time < REPO_CACHE_VALIDITY:
            return cached_entry.get("data", {})
        # Return expired cache while background refresh happens
        return repos[cache_key].get("data", {})

    return {}


def load_registry() -> list[dict[str, Any]]:
    """Load the integrations registry from URL or local file."""
    data = load_registry_data()
    if isinstance(data, dict) and "integrations" in data:
        return data["integrations"]
    if isinstance(data, list):
        return data
    return []


def load_registry_data() -> dict[str, Any] | list:
    """Load the full registry payload (integrations + sponsors + any future keys)."""
    global _registry_cache, _registry_cache_time

    # Local file override: always read fresh (dev/testing workflow)
    if os.path.exists(KNOWN_INTEGRATIONS_URL):
        try:
            with open(KNOWN_INTEGRATIONS_URL, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _LOG.warning("Failed to load local registry file: %s", e)
            return {}

    # Return in-memory cache if still fresh
    now = datetime.now().timestamp()
    if (
        _registry_cache is not None
        and (now - _registry_cache_time) < _REGISTRY_CACHE_TTL
    ):
        return _registry_cache

    # Fetch from remote and populate cache
    try:
        response = requests.get(
            KNOWN_INTEGRATIONS_URL,
            timeout=_SYNC_REQUEST_TIMEOUT,
            verify=certifi.where(),
        )
        if response.status_code == 200:
            _registry_cache = response.json()
            _registry_cache_time = now
            return _registry_cache
        return _registry_cache if _registry_cache is not None else {}
    except (requests.RequestException, OSError, json.JSONDecodeError) as e:
        _LOG.warning("Failed to load registry: %s", e)
        return _registry_cache if _registry_cache is not None else {}


def migrate_to_multi_remote(default_remote_id: str, default_remote_name: str) -> bool:
    """
    Migrate manager.json from v1.0 (single remote) to v2.0 (multi-remote) format.
    """
    if not os.path.exists(MANAGER_DATA_FILE):
        _LOG.info("No existing manager.json found - will create v2.0 format")
        return True

    try:
        with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
            old_data = json.load(f)

        if old_data.get("version") == "2.0":
            _LOG.info("manager.json already migrated to v2.0")
            return True

        _LOG.info("Migrating manager.json from v1.0 to v2.0 format")
        backup_path = f"{MANAGER_DATA_FILE}.v1.backup"
        shutil.copy2(MANAGER_DATA_FILE, backup_path)

        old_settings = old_data.get("settings", {})
        old_integrations = old_data.get("integrations", {})
        old_notification_settings = old_data.get("notification_settings", {})
        old_notification_state = old_data.get("notification_state", {})
        old_read_message_ids = old_data.get("read_message_ids", [])
        old_repo_cache = old_data.get("repo_cache", {})

        ui_preferences = {
            "sort_by": old_settings.get("sort_by", "stars"),
            "sort_reverse": old_settings.get("sort_reverse", False),
        }
        registry_tracking = {
            "last_count": old_notification_settings.get("_last_registry_count", 0),
            "known_ids": old_notification_settings.get("_known_integration_ids", []),
        }
        new_settings = {
            k: v
            for k, v in old_settings.items()
            if k not in ["sort_by", "sort_reverse"]
        }
        new_notification_settings = {
            k: v
            for k, v in old_notification_settings.items()
            if k not in ["_last_registry_count", "_known_integration_ids"]
        }

        new_data = {
            "version": "2.0",
            "remotes": {
                default_remote_id: {
                    "name": default_remote_name,
                    "settings": new_settings,
                    "integrations": old_integrations,
                    "notification_settings": new_notification_settings,
                    "notification_state": old_notification_state,
                    "read_message_ids": old_read_message_ids,
                }
            },
            "shared": {
                "repo_cache": old_repo_cache,
                "ui_preferences": ui_preferences,
                "registry_tracking": registry_tracking,
            },
        }

        with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2)

        _LOG.info("Successfully migrated manager.json to v2.0 format")
        return True

    except Exception as e:
        _LOG.error("Failed to migrate manager.json: %s", e, exc_info=True)
        backup_path = f"{MANAGER_DATA_FILE}.v1.backup"
        if os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, MANAGER_DATA_FILE)
            except Exception as restore_error:
                _LOG.error("Failed to restore backup: %s", restore_error)
        return False
