"""
Integration bundles from bundle.unfolded.tools.

A bundle is one custom integration that runs several integrations. The Remote reports it like any
other driver; what it contains is in two standard driver fields written by the bundle builder:

- ``setup_data_schema``: the dropdown ``bundle_integration`` lists each bundled integration as
  ``{"id": <key in the bundle>, "label": {"en": <name>}}`` (plus ``__migrate_*`` entries).
- ``description["en"]``: one line per integration,
  ``• <name> <version> by <developer> – <repository url>`` (unknown parts left out).

Updates are built by bundle.unfolded.tools from the Remote's driver data:

- ``POST /api/bundle/check  {"driver": ...}``  which bundled integrations have a newer release
- ``POST /api/bundle/update {"driver": ...}``  rebuilds the bundle (same driver ID and integration keys,
  so configuration and entities stay) and returns a build job
- ``GET /api/jobs/<job>`` until ``state`` is ``done``, then ``GET /api/jobs/<job>/download``

The new archive is installed in place like any other update.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import re
import ssl
import time
from typing import Any

import aiohttp
import certifi
from sync_api import DOWNLOAD_TIMEOUT, REQUEST_TIMEOUT, DownloadedArchive

_LOG = logging.getLogger(__name__)

BUNDLE_SITE = os.environ.get("UC_BUNDLE_SITE", "https://bundle.unfolded.tools").rstrip("/")
SETUP_CHOICE_FIELD = "bundle_integration"
BUILD_TIMEOUT = 15 * 60  # seconds; builds usually take under two minutes
POLL_INTERVAL = 3.0

# Bundles link to the bundle site; bundles built before it existed link the project's repository.
_BUNDLE_URL = re.compile(
    r"^https?://(bundle\.unfolded\.tools(/|$)|github\.com/jstnjx/uc-intg-bundle)",
    re.IGNORECASE,
)


class BundleError(Exception):
    """A bundle can't be checked or updated; the message is meant for the user."""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("en"), str):
            return value["en"]
        for item in value.values():
            if isinstance(item, str):
                return item
    return ""


def _choices(driver: dict[str, Any]) -> list[dict[str, Any]] | None:
    schema = driver.get("setup_data_schema")
    settings = schema.get("settings") if isinstance(schema, dict) else None
    for setting in settings if isinstance(settings, list) else []:
        if isinstance(setting, dict) and setting.get("id") == SETUP_CHOICE_FIELD:
            field = setting.get("field")
            dropdown = field.get("dropdown") if isinstance(field, dict) else None
            items = dropdown.get("items") if isinstance(dropdown, dict) else None
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return None


def looks_like_bundle(driver: dict[str, Any]) -> bool:
    """Cheap test on driver summary data (links only); confirm with is_bundle_driver()."""
    developer = driver.get("developer")
    urls = [driver.get("home_page"), developer.get("url") if isinstance(developer, dict) else None]
    return any(isinstance(url, str) and _BUNDLE_URL.match(url) for url in urls)


def is_bundle_driver(driver: dict[str, Any]) -> bool:
    """A driver built by the bundle builder (needs the full driver data with setup_data_schema)."""
    return isinstance(driver, dict) and looks_like_bundle(driver) and _choices(driver) is not None


def parse_child_line(rest: str) -> dict[str, str]:
    """The part after the name: ``1.2.0 by Someone – https://…`` → version, developer, repository."""
    text = rest.strip()
    repository = developer = ""
    match = re.search(r"(?:^|\s)[–-]\s*(https?://\S+)\s*$", text)
    if match:
        repository = match.group(1)
        text = text[: match.start()].strip()
    match = re.search(r"(?:^|\s)by\s+(.+)$", text)
    if match:
        developer = match.group(1).strip()
        text = text[: match.start()].strip()
    return {"version": text.split()[0] if text else "", "developer": developer, "repository": repository}


def bundle_children(driver: dict[str, Any]) -> list[dict[str, str]]:
    """The bundled integrations: key, name, version, developer, repository (from the driver data)."""
    lines = [
        line.strip().lstrip("•").strip()
        for line in _text(driver.get("description")).splitlines()
        if line.strip().startswith("•")
    ]
    children = []
    for item in _choices(driver) or []:
        key = item.get("id")
        if not isinstance(key, str) or key.startswith("__migrate"):
            continue
        name = _text(item.get("label")) or key
        line = next((ln for ln in lines if ln == name or ln.startswith(name + " ")), None)
        parsed = parse_child_line(line[len(name):]) if line else {"version": "", "developer": "", "repository": ""}
        children.append({"key": key, "name": name, **parsed})
    return children


def bundle_summary(driver: dict[str, Any]) -> str:
    """The description without the per-integration lines (they are shown as a list)."""
    first = _text(driver.get("description")).splitlines()
    return first[0].rstrip(":").strip() if first else ""


class BundleClient:
    """Async client for the bundle site."""

    def __init__(self, base_url: str = BUNDLE_SITE) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"User-Agent": "uc-intg-manager", "Accept": "application/json"}

    def _session(self, timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT) -> aiohttp.ClientSession:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        return aiohttp.ClientSession(
            headers=self._headers, timeout=timeout, connector=aiohttp.TCPConnector(ssl=ssl_context)
        )

    async def _post(self, path: str, body: dict[str, Any], *, ok: tuple[int, ...] = (200,)) -> tuple[int, dict]:
        async with self._session() as session:
            try:
                async with session.post(f"{self.base_url}{path}", json=body) as response:
                    data = await response.json(content_type=None)
                    if response.status not in ok:
                        raise BundleError(_error_message(data, response.status))
                    return response.status, data
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                raise BundleError(f"Couldn't reach {self.base_url}: {error}") from error

    async def check(self, driver: dict[str, Any]) -> dict[str, Any]:
        """Which bundled integrations have a newer release (see module docstring)."""
        return (await self._post("/api/bundle/check", {"driver": _driver_payload(driver)}))[1]

    async def start_update(self, driver: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """Start rebuilding the bundle; returns the plan with ``job``."""
        body = {"driver": _driver_payload(driver), **({"force": True} if force else {})}
        return (await self._post("/api/bundle/update", body))[1]

    async def wait_for_build(self, job: str, *, timeout: float = BUILD_TIMEOUT) -> dict[str, Any]:
        """Poll the build until it's done; raises BundleError when it fails."""
        deadline = time.monotonic() + timeout
        errors = 0
        async with self._session() as session:
            while True:
                try:
                    async with session.get(f"{self.base_url}/api/jobs/{job}") as response:
                        data = await response.json(content_type=None)
                        if response.status != 200:
                            raise BundleError(_error_message(data, response.status))
                    errors = 0
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    errors += 1
                    if errors >= 5:
                        raise BundleError(f"Lost contact with {self.base_url}: {error}") from error
                    data = {}
                state = data.get("state")
                if state == "done":
                    return data
                if state == "error":
                    raise BundleError(data.get("error") or "The bundle build failed.")
                if time.monotonic() > deadline:
                    raise BundleError("The bundle build took too long.")
                await asyncio.sleep(POLL_INTERVAL)

    async def download(self, job: str, filename: str) -> DownloadedArchive:
        """Stream the finished bundle to disk. The caller must close the returned archive."""
        async with self._session(DOWNLOAD_TIMEOUT) as session:
            try:
                async with session.get(
                    f"{self.base_url}/api/jobs/{job}/download", headers={"Accept": "application/gzip"}
                ) as response:
                    if response.status != 200:
                        data = await response.json(content_type=None)
                        raise BundleError(_error_message(data, response.status))
                    archive = DownloadedArchive(filename)
                    try:
                        with archive.path.open("wb") as destination:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                destination.write(chunk)
                                archive.size += len(chunk)
                        return archive
                    except BaseException:
                        archive.cleanup()
                        raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise BundleError(f"Couldn't download the bundle: {error}") from error

    async def build_update(self, driver: dict[str, Any], *, force: bool = False) -> tuple[dict, DownloadedArchive]:
        """Rebuild the bundle with the latest releases and download it: (plan, archive)."""
        plan = await self.start_update(driver, force=force)
        job = plan.get("job")
        if not isinstance(job, str) or not job:
            raise BundleError("The bundle site didn't start a build.")
        _LOG.info("Bundle %s: building update (job %s)", driver.get("driver_id"), job)
        status = await self.wait_for_build(job)
        result = status.get("result") or {}
        filename = result.get("file") or f"{driver.get('driver_id', 'bundle')}.tar.gz"
        return plan, await self.download(job, filename)


def _driver_payload(driver: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the bundle site reads (no tokens or other Remote data)."""
    keys = ("driver_id", "name", "version", "description", "developer", "home_page", "setup_data_schema")
    return {key: driver[key] for key in keys if key in driver}


def _error_message(data: Any, status: int) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return f"The bundle site answered HTTP {status}."
