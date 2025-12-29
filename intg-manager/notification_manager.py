"""Notification manager for triggering notifications based on events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from notification_service import NotificationService
from notification_settings import NotificationSettings

_LOG = logging.getLogger(__name__)


class NotificationManager:
    """
    Manages notification sending based on configured triggers.

    This class checks user preferences and sends notifications
    to all enabled providers when specific events occur.
    """

    def __init__(self) -> None:
        """Initialize the notification manager."""
        self._service = NotificationService()
        # Track what we've already notified about to avoid spam
        self._notified_updates: set[str] = set()  # {driver_id:version}
        self._notified_errors: dict[str, str] = {}  # {driver_id: error_state}

    def _load_settings(self) -> NotificationSettings:
        """Load current notification settings."""
        return NotificationSettings.load()

    def _should_notify(self, settings: NotificationSettings) -> bool:
        """Check if any notification provider is enabled."""
        return settings.is_any_enabled()

    async def notify_integration_update_available(
        self, driver_id: str, integration_name: str, current_version: str, latest_version: str
    ) -> None:
        """
        Notify when an integration update is available.

        :param driver_id: Driver ID of the integration
        :param integration_name: Name of the integration
        :param current_version: Current installed version
        :param latest_version: Latest available version
        """
        settings = self._load_settings()
        if (
            not self._should_notify(settings)
            or not settings.triggers.integration_update_available
        ):
            return

        # Only notify once per version
        notification_key = f"{driver_id}:{latest_version}"
        if notification_key in self._notified_updates:
            return

        title = "Integration Update Available"
        message = f"{integration_name} can be updated from {current_version} to {latest_version}"

        try:
            await self._service.send_all(settings, title, message)
            self._notified_updates.add(notification_key)
            _LOG.info("Sent update notification for %s", integration_name)
        except Exception as e:
            _LOG.error("Failed to send update notification: %s", e)

    async def notify_new_integration_in_registry(
        self, integration_names: list[str]
    ) -> None:
        """
        Notify when new integrations are detected in the registry.

        :param integration_names: List of new integration names
        """
        settings = self._load_settings()
        if (
            not self._should_notify(settings)
            or not settings.triggers.new_integration_in_registry
        ):
            return

        count = len(integration_names)
        title = f"{count} New Integration{'s' if count > 1 else ''} Available"

        if count <= 3:
            message = f"New integrations available: {', '.join(integration_names)}"
        else:
            message = f"{count} new integrations are now available in the registry"

        try:
            await self._service.send_all(settings, title, message)
            _LOG.info("Sent new integration notification for %d integrations", count)
        except Exception as e:
            _LOG.error("Failed to send new integration notification: %s", e)

    async def notify_integration_error_state(
        self, driver_id: str, integration_name: str, state: str
    ) -> None:
        """
        Notify when an integration enters an error state.

        :param driver_id: Driver ID of the integration
        :param integration_name: Name of the integration
        :param state: Current state
        """
        settings = self._load_settings()
        if (
            not self._should_notify(settings)
            or not settings.triggers.integration_error_state
        ):
            return

        # Only notify if this is a new error or the error state changed
        if self._notified_errors.get(driver_id) == state:
            return

        title = "Integration Error"
        message = f"{integration_name} has entered an error state: {state}"

        try:
            await self._service.send_all(settings, title, message, priority=1)
            self._notified_errors[driver_id] = state
            _LOG.info("Sent error state notification for %s", integration_name)
        except Exception as e:
            _LOG.error("Failed to send error state notification: %s", e)

    def clear_error_state(self, driver_id: str) -> None:
        """
        Clear the error state tracking for an integration.
        
        Call this when an integration recovers from an error state.
        
        :param driver_id: Driver ID of the integration
        """
        self._notified_errors.pop(driver_id, None)

    def clear_update_notification(self, driver_id: str, version: str) -> None:
        """
        Clear the update notification tracking for an integration.
        
        Call this when a user updates an integration to a new version.
        
        :param driver_id: Driver ID of the integration
        :param version: Version that was updated to
        """
        notification_key = f"{driver_id}:{version}"
        self._notified_updates.discard(notification_key)

    def update_registry_count(self, current_count: int) -> list[str]:
        """
        Update the registry count and return new integrations if count increased.

        :param current_count: Current number of integrations in registry
        :return: List of integration names that are new (empty if none or count decreased)
        """
        settings = self._load_settings()
        last_count = settings._last_registry_count

        if last_count > 0 and current_count > last_count:
            # Registry count increased - new integrations available
            # Note: We can't easily determine which specific integrations are new
            # without tracking individual integration IDs, so return count difference
            diff = current_count - last_count
            _LOG.info("Detected %d new integration(s) in registry", diff)

            # Update the stored count
            settings._last_registry_count = current_count
            settings.save()

            return [f"Integration {i}" for i in range(1, diff + 1)]

        # Update count (first run or count decreased/same)
        if settings._last_registry_count != current_count:
            settings._last_registry_count = current_count
            settings.save()

        return []


# Global notification manager instance
_notification_manager: NotificationManager | None = None


def get_notification_manager() -> NotificationManager:
    """Get the global notification manager instance."""
    global _notification_manager
    if _notification_manager is None:
        _notification_manager = NotificationManager()
    return _notification_manager


def send_notification_sync(coro_func, *args: Any, **kwargs: Any) -> None:
    """
    Helper to send notifications from synchronous code.

    :param coro_func: Async notification method to call
    :param args: Positional arguments
    :param kwargs: Keyword arguments
    """
    try:
        asyncio.run(coro_func(*args, **kwargs))
    except Exception as e:
        _LOG.error("Failed to send notification: %s", e)
