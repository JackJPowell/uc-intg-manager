"""Notification settings and configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)

# Notification settings file location
NOTIFICATION_SETTINGS_FILE = os.path.expanduser("~/.ucintg/notification_settings.json")


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

    # Track registry count for new integration detection
    _last_registry_count: int = 0
    """Internal: Last known count of integrations in registry."""

    @classmethod
    def load(cls) -> NotificationSettings:
        """Load notification settings from file or return defaults."""
        if os.path.exists(NOTIFICATION_SETTINGS_FILE):
            try:
                with open(NOTIFICATION_SETTINGS_FILE, encoding="utf-8") as f:
                    data = json.load(f)

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
            except (json.JSONDecodeError, OSError) as e:
                _LOG.warning("Failed to load notification settings: %s", e)
        return cls()

    def save(self) -> None:
        """Save notification settings to file."""
        try:
            os.makedirs(os.path.dirname(NOTIFICATION_SETTINGS_FILE), exist_ok=True)
            with open(NOTIFICATION_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
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
