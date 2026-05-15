"""Tests for SystemMessage dataclass and SystemMessagesService business logic."""

import os
import sys
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import system_messages as sm  # noqa: E402
from system_messages import SystemMessage  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(messages=(), read_ids=()):
    """Create a SystemMessagesService with injected state, no file I/O."""
    with (
        patch.object(sm.SystemMessagesService, "_load_messages", lambda self: None),
        patch.object(sm.SystemMessagesService, "_load_read_status", lambda self: None),
    ):
        svc = sm.SystemMessagesService()
    svc._messages = list(messages)
    svc._read_message_ids = set(read_ids)
    return svc


def _msg(id_, date="2024-01-01", title="T", content="C", priority="normal"):
    return SystemMessage(
        id=id_, date=date, title=title, content=content, priority=priority
    )


# ---------------------------------------------------------------------------
# SystemMessage dataclass
# ---------------------------------------------------------------------------


def test_system_message_required_fields():
    msg = SystemMessage(id="1", date="2024-03-15", title="Hello", content="World")
    assert msg.id == "1"
    assert msg.date == "2024-03-15"
    assert msg.title == "Hello"
    assert msg.content == "World"


def test_system_message_default_priority():
    msg = _msg("1")
    assert msg.priority == "normal"


def test_system_message_custom_priority():
    msg = SystemMessage(
        id="1", date="2024-01-01", title="T", content="C", priority="critical"
    )
    assert msg.priority == "critical"


# ---------------------------------------------------------------------------
# Empty service
# ---------------------------------------------------------------------------


def test_empty_service_all_messages():
    svc = _make_service()
    assert svc.get_all_messages() == []


def test_empty_service_unread():
    svc = _make_service()
    assert svc.get_unread_messages() == []


def test_empty_service_read():
    svc = _make_service()
    assert svc.get_read_messages() == []


def test_empty_service_count():
    svc = _make_service()
    assert svc.get_unread_count() == 0


# ---------------------------------------------------------------------------
# Sorting – newest first
# ---------------------------------------------------------------------------


def test_get_all_messages_sorted_newest_first():
    msgs = [
        _msg("1", date="2024-01-01"),
        _msg("2", date="2024-06-01"),
        _msg("3", date="2024-03-01"),
    ]
    svc = _make_service(msgs)
    result = svc.get_all_messages()
    assert [m.id for m in result] == ["2", "3", "1"]


def test_get_unread_messages_sorted_newest_first():
    msgs = [
        _msg("1", date="2024-01-01"),
        _msg("2", date="2024-06-01"),
        _msg("3", date="2024-03-01"),
    ]
    svc = _make_service(msgs)
    result = svc.get_unread_messages()
    assert [m.id for m in result] == ["2", "3", "1"]


def test_get_read_messages_sorted_newest_first():
    msgs = [
        _msg("1", date="2024-01-01"),
        _msg("2", date="2024-06-01"),
        _msg("3", date="2024-03-01"),
    ]
    svc = _make_service(msgs, read_ids=["1", "2", "3"])
    result = svc.get_read_messages()
    assert [m.id for m in result] == ["2", "3", "1"]


# ---------------------------------------------------------------------------
# Filtering – read vs unread
# ---------------------------------------------------------------------------


def test_unread_excludes_read_messages():
    msgs = [_msg("1"), _msg("2"), _msg("3", date="2024-02-01")]
    svc = _make_service(msgs, read_ids=["1"])
    unread_ids = {m.id for m in svc.get_unread_messages()}
    assert unread_ids == {"2", "3"}


def test_read_only_includes_read_messages():
    msgs = [_msg("1"), _msg("2"), _msg("3")]
    svc = _make_service(msgs, read_ids=["2"])
    read_ids = {m.id for m in svc.get_read_messages()}
    assert read_ids == {"2"}


def test_all_messages_read():
    msgs = [_msg("1"), _msg("2")]
    svc = _make_service(msgs, read_ids=["1", "2"])
    assert svc.get_unread_messages() == []
    assert len(svc.get_read_messages()) == 2


def test_no_messages_read():
    msgs = [_msg("1"), _msg("2")]
    svc = _make_service(msgs)
    assert len(svc.get_unread_messages()) == 2
    assert svc.get_read_messages() == []


# ---------------------------------------------------------------------------
# get_unread_count
# ---------------------------------------------------------------------------


def test_unread_count_none_read():
    svc = _make_service([_msg("1"), _msg("2"), _msg("3")])
    assert svc.get_unread_count() == 3


def test_unread_count_some_read():
    svc = _make_service([_msg("1"), _msg("2"), _msg("3")], read_ids=["1"])
    assert svc.get_unread_count() == 2


def test_unread_count_all_read():
    svc = _make_service([_msg("1"), _msg("2")], read_ids=["1", "2"])
    assert svc.get_unread_count() == 0


# ---------------------------------------------------------------------------
# mark_messages_as_read
# ---------------------------------------------------------------------------


def test_mark_messages_as_read_updates_state():
    svc = _make_service([_msg("1"), _msg("2")])
    with patch.object(svc, "_save_read_status"):
        svc.mark_messages_as_read(["1"])
    assert svc.get_unread_count() == 1
    assert svc.get_read_messages()[0].id == "1"


def test_mark_messages_as_read_saves_when_new():
    svc = _make_service([_msg("1")])
    with patch.object(svc, "_save_read_status") as mock_save:
        svc.mark_messages_as_read(["1"])
    mock_save.assert_called_once()


def test_mark_messages_as_read_idempotent():
    """Marking an already-read message does not trigger another save."""
    svc = _make_service([_msg("1")], read_ids=["1"])
    with patch.object(svc, "_save_read_status") as mock_save:
        svc.mark_messages_as_read(["1"])
    mock_save.assert_not_called()


def test_mark_messages_as_read_multiple():
    svc = _make_service([_msg("1"), _msg("2"), _msg("3")])
    with patch.object(svc, "_save_read_status"):
        svc.mark_messages_as_read(["1", "3"])
    assert svc.get_unread_count() == 1
    assert svc.get_unread_messages()[0].id == "2"


def test_mark_messages_as_read_unknown_id_ignored():
    """Marking an ID that doesn't correspond to a message is still tracked."""
    svc = _make_service([_msg("1")])
    with patch.object(svc, "_save_read_status"):
        svc.mark_messages_as_read(["999"])
    # The phantom ID is in the read set but doesn't affect unread messages count
    assert svc.get_unread_count() == 1
