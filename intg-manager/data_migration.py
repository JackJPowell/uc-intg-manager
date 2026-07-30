"""
Data Migration Module.

Handles migration of manager.json from v1.0 to v2.0 format.
This migration is forced on first run with multi-remote support to ensure
proper per-remote data isolation.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
import shutil
from typing import Any

from const import MANAGER_DATA_FILE

_LOG = logging.getLogger(__name__)


def _get_remote_id_from_config() -> str | None:
    """
    Read the remote identifier from config.json.

    In v1.0, there should only be one remote configured.
    We'll use its identifier for the v2.0 migration.

    :return: Remote identifier or None if not found
    """
    # Try to find config.json in the same directory as manager.json
    config_dir = os.path.dirname(MANAGER_DATA_FILE)
    config_file = os.path.join(config_dir, "config.json")

    if not os.path.exists(config_file):
        _LOG.warning("config.json not found at %s", config_file)
        return None

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            configs = json.load(f)

        if not configs or not isinstance(configs, list):
            _LOG.warning("config.json is not a list of configs")
            return None

        # Get the first (and likely only) remote's identifier
        first_remote = configs[0]
        remote_id = first_remote.get("identifier")

        if remote_id:
            _LOG.info("Found remote_id from config.json: %s", remote_id)
            return remote_id
        else:
            _LOG.warning("No identifier found in config.json first entry")
            return None

    except (json.JSONDecodeError, OSError, KeyError, IndexError) as e:
        _LOG.error("Failed to read config.json: %s", e)
        return None


def migrate(target_remote_id: str | None = None) -> bool:
    """
    Force migration from v1.0 to v2.0 format if needed.

    This function checks if manager.json exists and is in v1.0 format.
    If so, it migrates it to v2.0 format, creating proper per-remote
    data structure.

    This should be called early during integration startup to ensure
    all subsequent code can assume v2.0 format.

    :param target_remote_id: Optional remote ID to assign the migrated data to.
                             If not provided, will read from config.json.
    :return: True if migration was performed, False if not needed
    """
    if not os.path.exists(MANAGER_DATA_FILE):
        _LOG.info("No manager.json found - will be created in v2.0 format")
        return False

    try:
        with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _LOG.error("Failed to read manager.json for migration: %s", e)
        return False

    # Check if already v2.0
    if data.get("version") == "2.0":
        _LOG.info("manager.json already in v2.0 format")
        return False

    _LOG.info("Migrating manager.json from v1.0 to v2.0 format")

    # Create backup
    backup_path = f"{MANAGER_DATA_FILE}.v1.backup"
    try:
        shutil.copy2(MANAGER_DATA_FILE, backup_path)
        _LOG.info("Created backup at %s", backup_path)
    except OSError as e:
        _LOG.error("Failed to create backup: %s", e)
        return False

    # Determine remote_id
    if target_remote_id:
        remote_id: str = target_remote_id
        _LOG.info("Using provided target_remote_id: %s", remote_id)
    else:
        # Determine remote_id from config.json
        # In v1.0, there should only be one remote configured
        remote_id = _get_remote_id_from_config() or ""

        # Fall back to default if we can't read from config
        if not remote_id:
            remote_id = "uc-remote-default"
            _LOG.warning(
                "Could not determine remote_id from config.json, using default: %s",
                remote_id,
            )

    # Build v2.0 structure
    v2_data: dict[str, Any] = {
        "version": "2.0",
        "remotes": {remote_id: {}},
        "shared": {},
    }

    # Migrate settings (per-remote in v2.0)
    if "settings" in data:
        v2_data["remotes"][remote_id]["settings"] = data["settings"]
        _LOG.debug("Migrated settings to remotes.%s.settings", remote_id)

    # Migrate notification_settings (shared in v2.0 - same config for all remotes)
    if "notification_settings" in data:
        # Extract registry tracking fields (move to shared)
        notification_settings = data["notification_settings"].copy()

        # Move registry tracking to shared
        last_registry_count = notification_settings.pop("_last_registry_count", None)
        known_integration_ids = notification_settings.pop(
            "_known_integration_ids", None
        )

        v2_data["shared"]["notification_settings"] = notification_settings
        _LOG.debug("Migrated notification_settings to shared.notification_settings")

        # Initialize shared registry tracking
        if last_registry_count is not None or known_integration_ids is not None:
            v2_data["shared"]["registry_tracking"] = {
                "_last_registry_count": last_registry_count or 0,
                "_known_integration_ids": known_integration_ids or [],
            }
            _LOG.debug("Migrated registry tracking to shared.registry_tracking")

    # Migrate notification_state (per-remote in v2.0)
    if "notification_state" in data:
        v2_data["remotes"][remote_id]["notification_state"] = data["notification_state"]
        _LOG.debug(
            "Migrated notification_state to remotes.%s.notification_state", remote_id
        )

    # Migrate integrations (per-remote in v2.0)
    if "integrations" in data:
        v2_data["remotes"][remote_id]["integrations"] = data["integrations"]
        _LOG.debug("Migrated integrations to remotes.%s.integrations", remote_id)

    # Migrate backup_timestamp (per-remote in v2.0)
    if "backup_timestamp" in data:
        v2_data["remotes"][remote_id]["backup_timestamp"] = data["backup_timestamp"]
        _LOG.debug(
            "Migrated backup_timestamp to remotes.%s.backup_timestamp", remote_id
        )

    # Migrate repo_cache (shared in v2.0)
    if "repo_cache" in data:
        v2_data["shared"]["repo_cache"] = data["repo_cache"]
        _LOG.debug(
            "Migrated repo_cache to shared.repo_cache (%d repos)",
            len(data["repo_cache"].get("repos", {})),
        )
    else:
        v2_data["shared"]["repo_cache"] = {}
        _LOG.debug("Initialized empty shared.repo_cache")

    # Migrate read_message_ids (shared in v2.0)
    if "read_message_ids" in data:
        v2_data["shared"]["read_message_ids"] = data["read_message_ids"]
        _LOG.debug("Migrated read_message_ids to shared.read_message_ids")

    # Initialize ui_preferences if not already set
    if "ui_preferences" not in v2_data["shared"]:
        v2_data["shared"]["ui_preferences"] = {
            "sort_by": "original",
            "sort_reverse": False,
            "view_mode": "card",
        }
        _LOG.debug("Initialized shared.ui_preferences with defaults")

    # Registry tracking should already be set from notification_settings migration above
    # Only initialize if it's still missing
    if "registry_tracking" not in v2_data["shared"]:
        v2_data["shared"]["registry_tracking"] = {
            "_last_registry_count": 0,
            "_known_integration_ids": [],
        }
        _LOG.debug("Initialized shared.registry_tracking with defaults")

    # Write v2.0 data
    try:
        with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(v2_data, f, indent=2)
        _LOG.info("Successfully migrated manager.json to v2.0 format")
        _LOG.info("Data migrated to remote_id: %s", remote_id)
        return True
    except OSError as e:
        _LOG.error("Failed to write v2.0 data: %s", e)
        # Try to restore backup
        try:
            shutil.copy2(backup_path, MANAGER_DATA_FILE)
            _LOG.info("Restored backup after failed migration")
        except OSError:
            _LOG.error("Failed to restore backup - manual intervention may be required")
        return False
