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
    MANAGER_DATA_FILE,
    REPO_CACHE_VALIDITY,
)
from github_api import compare_versions_for_update

_LOG = logging.getLogger(__name__)

# Default timeout for all requests (connect, read) in seconds
REQUEST_TIMEOUT = aiohttp.ClientTimeout(connect=10, total=40)
# Longer timeout for file downloads (30s connect, 5min total)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(connect=30, total=330)
# Sync timeout tuple kept for requests-based uses (fetch_repository_batch)
_SYNC_REQUEST_TIMEOUT = (10, 30)

# In-memory registry cache to avoid blocking the event loop on every request
_registry_cache: dict[str, Any] | list | None = None
_registry_cache_time: float = 0.0
_REGISTRY_CACHE_TTL = 1800  # 30 minutes


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
