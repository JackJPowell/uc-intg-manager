"""Activity cleanup validates missing entities and protects newly used entities."""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402


@pytest.fixture
def remote(monkeypatch):
    remote = SimpleNamespace(
        helpers=SimpleNamespace(remove_activity_entities=AsyncMock(return_value={})),
        api=SimpleNamespace(get_activity=AsyncMock(return_value={"options": {"included_entities": []}})),
    )
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: remote)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "living-room")
    monkeypatch.setattr(ws, "is_remote_online", lambda _: True)
    monkeypatch.setattr(ws, "_operation_states", {})
    monkeypatch.setattr(ws, "_operation_locks", {})
    return remote


def remove(body, kind="unused"):
    async def run():
        async with ws.app.test_request_context(
            f"/api/v1/diagnostics/{'orphaned-entities' if kind == 'orphaned' else 'unused-activity-entities'}/remove", method="POST", json=body
        ):
            handler = ws.remove_orphaned_activity_entity if kind == "orphaned" else ws.remove_unused_activity_entity
            result = await handler()
            response, status = result if isinstance(result, tuple) else (result, 200)
            return await response.get_json(), status
    return asyncio.run(run())


SELECTION = {"remoteId": "living-room", "activityId": "watch-tv", "entityId": "light.old"}


def test_removal_always_uses_unused_guard_and_verifies_result(remote):
    payload, status = remove(SELECTION)
    assert status == 200 and payload["data"]["removed"] is True
    remote.helpers.remove_activity_entities.assert_awaited_once_with(
        "watch-tv", ["light.old"], only_unused=True
    )
    remote.api.get_activity.assert_awaited_once_with("watch-tv")
    assert not ws._operation_state_for("living-room").in_progress


def test_entity_that_became_used_is_not_reported_as_removed(remote):
    remote.api.get_activity.return_value = {"options": {"included_entities": [{"entity_id": "light.old"}]}}
    payload, status = remove(SELECTION)
    assert status == 409
    assert payload["error"]["code"] == "entity_in_use"
    assert not ws._operation_state_for("living-room").in_progress


@pytest.mark.parametrize("body", [None, [], {}, {**SELECTION, "entityId": []}, {**SELECTION, "activityId": "  "}])
def test_invalid_selection_never_mutates_remote(remote, body):
    _, status = remove(body)
    assert status == 400
    remote.helpers.remove_activity_entities.assert_not_awaited()


def test_remote_change_rejects_stale_confirmation(remote):
    _, status = remove({**SELECTION, "remoteId": "bedroom"})
    assert status == 409
    remote.helpers.remove_activity_entities.assert_not_awaited()


def test_offline_remote_never_mutates(remote, monkeypatch):
    monkeypatch.setattr(ws, "is_remote_online", lambda _: False)
    _, status = remove(SELECTION)
    assert status == 503
    remote.helpers.remove_activity_entities.assert_not_awaited()


def test_partial_helper_failure_is_not_success_and_releases_lock(remote):
    remote.helpers.remove_activity_entities.side_effect = ws.UnfurledError("Write failed")
    payload, status = remove(SELECTION)
    assert status == 502
    assert "Rescan" in payload["error"]["message"]
    assert not ws._operation_state_for("living-room").in_progress


def test_removal_respects_existing_operation_lock(remote):
    state = ws._operation_state_for("living-room")
    state.in_progress = True
    state.acquired_at = ws.time.monotonic()
    _, status = remove(SELECTION)
    assert status == 409
    remote.helpers.remove_activity_entities.assert_not_awaited()


@pytest.mark.parametrize("query, expected_marks", [("?markRead=false", 0), ("", 1)])
def test_message_preview_preserves_unread_status(monkeypatch, query, expected_marks):
    marks = []
    message = SimpleNamespace(id="notice", title="Update", content="A new release", date="2026-11-21", priority="normal")
    service = SimpleNamespace(
        get_unread_messages=lambda: [message], get_read_messages=lambda: [],
        mark_messages_as_read=lambda ids: marks.append(ids),
    )
    monkeypatch.setattr(ws, "get_system_messages_service", lambda: service)

    async def run():
        async with ws.app.test_request_context(f"/api/v1/system-messages{query}"):
            response = await ws.api_v1_system_messages()
            return await response.get_json()
    payload = asyncio.run(run())
    assert payload["data"]["unread"][0]["id"] == "notice"
    assert len(marks) == expected_marks


def test_orphan_cleanup_rechecks_missing_status_then_removes_references(remote):
    remote.api.get_activity.side_effect = [
        {"options": {"included_entities": [{"entity_id": "light.old", "available": False}]}},
        {"options": {"included_entities": []}},
    ]
    payload, status = remove(SELECTION, "orphaned")
    assert status == 200 and payload["data"]["removed"] is True
    remote.helpers.remove_activity_entities.assert_awaited_once_with(
        "watch-tv", ["light.old"], only_unused=False
    )
    assert remote.api.get_activity.await_count == 2
    assert not ws._operation_state_for("living-room").in_progress


@pytest.mark.parametrize("included", [
    [], [{"entity_id": "light.old"}],
    [{"entity_id": "light.old", "available": True}],
    [{"entity_id": "other.entity", "available": False}],
])
def test_recovered_or_unrelated_entity_cannot_use_orphan_cleanup(remote, included):
    remote.api.get_activity.return_value = {"options": {"included_entities": included}}
    payload, status = remove(SELECTION, "orphaned")
    assert status == 409 and payload["error"]["code"] == "entity_not_orphaned"
    remote.helpers.remove_activity_entities.assert_not_awaited()
    assert not ws._operation_state_for("living-room").in_progress


def test_failed_orphan_check_never_calls_removal(remote):
    remote.api.get_activity.side_effect = ws.UnfurledError("Remote unavailable")
    _, status = remove(SELECTION, "orphaned")
    assert status == 502
    remote.helpers.remove_activity_entities.assert_not_awaited()
    assert not ws._operation_state_for("living-room").in_progress


def test_client_cannot_disable_unused_guard(remote):
    _, status = remove({**SELECTION, "only_unused": False})
    assert status == 200
    remote.helpers.remove_activity_entities.assert_awaited_once_with(
        "watch-tv", ["light.old"], only_unused=True
    )


def test_orphan_removal_that_cannot_be_verified_is_not_success(remote):
    remote.api.get_activity.return_value = {"options": {"included_entities": [{"entity_id": "light.old", "available": False}]}}
    payload, status = remove(SELECTION, "orphaned")
    assert status == 409 and payload["error"]["code"] == "entity_removal_incomplete"


@pytest.mark.parametrize("kind", ["orphaned", "unused"])
def test_failed_entity_scan_returns_error_without_partial_results(remote, kind):
    # The updated unfurled scanners raise even when earlier activities produced
    # findings. The manager must preserve that failure rather than return a
    # successful empty/partial activity list.
    helper_name = "find_orphaned_entities" if kind == "orphaned" else "find_unused_activity_entities"
    setattr(remote.helpers, helper_name, AsyncMock(side_effect=ws.UnfurledError("Activity scan failed midway")))

    async def run():
        path = "orphaned-entities" if kind == "orphaned" else "unused-activity-entities"
        async with ws.app.test_request_context(f"/api/v1/diagnostics/{path}"):
            handler = ws.get_orphaned_entities if kind == "orphaned" else ws.get_unused_activity_entities
            response, status = await handler()
            return await response.get_json(), status

    payload, status = asyncio.run(run())
    assert status == 502
    assert payload["error"]["code"] == f"{kind}_entities_unavailable"
    assert "failed midway" in payload["error"]["message"]
    assert "data" not in payload
