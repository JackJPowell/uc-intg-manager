"""
Integration Data Service.

This module provides a unified service for managing integration data,
combining Remote API and GitHub API to provide complete integration info.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

from const import KNOWN_INTEGRATIONS_URL
from github_api import GitHubClient
from remote_api import RemoteAPIClient, RemoteAPIError

_LOG = logging.getLogger(__name__)


@dataclass
class IntegrationInfo:
    """Information about an installed integration."""

    instance_id: str
    driver_id: str
    name: str
    version: str
    description: str = ""
    icon: str = ""
    home_page: str = ""
    developer: str = ""
    enabled: bool = True
    state: str = "UNKNOWN"

    # Update information (only for custom integrations)
    update_available: bool = False
    latest_version: str | None = None

    # Integration type flags
    custom: bool = False
    """Custom integration that can be managed (updated, backed up, etc.)"""

    official: bool = False
    """Official UC integration - read-only, no management operations allowed"""

    configured_entities: int = 0


@dataclass
class AvailableIntegration:
    """Information about an available integration from the registry."""

    driver_id: str
    name: str
    description: str = ""
    icon: str = ""
    home_page: str = ""
    developer: str = ""
    version: str = ""
    category: str = ""
    installed: bool = False

    custom: bool = True
    """Custom integration that can be installed/managed"""

    official: bool = False
    """Official UC integration - shown for reference only, not manageable"""


class IntegrationService:
    """
    Service for managing integration data.

    Combines data from the Remote API and GitHub API to provide
    comprehensive integration information with update status.
    """

    def __init__(self, remote_client: RemoteAPIClient) -> None:
        """
        Initialize the integration service.

        :param remote_client: Configured Remote API client
        """
        self._remote = remote_client
        self._github = GitHubClient()
        self._known_integrations: list[dict[str, Any]] = []
        self._cache_file = os.path.join(
            os.environ.get("UC_DATA_HOME", "."), "integrations_cache.json"
        )

    async def close(self) -> None:
        """Close all API clients."""
        await self._remote.close()
        await self._github.close()

    async def load_known_integrations(self) -> list[dict[str, Any]]:
        """
        Load the list of known integrations from the registry.

        :return: List of known integration dictionaries
        """
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(KNOWN_INTEGRATIONS_URL) as response:
                    if response.status == 200:
                        self._known_integrations = await response.json()
                        # Cache for offline use
                        self._cache_known_integrations()
                        return self._known_integrations
        except Exception as e:
            _LOG.warning("Failed to fetch known integrations: %s", e)

        # Try to load from cache
        return self._load_cached_integrations()

    def _cache_known_integrations(self) -> None:
        """Cache known integrations to disk."""
        try:
            with open(self._cache_file, "w") as f:
                json.dump(self._known_integrations, f)
        except Exception as e:
            _LOG.warning("Failed to cache integrations: %s", e)

    def _load_cached_integrations(self) -> list[dict[str, Any]]:
        """Load cached integrations from disk."""
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "r") as f:
                    self._known_integrations = json.load(f)
                    return self._known_integrations
        except Exception as e:
            _LOG.warning("Failed to load cached integrations: %s", e)
        return []

    async def get_installed_integrations(
        self, check_updates: bool = True
    ) -> list[IntegrationInfo]:
        """
        Get all installed integrations with update status.

        :param check_updates: Whether to check GitHub for updates
        :return: List of IntegrationInfo objects
        """
        integrations: list[IntegrationInfo] = []

        try:
            instances = await self._remote.get_integration_instances()
        except RemoteAPIError as e:
            _LOG.error("Failed to fetch integration instances: %s", e)
            return integrations

        # Fetch driver details for each instance
        tasks = []
        for instance in instances:
            driver_id = instance.get("driver_id", "")
            if driver_id:
                tasks.append(self._get_integration_info(instance, check_updates))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, IntegrationInfo):
                    integrations.append(result)
                elif isinstance(result, Exception):
                    _LOG.warning("Failed to get integration info: %s", result)

        return integrations

    async def _get_integration_info(
        self, instance: dict[str, Any], check_updates: bool
    ) -> IntegrationInfo:
        """
        Build IntegrationInfo from instance and driver data.

        :param instance: Integration instance data
        :param check_updates: Whether to check for updates
        :return: IntegrationInfo object
        """
        driver_id = instance.get("driver_id", "")

        # Get driver metadata
        try:
            driver = await self._remote.get_driver(driver_id)
        except RemoteAPIError:
            driver = {}

        # Extract name (handle multi-language)
        name = driver.get("name", {})
        if isinstance(name, dict):
            name = name.get("en", name.get(list(name.keys())[0], driver_id))

        # Extract description
        description = driver.get("description", {})
        if isinstance(description, dict):
            description = description.get(
                "en", description.get(list(description.keys())[0], "")
            )

        # Extract developer
        developer_info = driver.get("developer", {})
        developer = (
            developer_info.get("name", "") if isinstance(developer_info, dict) else ""
        )

        info = IntegrationInfo(
            instance_id=instance.get("integration_id", ""),
            driver_id=driver_id,
            name=name,
            version=driver.get("version", "unknown"),
            description=description,
            icon=driver.get("icon", ""),
            home_page=driver.get("home_page", ""),
            developer=developer,
            enabled=instance.get("enabled", True),
            state=instance.get("device_state", "UNKNOWN"),
            custom=driver.get("custom", False),
            configured_entities=len(instance.get("configured_entities", [])),
        )

        # Check for updates if home_page points to GitHub
        if check_updates and info.home_page:
            update_available, latest = await self._github.check_update_available(
                info.home_page, info.version
            )
            info.update_available = update_available
            info.latest_version = latest

        return info

    async def get_available_integrations(self) -> list[AvailableIntegration]:
        """
        Get list of available integrations from the registry.

        :return: List of AvailableIntegration objects
        """
        if not self._known_integrations:
            await self.load_known_integrations()

        # Get currently installed driver IDs
        installed_ids: set[str] = set()
        try:
            drivers = await self._remote.get_all_drivers()
            installed_ids = {d.get("driver_id", "") for d in drivers}
        except RemoteAPIError as e:
            _LOG.warning("Failed to fetch installed drivers: %s", e)

        available: list[AvailableIntegration] = []

        for intg in self._known_integrations:
            # Extract name
            name = intg.get("name", {})
            if isinstance(name, dict):
                name = name.get("en", intg.get("driver_id", "Unknown"))

            # Extract description
            description = intg.get("description", {})
            if isinstance(description, dict):
                description = description.get("en", "")

            # Extract developer
            developer_info = intg.get("developer", {})
            developer = (
                developer_info.get("name", "")
                if isinstance(developer_info, dict)
                else ""
            )

            driver_id = intg.get("driver_id", "")

            available.append(
                AvailableIntegration(
                    driver_id=driver_id,
                    name=name if isinstance(name, str) else str(name),
                    description=description
                    if isinstance(description, str)
                    else str(description),
                    icon=intg.get("icon", ""),
                    home_page=intg.get("home_page", ""),
                    developer=developer,
                    version=intg.get("version", ""),
                    category=intg.get("category", ""),
                    installed=driver_id in installed_ids,
                )
            )

        return available

    async def refresh_integration(self, instance_id: str) -> IntegrationInfo | None:
        """
        Refresh information for a specific integration.

        :param instance_id: Integration instance ID
        :return: Updated IntegrationInfo or None
        """
        try:
            instances = await self._remote.get_integration_instances()
            for instance in instances:
                if instance.get("integration_id") == instance_id:
                    return await self._get_integration_info(
                        instance, check_updates=True
                    )
        except RemoteAPIError as e:
            _LOG.error("Failed to refresh integration: %s", e)
        return None
