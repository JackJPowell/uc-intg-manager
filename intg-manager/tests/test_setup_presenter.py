"""Tests for the Manager-specific setup REST presenter."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setup_presenter import SetupPresenter, setup_api_error  # noqa: E402
from unfurled import (  # noqa: E402
    HTTPError,
    InputSetupAction,
    IntegrationEntity,
    InvalidEntitySelection,
    LocalizedText,
    SetupField,
    SetupPage,
    SetupResult,
    SetupState,
    SetupTimeout,
)


def test_presenter_localizes_typed_models_to_the_existing_spa_contract():
    presenter = SetupPresenter("de_DE")
    page = SetupPage(
        LocalizedText({"en": "Setup", "de": "Einrichtung"}),
        (
            SetupField(
                "host",
                LocalizedText({"en": "Host", "de": "Server"}),
                "text",
                "remote.local",
                regex=".+",
            ),
        ),
    )
    result = SetupResult(
        "demo",
        SetupState.WAIT_USER_ACTION,
        action=InputSetupAction(page=page),
    )
    entity = IntegrationEntity(
        "device-1",
        "media_player",
        LocalizedText({"en": "Kitchen", "de": "Küche"}),
        LocalizedText({"en": "TV", "de": "Fernseher"}),
        area="Kitchen",
        device_class="tv",
        icon="mdi:television",
        features=("on_off",),
    )

    assert presenter.result(result) == {
        "id": "demo",
        "state": "WAIT_USER_ACTION",
        "error": "NONE",
        "action": {
            "type": "input",
            "page": {
                "title": "Einrichtung",
                "fields": [
                    {
                        "id": "host",
                        "label": "Server",
                        "type": "text",
                        "value": "remote.local",
                        "regex": ".+",
                    }
                ],
            },
        },
    }
    assert presenter.entity(entity) == {
        "id": "device-1",
        "type": "media_player",
        "name": "Küche",
        "description": "Fernseher",
        "area": "Kitchen",
        "deviceClass": "tv",
        "icon": "mdi:television",
        "features": ["on_off"],
    }


def test_presenter_maps_library_errors_to_the_manager_error_contract():
    assert setup_api_error(HTTPError(422, "Invalid input")).code == "core_http_422"
    assert setup_api_error(HTTPError(422, "Invalid input")).status == 422

    entity_error = setup_api_error(InvalidEntitySelection("Select an entity"))
    assert entity_error.code == "entity_selection_required"
    assert entity_error.status == 400

    timeout_error = setup_api_error(SetupTimeout("No update"))
    assert timeout_error.code == "setup_timeout"
    assert timeout_error.status == 504
