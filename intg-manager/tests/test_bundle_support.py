"""Integration bundles from bundle.unfolded.tools: detection, update checks and in-place updates."""

import asyncio
import copy
import os
import sys
from types import SimpleNamespace

import pytest
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bundle_service as bs  # noqa: E402
import web_server as ws  # noqa: E402

# What the Remote reports for a bundle built by the bundle builder (GET /intg/drivers/<id>).
BUNDLE_DRIVER = {
    "driver_id": "living_room",
    "version": "2026.10.1",
    "name": {"en": "Living room"},
    "driver_type": "CUSTOM",
    "description": {
        "en": "Integration bundle running 2 integrations as one:\n"
        "• Samsung TV 1.2.0 by JackJPowell – https://github.com/JackJPowell/uc-intg-samsungtv\n"
        "• My Gadget 0.1",
    },
    "developer": {"name": "jstnjx", "url": "https://bundle.unfolded.tools"},
    "home_page": "https://bundle.unfolded.tools",
    "setup_data_schema": {
        "title": {"en": "Integration bundle"},
        "settings": [
            {"id": "info", "label": {"en": "How it works"}, "field": {"label": {"value": {"en": "…"}}}},
            {
                "id": "bundle_integration",
                "label": {"en": "Integration to set up"},
                "field": {
                    "dropdown": {
                        "value": "samsungtv_driver",
                        "items": [
                            {"id": "samsungtv_driver", "label": {"en": "Samsung TV"}},
                            {"id": "mine", "label": {"en": "My Gadget"}},
                            {"id": "__migrate_in", "label": {"en": "↪ Move …"}},
                        ],
                    }
                },
            },
        ],
    },
}

PLAN = {
    "bundle": {"driver_id": "living_room", "name": "Living room", "version": "2026.10.1", "migration": True},
    "rows": [
        {"key": "samsungtv_driver", "name": "Samsung TV", "version": "1.2.0", "id": "uc-intg-samsungtv",
         "latest": "v1.3.0", "action": "update", "reason": ""},
        {"key": "mine", "name": "My Gadget", "version": "0.1", "id": "", "latest": "", "action": "keep",
         "reason": "Not from the integration registry (added as a file)."},
    ],
    "updates": 1,
    "updatable": True,
    "reason": "",
    "next_version": "2026.10.30",
    "site": "https://bundle.unfolded.tools",
}

REGISTRY = [
    {"id": "uc-intg-samsungtv", "name": "Samsung TV", "repository": "https://github.com/JackJPowell/uc-intg-samsungtv"},
    {"id": "uc-intg-other", "name": "Other", "repository": "https://github.com/x/uc-intg-other"},
]


def summary(driver):
    """GET /intg/drivers returns IntegrationDriverInfo summaries: no links, description or setup page."""
    info = {k: driver[k] for k in ("driver_id", "name", "driver_type", "version", "icon", "enabled") if k in driver}
    if isinstance(driver.get("developer"), dict):
        info["developer_name"] = driver["developer"].get("name", "")
    return info


class _API:
    def __init__(self, drivers, instances=()):
        self.drivers = {d["driver_id"]: d for d in drivers}
        self.instances = list(instances)
        self.driver_reads = []

    async def get_integrations(self):
        return self.instances

    async def get_drivers(self):
        return [summary(d) for d in self.drivers.values()]

    async def get_driver(self, driver_id):
        self.driver_reads.append(driver_id)
        return copy.deepcopy(self.drivers[driver_id])


def _client(api):
    return SimpleNamespace(
        api=api,
        device=SimpleNamespace(sw_version="2.9.3"),
        system=SimpleNamespace(flags=SimpleNamespace(inplace_upgrade_available=True)),
        settings=SimpleNamespace(localization=SimpleNamespace(language_code="en_US")),
    )


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(ws, "_bundle_driver_cache", {})
    api = _API(
        [BUNDLE_DRIVER, {"driver_id": "plain", "name": {"en": "Plain"}, "version": "1.0", "driver_type": "CUSTOM",
                         "developer": {"name": "x", "url": "https://github.com/x/plain"}}],
        [{"driver_id": "living_room", "integration_id": "living_room.main", "device_state": "CONNECTED",
          "configured_entities": ["a", "b"]}],
    )
    client = _client(api)
    monkeypatch.setitem(ws._remote_clients, "test-remote", client)
    monkeypatch.setattr(ws, "_remote_capabilities_ready", {"test-remote"})
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: client)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "load_registry", lambda: REGISTRY)
    monkeypatch.setitem(ws._cached_version_data, "test-remote", {})
    return api


def test_driver_data_describes_the_bundle():
    assert bs.is_bundle_driver(BUNDLE_DRIVER)
    assert not bs.is_bundle_driver(summary(BUNDLE_DRIVER)), "the driver list doesn't tell"
    assert not bs.looks_like_bundle(summary(BUNDLE_DRIVER))
    assert not bs.looks_like_bundle({"developer": {"url": "https://github.com/x/y"}})
    assert bs.looks_like_bundle({"developer": {"url": "https://github.com/jstnjx/uc-intg-bundle-v2"}})
    assert bs.bundle_children(BUNDLE_DRIVER) == [
        {"key": "samsungtv_driver", "name": "Samsung TV", "version": "1.2.0", "developer": "JackJPowell",
         "repository": "https://github.com/JackJPowell/uc-intg-samsungtv"},
        {"key": "mine", "name": "My Gadget", "version": "0.1", "developer": "", "repository": ""},
    ]
    assert bs.bundle_summary(BUNDLE_DRIVER) == "Integration bundle running 2 integrations as one"
    older = {**BUNDLE_DRIVER, "description": {"en": "Integration bundle running 2 integrations as one: Samsung TV, My Gadget."}}
    assert [c["version"] for c in bs.bundle_children(older)] == ["", ""]
    assert bs.parse_child_line(" v2 by A B – https://github.com/a/b") == {
        "version": "v2", "developer": "A B", "repository": "https://github.com/a/b"}


def test_installed_bundle_shows_its_integrations(remote):
    integrations = asyncio.run(ws._get_installed_integrations("test-remote"))
    bundle = next(i for i in integrations if i.driver_id == "living_room")
    plain = next(i for i in integrations if i.driver_id == "plain")
    assert plain.bundle is None
    assert sorted(remote.driver_reads) == ["living_room", "plain"], "custom drivers are read to find bundles"
    asyncio.run(ws._get_installed_integrations("test-remote"))
    assert len(remote.driver_reads) == 2, "once per installed version"
    assert [c["name"] for c in bundle.bundle] == ["Samsung TV", "My Gadget"]
    assert bundle.home_page == "https://bundle.unfolded.tools"
    assert bundle.description == "Integration bundle running 2 integrations as one"
    model = ws._integration_api_model(bundle)
    assert model["bundle"]["integrations"][0]["version"] == "1.2.0"
    assert model["bundle"]["checked"] is False
    assert model["capabilities"]["selectVersion"] is False
    assert ws._integration_api_model(plain)["bundle"] is None


def test_update_check_marks_the_bundle_and_its_integrations(remote, monkeypatch):
    sent = []

    async def check(driver):
        sent.append(driver)
        return PLAN

    notified = []

    class _NM:
        async def notify_integration_update_available(self, *args):
            notified.append(args)

    monkeypatch.setattr(ws._bundle_client, "check", check)
    monkeypatch.setattr(ws, "get_notification_manager", lambda _rid=None: _NM())
    monkeypatch.setattr(ws, "_github_client", SimpleNamespace())
    asyncio.run(ws._refresh_version_cache("test-remote"))
    assert sent[0]["setup_data_schema"]["settings"][1]["id"] == "bundle_integration"
    assert notified == [("living_room", "Living room", "2026.10.1", "2026.10.30")]

    bundle = next(i for i in asyncio.run(ws._get_installed_integrations("test-remote")) if i.driver_id == "living_room")
    assert bundle.update_available and bundle.can_update
    assert bundle.latest_version == "2026.10.30"
    model = ws._integration_api_model(bundle)
    first, second = model["bundle"]["integrations"]
    assert (first["latest"], first["updateAvailable"]) == ("v1.3.0", True)
    assert second["updateAvailable"] is False and "file" in second["note"]
    assert model["bundle"]["updates"] == 1 and model["bundle"]["checked"] is True
    assert model["capabilities"]["update"] is True

    # an integration the site can't rebuild: update shown, but not offered here
    blocked = {**PLAN, "updatable": False, "reason": "My Gadget can't be rebuilt automatically."}
    ws._cached_version_data["test-remote"]["living_room"]["bundle"] = blocked
    bundle = next(i for i in asyncio.run(ws._get_installed_integrations("test-remote")) if i.driver_id == "living_room")
    model = ws._integration_api_model(bundle)
    assert model["updateAvailable"] is True
    assert model["capabilities"]["update"] is False
    assert model["bundle"]["reason"].startswith("My Gadget")


def test_update_rebuilds_on_the_site_and_installs_in_place(remote, monkeypatch, tmp_path):
    monkeypatch.setenv("UC_DATA_HOME", str(tmp_path))
    installs = []

    class _Archive:
        size = 123
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

        async def install(self, api, *, update=False):
            installs.append((api, update))

    archive = _Archive()

    async def build_update(driver, force=False):
        assert driver["driver_id"] == "living_room"
        return PLAN, archive

    async def no_conflict(*_args):
        return None

    async def no_refresh(_remote_id=None):
        return None

    monkeypatch.setattr(ws._bundle_client, "build_update", build_update)
    monkeypatch.setattr(ws, "_github_client", SimpleNamespace())
    monkeypatch.setattr(ws, "_try_acquire_operation_lock", no_conflict)
    monkeypatch.setattr(ws, "_release_operation_lock", no_conflict)
    monkeypatch.setattr(ws, "_refresh_version_cache", no_refresh)

    async def request(path):
        async with ws.app.test_request_context(path, method="POST"):
            result = await ws.update_integration_inplace("living_room")
            response, status = result if isinstance(result, tuple) else (result, 200)
            return status, await response.get_json()

    status, payload = asyncio.run(request("/api/v1/integrations/living_room/update?version=v1.0.0"))
    assert status == 400 and payload["error"]["code"] == "version_unavailable"
    assert installs == []

    status, payload = asyncio.run(request("/api/v1/integrations/living_room/update"))
    assert status == 200, payload
    assert payload["data"]["reconnecting"] is True
    assert installs == [(remote, True)] and archive.closed

    async def failing(driver, force=False):
        raise bs.BundleError("My Gadget can't be rebuilt automatically.")

    monkeypatch.setattr(ws._bundle_client, "build_update", failing)
    status, payload = asyncio.run(request("/api/v1/integrations/living_room/update"))
    assert status == 502 and "My Gadget" in payload["error"]["message"]


def test_catalog_shows_which_bundle_holds_an_integration(remote, monkeypatch):
    monkeypatch.setattr(ws, "_github_client", None)
    monkeypatch.setattr(ws, "get_notification_manager", lambda _rid=None: SimpleNamespace(
        update_registry_count=lambda _data: [], notify_new_integration_in_registry=None))
    items = {i.catalog_id: i for i in asyncio.run(ws._get_available_integrations("test-remote"))}
    assert items["uc-intg-samsungtv"].bundled_in == "Living room"
    assert items["uc-intg-other"].bundled_in == ""
    assert ws._integration_api_model(items["uc-intg-samsungtv"])["bundledIn"] == "Living room"


def test_client_builds_waits_and_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(bs, "POLL_INTERVAL", 0.01)
    seen = {"polls": 0}

    async def update(request):
        body = await request.json()
        seen["driver"] = body["driver"]
        return web.json_response({**PLAN, "job": "0-abc"})

    async def status(request):
        seen["polls"] += 1
        state = "done" if seen["polls"] >= 3 else "running"
        return web.json_response({"state": state, "result": {"file": "living_room.tar.gz"}})

    async def download(request):
        return web.Response(body=b"bundle" * 1000, content_type="application/gzip")

    async def refused(request):
        return web.json_response({"error": "Every integration in this bundle is up to date."}, status=409)

    async def run():
        app = web.Application()
        app.router.add_post("/api/bundle/update", update)
        app.router.add_get("/api/jobs/0-abc", status)
        app.router.add_get("/api/jobs/0-abc/download", download)
        app.router.add_post("/api/bundle/check", refused)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = bs.BundleClient(f"http://127.0.0.1:{port}")
        try:
            plan, archive = await client.build_update({**BUNDLE_DRIVER, "token": "secret"})
            with archive:
                assert archive.filename == "living_room.tar.gz"
                assert archive.path.read_bytes() == b"bundle" * 1000
            assert not archive.path.exists()
            assert plan["updates"] == 1
            assert "token" not in seen["driver"], "only the bundle's own fields are sent"
            with pytest.raises(bs.BundleError, match="up to date"):
                await client.check(BUNDLE_DRIVER)
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert seen["polls"] == 3
