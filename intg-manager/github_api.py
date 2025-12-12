"""
GitHub API Client.

This module handles communication with the GitHub API to fetch
release information for integrations.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
import re
from typing import Any

import aiohttp

from const import GITHUB_API_BASE

_LOG = logging.getLogger(__name__)


class GitHubAPIError(Exception):
    """Exception raised when GitHub API calls fail."""


class GitHubClient:
    """
    Client for interacting with the GitHub API.

    Fetches release information to determine if updates are available.
    """

    def __init__(self) -> None:
        """Initialize the GitHub API client."""
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "uc-intg-manager",
            }
            # Create timeout object explicitly to avoid context manager issues
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def parse_github_url(home_page: str) -> tuple[str, str] | None:
        """
        Parse a GitHub URL to extract owner and repo.

        :param home_page: GitHub URL (e.g., https://github.com/owner/repo)
        :return: Tuple of (owner, repo) or None if not a valid GitHub URL
        """
        patterns = [
            r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$",
            r"github\.com/([^/]+)/([^/]+)$",
        ]

        for pattern in patterns:
            match = re.search(pattern, home_page)
            if match:
                return match.group(1), match.group(2).rstrip("/")
        return None

    async def get_latest_release(self, owner: str, repo: str) -> dict[str, Any] | None:
        """
        Get the latest release for a repository.

        :param owner: Repository owner
        :param repo: Repository name
        :return: Release data dictionary or None if no releases
        """
        session = await self._get_session()
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"

        try:
            async with session.get(url) as response:
                if response.status == 404:
                    # No releases found, try tags
                    return await self._get_latest_tag(owner, repo)
                if response.status == 403:
                    _LOG.warning("GitHub API rate limit exceeded")
                    return None
                if response.status >= 400:
                    _LOG.warning(
                        "GitHub API error %d for %s/%s", response.status, owner, repo
                    )
                    return None
                return await response.json()
        except aiohttp.ClientError as e:
            _LOG.error("GitHub API connection error: %s", e)
            return None

    async def _get_latest_tag(self, owner: str, repo: str) -> dict[str, Any] | None:
        """
        Get the latest tag for a repository (fallback when no releases).

        :param owner: Repository owner
        :param repo: Repository name
        :return: Tag data as release-like dictionary or None
        """
        session = await self._get_session()
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/tags"

        try:
            async with session.get(url) as response:
                if response.status >= 400:
                    return None
                tags = await response.json()
                if tags:
                    return {"tag_name": tags[0].get("name", "unknown")}
                return None
        except aiohttp.ClientError:
            return None

    @staticmethod
    def parse_version(version_str: str) -> tuple[int, ...]:
        """
        Parse a version string into a comparable tuple.

        :param version_str: Version string (e.g., "v1.2.3", "1.2.3")
        :return: Tuple of version numbers
        """
        # Remove 'v' prefix if present
        version_str = version_str.lstrip("vV")

        # Extract numeric parts
        parts = re.findall(r"\d+", version_str)
        return tuple(int(p) for p in parts) if parts else (0,)

    @staticmethod
    def is_newer_version(current: str, latest: str) -> bool:
        """
        Check if the latest version is newer than the current version.

        :param current: Current installed version
        :param latest: Latest available version
        :return: True if latest is newer than current
        """
        current_parts = GitHubClient.parse_version(current)
        latest_parts = GitHubClient.parse_version(latest)

        # Pad shorter tuple with zeros
        max_len = max(len(current_parts), len(latest_parts))
        current_padded = current_parts + (0,) * (max_len - len(current_parts))
        latest_padded = latest_parts + (0,) * (max_len - len(latest_parts))

        return latest_padded > current_padded

    async def get_latest_version(self, home_page: str) -> str | None:
        """
        Get the latest version from a GitHub home page URL.

        :param home_page: GitHub URL
        :return: Latest version string or None
        """
        parsed = self.parse_github_url(home_page)
        if not parsed:
            _LOG.warning("Not a valid GitHub URL: %s", home_page)
            return None

        owner, repo = parsed
        release = await self.get_latest_release(owner, repo)

        if release:
            return release.get("tag_name")
        return None

    async def check_update_available(
        self, home_page: str, current_version: str
    ) -> tuple[bool, str | None]:
        """
        Check if an update is available for an integration.

        :param home_page: GitHub home page URL
        :param current_version: Currently installed version
        :return: Tuple of (update_available, latest_version)
        """
        latest = await self.get_latest_version(home_page)

        if not latest:
            return False, None

        is_newer = self.is_newer_version(current_version, latest)
        return is_newer, latest
