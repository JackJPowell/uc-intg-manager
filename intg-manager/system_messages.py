"""System messages service for user notifications and announcements.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import certifi
import json
import logging
import requests
from dataclasses import dataclass
from datetime import datetime

from const import SYSTEM_MESSAGES_FILE, SYSTEM_MESSAGES_URL, MANAGER_DATA_FILE

_LOG = logging.getLogger(__name__)
@dataclass
class SystemMessage:
    """Represents a system message."""

    id: str
    """Unique identifier for the message."""

    date: str
    """ISO format date string (YYYY-MM-DD)."""

    title: str
    """Message title/subject."""

    content: str
    """Message content (supports HTML)."""

    priority: str = "normal"
    """Priority level: 'low', 'normal', 'high', 'critical'."""


class SystemMessagesService:
    """Service for managing system messages."""

    def __init__(self):
        """Initialize the system messages service."""
        self._messages: list[SystemMessage] = []
        self._read_message_ids: set[str] = set()
        self._load_messages()
        self._load_read_status()

    def _load_messages(self) -> None:
        """Load system messages from file."""
        try:
            with open(SYSTEM_MESSAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._messages = [
                    SystemMessage(**msg) for msg in data.get("messages", [])
                ]
                _LOG.debug("Loaded %d system messages", len(self._messages))
        except FileNotFoundError:
            _LOG.debug("System messages file not found, starting with empty list")
            self._messages = []
        except Exception as e:
            _LOG.error("Failed to load system messages: %s", e)
            self._messages = []

    def _load_read_status(self) -> None:
        """Load read message IDs from manager.json."""
        try:
            with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._read_message_ids = set(data.get("read_message_ids", []))
                _LOG.debug("Loaded %d read message IDs", len(self._read_message_ids))
        except FileNotFoundError:
            _LOG.debug("Manager data file not found, no messages marked as read")
            self._read_message_ids = set()
        except Exception as e:
            _LOG.error("Failed to load read message status: %s", e)
            self._read_message_ids = set()

    def _save_read_status(self) -> None:
        """Save read message IDs to manager.json."""
        try:
            # Load existing data
            try:
                with open(MANAGER_DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = {}

            # Update read_message_ids
            data["read_message_ids"] = list(self._read_message_ids)

            # Save back to file
            with open(MANAGER_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                
            _LOG.debug("Saved %d read message IDs", len(self._read_message_ids))
        except Exception as e:
            _LOG.error("Failed to save read message status: %s", e)

    def get_all_messages(self) -> list[SystemMessage]:
        """
        Get all messages sorted by date (newest first).

        :return: List of all system messages
        """
        return sorted(
            self._messages,
            key=lambda m: datetime.fromisoformat(m.date),
            reverse=True,
        )

    def get_unread_messages(self) -> list[SystemMessage]:
        """
        Get unread messages sorted by date (newest first).

        :return: List of unread system messages
        """
        unread = [m for m in self._messages if m.id not in self._read_message_ids]
        return sorted(
            unread,
            key=lambda m: datetime.fromisoformat(m.date),
            reverse=True,
        )

    def get_read_messages(self) -> list[SystemMessage]:
        """
        Get read messages sorted by date (newest first).

        :return: List of read system messages
        """
        read = [m for m in self._messages if m.id in self._read_message_ids]
        return sorted(
            read,
            key=lambda m: datetime.fromisoformat(m.date),
            reverse=True,
        )

    def get_unread_count(self) -> int:
        """
        Get count of unread messages.

        :return: Number of unread messages
        """
        return len([m for m in self._messages if m.id not in self._read_message_ids])

    def mark_messages_as_read(self, message_ids: list[str]) -> None:
        """
        Mark messages as read.

        :param message_ids: List of message IDs to mark as read
        """
        before_count = len(self._read_message_ids)
        self._read_message_ids.update(message_ids)
        after_count = len(self._read_message_ids)
        
        if after_count > before_count:
            self._save_read_status()
            _LOG.info(
                "Marked %d messages as read (total: %d)",
                after_count - before_count,
                after_count,
            )

    def reload_messages(self) -> None:
        """Reload messages from file (useful for refreshing from remote source)."""
        self._load_messages()

    def fetch_from_github(self) -> bool:
        """
        Fetch system messages from GitHub and update local file.

        :return: True if fetch was successful, False otherwise
        """
        try:
            _LOG.info("Fetching system messages from GitHub...")
            response = requests.get(
                SYSTEM_MESSAGES_URL,
                timeout=10,
                verify=certifi.where(),
            )
            response.raise_for_status()
            
            # Parse and validate the response
            data = response.json()
            if "messages" not in data:
                _LOG.warning("Invalid system messages format from GitHub")
                return False
            
            # Validate message structure
            try:
                messages = [SystemMessage(**msg) for msg in data["messages"]]
            except (TypeError, KeyError) as e:
                _LOG.error("Invalid message structure from GitHub: %s", e)
                return False
            
            # Save to local file as cache
            with open(SYSTEM_MESSAGES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            
            _LOG.info(
                "Successfully fetched and saved %d system messages from GitHub",
                len(messages),
            )
            
            # Reload messages into memory
            self._load_messages()
            return True
            
        except requests.RequestException as e:
            _LOG.warning("Failed to fetch system messages from GitHub: %s", e)
            return False
        except Exception as e:
            _LOG.error("Unexpected error fetching system messages: %s", e)
            return False


# Global instance
_service: SystemMessagesService | None = None


def get_system_messages_service() -> SystemMessagesService:
    """
    Get the global system messages service instance.

    :return: System messages service instance
    """
    global _service
    if _service is None:
        _service = SystemMessagesService()
    return _service
