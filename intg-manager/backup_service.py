"""
Integration Backup Service.

This module provides functionality to backup and restore integration
configurations using the UC Remote's setup API flow.

The backup flow:
1. POST /intg/setup with driver_id, reconfigure=true, setup_data={} (initiates setup mode)
2. GET /intg/setup/{driver_id} to retrieve the setup page with choices
3. Parse the response to get the first dropdown choice ID
4. PUT /intg/setup/{driver_id} with input_values={choice, action=backup}
5. GET /intg/setup/{driver_id} to retrieve the updated page with backup data
6. Parse the response to extract backup_data from textarea field

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from const import INTEGRATION_BACKUPS_FILE, API_DELAY, Settings
from sync_api import SyncRemoteClient, SyncAPIError

_LOG = logging.getLogger(__name__)

# Backup storage file
BACKUP_FILE = INTEGRATION_BACKUPS_FILE


def _load_backups() -> dict[str, Any]:
    """Load the backup data from disk."""
    if os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Migrate old format to new format if needed
                if "backups" in data and "integrations" not in data:
                    _LOG.info("Migrating backup file to new format")
                    return {
                        "settings": {},
                        "integrations": data.get("backups", {}),
                        "backup_timestamp": data.get("last_updated"),
                        "version": "1.0",
                    }
                return data
        except (json.JSONDecodeError, OSError) as e:
            _LOG.error("Failed to load backups file: %s", e)
    return {
        "settings": {},
        "integrations": {},
        "backup_timestamp": None,
        "version": "1.0",
    }


def _save_backups(data: dict[str, Any]) -> bool:
    """Save the backup data to disk."""
    try:
        data["backup_timestamp"] = datetime.now().isoformat()
        data["version"] = "1.0"
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        _LOG.error("Failed to save backups file: %s", e)
        return False


def _extract_first_choice_id(setup_response: dict[str, Any]) -> str | None:
    """
    Extract the first dropdown choice ID from a setup response.

    The response structure has:
    require_user_action.input.settings[0].field.dropdown.value

    :param setup_response: The response from start_setup()
    :return: The choice ID or None if not found
    """
    try:
        settings = (
            setup_response.get("require_user_action", {})
            .get("input", {})
            .get("settings", [])
        )
        for setting in settings:
            if setting.get("id") == "choice":
                dropdown = setting.get("field", {}).get("dropdown", {})
                return dropdown.get("value")
        return None
    except (KeyError, TypeError, IndexError) as e:
        _LOG.warning("Failed to extract choice ID: %s", e)
        return None


def _extract_backup_data(setup_response: dict[str, Any]) -> str | None:
    """
    Extract the backup_data textarea content from a setup response.

    The response structure has:
    require_user_action.input.settings[].field.textarea.value
    where setting.id == "backup_data"

    :param setup_response: The response from send_setup_input()
    :return: The backup data string or None if not found
    """
    try:
        settings = (
            setup_response.get("require_user_action", {})
            .get("input", {})
            .get("settings", [])
        )
        for setting in settings:
            if setting.get("id") == "backup_data":
                textarea = setting.get("field", {}).get("textarea", {})
                return textarea.get("value")
        return None
    except (KeyError, TypeError) as e:
        _LOG.warning("Failed to extract backup data: %s", e)
        return None


def backup_integration(
    client: SyncRemoteClient,
    driver_id: str,
    save_to_file: bool = True,
) -> str | None:
    """
    Backup an integration's configuration.

    This performs the full backup flow:
    1. Start setup with reconfigure=true
    2. Extract the first choice ID from the dropdown
    3. Send backup action request
    4. Extract and return the backup data

    :param client: The SyncRemoteClient instance
    :param driver_id: The driver ID to backup
    :param save_to_file: Whether to save to integration_backups.json
    :return: The backup data string, or None if backup failed
    """
    _LOG.info("Starting backup for integration: %s", driver_id)

    try:
        # Step 1: Start the setup flow (this just initiates setup mode)
        start_response = client.start_setup(driver_id, reconfigure=True)
        if not start_response:
            _LOG.error("No response from start_setup for %s", driver_id)
            return None

        _LOG.debug("Start setup response: %s", start_response)

        # Brief pause before next request
        time.sleep(API_DELAY)

        # Step 2: Get the setup page with choices
        setup_response = client.get_setup(driver_id)
        if not setup_response:
            _LOG.error("No response from get_setup for %s", driver_id)
            return None

        _LOG.debug("Get setup response: %s", setup_response)

        # Brief pause before next request
        time.sleep(API_DELAY)

        # Step 3: Extract the first choice ID
        choice_id = _extract_first_choice_id(setup_response)
        if not choice_id:
            _LOG.warning(
                "No choice ID found in setup response for %s. "
                "Integration may not support backup.",
                driver_id,
            )
            # Try to cancel the setup flow
            client.complete_setup(driver_id)
            return None

        _LOG.debug("Found choice ID: %s", choice_id)

        # Step 4: Send the backup action
        input_values = {
            "choice": choice_id,
            "action": "backup",
            "backup_data": "[]",  # Empty initial value
        }
        backup_response = client.send_setup_input(driver_id, input_values)
        if not backup_response:
            _LOG.error("No response from backup request for %s", driver_id)
            client.complete_setup(driver_id)
            return None

        _LOG.debug("Backup PUT response: %s", backup_response)

        # Brief pause before next request
        time.sleep(API_DELAY * 2)

        # Step 5: Get the updated setup page with backup data
        setup_response = client.get_setup(driver_id)
        if not setup_response:
            _LOG.error("No response from get_setup after backup for %s", driver_id)
            client.complete_setup(driver_id)
            return None

        _LOG.debug("Get setup response (with backup data): %s", setup_response)

        # Brief pause before next request
        time.sleep(API_DELAY * 2)

        # Step 6: Extract the backup data
        backup_data = _extract_backup_data(setup_response)
        if not backup_data:
            _LOG.warning("No backup data found in response for %s", driver_id)
            client.complete_setup(driver_id)
            return None

        _LOG.info("Successfully extracted backup data for %s", driver_id)

        # Complete the setup flow (we're done)
        client.complete_setup(driver_id)
        _LOG.debug("Completed setup flow for %s", driver_id)

        # Brief pause after completing setup
        time.sleep(API_DELAY)

        # Save to file if requested
        if save_to_file:
            if save_backup(driver_id, backup_data):
                _LOG.info("Backup for '%s' completed successfully", driver_id)
            else:
                _LOG.warning("Backup for '%s' extracted but failed to save to file", driver_id)

        return backup_data

    except SyncAPIError as e:
        _LOG.error("API error during backup of %s: %s", driver_id, e)
        try:
            client.complete_setup(driver_id)
            time.sleep(API_DELAY)  # Brief pause after cleanup
        except SyncAPIError:
            pass
        return None


def _clean_backup_data(raw_data: str) -> str:
    """
    Clean backup data by parsing and reformatting JSON.

    Removes escape characters and control data, ensuring clean JSON output.

    :param raw_data: Raw backup data string (potentially with escape chars)
    :return: Clean, formatted JSON string
    """
    try:
        # First, try to parse as JSON in case it's already escaped
        parsed_data = json.loads(raw_data)
        # Re-serialize with clean formatting
        return json.dumps(parsed_data, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        try:
            # If that fails, try to decode escape sequences manually
            # Handle common escape sequences
            cleaned = (
                raw_data.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            )
            # Try to parse again
            parsed_data = json.loads(cleaned)
            return json.dumps(parsed_data, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            # If all else fails, return the original data
            _LOG.warning("Could not parse backup data as JSON, saving raw data")
            return raw_data


def save_backup(driver_id: str, backup_data: str) -> bool:
    """
    Save backup data for an integration to the backups file.

    :param driver_id: The driver ID
    :param backup_data: The raw backup data string
    :return: True if saved successfully
    """
    # Clean the backup data before saving
    clean_data = _clean_backup_data(backup_data)

    backups = _load_backups()
    timestamp = datetime.now().isoformat()
    backups["integrations"][driver_id] = {
        "data": clean_data,
        "timestamp": timestamp,
    }
    success = _save_backups(backups)
    if success:
        _LOG.info("Successfully saved backup for integration '%s' at %s", driver_id, timestamp)
    else:
        _LOG.error("Failed to save backup for integration '%s'", driver_id)
    return success


def get_backup(driver_id: str) -> str | None:
    """
    Get the stored backup data for an integration.

    :param driver_id: The driver ID
    :return: The backup data string or None if not found
    """
    backups = _load_backups()
    backup_entry = backups.get("integrations", {}).get(driver_id)
    if backup_entry:
        return backup_entry.get("data")
    return None


def get_all_backups() -> dict[str, Any]:
    """
    Get all stored backups.

    :return: Dictionary with backup data keyed by driver_id
    """
    return _load_backups()


def delete_backup(driver_id: str) -> bool:
    """
    Delete a stored backup for an integration.

    :param driver_id: The driver ID
    :return: True if deleted (or didn't exist)
    """
    backups = _load_backups()
    if driver_id in backups.get("integrations", {}):
        del backups["integrations"][driver_id]
        return _save_backups(backups)
    return True


def backup_all_integrations(
    client: SyncRemoteClient, include_settings: bool = True
) -> dict[str, bool]:
    """
    Backup all installed custom integrations and optionally settings.

    :param client: The SyncRemoteClient instance
    :param include_settings: Whether to include settings in the backup
    :return: Dictionary of driver_id -> success boolean
    """
    results = {}

    try:
        # Get all drivers
        drivers = client.get_drivers()

        for driver in drivers:
            driver_id = driver.get("driver_id")
            driver_type = driver.get("driver_type", "")

            # Only backup CUSTOM integrations (installed on remote)
            if driver_type != "CUSTOM":
                continue

            if not driver_id:
                continue

            _LOG.info("Backing up integration: %s", driver_id)
            backup_data = backup_integration(client, driver_id, save_to_file=True)
            results[driver_id] = backup_data is not None

            # Pause between integrations to avoid overwhelming the remote
            time.sleep(API_DELAY * 2)

        # Save settings to backup file if requested
        if include_settings:
            settings = Settings.load()
            backups = _load_backups()
            backups["settings"] = settings.to_dict()
            _save_backups(backups)
            _LOG.info("Saved settings to backup file")

    except SyncAPIError as e:
        _LOG.error("Failed to get drivers for backup: %s", e)

    return results
