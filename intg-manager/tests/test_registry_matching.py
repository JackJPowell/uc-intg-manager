"""Registry identity matching must not alias similarly named integrations."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402
from unfurled import (  # noqa: E402
    IntegrationSetupDefinition,
    LocalizedText,
    SetupField,
    SetupNotFound,
    SetupPage,
    SetupTimeout,
)


APPLE_TV_REGISTRY = [
    {
        "id": "appletv-siri",
        "name": "Apple TV Siri Voice",
        "description": "Apple TV Siri Voice integration for Unfolded Circle Remotes",
        "author": "albaintor",
        "repository": "https://github.com/albaintor/appletv-siri",
        "categories": ["voice-assistant"],
        "custom": True,
        "driver_id": "appletv_siri_integration",
    },
    {
        "id": "uc-intg-appletv",
        "name": "Apple TV",
        "description": "Integration for Apple TV devices",
        "author": "Unfolded Circle",
        "repository": "https://github.com/unfoldedcircle/integration-appletv",
        "categories": ["media-player", "streaming"],
        "custom": False,
    },
]


class _RemoteAPI:
    async def get_drivers(self):
        return [
            {
                "driver_id": "uc_intg_appletv",
                "driver_type": "LOCAL",
                "version": "1.0.0",
                "name": {"en": "Apple TV"},
                "developer": {"name": "Unfolded Circle"},
            }
        ]

    async def get_integrations(self):
        return [
            {
                "driver_id": "uc_intg_appletv",
                "integration_id": "apple-tv-instance",
                "device_state": "CONNECTED",
                "configured_entities": [],
            }
        ]


class _RemoteClient:
    api = _RemoteAPI()


def test_registry_metadata_prefers_canonical_ids_over_partial_name_matches():
    match = ws._registry_item_for_driver(
        APPLE_TV_REGISTRY, "uc_intg_appletv", "Apple TV"
    )

    assert match["id"] == "uc-intg-appletv"
    assert match["author"] == "Unfolded Circle"


def test_inplace_update_rejects_manual_version_at_migration_boundary(monkeypatch):
    integration = ws.IntegrationInfo(
        instance_id="demo_driver.main",
        driver_id="demo_driver",
        name="Demo",
        version="1.3.0",
        home_page="https://github.com/example/demo",
    )

    async def installed(_remote_id):
        return [integration]

    async def no_conflict(_operation, _remote_id):
        return None

    class _GitHub:
        async def download_release_asset(self, *_args, **_kwargs):
            raise AssertionError("An incompatible version must not be downloaded")

    client = SimpleNamespace(
        api=SimpleNamespace(),
        device=SimpleNamespace(sw_version="2.9.3"),
        system=SimpleNamespace(flags=SimpleNamespace(inplace_upgrade_available=True)),
    )
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: client)
    monkeypatch.setitem(ws._remote_clients, "test-remote", client)
    monkeypatch.setattr(ws, "_remote_capabilities_ready", {"test-remote"})
    monkeypatch.setattr(ws, "_github_client", _GitHub())
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "_get_installed_integrations", installed)
    monkeypatch.setattr(ws, "_try_acquire_operation_lock", no_conflict)
    monkeypatch.setattr(ws, "_release_operation_lock", no_conflict)
    monkeypatch.setattr(
        ws,
        "load_registry",
        lambda: [{"id": "demo_driver", "migration_required_at": "1.2.0"}],
    )

    async def request_update():
        async with ws.app.test_request_context(
            "/api/v1/integrations/demo_driver/update?version=v1.2.0", method="POST"
        ):
            response = await ws.update_integration_inplace("demo_driver")
            return response[1], await response[0].get_json()

    status, payload = asyncio.run(request_update())

    assert status == 400
    assert payload["error"]["code"] == "migration_required"


def test_inplace_upgrade_flag_blocks_update_without_hiding_availability(monkeypatch):
    client = SimpleNamespace(
        api=SimpleNamespace(),
        device=SimpleNamespace(sw_version="2.9.2"),
        system=SimpleNamespace(flags=SimpleNamespace(inplace_upgrade_available=False)),
    )
    monkeypatch.setitem(ws._remote_clients, "test-remote", client)
    monkeypatch.setattr(ws, "_remote_capabilities_ready", {"test-remote"})
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: client)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "_github_client", object())
    integration = ws.IntegrationInfo(
        instance_id="demo.main",
        driver_id="demo",
        name="Demo",
        version="1.0.0",
        latest_version="1.1.0",
        update_available=True,
        can_update=True,
    )

    model = ws._integration_api_model(integration)
    assert model["updateAvailable"] is True
    assert model["capabilities"]["update"] is True
    assert model["inplaceUpgradeAvailable"] is False
    assert model["remoteFirmwareVersion"] == "2.9.2"

    async def request_update():
        async with ws.app.test_request_context(
            "/api/v1/integrations/demo/update", method="POST"
        ):
            response, status = await ws.update_integration_inplace("demo")
            return status, await response.get_json()

    status, payload = asyncio.run(request_update())
    assert status == 409
    assert payload["error"]["code"] == "inplace_upgrade_unavailable"


def test_unknown_firmware_capability_blocks_self_update(monkeypatch):
    client = SimpleNamespace(
        api=SimpleNamespace(),
        device=SimpleNamespace(sw_version="N/A"),
        system=SimpleNamespace(flags=SimpleNamespace(inplace_upgrade_available=False)),
    )
    monkeypatch.setitem(ws._remote_clients, "test-remote", client)
    monkeypatch.setitem(ws._remote_online, "test-remote", True)
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: client)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "_github_client", object())

    async def request_update():
        async with ws.app.test_request_context(
            "/api/v1/self-update/inplace", method="POST", json={"version": "2.1.0"}
        ):
            response, status = await ws.self_update_inplace()
            return status, await response.get_json()

    status, payload = asyncio.run(request_update())
    assert status == 409
    assert payload["error"]["code"] == "inplace_upgrade_unavailable"


def test_remote_initialization_warns_when_firmware_lacks_upgrade_flag(
    caplog, monkeypatch
):
    async def request(*_args, **_kwargs):
        return {"os": "2.9.2"}

    async def init():
        client.device.sw_version = "2.9.2"
        client.system.flags.inplace_upgrade_available = False

    async def refresh_localization():
        return SimpleNamespace(language_code="en_GB")

    client = SimpleNamespace(
        api=SimpleNamespace(request=request),
        init=init,
        device=SimpleNamespace(sw_version="N/A"),
        system=SimpleNamespace(flags=SimpleNamespace(inplace_upgrade_available=False)),
        settings=SimpleNamespace(refresh_localization=refresh_localization),
    )
    monkeypatch.setitem(ws._remote_clients, "test-remote", client)
    monkeypatch.setattr(ws, "_remote_capabilities_ready", set())
    asyncio.run(ws._initialize_remote("test-remote", client))

    assert "2.9.3 or newer (enable beta updates)" in caplog.text
    assert "Integration Manager v2.0.6" in caplog.text


def test_failed_remote_initialization_keeps_upgrade_support_unknown(monkeypatch):
    async def request(*_args, **_kwargs):
        return {"os": "2.9.3"}

    async def init():
        raise RuntimeError("Remote initialization failed")

    async def refresh_localization():
        return SimpleNamespace(language_code="en_GB")

    client = SimpleNamespace(
        api=SimpleNamespace(request=request),
        init=init,
        device=SimpleNamespace(sw_version="2.9.3"),
        system=SimpleNamespace(flags=SimpleNamespace(inplace_upgrade_available=False)),
        settings=SimpleNamespace(refresh_localization=refresh_localization),
    )
    monkeypatch.setitem(ws._remote_clients, "test-remote", client)
    monkeypatch.setattr(ws, "_remote_capabilities_ready", set())
    asyncio.run(ws._initialize_remote("test-remote", client))

    assert ws._inplace_upgrade_available("test-remote") is None


def test_automatic_updates_skip_unsupported_firmware(monkeypatch):
    monkeypatch.setattr(
        ws.Settings, "load", lambda **_kwargs: SimpleNamespace(auto_update=True)
    )
    monkeypatch.setattr(ws, "is_remote_online", lambda _remote_id: True)

    async def installed(_remote_id):
        raise AssertionError("Automatic updates must stop before loading integrations")

    monkeypatch.setattr(ws, "_get_installed_integrations", installed)
    asyncio.run(ws._run_automatic_updates("test-remote"))


def test_catalog_keeps_similarly_named_entries_distinct(monkeypatch):
    monkeypatch.setattr(ws, "load_registry", lambda: APPLE_TV_REGISTRY)
    monkeypatch.setitem(ws._remote_clients, "test-remote", _RemoteClient())
    ws.set_remote_online("test-remote", True)

    try:
        catalog = asyncio.run(ws._get_available_integrations("test-remote"))
        by_catalog_id = {item.catalog_id: item for item in catalog}

        assert set(by_catalog_id) == {"appletv-siri", "uc-intg-appletv"}
        assert by_catalog_id["appletv-siri"].driver_installed is False
        assert by_catalog_id["uc-intg-appletv"].driver_id == "uc_intg_appletv"
        assert by_catalog_id["uc-intg-appletv"].developer == "Unfolded Circle"

        installed = asyncio.run(ws._get_installed_integrations("test-remote"))
        assert installed[0].developer == "Unfolded Circle"
    finally:
        ws._remote_clients.pop("test-remote", None)
        ws._remote_online.pop("test-remote", None)


@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("registry_custom", [True, False])
def test_external_update_is_visible_without_manager_update_capability(
    monkeypatch, configured, registry_custom
):
    class _ExternalAPI:
        async def get_drivers(self):
            return [
                {
                    "driver_id": "docker_demo",
                    "driver_type": "EXTERNAL",
                    "version": "3.0.0",
                    "name": {"en": "Docker Demo"},
                    "developer": {"name": "Example", "url": "https://github.com/example/demo"},
                }
            ]

        async def get_integrations(self):
            return [
                {
                    "driver_id": "docker_demo",
                    "integration_id": "docker-demo.main",
                    "device_state": "UNKNOWN",
                    "configured_entities": [],
                }
            ] if configured else []

    monkeypatch.setitem(
        ws._remote_clients, "test-remote", SimpleNamespace(api=_ExternalAPI())
    )
    monkeypatch.setitem(ws._remote_online, "test-remote", True)
    monkeypatch.setitem(
        ws._cached_version_data,
        "test-remote",
        {"docker_demo": {"has_update": True, "latest": "v3.1.0"}},
    )
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "_github_client", None)
    monkeypatch.setattr(
        ws,
        "load_registry",
        lambda: [
            {
                "id": "docker-demo",
                "driver_id": "docker_demo",
                "name": "Docker Demo",
                "repository": "https://github.com/example/demo",
                "custom": registry_custom,
            }
        ],
    )

    installed = asyncio.run(ws._get_installed_integrations("test-remote"))
    catalog = asyncio.run(ws._get_available_integrations("test-remote"))

    for integration in (installed[0], catalog[0]):
        model = ws._integration_api_model(integration)
        assert model["management"] == "external"
        assert model["updateAvailable"] is True
        assert model["latestVersion"] == "v3.1.0"
        assert model["capabilities"]["update"] is False
        assert model["capabilities"]["install"] is False


def test_setup_route_uses_the_existing_remote_setup_api(monkeypatch):
    """The Manager serializes typed setup data; it does not proxy Core itself."""

    class _SetupSession:
        async def status(self):
            raise SetupNotFound("No setup is active")

    class _Integrations:
        def __init__(self):
            self.setup_calls = []

        def setup(self, driver_id, instance_id=None):
            self.setup_calls.append((driver_id, instance_id))
            return _SetupSession()

        @staticmethod
        async def get_setup_definition(driver_id):
            return IntegrationSetupDefinition(
                driver_id,
                LocalizedText({"en": "Demo"}),
                SetupPage(
                    LocalizedText({"en": "Initial setup"}),
                    (
                        SetupField(
                            "host",
                            LocalizedText({"en": "Host"}),
                            "text",
                            "remote.local",
                        ),
                    ),
                ),
            )

    class _Remote:
        def __init__(self):
            self.integrations = _Integrations()
            self.settings = SimpleNamespace(
                localization=SimpleNamespace(language_code="en_GB")
            )

    remote = _Remote()
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: remote)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "is_remote_online", lambda _remote_id: True)

    async def request_setup():
        async with ws.app.test_request_context(
            "/api/v1/integrations/demo/setup", method="GET"
        ):
            response = await ws.api_v1_integration_setup("demo")
            return await response.get_json()

    result = asyncio.run(request_setup())

    assert result == {
        "data": {
            "driverId": "demo",
            "driverName": "Demo",
            "setupDataSchema": {
                "title": "Initial setup",
                "fields": [
                    {
                        "id": "host",
                        "label": "Host",
                        "type": "text",
                        "value": "remote.local",
                        "regex": None,
                    }
                ],
            },
            "activeSetup": None,
        }
    }
    assert remote.integrations.setup_calls == [("demo", None)]


def test_active_remote_locale_comes_from_unfurled_settings(monkeypatch):
    remote = SimpleNamespace(
        settings=SimpleNamespace(localization=SimpleNamespace(language_code="de_DE"))
    )
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: remote)

    assert ws._active_remote_locale() == "de_DE"


def test_setup_status_uses_unfurled_long_polling_and_surfaces_timeout(monkeypatch):
    class _SetupSession:
        def __init__(self):
            self.calls = []

        async def wait_for_update(self):
            self.calls.append(True)
            raise SetupTimeout("No setup update yet")

    class _Integrations:
        def __init__(self):
            self.session = _SetupSession()

        def setup(self, driver_id, instance_id=None):
            assert (driver_id, instance_id) == ("demo", None)
            return self.session

    remote = SimpleNamespace(
        integrations=_Integrations(),
        settings=SimpleNamespace(localization=SimpleNamespace(language_code="en_GB")),
    )
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: remote)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "test-remote")
    monkeypatch.setattr(ws, "is_remote_online", lambda _remote_id: True)

    async def request_status():
        async with ws.app.test_request_context(
            "/api/v1/integrations/demo/setup/status", method="GET"
        ):
            response, status = await ws.api_v1_integration_setup_status("demo")
            return status, await response.get_json()

    status, payload = asyncio.run(request_status())
    assert status == 504
    assert payload == {
        "error": {"code": "setup_timeout", "message": "No setup update yet"}
    }
    assert remote.integrations.session.calls == [True]
