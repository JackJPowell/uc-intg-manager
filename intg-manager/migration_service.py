"""
Integration Migration Service.

This module provides functionality to handle entity ID migrations when
upgrading integrations that change entity IDs.

The migration flow:
1. Check for migration_possible metadata in initial setup response
2. If present and previous_version exists, execute migration flow after restore
3. Extract migration_mappings from migration setup response
4. Update configured entity IDs with mappings
5. Execute migration to update Remote's activities

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
from typing import Any

_LOG = logging.getLogger(__name__)


def extract_migration_mappings(setup_response: dict[str, Any]) -> list[dict[str, str]]:
    """
    Extract migration mappings from a setup response.

    Expected structure (after migration completes):
    {
        "require_user_action": {
            "input": {
                "settings": [
                    {
                        "id": "migration_data",
                        "field": {
                            "textarea": {
                                "value": "{JSON string with entity_mappings}"
                            }
                        }
                    }
                ]
            }
        }
    }

    The JSON string contains:
    {
        "entity_mappings": [
            {"previous_entity_id": "...", "new_entity_id": "..."},
            ...
        ]
    }

    :param setup_response: The response from setup flow
    :return: List of mapping dicts with previous_entity_id and new_entity_id
    """
    _LOG.debug("Extracting migration mappings from setup response")
    try:
        settings = (
            setup_response.get("require_user_action", {})
            .get("input", {})
            .get("settings", [])
        )
        _LOG.debug("Looking for migration_data in %d settings fields", len(settings))
        for setting in settings:
            if setting.get("id") == "migration_data":
                # The value is a JSON string in a textarea field
                textarea_value = (
                    setting.get("field", {}).get("textarea", {}).get("value", "")
                )
                _LOG.debug(
                    "Found migration_data field with textarea value: %s", textarea_value
                )

                if textarea_value:
                    try:
                        # Parse the JSON string
                        migration_data = json.loads(textarea_value)
                        _LOG.debug("Parsed migration data: %s", migration_data)

                        # Extract entity_mappings from the parsed data
                        entity_mappings = migration_data.get("entity_mappings", [])
                        if isinstance(entity_mappings, list):
                            _LOG.info(
                                "Found %d migration mappings", len(entity_mappings)
                            )
                            return entity_mappings
                        else:
                            _LOG.warning(
                                "entity_mappings is not a list: %s",
                                type(entity_mappings),
                            )
                    except json.JSONDecodeError as e:
                        _LOG.warning("Failed to parse migration_data JSON: %s", e)
                else:
                    _LOG.warning("migration_data textarea value is empty")

        _LOG.debug("migration_data field not found in settings")
        return []
    except (KeyError, TypeError) as e:
        _LOG.warning("Failed to extract migration mappings: %s", e)
        return []
