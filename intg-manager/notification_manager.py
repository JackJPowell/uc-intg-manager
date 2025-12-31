"""Notification manager for triggering notifications based on events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from const import MANAGER_DATA_FILE
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
        self._notified_orphaned_activities: set[str] = set()  # {activity_id}
        # Load persisted notification state from disk
        self._load_notification_state()

    def _load_notification_state(self) -> None:
        """Load notification state from manager.json file."""
        try:
            if os.path.exists(MANAGER_DATA_FILE):
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    notification_state = data.get("notification_state", {})
                    self._notified_updates = set(
                        notification_state.get("notified_updates", [])
                    )
                    self._notified_errors = notification_state.get(
                        "notified_errors", {}
                    )
                    self._notified_orphaned_activities = set(
                        notification_state.get("notified_orphaned_activities", [])
                    )
                    _LOG.debug(
                        "Loaded notification state: %d updates, %d errors, %d orphaned activities",
                        len(self._notified_updates),
                        len(self._notified_errors),
                        len(self._notified_orphaned_activities),
                    )
        except (json.JSONDecodeError, OSError) as e:
            _LOG.warning("Failed to load notification state: %s", e)

    def _save_notification_state(self) -> None:
        """Save notification state to manager.json file."""
        try:
            # Load existing data to preserve other sections
            existing_data = {}
            if os.path.exists(MANAGER_DATA_FILE):
                try:
                    with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            # Update notification state section
            existing_data["notification_state"] = {
                "notified_updates": list(self._notified_updates),
                "notified_errors": self._notified_errors,
                "notified_orphaned_activities": list(
                    self._notified_orphaned_activities
                ),
            }
            existing_data["version"] = "1.0"

            # Ensure directory exists
            os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)

            # Write to disk
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.debug("Saved notification state to %s", MANAGER_DATA_FILE)
        except OSError as e:
            _LOG.error("Failed to save notification state: %s", e)

    def _load_settings(self) -> NotificationSettings:
        """Load current notification settings."""
        return NotificationSettings.load()

    def _should_notify(self, settings: NotificationSettings) -> bool:
        """Check if any notification provider is enabled."""
        return settings.is_any_enabled()

    async def notify_integration_update_available(
        self,
        driver_id: str,
        integration_name: str,
        current_version: str,
        latest_version: str,
    ) -> None:
        """
        Notify when an integration update is available.

        :param driver_id: Driver ID of the integration
        :param integration_name: Name of the integration
        :param current_version: Current installed version
        :param latest_version: Latest available version
        """
        _LOG.debug(
            "notify_integration_update_available called: driver_id=%s, name=%s, current=%s, latest=%s",
            driver_id,
            integration_name,
            current_version,
            latest_version,
        )
        settings = self._load_settings()
        _LOG.debug(
            "Settings loaded: any_enabled=%s, trigger_enabled=%s",
            self._should_notify(settings),
            settings.triggers.integration_update_available,
        )
        if (
            not self._should_notify(settings)
            or not settings.triggers.integration_update_available
        ):
            _LOG.info("Notification skipped: provider or trigger not enabled")
            return

        # Only notify once per version
        notification_key = f"{driver_id}:{latest_version}"
        _LOG.debug(
            "Checking notification key: %s, already notified: %s",
            notification_key,
            notification_key in self._notified_updates,
        )
        if notification_key in self._notified_updates:
            _LOG.info("Notification already sent for this version")
            return

        title = "Integration Update Available"
        message = f"{integration_name} can be updated from {current_version} to {latest_version}"

        _LOG.info("Sending notification: title='%s', message='%s'", title, message)
        try:
            await self._service.send_all(settings, title, message)
            self._notified_updates.add(notification_key)
            self._save_notification_state()  # Persist to disk
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
        message = f"{', '.join(integration_names)}"

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

        _LOG.debug(
            "Sending error notification: title='%s', message='%s'", title, message
        )
        try:
            await self._service.send_all(settings, title, message, priority=1)
            self._notified_errors[driver_id] = state
            self._save_notification_state()  # Persist to disk
            _LOG.info("Sent error state notification for %s", integration_name)
        except Exception as e:
            _LOG.error("Failed to send error state notification: %s", e)

    def clear_error_state(self, driver_id: str) -> None:
        """
        Clear the error state tracking for an integration.

        Call this when an integration recovers from an error state.

        :param driver_id: Driver ID of the integration
        """
        if self._notified_errors.pop(driver_id, None) is not None:
            self._save_notification_state()  # Persist to disk

    async def notify_orphaned_entities(
        self, activity_names: list[str], activity_ids: list[str]
    ) -> None:
        """
        Notify when orphaned entities are detected in activities.

        :param activity_names: List of activity names with orphaned entities
        :param activity_ids: List of activity IDs with orphaned entities
        """
        _LOG.debug(
            "notify_orphaned_entities called with %d activities: %s (IDs: %s)",
            len(activity_names),
            activity_names,
            activity_ids,
        )

        settings = self._load_settings()
        if (
            not self._should_notify(settings)
            or not settings.triggers.orphaned_entities_detected
        ):
            return

        # Filter to only new orphaned activities
        new_activity_ids = set(activity_ids) - self._notified_orphaned_activities
        if not new_activity_ids:
            _LOG.debug("No new orphaned activities to notify about")
            return

        # Get names for the new activities
        id_to_name = {aid: name for aid, name in zip(activity_ids, activity_names)}
        new_activity_names = [
            id_to_name[aid] for aid in new_activity_ids if aid in id_to_name
        ]

        count = len(new_activity_names)
        if count == 0:
            return

        title = "Orphaned Entities Detected"
        message = f"{count} activit{'y' if count == 1 else 'ies'} with orphaned entities: {', '.join(new_activity_names)}"

        _LOG.info(
            "Sending orphaned entities notification: title='%s', message='%s'",
            title,
            message,
        )
        try:
            await self._service.send_all(settings, title, message, priority=1)
            # Update tracked activities
            self._notified_orphaned_activities.update(new_activity_ids)
            self._save_notification_state()
            _LOG.info("Sent orphaned entities notification for %d activities", count)
        except Exception as e:
            _LOG.error("Failed to send orphaned entities notification: %s", e)

    def clear_orphaned_activities(self, activity_ids: list[str]) -> None:
        """
        Clear orphaned activity notifications that have been resolved.

        :param activity_ids: List of activity IDs that no longer have orphaned entities
        """
        removed = False
        for aid in activity_ids:
            if aid in self._notified_orphaned_activities:
                self._notified_orphaned_activities.discard(aid)
                removed = True
        if removed:
            self._save_notification_state()

    def clear_update_notification(self, driver_id: str, version: str) -> None:
        """
        Clear the update notification tracking for an integration.

        Call this when a user updates an integration to a new version.

        :param driver_id: Driver ID of the integration
        :param version: Version that was updated to
        """
        notification_key = f"{driver_id}:{version}"
        if notification_key in self._notified_updates:
            self._notified_updates.discard(notification_key)
            self._save_notification_state()  # Persist to disk

    def update_registry_count(
        self, integration_data: list[tuple[str, str]]
    ) -> list[str]:
        """
        Update the registry tracking and return new integrations if any detected.

        :param integration_data: List of tuples (integration_id, integration_name)
        :return: List of integration names that are new (empty if none)
        """
        settings = self._load_settings()
        current_ids = {item[0] for item in integration_data}
        known_ids = set(settings._known_integration_ids)

        # Find new integrations - but only notify if we had known integrations before
        # (skip notification on first run when known_ids is empty)
        new_ids = current_ids - known_ids

        if (
            new_ids and known_ids
        ):  # Only notify if we have a baseline to compare against
            _LOG.info("Detected %d new integration(s) in registry", len(new_ids))

            # Get the names of the new integrations
            id_to_name = {item[0]: item[1] for item in integration_data}
            new_names = [id_to_name[new_id] for new_id in new_ids]

            # Update the stored list of known IDs
            settings._known_integration_ids = list(current_ids)
            settings._last_registry_count = len(current_ids)
            settings.save()

            return new_names

        # Update tracking (first run or no new integrations)
        if known_ids != current_ids:
            settings._known_integration_ids = list(current_ids)
            settings._last_registry_count = len(current_ids)
            settings.save()
            if not known_ids:
                _LOG.debug(
                    "First run: initialized registry tracking with %d integrations",
                    len(current_ids),
                )

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
        # Try to get the current event loop
        try:
            loop = asyncio.get_running_loop()
            # We're in an async context, create a task
            loop.create_task(coro_func(*args, **kwargs))
        except RuntimeError:
            # No running loop, use asyncio.run()
            asyncio.run(coro_func(*args, **kwargs))
    except Exception as e:
        _LOG.error("Failed to send notification: %s", e)
