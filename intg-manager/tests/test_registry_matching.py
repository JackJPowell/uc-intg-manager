"""Registry identity matching must not alias similarly named integrations."""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402
from unfurled import (  # noqa: E402
    IntegrationSetupDefinition,
    LocalizedText,
    SetupField,
    SetupNotFound,
    SetupPage,
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


def test_catalog_keeps_similarly_named_entries_distinct(monkeypatch):
    monkeypatch.setattr(ws, "load_registry", lambda: APPLE_TV_REGISTRY)
    monkeypatch.setitem(ws._remote_clients, "test-remote", _RemoteClient())

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
        settings=SimpleNamespace(
            localization=SimpleNamespace(language_code="de_DE")
        )
    )
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: remote)

    assert ws._active_remote_locale() == "de_DE"
