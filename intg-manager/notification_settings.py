"""Notification settings and configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from const import MANAGER_DATA_FILE

_LOG = logging.getLogger(__name__)

NOTIFICATION_SETTINGS_FILE = MANAGER_DATA_FILE


@dataclass
class HomeAssistantNotificationConfig:
    """Home Assistant notification configuration."""

    enabled: bool = False
    """Whether Home Assistant notifications are enabled."""

    url: str = ""
    """Home Assistant instance URL."""

    token: str = ""
    """Long-lived access token for Home Assistant API."""


@dataclass
class WebhookNotificationConfig:
    """Webhook notification configuration."""

    enabled: bool = False
    """Whether webhook notifications are enabled."""

    url: str = ""
    """Webhook endpoint URL."""

    headers: dict[str, str] = field(default_factory=dict)
    """Custom HTTP headers to include in requests."""


@dataclass
class PushoverNotificationConfig:
    """Pushover notification configuration."""

    enabled: bool = False
    """Whether Pushover notifications are enabled."""

    user_key: str = ""
    """Pushover user key."""

    app_token: str = ""
    """Pushover application API token."""


@dataclass
class NtfyNotificationConfig:
    """ntfy notification configuration."""

    enabled: bool = False
    """Whether ntfy notifications are enabled."""

    server: str = "https://ntfy.sh"
    """ntfy server URL."""

    topic: str = ""
    """Topic to publish notifications to."""

    token: str = ""
    """Optional access token for protected topics."""


@dataclass
class DiscordNotificationConfig:
    """Discord notification configuration."""

    enabled: bool = False
    """Whether Discord notifications are enabled."""

    webhook_url: str = ""
    """Discord webhook URL."""


@dataclass
class NotificationTriggers:
    """Configuration for when to send notifications."""

    # Update Events
    integration_update_available: bool = True
    """Notify when an update is available for an installed integration."""

    new_integration_in_registry: bool = False
    """Notify when a new integration is detected in the registry."""

    # Integration State Changes
    integration_error_state: bool = True
    """Notify when an integration enters an ERROR state."""

    orphaned_entities_detected: bool = True
    """Notify when orphaned entities are detected in activities."""


@dataclass
class NotificationSettings:
    """
    Notification settings for all providers.

    These settings control how and where notifications are sent.
    """

    home_assistant: HomeAssistantNotificationConfig = field(
        default_factory=HomeAssistantNotificationConfig
    )
    """Home Assistant notification configuration."""

    webhook: WebhookNotificationConfig = field(
        default_factory=WebhookNotificationConfig
    )
    """Webhook notification configuration."""

    pushover: PushoverNotificationConfig = field(
        default_factory=PushoverNotificationConfig
    )
    """Pushover notification configuration."""

    ntfy: NtfyNotificationConfig = field(default_factory=NtfyNotificationConfig)
    """ntfy notification configuration."""

    discord: DiscordNotificationConfig = field(
        default_factory=DiscordNotificationConfig
    )
    """Discord notification configuration."""

    triggers: NotificationTriggers = field(default_factory=NotificationTriggers)
    """Notification trigger preferences."""

    # Track registry for new integration detection
    _last_registry_count: int = 0
    """Internal: Last known count of integrations in registry."""

    _known_integration_ids: list[str] = field(default_factory=list)
    """Internal: List of known integration IDs from registry."""

    @classmethod
    def load(cls) -> NotificationSettings:
        """Load notification settings from manager.json or return defaults."""
        if os.path.exists(NOTIFICATION_SETTINGS_FILE):
            try:
                with open(NOTIFICATION_SETTINGS_FILE, encoding="utf-8") as f:
                    file_data = json.load(f)
                    # Get notification_settings section from manager.json
                    data = file_data.get("notification_settings", {})

                    if not data:
                        # Try legacy location for migration
                        legacy_file = os.path.expanduser(
                            "~/.ucintg/notification_settings.json"
                        )
                        if os.path.exists(legacy_file):
                            _LOG.info(
                                "Migrating notification settings from legacy location"
                            )
                            with open(legacy_file, encoding="utf-8") as lf:
                                data = json.load(lf)
                            # Save to new location and return
                            settings = cls._parse_settings_data(data)
                            settings.save()
                            # Clean up legacy file
                            try:
                                os.remove(legacy_file)
                                _LOG.info("Removed legacy notification settings file")
                            except OSError:
                                pass
                            return settings
                        return cls()

                    return cls._parse_settings_data(data)
            except (json.JSONDecodeError, OSError) as e:
                _LOG.warning("Failed to load notification settings: %s", e)
        return cls()

    @classmethod
    def _parse_settings_data(cls, data: dict) -> NotificationSettings:
        """Parse settings data dict into NotificationSettings instance."""
        # Convert nested dicts to dataclass instances
        if "home_assistant" in data:
            data["home_assistant"] = HomeAssistantNotificationConfig(
                **data["home_assistant"]
            )
        if "webhook" in data:
            data["webhook"] = WebhookNotificationConfig(**data["webhook"])
        if "pushover" in data:
            data["pushover"] = PushoverNotificationConfig(**data["pushover"])
        if "ntfy" in data:
            data["ntfy"] = NtfyNotificationConfig(**data["ntfy"])
        if "discord" in data:
            data["discord"] = DiscordNotificationConfig(**data["discord"])
        if "triggers" in data:
            data["triggers"] = NotificationTriggers(**data["triggers"])

        return cls(**data)

    def save(self) -> None:
        """Save notification settings to manager.json."""
        try:
            os.makedirs(os.path.dirname(NOTIFICATION_SETTINGS_FILE), exist_ok=True)

            # Load existing data to preserve other sections
            existing_data = {}
            if os.path.exists(NOTIFICATION_SETTINGS_FILE):
                try:
                    with open(NOTIFICATION_SETTINGS_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            # Update notification_settings section
            existing_data["notification_settings"] = self.to_dict()
            existing_data["version"] = "1.0"

            with open(NOTIFICATION_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.info("Notification settings saved to %s", NOTIFICATION_SETTINGS_FILE)
        except OSError as e:
            _LOG.error("Failed to save notification settings: %s", e)

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to dictionary."""
        return asdict(self)

    def is_any_enabled(self) -> bool:
        """Check if any notification provider is enabled."""
        return (
            self.home_assistant.enabled
            or self.webhook.enabled
            or self.pushover.enabled
            or self.ntfy.enabled
            or self.discord.enabled
        )
