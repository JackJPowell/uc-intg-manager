"""Constants for the Integration Manager.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict, fields
from typing import Any

_LOG = logging.getLogger(__name__)

# Configuration directory for persistent storage across upgrades
# Priority: UC_CONFIG_HOME (Docker) > UC_DATA_HOME (Remote) > local dev default
_DEFAULT_DATA_HOME = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# UC_CONFIG_HOME is set in Docker and should persist across upgrades (e.g., /config)
# UC_DATA_HOME is set by the Remote but may not persist across upgrades (e.g., /data)
# Both already point to the data directory, so no need for subdirectory
DATA_DIR = os.environ.get("UC_CONFIG_HOME") or os.environ.get("UC_DATA_HOME") or _DEFAULT_DATA_HOME
os.makedirs(DATA_DIR, exist_ok=True)

# Manager data file - stores settings, integration backups, and other persistent data
MANAGER_DATA_FILE = os.path.join(DATA_DIR, "manager.json")

# Version check interval (in poll cycles, at 60s each = 30 min)
VERSION_CHECK_INTERVAL_POLLS = 30

# API request delays
API_DELAY = (
    0.75  # seconds - delay between API requests to avoid overwhelming the remote
)


@dataclass
class Settings:
    """
    User settings for the Integration Manager.

    These settings control the behavior of the integration manager
    and are persisted to settings.json.
    """

    shutdown_on_battery: bool = True
    """Shutdown web server when remote is on battery (not docked)."""

    auto_update: bool = False
    """Automatically update integrations when new versions are available."""

    backup_configs: bool = False
    """Automatically backup integration configuration files."""

    backup_time: str = "02:00"
    """Time of day to run automatic backups (HH:MM format)."""

    auto_register_entities: bool = True
    """Automatically re-register previously configured entities after integration updates."""

    show_beta_releases: bool = False
    """Show pre-release (beta) versions in version selector."""

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from manager data file or return defaults."""
        if os.path.exists(MANAGER_DATA_FILE):
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                settings_data = data.get("settings", {})
                field_names = {f.name for f in fields(cls)}
                _LOG.info("Loaded settings from %s", MANAGER_DATA_FILE)
                return cls(**{k: v for k, v in settings_data.items() if k in field_names})
            except (json.JSONDecodeError, OSError) as e:
                _LOG.warning("Failed to load settings from %s: %s", MANAGER_DATA_FILE, e)
        else:
            _LOG.info("Manager data file not found at %s, using defaults", MANAGER_DATA_FILE)
        return cls()

    def save(self) -> None:
        """Save settings to manager data file."""
        try:
            os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)
            
            # Load existing data to preserve other sections
            existing_data = {}
            if os.path.exists(MANAGER_DATA_FILE):
                try:
                    with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass
            
            # Update settings section
            existing_data["settings"] = asdict(self)
            existing_data["version"] = "1.0"
            
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.info("Settings saved to %s", MANAGER_DATA_FILE)
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


# Web server port - read from environment variable or default to 8088
WEB_SERVER_PORT = int(os.environ.get("UC_INTG_MANAGER_HTTP_PORT", "8088"))

# Known integrations registry URL (local for development, will be GitHub URL in production)
KNOWN_INTEGRATIONS_URL = "https://raw.githubusercontent.com/JackJPowell/uc-intg-list/refs/heads/main/registry.json"
# KNOWN_INTEGRATIONS_URL = os.path.join(os.path.dirname(__file__), "registry.json")

# Polling interval in seconds for checking remote power status
POWER_POLL_INTERVAL = 30

# GitHub API base URL
GITHUB_API_BASE = "https://api.github.com"
