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
CONSECUTIVE_THRESHOLD = 6


class NotificationManager:
    """
    Manages notification sending based on configured triggers.

    This class checks user preferences and sends notifications
    to all enabled providers when specific events occur.
    """

    def __init__(self, remote_id: str, remote_name: str = "") -> None:
        """Initialize the notification manager.

        :param remote_id: The remote identifier this manager is for
        :param remote_name: Friendly display name of the remote
        """
        self._remote_id = remote_id
        self._remote_name = remote_name
        self._service = NotificationService()
        # Track what we've already notified about to avoid spam
        self._notified_updates: set[str] = set()  # {driver_id:version}
        self._notified_errors: dict[str, str] = {}  # {driver_id: error_state}
        self._consecutive_errors: dict[str, int] = {}  # {driver_id: count}
        self._notified_orphaned_activities: set[str] = set()  # {activity_id}
        self._notified_firmware_versions: set[str] = set()  # {version}
        # Load persisted notification state from disk
        self._load_notification_state()

    def _load_notification_state(self) -> None:
        """Load notification state from manager.json file."""
        try:
            if os.path.exists(MANAGER_DATA_FILE):
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                    # v2.0 format - get from this remote's notification_state
                    notification_state = (
                        data.get("remotes", {})
                        .get(self._remote_id, {})
                        .get("notification_state", {})
                    )

                    self._notified_updates = set(
                        notification_state.get("notified_updates", [])
                    )
                    self._notified_errors = notification_state.get(
                        "notified_errors", {}
                    )
                    self._consecutive_errors = notification_state.get(
                        "consecutive_errors", {}
                    )
                    self._notified_orphaned_activities = set(
                        notification_state.get("notified_orphaned_activities", [])
                    )
                    self._notified_firmware_versions = set(
                        notification_state.get("notified_firmware_versions", [])
                    )
                    _LOG.debug(
                        "[%s] Loaded notification state: %d updates, %d errors, %d orphaned activities",
                        self._remote_id,
                        len(self._notified_updates),
                        len(self._notified_errors),
                        len(self._notified_orphaned_activities),
                    )
        except (json.JSONDecodeError, OSError) as e:
            _LOG.warning(
                "[%s] Failed to load notification state: %s", self._remote_id, e
            )

    def _save_notification_state(self) -> None:
        """Save notification state to manager.json file."""
        try:
            # Load existing data to preserve other sections
            existing_data: dict[str, Any] = {}
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

            # Ensure remote entry exists
            if self._remote_id not in existing_data.get("remotes", {}):
                _LOG.warning(
                    "[%s] Remote not found in manager.json, creating entry",
                    self._remote_id,
                )
                if "remotes" not in existing_data:
                    existing_data["remotes"] = {}
                existing_data["remotes"][self._remote_id] = {}

            # Update notification state for this remote
            existing_data["remotes"][self._remote_id]["notification_state"] = {
                "notified_updates": list(self._notified_updates),
                "notified_errors": self._notified_errors,
                "consecutive_errors": self._consecutive_errors,
                "notified_orphaned_activities": list(
                    self._notified_orphaned_activities
                ),
                "notified_firmware_versions": list(self._notified_firmware_versions),
            }

            # Ensure directory exists
            os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)

            # Write to disk
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.debug(
                "[%s] Saved notification state to %s",
                self._remote_id,
                MANAGER_DATA_FILE,
            )
        except OSError as e:
            _LOG.error("[%s] Failed to save notification state: %s", self._remote_id, e)

    def _load_settings(self) -> NotificationSettings:
        """Load current notification settings."""
        return NotificationSettings.load(remote_id=self._remote_id)

    def _should_notify(self, settings: NotificationSettings) -> bool:
        """Check if any notification provider is enabled."""
        return settings.is_any_enabled()

    def _remote_title(self, title: str) -> str:
        """Prefix a notification title with the remote name."""
        if self._remote_name:
            return f"{self._remote_name}: {title}"
        return title

    def _remote_data(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return data dict enriched with remote identity fields."""
        result = dict(data) if data else {}
        result["remote_id"] = self._remote_id
        if self._remote_name:
            result["remote_name"] = self._remote_name
        return result

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

        title = self._remote_title("Integration Update Available")
        message = f"{integration_name} can be updated from {current_version} to {latest_version}"

        _LOG.info("Sending notification: title='%s', message='%s'", title, message)
        try:
            await self._service.send_all(
                settings, title, message, data=self._remote_data()
            )
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

        Uses consecutive failure tracking to avoid false positives during
        brief disconnections (e.g., during upgrades). Requires 6 consecutive
        error checks (~180 seconds) before sending notification.

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

        # Increment consecutive error counter (but cap at threshold to prevent unbounded growth)
        current_count = self._consecutive_errors.get(driver_id, 0)
        if current_count < CONSECUTIVE_THRESHOLD:
            self._consecutive_errors[driver_id] = current_count + 1
            _LOG.debug(
                "Integration %s in error state %s - count %d/%d, not notifying yet",
                integration_name,
                state,
                self._consecutive_errors[driver_id],
                CONSECUTIVE_THRESHOLD,
            )
            self._save_notification_state()
            return

        # At this point, current_count >= CONSECUTIVE_THRESHOLD, so we can notify
        # Only notify if this is a new error or the error state changed
        if self._notified_errors.get(driver_id) == state:
            return

        title = self._remote_title("Integration Error")
        message = f"{integration_name} has entered an error state: {state}"

        _LOG.debug(
            "Sending error notification: title='%s', message='%s'", title, message
        )
        try:
            await self._service.send_all(
                settings, title, message, data=self._remote_data(), priority=1
            )
            self._notified_errors[driver_id] = state
            self._save_notification_state()  # Persist to disk
            _LOG.info("Sent error state notification for %s", integration_name)
        except Exception as e:
            _LOG.error("Failed to send error state notification: %s", e)

    def clear_error_state(self, driver_id: str) -> None:
        """
        Clear the error state tracking for an integration.

        Call this when an integration recovers from an error state.
        Also resets the consecutive error counter.

        :param driver_id: Driver ID of the integration
        """
        changed = False
        if self._notified_errors.pop(driver_id, None) is not None:
            changed = True
        if self._consecutive_errors.pop(driver_id, None) is not None:
            changed = True
        if changed:
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

        title = self._remote_title("Orphaned Entities Detected")
        message = f"{count} activit{'y' if count == 1 else 'ies'} with orphaned entities: {', '.join(new_activity_names)}"

        _LOG.info(
            "Sending orphaned entities notification: title='%s', message='%s'",
            title,
            message,
        )
        try:
            await self._service.send_all(
                settings, title, message, data=self._remote_data(), priority=1
            )
            # Update tracked activities
            self._notified_orphaned_activities.update(new_activity_ids)
            self._save_notification_state()
            _LOG.info("Sent orphaned entities notification for %d activities", count)
        except Exception as e:
            _LOG.error("Failed to send orphaned entities notification: %s", e)

    async def notify_firmware_update(
        self,
        installed_version: str,
        available_version: str,
        title: str,
    ) -> None:
        """
        Notify when a firmware update is available for the remote.

        Only sends once per available version.

        :param installed_version: Currently installed firmware version
        :param available_version: Version available for update
        :param title: Update title from the API
        """
        await self._notify_firmware_update(
            notification_key=available_version,
            device_name="Remote",
            installed_version=installed_version,
            available_version=available_version,
            title=title,
        )

    async def notify_dock_firmware_update(
        self,
        dock_id: str,
        installed_version: str,
        available_version: str,
        title: str,
    ) -> None:
        """Notify when a firmware update is available for a dock."""
        await self._notify_firmware_update(
            notification_key=f"dock:{dock_id}:{available_version}",
            device_name=f"Dock {dock_id}",
            installed_version=installed_version,
            available_version=available_version,
            title=title,
            data={"device_type": "dock", "dock_id": dock_id},
        )

    async def _notify_firmware_update(
        self,
        notification_key: str,
        device_name: str,
        installed_version: str,
        available_version: str,
        title: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Send one firmware update notification per device and version."""
        settings = self._load_settings()
        if (
            not self._should_notify(settings)
            or not settings.triggers.firmware_update_available
        ):
            return

        if notification_key in self._notified_firmware_versions:
            _LOG.debug(
                "[%s] Already notified for %s firmware version %s",
                self._remote_id,
                device_name,
                available_version,
            )
            return

        notification_title = self._remote_title("Firmware Update Available")
        message = (
            f"{device_name} firmware update {title} ({available_version}) is available. "
            f"Currently installed: {installed_version}"
        )

        _LOG.info(
            "[%s] Sending firmware update notification: %s -> %s",
            self._remote_id,
            installed_version,
            available_version,
        )
        try:
            await self._service.send_all(
                settings,
                notification_title,
                message,
                data=self._remote_data(
                    {
                        "device_type": "remote",
                        "installed_version": installed_version,
                        "available_version": available_version,
                        **(data or {}),
                    }
                ),
            )
            self._notified_firmware_versions.add(notification_key)
            self._save_notification_state()
            _LOG.info(
                "[%s] Sent firmware update notification for %s",
                self._remote_id,
                available_version,
            )
        except Exception as e:
            _LOG.error(
                "[%s] Failed to send firmware update notification: %s",
                self._remote_id,
                e,
            )

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
        # Load registry tracking from shared data (v2.0)
        try:
            if os.path.exists(MANAGER_DATA_FILE):
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                    # v2.0 format - load from shared.registry_tracking
                    shared = data.get("shared", {})
                    registry_tracking = shared.get("registry_tracking", {})
                    known_ids = set(registry_tracking.get("_known_integration_ids", []))
            else:
                known_ids = set()
        except (json.JSONDecodeError, OSError) as e:
            _LOG.warning("Failed to load registry tracking: %s", e)
            known_ids = set()

        current_ids = {item[0] for item in integration_data}

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

            # Update the stored list of known IDs in shared registry_tracking
            self._save_registry_tracking(list(current_ids), len(current_ids))

            return new_names

        # Update tracking (first run or no new integrations)
        if known_ids != current_ids:
            self._save_registry_tracking(list(current_ids), len(current_ids))
            if not known_ids:
                _LOG.debug(
                    "First run: initialized registry tracking with %d integrations",
                    len(current_ids),
                )

        return []

    def _save_registry_tracking(self, known_ids: list[str], count: int) -> None:
        """Save registry tracking to shared data."""
        try:
            # Load existing data
            existing_data = {}
            if os.path.exists(MANAGER_DATA_FILE):
                try:
                    with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            # Ensure v2.0 structure exists
            if existing_data.get("version") != "2.0":
                existing_data["version"] = "2.0"
                if "shared" not in existing_data:
                    existing_data["shared"] = {}

            # Update registry tracking in shared section
            if "registry_tracking" not in existing_data["shared"]:
                existing_data["shared"]["registry_tracking"] = {}

            existing_data["shared"]["registry_tracking"]["_known_integration_ids"] = (
                known_ids
            )
            existing_data["shared"]["registry_tracking"]["_last_registry_count"] = count

            # Ensure directory exists
            os.makedirs(os.path.dirname(MANAGER_DATA_FILE), exist_ok=True)

            # Write to disk
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
            _LOG.debug("Saved registry tracking: %d integrations", count)
        except OSError as e:
            _LOG.error("Failed to save registry tracking: %s", e)


# Global notification manager instance
_notification_managers: dict[str, NotificationManager] = {}


def get_notification_manager(
    remote_id: str | None = None, remote_name: str = ""
) -> NotificationManager:
    """Get the notification manager instance for a remote.

    :param remote_id: Remote identifier. If None, returns manager for first available remote.
    :param remote_name: Friendly display name of the remote (used to prefix notification titles).
    :return: NotificationManager instance for the specified remote
    """
    global _notification_managers

    # If no remote_id provided, use first available remote from manager.json
    if remote_id is None:
        if os.path.exists(MANAGER_DATA_FILE):
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    remotes = data.get("remotes", {})
                    if remotes:
                        remote_id = next(iter(remotes.keys()))
                    else:
                        _LOG.error("No remotes found in manager.json")
                        # Create a default manager with empty remote_id
                        remote_id = ""
            except (json.JSONDecodeError, OSError) as e:
                _LOG.error("Failed to read manager.json: %s", e)
                remote_id = ""
        else:
            remote_id = ""

    # Get or create manager for this remote
    if remote_id not in _notification_managers:
        _notification_managers[remote_id] = NotificationManager(remote_id, remote_name)
    elif remote_name and _notification_managers[remote_id]._remote_name != remote_name:
        # Update name if it changed (e.g. remote was renamed)
        _notification_managers[remote_id]._remote_name = remote_name

    return _notification_managers[remote_id]


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
