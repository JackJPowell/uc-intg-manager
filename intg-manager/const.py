"""Constants for the Integration Manager.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict, fields
from typing import Any

_LOG = logging.getLogger(__name__)


# Configuration directory for persistent storage
# Use UC_CONFIG_HOME environment variable (Docker/Remote), fall back to ./config for local dev
def _get_data_dir():
    """Get the data directory, with fallback for local development."""
    # Check for UC_CONFIG_HOME environment variable (set by Docker/Remote)
    config_home = os.environ.get("UC_CONFIG_HOME")
    if config_home:
        os.makedirs(config_home, exist_ok=True)
        return config_home

    # Fall back to relative ./config directory for local development
    local_config_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config"
    )
    os.makedirs(local_config_dir, exist_ok=True)
    return local_config_dir


DATA_DIR = _get_data_dir()

# Manager data file - stores settings, integration backups, and other persistent data
MANAGER_DATA_FILE = os.path.join(DATA_DIR, "manager.json")

# Repository cache validity duration (24 hours in seconds)
REPO_CACHE_VALIDITY = 86400

# Maximum number of repository info requests per hour to avoid rate limits
REPO_FETCH_BATCH_SIZE = 10

# Minimum time between batches (1 hour in seconds)
REPO_FETCH_BATCH_INTERVAL = 3600

# System messages file - stores system announcements and notifications
SYSTEM_MESSAGES_FILE = os.path.join(DATA_DIR, "system_messages.json")
# System messages GitHub URL - remote source for messages
SYSTEM_MESSAGES_URL = "https://raw.githubusercontent.com/JackJPowell/uc-intg-list/main/system_messages.json"

# Version check interval (in poll cycles, at 30s each = 15 min)
VERSION_CHECK_INTERVAL_POLLS = 30

# API request delays
API_DELAY = (
    0.75  # seconds - delay between API requests to avoid overwhelming the remote
)


@dataclass
class UIPreferences:
    """UI preference settings shared across all remotes.

    These preferences control UI behavior and are stored in the shared section
    of manager.json in multi-remote setups.
    """

    sort_by: str = "stars"

    sort_reverse: bool = False
    """Reverse the sort order for available integrations."""

    @classmethod
    def load(cls) -> "UIPreferences":
        """Load UI preferences from shared section of manager data file."""
        if os.path.exists(MANAGER_DATA_FILE):
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # v2.0 format - load from shared section
                prefs_data = data.get("shared", {}).get("ui_preferences", {})

                field_names = {f.name for f in fields(cls)}
                return cls(**{k: v for k, v in prefs_data.items() if k in field_names})
            except (json.JSONDecodeError, OSError) as e:
                _LOG.warning("Failed to load UI preferences: %s", e)
        return cls()

    def save(self) -> None:
        """Save UI preferences to shared section of manager data file."""
        try:
            os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)

            # Load existing data
            existing_data: dict[str, Any] = {
                "version": "2.0",
                "remotes": {},
                "shared": {},
            }
            if os.path.exists(MANAGER_DATA_FILE):
                try:
                    with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            # Ensure shared section exists
            if "shared" not in existing_data:
                existing_data["shared"] = {}

            # Update UI preferences in shared section
            existing_data["shared"]["ui_preferences"] = asdict(self)

            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.debug("UI preferences saved")
        except OSError as e:
            _LOG.error("Failed to save UI preferences: %s", e)

    def to_dict(self) -> dict[str, Any]:
        """Convert preferences to dictionary."""
        return asdict(self)


@dataclass
class Settings:
    """
    User settings for the Integration Manager.

    These settings control the behavior of the integration manager
    and are persisted per-remote in manager.json.
    """

    settings_version: int = 1
    """Version number for settings schema, used for migrations."""

    shutdown_on_battery: bool = False
    """Shutdown web server when remote is on battery (not docked)."""

    auto_update: bool = False
    """Automatically update integrations when new versions are available."""

    backup_configs: bool = False
    """Automatically backup integration configuration files."""

    backup_time: str = "02:00"
    """Time of day to perform automatic backups (HH:MM format)."""

    auto_register_entities: bool = True
    """Automatically register new entities with the remote."""

    show_beta_releases: bool = False
    """Show pre-release (beta) versions in version selector."""

    @classmethod
    def load(cls, remote_id: str | None = None) -> "Settings":
        """
        Load settings for a specific remote or return defaults.

        :param remote_id: Remote identifier. If None, loads from first available remote (backward compatibility)
        """
        if os.path.exists(MANAGER_DATA_FILE):
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # v2.0 format - load from remotes section
                if remote_id is None:
                    # Backward compatibility - get first remote
                    remotes = data.get("remotes", {})
                    if remotes:
                        remote_id = next(iter(remotes.keys()))
                    else:
                        _LOG.warning("No remotes found in manager.json")
                        return cls()

                settings_data = (
                    data.get("remotes", {}).get(remote_id, {}).get("settings", {})
                )
                # Filter out UI preferences (now in UIPreferences)
                settings_data = {
                    k: v
                    for k, v in settings_data.items()
                    if k not in ["sort_by", "sort_reverse"]
                }

                field_names = {f.name for f in fields(cls)}
                _LOG.debug("Loaded settings for remote %s", remote_id or "default")

                # Create settings instance
                settings = cls(
                    **{k: v for k, v in settings_data.items() if k in field_names}
                )

                # Perform migrations based on settings_version
                settings._migrate(remote_id)

                return settings
            except (json.JSONDecodeError, OSError) as e:
                _LOG.warning("Failed to load settings: %s", e)
        else:
            _LOG.info("Manager data file not found, using defaults")
        return cls()

    def _migrate(self, remote_id: str | None = None) -> None:
        """Migrate settings from older versions to current schema."""
        current_version = self.settings_version
        needs_save = False

        # Migration from version 0 (no version field) to version 1
        if current_version < 1:
            if self.shutdown_on_battery is True:
                _LOG.info(
                    "Migrating settings v%d->v1: Changing shutdown_on_battery default",
                    current_version,
                )
                self.shutdown_on_battery = False
                needs_save = True

            self.settings_version = 1
            needs_save = True

        # Save if any migrations were applied
        if needs_save:
            self.save(remote_id)
            _LOG.info("Settings migrated to version %d", self.settings_version)

    def save(self, remote_id: str | None = None) -> None:
        """
        Save settings for a specific remote.

        :param remote_id: Remote identifier. If None, saves to first available remote (backward compatibility)
        """
        try:
            os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)

            # Load existing data
            existing_data: dict[str, Any] = {
                "version": "2.0",
                "remotes": {},
                "shared": {},
            }
            if os.path.exists(MANAGER_DATA_FILE):
                try:
                    with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            # Ensure v2.0 structure (should already be migrated at startup)
            if existing_data.get("version") != "2.0":
                _LOG.error(
                    "manager.json is not v2.0 format - migration should have run at startup"
                )
                existing_data["version"] = "2.0"
                if "remotes" not in existing_data:
                    existing_data["remotes"] = {}
                if "shared" not in existing_data:
                    existing_data["shared"] = {}

            # Resolve remote_id
            if remote_id is None:
                # Get first remote
                remotes = existing_data.get("remotes", {})
                if remotes:
                    remote_id = next(iter(remotes.keys()))
                else:
                    _LOG.error(
                        "Cannot save settings - no remote_id and no remotes exist"
                    )
                    return

            # Ensure remote entry exists
            if "remotes" not in existing_data:
                existing_data["remotes"] = {}
            if remote_id not in existing_data["remotes"]:
                existing_data["remotes"][remote_id] = {}

            # Update settings for this remote
            existing_data["remotes"][remote_id]["settings"] = asdict(self)

            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.debug("Settings saved for remote %s", remote_id or "default")
        except OSError as e:
            _LOG.error("Failed to save settings: %s", e)

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to dictionary."""
        return asdict(self)


@dataclass
class RemoteConfig:
    """
    Remote configuration dataclass.

    This dataclass holds all the configuration needed to connect to
    the Unfolded Circle Remote.
    """

    identifier: str
    """Unique identifier of the remote."""

    name: str
    """Friendly name of the remote for display purposes."""

    address: str
    """IP address or hostname of the remote."""

    pin: str = ""
    """Web configurator PIN for authentication."""

    api_key: str = ""
    """API key for authentication (preferred over PIN)."""

    def __repr__(self) -> str:
        """Return string representation with masked credentials."""
        return (
            f"RemoteConfig(identifier={self.identifier!r}, "
            f"name={self.name!r}, "
            f"address={self.address!r}, "
            f"pin='****', "
            f"api_key='****')"
        )


# Web server port - read from environment variable or default to 9999
WEB_SERVER_PORT = int(os.environ.get("UC_INTG_MANAGER_HTTP_PORT", "9999"))

# Legacy port kept open to serve a migration notice page
LEGACY_WEB_SERVER_PORT = 8088

# Known integrations registry URL
# Override with UC_KNOWN_INTEGRATIONS_URL env var or local registry.json for development:
#   KNOWN_INTEGRATIONS_URL = os.path.join(os.path.dirname(__file__), "registry.json")
KNOWN_INTEGRATIONS_URL = os.environ.get(
    "UC_KNOWN_INTEGRATIONS_URL",
    "https://raw.githubusercontent.com/JackJPowell/uc-intg-list/refs/heads/main/registry.json",
)

# Polling interval in seconds for checking remote power status
POWER_POLL_INTERVAL = 30

# GitHub API base URL
GITHUB_API_BASE = "https://api.github.com"
