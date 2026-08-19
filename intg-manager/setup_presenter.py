"""Manager REST presentation for typed :mod:`unfurled` setup models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unfurled.helpers.exceptions import (
    HTTPError,
    IntegrationInstanceAmbiguous,
    IntegrationNotFound,
    InvalidEntitySelection,
    SetupNotFound,
    SetupTimeout,
    UnfurledError,
)
from unfurled.setup import (
    IntegrationEntity,
    LocalizedText,
    SetupPage,
    SetupResult,
)


@dataclass(frozen=True)
class SetupApiError:
    """A Manager API error derived from a library-level setup exception."""

    code: str
    message: str
    status: int


class SetupPresenter:
    """Serialize unfurled setup models to the Manager SPA contract."""

    def __init__(self, locale: str) -> None:
        self._locale = locale

    def text(self, value: LocalizedText, fallback: str = "") -> str:
        """Display a Core localized value using the active Remote locale."""
        return value.text(self._locale, fallback)

    def page(self, page: SetupPage | None) -> dict[str, Any] | None:
        """Serialize a typed settings page for the existing SPA contract."""
        if page is None:
            return None
        fields: list[dict[str, Any]] = []
        for field in page.fields:
            item: dict[str, Any] = {
                "id": field.id,
                "label": self.text(field.label, field.id),
                "type": field.kind,
            }
            if field.kind == "label":
                item["text"] = (
                    self.text(field.value)
                    if isinstance(field.value, LocalizedText)
                    else str(field.value or "")
                )
            elif field.kind != "unknown":
                item["value"] = field.value
                if field.kind == "number":
                    item.update(
                        {
                            "min": field.minimum,
                            "max": field.maximum,
                            "step": field.step,
                            "decimals": field.decimals or 0,
                            "unit": self.text(field.unit),
                        }
                    )
                if field.kind in {"text", "password"}:
                    item["regex"] = field.regex
                if field.kind == "dropdown":
                    item["items"] = [
                        {"id": option.id, "label": self.text(option.label, option.id)}
                        for option in field.options
                    ]
            fields.append(item)
        return {
            "title": self.text(page.title, "Integration setup"),
            "fields": fields,
        }

    def result(self, result: SetupResult) -> dict[str, Any]:
        """Serialize a typed setup result, including its optional user action."""
        action = result.action
        action_model: dict[str, Any] | None = None
        if action and action.kind == "input":
            action_model = {"type": "input", "page": self.page(action.page)}
        elif action and action.kind == "confirmation":
            action_model = {
                "type": "confirmation",
                "title": self.text(action.title, "Confirmation required"),
                "message1": self.text(action.message1),
                "message2": self.text(action.message2),
                "image": action.image,
            }
        return {
            "id": result.setup_id or result.driver_id,
            "state": str(result.state),
            "error": result.error,
            "action": action_model,
        }

    def entity(self, entity: IntegrationEntity) -> dict[str, Any]:
        """Serialize an integration entity for the entity-picker response."""
        return {
            "id": entity.id,
            "type": entity.entity_type,
            "name": self.text(entity.name, entity.id),
            "description": self.text(entity.description),
            "area": entity.area,
            "deviceClass": entity.device_class,
            "icon": entity.icon,
            "features": list(entity.features),
        }


def setup_api_error(error: Exception) -> SetupApiError:
    """Map library exceptions to Manager's stable REST error envelope."""
    if isinstance(error, HTTPError):
        return SetupApiError(
            f"core_http_{error.status_code}", error.message, error.status_code
        )
    if isinstance(error, SetupNotFound):
        return SetupApiError("setup_not_found", str(error), 404)
    if isinstance(error, SetupTimeout):
        return SetupApiError("setup_timeout", str(error), 504)
    if isinstance(error, IntegrationNotFound):
        return SetupApiError("integration_instance_not_found", str(error), 404)
    if isinstance(error, IntegrationInstanceAmbiguous):
        return SetupApiError("integration_instance_ambiguous", str(error), 409)
    if isinstance(error, InvalidEntitySelection):
        return SetupApiError("entity_selection_required", str(error), 400)
    return SetupApiError(
        "setup_failed", str(error), 502 if isinstance(error, UnfurledError) else 500
    )
