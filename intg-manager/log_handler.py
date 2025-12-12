"""
Ring Buffer Log Handler.

This module provides a custom logging handler that stores log messages
in an in-memory ring buffer, automatically discarding the oldest messages
when the buffer is full.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

# Maximum number of log entries to keep
MAX_LOG_ENTRIES = 200


@dataclass
class LogEntry:
    """A single log entry."""

    timestamp: str
    level: str
    logger: str
    message: str

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }


class RingBufferHandler(logging.Handler):
    """
    A logging handler that stores log records in a ring buffer.

    Thread-safe implementation that automatically discards oldest
    entries when the buffer reaches MAX_LOG_ENTRIES.
    """

    def __init__(self, max_entries: int = MAX_LOG_ENTRIES):
        """
        Initialize the handler.

        :param max_entries: Maximum number of log entries to store
        """
        super().__init__()
        self._buffer: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """
        Store the log record in the ring buffer.

        DEBUG level messages are excluded.

        :param record: The log record to store
        """
        # Skip DEBUG level messages
        if record.levelno <= logging.DEBUG:
            return

        try:
            entry = LogEntry(
                timestamp=datetime.fromtimestamp(record.created).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                level=record.levelname,
                logger=record.name,
                message=self.format(record),
            )
            with self._lock:
                self._buffer.append(entry)
        except Exception:
            # Don't let logging failures crash the application
            self.handleError(record)

    def get_entries(self, limit: int | None = None) -> list[LogEntry]:
        """
        Get log entries from the buffer.

        :param limit: Maximum number of entries to return (None for all)
        :return: List of log entries (newest first)
        """
        with self._lock:
            entries = list(self._buffer)

        # Reverse to show newest first
        entries.reverse()

        if limit is not None:
            entries = entries[:limit]

        return entries

    def clear(self) -> None:
        """Clear all entries from the buffer."""
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        """Return the number of entries in the buffer."""
        with self._lock:
            return len(self._buffer)


# Global handler instance
_handler: RingBufferHandler | None = None


def setup_log_handler() -> RingBufferHandler:
    """
    Set up the global ring buffer log handler.

    This attaches a handler to the root logger that captures all
    log messages from the application (excluding DEBUG level).

    :return: The configured handler instance
    """
    global _handler

    if _handler is not None:
        return _handler

    _handler = RingBufferHandler()
    _handler.setLevel(logging.INFO)  # Minimum INFO level
    _handler.setFormatter(logging.Formatter("%(message)s"))

    # Attach to root logger to capture all log messages
    root_logger = logging.getLogger()
    root_logger.addHandler(_handler)

    return _handler


def get_log_handler() -> RingBufferHandler | None:
    """
    Get the global ring buffer log handler.

    :return: The handler instance, or None if not set up
    """
    return _handler


def get_log_entries(limit: int | None = None) -> list[LogEntry]:
    """
    Get log entries from the global handler.

    :param limit: Maximum number of entries to return (None for all)
    :return: List of log entries (newest first), or empty list if not set up
    """
    if _handler is None:
        return []
    return _handler.get_entries(limit)
