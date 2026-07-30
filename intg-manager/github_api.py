"""
GitHub API Client.

This module handles communication with the GitHub API to fetch
release information for integrations.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
import re
import ssl
from typing import Any

import aiohttp
import certifi
from const import GITHUB_API_BASE
from packaging.version import InvalidVersion, Version

_PRE_RELEASE_PATTERN = re.compile(
    r"(?:[-._]|(?<=\d))(alpha|beta|preview|pre|rc|dev|a|b)\.?(\d*)(?![A-Za-z])",
    re.IGNORECASE,
)


def normalize_version(version: str) -> str:
    """Normalize a GitHub-style tag toward PEP 440.

    Strips the optional ``v`` prefix and SemVer ``+build`` metadata, and
    rewrites known pre-release tokens (``-pre``, ``-alpha``, ``-beta``,
    ``-rc``, ``-dev``, ``-preview``) into PEP 440 form. Unknown suffixes
    (e.g. ``-build``) are left untouched, so the result is not guaranteed
    to be PEP 440-valid for every input. Callers needing ordering should
    go through ``compare_versions_for_update``.
    """
    if not version:
        return version
    s = version.lstrip("vV").split("+", 1)[0]

    def _repl(match: re.Match[str]) -> str:
        kind = match.group(1).lower()
        num = int(match.group(2)) if match.group(2) else 0
        if kind in ("alpha", "a"):
            return f"a{num}"
        if kind in ("beta", "b"):
            return f"b{num}"
        if kind == "dev":
            return f".dev{num}"
        return f"rc{num}"

    return _PRE_RELEASE_PATTERN.sub(_repl, s)


def _numeric_core(version: str) -> tuple[int, ...]:
    """Extract the leading dotted numeric segments from a tag.

    Used as a fallback for non-PEP 440 tags (e.g. ``1.0.1-build``) so we can
    still detect a numeric-core upgrade when ``packaging.version.Version``
    refuses to parse the input.
    """
    if not version:
        return ()
    s = version.lstrip("vV").split("+", 1)[0]
    match = re.match(r"\d+(?:\.\d+)*", s)
    if not match:
        return ()
    return tuple(int(p) for p in match.group(0).split("."))


def compare_versions_for_update(current: str, latest: str) -> bool:
    """Return True when ``latest`` should be offered as an update over ``current``.

    Uses PEP 440 ordering when both inputs normalize cleanly; otherwise falls
    back to comparing the leading dotted numeric core so non-PEP 440 tags
    (e.g. ``1.0.1-build``) still trigger updates when their numeric core
    increases.
    """
    try:
        return Version(normalize_version(latest)) > Version(normalize_version(current))
    except (InvalidVersion, TypeError, AttributeError):
        cur_core = _numeric_core(current) if isinstance(current, str) else ()
        lat_core = _numeric_core(latest) if isinstance(latest, str) else ()
        if not cur_core or not lat_core:
            return False
        return lat_core > cur_core


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

            # Create SSL context with certifi certificates for HTTPS
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)

            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
                connector=connector,
            )
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
    def is_newer_version(current: str, latest: str) -> bool:
        """
        Check if the latest version is newer than the current version.

        Pre-release tags (``-pre``, ``-alpha``, ``-beta``, ``-rc``, ``-dev``)
        rank lower than the matching release per PEP 440 / SemVer. Tags that
        are not PEP 440-compliant fall back to a numeric-core comparison.

        :param current: Current installed version
        :param latest: Latest available version
        :return: True if latest is newer than current
        """
        return compare_versions_for_update(current, latest)

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
